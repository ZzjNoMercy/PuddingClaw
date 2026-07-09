# Partial Run Session Persistence Plan

## Goal

用户手动中断 Agent/DeepAgents 流式运行后，刷新页面仍能看到已经发生的内容：

- 用户本轮问题
- 已输出的 assistant 文本和 reasoning
- 已开始、已完成、被中断的工具调用
- trace 的 cancelled 状态

后续用户输入“继续”时，不依赖 LangGraph checkpoint replay 旧事件，而是作为普通新一轮基于 session 历史继续推进。Checkpoint 保留为 HITL/permission interrupt 的执行恢复机制，不作为聊天连续性的主存储。

## First Principles

1. 用户已经在 UI 看到的事实必须成为 durable session record。
2. 前端内存、SSE 连接、LangGraph checkpoint 都不能作为用户可见历史的唯一来源。
3. 普通 stop 与 HITL interrupt 是不同语义：
   - 普通 stop：本轮执行被用户暂停/中断，partial output 应落盘，checkpoint 应作废，避免下轮 replay。
   - HITL approve/reject：同一活跃 run 内用 `Command(resume=...)` 精确恢复。
4. session 历史是“继续”的语义边界；checkpoint 是内部执行恢复边界。

## Implementation Checklist

- [x] 建立本计划文档，记录目标、边界和验收标准。
- [x] 后端 DeepAgents 取消分支保存 partial user/assistant 消息。
- [x] 将 pending 工具调用落成 interrupted/error 状态，禁止持久化 `running`。
- [x] 保留取消时 checkpoint 清理，避免下次普通输入 replay 旧图状态。
- [x] 补后端回归测试：取消后 session history 能恢复 partial 内容。
- [x] 验证前端历史解析不会把 interrupted 工具恢复成 running。
- [x] 将用户停止提示从 assistant 正文迁移为结构化 session 字段。
- [x] 前端仅在最后一条被中断输出后显示停止提示，后续继续输入后自然隐藏。
- [x] 将 LangGraph checkpoint thread 收窄到本轮 `session_id:query_id`，避免普通 follow-up 复用上轮图状态。
- [x] 过滤 LangGraph 对历史 tool_call/tool_result 的 echo，避免“继续”时旧工具再次出现在本轮 UI。
- [x] 新 query 进入 Agent stream 后立即持久化 user message，切换 session/刷新时不再像任务没发生过。
- [x] 前端发送新对话时直接使用 `createSession()` 返回的 session id，避免 React 状态切换竞态。
- [x] 历史 `tool_calls` 仍不按结构化工具消息回传，但将已完成工具输出压缩为 LLM-only 文本摘要，保证“继续”能看到中断前查到的事实。
- [x] DeepAgents 历史输入改为沿用旧 Agent 的协议化思路：从 raw session history 重建 `AIMessage(tool_calls)` + `ToolMessage(tool_call_id)`，避免只靠摘要续上下文。
- [x] 运行 targeted backend tests 与 frontend build。

## Implementation Notes

- `DeepAgentsAgentManager._persist_partial_run()` 负责取消时的 durable session 写入。
- 已完成工具保持原始输出；只 start 未 end 的工具写入 `summary_source=stream_cancelled` 和 `is_error=true`。
- assistant partial content 只保存模型已经实际输出的正文；用户停止状态保存为 `interrupted=true` 与 `interruption_notice`，避免把 UI 状态污染进 LLM 历史正文。
- 前端历史解析恢复 `interrupted/interruption_notice`，只在最后一条消息是被中断 assistant 时渲染状态条；用户继续输入后旧消息不再是最后一条，提示自然消失。
- 普通 run 的 LangGraph checkpoint thread 使用 `session_id:query_id`，因此下一次普通 query 不会复用上轮 checkpoint；取消时也只清理本轮 thread。
- HITL approve/reject 仍在同一活跃 SSE 内使用同一个 `session_id:query_id` 通过 `Command(resume=...)` 继续。
- `load_session_for_agent()` 会剥掉历史 `tool_calls`，但 LangGraph/checkpoint 仍可能 echo 历史工具事件；过滤集合必须从原始 session history 计算，不能从传给模型的 messages 计算。
- Agent stream 在构造本轮模型输入后、调用 LangGraph 前立刻写入 user message；正常完成只补 assistant，取消时 partial 保存不会重复写 user。
- 前端新对话发送时不要依赖 `setSessionId()` 同步完成，本轮 SSE 使用 `createSession()` 返回的 id 作为稳定 session id。
- `load_session_for_agent()` 不能原样回传历史 `tool_calls`，否则会触发 duplicate/missing `tool_call_id` 或让 LangGraph echo 历史工具事件；但必须把 `output/raw_output` 作为普通文本上下文补回，否则 partial assistant 正文没有最终答案时，“继续”会丢失已经查到的数值。
- 工具输出摘要使用每个工具短截断 + 总预算控制，避免前几个长输出挤掉后续关键结果；已用 `session-14a1b471414b` 验证 `205390` 会进入 LLM 历史上下文。
- 根因复盘：早期 Agent runtime 有 `_build_messages()` 重建 `AIMessage + ToolMessage`；DeepAgents 集成规划也写了必须复用或抽公共函数。但实际 DeepAgents first pass 走了单独 dict builder，随后 `load_session_for_agent()` 又为避免历史工具 replay 剥离 `tool_calls`，两处叠加导致 DeepAgents follow-up 看不到历史工具输出。这不是 DeepAgents 的硬性限制，而是实现分叉。
- DeepAgents 当前修复：`session.json` 仍保持前端友好的聚合结构；运行时输入从 raw history 重建 LangChain protocol messages，并继续用 `historical_tool_call_ids` 过滤历史工具 echo，避免 UI 重放旧工具。

## Verification

- `backend/.venv/bin/python -m pytest tests/test_deepagents_manager.py` -> 19 passed
- `backend/.venv/bin/python -m py_compile graph/deepagents_manager.py graph/permission_resume.py` -> passed
- `frontend/npm run build` -> passed
- `backend/.venv/bin/python -m pytest tests/test_deepagents_manager.py` -> 21 passed after checkpoint/thread echo fix
- `backend/.venv/bin/python -m py_compile graph/deepagents_manager.py graph/session_manager.py graph/permission_resume.py` -> passed
- `backend/.venv/bin/python -m pytest tests/test_deepagents_manager.py` -> 22 passed after immediate user-message persistence fix
- `frontend/npm run build` -> passed after session id send-path fix
- `backend/.venv/bin/python -m pytest tests/test_session_manager.py tests/test_deepagents_manager.py` -> 29 passed after LLM-only tool output context fix
- Latest session `session-14a1b471414b`: `load_session_for_agent()` contains `205390`.
- `backend/.venv/bin/python -m pytest tests/test_deepagents_manager.py tests/test_session_manager.py` -> 29 passed after DeepAgents protocol-history reconstruction.

## Acceptance Criteria

- 中断前已完成的工具输出刷新后仍可见。
- 中断前已输出的 reasoning/正文刷新后仍可见。
- 未完成工具刷新后显示为结束态，不无限转圈。
- 用户再输入“继续”时走普通新一轮，不 replay 上次已展示过的工具事件。
- HITL 的同请求 approve/reject resume 流程不被破坏。

## Adversarial Review Questions

- 如果取消发生在 tool_start 之后、tool_end 之前，是否有可见 interrupted 工具卡？
- 如果取消发生在 tool_end 之后、最终 token 之前，已完成工具输出是否完整落盘？
- 如果取消发生在只有 reasoning、没有 content 时，刷新后是否还能看到 reasoning？
- 如果取消后 checkpoint 未清理，下次普通 query 是否会 replay 旧工具？
- 如果正常 done 已保存，取消分支是否会重复保存同一轮？
- 如果停止提示被写入 assistant 正文，下一轮 LLM 是否会误把 UI 状态当成用户语义？
- 如果 follow-up 复用 session-scoped checkpoint，旧工具是否会被 replay？
- 如果 LangGraph echo 历史 tool result，而 adapter 只过滤 tool_start，不过滤 tool_end，UI 是否仍会出现旧工具？
- 如果新对话发送后立刻切到其他 session，本轮 user query 是否已经在后端 session history 可见？
- 如果立即落盘 user 后再构造模型输入，是否会让模型看到重复的当前 user query？
- 如果 partial assistant 正文只是“现在查询...”，而准确数值只在 tool output 中，下一轮“继续”是否还能看到这些工具事实？
- 如果前几个工具输出很长，后续关键工具结果是否会被摘要预算截断掉？
