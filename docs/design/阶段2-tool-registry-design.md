# 阶段 2 - Tool Registry 设计文档

> 创建于 2026-05-02。**这是 T11 (实现) 和 T12 (LLM 循环改造) 的实施依据。** 写代码前先看这份。

---

## 0. 目标

让 AITD 的 LLM agent 能"主动调用工具"按需获取数据，而不是 prompt 里一股脑塞所有东西。

第一个工具：`get_funding_rate(symbol)`。后续可扩展任何"模型按需取数"的能力。

---

## 1. 模块边界

```
backend/tools.py            ← T11 实现
  ├─ ToolDefinition         数据类
  ├─ ToolContext            数据类
  ├─ ToolResult             数据类
  ├─ ToolRegistry           注册表
  ├─ register_tool(...)     装饰器（注册到 default registry）
  ├─ execute_tool(...)      执行入口（永不抛异常）
  ├─ default_registry       全局唯一注册表实例
  └─ get_funding_rate(...)  第一个工具实现

backend/tests/test_tools.py ← T11 测试

backend/llm.py              ← T12 在这里读 default_registry 接入循环
backend/engine.py           ← T12 调用循环 + 决策记录追踪
```

**T10 只负责设计接口，不写代码。** T11 负责实现 + 单测。T12 负责接入 LLM 循环。

---

## 2. 核心数据结构

### 2.1 `ToolDefinition`

```python
@dataclass(frozen=True)
class ToolDefinition:
    name: str
    description: str           # 英文 — 模型对英文 tool desc 理解更准
    parameters_schema: dict    # JSON Schema 形式（properties dict）
    executor: Callable[[dict, "ToolContext"], "ToolResult"]
    required_params: tuple[str, ...] = ()
```

**为什么 frozen=True**：防止意外修改、可 hash、可放进 set。

**为什么 description 强制英文**：实测 DeepSeek/OpenAI/Claude 对英文 description 的工具选择准确率显著高于中文 description（中文偶尔会混淆相似工具）。工具的实际执行结果可以是中文/英文混合。

### 2.2 `ToolContext`

```python
@dataclass(frozen=True)
class ToolContext:
    """Runtime info that every tool can rely on. Tools NEVER reach for globals."""
    instance_id: str | None
    cycle_id: str | None
    network_settings: dict        # 让工具知道代理设置
    settings: dict                # 完整 trading_settings.json 内容
    exchange_id: str | None       # 当前活跃交易所 (binance / okx / ...)
```

**为什么有 ToolContext**：让工具实现可被单元测试 mock。工具内部不应该 `import config; read_network_settings()`，那样测试就要起一整套环境。Context 显式注入是 Python 测试友好的写法。

**Context 由谁构造**：T12 LLM 调用循环每次进入工具循环时构造一次，传给所有工具。

### 2.3 `ToolResult`

```python
@dataclass(frozen=True)
class ToolResult:
    ok: bool
    data: dict | None = None
    error: str | None = None
    hint: str | None = None       # 给模型的"下一步建议"
    meta: dict | None = None      # 元信息：执行耗时、来源等。**默认不喂给模型，仅用于决策记录追踪**

    @classmethod
    def success(cls, data: dict, *, hint: str | None = None, meta: dict | None = None) -> "ToolResult":
        return cls(ok=True, data=data, hint=hint, meta=meta)

    @classmethod
    def failure(cls, error: str, *, hint: str | None = None, meta: dict | None = None) -> "ToolResult":
        return cls(ok=False, error=error, hint=hint, meta=meta)

    def to_model_text(self) -> str:
        """Compact JSON suitable for the LLM tool result message.
        Excludes `meta` to save tokens — meta lives in decision audit trail only."""
        payload = {"ok": self.ok}
        if self.data is not None:
            payload["data"] = self.data
        if self.error is not None:
            payload["error"] = self.error
        if self.hint is not None:
            payload["hint"] = self.hint
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
```

**关于 meta**：执行耗时、调用了哪个交易所、走了哪个 baseUrl 等元信息，**仅用于决策追踪记录**（trade-cycle JSON 的 toolCallTrace 字段），**不喂给模型**避免增加 token 又对决策无价值。

---

## 3. ToolRegistry

```python
class ToolRegistry:
    def __init__(self):
        self._tools: dict[str, ToolDefinition] = {}

    def register(self, tool: ToolDefinition) -> None:
        if tool.name in self._tools:
            raise ValueError(
                f"Tool {tool.name!r} already registered. "
                f"Tool names must be unique across the registry."
            )
        self._tools[tool.name] = tool

    def get(self, name: str) -> ToolDefinition | None:
        return self._tools.get(name)

    def names(self) -> list[str]:
        return sorted(self._tools.keys())

    def openai_tools_schema(self) -> list[dict]:
        """Convert all registered tools to the OpenAI tools=[...] format."""
        return [{
            "type": "function",
            "function": {
                "name": t.name,
                "description": t.description,
                "parameters": {
                    "type": "object",
                    "properties": t.parameters_schema,
                    "required": list(t.required_params),
                },
            },
        } for t in self._tools.values()]


default_registry = ToolRegistry()
```

**全局单例 `default_registry`** 是为了让装饰器注册零样板。需要测试隔离时，可以新建一个临时 `ToolRegistry()` 实例传给 `execute_tool`。

---

## 4. 注册装饰器

```python
def register_tool(
    *, name: str, description: str, parameters: dict,
    required: list[str] | None = None,
    registry: ToolRegistry | None = None,
):
    """Decorator. Register a function as a callable tool.

    Example:
        @register_tool(
            name="get_funding_rate",
            description="Query current funding rate ...",
            parameters={"symbol": {"type": "string", "description": "..."}},
            required=["symbol"],
        )
        def get_funding_rate(args: dict, ctx: ToolContext) -> ToolResult:
            ...
    """
    target = registry or default_registry

    def decorator(func):
        tool = ToolDefinition(
            name=name,
            description=description,
            parameters_schema=parameters,
            executor=func,
            required_params=tuple(required or []),
        )
        target.register(tool)
        return func

    return decorator
```

---

## 5. 执行入口

```python
def execute_tool(
    name: str,
    args: dict,
    context: ToolContext,
    *,
    registry: ToolRegistry | None = None,
) -> ToolResult:
    """
    Run a registered tool. **Never raises.** Always returns a ToolResult,
    even if the tool internals crash. This invariant lets the LLM loop
    treat every tool result uniformly.
    """
    target = registry or default_registry
    tool = target.get(name)
    if tool is None:
        return ToolResult.failure(
            error=f"unknown tool: {name!r}",
            hint=f"available tools: {', '.join(target.names())}",
        )
    # Validate required params before calling
    missing = [p for p in tool.required_params if p not in args]
    if missing:
        return ToolResult.failure(
            error=f"missing required parameters: {', '.join(missing)}",
            hint=f"tool {tool.name!r} requires {list(tool.required_params)}",
        )
    # Call the tool — any exception becomes a structured failure
    started = time.monotonic()
    try:
        result = tool.executor(args, context)
    except Exception as exc:
        return ToolResult.failure(
            error=f"{type(exc).__name__}: {exc}",
            meta={"durationSeconds": time.monotonic() - started},
        )
    if not isinstance(result, ToolResult):
        # Defensive: tool author returned wrong type
        return ToolResult.failure(
            error=f"tool {tool.name!r} returned {type(result).__name__}, expected ToolResult",
        )
    # Inject duration into meta if not already present
    if result.meta is None:
        result = ToolResult(
            ok=result.ok, data=result.data, error=result.error, hint=result.hint,
            meta={"durationSeconds": time.monotonic() - started},
        )
    return result
```

**关键不变量**：
- `execute_tool` 永不 raise
- 总返回 `ToolResult`
- 总附带 meta.durationSeconds（即使是失败）
- 失败时给模型可读的 error + 可选 hint

---

## 6. 第一个工具：`get_funding_rate`

```python
@register_tool(
    name="get_funding_rate",
    description=(
        "Query the current funding rate for a perpetual futures symbol. "
        "Returns the most recent funding rate (as a fraction, e.g. 0.0054 = 0.54%) "
        "and the next funding time. Use this when you need fresher funding data "
        "than what's already in the candidates context, or to look up symbols "
        "outside the candidate universe."
    ),
    parameters={
        "symbol": {
            "type": "string",
            "description": (
                "Symbol in the active exchange's native format. "
                "For Binance: BTCUSDT, ETHUSDT. "
                "For OKX: BTC-USDT-SWAP, ETH-USDT-SWAP. "
                "The tool will normalize common variants automatically."
            ),
        },
    },
    required=["symbol"],
)
def get_funding_rate(args: dict, ctx: ToolContext) -> ToolResult:
    from .market import fetch_premium  # avoid circular import at module load
    symbol = str(args.get("symbol") or "").strip().upper()
    if not symbol:
        return ToolResult.failure("symbol must be a non-empty string")
    try:
        premium = fetch_premium(symbol, ctx.exchange_id)
    except Exception as exc:
        return ToolResult.failure(
            error=f"fetch_premium failed: {exc}",
            hint="check that the symbol is valid for the active exchange",
        )
    return ToolResult.success({
        "symbol": premium.get("symbol") or symbol,
        "fundingRate": premium.get("fundingRate"),
        "fundingPct": premium.get("fundingPct"),
        "nextFundingTime": premium.get("nextFundingTime"),
        "markPrice": premium.get("markPrice"),
    }, meta={"exchange": ctx.exchange_id})
```

**为什么这样选字段**：
- `fundingRate` 原始 fraction（保留精度）
- `fundingPct` 百分比形式（模型推理时更直观）
- `nextFundingTime` 让模型判断"还有多久要支付"
- `markPrice` 顺便给（已经从同一个 API 拿到，零成本）

---

## 7. T12 (LLM 循环) 怎么用这一切

```python
# 伪代码，T12 实现时参考
from .tools import default_registry, execute_tool, ToolContext

def llm_with_tools_loop(
    initial_prompt: str,
    provider: dict,
    *, max_rounds: int = 5,
    instance_id: str, cycle_id: str,
    settings: dict, exchange_id: str | None,
):
    ctx = ToolContext(
        instance_id=instance_id,
        cycle_id=cycle_id,
        network_settings=read_network_settings(instance_id),
        settings=settings,
        exchange_id=exchange_id,
    )
    messages = [{"role": "user", "content": initial_prompt}]
    tools_schema = default_registry.openai_tools_schema()
    trace = []

    for round_idx in range(max_rounds + 1):
        # 最后一轮强制 tools=None (no_more_tools), 让模型出最终决策
        is_final_round = round_idx == max_rounds
        response = call_llm(messages, provider, tools=None if is_final_round else tools_schema)
        msg = response["choices"][0]["message"]
        finish = response["choices"][0].get("finish_reason")

        if finish != "tool_calls" or is_final_round:
            return {"final": msg, "trace": trace}

        # 执行所有 tool_calls
        messages.append(msg)  # assistant message must come right after
        for call in msg["tool_calls"]:
            args = json.loads(call["function"]["arguments"] or "{}")
            result = execute_tool(call["function"]["name"], args, ctx)
            messages.append({
                "role": "tool",
                "tool_call_id": call["id"],
                "content": result.to_model_text(),
            })
            trace.append({
                "round": round_idx,
                "tool": call["function"]["name"],
                "args": args,
                "result": result.to_model_text(),
                "meta": result.meta,  # 不进 messages，只进 trace
            })
```

**T12 实施时**：把这个伪代码替换 backend/llm.py 里的 generate_trading_decision，并保留旧的"无工具"路径作为 `enableToolCalls: false` 时的 fallback。

---

## 8. 测试用例清单（T11 必须覆盖的最小集）

按 T11 任务约定 ≥ 25 个测试，分布如下：

### 8.1 ToolResult (5 个)
- success / failure factory 正确
- to_model_text() 是紧凑 JSON
- to_model_text() 不包含 meta 字段
- to_model_text() 在中文字符下不转义（ensure_ascii=False）
- frozen 不可修改

### 8.2 ToolRegistry (5 个)
- 注册 + get 往返
- 重复注册抛 ValueError
- names() 排序
- openai_tools_schema() 格式正确（含 type/function/parameters/required）
- 空 registry 返回空列表

### 8.3 register_tool 装饰器 (3 个)
- 装饰后函数本身仍可调用
- 装饰会自动加入 default_registry
- 自定义 registry 参数生效

### 8.4 execute_tool (10 个)
- 正常执行成功
- 未知工具名返回 failure + 列出可用工具
- 缺必填参数返回 failure + 列出 required
- 工具内部 raise → 包装成 failure，error 含异常类型
- 工具返回非 ToolResult → 防御失败
- meta.durationSeconds 总是被填入
- 自定义 registry 隔离生效
- ctx 正确传给工具
- 多余参数不报错（保留扩展性）
- args=None 安全处理（视为空 dict）

### 8.5 get_funding_rate (5 个)
- 正常路径（mock fetch_premium）
- symbol 空字符串 → failure
- symbol 缺失 → failure（execute_tool 层拦截）
- fetch_premium 抛异常 → failure 不崩溃
- 返回字段齐全（symbol/fundingRate/fundingPct/nextFundingTime/markPrice）

总计 28 个测试。

---

## 9. 开放设计决策（已确认）

| # | 问题 | 决策 |
|---|---|---|
| 1 | description 中文还是英文 | **英文** |
| 2 | 成功 result 是否带元信息 | **加可选 `meta` 字段；不喂模型，只入 trace** |
| 3 | 同 cycle 重复调用同一工具 | **允许；只限制总轮次** |
| 4 | 异步工具 | **暂不支持；同步即可** |
| 5 | 结果格式 dict 还是 str | **内部 dict (ToolResult.data)；序列化时 str (to_model_text)** |
| 6 | 命名冲突 | **register 时 raise** |

---

## 10. 风险与缓解

| 风险 | 缓解 |
|---|---|
| 工具 import 顺序导致循环依赖 | get_funding_rate 内 lazy import (`from .market import fetch_premium`) |
| 模型偶发 args JSON parse 失败 | T12 try/except 包装，转 failure 喂回模型让它自己重试 |
| 工具执行卡死 (网络挂) | 沿用阶段 1 的 http_client 重试机制 + 上层 timeout（T12 控制） |
| meta 字段未来膨胀污染 trace | 约定只放执行耗时、来源、版本等"低基数"信息，不放原始响应 |
| 测试时不想触发真实 fetch_premium | execute_tool 接受自定义 registry，单测注入 mock 工具 |
