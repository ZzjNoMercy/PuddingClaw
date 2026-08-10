# Spawn + Kernel 双执行模式重构方案

状态：设计方案，待实施

日期：2026-08-11

范围：只移除 PuddingClaw 自己的 Docker 沙箱与 Docker 运行时选择；不移除项目、工具或第三方服务对 Docker 的正常依赖。

## 1. 决策摘要

PuddingClaw 的 Shell 执行只保留两种用户可选模式：

| 配置值 | 用户界面名称 | 默认值 | 隔离含义 |
| --- | --- | --- | --- |
| `spawn` | 宿主执行 | 是 | 直接创建宿主进程；有 Tool Gate、权限询问、超时和审计，但没有 OS 文件或网络边界 |
| `kernel` | 内核沙箱 | 否 | 使用当前操作系统的进程级内核隔离；不可用时必须询问用户是否仅本次 Run 回退到 `spawn` |

`spwan` 仅视为讨论中的拼写错误，配置、API、日志和代码统一使用 `spawn`。

以下模式和开关从新配置中删除：

- `auto`
- `docker`
- `docker_enabled`
- `on_unavailable`
- Skill 的 `host | docker` runtime 选择

无论选择 `spawn` 还是 `kernel`，都必须向 DeepAgents 提供一个实现 `SandboxBackendProtocol` 的 execution backend。不能在 `spawn` 模式下退化为普通 `FilesystemBackend`，否则 DeepAgents 会动态移除 `execute` 工具。

内核沙箱不可用时不允许静默降级。推荐在第一次真正需要执行命令时发出 Run 级 HITL 请求：

- 用户批准：只对当前 Run 持久化 `spawn` 覆盖，重新解析环境并签发 runner 为 `spawn` 的新 permit。
- 用户拒绝：该命令返回结构化错误，Run 可继续使用不依赖 Shell 的工具。
- 无交互/Headless：没有显式预授权时失败关闭，不自动回退。

## 2. 第一性原理

### 2.1 执行能力不等于沙箱

Agent 是否拥有 `execute`，取决于 backend 是否实现 DeepAgents 的执行协议；命令是否受 OS 隔离，则取决于实际 runner。

因此目标结构不是“开沙箱才挂 execute”，而是：

```text
DeepAgents execute
        |
        v
Tool Gate / Permissions
        |
        v
ResolvedExecution（同一份命令、环境、目录和运行时绑定）
        |
        +--> SpawnRunner
        |
        `--> KernelRunner
```

两种模式具有相同的上层工具和运行时语义，只在最后的进程创建边界不同。

### 2.2 授权与隔离是两条独立控制面

- Tool Gate 回答“用户是否允许做这件事”。
- Kernel sandbox 回答“进程在操作系统层面实际能碰到什么”。
- `spawn` 中的目录授权是产品策略和审计，不是安全边界。
- `kernel` 中的目录授权必须被编译成 OS 可执行的 Read/Write/Deny 规则。

权限批准不能代替沙箱，沙箱也不能代替用户授权。

### 2.3 禁止静默降低安全级别

`kernel -> spawn` 会扩大进程权限，因此必须由用户明确批准。配置默认值、异常捕获、能力路由或 Skill 都不能隐式触发该变化。

批准必须是服务端持久化的 authority，不能仅信任 LangGraph resume payload 或前端传回的布尔值。

### 2.4 运行时解析与 runner 正交

先解析：

- 原始命令与最终 argv
- cwd
- 精确环境变量
- Python/Node/CLI 的绝对路径与 ABI
- Skill runtime owner
- 读写目录
- 网络、超时、输出和进程限制

再决定由 `SpawnRunner` 还是 `KernelRunner` 创建进程。不得根据命令字符串里是否出现 `/skills/...`、Chromium 路径或某个 CLI 名称来临时切 runner。

### 2.5 安装 ABI 必须等于执行 ABI

依赖安装和后续执行必须使用同一个宿主运行时身份。Python 环境不能因 Node 版本变化而失效，Node 环境也不能绑定 Python 版本或 Docker 镜像摘要。

每个生态分别计算 runtime identity，例如：

```text
python = os + arch + interpreter_realpath + version + abi + dependency_lock_digest
node   = os + arch + node_realpath + version + abi + dependency_lock_digest
cli    = executable_digest + adapter_revision + credential_profile_revision
```

### 2.6 能力必须来自真实探测

不能因操作系统名称宣称沙箱可用。每个 Kernel runner 必须做 allow/deny 自检，证明允许路径可访问、禁止路径不可访问，失败即视为不可用。

## 3. 当前源码事实

### 3.1 配置和模式已经发生语义混杂

- `backend/config.py` 和 `backend/config.json` 默认使用 `sandbox_mode=auto`，并保留 `docker_enabled`、`on_unavailable` 和整组 Docker 配置。
- `frontend/src/lib/settingsApi.ts` 与 `frontend/src/app/settings/page.tsx` 暴露 `auto | kernel | docker`，设置页仍包含镜像、CPU、内存、进程数和 Docker 探测。
- `backend/harness/workspace_backends.py::build_workspace_execution_backend` 同时负责内核探测、Docker 探测、自动回退和受限宿主回退，构造阶段已隐含安全策略。

### 3.2 DeepAgents 的 execute 不依赖 Docker，但依赖执行协议

- `RestrictedHostWorkspaceBackend`、`KernelWorkspaceBackend`、`DockerWorkspaceBackend` 和 `AdaptiveWorkspaceBackend` 都继承 `SandboxBackendProtocol`。
- DeepAgents 的 `FilesystemMiddleware` 会检查 backend 是否支持执行；如果 backend 只是普通文件 backend，它会在模型调用前过滤 `execute`。
- 当前 `RestrictedHostWorkspaceBackend.execute()` 已经通过 `subprocess.run` 在宿主执行，说明宿主 `spawn` 完全可以保留 DeepAgents 原生 `execute`。
- `PermissionedCompositeBackend` 的 default backend 必须继续是可执行 backend；只给其中某条文件 route 加执行能力不够。

结论：`spawn` 应是正式 execution backend，而不是“没有 sandbox backend”。

### 3.3 当前内核实现只有 macOS

- `backend/harness/kernel_sandbox.py` 只有 `MacOSSeatbeltRunner`。
- 它使用 `/usr/bin/sandbox-exec`，有真实 allow/deny probe、隔离 HOME/TMP、超时、输出截断和进程组终止。
- `SandboxGrantProfile` 已具备 runner-neutral 雏形，但当前会默认把 workspace 和 scratch 同时设为可读写，尚没有有优先级的 Deny 规则。
- Linux 和 Windows 目前没有 Kernel runner；在实现前必须报告 `unimplemented`，并走同一套用户确认回退流程。

### 3.4 Docker 渗透面不只在 WorkspaceBackend

需要迁移的 Docker 沙箱耦合包括：

- `backend/harness/workspace_backends.py`
  - `ProjectSandboxManager`
  - `DockerWorkspaceBackend`
  - `AdaptiveWorkspaceBackend`
  - Chromium HTML E2E 的 Docker 特判
  - `execute_external_directory` 的 Docker 委托
  - 托管 Provider CLI、浏览器授权 CLI 的 Docker 方法
- `backend/harness/tool_execution.py`
  - `auto/adaptive/docker/restricted_host` 分支
  - permit 中的 `docker` runner
  - 通过命令字符串推断 Docker Skill 和 Chromium runner
- `backend/graph/deepagents_manager.py`
  - 将 Skill、scratch 组织为 Docker mount envelope
  - run inventory 中的 fallback/dependency plan
- `backend/runtime_identity/*` 与 `backend/harness/host_skill_runtime.py`
  - `runtime_image_digest` 是大量 manifest、plan 和 cache 校验的核心字段
  - Skill runtime 可绑定 `host | docker`
- `backend/tools/request_skill_runtime_tool.py`
  - 用户显式选择 Docker Skill runtime
- `backend/api/connectors.py`
  - Connector catalog 和 Lark 等托管 CLI 通过 `ProjectSandboxManager` 获取 runtime contract
  - 授权入口明确要求 Docker runtime
- `backend/tools/filesystem/leases.py`、`validation.py`
  - 独立外部目录执行和 Chromium 验证仍假定 Docker 能力

因此不能先删除 Docker class 再逐个修错。必须先建立 runner-neutral execution contract，并将以上能力逐项迁移。

## 4. 目标领域模型

### 4.1 配置模型

```python
ExecutionMode = Literal["spawn", "kernel"]

class TerminalConfig:
    execution_mode: ExecutionMode = "spawn"
    default_timeout_seconds: int = 120
    external_directory_writable_enabled: bool = False
```

建议将字段命名为 `execution_mode`，不用 `sandbox_mode`。因为 `spawn` 不是沙箱，把它放进 `sandbox_mode` 会持续误导 UI、日志和策略代码。

运行时还需要区分：

```python
configured_mode: Literal["spawn", "kernel"]
effective_runner: Literal[
    "spawn",
    "kernel_macos_seatbelt",
    "kernel_linux_bwrap_seccomp",
    "kernel_windows_restricted_token",
]
```

`configured_mode` 表达用户偏好；`effective_runner` 是当前 Run 经探测和授权后的真实执行器。日志和 Run inventory 必须同时记录，不能只记录模糊的 `mode`。

### 4.2 统一执行请求

新增不可变的 `ResolvedExecution`，在 Tool Gate 之后、runner 之前生成：

```python
@dataclass(frozen=True)
class ResolvedExecution:
    tool_call_id: str
    original_command: str
    argv: tuple[str, ...]
    cwd: Path
    environment: tuple[tuple[str, str], ...]
    secret_values: tuple[str, ...]
    runtime_binding: RuntimeBinding
    filesystem_profile: FilesystemAuthority
    network_allowed: bool
    timeout_seconds: int
    max_output_bytes: int
    max_processes: int
```

约束：

- runner 不再改写 Skill runtime、PATH 或 interpreter。
- `/workspace`、`/scratch`、`/skills` 等虚拟路径在解析阶段统一映射。
- 环境快照和目录 profile 都参与 digest；spawn 前重新验证。
- 命令、权限 revision、runtime binding、profile 和 runner 任一变化都使 permit 失效。

### 4.3 Runner 接口

```python
class ExecutionRunner(Protocol):
    id: str
    def probe(self) -> RunnerAvailability: ...
    def execute(
        self,
        resolved: ResolvedExecution,
        permit: ExecutionPermit,
    ) -> ExecuteResponse: ...
```

建议只保留一个组合式 `ExecutionWorkspaceBackend(FilesystemBackend, SandboxBackendProtocol)`：

- 对 DeepAgents 始终表现为可执行 backend。
- 内部持有 `ExecutionRunner`。
- 文件操作和命令执行不因 mode 改变工具集合。
- `spawn` 与 `kernel` 共用路径映射、环境解析、Skill runtime、超时和输出逻辑。

如果为了渐进迁移暂时保留两个 backend class，也必须让它们只做 runner 适配，不能复制运行时解析逻辑。

### 4.4 SpawnRunner

`SpawnRunner` 取代语义模糊的 `RestrictedHostWorkspaceBackend`：

- 使用宿主 cwd、PATH、HOME 和本机工具链，满足“环境基本都在宿主”的目标。
- 保留进程组、超时、输出上限、stdin 关闭和审计。
- 从 Backend 自身进程环境中剔除 PuddingClaw 的内部服务密钥；Skill/Connector secret 只在对应 typed execution 中注入。
- 明确不宣称文件、网络或子进程隔离。命令拥有当前桌面用户可获得的 OS 权限。
- 目录 permission 仍用于用户确认、策略和审计，但 UI 必须标注“宿主模式无法由 OS 强制限制”。

### 4.5 KernelRunner

目标平台适配：

| 平台 | 目标 runner | 最低边界 |
| --- | --- | --- |
| macOS | `MacOSSeatbeltRunner` | Seatbelt deny-by-default、隔离 HOME/TMP、Read/Write/Deny、网络开关、进程组 |
| Linux | `LinuxBwrapSeccompRunner` | mount/user/pid namespace、只读/可写 bind、seccomp、no-new-privileges；可用时叠加 Landlock |
| Windows | `WindowsRestrictedTokenRunner` | Restricted Token、低权限 SID/完整性级别、Job Object、显式可访问目录和句柄控制 |

平台 runner 只有在真实 allow/deny 自检通过后才可用。Linux/Windows 实现完成前，选择 `kernel` 会得到结构化 unavailable reason，而不是假装已隔离。

macOS `sandbox-exec` 已被系统标记为 deprecated，应保留 fail-closed probe，并把未来 API 变化视为正常的 unavailable 情况，而不是捕获异常后直接 spawn。

## 5. 内核不可用时的用户确认协议

### 5.1 触发时机

采用惰性触发：Run 创建时可以探测并记录状态，但只有在第一个未被策略直接 BLOCK 的 Shell/managed CLI/Chromium 执行真正需要创建进程时才询问。

这样不会让只读问答、文件工具或不需要 Shell 的 Run 无故弹窗。

### 5.2 请求结构

新增独立的 `kernel_fallback_request`，不要复用普通目录授权：

```json
{
  "type": "kernel_fallback_request",
  "request": {
    "request_id": "...",
    "session_id": "...",
    "run_id": "...",
    "query_id": "...",
    "tool_call_id": "...",
    "configured_mode": "kernel",
    "fallback_runner": "spawn",
    "platform": "darwin|linux|win32",
    "reason_code": "unimplemented|probe_failed|runner_disappeared",
    "reason": "用户可读且已脱敏的说明",
    "probe_fingerprint": "sha256:...",
    "config_revision": 12
  },
  "decisions": [
    {"type": "fallback_once"},
    {"type": "reject"}
  ]
}
```

### 5.3 状态机

```text
configured=kernel
      |
      v
kernel probe
  |         |
available   unavailable
  |         |
  v         v
kernel    waiting_hitl
              |       |
       fallback_once  reject
              |       |
              v       v
       persist Run   command error
       override
              |
              v
       resolve again
              |
              v
       issue spawn permit
```

服务端持久化 `RunExecutionOverride`：

```python
run_id
configured_mode = "kernel"
effective_runner = "spawn"
scope = "run"
reason_code
probe_fingerprint
config_revision
approved_request_id
approved_at
```

恢复执行时必须重新读取这条记录。仅收到 `{"type":"fallback_once"}` 的 resume payload 而没有持久化 override，视为未批准。

### 5.4 生命周期规则

- 批准只对当前 Run 有效，不跨 Session、Goal 或下一个 Run。
- 同一 Run、同一 probe fingerprint 不重复询问。
- Kernel 曾可用但执行前失效时，旧 permit 作废并重新询问。
- 配置 revision、工作区或 probe fingerprint 改变时，旧批准不可复用。
- 拒绝只阻止需要执行进程的动作，不强制终止整个对话。
- Headless worker 必须显式传入受服务端验证的 run-scoped fallback grant；没有则返回 `waiting_for_user`/`kernel_unavailable`，不能自动 spawn。

### 5.5 Permit 约束

`ExecutionPermit.selected_runner` 必须从笼统字符串升级为稳定 runner id。最重要的规则是：

> Kernel permit 永远不能被 SpawnRunner 消费。

批准回退后必须从 `ResolvedExecution` 重新编译 profile 和 permit，不能修改现有 permit 的 runner 字段后继续执行。

## 6. 目录与权限语义

### 6.1 Runner-neutral 文件权限

将 `SandboxGrantProfile` 升级为有优先级的 `FilesystemAuthority`：

```python
read_roots: tuple[Path, ...]
write_roots: tuple[Path, ...]
delete_roots: tuple[Path, ...]
deny_roots: tuple[Path, ...]
```

优先级固定为：

```text
Deny > Delete > Write > Read > Default deny
```

目录必须 canonicalize，拒绝 symlink root；spawn 前再次检查 inode/file-id 或至少检查 canonical path 未变化。

### 6.2 默认权限

- workspace：默认读写；受保护的控制面子目录由 deny/read-only 规则覆盖。
- scratch：当前 Run/Goal scope 内读写，其他 scope 不可见。
- Skill 源码：只读。
- Skill runtime 环境：只读执行；安装器使用独立、短生命周期的写权限事务。
- 临时 HOME/TMP：kernel 模式下按 Run 独占读写。
- 外部目录：只从已持久化的精确目录 grant 生成规则。
- PuddingClaw 内部 credential、session、runtime registry：默认 Deny；仅 typed service 获得最小路径。

`.git` 不应永久一刀切禁止，否则合法 Git 工作流会失效。建议默认只读，在经过对应 Git mutation 授权后对最小必要路径开放写入。`.puddingclaw`、credential store 和其他控制面路径应始终优先 Deny，只给明确的内部 runtime 子目录例外。

### 6.3 两种模式的诚实表达

| 行为 | `spawn` | `kernel` |
| --- | --- | --- |
| workspace 读写 | OS 用户权限；Tool Gate 约束 | OS profile 强制 |
| 外部目录 grant | 用户授权和审计 | 授权后编译为 OS root |
| 未授权目录 | 策略应拒绝，但不能宣称 OS 隔离 | OS 拒绝 |
| 网络 | 宿主网络；批准是策略边界 | profile 中关闭/开启网络 |
| HOME/TMP | 使用宿主兼容环境 | 隔离到 Run scope |

`execute_external_directory` 不再拥有独立 Docker 运行路径。它可以保留为 typed convenience tool，但最终必须生成同一个 `ResolvedExecution`：

- spawn：在批准的实际目录执行。
- kernel：把该目录加入本次 profile 的精确 Read/Write root。

## 7. Host runtime 与 Skill 重构

### 7.1 删除 Skill 的 Docker runtime 选择

- 删除 `request_skill_runtime` 工具。
- 删除 `SkillRuntimeBindingStore` 中 `host | docker` 选择；如需保留 store，应改为保存明确的宿主 runtime binding id，而不是 runner 类型。
- Skill 不再选择隔离技术。Skill 声明的是所需软件、网络、系统能力和 runtime owner，平台负责解析执行方式。

### 7.2 显式 Skill runtime owner

不能继续通过正则搜索命令中的 `/skills/<id>` 或“当前激活的 Skill 列表”猜测解释器。

为执行请求增加显式的 owner，例如：

```text
runtime_owner = host-default
runtime_owner = skill:pdf@<content-digest>
runtime_owner = managed-cli:lark@<adapter-revision>
runtime_owner = platform:html-validator@<revision>
```

DeepAgents 原生 `execute` 仍保留，但 PuddingClaw 的 execute schema/中间件需要允许携带结构化 `runtime_owner`；普通命令默认 `host-default`。Skill 指令发起的命令必须带自己的 owner，不能靠命令文本推断。

这直接避免以下故障：依赖已安装到 pdf Skill venv，但后续裸 `python3` 因没有命中字符串投影而运行 Homebrew Python。

### 7.3 拆分生态身份

把当前广泛使用的 `runtime_image_digest` 替换为 `RuntimeBinding`：

```python
class RuntimeBinding:
    owner: str
    ecosystem: Literal["system", "python", "node", "managed_cli", "browser"]
    platform: str
    architecture: str
    executable: Path
    executable_version: str
    abi: str
    dependency_revision: str
    environment_revision: str
```

不同生态独立失效：

- Node 24 -> 26 只使 Node binding 失效。
- Python interpreter/ABI 不变时，Python Skill env 继续有效。
- Skill 内容或依赖声明变化，只重建该 Skill 对应生态环境。
- Docker 镜像构建出的旧 artifact 一律不投影到宿主，迁移时在宿主重建。

### 7.4 安装事务

安装器和执行器共享同一个 binding resolver：

1. 解析 owner 和 ecosystem。
2. 获取精确 interpreter/executable。
3. 在 owner 专属目录原子安装。
4. 写 manifest 并切换 `current` 指针。
5. 后续执行只使用 manifest 中的绝对 executable 和环境。

安装不再依赖 Docker runtime contract 或 image digest。

## 8. 现有特殊能力的迁移

### 8.1 Chromium HTML 报告 E2E

删除 `/opt/puddingclaw/bin/validate-html-report-e2e.mjs` 的 Docker 特判，改为 typed `HtmlValidationPlan`：

- 使用宿主托管的 Node + Playwright/Chromium binding。
- 解析 Chromium executable、浏览器 cache、字体、临时目录和报告目录。
- spawn 模式直接运行。
- kernel 模式把 executable/cache/fonts 设为只读，profile HOME/TMP 和输出目录设为可写；按验证需求决定网络是否关闭。
- 浏览器启动失败返回明确 dependency/profile 错误，不能自动换 runner。

验收必须覆盖真实 HTML 生成、Chromium 启动、页面加载、截图/检查和产物读取，不只测命令拼接。

### 8.2 Lark 与其他托管 CLI/Connector

将 `ManagedCliService` 从 `ProjectSandboxManager` 解耦，依赖通用的 `ManagedCommandRunner`：

- adapter 产出 argv、环境、credential view、状态目录、网络和交互需求。
- Host runtime resolver 提供可执行文件。
- runner 只消费已解析 plan。
- spawn 模式使用宿主进程。
- kernel 模式只开放该 connector 的 credential/state 目录及必要网络。
- credential 只注入 typed managed command，不进入普通 Shell 全局环境。

`backend/api/connectors.py` 不再检查 Docker 是否开启，也不再通过 Docker image digest 构造 Connector registry。

### 8.3 公共 HTTPS curl

- ShellPolicyAnalyzer 继续识别网络能力并走用户授权。
- spawn：批准后使用宿主网络，UI 明确没有域名级 OS 限制。
- kernel：批准后开放本次命令的网络能力。
- 如果产品要强制“只访问批准域名”，必须增加受控代理/DNS 层；Seatbelt 或普通 spawn 的布尔网络开关本身做不到域名级限制。

### 8.4 外部目录

`execute_external_directory` 保留用户体验，但删除其 Docker 专用 backend 要求：

- 仍要求精确目录、访问类型和 lease。
- 仍在执行前重新验证 grant revision。
- kernel profile 加入精确目录。
- spawn 直接执行并明确提示是策略授权而非 OS 隔离。
- writable draft 的 stage/commit/rollback 语义保持不变。

### 8.5 用户项目中的 Docker

本方案不移除以下能力：

- 用户项目自己的 Dockerfile/Compose。
- MinerU 等独立服务依赖的 Docker。
- 用户明确要求运行的 Docker CLI。

但在 `kernel` 模式中开放 Docker socket 等于把宿主控制权交给沙箱进程，不能作为普通路径 grant。第一版应拒绝并提示用户切换到 `spawn`；`spawn` 模式下仍按 Tool Gate 策略执行 Docker 命令。

## 9. 配置、数据与 UI 迁移

### 9.1 新安装

- 默认 `execution_mode=spawn`。
- 设置页只显示“宿主执行”和“内核沙箱”两张卡。
- 删除 Docker connection/context/image/CPU/memory/pids/network 等沙箱设置。
- Kernel 卡显示平台 runner、probe 状态和最近失败原因。

### 9.2 旧配置

不能把已有的隔离设置静默映射到更弱的 `spawn`：

| 旧值 | 迁移策略 |
| --- | --- |
| 显式 `kernel` | 自动迁移为 `execution_mode=kernel` |
| 显式 `auto` | 自动迁移为 `execution_mode=kernel`；保留旧模式的 kernel-first 安全意图，Kernel 不可用时改走新的 Run 级询问 |
| 显式 `docker` | 标记为待选择；不能假装等价迁移 |
| 只有旧 `docker_enabled=true` | 标记为 legacy unresolved；要求用户选择 |
| 只有旧 `docker_enabled=false` | 自动迁移为 `execution_mode=spawn` |
| 新安装或确知从未显式配置 | 使用 `spawn` 默认值 |

`auto -> kernel` 是保守迁移，不会把旧的隔离意图静默降级为宿主执行。旧 `docker` 无法无损映射到任一新模式，因此 UI 必须一次性要求选择，Headless 启动则返回可操作的配置错误。

### 9.3 历史 Run 和 manifest

- 历史记录中的 `adaptive`、`docker`、`restricted_host` 只读保留，供审计和 UI 展示。
- 新 Run 只写 `configured_mode=spawn|kernel` 与真实 `effective_runner`。
- 旧 `runtime_image_digest` manifest 不直接复用；迁移器标记 stale，由宿主安装事务按需重建。
- 不批量删除用户 artifact；确认新环境可重建后再提供可恢复的清理流程。

## 10. 分阶段实施方案

### Phase 0：锁定契约和回归样例

1. 为当前关键行为补 characterisation tests。
2. 固定 DeepAgents execute 挂载、permit revalidation、Skill Python import、Node/Python 独立失效、外部目录、HTML E2E、Connector auth 的样例。
3. 建立“Docker 未安装/daemon 未启动”测试环境，作为后续所有阶段的常规门禁。

退出条件：能用测试证明当前哪些能力依赖 Docker，哪些只是被错误路由到 Docker。

### Phase 1：建立统一 execution contract 和正式 SpawnRunner

1. 新增 `ResolvedExecution`、`RuntimeBinding`、`ExecutionRunner`。
2. 把 `RestrictedHostWorkspaceBackend` 重构/重命名为正式 `SpawnRunner` 或组合式 backend。
3. `PermissionedCompositeBackend.default` 始终使用实现 `SandboxBackendProtocol` 的 execution backend。
4. 将路径映射、环境投影、超时、输出处理移到 runner-neutral 层。
5. 增加 DeepAgents 工具列表断言：spawn 与 kernel 都包含 `execute`。

退出条件：没有 Docker daemon 时，默认 spawn Run 可看到并成功调用 DeepAgents `execute`。

### Phase 2：双模式配置和 Kernel fallback HITL

1. 配置/API/UI 改为 `execution_mode=spawn|kernel`，默认 spawn。
2. 增加 runner availability registry 和结构化 reason code。
3. 增加 `kernel_fallback_request` registry、API、前端卡片和持久化 Run override。
4. 调整 Tool Gate 顺序：策略 BLOCK 先返回；真正需要执行时解析 effective runner；随后显示与真实 runner 一致的权限卡。
5. fallback 后重新解析 execution 并签发 spawn permit。

退出条件：Kernel 不可用时批准、拒绝、并发 resolve、resume payload 伪造、配置变化和 Headless 场景都不会静默 spawn。

### Phase 3：Runner-neutral Read/Write/Deny 与平台内核适配

1. 升级 `SandboxGrantProfile`。
2. 让 macOS Seatbelt 消费统一 profile。
3. 实现 Linux bwrap+seccomp runner 及真实 probe；可选叠加 Landlock。
4. 实现 Windows Restricted Token + Job Object runner 及真实 probe。
5. 参数化同一套越权测试到所有 runner。

退出条件：每个平台只有在 allow/deny probe 真实通过后才报告 kernel available。

### Phase 4：宿主 runtime identity 与 Skill 显式绑定

1. 删除 Docker Skill runtime 选择工具和 command regex routing。
2. 引入显式 `runtime_owner`。
3. 拆分 Python、Node、CLI、Browser identity。
4. 将 `runtime_image_digest` 迁移为平台/ABI/runtime binding。
5. 让安装和执行共用绝对 executable 与同一 manifest。

退出条件：pdf Skill 安装 `pypdf` 后的裸 Python 调用明确使用该 Skill interpreter；Node 版本变化不再使 Python env 失效。

### Phase 5：迁移 Docker 专属能力

按以下顺序迁移，每项完成后都在 spawn 和 kernel 下验收：

1. `execute_external_directory`
2. Chromium HTML E2E
3. 通用 Node/Python Skill 安装与执行
4. Managed Provider CLI
5. Browser authorization CLI
6. Lark/Connector catalog、authorize、resume、revoke
7. 公共 HTTPS curl 与网络授权

退出条件：上述能力均不实例化 `ProjectSandboxManager`，且 Docker 未安装时可工作。

### Phase 6：删除 Docker 沙箱实现和兼容代码

1. 删除 `AdaptiveWorkspaceBackend`、`DockerWorkspaceBackend`、`ProjectSandboxManager`。
2. 删除 sandbox Dockerfile、镜像构建/探测脚本、sandbox compose 生命周期。
3. 删除配置、API、UI、prompt 和测试中的 sandbox Docker 选项。
4. 删除 `runtime_image_digest` 的新运行路径，只保留历史数据 reader/migrator。
5. 将 Docker E2E 改为 spawn/kernel contract E2E。

退出条件：源码静态扫描中，Sandbox execution 路径不再出现上述 class 或 runner 值；保留的 Docker 引用均有明确的非沙箱 owner。

### Phase 7：资源和网络硬化

Docker 删除后仍需显式补齐其曾提供的资源控制：

- macOS：rlimit + watchdog + 进程组。
- Linux：rlimit/cgroup v2（可用时）+ namespace + seccomp。
- Windows：Job Object 的 CPU、内存、进程数和 kill-on-close。
- 所有平台：输出上限、超时、取消传播和孤儿进程清理。
- 域名级网络策略需要独立的 managed proxy，不与 kernel filesystem sandbox 混为一谈。

## 11. 文件级改动清单

| 区域 | 主要文件 | 目标改动 |
| --- | --- | --- |
| 配置 | `backend/config.py`、`backend/config.json*` | 新增 `execution_mode`，删除 sandbox Docker 配置，加入 legacy migration |
| 设置页 | `frontend/src/lib/settingsApi.ts`、`frontend/src/app/settings/page.tsx` | 两张模式卡、Kernel probe、fallback 说明，删除 Docker 表单 |
| Backend | `backend/harness/workspace_backends.py` | 统一 execution backend + runner composition，最终删除 Docker/Adaptive/Manager |
| Kernel | `backend/harness/kernel_sandbox.py` | runner registry、平台实现、真实 probe |
| Profile | `backend/harness/sandbox_profiles.py` | Read/Write/Delete/Deny、稳定 digest、spawn 前验证 |
| Permit | `backend/harness/execution_permits.py`、`execution_context.py` | 绑定 resolved execution、runtime、runner 和 fallback revision |
| Tool Gate | `backend/harness/tool_execution.py` | 先解析 effective runner；新增 fallback interrupt；删除字符串推断和 Docker 分支 |
| Agent wiring | `backend/graph/deepagents_manager.py` | 始终挂 execution backend；删除 Docker mount envelope；记录 configured/effective mode |
| Composite | `backend/graph/permissioned_filesystem_backend.py` | 外部目录走统一 execution contract，删除 Docker 错误文案 |
| HITL/API | `backend/api/permissions.py` 及 resume registry/session manager | 新增 run-scoped kernel fallback authority |
| Runtime | `backend/runtime_identity/*`、`backend/harness/host_skill_runtime.py` | 平台/ABI binding，拆分生态，删除 image digest 主键 |
| Skill tool | `backend/tools/request_skill_runtime_tool.py` | 删除 Docker runtime 选择；改为显式 runtime owner contract |
| Connector | `backend/api/connectors.py`、managed CLI service | 脱离 `ProjectSandboxManager`，使用通用 managed command runner |
| Filesystem tools | `backend/tools/filesystem/leases.py`、`validation.py` | 外部目录和 HTML 验证迁移到统一 runner |
| Tests | `backend/tests/**`、integration tests | spawn/kernel 合同测试、fallback HITL、安全负例和 capability E2E |

## 12. 必须通过的验收标准

### 12.1 DeepAgents 工具挂载

- `execution_mode=spawn` 时，最终发给模型的工具包含 `execute`。
- `execution_mode=kernel` 且可用时，工具同样包含 `execute`。
- `PermissionedCompositeBackend` 的 default execution backend 被 DeepAgents 判定为 supports execution。
- Docker 未安装、daemon 未启动、PATH 中没有 docker 时，上述断言仍成立。

### 12.2 安全降级

- Kernel probe 失败不会执行任何宿主命令。
- 用户批准后只当前 Run 使用 spawn。
- 用户拒绝后没有进程被创建。
- 伪造 resume payload、重复 resolve、旧 config revision、旧 probe fingerprint 均不能授权 spawn。
- Kernel permit 不能被 SpawnRunner 消费。

### 12.3 目录边界

- Kernel 允许 workspace/scratch 和显式外部 grant。
- Kernel 拒绝未授权 HOME、其他项目和 PuddingClaw credential/session store。
- symlink 替换、目录移动和 TOCTOU 场景在 spawn 前使 permit 失效。
- spawn UI、日志和结果不把策略授权表述为 OS 隔离。

### 12.4 Runtime/Skill

- 同一 Skill 的依赖安装和执行使用相同 interpreter realpath 与 ABI。
- pdf Skill 安装 `pypdf` 后可在后续命令中 import，且不会落到系统 Python。
- Node 升级不使 Python binding 失效；Python 升级不使 Node binding 失效。
- 多个激活 Skill 不会通过猜测互相污染环境。
- 不存在新的 `runtime_image_digest` 写入。

### 12.5 特殊能力

- HTML 报告真实完成 Chromium E2E，无 Docker。
- `execute_external_directory` 在两种模式下遵守同一 grant/lease。
- Lark/Managed CLI 的 catalog、授权、resume、revoke 无 Docker。
- Kernel 模式下批准网络后可执行公共 HTTPS curl。
- Kernel 模式不开放 Docker socket；spawn 模式仍能在用户授权后运行项目 Docker 命令。

### 12.6 兼容与运维

- 历史 Run 可读。
- legacy 配置不会静默降级。
- 取消/超时后没有孤儿进程。
- 日志始终记录 configured mode、effective runner、probe、fallback authority、runtime binding 和 permit digest，但不记录 secret。

## 13. 删除边界

### 应删除

- PuddingClaw sandbox image 和构建文件。
- Docker sandbox 生命周期、探测、自动路由和 fallback。
- Sandbox Docker CPU/memory/pids/network 设置。
- Skill 的 Docker runtime 选择。
- 以 Docker image digest 作为宿主 runtime 真相的逻辑。

### 应保留

- 用户仓库中的 Dockerfile/Compose。
- MinerU 或其他明确的独立服务部署。
- Docker 作为用户显式调用的普通外部工具（仅 spawn 模式）。
- 历史记录对旧 Docker execution metadata 的只读解析。

## 14. 最终不变量

实施完成后，以下陈述必须始终为真：

1. PuddingClaw 只有 `spawn` 和 `kernel` 两种 Shell 执行模式。
2. 两种模式都保留 DeepAgents 原生 `execute`；选择不隔离不等于删除执行能力。
3. 默认是 `spawn`，且产品诚实说明它不是沙箱。
4. Kernel 不可用时，未经用户对当前 Run 的明确批准绝不 spawn。
5. 同一份 `ResolvedExecution` 可被不同 runner 消费，runner 不推断或修改 Skill runtime。
6. 权限批准与 OS 隔离相互独立，permit 在进程创建前重新验证。
7. Python、Node、CLI 和 Browser 分别拥有宿主 runtime identity，不再依赖 Docker image digest。
8. HTML 验证、外部目录、Managed CLI、Lark 和 curl 都不依赖 Docker 沙箱。
9. Linux/Windows runner 未实现或 probe 失败时明确报告不可用，不虚构安全边界。
10. 删除的是 PuddingClaw 沙箱 Docker，不是用户项目或独立服务对 Docker 的合法使用。
