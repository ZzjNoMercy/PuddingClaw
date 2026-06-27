# Agent/Chat 运行时拆分：Agent 走 DeepAgents，Chat 保留 LangChain

> 目标：参考 `designs/chat-layout-redesign/index.html` 的 `SegmentedTabs` 设计，在 Sidebar 提供 **Agent / Chat** 两种模式切换。Agent 模式使用 DeepAgents + `ModelClientChatModel`；Chat 模式保留现有 LangChain `create_agent` + 自定义 middleware 链路。Chat 模式下不再显示"项目"相关选项。

## 1. 背景与目标

### 1.1 当前状态

- 当前 PuddingClaw 只有一个运行时：`backend/graph/agent.py` 里的 `AgentManager`，基于 LangChain `create_agent`。
- 前端 `/` 页面就是 Chat 页面，Sidebar 里有"项目"区块但当前是空的（"暂无项目"）。
- Notebook 验证已确认：`ModelClientChatModel` 可以作为 latest DeepAgents `create_deep_agent(model=...)` 的模型参数，且 Higress 路径已跑通。

### 1.2 目标

| 模式 | 运行时 | 工作区 | Sidebar 显示 |
|---|---|---|---|
| **Agent** | DeepAgents `create_deep_agent` | 选择项目时使用该项目目录；未选择项目时使用隐式 session workspace | 显示"项目"列表；真实项目下显示关联 session；未选择项目的 Agent session 显示在普通"对话"列表 |
| **Chat** | LangChain `create_agent` + 现有 middleware | 无项目概念，纯对话，不操作本地文件 | 不显示"项目"，只显示全局对话 session |

### 1.3 核心架构原则：透明 Claw

DeepAgents 只能作为 **Agent runtime 与工具编排层**，不能取代 PuddingClaw 的用户可见会话模型。

硬约束：

- **Claw JSON 是产品事实源**：前端历史、session 列表、导出、调试、透明上下文都读取 PuddingClaw 的 `session.json`。
- **DeepAgents checkpoint 是运行时恢复态**：只用于 HITL resume、长任务中断恢复、graph state、内部 todo/files state 等 runtime 场景。
- **checkpoint/backend 内部状态不得成为唯一持久化来源**。
- 一轮 Agent 正常完成后，必须把 DeepAgents 最终状态同步回 Claw `session.json`。
- 前端永远不直接读取 checkpoint。

分层如下：

```text
用户可见层：
  session.json
  display_messages
  compressed_context
  middle_trim_context
  project_path / project_id
  tool_calls summary_source

DeepAgents 内部层：
  checkpoint(thread_id=session_id)
  graph state
  HITL resume state
  files/todos runtime state
```

## 2. 前端改动

### 2.1 Sidebar 增加模式切换

参考 `designs/chat-layout-redesign/components.jsx` 的 `SegmentedTabs`：

```jsx
function SegmentedTabs() {
  const [active, setActive] = useState("chat");
  return (
    <div className="flex items-center rounded-lg bg-gray-100/80 p-0.5">
      <button
        onClick={() => setActive("work")}
        className={...}
      >
        <BriefcaseIcon size={14} />
        Agent
      </button>
      <button
        onClick={() => setActive("chat")}
        className={...}
      >
        <MessageCircleIcon size={14} />
        Chat
      </button>
    </div>
  );
}
```

实施：

1. 在 `frontend/src/lib/store.tsx` 增加全局状态：
   ```ts
   interface AppState {
     // ... 已有状态
     runtimeMode: "agent" | "chat";
     setRuntimeMode: (mode: "agent" | "chat") => void;
   }
   ```

2. 在 `frontend/src/components/layout/Sidebar.tsx` 顶部增加 `SegmentedTabs` 组件。

3. 当切换到 **Agent** 模式时：
   - 显示"项目"区块，项目是用户选择的文件夹
   - 每个项目下显示属于该项目的 session
   - 点击项目切换工作区；点击项目下的 session 恢复对话
   - "新建对话" 在当前选中的项目工作区内创建 session
   - 发送消息时走 `/api/agent`，并携带 `project_id`
   - `project_path` 只在项目登记时出现；后续对话请求不直接信任前端传入的任意 path

4. 当切换到 **Chat** 模式时：
   - **隐藏"项目"区块**
   - 只显示全局对话 session
   - "新对话" 创建不关联项目的 session
   - 发送消息时走 `/api/chat`

### 2.4 Sidebar 项目与无项目 Agent Session 规则

最终 UI 结构：

```text
项目
├─ PuddingClaw
│  ├─ 评估引入 Higress
│  └─ 动态罗列 skill 文档
├─ 好得APP
│  ├─ 优化过期事件颜色优先级
│  └─ 实现全局搜索
├─ MagicClaw
│  └─ 输出 LangChain 架构文档
└─ HiClaw
   └─ 暂无对话

对话
├─ 安装 aihot skill
├─ 生成 agent 介绍视频提示词
└─ 创建AI三平台报告
```

规则：

- 项目名默认取 `project_path.name`，例如 `/Users/pet/Code/AI/Agent/PuddingClaw` 显示为 `PuddingClaw`。
- 有 `project_id` 的 Agent session 显示在对应项目下面。
- 用户在 Agent 模式未选择项目就发起 Agent work 时，UI 不显示"默认工作区"项目；该 session 显示在普通"对话"列表。
- Agent / Chat 两个 tab 的 session 列表必须按 `runtime_mode` 隔离：
  - Agent tab：只显示 `runtime_mode="agent"` 的 session；其中有 `project_id` 的显示在项目下，无 `project_id` 的显示在 Agent tab 的"对话"下。
  - Chat tab：只显示 `runtime_mode!="agent"` 的 session；老数据没有 `runtime_mode` 时按 Chat legacy 处理。
- 无项目 Agent session 虽然也显示在"对话"标题下，但只在 Agent tab 出现，不进入 Chat tab。
- 无项目 Agent session 后端仍创建隐式 workspace，保证 DeepAgents 始终有明确 filesystem root。
- 无项目 Agent session 的 metadata：

```json
{
  "runtime_mode": "agent",
  "project_id": null,
  "project_path": null,
  "workspace_type": "unscoped_agent",
  "workspace_path": "~/PuddingClaw/Workspaces/Unscoped/<session_id>"
}
```

- DeepAgents 运行时：

```python
root_dir = project_path if project_id else workspace_path
FilesystemBackend(root_dir=root_dir, virtual_mode=True)
```

- 后续可支持"移动到项目"：将无项目 Agent session 绑定到真实 `project_id`，并按产品策略决定是否迁移 workspace 文件。

### 2.2 ChatPanel 适配

`ChatPanel` 本身不需要大改，因为两种模式的事件协议保持兼容（都是 SSE：`token`, `tool_start`, `tool_end`, `done`, `error` 等）。

需要改的是 `sendMessage`：

```ts
const { runtimeMode, currentProjectId } = useApp();
const stream = runtimeMode === "agent"
  ? streamAgent(message, sessionId, currentProjectId, signal, userId)
  : streamChat(message, sessionId, signal, userId);
```

### 2.3 API 客户端新增

在 `frontend/src/lib/api.ts` 新增：

```ts
export async function* streamAgent(
  message: string,
  sessionId: string,
  projectId: string,
  signal?: AbortSignal,
  userId?: string
): AsyncGenerator<SSEEvent> { ... }
```

并新增 Next.js route：`frontend/src/app/api/agent/route.ts`（和 `/api/chat/route.ts` 结构一致，代理到 backend `/api/agent`）。

## 3. 后端改动

### 3.1 新增 DeepAgentsAgentManager

新增文件：`backend/graph/deepagents_manager.py`

核心职责：
- 使用 `deepagents.create_deep_agent` 构建 agent
- `model=ModelClientChatModel(role="agent", streaming=True)`
- 使用 DeepAgents filesystem / skills / todo / task 套件作为 Agent 模式默认能力
- 工具池仅补充 PuddingClaw 非冲突工具
- 以 `project_id` 解析出的项目目录作为 DeepAgents 文件工作区
- 提供 `astream()` 方法，输出与当前 `AgentManager` 兼容的事件协议

最小实现：

```python
from deepagents.backends import CompositeBackend, FilesystemBackend
from deepagents import create_deep_agent
from llm.model_client import ModelClientChatModel
from tools import get_all_tools

class DeepAgentsAgentManager:
    def __init__(self) -> None:
        self._base_dir: Path | None = None
        self._tools: list = []

    def initialize(self, app_base_dir: Path) -> None:
        self._base_dir = app_base_dir
        self._tools = filter_agent_mode_tools(get_all_tools(app_base_dir))

    async def astream(
        self, message: str, history: list[dict], project_id: str, user_id: str = "default_user", session_id: str = ""
    ) -> AsyncGenerator[dict, None]:
        project_path = project_registry.resolve(project_id)
        backend = CompositeBackend(
            default=FilesystemBackend(root_dir=project_path, virtual_mode=True),
            routes={
                "/skills/": FilesystemBackend(root_dir=self._base_dir / "skills", virtual_mode=True),
            },
        )
        model = ModelClientChatModel(role="agent", streaming=True)
        agent = create_deep_agent(
            model=model,
            tools=self._tools,
            backend=backend,
            skills=resolve_enabled_skill_paths(project_id),
            checkpointer=checkpointer,
        )

        # 历史消息转换
        messages = self._build_messages(history) + [{"role": "user", "content": message}]

        async for event in agent.astream(
            {"messages": messages},
            stream_mode=["messages", "updates", "custom"],
            config={"configurable": {"thread_id": session_id}},
        ):
            # Adapter: 把 DeepAgents graph event 转成当前 SSE 事件协议
            yield from self._adapt_event(event)

    def _adapt_event(self, event) -> AsyncGenerator[dict, None]:
        # 生产 token / tool_start / tool_end / done / error 等事件
        ...
```

### 3.2 现有 AgentManager 重命名或保留

方案 A（推荐）：保留现有 `AgentManager` 不变，作为 Chat 模式运行时。

方案 B：将现有 `AgentManager` 改名为 `LangChainAgentManager`，新增 `DeepAgentsAgentManager`，由工厂函数选择。

建议先按方案 A 做，风险最小。新增 `DeepAgentsAgentManager`，现有代码不动。

### 3.3 新增 /api/agent 路由

新增或修改 `backend/api/chat.py`：

```python
@router.post("/agent")
async def agent_chat(request: ChatRequest):
    if request.stream:
        return EventSourceResponse(
            deepagents_event_generator(request.message, request.session_id, request.project_id, request.user_id)
        )
    result = await deepagents_agent_manager.ainvoke(...)
    return {"reply": result}
```

也可以新建 `backend/api/agent.py` 更干净。

### 3.4 SSE 事件适配（关键）

DeepAgents `agent.astream(stream_mode=["messages", "updates", "custom"])` 产出的事件和当前 `AgentManager._run_agent_stream` 不完全相同。

需要写一个 adapter 把 DeepAgents 事件映射为当前前端认识的协议：

| DeepAgents 事件 | 当前 SSE 事件 |
|---|---|
| model 节点的 AIMessageChunk（内容） | `token` |
| model 节点的 AIMessage.tool_calls | `tool_start` |
| tools 节点的 ToolMessage | `tool_end` |
| 自定义 middleware 事件 | `context_maintenance` / `tool_result_clear` / `compaction` |
| graph 结束 | `done` |
| 异常 | `error` |

当前 `AgentManager._run_agent_stream` 已经有类似的解析逻辑，可以直接参考。

### 3.5 工具冲突处理

Agent 模式的工作区是用户选择的项目文件夹。DeepAgents 默认会注入文件、todo 与子代理工具：

- `write_todos`
- `ls`
- `read_file`
- `write_file`
- `edit_file`
- `glob`
- `grep`
- `task`

Agent 模式能力归属：

| 能力 | Agent 模式实现 |
|---|---|
| 技能执行 | DeepAgents `skills` |
| 文件操作 | DeepAgents filesystem suite：`ls/read_file/write_file/edit_file/glob/grep` |
| 项目外文件读取 | PuddingClaw 外部文件授权工具；必须用户确认，默认只读、session 级授权 |
| Todo / 子代理 | DeepAgents `write_todos` / `task` |
| 终端 | 暂时保留 PuddingClaw `terminal`；后续如接入 DeepAgents sandbox `execute` 再替换 |
| 搜索 / 知识库 / MCP | 继续保留 PuddingClaw 工具 |

Agent 模式下禁用 PuddingClaw 重叠工具：

- `read_file`
- `write_file`
- `task_manager`
- `execute_skill`

Agent 模式下保留 PuddingClaw 非冲突工具：

- `terminal`
- `tavily_search`
- `fetch_url`
- `search_knowledge_base`
- MCP tools
- memory tools（是否默认启用可后续按产品体验调整）

说明：

- `execute_skill` 不再作为 Agent 模式默认工具；技能执行由 DeepAgents `skills` 接管。
- 若技能需要执行脚本，不再通过 PuddingClaw `execute_skill` 二次包装，而是让 DeepAgents 先按需读取 `/skills/<skill>/SKILL.md`，再调用 PuddingClaw `terminal` 直接执行脚本，例如 `python3 /skills/aihot/scripts/aihot_query.py ...`。
- `create_deep_agent` 需要同时配置 `skills=["/skills/"]` 和 backend `/skills/` route：前者告诉 DeepAgents 扫描技能目录，后者让这个虚拟路径能实际读到 PuddingClaw 全局 skills。
- DeepAgents 默认 `execute` 只有在 backend 实现 `SandboxBackendProtocol` 时才可用；第一版不依赖它。
- PuddingClaw `terminal` 必须继续遵守项目目录边界，默认工作目录应为当前 `project_path` 或无项目 Agent session workspace；同时将 DeepAgents 虚拟路径 `/skills` 映射到后端真实 skills 目录，保证技能脚本可用同一套路径表达。
- DeepAgents filesystem 只负责项目目录内文件；当用户粘贴或请求读取项目目录外的绝对路径时，不打开 `virtual_mode=False`，也不直接扩展 DeepAgents backend，而是走 PuddingClaw 外部文件授权工具。
- 项目外文件访问必须先触发用户授权事件；第一版只支持只读、session 级授权。授权通过后，由 PuddingClaw 文件工具读取内容并作为 tool result 返回给 DeepAgents。

项目外文件读取流程：

```text
Agent 请求读取绝对路径
  ↓
后端 resolve 路径并判断不在 project_path
  ↓
发起 permission_request: external_file_read
  ↓
用户确认
  ↓
session 记录 allowed_external_paths
  ↓
PuddingClaw 文件工具读取该文件
  ↓
内容作为 tool result 返回给 Agent
```

### 3.6 DeepAgents backend、skills 与 checkpoint

Agent 模式使用前端登记的项目目录构建 DeepAgents backend：

```python
backend = CompositeBackend(
    default=FilesystemBackend(root_dir=project_path, virtual_mode=True),
    routes={
        "/skills/": FilesystemBackend(root_dir=global_skills_dir, virtual_mode=True),
    },
)
```

设计要点：

- 默认路由指向用户项目目录，DeepAgents 文件工具只在该项目内读写。
- `/skills/` 路由指向 PuddingClaw 全局 skills 目录，建议只读；用于 DeepAgents skills 读取技能说明。
- 用户不需要在后台配置项目目录；项目目录由前端选择后登记。
- `thread_id` 使用 Claw `session_id`，用于把 checkpoint 与 session 一一对应。

checkpoint 与 Claw JSON 同时启用时的主从规则：

1. 正常完成的一轮对话：DeepAgents 最终 messages 转换并写入 Claw `session.json`。
2. HITL / 长任务中断：checkpoint 保存 runtime 状态，Claw JSON 记录当前可见进度。
3. resume：通过 `thread_id=session_id` 从 checkpoint 恢复，不从 JSON 重新塞完整 messages 造成重复。
4. resume 完成：最终状态再次同步回 Claw `session.json`。
5. checkpoint 可清理；Claw JSON 不可丢。

### 3.7 本地客户端沙箱模式

PuddingClaw 是本地客户端形态，不应强制用户安装或启用 Docker。Agent 模式提供三档能力模式，由用户在设置中选择。

| 模式 | 文件操作 | 终端执行 | 默认建议 | 适合场景 |
|---|---|---|---|---|
| 轻量模式 | DeepAgents `FilesystemBackend` | PuddingClaw `terminal` | 默认 | 本地个人使用、资源少、无需 Docker |
| Docker 沙箱模式 | `DockerSandboxBackend` | DeepAgents `execute` | 可选增强 | 需要隔离、安全、可复现 |
| 禁用终端模式 | DeepAgents `FilesystemBackend` | 无终端 | 可选 | 只允许 Agent 改文件，不允许跑命令 |

默认模式：

```text
FilesystemBackend(project_path, virtual_mode=True)
+ PuddingClaw terminal(project_path cwd)
```

原因：

- 门槛最低，不要求 Docker。
- 能快速支持项目文件读写和必要终端操作。
- 适合本地单用户、信任项目的场景。

#### 3.7.1 DockerSandboxBackend 目标形态

Docker 沙箱模式作为增强能力，而不是 MVP 阻塞项。后续实现一个 PuddingClaw 自有 DeepAgents backend：

```python
class DockerSandboxBackend(BaseSandbox):
    def execute(self, command: str, *, timeout: int | None = None) -> ExecuteResponse:
        ...

    def upload_files(self, files: list[FileUploadResponse]) -> list[FileUploadResponse]:
        ...
```

容器映射：

```text
host project_path  -> container /workspace
backend/skills     -> container /skills:ro
container workdir  -> /workspace
```

容器原则：

- 每个 `project_id` 默认复用一个容器。
- `/workspace` 可读写。
- `/skills` 只读。
- 默认不继承宿主 `.env`、SSH key、云凭证、provider key。
- network 默认关闭，用户可显式开启。
- 命令执行必须有 timeout 与最大输出限制。

Docker 资源配置由用户在设置中选择，例如：

```json
{
  "agent_sandbox": {
    "mode": "docker",
    "image": "puddingclaw-agent-sandbox:latest",
    "memory": "4g",
    "cpus": 2,
    "network": "disabled",
    "auto_start": true,
    "reuse_per_project": true
  }
}
```

前端设置项：

- 沙箱模式：
  - 轻量模式
  - Docker 沙箱
  - 禁用终端
- CPU：
  - 1 / 2 / 4 / 自定义
- 内存：
  - 1GB / 2GB / 4GB / 8GB / 自定义
- 网络：
  - 禁用（推荐）
  - 允许
- 容器复用：
  - 每个项目复用一个容器（推荐）
  - 每次任务新建容器（更干净但慢）
- 启动时自动检查 Docker：
  - 开 / 关

后端按模式选择 backend 与工具：

```text
mode = light
  backend = FilesystemBackend(project_path, virtual_mode=True)
  tools 保留 PuddingClaw terminal
  不启用 DeepAgents execute

mode = docker
  backend = DockerSandboxBackend(project_path, resources)
  tools 不保留 PuddingClaw terminal
  启用 DeepAgents execute

mode = no_terminal
  backend = FilesystemBackend(project_path, virtual_mode=True)
  tools 不保留 PuddingClaw terminal
  不启用 DeepAgents execute
```

实现路线：

1. MVP：`FilesystemBackend` + PuddingClaw `terminal`。
2. PoC：实现 `DockerSandboxBackend.execute()` 与 `upload_files()`，手动创建容器。
3. 可用版：新增 `SandboxManager`，支持 `project_id -> container_name`、自动 create/start/stop。
4. 产品版：补资源限制、network 开关、环境变量隔离、容器清理、状态展示。

验收：

- 轻量模式不依赖 Docker。
- Docker 模式下 `execute("pwd")` 返回 `/workspace`。
- Docker 模式下文件写入真实落到项目目录。
- `/skills` 可读不可写。
- timeout / 最大输出限制生效。
- 禁用终端模式下没有任何 shell 执行入口。

### 3.8 Agent 模式 Context Engineering 策略

Agent 模式不是完全抛弃现有 Context Engineering，而是保留透明历史与必要压缩能力，避免与 DeepAgents 内置能力重复。

| 能力 | Agent 模式策略 |
|---|---|
| `_maybe_middle_trim_session()` | 保留，在 `/api/agent` 入模前执行 |
| `load_session_for_agent()` | 保留，负责注入 `compressed_context` / `middle_trim_context` |
| `_build_messages()` | 必须复用或抽成公共函数，负责还原历史 `AIMessage(tool_calls)` + `ToolMessage` |
| `DeepSeekCacheBoundaryMiddleware` | 可接入 DeepAgents，需验证 `system_prompt` / `request.system_message` 兼容性 |
| `TailTrimMiddleware` | 可接入 DeepAgents |
| `ToolResultClearMiddleware` | 可接入 DeepAgents，但 SSE adapter 必须透传 `custom` 事件并同步 `summary_source` |
| PuddingClaw `SummarizationMiddleware` | 第一版不额外挂，避免和 DeepAgents 内置 summarization 重复 |
| `CompactionMiddleware` | 第二阶段再接；接入时必须继续归档并写回 Claw JSON |
| `SkillsRouterMiddleware` | Agent 模式第一版不启用，避免和 DeepAgents skills 机制冲突 |
| `TaskStateMiddleware` | Agent 模式禁用，改用 DeepAgents `write_todos` |
| `_summarize_tool_result()` | 保留在 SSE adapter/tool_end 处理链路中，继续写 `summary_source="single_tool_overflow"` |

DeepAgents 内置 summarization 与 PuddingClaw summarization 不是同一个实现，但功能重叠。第一版优先使用 DeepAgents 内置 summarization；如果发现它破坏 Claw JSON 透明历史，再通过 profile 排除 `"SummarizationMiddleware"`，改用 PuddingClaw 自有透明压缩链路。

### 3.9 配置与启动

在 `backend/config.py` 的 `_DEFAULT_CONFIG` 或 `update_settings` 里增加开关：

```json
{
  "agent_runtime": "deepagents"
}
```

但这个开关主要影响默认模式、灰度和兜底。因为前端已经有模式切换，后端可以直接根据 URL 路由选择运行时：

- `/api/chat` → LangChain AgentManager
- `/api/agent` → DeepAgentsAgentManager

建议规则：

- `runtimeMode` 是前端用户态选择。
- `/api/chat` 和 `/api/agent` 是后端硬路由。
- `agent_runtime` 不决定单次请求走哪个 runtime，只用于默认首页模式、灰度开关、是否暴露 Agent 模式、测试环境强制关闭 DeepAgents。

## 4. 项目选择机制

### 4.1 项目 = 文件夹工作区

Agent 模式下的"项目"不是任务系统，而是用户选择的本地文件夹。目录选择必须是用户前端发起，而不是要求用户去后台配置。

推荐流程：

```text
前端选择项目目录
  ↓
POST /api/projects/register
  { path: "/Users/pet/Code/xxx" }
  ↓
后端校验 path 存在、是目录、可读写、resolve 后合法
  ↓
返回 project_id
  ↓
前端保存 project_id + display_name
  ↓
后续 /api/agent 只传 project_id，不直接传 path
```

后端安全规则：

- 后端维护 `project_id -> project_path` 注册表。
- `/api/agent` 只接受已登记的 `project_id`，不直接信任任意 `project_path`。
- path 必须 `resolve()` 后校验存在、是目录、可访问。
- DeepAgents `FilesystemBackend` 的 root 只能设置为该项目目录。
- session 中保存 `project_id` 与 `project_path` 作为透明记录，但权限判断以注册表解析结果为准。
- session 中保存 `allowed_external_paths`，记录用户对项目外文件的授权：
  - `path`
  - `access: "read"`
  - `granted_at`
  - `source: "user_confirmed"`
  - `scope: "session"`
- 已授权外部路径不进入 DeepAgents `FilesystemBackend`；它们由 PuddingClaw 文件工具按授权表读取，保持 DeepAgents 项目沙箱边界清晰。

浏览器形态说明：

- 如果是 Electron/Tauri/本地壳，可直接通过系统目录选择器拿到路径。
- 如果是普通浏览器，需要后端目录浏览 API、手动输入路径或 File System Access API 适配。
- 不论 UI 形态如何，最终都走后端登记接口换取 `project_id`。

### 4.2 项目下的 Session

Agent 模式下，Sidebar 显示：

```
项目
├── knowledge/                ← 项目文件夹
│   └── 开发GitHub监控Skill    ← 属于该项目的 session
├── another-project/
│   └── 调试子代理
```

- 每个 Agent session 关联一个 `project_id`
- 每个 Agent session 同时记录 `project_id` 与 `project_path`
- 切换 session 时，`project_id` 同步切换
- 后端根据 `project_id` 解析项目目录，并为该请求构建 DeepAgents backend

### 4.3 Chat 模式无项目

Chat 模式下：

- 不显示"项目"区块
- session 不关联任何 `project_path`
- 后端 `AgentManager` 使用默认 `base_dir`，不执行文件写入类工具

## 5. 会话与状态

### 5.1 Session 共享

两种模式可以共用 `session_manager` 的会话存储，但消息历史格式需要保持一致。

- Chat 模式：使用当前 `session_manager` 格式
- Agent 模式：DeepAgents 执行后的最终 messages 需要转换回当前格式再保存
- Agent session 元数据必须写入 `runtime_mode: "agent"`、`project_id`、`project_path`
- Chat session 元数据写入 `runtime_mode: "chat"`，不关联项目

### 5.2 历史消息转换

DeepAgents 执行后的 state 里 `messages` 是 LangChain Message 对象列表。需要写转换函数：

```python
def deepagents_messages_to_session_messages(messages) -> list[dict]:
    """把 DeepAgents state.messages 转成 session_manager 可保存的格式。"""
    ...
```

转换要求：

- 保留 `AIMessage.tool_calls` 与对应 `ToolMessage` 的配对关系。
- 工具结果写入 Claw 现有 `tool_calls[].output`。
- 若工具结果被摘要，继续写 `summary_source`。
- 前端展示仍通过 `display_messages` 或 archive 合并保持完整透明。
- 刷新页面、多轮对话、导出历史均不依赖 checkpoint。

## 6. 实施步骤

### 6.0 实施进度台账

| 阶段 | 状态 | 完成记录 |
|---|---|---|
| Phase 0：方案固化 | Done | 2026-06-26：确认项目 Sidebar 规则、无项目 Agent session 放入普通"对话"列表、后端使用隐式 session workspace |
| Phase 1A：后端 `/api/agent` 最小 PoC | Done | 2026-06-26：已实现 project registry、session runtime metadata、DeepAgentsManager 最小骨架、/api/agent 路由；Chat session 默认/兜底写入 `runtime_mode=chat`，Agent session 写入 `runtime_mode=agent`；本地启动脚本改为 `uv sync`，避免旧 `requirements.txt` 缺少 DeepAgents；`py_compile`、ModelClient/DeepAgents 29 项测试、project registry smoke 通过 |
| Phase 1B：前端切换 UI + 项目选择 | Done | 2026-06-26：已接入 runtimeMode、streamAgent、projects API、Next /api/agent SSE 代理、Sidebar Agent/Chat 切换、项目列表、手动路径登记、项目 session 分组、无项目 Agent session 归入普通"对话"；补充 `baoyu-design` 输入区项目选择参考稿 `designs/agent-input-project-picker/index.html`，并在 ChatInput 中加入 Agent 模式项目胶囊、项目菜单、添加本地文件夹入口、Agent/Chat 输入区切换；项目行增加更多菜单，可按系统显示"访达/资源管理器/文件管理器"并打开已登记项目目录；`npm run build` 通过 |
| Phase 1C：本地开发热重载稳定性 | Done | 2026-06-26：定位 `_next/static/* 404` 根因是 `next dev` 与 `next build` 共用 `.next`，build 会覆盖 dev chunks；已改为 dev 使用 `.next`，build/start 使用 `.next-build`，启动脚本只清理 dev 缓存，避免验证构建打断热重载 |
| Phase 1D：DeepAgents 运行时透明事件 | Done | 2026-06-26：参考 Chat `AgentManager._run_agent_stream`，Agent 模式从 DeepAgents `updates.model.messages[].tool_calls` emit `tool_start`，从 `updates.tools.messages[]` emit `tool_end`，工具完成后续文本 emit `new_response`；`custom` middleware 事件按原类型透传；tool_calls/output 写入 `session.json`；补齐 `source_found`、`citations_finalized`、首轮自动标题事件；DeepAgents 工具结果复用 `tool_result_adapter`，结构化 `puddingclaw_tool_result` 会拆分为展示文本和 sources，并通过 `format_sources_for_model` 提醒模型使用 `[^source_id]` 标注引用；新增 `tests/test_deepagents_manager.py` 验证事件、引用、标题与持久化 |
| Phase 2：DeepAgents 工具 / skills / backend 对齐 | In Progress | 2026-06-26：Agent 模式已过滤 PuddingClaw 重叠工具，保留 `terminal/fetch_url/tavily_search/search_knowledge_base`；`create_deep_agent` 已显式传入 `skills=["/skills/"]`，并通过 backend `/skills/` route 指向全局 skills；terminal 工作目录跟随当前 project/unscoped workspace，并把 `/skills` 虚拟路径映射到后端真实 skills 目录，支持 DeepAgents skills 读取说明后直接通过 terminal 跑技能脚本 |
| Phase 3：SSE、Context Engineering 与 checkpoint 对齐 | Pending | 待 DeepAgents event adapter 稳定后接入 |
| Phase 4：会话持久化与 history 转换 | Pending | 待 Agent session 元数据骨架落地后完善 |
| Phase 5：生产化 | Pending | 可选增强 |

### Phase 1A：后端 `/api/agent` 最小 PoC（1～2 天）

1. 新增 `backend/graph/deepagents_manager.py`
2. 新增 `backend/api/agent.py` 路由，接收 `project_id`
3. 新增项目注册/解析能力，先支持本地路径登记
4. 使用 `ModelClientChatModel` + Higress + DeepAgents 跑通最小流式对话
5. 验证 DeepAgents filesystem backend 指向登记后的项目目录

### Phase 1B：前端切换 UI + 项目选择（1 天）

设计约束：

- 正式细化 Agent/Chat 切换、项目 Sidebar、项目选择、项目外文件授权弹窗等 UI 前，先使用 `baoyu-design` 产出自包含 HTML 设计参考。
- 设计产物默认放在 `designs/agent-input-project-picker/`，再按确认后的视觉与交互实现到前端。
- 未选择项目的 Agent session 不显示"默认工作区"，仍归入普通"对话"列表；设计稿必须体现这一点。

1. Store 增加 `runtimeMode` 和 `currentProjectPath`
2. Sidebar 增加 `SegmentedTabs`，Chat 模式隐藏"项目"
3. Agent 模式下显示项目列表，每个项目下显示关联 session
4. 增加"选择项目文件夹"入口，通过 `/api/projects/register` 换取 `project_id`
5. `sendMessage` 根据 mode 调用不同 API，Agent 模式携带 `project_id`
6. 新增 `streamAgent` 和 `/api/agent/route.ts`
7. ChatInput 在 Agent 模式显示当前项目上下文：
   - 已选项目：显示项目名，发送时沿用 `currentProjectId`。
   - 未选项目：显示"进入项目工作"，发送时走无项目 Agent session / 隐式 workspace。
   - 菜单支持选择已有项目、使用现有文件夹登记项目、不使用项目。
   - Chat 模式隐藏项目入口，避免把纯聊天误绑定到项目。
8. Sidebar 项目行提供系统文件管理器入口：
   - macOS 显示"在“访达”中打开"。
   - Windows 显示"在“资源管理器”中打开"。
   - Linux / 其他桌面显示"在“文件管理器”中打开"。
   - 后端只接收 `project_id`，通过项目注册表解析真实路径后调用系统打开命令，不接受前端任意 path。

### Phase 2：DeepAgents 工具 / skills / backend 对齐（2～3 天）

1. 使用 DeepAgents `FilesystemBackend(root_dir=project_path, virtual_mode=True)`
2. 配置 `/skills/` 路由，让 DeepAgents skills 能读取 PuddingClaw skills 目录
   - `create_deep_agent(skills=["/skills/"], backend=CompositeBackend(... routes={"/skills/": ...}))`
3. 过滤 PuddingClaw 重叠工具：`read_file` / `write_file` / `task_manager` / `execute_skill`
4. 保留 PuddingClaw 非冲突工具：`terminal` / search / knowledge / MCP
   - `terminal` cwd = 当前项目目录或无项目 Agent session workspace。
   - `terminal` 将 `/skills` 映射到后端真实 skills 目录，因此 DeepAgents 读到的技能脚本路径可以直接用于执行。
   - `execute_skill` 不挂载到 Agent 模式，避免与 DeepAgents skills 双轨执行。
5. 新增项目外文件授权读取：绝对路径不在 `project_path` 时触发用户授权，确认后由 PuddingClaw 文件工具只读读取
6. 跑通一次 skills 执行、一次项目内文件读写、一次项目外文件授权读取、一次 `write_todos`、一次 `task`

已完成的透明事件规则：

- DeepAgents 文件工具、`write_todos`、`task`、skills 读取等只要以 LangGraph tool call / ToolMessage 形式出现，都会进入前端现有工具卡片。
- `tool_start` 的 `input` 来自 `AIMessage.tool_calls[].args`。
- `tool_end` 的 `output` 来自 `ToolMessage.content`，并写入 Claw `session.json`。
- `tool_end` 前复用 Chat 链路的 `tool_result_adapter`：结构化工具结果中的 `answer_context` 作为工具展示输出，`sources` 通过 `source_found` 事件暴露给前端，并保存到 assistant message。
- 有来源时，写回给 DeepAgents 模型的 ToolMessage 使用 `format_sources_for_model` 附加可引用来源目录，要求模型在回答中使用 `[^source_id]` 标识。
- `done` 前发送 `citations_finalized`，并将 `finalize_citations(content, sources)` 的结果保存到 `session.json`。
- DeepAgents `custom` middleware 事件按 `type` 透传；后续如需更细 UI，可在前端增加专门的 middleware/skill event 展示。

### Phase 3：SSE、Context Engineering 与 checkpoint 对齐（2～3 天）

1. 把 DeepAgents 自定义事件（todos、backend、HITL）转成 SSE
2. 接入 `_maybe_middle_trim_session()` 与 `_build_messages()`
3. 接入 `ToolResultClearMiddleware`、`TailTrimMiddleware`、`DeepSeekCacheBoundaryMiddleware`
4. 保留 `_summarize_tool_result()` 的单条超长工具结果摘要
5. 如启用 checkpoint，使用 `thread_id=session_id` 并验证 resume 后不会重复写历史

### Phase 4：会话持久化与 history 转换（1～2 天）

1. session 元数据增加 `runtime_mode`、`project_id`、`project_path`
2. session 元数据增加 `allowed_external_paths`，用于透明记录项目外文件读取授权
3. Agent 执行完成后保存消息历史到对应项目 session
4. 多轮对话测试
5. 异常中断时保存部分对话
6. HITL / checkpoint resume 完成后同步最终状态到 Claw JSON

### Phase 5：生产化（可选）

1. 后端配置开关
2. 默认模式选择
3. 根据用户是否选择项目自动进入 Agent 模式

## 7. 风险与注意事项

| 风险 | 说明 | 缓解 |
|---|---|---|
| SSE 事件协议不一致 | DeepAgents graph event 和当前自定义事件格式不同 | 写统一 adapter，保持前端协议不变 |
| 工具重名冲突 | DeepAgents 默认工具与 PuddingClaw 工具可能同名 | Agent 模式下显式过滤/选择工具 |
| Session 历史格式不兼容 | DeepAgents 的 messages 对象需要转换 | 写转换函数并测试多轮 |
| Middleware 重复 | summarization/todo/filesystem 两边都有 | Agent 模式优先 DeepAgents skills/fs/todo/task；PuddingClaw 只保留透明上下文工程必要层 |
| checkpoint 不透明 | DeepAgents checkpoint 晦涩，不能作为用户事实源 | Claw JSON 是事实源，checkpoint 只做 runtime 恢复态 |
| 项目路径安全 | 前端直接传 path 容易越权或误操作 | 前端登记 path，后端返回 project_id；/api/agent 只接受 project_id |
| DeepAgents skills 访问不到全局 skills | 项目 backend 只指向 project_path | 使用 CompositeBackend 路由 `/skills/` 到全局 skills 目录 |
| 终端执行能力缺失 | DeepAgents `execute` 依赖 sandbox backend | 第一版保留 PuddingClaw `terminal`；后续提供 Docker 沙箱模式接管 execute |
| Docker 门槛过高 | 本地客户端用户不一定安装 Docker 或资源有限 | 默认轻量模式，Docker 作为增强隔离选项 |
| 性能与缓存 | DeepAgents agent 编译和当前 AgentManager 缓存策略不同 | DeepAgentsAgentManager 内部也可做缓存 |
| 依赖升级漂移 | DeepAgents / LangChain 版本更新较快，运行时 API 可能漂移 | `deepagents` 已进入主依赖；升级后跑 DeepAgents 集成测试与最小 smoke |

## 8. 最小可验证目标（MVP）

完成 Phase 1 + Phase 2 后，应能验证：

1. 前端 Sidebar 可以切换 Agent / Chat
2. Chat 模式：对话正常，Sidebar 不显示"项目"
3. Agent 模式：选择一个本地文件夹并登记为项目，输入消息走 `/api/agent`
4. DeepAgents 调用 `ModelClientChatModel` + Higress，返回流式文本
5. Agent 模式下触发一次 `write_todos`、一次 `read_file`、一次 DeepAgents skill，前端能看到 `tool_start` / `tool_end`
6. 多轮 Agent 对话后刷新页面，历史仍从 Claw JSON 恢复
7. 工具调用失败时，前端能收到 `tool_end is_error` 或 `error`，session 不损坏
8. 同一个 Agent session 切回后，`project_id/project_path` 不丢，后续文件操作仍在同一项目目录

## 9. 相关文件

- `frontend/src/components/layout/Sidebar.tsx`
- `frontend/src/lib/store.tsx`
- `frontend/src/lib/api.ts`
- `frontend/src/app/api/chat/route.ts`
- `frontend/src/app/api/agent/route.ts`（新增）
- `backend/api/projects.py`（新增，项目登记/解析）
- `backend/graph/agent.py`（保留为 Chat 运行时）
- `backend/graph/deepagents_manager.py`（新增）
- `backend/api/chat.py`（保留）
- `backend/api/agent.py`（新增或扩展 chat.py）
- `backend/graph/session_manager.py`（需要支持 `runtime_mode` / `project_id` / `project_path` 字段）
- `backend/config.py`（可选增加配置开关）
- `docs/notebook-modelclient-deepagents-integration.md`（已验证的模型接入方式）

## 10. 后端开发计划

实施原则：

- 保留现有 `/api/chat`、`backend/graph/agent.py::AgentManager`、LangChain `create_agent` 能力不动。
- 所有 DeepAgents 能力走新增 `/api/agent` 旁路运行时。
- Claw JSON 始终是产品事实源，DeepAgents checkpoint 只做 runtime 恢复态。

### Phase 0：收口现有小修

状态：已完成。

- 修复 `AgentManager` 配置刷新签名，不再读取 legacy `config["llm"]`。
- 新签名基于 `ai_gateway`、`gateway_llm`、`fallback_llm`。
- 相关测试通过。

验收：

- gateway / fallback / model / key / base_url 配置变化会触发现有 AgentManager 重建。
- `/api/chat` 继续走现有 LangChain `create_agent` 链路。

### Phase 1：项目注册与安全边界

新增：

- `backend/api/projects.py`
- `backend/project_registry.py` 或 `backend/graph/project_registry.py`

接口：

- `POST /api/projects/register`
- `GET /api/projects`
- `POST /api/projects/{project_id}/open`
- `DELETE /api/projects/{project_id}`

能力：

- 前端传本地目录 path。
- 后端校验 path 存在、是目录、可访问。
- 生成 `project_id`。
- 后续 `/api/agent` 只接收 `project_id`，不直接信任 `project_path`。
- 本地保存 `project_id -> project_path`，第一版可用 JSON 文件。

验收：

- 能注册项目目录。
- 非法目录被拒绝。
- `/api/agent` 无法直接越权访问未登记路径。

### Phase 2：DeepAgentsAgentManager 最小链路

新增：

- `backend/graph/deepagents_manager.py`
- `backend/api/agent.py`

能力：

- 使用 `ModelClientChatModel(role="agent", streaming=True)`。
- 使用 `project_id` 解析项目目录。
- 使用 DeepAgents `create_deep_agent`。
- 先跑通最小流式响应。
- 新增 `/api/agent`，不影响 `/api/chat`。

验收：

- `/api/chat` 原样可用。
- `/api/agent` 能通过 Higress 调模型。
- DeepAgents stream 能输出 `token` / `done` / `error`。

### Phase 3：DeepAgents backend 与文件系统

接入：

- `FilesystemBackend(root_dir=project_path, virtual_mode=True)`
- `CompositeBackend`
- `/skills/` 路由到 PuddingClaw 全局 skills 目录

能力：

- 用户项目目录作为默认文件工作区。
- DeepAgents 文件工具接管：
  - `ls`
  - `read_file`
  - `write_file`
  - `edit_file`
  - `glob`
  - `grep`

验收：

- `read_file` 只能读项目目录内文件。
- `write_file` / `edit_file` 只能写项目目录内文件。
- 路径穿越被拦截。
- `/skills/` 可读。

### Phase 4：工具过滤与能力归属

实现工具过滤。

Agent 模式禁用 PuddingClaw：

- `read_file`
- `write_file`
- `task_manager`
- `execute_skill`

Agent 模式保留 PuddingClaw：

- `terminal`
- `tavily_search`
- `fetch_url`
- `search_knowledge_base`
- MCP tools
- memory tools 暂定可配置

DeepAgents 接管：

- skills
- filesystem
- `write_todos`
- `task`

验收：

- 工具列表无重名冲突。
- DeepAgents skill 能执行。
- DeepAgents `write_todos` 可用。
- PuddingClaw `terminal` 工作目录受 `project_path` 约束。

### Phase 5：SSE adapter

在 `DeepAgentsAgentManager` 中实现事件转换：

| DeepAgents event | Claw SSE |
|---|---|
| `AIMessageChunk` | `token` |
| `AIMessage.tool_calls` | `tool_start` |
| `ToolMessage` | `tool_end` |
| custom event | `context_maintenance` / `tool_result_clear` / `compaction` |
| exception | `error` |
| final | `done` |

验收：

- 前端无需大改即可消费 Agent 模式事件。
- 工具开始、结束、错误都能显示。
- 中断/异常不会破坏 session。

### Phase 6：Claw JSON 会话持久化

修改：

- `backend/graph/session_manager.py`

新增字段：

```json
{
  "runtime_mode": "agent",
  "project_id": "...",
  "project_path": "..."
}
```

新增转换：

- `deepagents_messages_to_session_messages(...)`

规则：

- Claw JSON 是事实源。
- DeepAgents checkpoint 不能作为唯一历史来源。
- 正常完成后同步 messages / tool_calls 到 `session.json`。
- 前端历史仍读 `session.json` / `display_messages` / `archive`。

验收：

- Agent 多轮对话刷新后可恢复。
- `project_id` / `project_path` 不丢。
- tool_calls 和 ToolMessage 输出能正确还原。
- `/api/chat` 旧 session 兼容。

### Phase 7：Context Engineering 接入

第一批接入：

- `_maybe_middle_trim_session()`
- `_build_messages()` 抽公共函数或复用
- `ToolResultClearMiddleware`
- `TailTrimMiddleware`
- `DeepSeekCacheBoundaryMiddleware`
- `_summarize_tool_result()`

暂缓：

- PuddingClaw `SummarizationMiddleware`
- `CompactionMiddleware`
- `SkillsRouterMiddleware`
- `TaskStateMiddleware`

验收：

- 长工具结果仍有 `single_tool_overflow`。
- 历史工具结果摘要仍写 `summary_source`。
- 当前轮 tool output 不被提前摘要。
- DeepAgents 内置 summarization 不破坏 Claw 透明历史。

### Phase 8：checkpoint / HITL resume

策略：

- `thread_id = session_id`
- checkpoint 只做 runtime 恢复态。
- Claw JSON 仍是事实源。

能力：

- HITL 中断保存 checkpoint。
- resume 从 checkpoint 恢复。
- 完成后同步最终结果到 Claw JSON。

验收：

- resume 不重复写历史。
- checkpoint 可清理。
- 前端历史不直接依赖 checkpoint。

### Phase 9：DockerSandboxBackend 增强模式

状态：增强能力，不阻塞 MVP。

新增：

- `backend/graph/docker_sandbox_backend.py`
- `backend/graph/sandbox_manager.py`

能力：

- 实现 DeepAgents `BaseSandbox` 子类。
- 支持 `execute()` 与 `upload_files()`。
- 每个 `project_id` 默认复用一个容器。
- 项目目录挂载到 `/workspace`。
- skills 目录只读挂载到 `/skills`。
- 支持用户配置 CPU、内存、网络、容器复用策略。

验收：

- Docker 模式下 DeepAgents `execute` 可用。
- 文件操作与命令执行都发生在同一个容器视图里。
- 不能写 `/skills`。
- 不继承宿主敏感环境变量。
- timeout、最大输出、资源限制生效。

### Phase 10：依赖与部署

处理：

- `deepagents>=0.6.12` 已进入 backend 主依赖。
- `deepagents-test` 仅保留 Notebook / 集成测试辅助依赖。
- `/api/agent` 在 DeepAgents 未安装时返回明确错误，不影响 `/api/chat`。
- Docker 沙箱镜像作为可选增强依赖，不阻塞轻量模式。

验收：

- Docker / backend 启动正常。
- `/api/chat` 无 DeepAgents 也可运行。
- 启用 Agent 模式的环境能正常 import DeepAgents。
- 未安装 Docker 时轻量模式仍可运行。

### Phase 11：测试矩阵

至少补这些测试：

- 项目注册路径校验。
- `/api/chat` 回归。
- `/api/agent` 最小流式。
- 工具过滤无冲突。
- DeepAgents filesystem 限制在项目目录。
- DeepAgents skills 可读取 `/skills/`。
- Agent session JSON 保存与恢复。
- `tool_start` / `tool_end` / `error` SSE。
- 长工具输出摘要。
- checkpoint resume 不重复写历史。
- 三档沙箱模式工具暴露符合预期。
- Docker 模式下 execute、timeout、资源限制、只读 `/skills` 生效。
