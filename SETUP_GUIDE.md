# AITD 使用说明

这份文档按“第一次拿到这个文件夹、几乎不了解项目”的情况来写。

## 1. 这是什么

AITD 是一个本地运行的交易代理页面。

它会：
- 读取你配置的候选池
- 获取 Binance Futures 行情
- 把行情、账户、持仓、风控和你的交易逻辑拼成模型输入
- 调用外部模型 API 生成交易决策
- 在本地网页里展示账户、持仓、候选池、最近决策和日志

## 2. 运行要求

只需要：
- `Python 3.11+`
- 浏览器
- 网络连接

不需要：
- Node.js
- npm
- 额外 Python 第三方包

## 3. 如何启动

在项目根目录运行：

```bash
python3 run.py
```

如果你想指定端口，也可以这样运行：

```bash
python3 run.py --port 1234
```

终端会输出一个本地地址，例如：

```text
http://127.0.0.1:8788/trader.html
```

把这个地址复制到浏览器打开即可。

如果 `8788` 被占用，程序会自动切到附近端口。

## 4. 页面怎么理解

页面大致分成两部分：

- 左侧
  - 运行设置
  - AI模型配置
  - 代理配置
  - 实盘账号配置
- 右侧
  - `交易`
  - `Prompt`
  - `候选池`
  - `Log`

顶部可以：
- 切换 `模拟盘 / 实盘`
- 启动或暂停当前页面对应的交易循环
- 在深色和浅色主题之间切换

## 5. 最推荐的首次使用流程

1. 运行 `python3 run.py`
2. 打开网页
3. 保持在 `模拟盘` 页面
4. 在 `AI模型配置` 填写：
   - `Provider`
   - `Model`
   - `Base URL`
   - `API Key`
5. 如果需要代理，在 `代理配置` 里填写：
   - 是否启用代理
   - 代理地址
   - 不走代理的地址
6. 在 `Prompt` 页面填写交易逻辑
7. 在 `候选池` 页面选择：
   - `静态候选池`
   - 或 `动态候选池`
8. 点当前页面的 `启动交易`
9. 观察：
   - `交易` tab 是否出现最近决策
   - `Prompt` tab 的测试输出是否正常
   - `Log` tab 是否有错误

## 6. Prompt 怎么改

你不需要直接编辑原始 JSON。

前端里有 4 个输入项：
- `role`
- `core_principles`
- `entry_preferences`
- `position_management`

保存后，后端会自动拼成：

```text
config/trading_prompt.json
```

你只负责交易逻辑本身。

系统会自动拼进去的内容包括：
- 当前行情
- 当前账户权益
- 当前持仓
- 风控上限
- 当前模式

## 7. Prompt 测试怎么用

`Prompt` 页面有一个测试按钮。

它会：
- 用你当前页面里的 Prompt 内容
- 加上系统上下文
- 调用模型 API
- 返回测试输出

它不会真的发单。

如果这里测试都不通过，就先不要启动交易。

## 8. 候选池怎么用

候选池有两种模式，二选一。

### 静态候选池

你手动输入一组 symbols，例如：

```text
BTCUSDT
ETHUSDT
SOLUSDT
```

保存后生效。

### 动态候选池

你写一个 Python function：

```python
def load_candidate_symbols(context):
    return ["BTCUSDT", "ETHUSDT"]
```

这个函数会在每轮交易决策前自动执行。

你可以让它：
- 读本地文件
- 读本地数据库
- 请求外部 API

要求只有一个：
- `load_candidate_symbols` 最终返回 `list`

## 9. AI 模型配置

支持的 provider：
- GPT
- Claude
- DeepSeek
- Qwen
- 自定义 OpenAI 兼容接口

页面里 `Model` 默认是下拉列表，尽量避免拼写错误。

高级参数如：
- `Timeout`
- `Temperature`
- `Max Output Tokens`

默认是折叠的，按需展开即可。

## 10. 代理配置

代理配置会统一影响：
- Binance 公共行情请求
- Binance 实盘请求
- 模型 API 请求

如果你的网络不需要代理，可以保持关闭。

## 11. 模拟盘和实盘

### 模拟盘

- 默认更适合先测试
- 不会真的下单

### 实盘

只有同时满足下面条件才会真的下单：
- 当前在 `实盘` 页面
- `启用实盘` 已勾选
- `模拟下单` 未勾选
- `API Key / API Secret` 已填写

建议顺序：
1. 先把模拟盘跑通
2. 再填写 Binance 实盘凭证
3. 先用 `模拟下单`
4. 最后才开启真实实盘

## 12. 运行后哪些文件会变化

常见会更新的本地运行文件：
- `data/cache/...`
- `data/scans/latest.json`
- `data/trading_agent_state.json`
- `data/trading-agent/decisions/...`

这些都不建议提交到 GitHub。

## 13. 目录说明

- `run.py`
  - 启动本地服务
- `backend/`
  - Python 后端代码
- `dashboard/`
  - 前端页面
- `config/trading_agent.json`
  - 运行设置
- `config/llm_provider.json`
  - AI 模型配置
- `config/network.json`
  - 代理配置
- `config/live_trading.json`
  - 实盘账号配置
- `config/fixed_universe.json`
  - 静态候选池配置
- `config/candidate_source.py`
  - 动态候选池函数
- `config/trading_prompt.json`
  - 保存后的交易逻辑

## 14. 常见问题

### 页面里看到 “LLM API key is missing”

说明还没在 `AI模型配置` 里填模型 key。

### Prompt 测试失败

优先检查：
- `Provider`
- `Model`
- `Base URL`
- `API Key`
- 代理是否需要开启

### Live 页面没有真正下单

请检查：
- 当前是否在 `实盘` 页面
- `启用实盘` 是否已勾选
- `模拟下单` 是否已经关闭
- Binance `API Key / API Secret` 是否已填写

### 候选池在哪里改

有两种方式：
- 直接在页面的 `候选池` 里手动填写 symbols
- 启用动态候选池，在 `config/candidate_source.py` 里定义 `load_candidate_symbols(context)`

### 风控为什么不会跟着 Prompt 一起消失

这是故意的。

Prompt 只负责交易逻辑；
风险上限、持仓数量、回撤限制始终由 Python 后端强制控制。

---

## 15. 迭代记录与排坑经验

> 按用户偏好，每次重大改动 / 踩坑都记录在这里方便日后查阅。

| 日期 | 版本 | 变更 | 备注 |
|------|------|------|------|
| 2026-05-02 | 项目运行架构发现 | **关键事实**：AITD 真正运行时使用的配置**不在** `config/` 目录，而在 `data/instances/<instance_id>/` 下。`config/` 里只是首次安装时的模板。每个实例（`paper-default`、`live-default` 等）都有自己独立的 `network.json`、`fixed_universe.json`、`trading_settings.json`、`live_trading.json`、`llm_provider.json` 等。改配置一定要去对应实例目录里改，否则改了等于没改。实例清单在 `data/instances/index.json` | 排查 SSL EOF 时发现：手改 `config/network.json` 完全无效，必须改 `data/instances/live-default/network.json` 才生效 |
| 2026-05-02 | `backend/http_client.py` 重试机制 | 给 `request_text` 加了**安全的瞬时错误自动重试**：(1) SSL 握手错误（`SSLEOFError`/`UNEXPECTED_EOF_WHILE_READING`/`WRONG_VERSION_NUMBER` 等白名单）总是重试 —— 因为请求体根本没到服务器，对 POST 下单也安全；(2) GET 额外重试 timeout / `ConnectionResetError`（GET 幂等）；(3) **POST/PUT/DELETE 在非 SSL 错误时绝不重试**（避免重复下单！）；(4) `HTTPError`（4xx/5xx）永不重试（服务端已明确响应）。退避：`0.4s → 1.0s`，最多 3 次尝试 | 实盘跑 OKX 时偶发 `EOF occurred in violation of protocol (_ssl.c:1002)` 导致整个 trade-cycle fallback。curl 测同样请求 100% 成功是因为 curl 自带重试。修复后 9/9 单元+集成测试通过 |
| 2026-05-02 | `live-default/llm_provider.json` token 上限 | 把 `maxOutputTokens` 从 `1200` 提到 `8192`，`timeoutSeconds` 从 `45` 提到 `90` | 用 DeepSeek v4-pro 这类**推理模型**时报 `Model decision failed: empty JSON payload`。根因不是 prompt 没注入也不是模型故障，而是推理模型的 `completion_tokens = reasoning_tokens + content_tokens`，1200 token 全被 reasoning 吃光（`finish_reason: "length"`），`content` 字段是空的，AITD 解析时拿到空 JSON。**关键鉴别方法**：去 `data/instances/<id>/decisions/<日期>/trade-cycle-*.json` 看 `rawModelResponse.rawResponse.choices[0].message`，如果 `content` 是空但 `reasoning_content` 有内容且 `finish_reason` 是 `length`，就是 token 不够。**经验法则**：推理模型（DeepSeek v4-pro / r1 / reasoner、OpenAI o1/o3、Qwen-QwQ）`maxOutputTokens` 至少给 8192；非推理模型（deepseek-chat、gpt-4o）1200-2000 即可 |
| 2026-05-02 | **阶段 1 完成：K 线 CSV 化 + token 监控**（ROADMAP 阶段 1）| 三件大事打包：(1) 新建 `backend/prompt_format.py` 把 K 线从 verbose JSON 改成紧凑 CSV（表头 `t,o,h,l,c,v`，秒级时间戳，volume 取整，删除 closeTime 和 quoteVolume 两个冗余字段）；(2) `engine.py` 加 `klineFormat` 开关（`csv` / `json`，默认 csv，写在 `data/instances/<id>/trading_settings.json`），每次 build_prompt 在终端打印当前格式以便调试；(3) 新建 `backend/token_monitor.py` 4 档预算监控（OK<60% / WARN 60-85% / DANGER 85-95% / BLOCK ≥95%），BLOCK 档拒绝调用模型避免浪费 API 钱拿到必然被截断的输出。同时新增 `backend/tests/test_{prompt_format,token_monitor}.py` 共 40 个单元测试。**实测效果**（连续 5 cycle 回归）：prompt token 从 73,943 → 23,811（**节省 67.8%**），波动仅 9 token；模型决策质量零退化（5/5 reasonable summary、零 warning、零 SSL/JSON 间歇错误）。**回滚**：把 `klineFormat` 改成 `"json"`、重启 AITD | **关键发现**：(1) 候选池其实开了 `5m + 15m` 双周期 K 线，每个 symbol 96+64=160 根，5 symbol × 160 根是 prompt 主要膨胀来源；(2) 实际节省 67.8% 而非预测的 81%，差距来自 prompt 中非 K 线部分（账户/规则/合同等 ~8k tokens）固定不变 |

### 排错经验：SSL EOF 完整诊断链

**症状**：trade-cycle 报 `EOF occurred in violation of protocol (_ssl.c:1002)` + `Model decision failed: empty JSON payload` 或 `Fallback decision because model output was unavailable`

**容易误判的方向**（这些通常都不是原因）：
- ❌ Prompt 没注入 / Prompt 模板有问题
- ❌ LLM API key 失效
- ❌ 模型本身故障

**真正的诊断顺序**：
1. **先看错误消息前缀**：`DOGE-USDT-SWAP: EOF...` 表示是**取行情**那一步挂了；`https://api.xxx.com: EOF...` 表示是**调模型**那一步挂了。empty JSON 通常是上游 SSL 失败的下游连锁反应
2. **确认进程在用哪个实例配置**：进 `data/instances/<id>/network.json`，看 `proxyEnabled` 和 `proxyUrl` 是否真是你想要的（界面"已保存"提示不能盲信）
3. **用 curl 走同一个代理测试目标 API**：例如 `curl -x http://127.0.0.1:1082 https://www.okx.com/api/v5/market/ticker?instId=DOGE-USDT-SWAP`，连测 5-10 次。如果 curl 全 200 而 Python 偶发失败，就是 Python 客户端的 SSL/重试逻辑问题
4. **改完配置必须重启 `python3 run.py`**，因为很多配置只在启动时加载

### 命名陷阱

- `config/network.json` ≠ `data/instances/<id>/network.json`，前者是模板，后者是真实运行配置
- `BTCUSDT`（Binance）≠ `BTC-USDT-SWAP`（OKX）；fixed_universe 写哪种格式都行，OKX gateway 的 `normalize_symbol` 会自动转
- README 写「OKX (todo)」但 `backend/exchanges/okx.py` 已经完整实现了，**OKX 实际可用**
