# LLM 工具调用学习笔记

> 适合长期复习。三个关键词搞清楚就能看懂 90% 的"AI agent"项目：**Function Calling**、**Skill**、**MCP**。
>
> 创建于 2026-05-02，配合 AITD 项目实战。

---

## 0. 一句话总览

| 名称 | 是什么 | 谁定的 | 解决什么问题 |
|---|---|---|---|
| **Function Calling** | 一种**协议** | OpenAI 最早提出，现在是事实标准 | 让模型能调用应用预定义的函数 |
| **Skill** | 一种**目录约定** | Anthropic | 把"领域知识 + 子工具 + 示例"打包成可复用单元 |
| **MCP** | 一种**开放协议** | Anthropic（2024 年提出） | 把工具/资源/prompt 标准化为外部服务，跨应用复用 |

**层次关系**：

```
应用层      ┌──── Skill 包（知识 + 工具的打包）
           │
能力扩展   ─┼──── Function Calling（应用内嵌工具）
           │
连接层      └──── MCP（跨应用、跨进程的工具/资源）
```

可以同时使用：一个 skill 内部既可以包含 function calling 工具，也可以引用 MCP server 提供的能力。

---

## 1. Function Calling 详解

### 1.1 概念

让模型能"调用应用提供的函数"，模型不会真的执行，只是返回"我想调用这个函数 + 这些参数"，应用执行后把结果喂回去，模型继续推理。

### 1.2 完整流程（必背）

```
┌────────────┐   1. messages + tools schema       ┌──────────┐
│            │ ────────────────────────────────►  │          │
│  应用      │                                    │  模型    │
│  (AITD)    │ ◄────────────────────────────────  │  (LLM)   │
│            │   2. tool_calls (我想调这个工具)   │          │
│            │                                    │          │
│  3. 自己   │                                    │          │
│  执行工具  │                                    │          │
│            │                                    │          │
│            │   4. messages 追加 tool result     │          │
│            │ ────────────────────────────────►  │          │
│            │                                    │          │
│            │ ◄────────────────────────────────  │          │
└────────────┘   5. 要么再要工具调用 → 回 2       └──────────┘
                 要么 finish_reason=stop → 出最终答案
```

### 1.3 一个最小例子（OpenAI 兼容 API，DeepSeek/OpenAI/Qwen 通用）

**第 1 步：声明工具 schema**

```python
tools = [{
    "type": "function",
    "function": {
        "name": "get_funding_rate",
        "description": "查询永续合约的资金费率",
        "parameters": {
            "type": "object",
            "properties": {
                "symbol": {
                    "type": "string",
                    "description": "交易对，如 BTCUSDT"
                }
            },
            "required": ["symbol"]
        }
    }
}]
```

**第 2 步：第一次调模型**

```python
import requests

response = requests.post(
    "https://api.deepseek.com/v1/chat/completions",
    headers={"Authorization": "Bearer YOUR_KEY"},
    json={
        "model": "deepseek-v4-pro",
        "messages": [
            {"role": "user", "content": "BTC 现在的资金费率是多少？"}
        ],
        "tools": tools,
    }
).json()
```

**第 3 步：模型返回 tool_calls**

```json
{
  "choices": [{
    "message": {
      "role": "assistant",
      "content": "",
      "tool_calls": [{
        "id": "call_abc123",
        "type": "function",
        "function": {
          "name": "get_funding_rate",
          "arguments": "{\"symbol\": \"BTCUSDT\"}"
        }
      }]
    },
    "finish_reason": "tool_calls"
  }]
}
```

注意 `finish_reason="tool_calls"` —— 这是关键标志，告诉应用模型想调工具。

**第 4 步：应用执行工具**

```python
import json

# 解析模型的请求
call = response["choices"][0]["message"]["tool_calls"][0]
args = json.loads(call["function"]["arguments"])

# 真正执行
result = my_get_funding_rate(args["symbol"])  # 比如返回 0.0054
```

**第 5 步：把结果喂回模型，继续对话**

```python
# 注意：需要把第一轮的 assistant message 也加入历史
response2 = requests.post(
    "https://api.deepseek.com/v1/chat/completions",
    json={
        "model": "deepseek-v4-pro",
        "messages": [
            {"role": "user", "content": "BTC 现在的资金费率是多少？"},
            response["choices"][0]["message"],  # 模型上一轮的请求
            {
                "role": "tool",
                "tool_call_id": call["id"],
                "content": json.dumps({"symbol": "BTCUSDT", "fundingRate": 0.0054})
            }
        ],
        "tools": tools,
    }
).json()

# 模型这一轮通常会 finish_reason="stop"，content 里是最终回答
print(response2["choices"][0]["message"]["content"])
# → "BTCUSDT 当前资金费率是 0.54%，多头需要支付空头..."
```

### 1.4 关键设计要点（实战经验）

| 要点 | 说明 |
|---|---|
| **轮次上限** | 必须设上限（比如 5 轮），否则模型可能无限循环 |
| **并行调用** | 现代模型一次可以请求多个工具，`tool_calls` 是数组 |
| **失败兜底** | 工具报错时，把错误信息作为 `role: "tool"` 的 content 喂回，让模型自己决定怎么应对 |
| **token 预算** | 每轮工具结果都会增加 prompt，要监控总 token 不要爆窗口 |
| **历史追加顺序** | `assistant` 的 tool_calls message 必须紧跟在前一轮之后，然后是 `tool` role 的 result |
| **tool_call_id 必须对上** | result 的 `tool_call_id` 必须等于请求里的 `id`，否则模型会 confuse |

### 1.5 各家支持情况（截至 2026-05）

| 厂商 | 支持 | 注意 |
|---|---|---|
| OpenAI | ✅ 全系列 | gpt-4o 体验最好 |
| Anthropic Claude | ✅ 全系列 | API 字段名略不同（用 `tool_use` 而非 `tool_calls`） |
| DeepSeek | ✅ 含 v4-pro 推理模型 | 实测推理模型也支持，会先在 reasoning_content 里思考再调用 |
| Qwen | ✅ Max / Plus / Turbo | 兼容 OpenAI 格式 |
| Gemini | ✅ 1.5+ | 字段名不同，需要适配层 |
| OpenAI o1/o3 系列 | ❌ | 推理模型，不支持 function calling |

**坑**：不要假设所有推理模型都不支持 tool calling。OpenAI o 系列不支持，但 DeepSeek v4-pro / r1 支持。

---

## 2. Anthropic Skill 详解

### 2.1 这不是协议，是组织方式

Skill = 一个**文件夹约定**，把以下东西打包成可加载单元：

```
skills/elliott-wave-analysis/
├── SKILL.md              # 触发条件、操作步骤、示例
├── reference/
│   ├── wave-rules.md     # 领域知识
│   └── examples.md       # 历史案例
└── helpers/
    └── count_waves.py    # 子工具脚本
```

### 2.2 SKILL.md 的标准结构（Anthropic 约定）

```markdown
---
name: elliott-wave-analysis
description: 用艾略特波浪理论分析价格结构。
              触发条件：用户询问"波浪""数浪""主升""调整浪"
---

## 何时使用

当价格结构出现 5 浪推动 + 3 浪调整的形态时...

## 操作步骤

1. 标出最近的高低点
2. 数出 1-5 浪是否符合规则（1 浪和 4 浪不重叠等）
3. 给出当前所处位置的判断

## 子工具

- count_waves(prices): 自动数浪
- check_wave_rules(waves): 验证是否合规

## 示例

[历史案例 1...]
[历史案例 2...]
```

### 2.3 与 Function Calling 的关系

Skill 是**更高层**的组织：

```
Skill（领域知识 + 操作流程 + 子工具）
    └─ 子工具用 Function Calling 实现
```

也就是说，Skill 不是"替代" Function Calling，而是"包装" Function Calling，让模型在某个领域有更系统的能力。

### 2.4 在非 Anthropic 模型（如 DeepSeek）里如何模拟 Skill

DeepSeek API 里**没有 skill 原生支持**。可以这样模拟：

**方案 A：静态加载（最简单）**
- 把 SKILL.md 内容直接塞进 system prompt
- 缺点：所有 skill 一开始就全部加载，浪费 token

**方案 B：按需加载（推荐）**
- 提供一个 meta-tool：`load_skill(skill_name)`
- 模型在需要时主动调用，应用把 SKILL.md 作为 tool result 返回
- 之后模型就"获得"了这个 skill 的能力
- 类似 lazy loading

**方案 C：路由分发**
- 在 prompt 拼装时根据当前任务的关键词决定加载哪些 skill
- 比如检测到"数浪"就加载 elliott-wave-analysis skill

### 2.5 Skill 在哪里有原生支持

| 平台 | 状态 |
|---|---|
| Claude Desktop | ✅ 原生 |
| Cowork | ✅ 原生 |
| Claude Code | ✅ 原生 |
| OpenAI / DeepSeek API | ❌ 需要应用自己模拟 |

---

## 3. MCP（Model Context Protocol）详解

### 3.1 为什么需要 MCP

**痛点**：每个 AI 应用都要自己写一遍工具实现。

- ChatGPT 想读你的 Notion → ChatGPT 团队写一遍
- Claude 想读你的 Notion → Anthropic 团队写一遍
- Cursor 想读你的 Notion → Cursor 团队写一遍

**MCP 的解决方案**：定义**开放协议**，让"工具/资源/prompt"以**标准服务**的形式暴露。任何兼容 MCP 的客户端都能接入。

### 3.2 架构

```
┌─────────────┐     stdio / SSE / HTTP      ┌─────────────┐
│             │ ◄──────────────────────────►│             │
│  MCP Client │                              │ MCP Server  │
│  (AI 应用)  │   list_tools / call_tool /  │ (工具提供方)│
│             │   list_resources / read     │             │
└─────────────┘                              └─────────────┘
```

**MCP Server**（提供能力）：
- Notion、GitHub、Slack、文件系统、数据库... 都已有官方/社区 MCP server
- 自己也可以写一个（用 Python/TypeScript SDK）

**MCP Client**（消费能力）：
- Claude Desktop、Cursor、Cowork、Continue 等都内置了
- 一个客户端可以同时连多个 MCP server

### 3.3 与 Function Calling 的关系

**MCP 是 Function Calling 的"上游"**：

```
MCP Server 暴露工具
    ↓
MCP Client 列出工具（list_tools）
    ↓
转换成 Function Calling 的 tools schema
    ↓
喂给 LLM（OpenAI 兼容协议）
    ↓
LLM 决定调用 → tool_call
    ↓
Client 调用 MCP Server（call_tool）
    ↓
结果回灌给 LLM
```

也就是说，**MCP 不取代 function calling**，它是工具的**来源**和**生命周期管理**机制。

### 3.4 MCP 在 AITD 里怎么用

DeepSeek API **不直接支持** MCP（MCP 是 Anthropic 的协议），所以要在 AITD 里用，必须 AITD 自己当 MCP client：

**最简方案**（适合 AITD）：

```python
# 伪代码
from mcp_python_sdk import MCPClient

# 1. 启动一个 MCP server（比如 web search）
client = MCPClient.connect("npx @modelcontextprotocol/server-brave-search")

# 2. 拿到 server 提供的所有工具
mcp_tools = client.list_tools()

# 3. 翻译成 OpenAI function calling schema
openai_tools = [translate_to_openai_schema(t) for t in mcp_tools]

# 4. 加入 AITD 的 tool registry，模型按 function calling 用法调用
# 5. 当模型调用某个 mcp 来源的工具时，AITD 转发给 MCP client
```

这就完成了"AITD 通过 function calling 协议 → 间接使用 MCP 生态"的桥接。

### 3.5 现成的 MCP server（生态）

可以直接拿来用的（不完全列表）：

| 类别 | Server |
|---|---|
| 数据 | postgres、sqlite、google-drive |
| 协作 | slack、github、notion、linear |
| 信息 | brave-search、puppeteer（爬网页）、sequential-thinking |
| 文件 | filesystem、git |
| 自动化 | playwright、everything-search |

完整目录：[modelcontextprotocol.io/servers](https://modelcontextprotocol.io/servers)

### 3.6 MCP 的劣势

- **复杂度**：需要 MCP SDK + 进程管理 + 协议转换
- **延迟**：跨进程通信比直接 function call 慢
- **生态成熟度**：2024 年才提出，2026 年才在快速发展
- **DeepSeek/OpenAI API 层未原生支持**：必须客户端自己适配

---

## 4. 三者对比速查表

| 维度 | Function Calling | Skill | MCP |
|---|---|---|---|
| **本质** | 协议 | 目录约定 | 协议 |
| **作者** | OpenAI | Anthropic | Anthropic |
| **范围** | 单次 API 调用 | 一组知识+工具的打包 | 跨应用工具/资源服务 |
| **运行位置** | 应用进程内 | 应用进程内 | 独立进程（可远程） |
| **复用性** | 应用内 | 同平台跨任务 | 跨应用、跨语言 |
| **学习成本** | 低 | 低（写 markdown 即可） | 中（要懂协议 + SDK） |
| **DeepSeek 支持** | ✅ 原生 | ❌ 需模拟 | ❌ 需适配层 |
| **OpenAI API 支持** | ✅ 原生 | ❌ 需模拟 | 部分（产品端有，API 端开发中） |
| **Claude API 支持** | ✅ 原生（不同字段名） | ✅ 部分 | ✅ 原生（Claude Desktop 等） |
| **典型用法** | 让模型查数据库、调 API | 包装领域知识让模型按系统流程做事 | 接入第三方服务（GitHub、Notion 等） |

---

## 5. 在 AITD 项目里的演进路径

| 阶段 | 时间 | 引入的能力 | 用什么技术 |
|---|---|---|---|
| 1 | 2026-05-02 已完成 | K 线 CSV + token 监控 | 纯重构 |
| **2** | **进行中** | **模型主动调用工具**（资金费率、订单簿、历史盈亏等）| **Function Calling** |
| 3 | 待定 | 视觉模型读 K 线图 | 多模态 API（换 provider） |
| 4 | 待定 | Skill 包系统（自定义） | 模拟 Anthropic Skill（markdown + meta-tool） |
| 5 | 待定 | 接入外部数据源（新闻、链上数据、社交情绪） | MCP client 适配层 |

**关键认知**：阶段 2 完成后，所有后续阶段都是建立在工具循环之上的扩展：
- 阶段 3 = 工具的输入扩展（接受图片）
- 阶段 4 = 工具的组合（多个工具 + 知识）
- 阶段 5 = 工具的来源（从 MCP server 获取）

---

## 6. 推荐学习资源

### Function Calling
- OpenAI 官方文档：[platform.openai.com/docs/guides/function-calling](https://platform.openai.com/docs/guides/function-calling)
- DeepSeek 官方文档：[api-docs.deepseek.com/guides/function_calling](https://api-docs.deepseek.com/guides/function_calling)
- Anthropic Tool Use：[docs.anthropic.com/claude/docs/tool-use](https://docs.anthropic.com/claude/docs/tool-use)

### Skill
- Anthropic Skills 文档（在 Claude / Cowork / Claude Code 里随时可读）
- Anthropic Cookbook 里的 skill 案例

### MCP
- 官方主页：[modelcontextprotocol.io](https://modelcontextprotocol.io)
- 协议规范：[modelcontextprotocol.io/specification](https://modelcontextprotocol.io/specification)
- Python SDK：[github.com/modelcontextprotocol/python-sdk](https://github.com/modelcontextprotocol/python-sdk)
- 现成 server 列表：[modelcontextprotocol.io/servers](https://modelcontextprotocol.io/servers)

---

## 7. 词汇表

| 术语 | 含义 |
|---|---|
| **Tool / Function** | 应用提供给模型的可调用函数 |
| **Tool Call** | 模型请求"我想调用这个函数 + 参数" |
| **Tool Result** | 应用执行后给模型的返回值 |
| **finish_reason** | 模型本轮停止的原因：`stop` / `length` / `tool_calls` |
| **Schema** | 工具的描述（名字、参数类型、用途）—— 用 JSON Schema 格式 |
| **Tool Loop / Agent Loop** | 模型 ↔ 应用 多轮交互直到 `finish_reason=stop` 的循环 |
| **System Prompt** | 整个对话最前面的固定指令 |
| **Context Window** | 模型能"看到"的总 token 上限（DeepSeek v4-pro 是 128k） |
| **MCP Server** | 用 MCP 协议暴露工具/资源的进程 |
| **MCP Client** | 连接 MCP server 的应用（如 Claude Desktop） |
| **stdio Transport** | MCP 最常见的传输方式：通过子进程的标准输入输出通信 |

---

## 8. 思考题（自测）

1. 如果模型在一次 cycle 里调用了 3 次工具，应用应该把多少条 message 加入历史？
   <details><summary>答案</summary>
   每轮调用对应 2 条新 message：模型的 `assistant` message（含 tool_calls）+ 应用的 `tool` message（含结果）。3 轮调用 = 6 条新 message。
   </details>

2. 为什么 OpenAI 的 o1 不支持 function calling，DeepSeek v4-pro 却支持？
   <details><summary>答案</summary>
   纯架构选择，不是技术限制。Anthropic 和 DeepSeek 让推理模型同时支持工具调用，模型会先在 reasoning 阶段思考"我该用哪个工具"再发出 tool_call。OpenAI o1 把推理和工具调用做成了两个独立模式。
   </details>

3. Skill 和 MCP 都能打包"工具集"，区别是什么？
   <details><summary>答案</summary>
   Skill 是**应用进程内**的"知识 + 工具"打包，强调让模型按系统流程做事；MCP 是**跨进程/跨应用**的工具/资源标准服务化，强调外部能力的可复用接入。一个 Skill 内部可以引用 MCP server 提供的工具。
   </details>

4. 如果你想让 AITD 调用 Notion 数据库做交易日志，技术上有几种实现方式？
   <details><summary>答案</summary>
   (a) 自己用 Notion API 写一个 function calling 工具（最直接）；
   (b) 接入官方的 Notion MCP server，AITD 当 MCP client + 翻译成 function calling tools 喂给 DeepSeek（生态最优）；
   (c) 把 Notion 数据库的 schema 和操作方法做成一个 Skill 包让模型自学（最灵活但最复杂）。
   </details>
