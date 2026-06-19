# 好得 APP Agent 升级改进文档

> 背景：好得 APP 中的 agent 是较早期版本；PuddingClaw 当前 agent 已经过多轮上下文工程、工具输出治理、session 历史展示/入模解耦等迭代。本文记录两者差异、风险点和建议迁移顺序。

## 结论摘要

好得 APP 当前 agent 已具备基础产品化能力，包括多租户 workspace、LangChain `create_agent`、基础 session、RAG/mem0、skill 执行、SSE streaming 和简单历史压缩。

但它仍停留在“早期可用版”的上下文处理模型：

- 按消息数和粗略 token 阈值裁剪历史。
- 工具输出存在后端硬截断。
- session 压缩会影响前端可见历史。
- 历史 tool calls 没有完整还原给 LLM。
- 缺少入模前 middle trim、工具结果摘要、context usage 观测、cache-friendly middleware 等机制。
- 流式输出缺少对 LangGraph replay 的严格过滤，历史 chunk 可能被误保存为当前 assistant 回复。
- 上下文维护动作没有前端状态提示，用户容易把 tool 历史摘要/compaction 的耗时误认为模型延迟。

PuddingClaw 当前版本已经演进为“面向长会话和工具密集任务的 context-engineering agent”。迁移时应优先统一 session 数据模型和 SSE/tool output 契约，再逐层迁移 middleware。

## 差异对照

| 维度 | 好得 APP 现状 | PuddingClaw 现状 | 影响 |
| --- | --- | --- | --- |
| 上下文窗口策略 | 固定 60K warning / 80K critical；超限保后半段；最多 50 条历史 | 按 `context_window=1M` 配置，200K tail/middle trim，500K compaction | 好得长会话更容易丢中间任务状态 |
| session 展示/LLM 双轨 | `messages` 被压缩后前端也只能看到剩余 messages，archive 不自动回显 | `display_messages` 给前端完整历史，active `messages` 给 LLM，archive 兼容合并 | 好得压缩后用户历史可能不完整 |
| 中段裁剪摘要 | 无 middle trim | 入模前 `_maybe_middle_trim_session()` 做中段摘要归档 | 避免“用户请求还在、完成证据没了”导致 agent 误以为任务未完成 |
| 工具历史还原 | `_build_messages()` 主要还原 user/assistant 文本，未完整还原历史 `tool_calls`/`ToolMessage` | 还原 `AIMessage(tool_calls)` + `ToolMessage`，并给历史工具输出加防污染前缀 | 好得后续轮次看不到历史工具真实结果 |
| 工具调用协议兜底 | tool_start 后若流中断，可能保存缺 output 的 tool_call | 保存前补缺失 output；加载时为缺 output 的历史 tool_call 补 `ToolMessage`；工具异常时补 `tool_end(is_error=true)` | 防止下一轮触发 provider 400：`insufficient tool messages following tool_calls message` |
| 工具输出处理 | `tool_end` 直接 `[:2000]`；部分工具内部 `[:5000]` 硬截断 | 后端持久化完整输出或摘要；前端只拿 `output_preview` | 好得会真实丢工具输出，尤其 JSON/搜索/长报告风险高 |
| 单条超长工具摘要 | 有 `_summarize_tool_result()`，但主链路仍硬截断 | `single_tool_overflow` 超 20K tokens 实时摘要并标记来源 | PuddingClaw 对长工具结果更可控 |
| streaming replay 防护 | token 流接收 `AIMessageChunk or ai` | 只累计 `AIMessageChunk` 且 `metadata["langgraph_node"] == "model"`，避免 LangGraph replay 历史 AI/chunk 污染当前回复 | 好得存在上一轮内容混入当前输出并落盘的风险 |
| 连续 assistant 合并 | 普通合并容易忽略 tool_call 边界 | `load_session_for_agent()` 只合并前后都无 `tool_calls` 的普通 assistant 文本 | 防止最终回答被拼进上一条 tool-calling assistant |
| middleware pipeline | 基本没有 LangChain middleware 分层治理 | `cache_boundary -> tail_trim -> tool_clear -> summarization -> compaction -> skills_router -> task_state` | PuddingClaw 已形成分层上下文治理 |
| prefix cache 友好 | 动态工具分类会重建 agent，cache key 粗 | SkillsRouterMiddleware 注入路由提示，尽量不改 system prefix | 好得 DeepSeek prefix cache 命中率和稳定性较弱 |
| context usage 观测 | 无运行时 token usage/峰值持久化 | `stream_usage=True`，SSE `context_usage`，记录 `context_usage_peak` | 好得难以判断真实上下文消耗 |
| LLM 输入审计 | 缺少入模上下文快照 | `llm_input_logger` 记录 `pre_agent` / `model_request` 的 system/messages 摘要、长度和 hash | 能区分 LLM 输入污染、SSE 保存污染、前端展示污染 |
| 中间件耗时提示 | 压缩/摘要时只有普通 loading | `context_maintenance` SSE 事件，前端显示“正在整理历史工具结果...”并在 done 后消失 | 用户能理解是在维护上下文，不会误判模型卡死 |
| MCP | 未看到 agent 主链路 MCP | 支持 MCP 持久 session 加载工具 | 好得工具生态扩展能力较弱 |

## 关键风险

### 1. 历史工具输出污染当前回复

好得当前 streaming 中同时接受 `AIMessageChunk` 和完整 `ai` 消息作为 token 来源。LangGraph/LangChain streaming 中完整 `AIMessage` 可能是 graph state/replay 事件，历史 `AIMessageChunk` 也可能被回放，包含历史回复或历史工具摘要。若被当作当前 token 累计，就可能出现上一轮内容混入本轮回复，并被保存进 session。

PuddingClaw 已复现并修复过一个典型链路：

```text
用户新请求：整理最近一周的 AI 论文
  ↓
messages stream 回放上一任务历史 chunk：
搜索到15条专利，展示5条：比亚迪...
  ↓
后端误当作当前 token 累积
  ↓
session 保存为：
比亚迪专利摘要... + 好的，我来查询最近一周的 AI 论文信息。
  ↓
前端正常展示被污染的 session
```

该问题不是模型主动引用旧任务，也不是前端单独拼接错误；根因在后端流式消费阶段。

改进目标：

- 只把 `AIMessageChunk` 且 `metadata["langgraph_node"] == "model"` 的消息作为 token 流累计。
- 完整 `AIMessage` 只用于读取 tool calls、reasoning metadata 等结构化信息。
- graph replay、历史 chunk、tool/update/custom 事件都不能进入 `full_response`。
- 增加 LLM 输入日志和 raw session 对照排查：若 `llm-input` 干净但 `session.json` 污染，优先查 SSE token 保存逻辑。

### 1.1 连续 assistant 合并跨过 tool_call 边界

历史 session 中常见结构是：

```text
assistant: 我来调用工具... + tool_calls
tool: ...
assistant: 最终回答
```

如果 `load_session_for_agent()` 为了避免连续 assistant 而简单合并“当前无 tool_calls 的 assistant”，就可能把最终回答拼进上一条带 `tool_calls` 的 assistant，形成结构异常的入模消息。

改进目标：

- 只在“上一条 assistant 无 tool_calls 且当前 assistant 也无 tool_calls”时合并普通文本。
- 带 `tool_calls` 的 assistant 必须保持和对应 ToolMessage 的结构边界。
- 为该场景补回归测试：tool-calling assistant 后面的 final answer 不得被合并。

### 1.2 tool_start 后中断导致缺失 ToolMessage

工具调用在协议层不是普通 UI 状态，而是 LLM provider 要求严格配对的消息序列：

```text
assistant: content + tool_calls=[call_1, call_2]
tool: tool_call_id=call_1
tool: tool_call_id=call_2
```

如果 SSE 已经收到 `tool_start` 并把 tool call 放入当前 assistant segment，但后续 MCP/工具服务异常、客户端断开或后端流中断，`tool_end` 可能没有到达。若此时 partial save 直接把缺 `output` 的 `tool_calls` 写入 session，下一轮 `_build_messages()` 会生成：

```text
AIMessage(tool_calls=[call_1, call_2])
HumanMessage(...)
```

由于中间缺少对应 `ToolMessage`，DeepSeek/OpenAI 会直接返回 400：

```text
An assistant message with 'tool_calls' must be followed by tool messages
responding to each 'tool_call_id'. (insufficient tool messages following tool_calls message)
```

PuddingClaw 的当前兜底策略：

- **保存时兜底**：`chat.py` 保存 session 前检查每个 `tool_call`，缺 `output` 时补占位输出：
  `[工具执行失败/无返回] 工具调用已开始，但没有收到完成事件...`，并标记 `is_error=true`、`summary_source="missing_tool_output"`。
- **加载时兜底**：`agent.py::_build_messages()` 即使遇到历史脏数据，也会为缺 `output` 的 `tool_call` 补一个匹配 `tool_call_id` 的 `ToolMessage`。
- **工具层兜底**：工具异常时，对已经发出的 pending `tool_start` 补 `tool_end(is_error=true)`，让前端卡片闭合，也避免新脏数据落盘。

迁移到好得时，这三层要一起迁移。只修其中一层无法覆盖历史脏数据、流中断和工具服务异常三个入口。

### 2. 工具输出被后端硬截断

好得当前有多处硬截断：

- `agent.py` 的 `tool_end.output = str(tool_msg.content)[:2000]`。
- `execute_skill_tool.py` 中脚本输出超过 5000 字符会截断。
- `terminal_tool.py` 中命令输出超过 5000 字符会截断。

这会导致：

- LLM 后续无法看到完整工具结果。
- 前端结构化数据可能从截断文本里解析失败。
- session 历史无法审计完整工具输出。
- 长报告、搜索结果、JSON、专利数据等场景尤其容易失真。

改进目标：

- 后端 session 持久化完整工具输出，或持久化明确标记的摘要。
- SSE 给前端展示时使用 `output_preview`。
- 若输出超过单条阈值，使用 `single_tool_overflow` 摘要，而不是硬截断。

### 3. 压缩影响前端完整历史

好得当前 `compress_history()` 会把旧消息从 `messages` 移走并写入 archive，同时把摘要追加到 `compressed_context`。但 `load_session()` 返回的是当前 `messages`，没有自动合并 archive，也没有 `display_messages` 独立轨道。

这会导致：

- 用户在前端看到的历史可能被压缩后的 active messages 替代。
- 后续调试时难以从 session 文件直接还原用户视角。
- 摘要机制对用户不是完全无感。

改进目标：

- 增加 `display_messages`，作为用户可见完整历史轨道。
- `messages` 仅作为 LLM active context。
- `load_session()` 优先返回 `display_messages`；没有该字段时兼容合并 archive。
- 所有摘要、裁剪、归档都只影响 LLM context，不影响前端完整历史。

### 4. 中段历史缺口导致任务状态误判

按尾部保留或按固定消息数压缩，容易出现：

- 用户请求仍在上下文中。
- 工具调用、执行结果或最终回复被裁掉。
- agent 误以为任务未完成，重新执行旧任务。

改进目标：

- 引入入模前 middle trim preflight。
- 当 active messages 超过阈值时，保留头部和最近 tail，把中段摘要后归档。
- 摘要重点记录任务状态：已完成、失败、部分完成、未完成。
- tail 必须从 user message 边界开始，避免保留半截任务。

### 5. 中间件耗时被误认为模型卡顿

ToolResultClear、single_tool_overflow、Compaction 都可能调用 LLM 做摘要。它们发生在模型正式回复前或工具结束后，如果前端只显示普通 loading，用户会以为模型没有响应。

改进目标：

- 后端统一发 `context_maintenance` SSE 事件：
  - `status=start|done|error`
  - `phase=tool_result_clear|single_tool_overflow|compaction`
  - `message=正在整理历史工具结果...`
- 前端收到 `start` 后显示轻量状态行，收到 `done` 后消失。
- 该状态不作为聊天消息落盘，不影响输入禁用逻辑。
- `TailTrimMiddleware` 这类纯本地裁剪通常不提示，避免噪音。

## 建议迁移顺序

### P0：修复当前最容易复现的问题

1. 修 streaming replay：
   - 只累计 `AIMessageChunk` 且 `metadata["langgraph_node"] == "model"`。
   - 不把完整 `ai` 消息、历史 chunk、graph replay 作为当前 token。
   - 保存 session 的 `full_response` 必须来自过滤后的 token 流。

2. 移除后端工具输出硬截断：
   - `tool_end.output` 保留完整输出或摘要。
   - SSE 增加 `output_preview` 和 `output_full_length`。
   - session 持久化使用完整 `output`。

3. 跳过空 assistant segment：
   - `new_response` 或异常场景下，空 content 且无 tool_calls 的 segment 不落库。

4. 修复 assistant 合并边界：
   - `load_session_for_agent()` 只合并前后都不含 `tool_calls` 的普通 assistant 文本。
   - 带工具调用的 assistant 与后续最终回答保持独立消息。

5. 增加工具调用协议兜底：
   - session 保存前，缺 `output` 的 `tool_call` 必须补占位 output 并标记 `is_error=true`。
   - `_build_messages()` 加载历史时，缺 `output` 的 `tool_call` 必须补匹配 `tool_call_id` 的 `ToolMessage`。
   - 工具/MCP 异常时，已经发出 `tool_start` 的 pending tool call 必须补 `tool_end(is_error=true)`。
   - 回归测试覆盖：缺 output 的历史 session 不能再触发 `insufficient tool messages following tool_calls message`。

6. 加入入模日志：
   - 记录 `pre_agent` 和 `model_request` 的 message 类型、长度、hash、tool_call 概况。
   - 日志默认写入 `backend/logs/llm-input/YYYY-MM-DD.jsonl`。
   - 用于快速判断污染发生在 LLM 输入、SSE 保存还是前端展示。

### P1：统一 session 数据模型

1. 增加 `display_messages`：
   - 首次裁剪/压缩时初始化为完整历史。
   - 后续 `save_message()` 同时追加 active `messages` 和 `display_messages`。

2. 增加 active/full 读取接口：
   - `get_active_messages()`：仅返回 active `messages`。
   - `load_session()`：前端完整历史，优先 `display_messages`，否则合并 archive。
   - `load_session_for_agent()`：只返回摘要上下文 + active messages。

3. 增加 `middle_trim_context`：
   - 和 `compressed_context` 分离。
   - 多次触发时按时间追加，用 `---` 分隔。

### P2：引入 context-engineering middleware

建议按以下顺序迁移：

1. `ToolResultClearMiddleware`
   - 摘要历史 tool output。
   - 持久化回对应 `tool_call.output`。
   - 标记 `summary_source="tool_result_clear"`。

2. `TailTrimMiddleware`
   - 保前缀 + 保最近消息。
   - 中段整体裁剪。
   - 保护 `AIMessage(tool_calls)` 与 `ToolMessage` 原子配对。
   - tail 从 user 边界开始。

3. `CompactionMiddleware`
   - 超大上下文时触发全局摘要。
   - 归档旧 active messages。
   - 写入 `compressed_context`。

4. `SkillsRouterMiddleware`
   - 替代“动态工具分类后重建 agent”的模式。
   - 将路由提示注入最后一条 user message，尽量不破坏 system prefix cache。

### P3：增强观测和部署能力

1. 开启 `stream_usage=True`。
2. SSE 增加 `context_usage`。
3. session 写入 `context_usage_peak`。
4. token API 使用运行时峰值修正静态估算。
5. Docker 预置 SkillHub CLI 或项目依赖包，避免每次容器启动后手动安装。
6. SSE 增加 `context_maintenance`，让 ToolResultClear、single_tool_overflow、Compaction 的耗时在前端可见。
7. 前端把 context maintenance 显示为临时状态，收到 `done` 后消失，不写入消息历史。

## 数据结构建议

升级后的 session 文件建议保留以下结构：

```json
{
  "title": "New Chat",
  "created_at": 0,
  "updated_at": 0,
  "messages": [
    {
      "role": "assistant",
      "content": "",
      "tool_calls": [
        {
          "id": "call_xxx",
          "tool": "execute_skill",
          "input": "{...}",
          "output": "完整输出或[摘要]...",
          "is_error": false,
          "summary_source": "single_tool_overflow"
        }
      ]
    }
  ],
  "display_messages": [],
  "compressed_context": "[历史对话摘要] ...",
  "middle_trim_context": "[中段历史摘要] ...",
  "context_usage_peak": 0
}
```

字段约定：

- `messages`：LLM active context，不保证完整历史。
- `display_messages`：用户可见完整历史，前端优先使用。
- `compressed_context`：全局 compaction 摘要。
- `middle_trim_context`：中段裁剪摘要。
- `summary_source`：
  - `single_tool_overflow`：单条工具输出过长，实时摘要。
  - `tool_result_clear`：历史工具输出过多，由 middleware 摘要。
  - `missing_tool_output`：tool call 已开始但没有收到完成事件，由保存兜底补齐。
- `is_error`：工具调用失败或补齐占位输出时为 `true`，前端可用于错误态展示；不要把协议层 error 文案硬拼进 assistant 正文。

## 改造验收标准

### streaming

- 同一 session 连续多轮工具调用后，当前回复不出现上一轮回复正文。
- 保存到 session 的 assistant content 不包含 graph replay 的历史 AIMessage 内容。
- 历史 `AIMessageChunk` replay 不会进入 `full_response`。
- `load_session_for_agent()` 不会把 final answer 合并进上一条带 `tool_calls` 的 assistant。

### 工具输出

- 工具输出长度超过 2000 字符时，前端收到 preview，但 session 中不是 preview。
- 如果触发摘要，必须出现 `summary_source`。
- 结构化数据不从截断文本中解析。
- 任意 assistant 消息只要包含 `tool_calls`，入模时必须紧跟同数量、同 `tool_call_id` 的 `ToolMessage`。
- 人为构造一个缺 `output` 的历史 tool_call，`load_session_for_agent()` + `_build_messages()` 后不应触发 provider 400。
- 流式中断或 MCP 异常后，session 中不得出现没有 `output` 的 tool_call；前端 tool card 应闭合为错误态，而不是永久 running。

### session 历史

- 触发压缩或 middle trim 后，前端仍能看到完整原始消息顺序。
- LLM context 中能看到摘要，但不会看到被移出 active messages 的原文。
- archive 文件可审计被移出的原始消息。

### 长会话任务状态

- 多轮多任务后触发 middle trim，agent 不会把已完成的旧任务当作未完成继续执行。
- 中段摘要中明确保留旧任务状态、关键命令、文件路径、错误信息和结论。

### 上下文维护提示

- 触发 ToolResultClear 时，前端显示“正在整理历史工具结果...”，收到 done 后消失。
- 触发 single_tool_overflow 时，前端显示“正在提炼超长工具结果...”，收到 done 后消失。
- 触发 Compaction 时，前端显示“正在压缩长对话历史...”，收到 done 后消失。
- context maintenance 状态不落库，不出现在 `/history` 的消息列表中。

## 代码落点参考

好得 APP 主要相关位置：

- `/Users/pet/Code/好得APP/backend/app/agent/graph/agent.py`
- `/Users/pet/Code/好得APP/backend/app/agent/graph/session_manager.py`
- `/Users/pet/Code/好得APP/backend/app/routers/agent_chat.py`
- `/Users/pet/Code/好得APP/backend/app/agent/tools/execute_skill_tool.py`
- `/Users/pet/Code/好得APP/backend/app/agent/tools/terminal_tool.py`

PuddingClaw 可参考实现：

- `backend/graph/agent.py`
- `backend/api/chat.py`
- `backend/graph/session_manager.py`
- `backend/graph/middlewares/cache.py`
- `backend/graph/middlewares/compression.py`
- `backend/graph/middlewares/skills_router.py`
- `backend/graph/llm_input_logger.py`
- `backend/config.py`
- `docs/context-engineering-design.md`
