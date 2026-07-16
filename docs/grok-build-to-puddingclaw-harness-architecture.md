# 从 Grok Build 反推 PuddingClaw Harness：差距分析与演进架构

状态：**方案已审核，第一阶段实现位于 `codex/harness-control-plane`；后端 492 项测试、前端生产构建、Goal/Rubric UI E2E 与真实 Docker Backend E2E 已通过**
日期：2026-07-16
Grok Build 源码：<code>b189869b7755d2b482969acf6c92da3ecfeffd36</code>
PuddingClaw 基线提交：<code>7fb380f43be9c9b13fd3478bb28ef1a637fe6203</code>
说明：PuddingClaw 分析包含当前工作区尚未提交的 Tool Context、Workspace Router、Session 持久化等改动。

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
    permission_mode: standard         # standard | dont_ask | future_auto
    remember_session_approvals: true

    docker:
      connection: auto                # auto | context
      context: desktop-linux
      image: puddingclaw/sandbox:python3.12-node22-v2
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
- 默认展示不可编辑的 PuddingClaw 托管镜像；“使用自定义镜像（高级）”、说明与 image reference 输入框包装在独立卡片中，开启后在卡片内部展开输入框；
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
  "policy_version": "terminal-policy-v1",
  "image_digest": "sha256:...",
  "fallback_reason": null
}
~~~

Docker 主动关闭时使用 <code>restricted_host</code>，这是用户选择，不叫 fallback。只有 Docker 已启用但 CLI、daemon、image 或安全运行条件不可用时，才记录 <code>fallback_reason</code>。

自动降级只允许发生在命令执行前的 Backend preflight。某个 Run 已经在 Docker 中执行后，如果 Docker 中途故障，不能把失败命令静默转到宿主重放；应返回 <code>sandbox_unavailable</code>，由用户决定后续是否切换。

用户选择项目并启用 Docker Backend 后，PuddingClaw 按项目生命周期自动准备默认托管镜像和项目容器；这是用户启用 Docker 后的 Backend provisioning，不要求额外进入设置页操作。Run 中 Agent 执行 <code>npm ci</code>、<code>uv sync</code>、<code>pip install</code> 等项目依赖安装命令时，仍必须进入 <code>ToolExecutionPipeline</code> 的 <code>package_install/network_access</code> HITL。

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

### 5.4.1 托管运行时与项目依赖准备

来源：**[本方案新增]**。

默认托管镜像至少保证：

~~~text
Python 3.12 + pip
Node.js 22 + npm + corepack
仅附带基础 POSIX shell 与 CA 证书
~~~

普通用户不需要选择或上传镜像。PuddingClaw 使用内置 Dockerfile 在本机 Docker daemon 中自动准备托管镜像。高级用户可以填写本机已有的 Docker tag 或 registry image reference；Backend preflight 会验证 <code>python3</code> 与 <code>node</code> 同时存在，不满足基础运行时契约时按 <code>on_unavailable</code> 选择受控降级或拒绝。

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

依赖下载的联网闭环：

~~~text
Agent 提交 manifest-derived 精确安装命令
  → ToolExecutionPipeline 分类为 package_install / network_access
  → 用户 allow-once 或 allow-session
  → 默认 network=none 的项目容器临时 connect bridge
  → docker exec 执行获批的原始命令
  → finally disconnect bridge
  → 断网失败则删除容器，避免授权结束后残留联网能力
~~~

即使用户开启“容器常驻网络”，网络类和包安装类命令仍需经过权限管线；这个开关只改变 Sandbox capability，不改变授权结果。

第三方 Skill 的依赖属于运行中动态依赖，不并入项目启动时的 manifest 扫描：

~~~text
Agent 成功读取 /skills/<skill-id>/SKILL.md
  → Skill 流程发现缺少 Python / Node 包
  → 提交精确 pip / uv / npm / npx 命令
  → ToolExecutionPipeline 分类为 package_install
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

该 volume 随项目沙箱复用，不污染宿主 Python/Node 环境，也不要求每个 Run 重装。<code>PATH</code>、<code>PYTHONUSERBASE</code>、<code>npm_config_prefix</code> 和 <code>NODE_PATH</code> 由 Backend 统一设置。Skill 自己要求修改项目 <code>package.json</code> 或项目虚拟环境时，仍写入项目依赖通道并受 workspace write policy 约束。

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
3. 将 satisfied、needs_revision exhausted、grader_error 映射为不同 RunOutcome；
4. 将 Run verification report 写入 Session；仅当存在 <code>goal_id</code> 时再写 GoalState；
5. 通过 SSE/Trace 向 UI 呈现“已验证、未通过、验证器异常”；
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
  → AnalyticsTaskContractBuilder 提取任务类型和显式要求
  → RubricCompiler 注入系统强制标准与智能问数模板
  → 自动分配 deterministic / analytics / llm verifier
  → 固化为 Run verification contract
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

### 10.2 输入区 Goal 交互

在现有 <code>frontend/src/components/chat/ChatInput.tsx</code> 底部工具栏增加“目标”入口，仅在 Agent 模式显示，与项目、分析模型和思考模式并列。

首版交互：

1. 未开启时点击“目标”，打开轻量 Popover；
2. 用户确认后，本次发送携带 <code>goal_mode=true</code>，后端创建 Goal；
3. Goal 创建后，入口变为带状态的 active chip，例如“目标进行中”；
4. 点击 active chip 可查看目标描述、当前状态、剩余预算、gaps，并执行暂停、恢复或取消；
5. Goal 达成后显示“目标已完成”，下一次发送默认回到普通模式，除非用户新建 Goal；
6. 用户取消当前 Run 时不自动取消 Goal；“停止本轮”和“取消目标”必须是两个不同操作。

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

## 15. 待审核的十个决策

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
| Tool 类型化结果、错误不吞、preflight | **[Grok Build 借鉴]** | 适配为 ToolExecutionPipeline 与统一 ToolResult | 待审核 |
| AccessKind、managed policy、permission 分层 | **[Grok Build 借鉴]** | 结合现有 external-file permission 和 Toolset 硬门 | 待审核 |
| allow/ask/deny、deny > ask > allow | **[Grok Build 借鉴]** | 实现 PuddingClaw CompiledPolicy | 待审核 |
| Bash chained segment / wrapper / bash -c 解析 | **[Grok Build 借鉴]** | Python 侧 Shell AST analyzer，解析失败 ask | 待审核 |
| Shell read/write/redirect/symlink 访问提取 | **[Grok Build 借鉴]** | 重新进入 filesystem permission policy | 待审核 |
| Session command grant | **[Grok Build 借鉴] + [PuddingClaw 适配]** | 写入既有 Session permission 状态 | 待审核 |
| LLM auto permission classifier / YOLO | **[Grok Build 借鉴候选]** | 第一阶段不采用 | 暂不采用 |
| sandbox auto-allow bash | **[Grok Build 借鉴候选]** | 默认不采用；未来也必须晚于 hard policy/path analysis | 暂不采用 |
| OS 级 sandbox profile 思想 | **[Grok Build 借鉴]** | 不复制 Rust nono；映射为 Docker mount/network/capability profile | 待审核 |
| DeepAgents SandboxBackendProtocol | **[DeepAgents 复用]** | Workspace Backend 实现 id/execute/aexecute，启用内置 execute | 待审核 |
| DockerWorkspaceBackend | **[本方案新增]** | FilesystemBackend + SandboxBackendProtocol hybrid adapter | 待审核 |
| RestrictedHostWorkspaceBackend | **[本方案新增]** | 同一协议下的 best-effort fallback | 待审核 |
| 一项目一个长生命周期容器 | **[本方案新增]** | project sandbox lease + idle stop + generation | 待审核 |
| effective tool manifest | **[PuddingClaw 延续] + [Grok Build 命名/呈现借鉴]** | 复用已有 model.input 最终 tool schemas，补顶层投影、UI 和 inventory 对账 | 核心机制已有，待产品化 |
| Session JSON 跨 Run 权威 | **[PuddingClaw 延续]** | 保持现状，不重新设计 | 已冻结 |
| LangGraph checkpoint 只负责同 Run HITL | **[PuddingClaw 延续] + [DeepAgents 复用]** | 保持 <code>session_id:query_id</code> | 已冻结 |
| Trace 只记录、不恢复 | **[PuddingClaw 延续]** | 增加 RunOutcome/GoalDecision 观测，不参与控制 | 已冻结 |
| Tool Context output/UI evidence 分离 | **[PuddingClaw 延续]** | 保持当前即时保护和后台压缩 | 已有 |
| TodoGate / 未完成任务 nudge | **[Grok Build 借鉴]** | 结合 DeepAgents Todo，做有限次数 Analytics Completion Gate | 待审核 |
| Goal status、budget、gaps、continuation | **[Grok Build 借鉴]** | 仅用户显式开启 Goal Mode 时，由 Analytics GoalState 与 GoalCoordinator 跨 Run 推进 | 待审核 |
| 单 Rubric grader | **[DeepAgents 复用] + [本方案适配]** | 复用 RubricMiddleware 的迭代/状态机；PuddingClaw 使用无 Tool 严格 JSON transport，避免 thinking/tool_choice 冲突 | 已实现并 E2E |
| 智能问数 deterministic checks | **[本方案新增]** | SQL、指标、Join、时间、覆盖、引用检查先于 Rubric | 待审核 |
| RubricCompiler 与 criterion source 合并 | **[本方案新增]** | 合并 system/managed/settings/user/task criteria，严格者胜出 | 待审核 |
| Harness Settings 高级 Rubric 规则 | **[本方案新增]** | 普通用户零维护；高级用户只能追加/强化并选择注册 verifier | 待审核 |
| CompletionVerificationCoordinator / RubricEvaluationReport | **[本方案新增]** | 对每个需要验收的 Run 汇总 deterministic、analytics 与 LLM criterion evidence | 待审核 |
| Agent 输入区“目标”模式与 active Goal chip | **[产品交互借鉴] + [本方案适配]** | 显式开启 Goal、查看状态并区分停止 Run/取消 Goal | 待审核 |
| GoalCard / VerificationCard | **[本方案新增]** | GoalCard 仅 Goal Mode 显示；VerificationCard 可独立展示 Run 级验收 | 待审核 |
| Goal SSE 与前端 Session 级状态 | **[本方案新增]** | done 只结束传输，由 RunOutcome/GoalDecision 决定完成语义 | 待审核 |
| HarnessRunCoordinator | **[本方案新增，受 Grok 嵌套状态机启发]** | 协调既有 Session/Checkpoint/Trace，不创建新权威 | 待审核 |
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
