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

PuddingClaw 当前版本已经演进为“面向长会话和工具密集任务的 context-engineering agent”。迁移时应优先统一 session 数据模型和 SSE/tool output 契约，再逐层迁移 middleware。

## 差异对照

| 维度 | 好得 APP 现状 | PuddingClaw 现状 | 影响 |
| --- | --- | --- | --- |
| 上下文窗口策略 | 固定 60K warning / 80K critical；超限保后半段；最多 50 条历史 | 按 `context_window=1M` 配置，200K tail/middle trim，500K compaction | 好得长会话更容易丢中间任务状态 |
| session 展示/LLM 双轨 | `messages` 被压缩后前端也只能看到剩余 messages，archive 不自动回显 | `display_messages` 给前端完整历史，active `messages` 给 LLM，archive 兼容合并 | 好得压缩后用户历史可能不完整 |
| 中段裁剪摘要 | 无 middle trim | 入模前 `_maybe_middle_trim_session()` 做中段摘要归档 | 避免“用户请求还在、完成证据没了”导致 agent 误以为任务未完成 |
| 工具历史还原 | `_build_messages()` 主要还原 user/assistant 文本，未完整还原历史 `tool_calls`/`ToolMessage` | 还原 `AIMessage(tool_calls)` + `ToolMessage`，并给历史工具输出加防污染前缀 | 好得后续轮次看不到历史工具真实结果 |
| 工具输出处理 | `tool_end` 直接 `[:2000]`；部分工具内部 `[:5000]` 硬截断 | 后端持久化完整输出或摘要；前端只拿 `output_preview` | 好得会真实丢工具输出，尤其 JSON/搜索/长报告风险高 |
| 单条超长工具摘要 | 有 `_summarize_tool_result()`，但主链路仍硬截断 | `single_tool_overflow` 超 20K tokens 实时摘要并标记来源 | PuddingClaw 对长工具结果更可控 |
| streaming replay 防护 | token 流接收 `AIMessageChunk or ai` | 只累计 `AIMessageChunk`，避免 LangGraph replay 历史 `AIMessage` 污染当前回复 | 好得存在上一轮内容混入当前输出的风险 |
| middleware pipeline | 基本没有 LangChain middleware 分层治理 | `cache_boundary -> tail_trim -> tool_clear -> summarization -> compaction -> skills_router -> task_state` | PuddingClaw 已形成分层上下文治理 |
| prefix cache 友好 | 动态工具分类会重建 agent，cache key 粗 | SkillsRouterMiddleware 注入路由提示，尽量不改 system prefix | 好得 DeepSeek prefix cache 命中率和稳定性较弱 |
| context usage 观测 | 无运行时 token usage/峰值持久化 | `stream_usage=True`，SSE `context_usage`，记录 `context_usage_peak` | 好得难以判断真实上下文消耗 |
| MCP | 未看到 agent 主链路 MCP | 支持 MCP 持久 session 加载工具 | 好得工具生态扩展能力较弱 |

## 关键风险

### 1. 历史工具输出污染当前回复

好得当前 streaming 中同时接受 `AIMessageChunk` 和完整 `ai` 消息作为 token 来源。LangGraph/LangChain streaming 中完整 `AIMessage` 可能是 graph state/replay 事件，包含历史回复或历史工具摘要。若被当作当前 token 累计，就可能出现上一轮内容混入本轮回复。

改进目标：

- 只把 `AIMessageChunk` 作为 token 流累计。
- 完整 `AIMessage` 只用于读取 tool calls、reasoning metadata 等结构化信息。

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

## 建议迁移顺序

### P0：修复当前最容易复现的问题

1. 修 streaming replay：
   - 只累计 `AIMessageChunk`。
   - 不把完整 `ai` 消息作为当前 token。

2. 移除后端工具输出硬截断：
   - `tool_end.output` 保留完整输出或摘要。
   - SSE 增加 `output_preview` 和 `output_full_length`。
   - session 持久化使用完整 `output`。

3. 跳过空 assistant segment：
   - `new_response` 或异常场景下，空 content 且无 tool_calls 的 segment 不落库。

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

## 改造验收标准

### streaming

- 同一 session 连续多轮工具调用后，当前回复不出现上一轮回复正文。
- 保存到 session 的 assistant content 不包含 graph replay 的历史 AIMessage 内容。

### 工具输出

- 工具输出长度超过 2000 字符时，前端收到 preview，但 session 中不是 preview。
- 如果触发摘要，必须出现 `summary_source`。
- 结构化数据不从截断文本中解析。

### session 历史

- 触发压缩或 middle trim 后，前端仍能看到完整原始消息顺序。
- LLM context 中能看到摘要，但不会看到被移出 active messages 的原文。
- archive 文件可审计被移出的原始消息。

### 长会话任务状态

- 多轮多任务后触发 middle trim，agent 不会把已完成的旧任务当作未完成继续执行。
- 中段摘要中明确保留旧任务状态、关键命令、文件路径、错误信息和结论。

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
- `backend/config.py`
- `docs/context-engineering-design.md`

