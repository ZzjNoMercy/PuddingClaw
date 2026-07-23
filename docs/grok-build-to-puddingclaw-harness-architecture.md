# 外部 Agent Harness 对比与 PuddingClaw 演进记录

> **历史文件名说明**：文件名保留 `grok-build-to-puddingclaw-*`，因为本文最初从 Grok Build 扫描开始；这不表示 PuddingClaw Harness 源自 Grok Build。
>
> **文档定位**：本文是外部 Agent 机制的附属调研与演进台账，不是 PuddingClaw Harness 的主架构文档。产品设计、当前边界与长期入口以 [PuddingClaw Harness Engineering 整合说明](./puddingclaw-harness-engineering.md) 为准。
>
> **归因边界**：Grok Build 只为权限确定性快路径、TodoGate 等少量机制提供过参考。PuddingClaw 的三控制面、Session/Checkpoint/Trace 权威边界、Goal/Run 状态机、Rubric/Effective Contract、Docker Backend、外部 Artifact/Directory/Attachment lease、上下文压缩、Toolset、协议修复与前端产品化，主要来自 PuddingClaw 自身需求、DeepAgents/LangGraph 能力和多轮真实 Session 复盘。

状态：**方案已审核并持续实现于 `main` 工作区；项目后端测试集 670 项、前端生产构建、TaskProfile/Effective Contract、Manager 级动态问数、Goal/Rubric UI 与真实 Docker Backend E2E 已通过**
日期：2026-07-17
Grok Build 源码：<code>b189869b7755d2b482969acf6c92da3ecfeffd36</code>
PuddingClaw 基线提交：<code>7fb380f43be9c9b13fd3478bb28ef1a637fe6203</code>
说明：PuddingClaw 分析与实现以当前 `main` 工作区为准；Session、Checkpoint、Trace 的权威边界保持本文冻结结论。

## 0. 审核冻结说明

### 0.1 当前产品定位

本轮不把 PuddingClaw 重构成通用 Agent 基座。当前 PuddingClaw 明确按 **智能问数 Agent** 继续开发，先完成一条可用、可控、可验收的垂直闭环。通用基座只在智能问数 Agent 完成、第二个真实产品（例如人脉管理 App）出现后，从两个具体实现中抽取交集。

本方案当前不做：

- 不迁移现有智能问数模块；
- 不引入 AgentProfile / Domain Pack / 通用 ContractCompiler；
- 不拆分现有 <code>DeepAgentsAgentManager</code>；
- 不修改现有 Session JSON、Trace、LangGraph checkpoint 的权威边界；
- 不开发多 Skeptic；
- 不在本轮抽取通用 Agent 基座；先让智能问数 Agent 的 Harness 闭环稳定。

### 0.2 已同意进入审核的三个控制面

| 控制面 | 建议组件 | 控制对象 | 一句话职责 |
|---|---|---|---|
| Action Control | <code>ToolExecutionPipeline</code> | 每一次 Tool Call | 动作能不能做、以什么权限和沙箱边界执行 |
| Completion Control | <code>CompletionVerificationCoordinator</code> | 每个需要验收的 Run | 本 Run 是否满足 Rubric，还是在 Run 内带 gaps 有限修正 |
| Lifecycle Control | <code>HarnessRunCoordinator</code> | 产品级 Run | Run 当前处于什么状态、下一步允许迁移到哪里 |

用户显式开启 Goal Mode 时，额外由 <code>GoalCoordinator</code> 消费各 Run 的验证报告，负责跨 Run 推进。DeepAgents 继续拥有模型—工具内循环；上述组件不重写 DeepAgents Agent Loop。

### 0.3 机制来源标记

本文后续设计使用以下来源标签：

- **[Grok Build 借鉴]**：源码中存在对应机制，PuddingClaw 按自身技术栈适配；
- **[DeepAgents 复用]**：直接利用当前依赖提供的能力；
- **[PuddingClaw 延续]**：项目已经明确或实现的设计，不归因于 Grok Build；
- **[本方案新增]**：结合 PuddingClaw 产品边界提出的协调或抽象；
- **[暂不采用]**：机制有参考价值，但不进入本轮开发范围。

### 0.4 2026-07-16 实现进度

本轮已经落地：

- <code>ToolExecutionPipeline</code>：所有 Agent Tool 先经过显式分类；未知或未分类 Tool 默认拒绝，网络 Tool 与高风险命令进入 HITL；
- <code>DockerWorkspaceBackend</code> / <code>RestrictedHostWorkspaceBackend</code>：同一 Backend 协议，Docker 按项目复用、项目级命令串行、空闲自动停止；
- <code>HarnessRunCoordinator</code> / <code>GoalCoordinator</code>：Session JSON 是跨 Run 唯一产品状态权威，同一 Session 只允许一个非终态 Run 和一个 active Goal；
- <code>PuddingClawRubricMiddleware</code>：执行顺序为 Todo/产物 deterministic gate → 单 Grader 严格 JSON 验收 → RunOutcome；复用 DeepAgents 的回跳与有限迭代，但不依赖供应商 function calling；
- Goal 前后端：输入区显式开关、Session 级 Goal/Run/Verification 状态、暂停/恢复/取消与右侧面板；
- Harness Settings：Goal/Rubric 高级规则、Docker 配置与 daemon probe；
- ModelCallLimit 已映射为 Run 级 <code>budget_exceeded</code>，不会在 Goal 尚有 Run 预算时误终止整个 Goal。
- 显式 Goal 无条件生成并冻结 Goal contract；后续“继续”“确认”等短消息继承原合同，不能通过关键词启发式绕过验收。

### 0.5 2026-07-17 Action Control 与 Composer 收口

本轮新增实现按来源区分如下：

- **[Grok Build 借鉴]**：所有 Tool Call 统一进入 `ToolExecutionPipeline`；决策保持 `deny > ask > allow`，未知动作 fail-closed；授权与策略版本绑定，不依赖模型自觉遵守；
- **[PuddingClaw 延续]**：Session JSON 仍是跨 Run 唯一产品状态权威，LangGraph checkpoint 只恢复同 Run HITL，Trace 只记录；
- **[本方案新增]**：Session 提供 `strict` / `smart` 两种审批模式，默认 `strict`；Session 创建时与分析模型一起原子写入，Run 开始后冻结为不可变 `RunPermissionContext`；
- **[本方案新增]**：授权携带 `run_id`、`policy_epoch`、`policy_version`、backend/workspace binding，Session 切换审批模式会递增 epoch 并撤销旧 Tool grant；活动 Run 存在时禁止切换；
- **[本方案新增]**：`smart` 仅自动放行确定性低风险联网工具（Tavily、通过 SSRF 校验的公开网页抓取）；Docker 动态装包仍需用户审批，但可给当前 Session 的类型化安装能力；
- **[本方案新增]**：动态依赖通过类型化 `install_packages` 工具进入一次性联网 installer 容器；长期项目容器保持 `network=none`，原始 Shell 安装命令不能继承这一可复用授权；
- **[本方案新增]**：网页抓取执行公开地址校验、DNS 全地址校验、连接 IP 固定、HTTPS SNI/证书主机名校验、跳转重验、peer IP 校验、内容类型与解压后体积限制；
- **[本方案新增]**：Docker runtime/dependency volume 绑定解析后的不可变 image ID，而不是可漂移 tag；默认镜像为 `puddingclaw/sandbox:python3.12-node22-chromium-v4`；
- **[本方案新增]**：`/skills`、`/knowledge`、`/semantic-assets`、`/sql-guardrails`、`/analytics-models` 同时在 Docker mount 与 DeepAgents 虚拟文件路由层强制只读，外部写权限不能覆盖这一系统约束；
- **[产品交互借鉴] + [本方案适配]**：Composer 外层只保留项目目录与审批模式；模型、附件、Goal 收入 `+`；思考模式移到右侧。Popover 互斥，Goal 是“下一 Run 意图”，只有收到 `run_started` 才消费。

第二轮对抗式状态审查进一步补强：旧 Run 恢复时保留冻结的 policy version；删除 Session、message/trace/metadata 写入使用同一锁且写入口不再隐式重建 Session；metadata 只能更新显式白名单字段；外部文件审批 API 必须匹配 pending request 类型；Restricted Host identity 对 workspace 稳定；Permission HITL 持久化 `waiting_hitl → running`；LangGraph 重放使用确定性 permission request ID；Docker spec 在 Run 内变化时明确失败；终态 Run 的 verification contract 不可再改；Session 权威写入口校验 Run 初始状态与合法迁移。

第三轮对抗式审查围绕“Python 联网失败”和“验收器异常”补强：

- **[本方案新增]**：Shell policy 与 Docker backend 共用同一个 capability classifier，分别识别 `network_access`、`managed_write`、`package_install`；执行层不得在 policy 已经 allow 后再偷偷增加联网或写入能力；
- **[本方案新增]**：普通联网 Python/Node 命令经用户批准后使用临时 bridge 容器；纯网络读取时 workspace 保持只读，只有分类结果明确包含写入时才给可写 mount；
- **[本方案新增]**：单次 Tool grant 绑定发起它的 `run_id`；Session 级 grant 才允许在绑定一致的新 Run 中复用；
- **[本方案新增]**：若 `/skills` 等系统只读资源同时位于 project workspace 内，Docker 增加 `/workspace/...` 嵌套只读 mount，DeepAgents 文件路由也拒绝该别名，避免通过第二条路径绕过；
- **[DeepAgents 复用修复]**：Trace middleware proxy 保留 `__can_jump_to__`，确保 deterministic gate 的 `needs_revision → model` 真正形成 LangGraph 条件边；
- **[本方案新增]**：`grader_error` 只表示 grader 实际失败；流程没有形成终态时使用 `verification_incomplete`，未执行 criterion 显示为“未执行”而不是“未通过”，并且内部流程失败不消耗 Goal 业务轮次；
- **[本方案新增]**：deterministic gate 与 LLM grader 共用单一 `_verification_attempts` 计数；每次 natural stop 只计一次，不能通过两套独立计数把 `max_iterations=2` 实际扩成三轮；
- **[本方案新增]**：verification pack 的“任务适用性”和 evidence 的“是否充分”分开；语义上发生了网页/分析动作就激活 pack，non-material evidence 由 deterministic verifier fail-closed。本地 Skill 哈希只因路径被提及时不会误激活 web pack。

Skill 生命周期不继续增加 Python 命令白名单。当前已提供类型化 `inspect_skill`：只读返回本地版本、文件清单、逐文件哈希与聚合哈希，不执行 Skill 代码，并可由 Harness 确定性放行。下一步 `refresh_skill` 必须建立在可信更新 manifest/source registry 上，固定展示来源域名、写入目标和依赖计划，再按 `network_access + managed_write + package_install` 能力审批；在这个协议完成前，不执行远端 `install.sh`，也不把“Python”整体视为低风险。

验收边界：`backend/tests` 为当前项目正式测试集，结果为 **670 passed**；本轮涉及核心文件 Ruff 通过；前端 Next.js production build 通过；Playwright 在 390×844 视口确认 Composer 为两行布局，审批菜单未越界。直接对整个 `backend` 目录运行 pytest/Ruff 会额外收集历史 `skills` 样例与 vendored `vanna`，目前存在既有同名测试模块收集冲突和 lint 基线，不能误报为本轮全仓通过。

本轮尚未宣称完成：

- Goal 的跨 Run token/cost/wall-time 组合预算；当前默认 Goal 总预算只按 <code>max_rounds</code>；
- 更完整的智能问数 analytics verifier（指标口径、Join、时间范围、数据覆盖）；
- 多 evaluator / skeptic panel。

## 1. 结论先行

PuddingClaw 不需要复制 Grok Build 的 Rust SessionActor，也不应在 DeepAgents 外再造一套模型—工具循环。DeepAgents 已经承担了内层 Agent Loop、Todo、Filesystem、Subagent、Summarization 和 HITL 基础设施。

PuddingClaw 真正需要建设的是位于 DeepAgents 之上的 **产品级 Harness Control Plane**：

1. 在不重写 DeepAgents 内循环的前提下，统一定义 Run、Goal、Tool、Permission、Verification 的状态与终态；
2. 把所有工具纳入同一条风险分类和执行前策略管线；
3. 把“模型说完成了”升级为 Harness 可验证的 RunOutcome；只有用户主动开启 Goal 时，才进一步形成跨 Run 的 Goal 状态迁移；
4. 在已经明确的 Session JSON、LangGraph checkpoint、Trace、Artifact 权威范围上增加 Run/Goal 生命周期状态机；
5. 在已有 Context GC 之外补齐执行预算、验收预算和 OS 级隔离。

一句话目标：

> DeepAgents 负责“怎样循环”，PuddingClaw Harness 负责“为什么继续、允许做什么、何时算完成、状态如何恢复”。

## 2. PuddingClaw 当前 Harness 地图

~~~mermaid
flowchart TB
    API["FastAPI / SSE"] --> Manager["DeepAgentsAgentManager<br/>Host + Stream Adapter + Persistence Orchestrator"]
    Manager --> DA["DeepAgents compiled agent<br/>Model ↔ Tool inner loop"]
    Manager --> Session["SessionManager<br/>UI history / todos / trace / context / grants"]
    Manager --> Checkpoint["LangGraph Checkpointer<br/>same-run HITL resume"]
    DA --> MW["PuddingClaw Middleware"]
    MW --> Context["Memory / Semantic Assets / Skills / Toolset / Compaction"]
    MW --> Guard["External-file Permission / Workspace Router"]
    DA --> Tools["Native FS + PuddingClaw business tools"]
    Manager --> Trace["TraceCollector + SSE"]
~~~

### 2.1 已有能力

| Harness 领域 | 当前实现 | 评价 |
|---|---|---|
| 内层 Agent Loop | DeepAgents <code>create_deep_agent</code> | 强，交给依赖库是正确方向 |
| Prompt/Context | 项目 Prompt、Memory、Analytics Model、Semantic Assets、Skills | 强 |
| 动态工具面 | SkillIntentRouter + ToolsetMiddleware | 强，已经是硬执行边界 |
| 上下文 GC | DeepAgents Summarization + Tool Context 即时/后台压缩 | 很强，是当前优势 |
| 模型调用上限 | ModelCallLimitMiddleware | 已有，但只覆盖 call count |
| Todo | DeepAgents Todo + PuddingClaw 白盒持久化 | 已有状态，没有独立完成门 |
| Subagents | general-purpose + 配置化 subagents + image analyzer | 已有 |
| HITL | 外部文件、维度规则、逻辑数据集、SQL revision | 分领域成熟，尚未统一 |
| Partial run | 运行中快照、取消/异常保存、缺失工具结果合成 | 强 |
| Trace | State/Model Input/Middleware/Tool/Todo 白盒事实 | 强 |
| Analytics Goal | 无持久化 Goal 状态机 | 缺失 |
| Analytics Verification | 无 Harness-owned completion verifier | 缺失 |
| OS sandbox | Terminal 实际为 host shell + blacklist | 高风险缺口 |
| 统一 Tool Policy | 权限与路由按具体 tool name 分散 | 缺失 |

## 3. 最值得保留的 PuddingClaw 设计

### 3.1 UI 历史与模型上下文分离

Tool Context 方案保留完整 UI output，同时维护模型专用 <code>context_output</code> 和原始证据引用。它比“把整个会话总结成一段”更符合 Harness 的证据保真。

证据：

- <code>backend/graph/middlewares/tool_context_compaction.py:204</code>
- <code>backend/graph/middlewares/tool_context_compaction.py:457</code>
- <code>backend/graph/session_manager.py:1041</code>
- <code>backend/graph/session_manager.py:1248</code>

这是 PuddingClaw 可以反向输出给其他 Agent 产品的差异化能力，不应被通用 Goal 改造破坏。

### 3.2 Session 是聊天连续性的主存储

现有设计已经明确：

- Session JSON 保存用户实际看过的 durable history；
- LangGraph checkpoint 只服务同一活跃 run 的 HITL resume；
- checkpoint thread 使用 <code>session_id:query_id</code>；
- 普通 follow-up 从 Session 重建，不 replay 旧 graph。

证据：

- <code>backend/graph/deepagents_manager.py:2494</code>
- <code>backend/graph/deepagents_manager.py:1805</code>
- <code>backend/graph/session_manager.py:1624</code>

这个边界已经明确，后续不重新设计：Session JSON 继续是跨 Run 权威，LangGraph checkpoint 继续只负责同 Run HITL 恢复。新增状态机只能在这个边界上协调状态迁移，不能创建第二套恢复权威。

### 3.3 Skill 触发与 Tool 能力是两层

SkillIntentRouter 只推荐；ToolsetMiddleware 根据已成功读取的 Skill 决定模型能看见和真正能执行哪些业务工具。

证据：

- <code>backend/graph/middlewares/toolset.py:39</code>
- <code>backend/graph/middlewares/toolset.py:93</code>
- <code>backend/graph/middlewares/toolset.py:115</code>
- <code>backend/graph/middlewares/toolset.py:153</code>

这是很好的 Architectural Constraint。后续 Tool Policy 应叠加在其下，不应把它改回 Prompt 软约束。

### 3.4 Trace 记录事实边界，而不只记录解释

TraceCollector 已记录最终 Model Input、tool schema、指纹、State/Middleware/Todo 等事实。Grok Build 的 telemetry 思路可以接入这个体系，无需另建第二套 tracing。

证据：

- <code>backend/graph/trace_collector.py:86</code>
- <code>backend/graph/trace_collector.py:234</code>
- <code>backend/graph/trace_collector.py:1602</code>
- <code>backend/graph/trace_collector.py:1741</code>

## 4. 关键差距一：缺少统一 Tool Control Plane

### 4.1 当前权限是“按工具补洞”

ExternalFilePermissionMiddleware 只检查：

- read_external_file；
- read_resource；
- read_file；
- edit_file；
- write_file。

见 <code>backend/graph/permission_middleware.py:63</code>、<code>permission_middleware.py:85</code>。

WorkspacePathRouter 只认识 read_file、glob、grep 的路径字段：

- <code>backend/graph/middlewares/workspace_path_router.py:20</code>
- <code>workspace_path_router.py:89</code>

这意味着权限判断依赖具体 tool name 和参数名，新的 Tool 如果忘记接入，就可能绕过统一审查。

### 4.2 Terminal 目前不是真正 sandbox

<code>SafeTerminalTool</code>：

- 自己标记为 <code>risk_level = dangerous</code>；
- 仅通过字符串 blacklist 判断；
- 使用 <code>subprocess.run(..., shell=True, cwd=root_dir)</code>；
- cwd 不是文件系统隔离，绝对路径、<code>..</code>、网络和子进程仍可用。

证据：

- <code>backend/tools/terminal_tool.py:13</code>
- <code>backend/tools/terminal_tool.py:33</code>
- <code>backend/tools/terminal_tool.py:41</code>
- <code>backend/tools/terminal_tool.py:79</code>
- <code>backend/tools/terminal_tool.py:81</code>

因此当前 description 中“sandboxed environment”属于能力声明过强。这个问题优先级高于增加更多 Agent 角色。

### 4.3 Effective Manifest 核心机制已经存在，缺少的是统一投影与对账

<code>_tool_inventory</code> 无条件列出 DeepAgents 的 <code>execute</code>，但当前 backend 是普通 FilesystemBackend，并不实现 SandboxBackendProtocol；DeepAgents 会在 model-call 边界过滤 execute。PuddingClaw 同时又挂载了自定义 <code>terminal</code>。

证据：

- <code>backend/graph/deepagents_manager.py:927</code>
- <code>backend/graph/deepagents_manager.py:563</code>
- <code>backend/.venv/lib/python3.12/site-packages/deepagents/middleware/filesystem.py:1720</code> 附近

但 PuddingClaw 已经存在 Effective Manifest 的核心事实链：

1. <code>ToolsetMiddleware.wrap_model_call/awrap_model_call</code> 根据已读取 Skill 动态过滤 <code>request.tools</code>；
2. 最终 <code>ModelClientChatModel</code> 记录实际绑定的 tools；
3. <code>TraceCollector.add_model_input_span</code> 将最终 <code>tool_schemas</code> 写入每次 <code>model.input</code> span 的 <code>model_call_contract</code>；
4. <code>tool_schema_hash</code> 可以判断两次 Model Call 的有效工具面是否发生变化。

证据：

- <code>backend/graph/middlewares/toolset.py:93</code>
- <code>backend/graph/middlewares/toolset.py:170</code>
- <code>backend/llm/model_client.py:147</code>
- <code>backend/llm/model_client.py:638</code>
- <code>backend/graph/trace_collector.py:234</code>
- <code>backend/tests/test_trace_collector.py:101</code>

因此这里不是新增一套 Effective Manifest 机制。真正缺口是：

- Runtime Inventory 仍是启动/挂载视角，可能展示“已挂载 execute”，而当轮模型实际未看到；
- 已有最终 <code>tool_schemas</code> 目前主要嵌在 <code>model.input</code> contract 中，没有统一命名为 <code>effective_manifest</code>；
- UI/Trace 需要清楚区分 mounted inventory 与每轮 effective manifest；
- 需要增加两者的对账和漂移诊断，而不是重新计算或维护第二份工具清单。

Grok Build 在这里提供的是对照命名和产品呈现启发，PuddingClaw 的最终 ModelRequest 记录才是权威事实。

## 5. Action Control：ToolExecutionPipeline 目标设计

来源：**[Grok Build 借鉴] + [PuddingClaw 延续] + [本方案新增]**。

- Grok Build 借鉴：类型化 Tool 协议、AccessKind 分类、preflight、managed policy、permission、OS sandbox、错误回填模型；
- PuddingClaw 延续：ToolsetMiddleware、外部文件授权、WorkspacePathRouter、Tool Context、Trace；
- 本方案新增：把分散能力收口为 PuddingClaw 的 <code>ToolExecutionPipeline</code>，并以未知 Tool 默认拒绝作为完整性约束。

新增一个统一的 ToolDescriptor Registry：

~~~text
ToolDescriptor
  name
  kind                  # filesystem_read / filesystem_write / shell / network / database / publish / delegation
  risk_level            # safe / sensitive / dangerous / irreversible
  access_classifier     # 从 args 提取 path, host, command, dataset, write_set
  permission_policy     # allow / ask / deny / managed
  concurrency_policy    # parallel / serial / keyed_lock
  timeout_policy
  output_policy
  verifier_hint
~~~

所有工具执行前统一经过：

~~~text
Toolset visibility
  → JSON/schema validation
  → ToolDescriptor lookup
  → Access classification
  → Managed policy
  → Session grant / HITL
  → Sandbox capability check
  → Dispatch
  → Normalized ToolResult
  → Trace + context output policy
~~~

建议新增模块：

| 模块 | 职责 |
|---|---|
| <code>backend/harness/tool_catalog.py</code> | ToolDescriptor、注册与启动时完整性校验 |
| <code>backend/harness/tool_policy.py</code> | 风险分类、managed policy、session grant |
| <code>backend/harness/tool_middleware.py</code> | 统一 awrap_tool_call 执行边界 |
| <code>backend/harness/tool_result.py</code> | success/error/cancelled/denied 统一结果 |
| <code>backend/harness/sandbox.py</code> | sandbox backend 能力与不可用时 fail-closed |

启动时必须验证：模型可见的每个 Tool 都有 Descriptor。未知工具不能默认 allow。

### 5.1 统一 Workspace Backend：不是删除 FilesystemBackend，而是升级默认 Backend

来源：**[DeepAgents 复用] + [PuddingClaw 延续] + [本方案新增]**。

当前 Agent 模式使用：

~~~text
PermissionedCompositeBackend
  default = FilesystemBackend(workspace)
  routes  = knowledge / skills / semantic-assets / analytics-models ...

额外挂载自定义 terminal
  → host subprocess
~~~

审核通过后的目标结构：

~~~text
PermissionedCompositeBackend
  default = WorkspaceExecutionBackend
              ├─ DockerWorkspaceBackend
              └─ RestrictedHostWorkspaceBackend
  routes  = 保留现有 FilesystemBackend 路由

DeepAgents 内置 execute
  → ToolExecutionPipeline
  → default WorkspaceExecutionBackend.execute()
~~~

这里的关键结论是：

1. **不删除 FilesystemBackend**：workspace 文件读写、virtual path、Composite routes 仍然依赖它；
2. **不再把“fs backend”和“sandbox backend”做成两套 Agent 架构**：统一 Backend 都具备文件能力，只是 execution adapter 不同；
3. **Agent 模式迁移完成后不再使用“纯 FilesystemBackend + 独立 terminal”作为正常路径**；
4. **Docker 关闭或不可用时仍返回同一种 Backend 协议**，只是实现切换为受控宿主执行；
5. **模型始终只看到 DeepAgents 内置 <code>execute</code>**，不能主动选择 Docker 或 Host，也不能通过旧 <code>terminal</code> 绕过管线；
6. 现有 <code>PermissionedCompositeBackend</code> 继续作为外层路由与 exact external write grant 适配器，不需要推翻。

建议的基础类型：

~~~python
class WorkspaceExecutionBackend(FilesystemBackend, SandboxBackendProtocol):
    """统一的项目文件 + 命令执行 Backend 契约。"""


class DockerWorkspaceBackend(WorkspaceExecutionBackend):
    """文件操作落在宿主 workspace，execute 在项目 Docker 容器内运行。"""


class RestrictedHostWorkspaceBackend(WorkspaceExecutionBackend):
    """Docker 未启用/不可用时的 best-effort 受控宿主执行，不宣称真沙箱。"""
~~~

不建议第一版直接继承 DeepAgents <code>BaseSandbox</code>，因为 PuddingClaw 的 workspace 已经是宿主持久化目录，文件工具继续通过 <code>FilesystemBackend</code> 访问更符合当前架构。Docker Backend 只需要把同一目录挂载到容器，并补充 <code>id</code>、<code>execute</code>、<code>aexecute</code>。

DeepAgents 0.6.12 已提供 <code>SandboxBackendProtocol</code> 和内置 <code>execute</code>，但没有开箱即用的本地 Docker Backend：

- <code>backend/.venv/lib/python3.12/site-packages/deepagents/backends/protocol.py:803</code>
- <code>backend/.venv/lib/python3.12/site-packages/deepagents/backends/__init__.py:16</code>
- <code>backend/.venv/lib/python3.12/site-packages/deepagents/middleware/filesystem.py:815</code>

因此 <code>DockerWorkspaceBackend</code> 属于 PuddingClaw adapter，不是重复实现 DeepAgents Agent Loop。

### 5.2 Docker 配置属于 Harness Settings，Run 只固化配置快照

来源：**[本方案新增]**。

Docker 是否启用、怎样连接本机 Docker、使用什么镜像和资源限制，由用户在 Harness Settings 中配置。Run 开始时读取一次有效配置，并把选择结果固化到 RunState；运行中修改设置不改变已经开始的 Run。

建议配置：

~~~yaml
harness:
  terminal:
    docker_enabled: true
    on_unavailable: restricted_host   # restricted_host | deny
    permission_mode: strict           # strict | smart
    remember_session_approvals: true

    docker:
      connection: auto                # auto | context
      context: desktop-linux
      image: puddingclaw/sandbox:python3.12-node22-chromium-v4
      cpu_limit: 2
      memory_limit_mb: 2048
      pids_limit: 128
      default_timeout_seconds: 120
      network_enabled: false
      dependency_setup_enabled: false
      lifecycle: project
      idle_stop_minutes: 30
~~~

Harness Settings UI 至少提供：

- 启用/关闭 Docker Sandbox；
- 自动检测 Docker CLI、daemon、context、OS/arch 和镜像状态；
- 测试连接、测试启动 Sandbox；
- 默认展示不可编辑的 PuddingClaw 托管镜像；“使用自定义镜像（高级）”、运行时说明与 image reference 输入框全部包装在独立卡片中，开启后在卡片内部展开输入框；
- CPU 核数与内存使用固定预设下拉框（2/4/8/16 核，2/4/8/16 GB），另提供进程数、超时和默认网络开关；
- 可选的项目 manifest/lockfile 依赖准备高级开关放在运行时说明之后的独立警示区域，默认关闭；历史隐式开启配置一次性迁移回关闭；
- Docker 不可用时“降级为本机受控模式”或“拒绝执行”；
- 项目 Sandbox 的运行状态、重启、停止和重置；
- 不提供可直接注入任意 <code>docker run</code> 参数的自由文本入口。

RunState 保存执行事实，而不是只保存用户设置：

~~~json
{
  "execution_backend": "docker",
  "sandbox_scope": "project",
  "sandbox_id": "puddingclaw-project-abc",
  "sandbox_generation": 3,
  "sandbox_strength": "container_isolated",
  "approval_mode": "smart",
  "policy_epoch": 3,
  "policy_version": "tool-execution-v2",
  "image_digest": "sha256:...",
  "fallback_reason": null
}
~~~

Docker 主动关闭时使用 <code>restricted_host</code>，这是用户选择，不叫 fallback。只有 Docker 已启用但 CLI、daemon、image 或安全运行条件不可用时，才记录 <code>fallback_reason</code>。

自动降级只允许发生在命令执行前的 Backend preflight。某个 Run 已经在 Docker 中执行后，如果 Docker 中途故障，不能把失败命令静默转到宿主重放；应返回 <code>sandbox_unavailable</code>，由用户决定后续是否切换。

用户选择项目并启用 Docker Backend 后，PuddingClaw 在首次实际使用时按项目生命周期准备默认托管镜像和项目容器；这是 Backend provisioning，不要求用户再进入设置页执行“联网准备”。镜像缺失时 Docker 自行拉取或构建；第三方 Skill 缺包时，Agent 在当前对话中发起类型化安装请求并直接触发 HITL。

### 5.3 Docker 生命周期：当前采用“一项目一个容器”，不是“一 Run 一个容器”

来源：**[本方案新增]**。

当前 PuddingClaw 是本地单用户智能问数 Agent，同一项目的 Run 共享 workspace 和依赖环境。第一版采用：

~~~text
project_id
  → 一个长生命周期 Project Sandbox
  → 多个 Run 通过 docker exec 使用
~~~

无 project 的临时 Agent workspace 使用 <code>unscoped_workspace_id</code> 或 <code>session_id</code> 作为 sandbox scope，避免不同临时 workspace 共享容器。

生命周期：

~~~text
第一次使用项目
  → 创建并启动容器

Run
  → acquire project sandbox lease
  → docker exec
  → release lease

空闲超过 idle_stop_minutes
  → docker stop，不删除

下一次调用
  → docker start

镜像 / mount / resource / policy spec digest 变化
  → 重建容器并递增 generation

用户“重置项目沙箱”
  → 删除并重建
~~~

建议组件：

| 模块 | 职责 |
|---|---|
| <code>backend/harness/backends/base.py</code> | WorkspaceExecutionBackend 契约 |
| <code>backend/harness/backends/docker_workspace.py</code> | DeepAgents SandboxBackendProtocol adapter |
| <code>backend/harness/backends/restricted_host.py</code> | 受控 Host fallback |
| <code>backend/harness/sandbox_manager.py</code> | create/start/lease/exec/idle-stop/recreate/reset |
| <code>backend/harness/backend_selector.py</code> | 根据 Harness Settings 和 probe 选择 Backend |

同一项目多个 Run 可能同时修改文件、安装依赖或运行测试。第一版对 Terminal 使用 <code>project_id</code> keyed lock；Agent 推理、只读业务工具和数据库工具仍可并发。后续只有在能稳定识别纯读命令时，才放开 Terminal read concurrency。

Project Sandbox 用 Docker label 和 spec hash 管理，应用重启后可以重新发现：

~~~text
puddingclaw.managed=true
puddingclaw.project_id=<project_id>
puddingclaw.spec_hash=<digest>
puddingclaw.generation=<n>
~~~

这个选择以“当前本地单用户、project 是主要信任边界”为前提。未来如果进入多用户、多租户、运行不可信第三方代码，或同一项目内需要强 Session 隔离，应增加 <code>lifecycle = run</code> / <code>session</code>，不能继续默认复用项目容器。

### 5.4 Docker mount 与能力边界

来源：**[Grok Build sandbox profile 借鉴] + [本方案新增]**。

项目真实目录 bind mount 到容器：

~~~text
host project path → /workspace:rw
working_dir         = /workspace
~~~

这样 DeepAgents <code>write_file("/workspace/report.py")</code> 与 <code>execute("python /workspace/report.py")</code> 看到同一份文件，不需要同步或上传。

建议 mount：

| 资源 | 容器路径 | 权限 |
|---|---|---|
| 当前项目 workspace | <code>/workspace</code> | rw |
| 项目 Sandbox HOME/cache/env volume | <code>/home/agent</code> | rw |
| 必要 skills | <code>/skills</code> | ro |
| 必要 analytics models | <code>/analytics-models</code> | ro |
| Run 临时目录 | <code>/run/puddingclaw/&lt;run_id&gt;</code> | rw、Run 结束清理 |

禁止 mount：

- 用户整个 Home；
- PuddingClaw 整个源码根或 data 根；
- backend <code>.env</code>、SSH、云凭证；
- Docker socket；
- 宿主根目录、设备和进程命名空间。

容器默认：

~~~text
network none
cap-drop ALL
no-new-privileges
non-root user
pids/memory/cpu limit
不传入 backend secrets
~~~

项目目录是 rw mount，因此 Docker 只能阻止命令影响项目外宿主资源，不能阻止删除整个项目。权限规则、HITL 和可选项目快照仍然必要。

`ro` 不能只停留在 Docker mount。DeepAgents 文件工具仍可能通过宿主路由命中同一来源，因此 `/skills`、`/knowledge`、`/semantic-assets`、`/sql-guardrails`、`/analytics-models` 及其真实宿主根目录都属于 Harness managed resources：`write_file` / `edit_file` 必须在路由层硬拒绝，即使存在外部写 grant 也不能覆盖。

### 5.4.1 托管运行时与项目依赖准备

来源：**[本方案新增]**。

默认托管镜像至少保证：

~~~text
Python 3.12 + pip
Node.js 22 + npm + corepack
仅附带基础 POSIX shell 与 CA 证书
~~~

普通用户不需要选择或上传镜像。PuddingClaw 默认使用 <code>puddingclaw/sandbox:python3.12-node22-chromium-v4</code>；高级用户可以填写本机已有 Docker tag 或 registry image reference，Docker 会按标准规则解析本地镜像或拉取 registry 镜像。Backend 在 Run 开始时把 tag 解析为不可变 image ID 并写入执行快照；项目容器和依赖 volume 都以该 image ID 为键，避免同名 tag 漂移后复用 ABI 不兼容的旧环境。自定义镜像仍必须提供 Python、Node.js、Chromium 与 curl 基础运行时。

PuddingClaw 默认不扫描或安装项目依赖，因为当前产品不是以执行任意项目脚本为主要目标。只有高级用户显式开启 <code>dependency_setup_enabled</code> 后，才扫描项目根及有限层级子目录中的实际配置：

| 项目配置 | 确定性安装命令 |
|---|---|
| <code>pyproject.toml + uv.lock</code> | <code>uv sync --frozen</code> |
| <code>pyproject.toml + poetry.lock</code> | 项目内 <code>.venv</code> + <code>poetry install</code> |
| <code>Pipfile + Pipfile.lock</code> | 项目内 <code>.venv</code> + <code>pipenv sync</code> |
| <code>requirements.txt</code> | 创建项目内 <code>.venv</code> 后安装 |
| <code>package.json + package-lock.json</code> | <code>npm ci</code> |
| <code>package.json + pnpm-lock.yaml</code> | <code>pnpm install --frozen-lockfile</code> |
| <code>package.json + yarn.lock</code> | Yarn 对应的 frozen/immutable 安装 |

manifest 内容、目录和安装命令共同生成 fingerprint。只有 fingerprint 对应的完成标记存在时，才认为该项目依赖已准备；lockfile 变化会自然产生新计划并重新要求安装。

为避免 Linux 容器依赖污染宿主项目：

~~~text
Python: 项目 .venv      → Docker managed volume
Node:   项目 node_modules → Docker managed volume
源码与 lockfile          → host workspace bind mount
~~~

命名卷在容器创建前以无网络的一次性初始化容器修正为宿主 UID/GID，主项目容器继续以 non-root 用户运行。

动态包安装的当前联网闭环：

~~~text
Agent 调用 install_packages(ecosystem, packages)
  → schema 与 registry package syntax 确定性校验
  → ToolExecutionPipeline 分类为 package_install + network_access
  → strict：每次询问；smart：仍询问，但可批准当前 Session 的类型化安装能力
  → 启动一次性 networked installer 容器
  → workspace 只读，runtime/dependency volume 可写，使用 argv 执行而非 shell 拼接
  → installer 容器退出并删除
  → 长期项目容器始终保持 network=none
~~~

当前审批保证的是“用户明确允许这次包安装联网”，不是 registry 域名级 egress 防火墙。顶层包引用拒绝 URL、Git、file/path 和命令选项，但包管理器的传递依赖与 lifecycle scripts 仍可能产生额外网络行为；需要更强供应链边界时，应增加 registry proxy、域名/IP egress allowlist、hash/lockfile 和禁用脚本策略。UI 与 Trace 不得把当前能力宣称成“只访问官方 registry”。

第三方 Skill 的依赖属于运行中动态依赖，不并入项目启动时的 manifest 扫描：

~~~text
Agent 成功读取 /skills/<skill-id>/SKILL.md
  → Skill 流程发现缺少 Python / Node 包
  → 调用类型化 install_packages，不提交任意 pip/npm shell 字符串
  → ToolExecutionPipeline 分类为 package_install + network_access
  → 当前对话直接 HITL 请求 package + network
  → 批准后临时联网安装
  → 回到原 Skill 流程继续执行
~~~

Docker Backend 将项目 Skills 只读挂载到 <code>/skills</code>，Skill 脚本可以直接执行但不能修改 Skill 源码。动态 Python user packages、Node global packages、CLI 与缓存写入项目专属的 Docker runtime-home volume：

~~~text
/home/puddingclaw/.local
/home/puddingclaw/.npm-global
/home/puddingclaw/.cache
~~~

该 volume 按 workspace 与不可变 image ID 复用，不污染宿主 Python/Node 环境，也不要求每个 Run 重装。<code>PATH</code>、<code>PYTHONUSERBASE</code>、<code>npm_config_prefix</code> 和 <code>NODE_PATH</code> 由 Backend 统一设置。Skill 自己要求修改项目 <code>package.json</code> 或项目虚拟环境时，仍写入项目依赖通道并受 workspace write policy 约束。

后续可以为第三方 Skill 增加可选的机器可读 dependency manifest；在此之前，Skill 文档中的安装命令仍必须经过同一确定性权限管线，不能因“来自 Skill”而自动放行。

项目级容器无法在运行后动态增加 bind mount，也不能把 Session 级外部文件 grant 永久暴露给共享容器。外部文件继续优先走类型化工具；确实要交给 Terminal 处理时，授权文件复制到：

~~~text
/workspace/.puddingclaw/runtime/<run_id>/external/<grant_id>/...
~~~

Run 完成后清理，不向容器暴露原始宿主路径。<code>.puddingclaw/runtime</code> 是 Harness 保留目录，Workspace Router、文件 Tool 和 Terminal path policy 必须只允许当前 <code>run_id</code> 访问自己的目录，防止共享项目容器中的跨 Run grant 泄漏。外部写入仍走专用 Tool，不允许 Terminal 直接写 workspace 外路径。

### 5.5 权限规则：借鉴 Grok Build，不让团队维护完整命令清单

来源：**[Grok Build 借鉴] + [PuddingClaw 适配]**。

此前配置示例中的：

~~~yaml
safe_read_commands: allow
package_install: ask
network_access: ask
~~~

是策略语义示意，不是要求用户或产品团队维护所有命令。Grok Build 的真实机制是：

1. Tool Call 先归一化为 <code>AccessKind</code>；
2. 规则只表达 <code>allow / ask / deny</code>、tool filter 与 pattern；
3. 冲突优先级固定为 <code>deny &gt; ask &gt; allow</code>；
4. Bash 通过 parser 拆成每个 chained segment；
5. 解包 <code>timeout</code>、<code>env</code>、<code>nice</code> 等 wrapper，并递归检查 <code>bash -c</code>；
6. 从重定向和命令参数中提取实际文件读写；
7. 无法可靠解析时 fail-closed 到 <code>ask</code>；
8. 用户的 allow-once / allow-session 形成 Session grant；
9. 只有一小组内建安全基线和永不自动放行命令需要人工、版本化维护。

对应 Grok Build 证据：

- AccessKind / Decision：<code>crates/codegen/xai-grok-workspace/src/permission/types.rs:144</code>
- PermissionRule / PromptPolicy：<code>types.rs:283</code>
- <code>deny &gt; ask &gt; allow</code>：<code>permission/policy.rs:110</code>
- chained segment、wrapper、嵌套 shell：<code>permission/policy.rs:64</code>、<code>permission/manager.rs:362</code>
- Shell 文件读写与 symlink target：<code>permission/shell_access.rs:14</code>
- safe/dangerous 小基线：<code>permission/manager.rs:182</code>、<code>manager.rs:311</code>
- sandbox auto-allow 可选且默认 false：<code>xai-grok-sandbox/src/lib.rs:67</code>、<code>xai-grok-shell/src/agent/config.rs:1133</code>

PuddingClaw 建议模型：

~~~text
AccessKind
  filesystem_read
  filesystem_write
  shell
  network
  database_read
  database_write
  package_install
  process_control
  host_control
  publish
  delegation

PermissionRule
  action              # allow / ask / deny
  access_kind
  pattern
  source              # builtin / managed / user / project / session
  scope
  expires_at
~~~

Terminal command analyzer 输出：

~~~text
CommandAnalysis
  segments[]
  executables[]
  read_paths[]
  write_paths[]
  redirect_targets[]
  network_targets[]
  package_operations[]
  process_operations[]
  destructive_operations[]
  shell_features[]
  parse_complete
~~~

第一版不照搬 Grok Build 的全部 PermissionManager，也不引入 LLM auto classifier / YOLO mode。优先移植确定性机制：

- Shell AST 分段；
- wrapper 解包；
- 命令前缀 word-boundary；
- 重定向和文件 operand 提取；
- symlink 真实目标检查；
- 解析失败进入 HITL；
- session grant；
- 决策原因和 trace。

需要移植的关键对抗测试包括：

- <code>ls &amp;&amp; rm -rf ...</code> 不能因第一段安全而整体放行；
- <code>timeout 30 rm ...</code> 必须识别内部危险命令；
- <code>tr</code> 不能错误匹配 <code>truncate</code>；
- <code>tee</code> 不能进入只读白名单；
- <code>rg --pre</code> 不能按普通搜索命令放行；
- command substitution、heredoc、background 等无法可靠解析时必须 ask；
- workspace symlink 指向外部路径时不能绕过路径权限；
- 管道中的每一个 segment 都必须独立检查。

### 5.6 PuddingClaw Terminal 决策顺序

来源：**[Grok Build 借鉴] + [本方案新增]**。

~~~text
execute Tool Call
  → schema validation
  → ToolDescriptor / AccessKind
  → Shell AST + path/network/write analysis
  → hard invariant policy
  → managed/user/project rules
  → session grant
  → deterministic safe fast path
  → HITL if unresolved
  → sandbox capability enforcement
  → Docker or RestrictedHost dispatch
  → normalized ExecuteResponse / ToolResult
  → Trace + RunState
~~~

默认决策建议：

| 分类 | Docker Backend | Restricted Host Backend |
|---|---|---|
| 项目内确定性只读命令 | allow | allow，范围更保守 |
| 测试、脚本、解释器执行 | 首次 ask，可记 Session grant | ask，不提供过宽 grant |
| 项目文件写入 | managed，按 write set 判断 | managed，范围更保守 |
| package install | ask，且需要 network capability | ask；无法可靠限制网络时提示降级风险 |
| network | 默认 deny/ask，由独立 capability 决定 | ask，但明确 best-effort |
| 删除/批量覆盖 workspace | ask 或 deny | ask 或 deny |
| privilege/device/docker/host process control | deny | deny |
| workspace 外宿主路径 | deny，使用专用 Tool / staging | deny，使用专用 Tool |
| 无法解析 | ask | ask |

### 5.6.1 严格审批与智能审批

来源：**[Grok Build managed policy 借鉴] + [本方案新增]**。

审批模式是 Session 配置，不是模型提示词，也不是关闭 HITL 的总开关：

| 模式 | 自动放行 | 仍需询问 | 永不放行 |
|---|---|---|---|
| `strict`（默认） | 确定性本地安全基线 | Tavily、公开网页抓取、Docker 动态装包、项目写入与其他风险动作 | privilege、Docker control、host control、宿主 workspace 外路径等 hard deny |
| `smart` | strict 基线 + Tavily + 通过 SSRF 校验的公开标准端口 HTTP/S 抓取 | Docker 动态装包（可批准当前 Session 的类型化 package capability）、高风险写入等 | 与 strict 相同，hard deny 不因模式改变 |

智能审批不使用 LLM 猜风险。它只减少“可被确定性验证”的低风险常用动作弹窗；`fetch_url` 的 URL 查询参数变化不会重复授权，因为批准对象是受安全校验的工具能力，而不是完整 URL fingerprint。Docker 装包之所以仍询问，是因为它会下载并执行第三方代码，风险显著高于只读检索。

命令名不是最终授权边界。`python3`、`node`、`pytest` 之类入口先由 parser 提取实际能力：仅打印 URL 不获得网络；携带远端 base URL 的测试命令需要 `network_access`；`curl -o` 同时需要 `network_access + managed_write`；安装命令还需要 `package_install`。前端用中文展示这些能力，不再只显示无法解释的 `high`。

Session 与 Run 的权威关系：

~~~text
Session permission config
  approval_mode + policy_epoch + policy_version
        │ start_run（同一文件锁内）
        ▼
RunPermissionContext（不可变）
  run_id + mode + epoch + version + backend_id + workspace_id
        │
        ├─ Tool policy decision
        └─ grant exact bindings
~~~

- Session 创建时，分析模型与审批模式在一次 API 写入中原子落盘；metadata 无权注入 permissions；
- Run 开始和审批模式切换使用同一 Session 锁，因此二者线性化：要么新模式先于 Run 生效，要么切换因 active Run 被拒绝；
- Run 执行 backend 只允许在 `preparing` 阶段绑定一次，不能中途从 Docker 静默换到 Host；
- grant 必须匹配当前 Run 的 policy/backend/workspace bindings；终态 Run、旧 epoch、已删除 Session 或并发已解决的请求都 fail-closed；
- Session grant 是产品状态，不是恢复源；同 Run HITL 仍由 LangGraph checkpoint 恢复。

Docker 与 HITL 是两回事：

- Docker 决定“在哪里执行、最大影响范围”；
- Policy/HITL 决定“这个动作是否允许”；
- Docker 开启后可以减少普通命令的打扰，但不能取消权限管线；
- 不默认采用 Grok Build 可选的 <code>sandbox.auto_allow_bash</code>；若未来提供，也必须在 hard deny/ask 与路径分析之后生效。

## 6. 关键差距二：智能问数 Todo 有了，Run Completion Gate 与可选 Goal 还没有

PuddingClaw 当前持久化 todos：

- <code>backend/graph/session_manager.py:600</code>
- <code>backend/graph/session_manager.py:609</code>

也在 stream 中同步 DeepAgents state。基线源码原本没有统一的 Run Completion Gate，也没有用户显式开启后才生效的 GoalTracker；本分支已经补入上述控制面。

基线中的 ModelCallLimit 只防 runaway：

- <code>backend/graph/deepagents_manager.py:664</code>

它不能解决 premature completion。本分支保留它作为 Run 内熔断器，并把其终态接入 Run/Goal 预算状态，但完成质量仍由 deterministic gate + Rubric 负责。

## 7. Completion Control：不移植 Grok skeptic panel，先复用 DeepAgents Rubric

来源：**[Grok Build 借鉴] + [DeepAgents 复用] + [本方案新增]**。

- Grok Build 借鉴：Generator–Evaluator 分离、Goal gaps/continuation、预算与完成权由 Harness 持有；
- DeepAgents 复用：<code>RubricMiddleware</code> 的状态字段、needs_revision 回跳、有限迭代与终态语义；
- 本方案新增：在 Rubric 前加入智能问数 deterministic checks，并由 <code>CompletionVerificationCoordinator</code> 将验收结果映射为 RunOutcome；只有请求携带 active Goal 时，再由 <code>GoalCoordinator</code> 更新 GoalState；
- PuddingClaw 适配：Grader 使用无 Tool 的严格 JSON 协议，不使用 DeepAgents 默认 <code>response_format</code> 所触发的强制 <code>tool_choice</code>。原因是部分 thinking 模型即使切换“非思考模型名”仍拒绝 function-calling structured output；
- 暂不采用：Grok Build 多 Skeptic majority-refute。

当前安装的 DeepAgents 0.6.12 已提供 <code>RubricMiddleware</code>：

- 调用 state 传入 rubric 时才启用；
- 在 Agent 自然结束后调用独立 grader；
- needs_revision 时注入 gaps 并 jump 回 model；
- max_iterations 有 1–20 的硬上限；
- 结果保存在 <code>_rubric_status</code> 和 <code>_rubric_evaluations</code>。

证据：

- <code>backend/.venv/lib/python3.12/site-packages/deepagents/middleware/rubric.py:218</code>
- <code>.../rubric.py:298</code>
- <code>.../rubric.py:625</code>

这与 Grok Build 的 Generator–Evaluator 思路同源，但成本和复杂度更适合 PuddingClaw 第一阶段。

### 7.1 PuddingClaw 必须补的外层语义

RubricMiddleware 文档明确指出：grader_error、failed、max_iterations_reached 不会自动修改最终 assistant message。调用方必须检查状态。

因此 PuddingClaw 不能只把它插入 middleware 列表，还要：

1. 在 <code>PuddingClawAgentState</code> 暴露本 Run 的 rubric 字段；Goal 字段仅在 Goal Mode 开启时存在；
2. 在 final_state 检查 <code>_rubric_status</code>；
3. 将 satisfied、needs_revision exhausted、verification_incomplete、grader_error 映射为不同 RunOutcome；
4. 将 Run verification report 写入 Session；仅当存在 <code>goal_id</code> 时再写 GoalState；
5. 通过 SSE/Trace 向 UI 呈现“已验证、未通过、验收流程未完成、验收器异常”；
6. 非 satisfied 不得一律发普通 <code>done</code>。

### 7.1.1 为什么不直接照搬默认 structured-output grader

真实 E2E 暴露了两个供应商适配问题：

1. Gateway 与 direct provider 都可能把配置中的基础模型路由为 thinking 模式，并拒绝 <code>tool_choice=any</code>；
2. <code>ModelClientChatModel(streaming=False)</code> 若未同步设置 LangChain 的 <code>disable_streaming</code>，回调环境仍可能错误进入 <code>_astream</code>。

当前实现因此采用：

- Rubric 模型显式 <code>thinking_enabled=false</code>，默认使用 <code>fallback_llm.model</code>；
- Grader 直接调用模型，要求只返回一个 JSON object，再用 DeepAgents 的 <code>GraderResponse</code> schema 校验；
- Grader 不绑定 Tool，trace 中最终成功请求的 <code>tool_choice=null</code>；
- non-streaming wrapper 同时设置 provider streaming 与 LangChain <code>disable_streaming</code>。

保留 DeepAgents RubricMiddleware 的 evaluator loop，不等于必须保留其 provider-specific structured-output transport。

### 7.2 验证分级

| 等级 | 任务 | 验证方式 |
|---|---|---|
| L0 | 普通问答/闲聊 | 无 rubric，保持当前路径 |
| L1 | 有明确产物的短任务 | 单 Rubric grader，最多 1–2 次 |
| L2 | 代码、数据分析、发布 | Deterministic checks + Rubric |
| L3 | 高价值、长时自治 | 多 evaluator / skeptic panel，后续再做 |

不要让所有用户问题都支付 verifier 税。

### 7.3 Rubric 默认自动生成，高级自定义规则开放到 Harness Settings

来源：**[本方案新增]**。

普通用户不维护 Rubric。默认流程是：

~~~text
用户本次 Run 的任务与约束
  → TaskProfileClassifier 只根据本轮请求识别任务类型
  → RunRubricCompiler 选择 core / web_research / analytics / artifact / code pack
  → 自动分配 deterministic / analytics / llm verifier
  → 固化 declared Run verification contract
  → 当前 Run 成功 Tool 事件单调扩展 Effective Verification Contract
  → 若用户已开启 Goal，再关联 Goal contract
~~~

高级用户可以在 Harness Settings 中追加或强化验收规则，但不能从零接管整个 Rubric，也不能关闭系统的数据正确性、安全和证据底线。

第一版高级文本规则只允许选择 <code>llm_grader</code> 或已注册的 <code>analytics</code> verifier。不能把一段自然语言规则标成 <code>deterministic</code>；真正的 deterministic verifier 必须由代码注册，否则只是伪确定性。

建议设置模型：

~~~yaml
harness:
  completion:
    rubric:
      enabled: true
      max_iterations: 2
      custom_rules_enabled: true
      custom_rules:
        - id: custom_root_cause_quantification
          enabled: true
          scope: global                 # global | project
          project_id: null
          task_types: [root_cause]
          statement: 主要原因必须给出贡献量或影响量级
          required: true
          verifier: analytics_check
          verifier_ref: contribution_quantification
~~~

高级设置 UI 使用表单，不要求用户直接编辑 YAML：

- 规则名称与验收描述；
- 适用范围：全部任务、指定智能问数任务类型、指定项目；
- required / advisory；
- 从注册表选择 verifier；
- 启用、停用、复制和删除用户自定义规则；
- 展示规则来源和最终是否进入本次 Rubric。

规则来源与约束：

| 来源 | 是否可编辑 | 能否被用户规则覆盖 | 用途 |
|---|---|---|---|
| system mandatory | 否 | 否 | SQL 合法、指标一致、证据可追溯、安全底线 |
| managed policy | 管理员/产品配置 | 否 | 企业或部署级要求 |
| Harness Settings custom | 高级用户 | 只能追加或强化 | 用户长期偏好 |
| user message constraints | 当前用户消息 | 不能削弱系统规则 | 本任务显式要求 |
| task-generated criteria | Builder 自动生成 | 可去重，不可覆盖更强规则 | 当前任务特定标准 |

RubricCompiler 合并时采用“更严格者胜出”：

- 同一 criterion 同时出现 required 与 advisory，最终为 required；
- 用户规则可以增加 criterion，不能把 mandatory 改成 optional；
- 冲突规则进入 <code>rubric_compile_error</code>，不得默默选择宽松版本；
- 每个需要验收的 Run 启动时固化 <code>rubric_version</code> 和 <code>contract_hash</code>；
- active Goal 额外固化跨 Run 的 Goal contract；设置变更不追溯修改已启动的 Run 或 active Goal；
- 每条最终 criterion 必须记录 <code>source</code>，验证报告能说明它来自系统、设置、用户消息还是任务生成。

安全限制：

- Harness Settings 不能直接配置任意 shell command 作为 verifier；
- <code>verifier_ref</code> 只能引用 Tool/Verifier Registry 中已注册、带权限和超时策略的验证器；
- 自定义 LLM criterion 可以使用 <code>llm_grader</code>，但不能修改 grader system prompt；
- 高级用户关闭自定义规则不影响 system mandatory 和 managed policy；
- 自定义规则数量、LLM verifier 数量和验证预算必须有上限。

### 7.4 模型选择、任务画像与 Effective Contract 必须解耦

来源：**[本方案新增，经过对抗性审查]**。

用户在项目中选中分析模型，只表示该模型上下文对 Agent **可用**，不能直接证明本轮是智能问数任务：

~~~text
selected analytics model = available context
current user request       = Run TaskProfile
successful current tools   = runtime activation ledger

Effective Verification Contract
  = declared TaskProfile packs
  ∪ current-Run successful Tool packs
~~~

当前实现遵守以下不变量：

1. <code>analytics_model_id</code> 不参与 TaskProfile 与 contract hash；同一任务选中或不选中模型，验收语义必须一致；
2. 任务画像只读取当前 Run 的用户请求，不读取 Session 历史 Skill 或历史 ToolMessage；
3. 只有当前 <code>query_id</code> 下成功完成的精确 Tool 事件可以激活 runtime pack；失败、pending、非零退出码、伪造近似名称和历史事件都不能激活；
4. <code>VerificationActivationMiddleware</code> 同时安装在主 Agent 与 Subagent，写入 Session JSON 中的 Run-local 幂等账本；Trace 继续只记录，不作为恢复权威；
5. activation 只表示“本轮实际使用了某种能力”，不能直接充当 acceptance evidence；验收证据还必须带当前 Tool call 的输出摘要/hash、source/result 引用、成功退出码或产物写入路径；
6. 网页与分析证据拆成 <code>web_evidence_traceability</code> 和 <code>analytics_evidence_traceability</code>，逐 pack fail-closed，网页来源不能冒充分析查询结果；
7. Rubric Grader 运行前，<code>PuddingClawRubricMiddleware</code> 必须从该账本生成并写入 Effective Contract，再开始 deterministic gate 和 LLM grader；
8. Run 进入 evaluating 时原子冻结 Tool ledger 并物化 Effective Contract；正常完成、失败、取消和预算终止都通过 Session 原子 terminalize，stale graph state 不得覆盖权威 ledger；
9. required criterion 只要缺失、重复、未通过或仍带 gap，整体状态就不能是 <code>satisfied</code>；
10. Goal 冻结基础接受标准，但其有效 pack 只能跨 Run 单调增加；即使某一 Run 失败或被取消，已经成功形成的 runtime pack 也不会从 Goal contract 丢失；
11. 分析模型的完整语义资产与 system prompt 仅在 analytics 路径激活时加载；普通新闻、代码和通用任务只保留“模型可用”事实，不支付完整上下文税；
12. 用户关闭本 Run 验收时，TaskProfile 仍可用于路由，但 runtime activation 不得重新隐式开启 Rubric。

典型兼容场景：

| 已选分析模型 | 本轮任务 | 初始 Pack | 运行时变化 |
|---|---|---|---|
| 是 | AI 最新新闻 | core + web_research | fetch/AIHOT 成功后补来源证据，不加入 analytics |
| 是 | 修改代码并运行测试 | core + code | 测试/构建成功形成 code evidence，不加入 analytics |
| 是 | 普通解释/翻译 | 无 Rubric 或 Goal 指定的 core | 不注入完整分析模型上下文 |
| 是/否 | “继续处理”，随后读取问数 Skill 并实际执行 SQL | 初始 general | 成功数据库 Tool 在 Grader 前激活 analytics；所选模型本身不改变验收 |
| 任意 | 生成报告并分析指标 | core + analytics + artifact | Tool 账本补分析证据和产物确定性检查 |

## 8. Run Verification 与可选 Analytics GoalState

Rubric 首先是 **Run 级验收机制**，不是 Goal Mode 的附属能力。

默认产品语义：

~~~text
用户发送一次请求
  → 创建一个 Run
  → 模型与 Tool 在同一 Run 内工作
  → deterministic checks + Rubric
  → needs_revision 时在同一 Run 内有限修正
  → 产生 RunOutcome
  → Run 结束
~~~

例如“刷新 6 月的销售分析报告”，即使使用 Rubric 检查月份、指标口径、图表和引用，也默认由一个 Run 完成。若验证未通过且 Run 内修正次数耗尽，则向用户展示 gaps 和 <code>verification_failed</code>，不得自动创建 Goal 或偷偷启动下一个 Run。

Run 级验证记录：

~~~text
RunVerificationState
  run_id
  rubric_id
  rubric_version
  rubric_contract_hash
  rubric_criteria[]
  status                  # pending / evaluating / satisfied / needs_revision / verifier_error / exhausted
  iteration
  max_iterations
  evaluations[]
  gaps[]
  evidence_refs[]
  verification_report_ref
~~~

只有用户在前端主动勾选 Goal Mode，才创建以下跨 Run 状态：

~~~text
GoalState
  goal_id
  objective              # 当前智能问数目标
  goal_contract_id
  goal_contract_version
  goal_criteria[]
  status                 # active / paused / achieved / blocked / budget_exceeded / cancelled
  round
  max_rounds
  token_budget
  model_call_budget
  tool_call_budget
  started_at
  updated_at
  gaps[]
  evidence_refs[]
  evaluations[]
  verification_report_ref
  blocked_streak
~~~

Run 与 Goal 的关系：

- 一次用户请求默认创建一个 Run，并应尽量在该 Run 内完成；
- Rubric 可以只审查 Run，不要求存在 Goal；
- Goal Mode 默认关闭，系统不得根据任务复杂度自动开启；
- 只有用户主动勾选 Goal Mode，才创建或关联 Goal；
- 一个显式 Goal 可以跨多个 Run；
- RubricMiddleware 处理单 Run 内的 generator–evaluator 迭代；
- <code>CompletionVerificationCoordinator</code> 生成每个 Run 的 <code>RubricEvaluationReport</code> 与 <code>RunOutcome</code>；
- 存在 <code>goal_id</code> 时，外层 <code>GoalCoordinator</code> 再消费 Run 报告，决定 Goal achieved、继续 active、paused 或 blocked；
- 无 Goal 时，Run 结束即回到普通等待输入状态，不自动跨 Run 续跑；
- 用户取消 Run 不等于取消 Goal；
- verifier_error 不等于 achieved。

### 8.1 Goal Budget 与 ModelCallLimit 的边界

两者不是同一个预算：

- <code>ModelCallLimit</code> 是单 Run 的即时熔断器，当前可限制 <code>run_limit</code> / <code>thread_limit</code>；
- <code>Goal Budget</code> 是跨 Run 累计账本，当前第一阶段以 <code>max_rounds</code> 作为默认总预算；
- Rubric 的 <code>max_iterations</code> 是单 Run 内的验收修正上限，也不等于 Goal 预算。

当前实现已经做到：

- ModelCallLimit 触发时，Run 终态为 <code>budget_exceeded</code>，并记录 <code>run_model_call_limit</code> 或 <code>thread_model_call_limit</code>；
- 若 Goal 仍有剩余 Run，Goal 保持 active，可由用户下一次显式继续；
- 最后一个允许的 Run 仍未完成时，Goal 立即进入 <code>budget_exceeded</code>，原因是 <code>goal_max_runs</code>；
- Goal 面板展示累计模型调用数，但这不代表已经实现完整 token 预算。

后续组合预算建议：

~~~yaml
goals:
  budget:
    max_runs: 8
    max_total_model_calls: null
    max_total_tokens: null
    max_elapsed_minutes: null
~~~

所有可计费模型调用（主 Agent、Rubric grader、子 Agent）最终都应进入 Run usage，再聚合到 Goal usage。该账本必须写入 Session JSON；Trace 只能记录，不作为恢复或预算权威。

当前 <code>model_call_count</code> 主要来自主 Agent 的 ModelCallLimit 状态；Rubric grader 由独立的 <code>max_iterations</code> 有界控制，但尚未并入 Goal 的统一 token/call/cost 总账。这是后续组合预算工作，不应把当前 UI 数字解释为完整计费模型调用量。

审核通过后的组件边界与当前实现映射：

| 模块 | 职责 |
|---|---|
| <code>backend/harness/models.py</code> | Run/Goal/Verification 类型、序列化、合法迁移与 RunOutcome |
| <code>backend/harness/coordinators.py</code> | HarnessRunCoordinator、GoalCoordinator、CompletionVerificationCoordinator |
| <code>backend/harness/deterministic_checks.py</code> | Todo reconciliation、artifact existence 等代码验证器 |
| <code>backend/harness/rubric_compiler.py</code> | 合并 system/managed/settings/user/task criteria 并生成 DeepAgents rubric string |
| <code>backend/graph/deepagents_manager.py</code> | PuddingClawRubricMiddleware 执行 deterministic gate → Rubric，并把事件接入 Run 生命周期 |
| <code>backend/graph/session_manager.py</code> | Session JSON 中 Run/Goal/permission 的原子权威写路径 |

## 9. Lifecycle Control：在既定权威边界上补齐 HarnessRunCoordinator

来源：**[PuddingClaw 延续] + [Grok Build 借鉴] + [本方案新增]**。

- PuddingClaw 延续：Session JSON 是跨 Run 权威；Trace 只记录；同 Run HITL 只依靠 LangGraph 原生 checkpoint；workspace/result store 保存真实世界事实；
- Grok Build 借鉴：把 session、turn、goal、retry、compaction 看成边界明确的嵌套状态机，并为取消、预算、验证和恢复定义独立终态；
- 本方案新增：用 <code>HarnessRunCoordinator</code> 协调 PuddingClaw 产品级 Run，不新增 checkpoint、不让 Trace 参与恢复、不引入第二个状态权威。

### 9.1 已冻结的权威边界

| 状态 | 权威来源 | 说明 |
|---|---|---|
| 用户可见消息、Tool cards、Todo、Goal、权限 | Session JSON | 跨 Run 产品状态 |
| 同 Run HITL 执行位置 | LangGraph checkpoint | 使用 <code>session_id:query_id</code>，仅原 Run resume |
| Trace | Trace sidecar | 记录、解释、展示，不参与控制和恢复 |
| 文件、SQL result、Artifact | Workspace / Result Store | 真实世界事实，Session 保存引用 |
| Model Context Snapshot | Session 中的可重建缓存 | 丢失时从 Session history/context_output 重建 |

本轮不引入 RunJournal/Event Sourcing。若未来确实出现并发写入、崩溃重放或审计需求，再把它作为 Session 持久化的内部实现评估，不能成为新的权威来源。

### 9.2 RunState

~~~text
preparing
running
waiting_hitl
evaluating
completed
cancelled
failed
blocked
budget_exceeded
verification_failed
~~~

典型迁移：

~~~text
preparing → running
running → waiting_hitl → running
running → evaluating
evaluating → running                 # needs_revision
evaluating → completed               # satisfied
evaluating → verification_failed     # grader error / exhausted
running → cancelled / failed / budget_exceeded
~~~

### 9.3 GoalState

~~~text
active
paused
blocked
achieved
cancelled
budget_exceeded
~~~

Run 与 Goal 必须分离：

- 未开启 Goal Mode：只创建 Run，不创建 GoalState；
- 用户停止本轮：Run cancelled，Goal 可保持 active/paused；
- 模型/API 异常：Run failed，Goal 不得自动 cancelled；
- Rubric needs_revision：优先在同一个 Run 内 evaluating → running；
- Rubric satisfied：无 Goal 时仅表示 Run completed；有 Goal 时再由 GoalCoordinator 判断 Goal achieved 或保持 active；
- Run 内修正耗尽：无 Goal 时返回 verification_failed + gaps；有 Goal 时由 GoalCoordinator 决定是否留待后续 Run 推进；
- verifier_error：Run verification_failed，Goal active/paused，不得 achieved。

### 9.4 HarnessRunCoordinator 的职责

<code>HarnessRunCoordinator</code> 始终存在，负责：

- 创建 Run 并写入初始状态；
- 接收 Tool permission、HITL、取消、异常、预算和验证事件；
- 校验状态迁移是否合法；
- 始终协调 RunState；仅在请求关联 Goal 时协调 GoalState；
- 通过现有 SessionManager 持久化；
- 把状态投影给 Trace 与 SSE；
- 只有在状态提交完成后才允许发送最终 <code>done</code>。

它不负责：

- 重写 DeepAgents Model ↔ Tool 内循环；
- 保存 checkpoint；
- 保存 Trace 内容；
- 重新设计 Session 数据库；
- 自己执行 Tool 或自己充当 Rubric Grader。

## 10. 三个控制面的落点约束

当前不拆分 <code>DeepAgentsAgentManager</code>。审核通过后的首版以窄接口嵌入现有流程：

| 组件 | 首版落点 | 与 DeepAgents 的关系 |
|---|---|---|
| <code>ToolExecutionPipeline</code> | 统一 <code>awrap_tool_call</code> middleware + sandbox adapter | 包住每次 Tool Call |
| <code>CompletionVerificationCoordinator</code> | natural stop / after_agent + final_state 处理 | 对每个需要验收的 Run 组合 deterministic checks 与 RubricMiddleware |
| <code>GoalCoordinator</code> | RunOutcome 提交之后；仅 Goal Mode 开启时 | 消费 Run verification report，推进跨 Run Goal |
| <code>HarnessRunCoordinator</code> | 现有 <code>astream</code> 外层生命周期 | 消费事件并持久化 Run/Goal 状态 |

只有当这三个控制面在现有智能问数 Agent 中稳定并出现真实维护压力时，才评估拆分 Manager 或抽取通用 Agent 基座。

### 10.1 Goal Mode 当前状态与产品边界

PuddingClaw 当前分支已经实现 Goal Mode 的第一阶段前后端闭环：后端具有可运行的 <code>GoalRecord</code> / <code>GoalCoordinator</code>，前端具有默认关闭的 Goal 开关、Session 隔离状态、GoalCard 与 VerificationCard。

Goal Mode 必须作为一项前后端同时交付、且由用户主动开启的产品能力，不能只在后端增加状态字段：

- Goal Mode 默认关闭；每次用户请求默认只创建一个 Run；
- 未开启 Goal 不代表禁用 Rubric：有明确产物或分析结果的 Run 仍可自动获得 Run 级 Rubric；
- Run 级 Rubric 的 needs_revision 在同一 Run 内有限修正；
- 用户可在 Agent 输入区显式开启“目标”模式；
- 开启后，首条消息创建一个可跨多个 Run 延续的 Analytics Goal；
- 同一 Session 首版只允许一个 active Goal，降低恢复和交互复杂度；
- 后续消息默认归属当前 active Goal，直到 achieved、cancelled 或显式退出；
- 系统不得因为任务复杂、Run 验证失败或修正耗尽而自动开启 Goal；
- Run 完成不等于 Goal 完成，前端必须分别展示本轮结果与长期目标状态。

产品行为冻结为：

| 用户操作 | 产品对象 | Rubric | 允许的推进范围 | Run 结束后 |
|---|---|---|---|---|
| 不勾选“目标” | 只创建 Run | 可按任务类型自动启用 | 只在当前 Run 内进行模型/Tool 循环和有限 verifier 回跳 | 返回 RunOutcome，等待用户下一次输入 |
| 主动勾选“目标” | 创建 Run + Goal | 每个 Run 独立验收，GoalCoordinator 聚合 | 当前 Run 内修正；必要时可在后续 Run 继续同一 Goal | Goal 可保持 active，直到 achieved/paused/cancelled |

这里的“一个 Run 搞定”不等于只调用一次模型。一个 Run 内仍可包含多次模型调用、Tool Call、Todo 推进和 Rubric needs_revision 回跳，但不能在用户未开启 Goal 时自动创建第二个 Run。

截图所示的“目标”入口可作为交互借鉴，但 PuddingClaw 不照搬其未知内部实现。该入口是 **[产品交互借鉴]**，Goal/Run/Rubric 契约是 **[本方案新增]**。

### 10.2 输入区 Goal 与审批模式交互

当前 Composer 采用“高频上下文在外、低频配置收纳”的布局：

~~~text
左侧：+ ｜项目目录｜严格审批/智能审批       右侧：思考｜上下文｜发送

+ 菜单：附件｜分析模型｜目标
~~~

- 项目目录和审批模式持续可见，因为它们直接定义本 Run 的工作范围和权限边界；
- 附件、分析模型和 Goal 收入 `+` 菜单，避免底栏横向拥挤；
- 思考模式放到右侧并使用 `aria-pressed` 表达开关状态；
- 所有 Popover 互斥，`Escape` 先关闭当前 Popover，只有没有弹层时才进入停止 Run 的快捷行为；
- 390px 宽度下 Composer 自动分成上下两行，菜单不得越出视口。

首版交互：

1. 未开启时从 `+` 菜单点击“目标”，设置“下一 Run 开启 Goal”的输入意图；
2. 本次发送携带 <code>goal_mode=true</code>，后端创建 Goal；
3. Goal 创建后，入口变为带状态的 active chip，例如“目标进行中”；
4. 点击 active chip 可查看目标描述、当前状态、剩余预算、gaps，并执行暂停、恢复或取消；
5. Goal 达成后显示“目标已完成”，下一次发送默认回到普通模式，除非用户新建 Goal；
6. 用户取消当前 Run 时不自动取消 Goal；“停止本轮”和“取消目标”必须是两个不同操作。

Goal intent 不能在点击开关或发起 HTTP 请求时提前消费。前端冻结本次发送的 model/project/runtime/goal/approval snapshot，只有收到后端 `run_started` 才清除“下一 Run”意图；网络失败或 Session 创建失败后用户可直接重试，不会静默丢失 Goal。Session 尚不存在时，分析模型与审批模式随 Session create 原子提交，避免先建默认 Session、随后 PATCH 造成首个 Run 使用错误配置。

审批模式入口同样位于 Composer 外层：

- `strict` 中文显示“严格审批”，`smart` 显示“智能审批”；
- 菜单解释智能模式会自动放行只读网页检索，但 Docker 装包仍需审批；
- 活动 Run 期间禁用模式切换；切换请求携带 `expected_epoch`，后端用乐观并发拒绝陈旧 UI；
- 模式是 Session 级设置，而真正执行时使用 Run 冻结快照，前端展示不得暗示它会改变正在运行的 Run。

未点击“目标”时，请求明确携带 <code>goal_mode=false</code> 或省略该字段，并保持“一次请求、一个 Run”的默认路径。即使该 Run 启用了 Rubric，也不显示 active Goal chip，不写 GoalState，不自动发起后续 Run。

请求契约首版最小字段：

```json
{
  "goal_mode": true,
  "goal_id": "goal_01...",
  "message": "分析最近三个月销量下降原因"
}
```

- 创建 Goal 时 <code>goal_id</code> 为空，由后端返回；
- 延续 active Goal 时携带已有 <code>goal_id</code>；
- 用户不直接编辑完整 Rubric；显式要求继续写在自然语言消息中；
- 高级 Rubric 自定义规则仍位于 Harness Settings，只影响新 Run；已创建 Goal 的 Goal contract 同样保持冻结。

### 10.3 Frontend State、API 与 SSE 契约

<code>frontend/src/lib/store.tsx</code> 已增加按 Session 隔离的 Goal 产品状态，并保持它不混入 Todo 或 Trace：

```ts
type GoalStatus =
  | "draft"
  | "active"
  | "waiting_hitl"
  | "evaluating"
  | "paused"
  | "achieved"
  | "blocked"
  | "budget_exceeded"
  | "cancelled";

interface GoalViewState {
  goalId: string;
  objective: string;
  status: GoalStatus;
  currentRunId?: string;
  gaps: string[];
  rubricVersion?: string;
  verificationReportId?: string;
}
```

建议增加：

- <code>goalModeEnabled</code>：输入区下一次发送是否创建 Goal；
- <code>activeGoal</code> / <code>activeGoalBySession</code>：当前 Session 的 active Goal；
- <code>goalHistoryBySession</code>：历史 Goal 摘要；
- <code>verificationReportByRun</code>：每个 Run 的逐 criterion 验证结果；
- <code>latestGoalDecisionByGoal</code>：仅 Goal Mode 下保存跨 Run 聚合决定；
- <code>currentRunOutcome</code>：本轮终态，不与 Goal status 混用；
- <code>InspectorActiveTab</code> 增加 <code>goal</code> / <code>verification</code>。

后端 API 至少提供：

- 创建或获取 Session active Goal；
- 查询 Goal 详情与历史；
- 暂停、恢复、取消 Goal；
- 查询 Run 级 <code>RubricEvaluationReport</code>；
- 查询 Goal 的跨 Run进度摘要与最近一次 GoalDecision；
- 查询最后一次 <code>RunOutcome</code>。

SSE 增加以下领域事件：

| 事件 | 前端用途 |
|---|---|
| <code>goal_created</code> | 将输入区切换为 active Goal，并保存后端生成的 goal_id |
| <code>goal_updated</code> | 更新 objective、budget、gaps 等产品状态 |
| <code>goal_status_changed</code> | 更新 active / paused / evaluating / achieved 等状态 |
| <code>rubric_compiled</code> | 显示验收维度已生成，不展示内部 prompt |
| <code>verification_started</code> | 进入验证中状态 |
| <code>criterion_evaluated</code> | 增量展示单项验证、证据和 gap |
| <code>verification_report</code> | 固化完整验证报告 |
| <code>run_outcome</code> | 展示本 Run 的唯一终态及与 Goal 的关系 |

现有 <code>done</code> 事件只表示本次 SSE/模型输出传输结束。非 Goal 模式根据 <code>run_outcome</code> 展示本 Run 是否完成；Goal Mode 下只有 <code>run_outcome=completed</code> 且 <code>goal_status_changed=achieved</code> 才展示“目标已完成”。

SSE 事件必须携带 <code>session_id</code>、<code>run_id</code> 和可用于幂等处理的事件标识；仅 Goal Mode 事件要求携带 <code>goal_id</code>。这可以避免切换 Session 或断线重连时把旧 Run/Goal 投影到当前页面。

### 10.4 Goal 面板与验证报告

在现有 <code>frontend/src/components/citations/SourcesPanel.tsx</code> 的 Progress、Permissions、Sources 旁增加：

- <code>GoalCard</code>：目标、状态、当前 Run、预算、未解决 gaps、暂停/恢复/取消操作；
- <code>VerificationCard</code>：Run 级 Rubric 总结、逐项结果、criterion source、verifier、evidence、gap；没有 Goal 也可以显示；
- Goal Mode 下验证未达成时，“继续推进”触发新的 Run 并保持同一 Goal；
- 非 Goal 模式下 Run 验证失败只展示 gaps，由用户决定是否重新发送；不得自动跨 Run；
- blocked、budget_exceeded、grader_error 必须使用不同文案，不能统一显示为“失败”；
- 普通聊天没有 Goal 时不显示空 Goal 面板。

Harness Settings 的前端同时增加 Goal / Rubric 高级设置区：

- Goal 默认预算和最大修正轮数；
- 全局/项目级自定义 Rubric 规则；
- rule scope、required/advisory、registered verifier；
- 明确提示 Rubric 设置只影响新 Run；active Goal 的 Goal contract 不被追溯修改。

首版前端涉及文件预计为：

| 文件 | 改造职责 |
|---|---|
| <code>frontend/src/components/chat/ChatInput.tsx</code> | Goal 开关、active chip、Goal 操作入口 |
| <code>frontend/src/lib/store.tsx</code> | Session 级 Goal/Report/RunOutcome 状态与 SSE reducer |
| <code>frontend/src/lib/api.ts</code> | Goal、verification API 与发送请求字段 |
| <code>frontend/src/components/citations/SourcesPanel.tsx</code> | GoalCard、VerificationCard |
| <code>frontend/src/app/settings/page.tsx</code> | Goal/Rubric Harness Settings |
| <code>frontend/src/lib/settingsApi.ts</code> | Goal/Rubric 配置类型与序列化 |

## 11. 三大支柱下的 PuddingClaw 差距矩阵

| 机制 | 当前 | 缺口 | 下一步 |
|---|---|---|---|
| Context assembly | 强 | 来源与预算策略仍分散 | ContextManifest + final input facts |
| Tool Context GC | 强 | 缺总 Run token budget | 增加 run/goal budget |
| Tool visibility | 强，已有每轮最终 tool schemas | 静态 inventory 与 effective manifest 可偏离，缺统一投影 | 复用 model.input contract，增加对账与 UI 命名 |
| Tool constraints | 中 | 只覆盖部分路径工具 | ToolDescriptor + deny unknown |
| Shell safety | 弱 | blacklist 不是 sandbox | OS/container sandbox + HITL |
| Persistence | 强 | 缺统一 Run/Goal 状态迁移入口 | 保持 Session JSON 权威，由 HarnessRunCoordinator 写状态 |
| Todo/progress | 中强 | 无 turn-end completion gate | CompletionGate |
| Goal orchestration | 弱 | 无用户显式开启的跨 Run Analytics GoalState | GoalCoordinator |
| Verification | 弱 | 无智能问数 grader/status | RubricMiddleware + analytics checks |
| Subagents | 中强 | 无统一预算/权限/验收角色 | 继承策略 + verifier agent |
| Observability | 强 | control outcome 未标准化 | RunOutcome/GoalDecision spans |

## 12. 分阶段落地路线

> 开发门禁：以下阶段只有在本方案经用户审核通过后才启动。

### P0：状态契约 + Action Control 安全闭环

目标：先冻结 Run/Goal/ToolResult 的最小状态契约，并保证“模型能做什么”与“UI/Trace 说它能做什么”一致。

1. 定义最小 <code>RunState</code>、<code>GoalState</code>、<code>RunOutcome</code>、<code>ToolResult</code>；
2. 定义 <code>WorkspaceExecutionBackend</code>，统一 Docker 与 Restricted Host 两种 execution adapter；
3. 实现 Harness Settings 中的 Docker probe、启停、fallback 和 Run 配置快照；
4. 实现一项目一个容器的 ProjectSandboxManager、keyed lock、idle stop、reset 和 spec generation；
5. 项目目录只挂载到 <code>/workspace:rw</code>，禁止 Docker socket、Home、secrets 和宿主根目录；
6. 将 Agent 模式从自定义 <code>terminal</code> 迁移到 DeepAgents 内置 <code>execute</code>；
7. 建立 ToolDescriptor Registry，未注册工具 fail-closed；
8. 移植 Grok Build 的 AccessKind、allow/ask/deny、Shell segment/parser、wrapper、路径/重定向分析和 Session grant 语义；
9. Terminal 默认只自动放行确定性低风险集合，解析失败进入 HITL；
10. 从已有 <code>model.input.model_call_contract.tool_schemas</code> 投影 Effective Manifest，并与 Runtime Inventory 对账；
11. 为绝对路径、<code>..</code>、管道、重定向、命令替换、heredoc、network、后台进程、symlink 和 prefix collision 写对抗测试；
12. 迁移完成前保留旧 <code>SafeTerminalTool</code> 作为兼容路径，但停止宣称 sandbox；迁移完成后 Agent 模式不再挂载它。

验收：

- 未授权命令不能读取 workspace 外文件；
- Docker 模式下未授权命令不能建立网络连接；
- Restricted Host 模式对已知网络命令执行 deny/ask，并明确标记无法为任意解释器代码提供强网络隔离；
- Docker 启用时同一项目多个 Run 复用同一 Sandbox generation；
- Docker 未启用/不可用时仍经过同一 ToolExecutionPipeline；
- Docker 运行中故障不会把失败命令自动重放到宿主；
- Agent 模式只暴露一个命令执行工具 <code>execute</code>；
- 每个模型可见 Tool 都有风险描述和 policy；
- Trace 中 Effective Manifest 直接来源于真实 ModelRequest，不另建推算清单；
- Runtime Inventory 与 Effective Manifest 的差异有明确原因，例如 backend capability 或 Toolset 过滤。

### P1：Run Completion Gate + 可选 Analytics Goal + 单评估器

目标：模型自然停止不再等同于 Run 完成；Goal 只在用户主动开启时跨 Run 推进。

1. 定义智能问数专用 <code>AnalyticsTaskContract</code>，不抽通用 Contract；
2. 定义 system mandatory、managed policy 和智能问数 task template criterion library；
3. 实现 RubricCompiler，合并 Harness Settings custom rules、用户消息约束和 task-generated criteria；
4. Harness Settings 增加高级 Rubric 规则表单、作用域和 registered verifier 选择；
5. 每个需要验收的 Run 固化 <code>rubric_version</code>、<code>contract_hash</code> 和 criterion source；Goal Mode 开启时再固化 Goal contract；
6. 加入 TodoCompletionGate，最多有限次数 nudging；
7. 先运行 SQL validation、指标口径、Join 路径、时间范围、数据覆盖、引用等 deterministic/analytics checks；
8. 只把 <code>llm_grader</code> criterion 编译成 Rubric string；由 PuddingClaw 的无 Tool JSON grader 执行，DeepAgents RubricMiddleware 负责迭代与状态；
9. final_state 严格处理 rubric 终态；
10. 由 CompletionVerificationCoordinator 汇总为 <code>RubricEvaluationReport</code> 和 RunOutcome；
11. 仅当请求关联 <code>goal_id</code> 时，由 GoalCoordinator 消费报告并生成 GoalDecision；
12. 将 Run evaluation、gaps、证据和 report reference 写入 Session，Trace 只记录；Goal 数据按需写入；
13. 增加 model/tool/token/round 四类预算；
14. Agent 输入区增加默认关闭、必须由用户主动开启的 Goal Mode 开关与 active Goal chip；
15. Store 增加按 Session 隔离的 Goal、VerificationReport、RunOutcome 状态及 SSE reducer；
16. 右侧 Inspector 增加 GoalCard 与可独立显示的 VerificationCard；
17. Harness Settings 增加 Goal 默认预算和高级 Rubric 设置；
18. 前端严格区分 SSE <code>done</code>、RunOutcome 和 Goal achieved。

验收：

- grader needs_revision 会进入下一轮，而不是发 done；
- grader_error 不会显示“验证通过”；
- max iterations 有明确用户可见状态；
- 普通用户不配置 Rubric 也能自动生成完整验收标准；
- Harness Settings 自定义规则只影响新 Run；active Goal 的 Goal contract 同样不被追溯修改；
- 用户规则不能关闭或弱化 system mandatory / managed criteria；
- 自定义 verifier 只能选择 Registry 中已注册项，不能执行任意命令；
- 验证报告能显示每条 criterion 的 source、verifier、evidence 和 gap；
- Goal Mode 开启后的 Goal 可在服务重启后恢复；
- 无 rubric 的普通聊天行为不变；
- Goal Mode 关闭时不创建 Goal，但符合 L1/L2 的 Run 仍可生成和执行 Rubric；
- Goal Mode 关闭时一次请求只创建一个 Run，验证失败不得自动创建后续 Run；
- 输入区与右侧面板不出现伪 Goal 状态，但 VerificationCard 可以展示当前 Run 的验收报告；
- 切换 Session 后只显示该 Session 的 active Goal；
- 取消当前 Run 不会让 Goal chip 错误显示为已取消；
- <code>done</code> 到达但 verification_failed 时，前端不能显示“目标已完成”。

### P2：HarnessRunCoordinator 全链路接入

目标：让 Tool、HITL、Goal、Rubric、取消、异常和预算统一进入合法状态迁移。

1. HarnessRunCoordinator 包住现有 <code>astream</code>；
2. ToolExecutionPipeline 的 permission/result 进入 RunState；
3. CompletionVerificationCoordinator 始终更新 RunState；仅存在 goal_id 时 GoalCoordinator 才更新 GoalState；
4. 取消、异常、budget、verification_failed 映射为互斥终态；
5. 状态先写 Session JSON，再投影 Trace/SSE；
6. 保持 LangGraph checkpoint 只负责同 Run HITL；
7. 为断连、重复终态、服务重启和普通 follow-up 写恢复测试。

验收：

- Run 只有一个互斥终态；
- Session JSON 重启后能恢复 Run/Goal 产品状态；
- 普通 follow-up 不 replay 旧 checkpoint；
- Trace 与 SSE 展示的状态等于 Session 中已提交的状态；
- 用户取消 Run 不会错误取消整个 Goal。

### P3：暂不进入本轮开发

- 多 Evaluator / skeptic panel；
- worktree/rewind；
- 通用 Agent Profile / Domain Pack；
- RunJournal/Event Sourcing；
- DeepAgentsAgentManager 大拆分；
- 面向人脉管理 App 的预先抽象。

## 13. 推荐的 Run 状态机

~~~mermaid
stateDiagram-v2
    [*] --> Preparing
    Preparing --> Running
    Running --> WaitingPermission
    WaitingPermission --> Running: approve/edit
    WaitingPermission --> Cancelled: reject/cancel
    Running --> Evaluating: natural stop + run checks/rubric
    Evaluating --> Running: needs revision
    Evaluating --> Completed: satisfied
    Evaluating --> VerificationFailed: grader error / exhausted
    Running --> Compacting: context threshold
    Compacting --> Running
    Running --> Cancelled: user stop
    Running --> Failed: runtime error
    Completed --> [*]
    VerificationFailed --> [*]
    Cancelled --> [*]
    Failed --> [*]
~~~

终态必须互斥：

~~~text
RunOutcome =
  completed
  cancelled
  failed
  blocked
  budget_exceeded
  verification_failed
~~~

不要再用“有没有 final_content”推断运行成功。

## 14. 配置面建议

当前 Harness Settings 已有 subagents、model call limit、context engineering，并已加入第一阶段 Goal/Rubric/Docker 设置。完整目标配置面如下：

~~~text
harness:
  execution:
    model_call_limit
    tool_call_limit
    token_budget
    max_wall_time_seconds
  completion:
    todo_gate_enabled
    verifier_failure_policy
    rubric:
      enabled
      max_iterations
      custom_rules_enabled
      custom_rules[]
      max_custom_rules
      max_llm_criteria
  permissions:
    default_policy
    permission_mode
    remember_session_approvals
    managed_rules[]
    network_policy
    unknown_tool_policy
  terminal:
    docker_enabled
    on_unavailable
    docker:
      connection
      context
      image
      cpu_limit
      memory_limit_mb
      pids_limit
      default_timeout_seconds
      network_enabled
      dependency_setup_enabled       # default false
      lifecycle                  # project
      idle_stop_minutes
  goals:
    enabled
    activation                    # explicit_user_only
    default_enabled              # false
    auto_promote_from_run        # false
    max_rounds
    blocked_streak_threshold
~~~

注意：

- UI 开关不能代替服务器 managed policy；
- verifier_failure_policy 应按任务风险分级；
- unknown_tool_policy 默认 deny；
- Restricted Host 是 best-effort 降级，不得在 UI/Trace 中标记为真沙箱；
- Docker 配置在 Run 开始时固化，运行中不可静默扩大能力；
- 用户无需维护完整命令表，只需要选择 permission mode、Docker/fallback 和可选高级规则；
- 内建 safe/dangerous 基线由代码版本化维护，并必须带对抗测试。
- 普通用户无需维护 Rubric；高级规则只在 Harness Settings 的 completion/rubric 中开放；
- Rubric 自定义规则只能追加或强化标准，不能覆盖 system mandatory / managed policy；
- Rubric verifier 只能从 Registry 选择，不允许在设置中输入任意 shell command；
- 每个 Run 固化自己的 Rubric 快照，设置变化只影响新 Run；
- active Goal 固化独立的 Goal contract，设置变化不追溯修改；
- Goal Mode 默认关闭且只能由用户显式开启，Run 失败或任务复杂度不得触发自动升级。

## 15. 已审核并冻结的十个决策

1. **不重写 DeepAgents inner loop**：继续复用其 Todo、SubAgent、Summarization、HITL。
2. **先修 Terminal 安全语义**：这是当前最明显的能力—约束不一致。
3. **复用 RubricMiddleware 做第一版 Generator–Evaluator**：不先移植多 skeptic。
4. **把 Session 继续设为跨 Run 权威**：checkpoint 只负责同 Run resume。
5. **复用 Trace 已记录的 effective manifest 事实，并新增 RunOutcome**：不重建第二份工具清单，Trace 不参与恢复。
6. **统一重构 Workspace Backend**：保留 FilesystemBackend 能力，但 Agent 模式不再使用纯 fs-only default + 独立 terminal。
7. **Docker 生命周期按项目复用**：一项目一个容器，Run 获取 lease；不是每 Run 创建容器。
8. **权限借鉴 Grok Build 的确定性管线**：AccessKind、规则、Shell AST、路径分析、Session grant；暂不引入 LLM auto classifier、YOLO 和默认 sandbox auto-allow。
9. **Rubric 默认自动生成，高级规则开放到 Harness Settings**：用户规则只能追加/强化，不能关闭系统强制标准，verifier 必须来自注册表。
10. **Rubric 属于 Run，Goal 必须用户主动开启**：默认一次请求由一个 Run 完成并可独立验收；只有用户勾选 Goal Mode 才创建跨 Run Goal，系统不得自动升级。

## 16. 不应从 Grok Build 照搬的部分

- 不移植 Rust actor/channel 结构；Python/LangGraph 的运行模型不同；
- 不复制多个超大 session 文件；
- 不默认启用三名 skeptic；
- 不立即实现复杂 compaction prefire；PuddingClaw 的 Tool Context GC 已解决当前主要污染；
- 不把所有 state 都塞回 checkpoint；
- 不把 Hook 当安全边界；
- 不因 Docker/Sandbox 已启用就默认 auto-allow 所有 Bash；
- 不在第一版引入 permission LLM classifier 或 YOLO/bypass 模式；
- 不让 Goal 系统侵入普通问答。

## 17. 建议的第一批 Contract Tests

### Tool Policy

- 未登记 Tool 无法执行；
- Toolset 未启用时既不可见也不可调用；
- <code>execute</code> 的绝对路径、<code>../</code>、管道、重定向、命令替换都进入正确策略；
- <code>ls &amp;&amp; rm</code>、<code>timeout rm</code>、<code>tee</code>、<code>rg --pre</code>、<code>tr</code>/<code>truncate</code> 边界与 Grok Build 对抗测试等价；
- Shell 中的 read/write path、redirect、symlink target 能映射到文件权限；
- 无法解析的 shell 结构必须 ask，不得默认 allow；
- permission reject 生成协议完整 ToolMessage；
- 同一 grant 的 scope 与过期语义可验证。

### Sandbox Backend

- Docker 关闭时选择 RestrictedHostWorkspaceBackend；
- Docker 启用且可用时选择 DockerWorkspaceBackend；
- Docker 启用但不可用时按 <code>on_unavailable</code> fallback 或 deny；
- 同一项目多个 Run 复用同一 container generation；
- config/spec hash 变化触发 recreate；
- Run 只申请 lease，不创建独占容器；
- 项目目录在容器中为 <code>/workspace:rw</code>；
- Docker socket、宿主 Home、backend secrets 不可见；
- 外部文件 grant 不形成项目容器永久 mount；
- 同项目 Terminal keyed lock 可防止并发写入竞争；
- Docker 中途故障不会在 Host 自动重放同一命令。

### Goal / Verification

- Goal Mode 默认关闭，未发生用户操作时请求不创建 Goal；
- Goal Mode 关闭时一次请求只创建一个 Run；
- Goal Mode 关闭且任务属于 L1/L2 时仍会生成 Run Rubric 并执行验证；
- Run Rubric 的 needs_revision 只在同一 Run 内有限回跳，不创建第二个 Run；
- 非 Goal Run 验证耗尽时返回 verification_failed + gaps，不自动升级为 Goal；
- Goal Mode 开启后的首条消息创建 Goal，响应 goal_id 回填 active chip；
- 显式 Goal 即使任务不命中 L1/L2 关键词，也必须生成最小 <code>task_fulfillment + todo_reconciliation</code> 合同；
- active Goal 后续 Run 携带同一个 goal_id；
- active Goal 后续短消息继承冻结的 <code>goal_contract</code>，不能退化为 <code>not_required</code>；
- 同一 Session 首版不能并行创建两个 active Goal；
- 切换 Session 后恢复各自的 active Goal，不发生串线；
- SSE 重连或重复事件不会重复创建 Goal 或覆盖较新的状态；
- <code>done</code> 先于 <code>run_outcome</code> 到达时，UI 保持“收尾/验证中”，不提前标记 completed；
- <code>run_outcome=verification_failed</code> 时，即使 SSE 已 done 也不能显示 achieved；
- 暂停/恢复/取消 Goal 与停止当前 Run 的 API 和 UI 语义分离；
- 无用户配置时由系统 mandatory + task template 自动生成 Rubric；
- 用户消息中的显式约束进入 required criterion；
- Harness Settings global/project custom rule 只作用于匹配范围；
- custom advisory 与 mandatory 冲突时最终保持 mandatory required；
- 未注册 <code>verifier_ref</code> 导致 rubric_compile_error；
- 自定义规则不能携带任意 shell command；
- 已启动 Run 的 rubric_version/contract_hash 不随设置修改而变化；
- active Goal 的 goal_contract_version 不随设置修改而变化；
- rubric satisfied → completed；
- needs_revision → 注入 gaps 并继续；
- grader_error → verification_failed；
- thinking 模式开启时，Rubric 请求不携带 Tool schema / 强制 tool_choice，仍能返回并解析严格 JSON；
- max iterations → 非 completed；
- todo 未完成 → 有界 nudge；
- token/model/tool budget 任一耗尽 → budget_exceeded。
- RubricEvaluationReport 包含 criterion source、verifier、evidence、gap 和聚合状态。
- Harness Settings 修改后，已启动 Run 的 rubric snapshot 与 active Goal 的 goal contract 都不变化；
- GoalCard 能区分 active、waiting_hitl、evaluating、paused、blocked、budget_exceeded、achieved；
- 无 Goal 时不展示空 GoalCard；无 Rubric 时不展示空 VerificationCard。

### 本轮冻结：输入资产、外部产物与临时目录的权威边界

1. 用户上传、拖入或粘贴的文件/图片复制到当前 Session 的 <code>attachments</code>，生成稳定的 <code>attachment_id</code>；后续 Goal Run 通过 Session 内引用复用同一份输入快照，不做跨 Session 猜测或全局回退查找。
2. 普通粘贴文本直接保存在消息正文；只有达到大文本阈值时才转存为 attachment。attachment 是输入快照，不是最终交付目录。
3. 用户直接给出的本机路径默认不复制。Harness 保存指向原文件的外部 <code>FileRef</code>，读取、修改和写回均经过 Tool 权限管线；只有用户明确要求冻结/留档时才额外复制到 attachments。
4. 用户指定的外部修改目标是唯一权威交付路径。<code>/workspace</code> 中的副本、attachment 副本及其他派生文件不得冒充原始交付目标。
5. <code>/scratch</code> 是每次 Run 隔离的临时转换与验证目录，映射在 workspace 之外；Run 结束后清理。其产物固定标记为 <code>scope=scratch, role=temporary</code>，不能满足 artifact delivery Rubric。
6. Docker 的隐藏 <code>/harness-scratch</code> 仅供 Backend 映射使用，模型和 Terminal 权限策略禁止直接访问；Host fallback 与 Docker 均只向命令暴露虚拟 <code>/scratch</code>。

### 本轮冻结：Goal 连续性、Todo 所有权与三态语义资产

- Goal 的 Todo ledger 按 <code>goal_id + goal_revision</code> 隔离；同一 revision 的下一 Run 继承进度，新 revision、新 Goal 与普通 Run 不继承旧 Todo。
- 每个 Todo 保留创建/最近变更的 Run 与 query 归属，TodoGate 只验收当前 Goal revision 或当前普通 Run 的事项。
- Goal 跨 Run 的对话仍呈现为同一个助手任务过程；每个持久化 segment 绑定 <code>run_id + goal_id + verification_state</code>，刷新后按 Goal 重建同一消息。Run 边界、预算原因、模型调用和终态进入右侧时间线，不再向聊天区插入伪消息；后一 Run 的验收不能重标或隐藏前一 Run 的候选内容。
- 活跃 Run 中的暂停/取消先写入 <code>requested_status</code>，再中止执行任务；Run 收尾必须通过 Session 单锁下的 compare-and-set 原子读取 <code>current_run_id/objective_revision/requested_status</code>，控制请求与新 revision 优先于 grader 或旧 graph state，避免暂停后被写回 active。跨进程执行者还需在模型、工具与 HITL 流边界轮询持久化控制请求。
- Todo 从中间件数组中消失不代表完成；未先标记 <code>completed/cancelled</code> 的删除转为 <code>removed_unresolved</code> tombstone，继续阻断 TodoGate。
- 语义资产解析采用三态：显式资产 ID 为 strict；仅选择模型时只在该模型资产内匹配；确实未命中时进入 generalized，由模型结合 schema 与用户问题泛化并显式说明假设，而不是 fail-closed。
- verifier/grader/基础设施异常属于控制面说明 <code>control_notices</code>，不得伪装成用户任务未满足的 acceptance gap；只有真实验收失败才进入下一 Run 的修正上下文。

### Persistence

- tool_start 后崩溃 → interrupted terminal record；
- permission pending 时重启 → 同 Run 可恢复；
- Run completed 后 checkpoint 不影响下一 query；
- Run/Goal 状态迁移写入 Session JSON；
- 重复终态不能覆盖第一个合法终态；
- 用户取消 Run 不会自动取消 Goal；
- UI history 与 model context 保持 tool_call_id 一致。

### Observability

- final ModelRequest tool schemas 与 Runtime Inventory 一致；
- 每次 Run 只有一个 terminal RunOutcome；
- verifier、permission、compaction 都有 trace span；
- Trace 能定位对应 session/query/goal。

## 附录 A：机制来源与借鉴边界

| 机制 | 来源判定 | PuddingClaw 采用方式 | 本轮状态 |
|---|---|---|---|
| DeepAgents Model ↔ Tool 内循环 | **[DeepAgents 复用]** | 保持 <code>create_deep_agent</code>，不重写循环 | 保留 |
| Tool 类型化结果、错误不吞、preflight | **[Grok Build 借鉴]** | 适配为 ToolExecutionPipeline 与统一 ToolResult | 已实现并测试 |
| AccessKind、managed policy、permission 分层 | **[Grok Build 借鉴]** | 以 ToolDescriptor、capability 与冻结的 RunPermissionContext 落地 | 已实现并测试 |
| allow/ask/deny、deny > ask > allow | **[Grok Build 借鉴]** | 实现确定性 Tool/Shell policy，未知动作 fail-closed | 已实现并测试 |
| Bash chained segment / wrapper / bash -c 解析 | **[Grok Build 借鉴]** | Python 侧 Shell analyzer，解析失败 ask | 已实现并对抗测试 |
| Shell read/write/redirect/symlink 访问提取 | **[Grok Build 借鉴]** | 进入 filesystem permission 与 execution capability policy | 已实现并对抗测试 |
| Session command grant | **[Grok Build 借鉴] + [PuddingClaw 适配]** | 写入既有 Session permission 状态，并绑定 policy/backend/workspace | 已实现并测试 |
| LLM auto permission classifier / YOLO | **[Grok Build 借鉴候选]** | 第一阶段不采用 | 暂不采用 |
| sandbox auto-allow bash | **[Grok Build 借鉴候选]** | 默认不采用；未来也必须晚于 hard policy/path analysis | 暂不采用 |
| OS 级 sandbox profile 思想 | **[Grok Build 借鉴]** | 不复制 Rust nono；映射为 Docker mount/network/capability profile | 已实现并真实 Docker 验证 |
| DeepAgents SandboxBackendProtocol | **[DeepAgents 复用]** | Workspace Backend 实现 id/execute/aexecute，启用内置 execute | 已实现 |
| DockerWorkspaceBackend | **[本方案新增]** | FilesystemBackend + SandboxBackendProtocol hybrid adapter | 已实现并测试 |
| RestrictedHostWorkspaceBackend | **[本方案新增]** | 同一协议下的 best-effort fallback | 已实现并测试 |
| 一项目一个长生命周期容器 | **[本方案新增]** | project sandbox lease + idle stop + generation | 已实现并测试 |
| effective tool manifest | **[PuddingClaw 延续] + [Grok Build 命名/呈现借鉴]** | 复用已有 model.input 最终 tool schemas，补顶层投影、UI 和 inventory 对账 | 核心机制已有，待产品化 |
| Session JSON 跨 Run 权威 | **[PuddingClaw 延续]** | 保持现状，不重新设计 | 已冻结 |
| LangGraph checkpoint 只负责同 Run HITL | **[PuddingClaw 延续] + [DeepAgents 复用]** | 保持 <code>session_id:query_id</code> | 已冻结 |
| Trace 只记录、不恢复 | **[PuddingClaw 延续]** | 增加 RunOutcome/GoalDecision 观测，不参与控制 | 已冻结 |
| Tool Context output/UI evidence 分离 | **[PuddingClaw 延续]** | 保持当前即时保护和后台压缩 | 已有 |
| TodoGate / 未完成任务 nudge | **[Grok Build 借鉴]** | 结合 DeepAgents Todo，做有限次数 Completion Gate | 已实现并测试 |
| Goal status、budget、gaps、continuation | **[Grok Build 借鉴]** | 仅用户显式开启 Goal Mode 时，由 GoalState 与 GoalCoordinator 跨 Run 推进 | 基础版已实现；组合预算后续增强 |
| 单 Rubric grader | **[DeepAgents 复用] + [本方案适配]** | 复用 RubricMiddleware 的迭代/状态机；PuddingClaw 使用无 Tool 严格 JSON transport，避免 thinking/tool_choice 冲突 | 已实现并 E2E |
| 智能问数 deterministic checks | **[本方案新增]** | SQL、指标、Join、时间、覆盖、引用检查先于 Rubric | 基础版已实现；领域深度持续增强 |
| RubricCompiler 与 criterion source 合并 | **[本方案新增]** | 合并 system/managed/settings/user/task criteria，严格者胜出 | 已实现并测试 |
| Harness Settings 高级 Rubric 规则 | **[本方案新增]** | 普通用户零维护；高级用户只能追加/强化并选择注册 verifier | 已实现并构建验证 |
| CompletionVerificationCoordinator / RubricEvaluationReport | **[本方案新增]** | 对每个需要验收的 Run 汇总 deterministic、analytics 与 LLM criterion evidence | 已实现并测试 |
| Agent 输入区“目标”模式与 active Goal chip | **[产品交互借鉴] + [本方案适配]** | 显式开启 Goal、查看状态并区分停止 Run/取消 Goal | 已实现并构建验证 |
| GoalCard / VerificationCard | **[本方案新增]** | GoalCard 仅 Goal Mode 显示；VerificationCard 可独立展示 Run 级验收 | 已实现并构建验证 |
| Goal SSE 与前端 Session 级状态 | **[本方案新增]** | done 只结束传输，由 RunOutcome/GoalDecision 决定完成语义 | 已实现并测试 |
| HarnessRunCoordinator | **[本方案新增，受 Grok 嵌套状态机启发]** | 协调既有 Session/Checkpoint/Trace，不创建新权威 | 已实现并测试 |
| 多 Skeptic majority-refute | **[Grok Build 借鉴候选]** | 当前不采用，单 Grader 稳定后再评估 | 暂不采用 |
| RunJournal/Event Sourcing | **[本方案讨论后否决]** | 当前 Session JSON 已是明确权威，不增加第二套事实源 | 不采用 |
| 通用 Agent Profile / Domain Pack | **[未来可能抽象]** | 智能问数 Agent 完成且第二个产品出现后再提取 | 暂不采用 |

借鉴原则：

1. 借鉴 Grok Build 的机制和故障边界，不复制其 Rust crate/actor 结构；
2. 能由 DeepAgents 稳定提供的能力优先复用；
3. PuddingClaw 已有的 Session、Trace、Tool Context 设计继续作为产品事实；
4. 本方案新增组件必须说明它解决的 PuddingClaw 具体状态冲突或控制缺口；
5. 未标注来源的机制不得直接进入开发任务。

## 附录 B：Grok Build Harness 的 Notebook 机制拆解

> 本附录保留 Grok Build Hub/Protocol 低层视角，并补充完整仓库中的 Shell/Goal 高层实现。只阅读 <code>xai-computer-hub</code> 子系统会误以为 Grok Build 没有 Agent Loop 或 skeptic panel；完整控制流应以 <code>xai-grok-shell</code> 为准。

### B.1 八大机制在 Grok Build 中的落点

| Notebook 机制 | 解决的故障 | Grok Build 对应实现 | 关键文件与行号 |
|---|---|---|---|
| **Agent Loop 四相循环** | ① 循环失控 | <code>xai-grok-shell</code> 明确实现模型—工具 turn loop，并在外层叠加 goal round；Hub 的 TurnHook 是低层注入点，不是完整 Agent Loop | <code>crates/codegen/xai-grok-shell/src/session/acp_session_impl/turn.rs:1693</code>、<code>turn.rs:1799</code>、<code>turn.rs:759</code> |
| **Tool Use 工具编排** | ④ tool 错误吞 | `ToolServerHandler` trait 要求 `ToolStream` 必须 `Progress* Terminal`；`ToolError` 结构化分类；协议层 `ToolErrorWire` | `xai-computer-hub-sdk/src/server.rs:159-180`（`ToolServerHandler`）<br>`xai-tool-runtime/src/error.rs`（`ToolErrorKind`）<br>`xai-tool-protocol/src/frames.rs`（`ToolErrorWire`） |
| **Progress Tracking 进度追踪** | ⑤ 状态丢失 | 低层 ActivityTracker/ProgressFrame 记录调用进度；高层 GoalTracker、TodoGate 和 persistence 保存任务进度 | <code>crates/codegen/xai-grok-shell/src/session/goal_tracker.rs:428</code>、<code>turn.rs:2119</code>、<code>persistence.rs:306</code> |
| **Context Management 上下文管理** | ② context 溢出、③ cache miss | 独立的 `xai-grok-compaction` crate：token 估计、intra/inter compaction、history validate、code compaction summary；保持 system prompt 前缀稳定 | `crates/common/xai-grok-compaction/src/` 整 crate<br>`xai-grok-compaction/src/intra_compaction/compact.rs`<br>`xai-grok-compaction/src/inter_compaction/compact.rs` |
| **Feature List 任务拆解** | ② context 溢出的源头 | Workspace session 的 todo/plan 能力由上层 shell 承载；Grok Build 通过 `TurnHook` 暴露 turn 边界，让上层注入 system-reminder / todo 状态 | `xai-tool-protocol/src/turn_hook.rs:164-172`（`HookInjection`）<br>`xai-computer-hub-sdk/src/harness.rs:1368-1400`（`send_before_turn_hook` / `send_after_turn_hook`） |
| **Verification Loop 验证闭环** | ⑦ 缺自动化评审 | Goal completion 由 harness-owned adversarial verifier 验收；NotAchieved gaps 进入下一轮，Achieved 才提交 Goal | <code>crates/codegen/xai-grok-shell/src/session/goal_classifier.rs:1924</code>、<code>acp_session_impl/goal.rs:1937</code> |
| **Subagents 子代理分治** | ② context 溢出的另一解法 | Hub 支持 session relationship；高层 <code>task</code> 工具支持 blocking/background subagent，并限制深度 | <code>crates/codegen/xai-grok-tools/src/implementations/grok_build/task/mod.rs:29</code>、<code>task/mod.rs:157</code>、<code>task/mod.rs:324</code> |
| **Generator-Evaluator** | ⑦ 缺自动化评审 | Grok Build 明确实现 hidden adversarial skeptic panel，并以 majority-refute 聚合；PuddingClaw 当前只借鉴分权思想，首版复用单 Rubric grader | <code>crates/codegen/xai-grok-shell/src/session/goal_classifier.rs:1</code>、<code>goal_classifier.rs:981</code>、<code>goal_classifier.rs:1924</code> |

几点观察：

1. **Grok Build 同时包含低层 Harness Protocol 与高层 Agent Control**：Hub/Tool Protocol 负责连接、路由和工具终态；<code>xai-grok-shell</code> 负责模型—工具循环、Goal、TodoGate、compaction 和 verifier。
2. **TurnHook 是低层控制注入点，SessionActor/turn.rs 是高层控制核心**：二者不能互相替代。
3. **Context Management 被独立成一个 crate**，说明 Anthropic/xAI 把它当成与网络协议同等重要的工程模块，而不是简单 prompt 工程。
4. **Subagent 在低层协议和高层编排中分工存在**：协议提供 session relationship，<code>task</code> 工具提供实际深度、后台化和模型选择约束。

### B.2 三支柱归属矩阵

| Grok Build 组件 | Context Engineering | Architectural Constraints | Garbage Collection | 说明 |
|---|---|---|---|---|
| `ToolHarness::call` + `LocalRegistry` | 辅 | **主** | - | 工具可见性与本地/远程分发是硬约束 |
| `HubConnection` / `Demux` | - | **主** | - | 连接池、session 路由、并发控制 |
| `ToolServerHandler` trait | - | **主** | - | 工具必须返回 `Progress* Terminal` 结构 |
| `TurnHook` / `HookInjection` | **主** | 辅 | - | 在 turn 边界注入上下文/提醒 |
| `xai-grok-compaction` | **主** | - | **辅** | 上下文压缩与清理 |
| `ToolCapabilities::tool_scope` | 辅 | **主** | - | 多 agent 写协调约束 |
| `CancelOnDrop` / `cancel_call` | 辅 | **主** | **辅** | 取消语义既是约束也是资源回收 |
| `session.bind/unbind` + `serve` | 辅 | **主** | **辅** | session 生命周期约束与资源释放 |
| reconnect replay + `drain_waiters_with` | - | 辅 | **主** | 连接断开后快速释放等待者与进度通道 |
| `ModelCallLimitMiddleware`（PuddingClaw） | - | 辅 | **主** | 模型调用次数 GC |

### B.3 对 PuddingClaw 的映射启示

把 Grok Build 的拆解套回 PuddingClaw，可以验证正文中的几个判断：

- **PuddingClaw 的 Context Engineering 已经很强**：`ToolContextCompactionMiddleware` + `SemanticAssetsMiddleware` + `MemoryMiddleware` 等价于 Grok Build 的 compaction crate + turn hook 注入。
- **PuddingClaw 的 Architectural Constraints 需要补统一 Tool Policy**：Grok Build 通过 `ToolCapabilities` / `ToolScope` / `ToolServerHandler` trait 把约束做在协议层；PuddingClaw 目前还分散在 `permission_middleware.py`、`workspace_path_router.py`、`toolset.py`。
- **PuddingClaw 的 Garbage Collection 缺执行预算**：Grok Build 没有显式 token budget 协议，但 `CancelOnDrop`、`drain_waiters_with`、`untrack_session` 都在做资源回收；PuddingClaw 已有 `ModelCallLimitMiddleware`，但还没覆盖 tool-call / token / wall-time 的统一预算。
- **Grok Build 在 <code>xai-grok-shell</code> 高层明确实现 Goal/Verification**：PuddingClaw 借鉴其“完成权归 Harness”思想，但用 DeepAgents Rubric + 智能问数 checks 做首版，不复制多 Skeptic。

## 18. 最终判断

PuddingClaw 当前最强的是 **Context Engineering 与白盒可观测性**，不是短板。用户审核通过后，下一阶段围绕三个控制面推进：

1. **Action Control Loop**：所有 Tool 进入统一 risk → permission → sandbox → result 管线；
2. **Completion Control Loop**：自然停止进入 todo/goal → verifier → continue/complete 管线；
3. **Lifecycle Control Loop**：HarnessRunCoordinator 统一 Run/Goal/HITL/取消/异常/预算的合法状态迁移。

三个控制面补齐后，PuddingClaw 智能问数 Agent 才会从“可观察的 DeepAgents 产品封装”升级为“动作受控、完成可验、生命周期一致”的产品级 Agent。

## 19. 2026-07-19 长 Goal Session 复盘与修订方案（待审核）

> 本节来自真实 Session `session-8d0da6dfb9c5` 的 Session JSON、Trace sidecar、Docker 运行时和前端截图复盘。它修订的是已经实现后的产品行为，不推翻前三个控制面的总体架构。

### 19.1 复盘事实

该 Goal 用于刷新外部 HTML/JS 报告，最终形成以下运行事实：

- Goal 已运行 6 个 Run，累计 189 次主 Agent 模型调用；前两个 Run 均达到 50 次单 Run 上限；随后三个 Run 分别以 `max_iterations_reached`、`needs_revision`、`max_iterations_reached` 结束，第六个 Run 被用户取消。
- 一个 assistant message 内最多出现 26 个执行 segment；模型的过程性 content 与候选最终 content 被同时渲染，导致用户先看到“闭环完成”，随后又看到继续验收和修正。
- `edit_file` 共调用 80 次，其中 14 次因 `old_string` 精确匹配失败，失败率 17.5%。主要诱因是旧快照、Unicode/中文引号和多次修改后的文本漂移。
- workspace 为 `/Users/pet/puddingclaw`，交付目标位于 workspace 外部。Host 文件工具可以读取和编辑目标，但 Docker Terminal 无法直接访问；Agent 因此尝试将文件复制到 `/workspace` 做 Python/Node 验证。
- Run 记录已经声明 `scratch_host_path`，当前源码也声明 `/harness-scratch` mount；但真实长生命周期容器没有 `/scratch` 或 `/harness-scratch`，属于运行时容器 generation 与当前 Backend contract 漂移。
- Todo 在多个 Run 中被重命名、拆分和整表覆盖，最终出现 3 个 `removed_unresolved` tombstone；后一 Run 又重建早期计划，形成已完成进度回退。
- Global Summarization 实际已经触发：压缩前 UI 曾显示约 201k/200k，压缩后持久化的有效 Agent context 为约 46k tokens；但生成摘要错误声称“全部完成、无下一步”，与权威 Goal/Todo 不一致。
- Tool Context Compaction 实际完成：本次选中 4 个结果，从约 23,066 tokens 压到 2,808 tokens，失败为 0；但它目前是后台异步任务，Goal 下一 Run 不一定等待压缩完成。
- Trace sidecar 约 196 MB，Session JSON 约 7.7 MB。Trace 不参与 Agent 恢复或下一轮模型输入；该体积本身不构成 Harness 正确性缺陷。

### 19.2 修订决策一：Goal 仍然必须自主跨 Run，不增加普通确认点

**审核结论：同 Run 修正次数耗尽后，active Goal 应自动进入下一 Run；只要 Goal Run 预算尚未耗尽，就不要求用户确认。**

这一区分必须固定为两层：

1. **Run 内修正循环**：`deterministic checks → rubric → correction` 在同一 Run 内有限回跳，避免一次小缺口立即切换 Run。
2. **Goal 外层推进**：Run 内修正耗尽、单 Run model-call budget 耗尽或 context boundary 需要切段时，只要 Goal 仍 active 且 `round < max_rounds`，Harness 自动创建下一 Run，并注入权威 gaps、Todo、Artifact/Evidence Manifest 后继续。

因此推荐状态机为：

```text
Run natural stop
  → deterministic checks
  → rubric
  → satisfied ───────────────→ Goal aggregate verification
  → needs_revision
       ├─ Run correction budget remains → same Run correction
       └─ Run correction budget exhausted
            ├─ active Goal and Goal budget remains → next Run automatically
            └─ no Goal / Goal budget exhausted → terminal needs_revision
```

只有以下情况允许打断 Goal 并要求用户处理：

- Goal 的 `max_rounds`、总 token/model-call/wall-time 组合预算耗尽；
- 需要新增权限、外部系统协调或扩大任务范围；
- HITL 明确要求用户选择；
- 不可恢复的基础设施错误重复发生；
- 用户主动暂停、取消或修改 Goal。

普通 Grader 未通过不是用户确认点。否则 Goal Mode 会退化成需要人工不断点击“继续”的普通多轮对话。

来源标记：**[Grok Build Goal round / verifier 思想借鉴] + [PuddingClaw 自动 Run continuation 适配]**。

### 19.3 修订决策二：区分 Run 验收与 Goal 聚合验收

当前实现把 LLM grader 限定在当前 Run，同时部分 deterministic check 又读取 Goal 继承证据，导致验收范围不一致。例如前序 Run 已完成 16 组 SQL，后续 Run 只修 HTML，却被 grader 要求重新执行核心 SQL。

必须拆成两个明确层级：

#### Run Verification

只判断本 Run 的局部执行是否合法、是否产生可信增量：

- 本 Run 工具调用是否成功或被明确处理；
- 当前 Run 新增/修改的 Todo 是否收口；
- 本 Run 声称的修改是否存在对应 Artifact Receipt；
- 当前 Run 使用的验证命令是否真实执行；
- 是否出现 infrastructure error、permission interruption 或预算边界。

#### Goal Aggregate Verification

判断当前 Goal revision 的最终完成度：

- 使用跨 Run 的 Effective Evidence Manifest；
- 使用 Goal revision 下的 canonical Todo ledger；
- 使用目标 Artifact 的当前版本与全部写入 receipt；
- 允许前一 Run 的 SQL/result_id/generation_id 在后一 Run 被继承；
- 只要证据仍有效，不要求每个 Run 重复查询、重复写入或重复安装依赖。

Goal 完成判定的输入不再是“当前 Run 对话文本”，而是：

```text
Goal objective revision
+ Effective Verification Contract
+ Canonical Todo Ledger
+ Effective Artifact Manifest
+ Effective Evidence Manifest
+ Latest candidate answer
+ unresolved gaps / control notices
```

来源标记：**[本方案新增]**。这是为解决 PuddingClaw 已有 Run 级 Rubric 与 Goal 级连续性冲突而引入的双层验收。

### 19.4 修订决策三：候选答案不能冒充已验收答案

模型 content 需要按生命周期分流：

| 内容类型 | 前端位置 | 是否代表完成 |
|---|---|---|
| 带 Tool Call 的过程性 content | Run 进度/时间线 | 否 |
| 自然停止后的回答 | 候选结果，标记“验收中” | 否 |
| deterministic/rubric 修正提示 | 验收面板与 Run 时间线 | 否 |
| Goal/Run 验收通过后的候选结果 | 正式 assistant answer | 是 |

前端在 `executing → deterministic_checking → grading → revising` 期间必须保持统一的“执行/验收中”状态。不能因为模型输出了“完成”“全部通过”就提前显示任务完成。

来源标记：**[本方案新增]**，借鉴 Harness-owned completion 的原则，但交互和事件契约属于 PuddingClaw 自己的设计。

### 19.5 修订决策四：Global Summary 必须携带权威 Harness Envelope

Global Summarization 保持跨 Run 全局压缩能力，但最终上下文必须由两部分组成：

```text
LLM semantic conversation summary
+ deterministic Harness State Envelope
```

Harness State Envelope 由代码生成，不能交给摘要模型自由归纳，至少包含：

- `session_id`、`goal_id`、`goal_revision`；
- Goal objective、status、current/max round、组合预算余量；
- canonical Todo 的稳定 ID、顺序、状态和未完成项；
- Effective Verification Contract 的 criterion ID/version；
- 当前 gaps 与 control notices；
- Artifact Manifest：权威目标、当前 receipt/hash、临时副本身份；
- Evidence Manifest：SQL generation/result/trace refs、数据源、验证命令与产物证据；
- active permission/HITL 的最小恢复信息；
- 下一 Run 的 continuation reason。

压缩后必须对账：

```text
summary Harness Envelope
  == Session JSON authoritative Harness projection
```

任一 Goal/Todo/Artifact/Evidence 关键字段缺失或冲突时，摘要不得成为下一 Run 输入，必须回退到确定性 Harness Envelope + 最近消息。

#### 前端交互

上下文压缩不能再是一闪而过的 toast。前端需要持续展示全局阶段：

```text
正在压缩全局上下文
  1. 整理历史消息
  2. 生成语义摘要
  3. 注入 Goal / Todo / 证据状态
  4. 重建上下文并继续
```

完成后显示一次明确结果，例如“201k → 46k，Goal/Todo/证据已保留”，随后自动继续，不要求用户操作。

触发阈值是否从 200k 提前属于性能参数，不是本节正确性前提；当前实现允许单 Run 膨胀，只要在真正越过模型安全窗口前完成全局压缩即可。

来源标记：**[Grok Build compaction/continuation 思想借鉴] + [PuddingClaw Harness Envelope 新增]**。

### 19.6 修订决策五：Tool Context Compaction 保持 Run 后执行，但必须 Harness-aware

Tool Context Compaction 不改为频繁的单 Run 内即时压缩。单 Run 内保留完整工具上下文有利于模型连续执行和调试。

固定边界调整为：

```text
Run terminal
  → persist raw Run result
  → Tool Context Compaction
  → verify Harness evidence preservation
  → persist compact Agent context
  → start next Goal Run
```

即：**每个 Run 结束后、下一 Run 开始前先完成工具压缩**，不能只 enqueue 后立刻启动下一 Run。

工具压缩必须保留以下 Harness 信息：

- `tool_call_id`、tool name、成功/失败/中断状态；
- permission decision、grant ID 与风险能力摘要；
- SQL `generation_id`、`result_id`、数据源与 trace ref；
- Artifact Receipt、目标路径、scope/role、hash；
- deterministic verifier 依赖的结构化字段；
- 原始结果的 `raw_output_ref` 与 digest。

可以压缩的是面向模型的冗长正文、重复 schema、表格行和日志；不能压缩掉验收所依赖的结构化事实。压缩完成后运行一次 Evidence Manifest 对账，缺失证据则保留原结果并记录 compaction failure。

来源标记：**[PuddingClaw 现有 Tool Context 延续] + [本方案 Harness-aware 对账新增]**。

### 19.7 修订决策六：Trace 大文件暂不视为正确性问题

Trace 继续保持以下边界：

- Trace 只记录事实，不参与 Session 恢复；
- Trace 不作为 Agent 下一 Run 的输入；
- Goal/Run 权威状态仍在 Session JSON；
- LangGraph checkpoint 仍只负责同 Run HITL。

因此本次约 196 MB Trace 不进入 P0/P1 正确性修复。Session 约 7.7 MB 当前也可接受。

后续只做低优先级产品化治理：

- Trace UI 分页/按 Run 懒加载；
- sidecar 保持独立，禁止随 Session 列表接口整体返回；
- 可选按 span 类型去重 hook snapshot；
- 磁盘配额、归档和清理策略；
- 不以压缩 Trace 为代价丢失审计证据。

来源标记：**[PuddingClaw 既有权威边界延续]**。

### 19.8 外部文件与 `/scratch` 修复

现有冻结边界保持不变：用户直接给出的外部路径仍是权威目标，不默认复制到 attachments，也不能用 workspace 副本冒充最终产物。

需要补齐的实现：

1. Docker project container 固定挂载项目级 `/harness-scratch`；
2. Backend 将当前 Run 的 `/scratch` 重写到独立子目录；
3. `_validate_runtime` 检查 mount、读写权限和 generation contract，不满足就重建容器；
4. 外部文件需要 Python/Node 验证时，由 Host 侧建立 `ExternalArtifactLease`，暂存到 `/scratch/external/<artifact_id>/`；
5. 验证完成后通过带 `expected_source_sha256` 的受控 commit 写回原路径；
6. Run 结束清理 scratch，Artifact Manifest 保留原目标 receipt，不登记临时副本为交付物。

来源标记：**[本方案新增]**，延续此前冻结的 FileRef、外部路径授权与 scratch 设计。

### 19.9 `edit_file` 版本化 Patch

模型工具面不再继续放行简单 `old_string/new_string` 盲改入口；保留其底层兼容实现仅供旧记录恢复，新的 Agent 写路径使用稳健编辑协议：

- `inspect_file_version` 返回完整 UTF-8 内容与版本/hash；
- Patch 携带 `expected_sha256`；
- 第一阶段支持原子 `replacements[]` hunk；后续可扩展 line range、before/after anchor 或 unified diff，但不作为当前已实现能力宣称；
- source hash mismatch 返回当前 hash；hunk mismatch 要求重新 inspect/rebase，不允许模型连续猜测；
- 同一路径第一次 mismatch 后强制重新读取，禁止连续猜测字符串；
- 多处修改支持原子 batch，任一冲突不写入半成品。

来源标记：**[Grok Build/成熟 Coding Agent patch 交互借鉴] + [PuddingClaw 外部文件权限适配]**。

### 19.10 Todo 稳定身份与增量协议

Todo 不再以“整张自然语言列表”作为身份来源，改为 Harness-owned stable ID：

```text
create_todo
update_todo
complete_todo
cancel_todo
reopen_todo
reorder_todos
```

规则：

- 改标题不改变 ID；
- 拆分 Todo 必须显式声明 parent/children；
- completed Todo 不能因后一 Run 重建列表而恢复 pending；
- 省略 Todo 不等于删除，仍保留原状态；
- 只有显式 complete/cancel 才满足 TodoGate；
- Global Summary 只引用 canonical Todo，不反向覆盖 Todo ledger。

来源标记：**[Grok Build TodoGate 思想借鉴] + [PuddingClaw stable ledger 新增]**。

### 19.11 修订后的开发优先级

#### P0：完成语义与跨 Run 连续性

1. **候选答案/正式答案分离**：前端增加 executing/checking/grading/revising/accepted 生命周期，禁止提前显示完成。
2. **Run Verification 与 Goal Aggregate Verification 分层**：Goal 验收读取跨 Run Effective Evidence/Artifact Manifest。
3. **Goal 自动跨 Run 规则收口**：同 Run 修正耗尽后，只要 Goal 预算仍有余量就自动进入下一 Run，不询问用户。
4. **Harness-aware Global Summary**：确定性 Harness Envelope、摘要对账、前端持续压缩状态。
5. **Todo stable ID + patch protocol**：消除重命名、拆分和跨 Run 重建造成的进度回退。
6. **Docker scratch contract 修复**：容器 mount 自检、generation recreate、外部产物 staging/commit。

#### P1：工具可靠性与 Run 边界压缩

1. **Tool Context 设为跨 Run barrier**：Run 后压缩完成并完成 Evidence Manifest 对账，再启动下一 Goal Run。
2. **`edit_file` 版本化 Patch**：hash、anchor、候选匹配、原子 batch。
3. **验收明细产品化**：区分本 Run 局部缺口、Goal 总体缺口、控制面错误和继承证据。
4. **Todo/Goal/Grader 进度顺序修复**：所有卡片使用权威 position、run_id、attempt，不按自然语言或 SSE 到达顺序猜测。

#### P2：可观测性与存储体验

1. Trace 按 Run/span 懒加载与分页；
2. 可选去重重复 hook snapshot，但不得影响审计；
3. Session segments/tool output 是否进一步引用化，按真实加载性能决定；
4. Trace 归档、磁盘配额和清理设置。

Trace 体积和当前 7.7 MB Session 不作为 P0/P1 阻塞项。

### 19.12 修订后的验收用例

#### Goal Continuation

- 同 Run rubric correction 达到上限、Goal 尚有 Run 预算时，自动启动下一 Run，不产生 HITL；
- Goal `max_rounds` 耗尽时停止并展示最终 gaps；
- 普通非 Goal Run 验收耗尽时不自动创建 Goal；
- 前一 Run 完成 SQL、后一 Run 只编辑 HTML 时，Goal Aggregate Verification 能继承 SQL evidence，不要求重复查询；
- Goal 自动下一 Run 前必须完成 Tool Context barrier。

#### Summary / Tool Context

- Global Summary 后 Goal ID/revision/status/round 不变；
- Summary 前后 canonical Todo ID、状态、顺序完全一致；
- Summary 前后 Artifact/Evidence Manifest 对账一致；
- 摘要模型错误声称“已完成”时，Harness Envelope 仍保持 active + unresolved gaps；
- 前端完整显示 global compression 的开始、阶段、前后 token 和完成状态；
- Tool Context 压缩后 SQL generation/result ID、Artifact Receipt、permission decision 和 raw ref 仍存在；
- Tool Context 失败时下一 Goal Run 使用未压缩原结果，不丢证据。

#### External Artifact / Scratch

- Docker container 启动后 `/scratch` 虚拟路径可写，`/harness-scratch` 不直接暴露给模型权限；
- spec 已声明 scratch、真实 container 缺 mount 时自动 recreate；
- 外部 HTML 可在 scratch 中通过 Python/Node 验证，不写入 `/workspace`；
- scratch 副本不能满足 artifact delivery，只有原目标 commit receipt 可以；
- source hash 冲突时禁止覆盖外部文件并返回明确冲突。

#### Todo / Edit

- Todo 改名不创建新 ID；
- Todo 拆分后 parent/children 可追踪；
- 后一 Run 省略已完成 Todo 不会让其回退；
- `edit_file` hash 冲突返回候选与行号，不产生部分写入；
- 中文引号、Unicode 转义和并发修改场景不会进入连续 blind retry。

### 19.13 第一阶段实现检查点（2026-07-19）

本检查点记录代码已落地与仍未闭环的边界，不能把“已写代码”等同于整轮 Harness 已验收。

已落地：

- Run 启动即把输出标为 candidate/pending；通过时仅最后一个 content segment 成为 accepted，前序模型/工具片段统一折叠为 progress；刷新恢复时读取持久化 running/pending 状态；budget、grader、infrastructure 等无合法终态的候选标为 unverified，不再投影成普通答案。
- Goal 模式把当前修订版的跨 Run evidence refs、Todo、known gaps 和 prior Run 状态作为结构化 aggregate context 提供给 grader；analytics/web evidence 可在同一 Goal revision 下继承，报告显式标记 `verification_scope=goal_aggregate` 与 supporting Run IDs。
- 同 Run 正常 rubric correction 仍由 RubricMiddleware 完成；耗尽后由 Goal 自动进入下一 Run。验收器/基础设施异常使用独立 failure fingerprint 和有限 retry budget，连续同指纹达到阈值后转 blocked，避免退款导致无限循环。
- Global Summary 默认阈值调整到约 160k；摘要末尾由 Harness 确定性附加 `puddingclaw.harness-envelope/v1`，包含 Goal、Run、canonical Todo、artifact target、evidence 与 gaps；前端在压缩期间显示持续的“全局上下文压缩”状态。
- 原 `write_todos` 整表替换从模型工具面移除，改为 `update_todos` 增量协议；create ID 由 tool-call identity 确定性生成，rename/reorder 不换 ID，cancel 保留 tombstone。
- 新增 `inspect_file_version → patch_file(expected_sha256, replacements[])` 原子 compare-and-swap 流程；旧 `edit_file` 在 ToolExecutionPipeline 中拒绝并引导重读/rebase。
- Tool Context 从“只 enqueue”改为 Run boundary barrier；完成或形成明确错误终态后才允许外层 Goal loop 继续，摘要保留 tool_call_id、source_hash、raw_output_ref 与 Harness evidence 标记。
- Docker 启动复用前增加 inspect Mount、RW、UID/GID 与实际写探针；runtime contract 不匹配时销毁并重建。Host/Docker 执行入口和 shell policy 同时拒绝显式 `/scratch/..` parent traversal。
- 外部目标增加 `stage_external_artifact → ExternalArtifactLease → commit_external_artifact`：staging 只进入当前 `/scratch/external/<lease_id>/`，lease 在 Session JSON 绑定原始绝对路径和 source SHA；commit 只允许写回该精确路径，源文件在 staging 后发生变化时 fail-closed，scratch 副本不登记为交付产物。
- Goal 已持久化独立 `GoalVerificationDecision`，记录 objective revision、supporting Run IDs、聚合证据数、gaps、accepted Run 与 report ID；前端验收卡显式区分“本 Run 验收”和“Goal 聚合验收”。
- 第二轮对抗审查后，`GoalVerificationDecision` 不再只是最后一个 Run report 的别名：新增 `accepted` 与 `criterion_provenance[]`，逐项记录 verifier、实际 supporting Run、evidence ref、gap 和 report ID；下一 Run 的 grader 同时读取前序 Run 的候选正文摘要和验收来源，因此“Run 1 完成纯文本子任务 A、Run 2 完成 B”不再因为 A 没有工具证据而天然丢失。
- Goal objective revision 在运行中被修改或用户请求暂停/取消时，即使旧 Run 的 grader 返回 satisfied，也只保存为 superseded candidate，`accepted_for_goal_revision=false`；前端显示“旧版目标验收通过（未接纳）”，不能冒充当前 Goal 的正式结果。
- Harness Envelope 不再用 `todos[-80:]` 截断权威进度：全部 pending/in_progress Todo 必须完整保留，仅 completed/cancelled 历史保留最近 40 条并附总数与 digest；evidence refs 保留全部结构化索引。模型摘要中伪造的 `<HARNESS_ENVELOPE>` 会先被剥离，最后只追加控制面生成的权威块。
- Tool Context `completed_with_errors` 明确显示“部分压缩、失败项保留原始结果”，不再误称全部证据已对账；后台任务异常取消会持久化 failed，下一 Run 使用原始上下文。
- 最终对抗复审补上跨 revision 污染反例：`prior_runs` 与 `prior_run_candidates` 必须逐条匹配当前 `goal_id + objective_revision`，Goal 修改后保留的旧 run_ids 只能用于审计，不能进入新 revision 的 aggregate grader。
- 普通非 Goal Run 触发 Summary 时不会导入历史 terminal Goal；只有 latest Run 自身携带 goal_id 时，才允许加载对应 terminal Goal 形成收尾 Envelope。
- `GoalVerificationDecision` 对 `NOT_REQUIRED` 的 accepted 语义与 Goal/Run 终态保持一致；criterion provenance 显式区分 `evaluated_in_run_id` 和 `evidence_origin_run_ids`，当前 evidence 已覆盖时不再合并旧 artifact receipt。
- Tool Context 后台任务被 event-loop/client 生命周期取消时，会立即把 job 和仍 pending 的 candidate 标为 failed、保留 raw output 并释放重试租约，不再等待 lease timeout。
- Todo 每次增量操作后写入显式 `position`；前端按 position 投影，不再用 SSE 到达顺序猜测列表位置。

仍未闭环，继续按优先级处理：

1. **Docker exact-Run scratch 强隔离**：当前 project container 挂载项目 scratch root，可写探针与路径策略已补，但一个长驻 project container 无法天然阻止解释器/符号链接访问其他 Run 子目录。要满足强隔离，需要 per-Run helper/ephemeral exec mount 或等价 FS jail；不能用字符串过滤冒充真隔离。本轮不把这个物理限制包装成已经实现的“Run 级真隔离”。
2. **ExternalArtifactLease 清理策略**：staging/冲突保护/精确写回已经闭环；仍需按审计保留期清理已 committed/abandoned 的 scratch 文件。清理不得删除原目标或 Artifact Receipt，因此作为生命周期卫生项处理，不阻塞正确写回。
3. **Evidence 有效期**：跨 Run analytics/web evidence 已绑定 Goal revision；仍需针对可失效 result/source 增加 TTL/version 校验，code validation 默认不跨修改继承。
4. **进度位置与验收明细**：Todo position、Run ID、verification scope 已由控制面提供；candidate/accepted ID 和更细的 grader attempt 仍需继续从显示层彻底移除推断逻辑。
5. Trace/Session 体积治理仍为 P2；Trace 不进入 Agent context，也不作为恢复权威，因此不阻塞上述正确性闭环。

本阶段机制来源区分：

- 借鉴 Grok Build/成熟 coding agent：candidate→verify→accept、TodoGate、版本化 patch、compaction 后 continuation 的交互原则。
- PuddingClaw 自有设计：Run/Goal 双预算、Goal revision-bound Evidence Manifest、Session JSON 权威边界、LangGraph 同 Run checkpoint、Harness Envelope、Tool Context Run barrier、Docker/Host 同一权限管线。

### 19.14 本轮实现验收记录（2026-07-19）

- Backend 产品测试边界：`backend/.venv/bin/pytest -q tests`，**779 passed**；Backend 根级无范围 `pytest` 会额外收集第三方 Skill 自带测试并产生模块名冲突，不作为 Backend 产品回归入口。
- Frontend：`tsc --noEmit` 通过；Next.js production build 通过，13 个路由完成静态/动态构建。
- Docker 镜像 smoke：`puddingclaw/sandbox:python3.12-node22-chromium-v4` 在 `--network none`、宿主 UID/GID、真实 bind mount 下确认 Python 3.12、Node.js 22、Chromium、curl 与 `/harness-scratch/<session>/<query>` 写入成功。
- 当前本机两个旧 project container 均缺 `/harness-scratch` mount；没有在审核过程中强制销毁。新 Backend 下次使用相应 project sandbox 时会因 spec hash/runtime validation 不匹配而自动重建。
- `git diff --check` 通过。
- 最终只读对抗复核确认四个高风险反例均已封住：旧 revision/cross-goal candidate 注入、普通 Run 导入 terminal Goal、未闭合伪造 Harness Envelope、Tool Context cancel 后租约悬挂；对应定向用例 4 passed。

上述验收证明的是代码路径、状态契约和运行时基础能力已形成闭环；不把 Docker project container 内的 exact-Run 物理隔离、Evidence TTL、scratch retention 或 Trace 存储治理提前宣称为已完成。

### 19.15 对抗式复审后的最终优先级（本轮审核版）

复审顺序按“先保证控制语义，再保证状态穿越，再改善工具可靠性和体验”执行：

#### P0-A：Goal 自主性与验收权威

1. 同 Run rubric correction 或 `run_model_call_limit` 耗尽，只要 Goal 的 `max_rounds`、Goal model-call/token/time budget 仍有余量，必须自动进入下一 Run；不增加确认点。
2. Goal 结束只能由 revision-bound `GoalVerificationDecision.accepted=true` 触发；Run candidate、grader satisfied、Todo 文案或模型口头声明都不能独立结束 Goal。
3. GoalDecision 必须按 criterion 保存来源，`supporting_run_ids` 只能来自实际复核 Run、继承的 prior decision 或 evidence origin，禁止直接填满 `goal.run_ids`。
4. 用户修改 Goal objective 时立刻提升 revision；旧 revision 的并发 Run 可留作审计，但永不接纳为新 revision 的完成结果。

#### P0-B：跨上下文边界保持 Harness 不变量

1. Global Summary 在约 160k 触发；前端持续显示“正在进行全局上下文压缩”，完成后自动继续。
2. 摘要自然语言仅帮助模型理解；Goal、canonical Todo、Artifact/Evidence、permissions、gaps、budget 和 latest decision 必须来自确定性 Harness Envelope。
3. 全部未收口 Todo 永不因摘要长度上限丢失；终态 Todo 允许以 recent items + count + digest 表示。
4. 每个 Run 终态后、下一 Goal Run 前执行 Tool Context barrier；部分失败时保留原始工具结果，不能因压缩失败阻止 Goal 在证据仍可用时继续。

#### P1：文件与容器执行可靠性

1. 外部绝对路径保持交付权威，通过 revision/Goal/Run-bound `ExternalArtifactLease` 暂存、验证和 CAS commit；不复制到 workspace 冒充最终交付。
2. 新写操作统一使用 inspect + expected hash + atomic patch；旧 `edit_file` 只做历史兼容，模型面禁止 blind retry。
3. Docker project container 继续复用，基础镜像只保证 Python/Node；依赖按需、联网经权限管线。`/harness-scratch` mount、RW、UID/GID、Python/Node 和真实写探针必须在复用前通过。
4. **待产品决策**：project-level 长驻容器与“解释器层物理 exact-Run scratch 隔离”存在结构冲突。若必须达到物理隔离，需要 per-Run helper/ephemeral exec mount、独立 UID/namespace 或 FS jail；当前路径重写和 traversal policy 只是逻辑隔离，不能宣称为强隔离。

#### P2：存储与可观测性

1. 7.7 MB Session 和大 Trace 本身不构成控制正确性问题；Trace 不参与 Agent 输入、恢复或 checkpoint authority。
2. 后续按真实加载性能实现 Trace 分页、按 Run/span 懒加载、配额和归档；不能为减小文件牺牲审计事实。
3. Session/Trace 大小不能反向驱动 P0 控制面设计，只有 Session 列表加载、单 Run 恢复或磁盘增长出现可测量问题时才升级优先级。

来源区分：Goal 自主跨 Run、revision-bound Decision、Harness Envelope、ExternalArtifactLease、project container 权衡属于 **PuddingClaw 本方案新增/演进**；candidate→verify→accept、TodoGate、版本化 patch、compaction 后 continuation 的交互原则属于 **Grok Build 与成熟 coding agent 的机制借鉴**；Session JSON/Trace/checkpoint 权威边界属于 **PuddingClaw 既有产品决策的延续**。

### 19.16 上传附件的只读/修改分流（审核补充）

“通用附件自动暂存到 Docker `/scratch`”容易被理解为所有上传文件都会启动容器并产生副本，因此不采用这个表述，也不采用这种行为。正确边界是由任务能力决定是否升级：

#### 只读路径

- 用户上传或粘贴的文件先保存为当前 Session 的托管附件 `att_xxx`；
- 原始附件在 `data/attachments/<session_id>/...` 中保持不可变；
- 仅查看、摘录、问答或读取内容时，Agent 直接调用现有 `read_resource(att_xxx)`；
- 只读路径不创建 scratch 副本、不启动 Docker 编辑链路，也不产生新的交付附件。

#### 修改路径

当用户要求修改、转换或生成该附件的新版本时，Agent 必须显式升级到附件编辑能力：

```text
att_xxx（不可变源附件）
  -> prepare_attachment_edit
  -> AttachmentEditLease（绑定 session/run/query/goal revision/source sha256）
  -> /scratch/attachments/<lease_id>/<filename>
  -> Docker Backend 内修改与验证
  -> publish_attachment
  -> 新的 derived attachment（可下载交付物）
```

核心规则：

1. `prepare_attachment_edit` 只能读取当前 Session 已授权的附件，并把字节级副本放入当前 Run 的 scratch；不能接受任意宿主路径。
2. staging 本身不需要再次 HITL，因为用户上传附件已经授权当前 Session 使用；Docker 内命令、联网和安装依赖仍分别经过 `ToolExecutionPipeline`。
3. `publish_attachment` 只能发布该 lease 目录中的文件，不能借机读取其他 Run、其他 Session 或任意宿主路径；这条是发布工具的确定性边界，不等同于宣称 project container 已具备物理 exact-Run 文件系统隔离。
4. 发布结果保存为新的 `source=generated` 附件，并记录 `derived_from`、Run/Query、内容 hash 和 Artifact Receipt；原始上传附件永不原地覆盖。
5. 前端在助手消息中展示新附件的名称、大小、来源和下载入口；scratch 路径不是交付路径，也不暴露给用户。
6. Docker 不可用时是否允许受控 Host fallback 继续遵循 Harness 设置；无论使用哪个 Backend，权限、lease、发布和验收语义必须一致，UI 不得把 Host fallback 宣称为 Docker 沙箱。

因此，“用户上传一个文件，然后要求修改”是 `AttachmentEditLease` 的标准场景；“用户上传一个文件，然后询问内容”仍然只是 `read_resource` 场景。识别结果最终由工具契约约束，而不是仅靠提示词：未取得 edit lease 就没有可写路径，未执行 publish 就没有可接纳的附件交付物。

该机制属于 **[PuddingClaw 本方案新增]**：复用现有托管附件、Run scratch、Backend 和 Artifact Verification，不照搬 Grok Build 的具体实现。

#### 19.16.1 已实现的权威链路

- `AttachmentStore` 使用临时目录写文件与 manifest，再原子 rename；确定性派生 ID 遇到无有效 manifest 的半提交目录时可安全回收重放。
- 同一 lease 通过 Session 内原子 `staged → publishing → published` 状态转换选择唯一发布分支；并发发布不同 output path/name 时只有一个分支可以成功。
- `published` lease 与 `attachment_deliveries[query_id]` 在同一次 Session JSON 提交中完成。该 outbox 是刷新/进程中断后的交付权威，SSE 和助手消息只是其投影。
- Chat/SSE 不信任 ToolMessage 自带的 `download_url`。服务端重新核对 AttachmentStore、Session/Run/Query/Goal revision、Artifact Receipt、真实路径与实际字节 hash 后，才重建 public item 和下载 URL。
- 上传与下载均要求 Session 真实存在；删除 Session 同时删除其附件树，旧 URL 随即失效。上传入口限制最多 8 个 multipart 文件，并在解析流阶段限制整个请求为 100 MB，避免先完整 spool 再进入业务限额。
- 一旦成功调用 `prepare_attachment_edit`，Artifact Verification 被激活但仍为 non-material；只有权威 `publish_attachment` receipt 才能满足交付，不允许 scratch 文件或模型口头声明冒充产物。

#### 19.16.2 对抗审查后的诚实限制

当前 project container 仍复用项目级 `/harness-scratch` mount。本轮已经确定性拒绝：

- 字面量 `/harness-scratch` 与相对 `../harness-scratch`；
- Docker 命令中的 parent traversal；
- 可把路径隐藏为 `harness-scrat[c]h`、`h*` 的 shell glob/path expansion。

这能封住已复现的 shell 绕过，但不能把同 UID 解释器看到整棵 mount 的事实变成物理隔离。例如经用户批准执行的任意 Python 仍可能动态拼接不可见路径。若产品要求“即使任意容器代码也绝不读取相邻 Run scratch”，必须改为 per-Run helper/ephemeral mount、独立 UID/namespace 或 FS jail。结合当前产品决策——单文件修改走附件 lease，项目联调由用户明确选择项目目录——本轮保留 project container 复用，不伪称已完成 exact-Run 强隔离。

#### 19.16.3 本轮附件链路验收

- 定向附件、Session、权限管线及 DeepAgents 流测试：127 passed；
- Backend 产品全量：779 passed，24 warnings；
- Frontend `tsc --noEmit` 通过；Next.js production build 通过，13 个路由；
- `git diff --check` 通过。

对抗回归覆盖：跨 Session/Run/Query/Goal revision lease、source hash 篡改、路径穿越、shell glob 隐藏、超大源/输出、并发双发布、伪造 ToolMessage 下载卡、半提交重放、publish 后 stream 消费前崩溃、Session 删除后下载失效、任意文件类型统一 Artifact Verification。

## 20. 权限控制横向审计：Codex / Grok Build / StaffDeck / Yuxi / PuddingClaw（2026-07-20，P0/P1 首版已实现）

本节记录 Codex、Grok Build、StaffDeck、Yuxi 的扫描结论，并据此修订 PuddingClaw 的智能审批方案。20.9 的 P0 与 P1 首版已于 2026-07-20 落地；P2 仍是产品演进候选。实现边界与未完成项以 20.11 为准。

需要先区分三个经常被混在一起的控制问题：

1. **Action Control**：某个具体动作是否允许执行，是否需要 HITL，例如 shell、联网、安装包、删除文件；
2. **Resource Control**：当前 Agent 是否有资格看到或调用某项业务资源，例如租户工具、Skill、数据集、HTTP/MCP 工具；
3. **Isolation Control**：动作即使被允许，实际进程还能接触哪些文件、网络、凭据和系统能力。

Codex 与 Grok Build 主要解决第一类，StaffDeck 主要解决第二类，Yuxi 主要解决第三类。PuddingClaw 的目标不是抄其中任意一套，而是把三类控制统一放进 Harness 控制面。

### 20.1 Codex：边界优先，Reviewer 只审查已经越界的动作

来源：Codex 本地手册，以及 OpenAI 官方的 [Agent approvals & security](https://learn.chatgpt.com/docs/agent-approvals-security.md) 和 [Auto-review approvals](https://learn.chatgpt.com/docs/sandboxing/auto-review.md)。

Codex 的智能模式不是“每条命令先判一次风险”，而是两层模型：

```text
Workspace sandbox / network boundary
  ├─ 边界内：直接执行
  └─ 需要越界：进入 approval reviewer
                    ├─ 低/中风险：替用户批准
                    ├─ 高风险：仍询问用户
                    └─ 关键风险：拒绝
```

关键结论：

- `workspace-write + on-request` 下，工作目录内读取、修改文件和运行普通命令默认不重复打断；工作区外写入、受限网络或其他越界动作才请求升级；
- “替我审批/Auto review”只改变**升级请求由谁审核**，不扩大文件系统、网络或沙箱本身的边界；
- 审查不是静态命令白名单，而是对动作、参数、目标和上下文做风险判断，并且审查失败时 fail closed；
- 带有破坏性语义的 Apps/MCP 动作仍保持明确审批，不因智能模式变成无条件放行。

对 PuddingClaw 的启示是：Docker 已经提供清晰边界后，容器内普通 Python/Node、构建、测试和项目写入不应继续逐条 HITL。Reviewer 应放在“确定性策略无法决定的灰区”，不能替代沙箱边界。

### 20.2 Grok Build：确定性快路径 + 危险规则 + LLM 灰区分类

扫描范围：

- `crates/codegen/xai-grok-workspace/src/permission/auto_mode.rs`
- `crates/codegen/xai-grok-workspace/src/permission/manager.rs`
- `crates/codegen/xai-grok-workspace/src/permission/rules.rs`
- `crates/codegen/xai-grok-sandbox/src/profiles.rs`

Grok Build 采用混合模型：

```text
显式 deny
  -> 确定性 allowlist / metadata fast-path
  -> 明确危险模式
  -> 上下文感知 LLM classifier
  -> classifier 不可用或仍不确定时询问用户
```

源码事实：

- `Read`、`Grep`、`WebSearch`、Todo/plan/task wait 等协调工具可进入自动快路径；
- 常用构建、测试、Python/Node、Cargo、Make 和一批 Git 操作被视为普通本地开发行为；
- npm/pnpm/yarn/uv/pip 的常用本地项目子命令有结构化识别；`npx`、`uvx` 这类下载后直接执行的 launcher 不进入安全快路径；
- 复杂 shell 不简单按可执行文件名决定，而是交给会话感知 classifier 判断；发布、远程机器、秘密信息、不可逆 Git、下载并执行等继续询问；
- `WebFetch` 没有像 PuddingClaw 的安全公共 `fetch_url` 一样自动放行；未知 MCP 也默认询问；
- 权限层对 Edit 很宽松，但真实文件写入仍受 OS sandbox 限制。不能把“permission manager 允许 Edit”误读为“宿主任意路径可写”。

Grok Build 值得借鉴的是“确定性快路径覆盖高频开发动作，灰区才调用 reviewer”；不应照搬的是过宽的 Edit 快路径、过大的包管理 allowlist，以及把网络默认开放的 workspace sandbox 直接移植到 PuddingClaw。

### 20.3 StaffDeck：业务资源治理强，但不是执行沙箱或智能审批

扫描范围：

- `backend/app/security/permissions.py`
- `backend/app/tools/tool_schema.py`
- `backend/app/tools/tool_executor.py`
- `backend/app/core/agent_loop.py`
- `backend/app/general_skills/runner.py`
- `backend/app/general_skills/runtime_env.py`

StaffDeck 的权限主轴是“谁能调用哪个业务工具”：

1. tenant admin/member 与 agent manager/viewer 控制管理、查看权限；
2. 工具必须启用、对当前员工可见或已绑定；
3. 当前 workflow step 的 `allowed_actions` 与 `allowed_skills` 再收窄本步可调用能力；
4. POST/PUT/PATCH/DELETE 等副作用 HTTP 调用使用事件历史做幂等重放，避免恢复或重试时重复执行；
5. `handoff_human` 是工作流动作，不是每次危险工具的通用审批中间件。

它没有形成 PuddingClaw 所需的 Action Control：

- HTTP/MCP 工具通过绑定校验后直接执行，没有统一的风险描述、域名/SSRF 策略和 HITL；
- general Skill runner 让模型生成 Python/Bash，在宿主 `mkdtemp` 中通过 `subprocess.Popen` 执行；主要依赖提示词约束“不要执行用户输入命令”；
- runner 继承运行环境，并可按配置自动建立 venv、联网安装依赖，没有 Docker/OS 沙箱和统一审批管线。

因此 StaffDeck **不能作为 PuddingClaw 智能审批的模板**。可借鉴部分只有：

- `tenant → agent → workflow step → skill/tool` 多轴资源绑定；
- 对业务副作用工具建立 idempotency key 与 replay receipt；
- 把人工接管作为显式 workflow capability。

禁止照搬其模型生成代码后直接宿主执行的 runner。

### 20.4 Yuxi：线程级真容器与文件命名空间明确，但没有动作风险审核

扫描范围：

- `docs/agents/sandbox-architecture.md`
- `backend/package/yuxi/agents/backends/sandbox/backend.py`
- `backend/package/yuxi/agents/backends/sandbox/provider.py`
- `docker/sandbox_provisioner/app.py`
- `backend/package/yuxi/agents/buildin/subagent/graph.py`

Yuxi 将应用层与独立 `sandbox-provisioner` 分开，支持 Docker 和 Kubernetes Backend。其稳定 sandbox identity 由 `uid + file_thread_id + skills_thread_id` 派生：

- 用户共享 workspace：`/home/gem/user-data/workspace`；
- 当前 file thread 的 uploads：`/home/gem/user-data/uploads`；
- 当前 file thread 的 outputs：`/home/gem/user-data/outputs`；
- 当前 skills thread 的 skills：`/home/gem/skills`，只读；
- 子代理继承父任务的 `file_thread_id`，因此共享附件和 outputs，但使用自己的 `skills_thread_id`，因此可以获得不同 Skill 视图；
- idle reaper 默认 120 秒清理空闲 sandbox。

文件 API 侧的边界较清晰：路径必须是绝对 Posix path，拒绝 `..`；读取仅允许 user-data 与 skills，写入只允许 workspace 与 outputs，uploads/skills 对 Agent 文件工具只读。

但 `ProvisionerSandboxBackend.execute()` 将 command 直接交给 sandbox shell，没有命令分类、危险模式判断或 HITL。这意味着“文件工具不让写 uploads”并不自动等价于“任意 shell 也无法写 uploads”，真实保证取决于容器 mount 和进程权限。

容器硬化也不能直接照搬：

- Docker 使用 `seccomp=unconfined`，`/home/gem` 为 `rw,exec,mode=777` tmpfs；
- 未看到 `cap_drop=ALL`、`no-new-privileges`、只读根文件系统、非 root 用户、CPU/内存/PID 限制；
- Kubernetes 明确 `run_as_user=0`、`fs_group=0`，并通过 `chmod 777` 准备目录；
- Docker 默认可加入配置网络，且把用户 Agent env 注入 sandbox。

Yuxi 值得借鉴的是：

- 应用服务与 provisioner 分离；
- `file_thread_id` 与 `skills_thread_id` 分离，子代理共享文件但不必共享全部 Skill；
- uploads/workspace/outputs/skills 的稳定虚拟命名空间；
- Docker/Kubernetes Backend 抽象和 idle lifecycle。

不应照搬的是：root、`seccomp=unconfined`、777、无资源上限、常态网络与凭据注入，以及可见 shell 无 Action Control。

### 20.5 五方权限模型对比

| 维度 | Codex | Grok Build | StaffDeck | Yuxi | PuddingClaw 当前 |
|---|---|---|---|---|---|
| 核心控制 | workspace 边界 + approval reviewer | 规则快路径 + LLM 灰区分类 | tenant/agent/step/skill 资源绑定 | thread/uid 容器与文件命名空间 | ToolExecutionPipeline + Docker/Host Backend |
| 边界内普通命令 | 默认放行 | 大量常用开发命令放行 | general Skill 可直接宿主执行 | sandbox 内任意 shell 直接执行 | 智能模式已允许一部分 Docker 普通计算，但仍偏保守 |
| 文件修改 | workspace 内默认放行 | Edit 权限层宽松，OS sandbox 收口 | 不提供统一本地文件 action policy | 文件 API 仅 workspace/outputs 可写 | 版本化 inspect/patch；外部文件走 lease/commit |
| 网络 | 默认受限，越界审批 | workspace profile 较宽；WebFetch 仍询问 | HTTP/MCP 绑定后直连 | 容器可加入网络并注入 env | safe public fetch/search 自动；安装与 raw network 分级询问 |
| 包安装 | 越界/联网时审批 | 识别常见包管理命令，remote launcher 更严格 | runner 可自动 pip install | shell 可直接执行，取决于容器网络 | Docker 内按需安装，package/network Session 授权 |
| Git 本地写操作 | workspace 内常规操作低摩擦 | add/commit/checkout/switch/stash 等大量快路径 | 不适用 | shell 无分类 | 目前大多归 managed write 并询问，明显更保守 |
| 业务副作用工具 | Apps/MCP 依注解审批 | 未知 MCP 询问 | 资源绑定 + 幂等重放，但无通用 HITL | 可见工具直接调用 | 尚需统一 side-effect descriptor、idempotency 与 approval |
| 子代理授权 | 继承任务沙箱与规则 | 受统一 permission manager 约束 | 继承员工/step/tool binding | 共享 file thread、分离 skills thread | 应继承父 Run 的 immutable permission context，当前重复询问体验需继续收口 |
| 真沙箱 | workspace OS sandbox | workspace/strict sandbox profile | general Skill runner 无真沙箱 | 真 Docker/K8s，但硬化偏弱 | Docker 非 root、cap drop、no-new-privileges、资源限制、默认断网；Host fallback 明示降级 |
| 主要优势 | 低摩擦边界模型 | 高频动作覆盖与灰区 reviewer | 业务资源治理、幂等副作用 | provisioner、线程命名空间、生命周期 | 三类控制可以统一进 Harness，已有强 Docker 基线与 artifact lease |
| 主要风险 | reviewer 不能替代 sandbox | allowlist 过宽会扩大攻击面 | 宿主执行生成代码 | arbitrary shell 绕过文件 API；容器 root/联网/env | 规则过细、过度询问；subagent grant 继承和业务工具副作用尚未完全产品化 |

### 20.6 修订后的 PuddingClaw 智能审批原则

智能模式应从“逐命令谨慎确认”调整为“边界内默认工作，越界才打断”：

```text
Tool/command
  -> 硬拒绝：宿主越界、提权、Docker control、跨 lease、秘密外传
  -> 明确询问：不可逆删除、危险 Git、package install、raw network、外部副作用
  -> 明确放行：Docker 边界内普通读取/写入/计算/构建/测试/常规 Git
  -> 灰区 reviewer：仅处理确定性规则无法分类的复杂 shell 或工具
```

#### 必须继续硬拒绝

- 容器访问未挂载宿主路径、跨 Session/Run/lease 读取或写入；
- `sudo`/提权、Docker daemon/socket/control、宿主系统目录；
- 未声明目的地的秘密、凭据或私有数据外传；
- Host fallback 中突破 project/lease 边界的写入；
- 未登记、无 toolset、无 descriptor 的未知工具。

#### 智能模式应默认放行

- Docker 内 Python、Node 与其他可确定分类的普通本地计算；
- `sh/bash/zsh script.sh` 必须先读取并分类实际脚本内容，不能仅凭解释器名称自动放行；
- project/lease 边界内读取、生成、patch、格式化、构建和测试；
- `git status/diff/log/show/branch/rev-parse`；
- 建议追加结构化低风险本地 Git：`git add`、`git commit`、`git switch/checkout`（不覆盖未提交修改）、`git stash`；
- project 内可回退的文件移动/重命名；
- 已验证为公共、只读且通过 SSRF 防护的 `fetch_url` 与搜索工具；
- 子代理在父 Run 已授权 workspace/project/lease 内的同类动作。

#### 仍需询问，但授权应尽量聚合为 Session grant

- Docker 内安装/更新依赖以及为此临时联网；
- raw network、未知域名、下载后执行；
- `rm -r/-rf/--recursive`、`rmdir/truncate/dd/find -delete`、权限/所有权修改；普通 project 内单文件 `rm file` 可自动放行；
- `git reset/rebase/clean`、force 操作、覆盖本地修改；
- 外部文件或目录写回；
- POST/PUT/PATCH/DELETE、发送消息、创建事件、发布等外部业务副作用。

#### 不再按单个可执行文件粗暴判断

`python3`、`node`、`bash` 本身既不安全也不危险。分类输入必须至少包括：

- Backend（Docker / restricted host）；
- 工作目录、读写目标和 mount/lease；
- 是否联网、安装包、产生外部副作用；
- 是否删除、覆盖、提权或控制 Docker；
- 父 Run 已有的 Session grants。

因此，`python3` 在 Docker 内读取、计算和写项目文件应自动放行；同一个 `python3` 若尝试联网下载并执行、读取 secrets 或访问 lease 外路径，则继续询问或拒绝。

### 20.7 子代理授权收敛

截图中子代理反复请求 `workspace 命令授权`，不是安全收益，而是权限上下文没有被当成 Run 权威状态继承。目标模型：

1. 父 Run 创建 immutable `EffectivePermissionContext`，包含 Backend、project root、有效 leases、network/package grants、approval mode 和 policy version；
2. 子代理只能获得父上下文的相同或更小能力，不能自行扩大；
3. 同一 Run/Session 已批准的能力按语义 grant 复用，不再按 command string 或 tool_call_id 重复询问；
4. 子代理普通 Docker workspace 命令直接使用父 grant；触发 package/network/destructive/external side effect 时才产生新的聚合审批；
5. UI 把相同 grant 合并展示为一张卡，显示“谁申请、作用域、有效期、允许的能力”，不堆叠多个“本 Session”标签。

这里可借鉴 Yuxi 的 `file_thread_id / skills_thread_id` 分离，但不能继承它的任意 shell 直通：PuddingClaw 子代理共享父 Run 的 artifact/file scope，同时工具和 Skill visibility 可以进一步缩小。

### 20.8 业务工具补充控制面

StaffDeck 暴露出 PuddingClaw 下一阶段不能只治理 terminal。所有外部业务工具需要统一声明：

```yaml
side_effect: none | reversible | external_mutation | destructive
data_classification: public | internal | private | secret
network_scope: fixed_domain | declared_domains | arbitrary
idempotency: unsupported | optional | required
approval_scope: call | run | session
```

执行顺序建议为：

```text
tool visibility（tenant/agent/skill/step）
  -> ToolExecutionPipeline 风险判定
  -> HITL / grant
  -> idempotency key
  -> execute
  -> receipt + trace
```

这部分借鉴 StaffDeck 的多轴 binding 与副作用重放，但 idempotency 不能替代 approval：重复执行安全和“用户是否允许第一次执行”是两个问题。

### 20.9 开发优先级

#### P0：先消除低价值重复审批

1. 将 Docker/project/lease 内普通 Python、Node、构建、测试、写文件统一视为边界内操作；
2. 拆分当前过宽的 `managed_git_write`：低风险本地 Git 自动放行，reset/rebase/clean/force 继续询问；
3. 将 project 内 `mv/rename` 从 destructive 类中拆出，只有覆盖目标、移出边界或跨 lease 才询问；
4. 建立 `EffectivePermissionContext`，确保 subagent 与下一 Goal Run 继承 Session grants，修复重复 workspace 授权；
5. 审批 UI 按 grant 聚合，修复重复“本 Session”与无法看出具体能力的问题。

#### P1：引入受控灰区 reviewer 与业务副作用契约

1. 对确定性规则无法理解的复杂 shell 调用 reviewer；明确 allow/block/ask，reviewer 不可用时 ask，绝不静默扩大权限；
2. 为 HTTP/MCP/业务工具增加 side-effect、data、domain、idempotency descriptor；
3. 外部副作用必须产生 receipt，重试/恢复使用 idempotency key；
4. 增加高级用户自定义 rule，但规则只允许在产品硬边界内收紧或配置 ask/allow，不能取消 hard deny。

#### P2：Provisioner 化与远程沙箱

仅当 PuddingClaw 需要多用户服务端部署、远程执行或 Kubernetes 时，再把当前 Docker Backend 拆成独立 provisioner。届时借鉴 Yuxi 的 provider/provisioner、idle reaper 与 thread namespace，但保留 PuddingClaw 当前更强的非 root、cap drop、no-new-privileges、资源限制、默认断网和最小 env 注入。

### 20.10 验收场景

| 场景 | 智能模式期望 |
|---|---|
| 主 Agent 或子代理在 Docker project 内运行 Python/Node 处理文件 | 自动放行，不重复 workspace 授权 |
| 运行测试、格式化、构建、生成 HTML/JS | 自动放行 |
| `git add/commit/stash/switch` 且不覆盖未提交修改 | 自动放行并记录 Trace |
| `git reset --hard`、`git clean`、force 操作 | HITL |
| project 内重命名文件 | 自动放行；覆盖或移出边界时 HITL |
| 公共安全 URL 的只读 fetch/search | 自动放行 |
| Docker 缺包，申请安装并临时联网 | 一次 Session 级 package/network HITL，后续同 scope 复用 |
| Python 下载脚本后直接执行 | HITL，不能因命令名是 Python 自动放行 |
| 子代理使用父 Run 已批准的 workspace | 自动继承，不再申请 |
| 子代理扩大到新域名、外部目录或新副作用工具 | 新 HITL |
| POST 创建业务记录，因恢复再次调用 | 首次按策略审批，后续凭 idempotency receipt 重放，不重复创建 |
| 未注册 toolset 或缺少风险 descriptor 的新工具 | fail closed，并给开发者明确注册错误 |

本节机制来源区分：

- **借鉴 Codex**：boundary-first、Reviewer 不扩大 sandbox、低风险边界内动作低摩擦；
- **借鉴 Grok Build**：确定性 fast-path + 危险模式 + 会话感知灰区 classifier；
- **借鉴 StaffDeck**：tenant/agent/step/skill 多轴绑定和业务副作用幂等重放；
- **借鉴 Yuxi**：provider/provisioner 分离、file thread 与 skills thread 分离、稳定虚拟命名空间、idle lifecycle；
- **PuddingClaw 自有并继续保留**：ToolExecutionPipeline、Docker/Host 统一策略、Goal/Run/Session grants、ExternalArtifactLease、AttachmentEditLease、Artifact Receipt、默认断网与强容器硬化。

### 20.11 P0/P1 首版实现记录与溯源

#### 实际采用的参考机制

| 最终机制 | 参考来源 | PuddingClaw 落点 | 实现状态 |
|---|---|---|---|
| sandbox boundary 内低摩擦执行，reviewer 不扩大边界 | **Codex boundary-first / auto-review** | `harness/tool_execution.py::ToolExecutionPipeline` 在确定性 host path、Docker control、network、package、destructive 检查之后才进入智能快路径或 reviewer | 已实现 |
| deterministic fast-path → dangerous rules → LLM gray zone | **Grok Build `permission/auto_mode.rs`、`manager.rs`、`rules.rs`** | `ShellPolicyAnalyzer` + `ModelPermissionReviewer`；reviewer 仅接收 eligible ASK，失败时回退人工确认 | 已实现 |
| 常规本地 Git 低摩擦，危险 Git 单独拦截 | **Codex 工作区边界 + Grok 本地 Git fast-path** | `git add/commit/switch/stash` 与安全 branch checkout 自动放行；reset/clean/rebase/restore、force、checkout path、stash drop/clear 继续 HITL | 已实现 |
| 工具注册时声明副作用契约 | **StaffDeck 业务工具 binding/idempotency 启发 + PuddingClaw 自有控制面** | `tools/toolsets.py::ToolControlDescriptor` 覆盖全部已注册 Tool；缺 descriptor 的 Tool fail closed；runtime inventory 投影 control 信息 | 已实现首版 |
| 子代理共享任务文件域但不能扩大能力 | **Yuxi file-thread 继承思想 + Codex/Grok 统一策略边界** | 主 Agent 与 subagent 共享同一个冻结 `RunPermissionContext`、Backend 与 Session grant 权威源；subagent 单独建立 Pipeline，但不能修改父 Run policy | 已实现 |

#### 本轮具体行为边界

- 智能 Docker 模式自动放行：普通 Python/Node 本地计算与 project 写入、测试/构建/格式化、`git add/commit/switch/stash`、project 内静态 `mv`、非递归 `rm file`。
- `sh/bash/zsh script.sh` 不是白名单。Harness 将脚本解析为实际命令后再判定；无法读取、路径越界或脚本语义不透明时继续询问。Reviewer 收到脚本时同时看到已读取的脚本正文，避免只审查无害的 wrapper。
- 确定性规则不能理解的普通终端命令不会静默放行，而由 `ModelPermissionReviewer` 判断 allow/ask/deny；它无权放开联网、安装包、递归删除、提权、Docker control、host path 或外部副作用。
- `rm -r/-rf/--recursive`、`find -delete/-exec`、危险 Git、下载执行、package install、raw network、外部 artifact commit 仍为 HITL 或 hard deny。
- 等价 Session grant 在 `SessionManager.add_permission_grant` 中去重；主 Agent 与 subagent 都从 Session JSON 消费同一绑定 grant，避免重复授权卡。Run policy 继续绑定 `policy_epoch + policy_version + backend + workspace`，切换策略后旧 grant 失效。
- 权限请求与 Trace 记录 `policy_source`、中文 `policy_explanation` 和 `control_descriptor`；前端区分“确定性规则”与“智能审查后需确认”。

#### 关键代码文件

- `backend/harness/tool_execution.py`：确定性分类、脚本正文检查、智能 Docker fast-path、Git/mv/rm 边界、reviewer 接入；
- `backend/harness/permission_reviewer.py`：灰区审查 prompt、结构化结果与 fail-closed；
- `backend/tools/toolsets.py`：Tool control descriptor 单一注册表；
- `backend/graph/permission_policy.py`、`backend/graph/session_manager.py`：冻结 Run policy、绑定校验、Session grant 复用与去重；
- `backend/graph/deepagents_manager.py`：main/subagent 共享 effective permission context 与 reviewer，inventory 暴露 control contract；
- `frontend/src/components/chat/ChatMessage.tsx`、`frontend/src/components/citations/SourcesPanel.tsx`：中文风险说明、智能审查来源与 grant 展示。

#### 尚未伪称完成的部分

- HTTP/MCP 外部副作用的通用 receipt/idempotency executor 尚未统一；当前 descriptor 已能 fail closed 和声明契约，既有附件、外部 artifact、SQL 等流程继续使用各自 receipt/plan 协议。
- 高级用户自定义 permission rules 尚未开放到 Harness 设置；后续规则只能在 hard boundary 内调整 allow/ask，不能取消 hard deny。
- P2 独立 provisioner、Kubernetes 与 exact-Run 物理隔离仍是后续产品取舍，不属于本轮权限降噪。

#### 本轮验证

- `pytest -q backend/tests`：862 passed，24 warnings；
- 智能审批定向回归覆盖 Python/Node、Git、mv、单文件 rm、递归删除、shell script inspection、gray-zone reviewer、fail-closed、descriptor 完整性和 Session grant 去重；
- Frontend `tsc --noEmit` 与 Next.js production build（13 个静态路由）通过；
- Ruff 定向检查与 `git diff --check` 通过。

### 20.12 单文件外部产物的 Backend 写入收口

最新真实 Session 暴露的故障并非数据查询失败，而是外部文件进入 Docker 后的路径语义不一致：

1. `stage_external_artifact` 已把精确文件放入 `/scratch/external/<lease_id>/...`，但模型把 `/scratch` 误当成宿主机非 workspace 路径，重复调用 `read_resource`，产生虚假的 `File not found`；
2. Python heredoc 中的 JSON/数组 `[` 被 ShellPolicyAnalyzer 误判成容器路径通配展开，返回 `container_path_expansion (critical)`；
3. 同一 Run 对同一精确目标重复 staging 会生成多个 lease，模型在多个 scratch 路径之间漂移；
4. 精确文件授权不应隐式扩大到父目录，但读取文件后发现真实兄弟依赖时，应允许 Agent 调用 `stage_external_directory(parent)` 主动发起目录级 HITL。

收口后的权威规则：

- `/scratch/...` 与 `/workspace/...` 同属 Backend 虚拟命名空间；前者用 `read_file`、Harness 文件工具或受控 Terminal，不得交给 `read_resource`。执行边界对误用的 `read_resource(/scratch/...)` 自动适配到 Backend read，避免模型循环；
- `read_resource` 只负责附件、托管知识以及宿主机精确外部文件；
- Python/Node heredoc 的数据数组不再触发 critical path expansion；真实 `/harness-scratch`、路径遍历、通配探测仍确定性拒绝。智能 Docker 模式下，边界内无联网、无安装、无破坏性的 heredoc 文件处理自动放行；
- 同一 Session/Run/Query/Goal revision/target 的活动 `ExternalArtifactLease` 必须复用，保留已经完成的 staged edits；外部权威源发生变化仍 fail closed；
- 精确文件默认不推断父目录。只有 Agent 读后确认确需发现 sibling dependency，才调用 `stage_external_directory` 请求显式目录授权；整个目录联调仍优先提示用户将目录作为项目打开。

该修复属于 **PuddingClaw 自有 Backend/Harness 边界一致性修复**；低摩擦的 sandbox 内解释器执行继续沿用前文借鉴的 Codex boundary-first 与 Grok deterministic fast-path 思路。
