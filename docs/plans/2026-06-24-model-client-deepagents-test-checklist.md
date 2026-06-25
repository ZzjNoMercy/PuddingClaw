# ModelClient × DeepAgents 测试清单

> 日期：2026-06-24  
> 最新更新：2026-06-25  
> 状态：测试实施中  
> 目标：验证 `ModelClient` / `ModelClientChatModel` 能作为 `BaseChatModel` 注入 DeepAgents，并在 Higress 优先、Provider 直连 fallback 的双模式下保持 LangChain 模型契约。

## 1. 测试依据与边界

本清单来自以下代码与课件：

- `backend/llm/model_client.py`
- `backend/tests/test_model_client.py`
- `HarnessEngineering_第三节_deepAgents实战.ipynb`
- Notebook 固定环境：`deepagents==0.5.3`、`langchain==1.2.15`、`langgraph==1.1.10`、`langchain-core==1.3.2`，仅作为功能覆盖参考，不作为 API 依据。
- 当前 latest 目标环境：`deepagents==0.6.11`、`langchain==1.3.11`、`langchain-core==1.4.8`、`langgraph==1.2.6`。
- PuddingClaw 当前 backend Python 要求：`>=3.11,<3.13`
- PuddingClaw `deepagents-test` 依赖组：`deepagents==0.6.11`
- PuddingClaw `requirements.txt` 固定环境仍保留当前运行 pin；如后续 runtime 正式接入 DeepAgents，需要再统一锁定生产依赖。

API 以最新 DeepAgents 版本为准；Notebook 主要用于确认应该覆盖哪些功能类别。测试重点不是重复验证 DeepAgents 的 Backend、Skills、Memory 或权限实现，而是验证这些机制反复调用模型时，ModelClient 不会破坏以下契约：

1. 标准 `BaseChatModel` 输入、输出和 RunnableConfig。
2. 工具绑定与工具调用消息。
3. 同步、异步、流式调用。
4. 结构化输出。
5. Higress 与 Provider 直连切换、故障回退。
6. callbacks、LangSmith tracing、token usage 等可观测性。
7. 多轮、长上下文、子代理和 HITL 恢复后的连续调用。

## 1.1 Notebook 功能映射与测试边界

| Notebook 章节 | 功能类别 | 是否测试 | ModelClient 测试目标 |
|---|---|---:|---|
| 第0章 环境、LangSmith、langgraph dev | 环境与可观测性 | 是 | latest 依赖环境可创建；callbacks / tracing 不因 wrapper 断链。 |
| 第1章 框架定位 | 概念说明 | 否 | 不单独测试。 |
| 第2章 `create_deep_agent` 八步流水线 | 模型解析、工具预处理、backend、子代理、middleware、系统提示词、编译 | 是 | `ModelClientChatModel` 可作为 latest API 的 `model=`；工具、backend、子代理、中间件组合后仍可调用模型。 |
| 第3章 Middleware | Todo、Filesystem、Summarization、Memory、自定义 middleware、执行顺序 | 是，优先级最高 | middleware 改写 prompt/messages/tool calls 后，ModelClient 仍保持原生 ChatModel 行为。 |
| 第4章 Backend | StateBackend、FilesystemBackend、StoreBackend、CompositeBackend、SandboxBackendProtocol | 是 | Backend 工具返回的 `ToolMessage` 可被下一轮模型消费；不测试 DeepAgents backend 内部实现。 |
| 第5章 子代理 | general-purpose、SubAgent、CompiledSubAgent、异步/后台子任务能力 | 是，优先级最高 | 子代理继承或显式使用 `ModelClientChatModel` 时，工具绑定、config、callbacks、路由不丢失。latest API 中若旧 Notebook API 已变化，以 latest 导出与签名为准。 |
| 第6章 HITL + Skills + Memory + Permissions | interrupt/resume、skills 按需加载、AGENTS.md memory、权限拦截 | 是，优先级最高 | interrupt/resume 后历史 `AIMessage.tool_calls`、`ToolMessage.tool_call_id` 和系统提示注入仍可被模型接受。 |
| 第7章 gstack HTML 生成 | CompositeBackend + Skills + Memory + Permissions + HITL + streaming 端到端 | 是，作为 P1/P2 E2E | 用代表场景覆盖长任务、多轮工具、文件写入、审批恢复；不要求复刻 Notebook 旧版代码。 |
| 第8章 总结与进阶 | 能力总结 | 否 | 不单独测试。 |

## 1.2 当前优先级

先测最容易暴露 wrapper 兼容问题的三类路径：

1. `P0-A` Middleware + tool calling：DeepAgents 默认 middleware 注入工具后，底层模型能收到完整工具集合，并完成“模型 → 工具 → 模型”循环。
2. `P0-B` 子代理：`task` / general-purpose subagent 能继承主模型或再次通过 `ModelClientChatModel` 调用。
3. `P0-C` HITL / resume：interrupt 后 approve/edit/reject 恢复时，消息格式和 tool-call 对应关系不断链。

普通文本调用已经通过 smoke test，不作为下一阶段的主要风险点。

## 2. 完成标准

- [ ] P0 单元测试全部通过。
- [ ] P0 DeepAgents 集成测试全部通过。
- [ ] Higress 和直连两种路径使用同一组契约用例。
- [ ] 网关首响应前失败可以安全回退；首个流式 chunk 后失败不得重试。
- [ ] fallback 后仍保留 tools、tool binding 参数、stop/config/callbacks。
- [ ] Notebook 第 2、3、5、6、7 章涉及的模型调用路径有代表性覆盖。
- [ ] 在最终选定依赖版本下跑一次真实 Provider E2E，并保存版本与结果。

## 3. 测试环境矩阵

| 维度 | 必测值 |
|---|---|
| 路由 | `force_direct=True`；Higress 可用；Higress 不可用自动直连 |
| Provider | DeepSeek；OpenAI-compatible（至少 Higress） |
| 调用方式 | `invoke`、`ainvoke`、`stream`、`astream` |
| 工具 | 无工具；单工具；多工具；带 `tool_choice/strict/parallel_tool_calls` |
| 输入 | `list[BaseMessage]`、字典消息、字符串、`PromptValue` |
| 输出 | 普通文本、tool calls、结构化输出、usage metadata |
| 故障阶段 | 建连前；首 token 前；首 token 后；Provider 返回 4xx/5xx |
| Agent | LangChain `create_agent`；DeepAgents `create_deep_agent`；主/子代理 |

建议把网络测试分为两组：默认 CI 运行 mock/contract 测试；显式标记 `integration` 的测试才访问本地 Higress 或真实 Provider。

## 4. P0：ModelClient 单元与契约测试

### 4.1 模型选择和配置

- [x] `MC-U-001` `force_direct=True` + DeepSeek 返回 `ChatDeepSeek`。
- [x] `MC-U-002` `force_direct=True` + OpenAI 返回 `ChatOpenAI`。
- [x] `MC-U-003` 构造参数 `temperature` 覆盖配置值。
- [x] `MC-U-004` 未知 Provider 明确抛错。
- [ ] `MC-U-005` Higress 可用时使用 `gateway_llm.model` 和有效 gateway URL。
- [ ] `MC-U-006` Higress 不可用时使用 `fallback_llm` 的 provider/model/base_url/key。
- [ ] `MC-U-007` `force_direct=True` 必须跳过 capability 探测结果和 Higress。
- [ ] `MC-U-008` capability 探测异常时安全降级直连且记录告警。
- [ ] `MC-U-009` 单次调用只做一次路由决策，避免 `_should_use_gateway()` 两次结果不一致。
- [ ] `MC-U-010` 刷新配置后，新 Agent/ModelClient 使用新的 gateway 与 fallback 配置。

### 4.2 BaseChatModel 输入输出契约

- [ ] `MC-U-011` `ModelClientChatModel` 是 `BaseChatModel`，可序列化识别参数稳定。
- [ ] `MC-U-012` `invoke/ainvoke` 接受 `list[BaseMessage]` 并返回 `BaseMessage`。
- [x] `MC-U-013` 接受字符串输入并转换为合法 `HumanMessage`，不能把裸字符串直接放进 messages 列表。
- [ ] `MC-U-014` 接受字典消息和 `PromptValue`，转换行为与标准 `BaseChatModel` 一致。
- [x] `MC-U-015` `stop`、provider kwargs、timeout 等调用参数传递到底层模型。
- [x] `MC-U-016` `RunnableConfig` 中 callbacks、tags、metadata、run_name、configurable 被保留。
- [ ] `MC-U-017` `_generate/_agenerate` 返回合法 `ChatResult/ChatGeneration`。
- [x] `MC-U-018` `_stream/_astream` 返回合法 `ChatGenerationChunk`，chunk id/content/tool metadata 不丢失。
- [ ] `MC-U-019` `batch/abatch` 使用继承的 Runnable 接口可正常工作。

### 4.3 工具绑定与 Tool Calling

- [ ] `MC-U-020` `bind_tools()` 返回新实例，不修改原实例。
- [ ] `MC-U-021` LangChain `@tool`、Pydantic schema、OpenAI tool dict 三种输入均可绑定。
- [ ] `MC-U-022` 工具名称、description、JSON schema 原样传到底层模型。
- [x] `MC-U-023` `bind_tools(..., tool_choice=...)` 参数不丢失。
- [x] `MC-U-024` `strict`、`parallel_tool_calls` 等 provider 参数不丢失。
- [ ] `MC-U-025` 返回的 `AIMessage.tool_calls` 含正确 name、args、id。
- [ ] `MC-U-026` `ToolMessage(tool_call_id=...)` 可进入下一轮并得到最终答案。
- [ ] `MC-U-027` 多工具和并行 tool calls 不被 wrapper 改写或截断。
- [x] `MC-U-028` 网关调用失败转直连后，已绑定工具和所有 bind kwargs 仍然存在。

### 4.4 流式调用

- [x] `MC-U-029` `stream/astream` 按顺序输出全部文本 chunk。
- [ ] `MC-U-030` 流式 tool-call chunks 能正确累积为完整工具调用。
- [x] `MC-U-031` 网关在首 chunk 前失败时只回退一次直连。
- [x] `MC-U-032` 网关已输出 chunk 后失败时抛错，不拼接直连结果，避免重复内容。
- [ ] `MC-U-033` 直连模式失败时不再次 fallback。
- [ ] `MC-U-034` `fallback_to_direct=False` 时原始网关异常向上抛出。
- [ ] `MC-U-035` 调用方取消 `astream` 时底层生成器正确关闭，不继续请求或记录虚假成功。

### 4.5 Token usage 与可观测性

- [x] `MC-U-036` 非流式 `ainvoke` 记录 input/output token、角色和会话信息。
- [ ] `MC-U-037` 同步 `invoke` 同样记录 usage。
- [ ] `MC-U-038` `stream/astream` 正确聚合 usage，不能把 provider 的累计值重复相加。
- [ ] `MC-U-039` gateway 失败后只记录最终成功路径一次，并能标识实际路由。
- [ ] `MC-U-040` 调用失败或取消时不记录为成功 usage。
- [ ] `MC-U-041` `record_usage=False` 不写入 usage store。
- [x] `MC-U-042` wrapper callbacks 能收到 start/end/error 事件，LangSmith trace 不断链；token 事件待真实流式 provider 细测。
- [ ] `MC-U-043` 日志不得输出 Provider key、Authorization header 或完整敏感请求。

## 5. P0：DeepAgents 真实集成测试

### 5.1 最小 Agent 与标准调用

- [ ] `MC-DA-001` 安装目标版本后，`create_deep_agent(model=ModelClientChatModel(...))` 编译为 `CompiledStateGraph`。
- [ ] `MC-DA-002` 最小 Agent `invoke` 返回 state，末条消息为有效 `AIMessage`。
- [x] `MC-DA-003` 同一 Agent 的 `stream` 能输出模型与图事件，正常结束。
- [ ] `MC-DA-004` `config={"configurable": {"thread_id": ...}}` 完整通过 wrapper，checkpointer 可恢复同一线程。
- [ ] `MC-DA-005` 启用 LangSmith callback 后能看到主 Agent 模型 span、工具 span 和结束状态。

### 5.2 DeepAgents Middleware 与内置工具链

- [x] `MC-DA-006a` latest DeepAgents 默认 middleware + 自定义工具能通过 `ModelClientChatModel` 完成真实工具循环（fake model contract test）。
- [ ] `MC-DA-006` Agent 能调用 `write_todos` 并继续模型循环。
- [ ] `MC-DA-007` Agent 能调用 `ls/read_file/write_file/edit_file`，tool-call id 在往返过程中保持一致。
- [ ] `MC-DA-008` 单任务至少连续执行三轮“模型 → 工具 → 模型”，无消息格式错误。
- [ ] `MC-DA-009` 工具返回错误时，`PatchToolCallsMiddleware` 修补后的消息仍可被模型接受。
- [ ] `MC-DA-010` 多个工具被同时绑定时，ModelClient 不改变工具集合或 description。

### 5.3 结构化输出

- [x] `MC-DA-011` `response_format=PydanticModel` 返回可校验结构。
- [x] `MC-DA-012` DeepAgents 为结构化输出传入的 `bind_tools` 参数被完整保留。
- [ ] `MC-DA-013` 结构化输出首次校验失败后的重试仍可成功。
- [ ] `MC-DA-014` Higress 和直连对同一 schema 都能返回一致字段类型。

### 5.4 Summarization 与长上下文

- [ ] `MC-DA-015` 构造超过 summarization 阈值的多轮消息，自动压缩后模型继续调用成功。
- [ ] `MC-DA-016` 压缩后保留必要的 system message、最近工具调用及 tool_call_id 对应关系。
- [ ] `MC-DA-017` 长上下文下 Higress 与直连不会因 wrapper 输入转换产生 400。
- [ ] `MC-DA-018` reasoning/thinking 模型单列兼容测试；不支持时应显式阻止或给出可诊断错误。

### 5.5 子代理

- [x] `MC-DA-019a` latest DeepAgents 默认 `general-purpose` 子代理可经由 `task` 工具回调同一个 `ModelClientChatModel` wrapper（fake model contract test）。
- [ ] `MC-DA-019` 默认 `general-purpose` 子代理继承 `ModelClientChatModel` 并完成一次真实 `task` 调用。
- [x] `MC-DA-020` 声明式 SubAgent 继承主模型时，工具绑定与网关路由正常。
- [ ] `MC-DA-021` 子代理显式使用另一 `ModelClientChatModel` 时，可使用不同 role/temperature/模型配置。
- [ ] `MC-DA-022` 子代理进行多轮工具调用后，主代理能读取最终消息并继续回答。
- [x] `MC-DA-023` CompiledSubAgent 返回 `messages` 后主模型能正常消费。
- [ ] `MC-DA-024` AsyncSubAgent 的启动、轮询和结果回收不因 callbacks/config 丢失而失败。

### 5.6 HITL、Skills、Memory 与 Permissions 协同

- [x] `MC-DA-025a` latest DeepAgents 模型生成受控工具调用后触发 HITL interrupt（fake model contract test）。
- [x] `MC-DA-026a` latest DeepAgents approve 后恢复执行，历史 `AIMessage.tool_calls` 可继续使用（fake model contract test）。
- [ ] `MC-DA-025` 模型生成受控工具调用后触发 HITL interrupt。
- [ ] `MC-DA-026` approve 后恢复执行，历史 `AIMessage.tool_calls` 可继续使用。
- [x] `MC-DA-027` edit 决策修改参数后，模型能消费新 ToolMessage 并结束任务。
- [x] `MC-DA-028` reject 后模型能理解拒绝结果并选择其他路径。
- [ ] `MC-DA-029` Skills metadata 注入长 system prompt 后仍能正确选择 `read_file`。
- [ ] `MC-DA-030` MemoryMiddleware 注入 AGENTS.md 后模型遵守记忆约束。
- [ ] `MC-DA-031` Permissions 返回 denied 工具结果后模型不循环重试同一危险调用。
- [ ] `MC-DA-032` Notebook“四者协同”四步场景完整通过：skills 可见、敏感文件拦截、memory 写入、HITL 中断。

### 5.7 Notebook 第 7 章端到端代表用例

- [ ] `MC-DA-033` 使用 CompositeBackend + design skill + memory + permissions 创建 Agent。
- [ ] `MC-DA-034` Agent 按需读取完整 SKILL.md，而不是只凭 metadata 生成结果。
- [ ] `MC-DA-035` `stream` 中收到 `write_file` interrupt，并用 `Command(resume=...)` 恢复。
- [ ] `MC-DA-036` 最终 HTML 文件真实落盘且非空，Agent 最终消息可正常返回。
- [ ] `MC-DA-037` 整个长任务中至少包含 Skills 读取、Todo、文件写入等多轮工具调用，期间无 tracing/config/tool schema 丢失。

## 6. P1：路由、恢复与稳定性测试

- [ ] `MC-R-001` Higress 健康时所有主 Agent 调用默认经过网关。
- [ ] `MC-R-002` Higress 返回连接失败、超时、502/503 时，首响应前转直连。
- [ ] `MC-R-003` Higress 返回 400/401/403 时是否 fallback 必须有明确策略和测试，避免错误 key/config 被掩盖。
- [ ] `MC-R-004` gateway model 名与 fallback model 名不同的情况下，路由切换正确。
- [ ] `MC-R-005` fallback Provider key 缺失时给出可诊断错误，不泄露 key。
- [ ] `MC-R-006` 连续 20 轮模型/工具循环不创建无界资源或重复探测。
- [ ] `MC-R-007` 10 个并发 `ainvoke` 不串用 tools、config、usage 或会话标识。
- [ ] `MC-R-008` 同一 wrapper 多线程调用时没有可变共享状态污染。
- [ ] `MC-R-009` 超时和任务取消能快速释放连接。
- [ ] `MC-R-010` 配置变更后的 Agent 缓存失效规则有自动测试。

## 7. P2：Provider 差异与演进测试

- [ ] `MC-P-001` DeepSeek `deepseek-chat` 普通、工具、流式三条路径。
- [ ] `MC-P-002` DeepSeek reasoning/thinking 响应包含 `reasoning_content` 时的多轮回传行为。
- [ ] `MC-P-003` OpenAI Chat Completions 模式工具与结构化输出。
- [ ] `MC-P-004` 如果启用 Responses API，验证 `store=False` 等隐私参数可透传。
- [ ] `MC-P-005` Qwen/custom OpenAI-compatible base URL 的模型名和工具 schema。
- [ ] `MC-P-006` DeepAgents Harness Profile 对自定义 `BaseChatModel` 的识别结果符合预期；无 Profile 时记录限制。
- [ ] `MC-P-007` DeepAgents 或 LangChain 升级后运行兼容矩阵，重点关注 `bind_tools`、stream chunks、structured output。

## 8. 建议的测试文件拆分

| 文件 | 内容 |
|---|---|
| `backend/tests/test_model_client.py` | 路由、配置、usage 基础单测 |
| `backend/tests/test_model_client_chat_model.py` | BaseChatModel、config、callbacks、tools、stream 契约 |
| `backend/tests/test_model_client_fallback.py` | 网关异常、流式边界、fallback 工具保留 |
| `backend/tests/integration/test_model_client_providers.py` | Higress 与真实 Provider 冒烟测试 |
| `backend/tests/integration/test_model_client_deepagents.py` | DeepAgents 最小、工具、结构化输出、子代理、HITL |
| `backend/tests/integration/test_model_client_deepagents_latest.py` | latest DeepAgents API 下的 middleware/tool loop、子代理、HITL contract tests；默认环境无 `deepagents` 时跳过 |
| `backend/tests/integration/test_deepagents_notebook_scenario.py` | Notebook 四者协同与 HTML 端到端代表用例 |

## 9. 推荐实施顺序

1. 先补 `MC-U-020`～`MC-U-028`，锁住工具绑定与 fallback 契约。
2. 补 `MC-U-012`～`MC-U-019`、`MC-U-042`，锁住 BaseChatModel/RunnableConfig 契约。
3. 补流式失败边界和 usage 聚合。
4. 对齐并锁定 DeepAgents 依赖版本。
5. 完成 `MC-DA-001`～`MC-DA-014`，作为接入门禁。
6. 完成子代理、HITL 和 Notebook 第 7 章代表用例。
7. 最后加入真实 Higress/Provider E2E 和并发稳定性测试。

## 10. 当前基线

- 已有 `backend/tests/test_model_client.py` 共 7 个测试，覆盖直连模型选择、temperature、role、未知 Provider、异步 usage、异步网关 fallback。
- 当前尚无 `ModelClientChatModel` 专项测试。
- 当前尚无 DeepAgents 依赖和 DeepAgents 集成测试。
- 已人工验证一次：当前 wrapper 可被 LangChain `create_agent` 使用，并能通过 Higress 完成真实工具调用；该结果应转成可重复的 integration test。

## 11. 执行记录

| 日期 | 状态 | 说明 |
|---|---|---|
| 2026-06-24 | 清单完成 | 已从 Notebook 第 0～8 章提取 ModelClient 相关测试边界；尚未实施新增测试 |
| 2026-06-25 | 测试启动 | 明确 API 以 latest DeepAgents 为准，Notebook 只作为功能参考；新增 latest contract tests 覆盖 Middleware/tool loop、general-purpose subagent、HITL interrupt/resume |
| 2026-06-25 | 契约补强 | 已补 `bind_tools(**kwargs)`、fallback 保留 tools/bind kwargs、`config/stop/**kwargs` 透传、标准 `BaseChatModel` 输入转换路径；新增 `test_model_client_chat_model.py` |
| 2026-06-25 | 三组补测 | 已补结构化输出、stream/astream、callbacks/error lifecycle 契约测试；全量 backend 与 latest DeepAgents 隔离测试通过 |
| 2026-06-25 | 测试环境迁移 | backend Python 要求提升为 `>=3.11,<3.13`；新增 `deepagents-test` 依赖组，DeepAgents latest 测试可用 `uv run --group deepagents-test ...` 在 backend 内执行 |
| 2026-06-25 | E2E 扩展 | 按顺序补 DeepAgents `response_format=`、`agent.stream`、HITL edit/reject/respond、FilesystemBackend/CompositeBackend、Custom SubAgent/CompiledSubAgent；全量 backend 测试通过 |
