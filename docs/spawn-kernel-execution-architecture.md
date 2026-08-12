# Spawn + Kernel 双执行模式重构方案

状态：规范基线 + 实施中（已合并 spawn/kernel 产品契约、Linux/WSL2 runner、外部目录与 HTML E2E、显式 Kernel fallback；仍需完成 runner-neutral runtime 收口和真实平台 E2E）

本轮待审核决策：第 3.5、6、7.4、9、10 和 12 节提出 Spawn 低风险探索零干预、Spawn 下完整启用 smart、DeepAgents 虚拟目录降级为路径/路由抽象、文件工具与 Shell 统一 effect 判定、allow/ask/deny 模式规则、once/session/project 审批记忆、凭证与网络耦合审批，以及 Spawn 安装事务；审核通过后再作为后续权限实现和批量验收的规范基线。

日期：2026-08-11

范围：只移除 PuddingClaw 自己的 Docker 沙箱与 Docker 运行时选择；不移除项目、工具或第三方服务对 Docker 的正常依赖。

本次合并了三个并行结论：DeepAgents 的 `execute` 能力属于执行 backend 协议，不属于 Docker；权限判断、运行时投影和 OS 隔离必须分层；Windows 首发 Kernel 路径采用 WSL2 复用 Linux runner。本文因此同时记录产品目标、Harness 权限不变量和当前代码差距；“目标/必须迁移”不能误读成“当前已全部上线”。

平台承诺：macOS、原生 Linux，以及 Windows 通过 WSL2 使用 Linux Kernel runner，是完整 `kernel` 支持路径。原生 Windows 本地启动默认使用 `spawn`，当前不声明原生 Kernel 沙箱；原生 AppContainer Kernel runner 作为后续增强，不阻塞本轮 Docker 沙箱移除。WSL2 bootstrap 降级为部署文档和前置条件，不作为当前产品化向导或自动安装能力交付。

## 1. 决策摘要

PuddingClaw 的 Shell 执行只保留两种用户可选模式：

| 配置值 | 用户界面名称 | 默认值 | 隔离含义 |
| --- | --- | --- | --- |
| `spawn` | 宿主执行 | 是 | 直接创建宿主进程；低风险探索不干预，高风险副作用仍经过 Tool Gate；没有 OS 文件或网络边界 |
| `kernel` | 内核沙箱 | 否 | 使用当前操作系统的进程级内核隔离；不可用时必须询问用户是否切换项目或仅本次 Run 回退到 `spawn` |

`spwan` 仅视为讨论中的拼写错误，配置、API、日志和代码统一使用 `spawn`。

审批策略与 runner 正交。选择 `spawn` 不意味着普通文件读取、向上搜索、项目内编辑或常规本地命令需要逐次批准；这些能力是 Agent 自主探索和完成开发任务的基础，默认直接放行。选择 `kernel` 也不会创建另一套 Tool Gate，只会把同一份权限结果编译成更窄的 OS 可见范围。

以下模式和开关从新配置中删除：

- `auto`
- `docker`
- `docker_enabled`
- `on_unavailable`
- Skill 的 `host | docker` runtime 选择

无论选择 `spawn` 还是 `kernel`，都必须向 DeepAgents 提供一个实现 `SandboxBackendProtocol` 的 execution backend。不能在 `spawn` 模式下退化为普通 `FilesystemBackend`，否则 DeepAgents 会动态移除 `execute` 工具。

内核沙箱不可用时不允许静默降级。推荐在第一次真正需要执行命令时发出 HITL 请求，并按失败稳定性提供作用域：

- 平台/部署稳定不可用：默认建议“将本项目切换为 `spawn`”，持久化项目执行模式，后续 Run 不再重复询问。
- 一次性或暂时故障：默认建议“仅本次 Run 回退”，不永久降低项目安全级别。
- 用户也可以在卡片中明确选择另一个作用域；任一批准都要重新解析环境并签发 runner 为 `spawn` 的新 permit。
- 用户拒绝：该命令返回结构化错误，Run 可继续使用不依赖 Shell 的工具。
- 无交互/Headless：没有显式预授权时失败关闭，不自动回退。

“内核不可用”指真实运行环境不满足安全前提，例如 Linux/WSL2 禁止 unprivileged user namespace、WSL1、原生 Windows 本地启动、Windows Backend 尚未运行在 WSL2 内，或 macOS Seatbelt 自检失败。原生 Windows 设置页默认 `spawn`；需要完整 `kernel` 时引导用户按文档在 WSL2 中部署，而不是把原生受限令牌包装成等价沙箱。

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
- `spawn` 中的权限判断是产品策略和审计，不是安全边界；普通宿主目录发现和读取默认允许，不能把 workspace root 误当作安全边界。
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

### 3.1 当前配置契约已经收敛

- `backend/config.py`、`backend/config.json.example` 和本地默认配置使用 `execution_mode=spawn|kernel`，默认值为 `spawn`；旧 `sandbox_mode`、`docker_enabled`、`on_unavailable` 不再迁移或解释。
- `frontend/src/lib/settingsApi.ts` 与 `frontend/src/app/settings/page.tsx` 只暴露“宿主执行 / 内核沙箱”两种模式，不再提供 Docker 沙箱配置或 Docker 探测入口。
- `backend/harness/workspace_backends.py::build_workspace_execution_backend` 只构造 `spawn` 或 `kernel`。Docker/Adaptive 类尚未从源码删除，但新的 Managed CLI composition 已不再实例化它们。

### 3.2 DeepAgents 的 execute 不依赖 Docker，但依赖执行协议

- `SpawnWorkspaceBackend`、`KernelWorkspaceBackend`、`DockerWorkspaceBackend` 和 `AdaptiveWorkspaceBackend` 都继承 `SandboxBackendProtocol`。
- DeepAgents 的 `FilesystemMiddleware` 会检查 backend 是否支持执行；如果 backend 只是普通文件 backend，它会在模型调用前过滤 `execute`。
- 当前 `SpawnWorkspaceBackend.execute()` 已经通过 `subprocess.run` 在宿主执行，说明宿主 `spawn` 完全可以保留 DeepAgents 原生 `execute`。
- `PermissionedCompositeBackend` 的 default backend 必须继续是可执行 backend；只给其中某条文件 route 加执行能力不够。

结论：`spawn` 应是正式 execution backend，而不是“没有 sandbox backend”。

### 3.3 当前内核实现覆盖 macOS 与 Linux/WSL2，仍需真实平台 E2E

- `backend/harness/kernel_sandbox.py` 已包含 `MacOSSeatbeltRunner` 与 `LinuxBwrapSeccompRunner`。
- macOS runner 使用 `/usr/bin/sandbox-exec`，Linux/WSL2 runner 使用 root-owned/trusted `bubblewrap` 加 seccomp 过滤；二者都必须通过真实 allow/deny probe，失败即进入显式 fallback，而不是自动改用 `spawn`。
- `SandboxGrantProfile` 已具备 runner-neutral 权限快照，当前显式保留 `deny_roots` 字段、digest 和 spawn-time canonical 校验；上层 Grant 解析仍需继续补齐嵌套 Deny 的来源。
- Linux/WSL2 Kernel runner 已加入源码，但仍需在真实 Linux/WSL2 主机完成 bwrap、namespace、seccomp、网络、超时和 symlink/deny E2E 后，才能标记为稳定交付。Windows 首发兼容路径是把 Backend 和宿主 runtime 部署在 WSL2，复用同一 Linux runner；原生 Windows 本地启动默认 `spawn`。
- 当前 runner mode 会绑定到 permit：macOS 为 `kernel_macos_seatbelt`，Linux/WSL2 为 `kernel_linux_bwrap_seccomp`；不能把一个平台的 permit 重放到另一个 runner。
- 当前实现已将 `execute_external_directory` 的 Kernel/Adaptive 路径接入精确 external cwd/profile；HostFileBroker 仍负责外部目录 grant，writable draft 仍由 lease 控制。
- 当前实现已将 workspace 内 HTML browser E2E 接入 typed Kernel validator；workspace 外的 HTML 仍先经过 HostFileBroker，再使用 Kernel external-directory runner。固定的 `/opt/puddingclaw/bin` 仅映射到受信任的仓库脚本目录，不向普通 shell 暴露。
- macOS 的 Managed Provider CLI 与 browser authorization CLI 已迁到 Host Toolchain + Seatbelt：Toolchain 发布不可变 CLI，执行时创建私有 HOME/TMP，只挂载当前 workspace 和精确 Toolchain revision，Vault 密文状态只投影到该 HOME，结束后再做 CAS 回写；不再要求 Docker。Linux/WSL2 仍需把同一 typed contract 接到 bwrap/seccomp runner 后，才能宣称跨平台完成。
- Docker 沙箱删除的完成条件必须包含 macOS、原生 Linux、Windows/WSL2 三条用户路径的真实越权 E2E；原生 Windows AppContainer runner 不作为首发门槛。

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
  - `spawn/kernel` 产品分支，以及仅供 managed runtime 使用的 `adaptive/docker` 内部兼容分支
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
  - Connector catalog、Lark 执行与授权已使用统一 Host managed-integration composition
  - 仍需在 Linux/WSL2 上补同一 contract 的真实 E2E
- `backend/tools/filesystem/leases.py`、`validation.py`
  - 独立外部目录执行和 Chromium 验证仍假定 Docker 能力

因此不能先删除 Docker class 再逐个修错。必须先建立 runner-neutral execution contract，并将以上能力逐项迁移。

### 3.5 基线审批噪声来自机制缺陷，不是风险本身

原始实现基线中有四个会系统性制造多余弹窗或错误拒绝的机制；本节保留它们作为本轮实现的验收依据：

1. `backend/graph/permission_policy.py` 仍以 `strict` 作为默认审批模式；更关键的是，`tool_execution.py::_reviewer_eligible()` 和 `_smart_sandbox_result()` 只接受 `docker/adaptive/kernel`，`_smart_network_result()` 只接受 `docker/adaptive`。默认 runner 改成 Spawn 后，smart 的主要自动放行路径反而被硬关闭，实际行为趋近 strict。
2. `tool_execution.py::_session_grant_scope()` 对普通 `execute` 恒返回 `None`，而 permission fingerprint 使用完整 action preview。结果是相同程序处理不同输入文件也无法复用授权，例如 `pdftotext a.pdf -` 与 `pdftotext b.pdf -` 被当作两个完全无关的能力。
3. 当前只有确定性白名单、危险模式和兜底 ASK，没有用户可配置的 `allow/ask/deny × pattern × scope` 规则层。“这个项目允许运行某类本地转换器”无法表达，只能逐命令批准。
4. `install_packages` 在 Spawn 下直接返回 `package_install_requires_host_runtime` DENY。这是 Docker/Kernel 时代残留，与“Spawn 是默认宿主 runtime、安装和执行共享同一 ABI binding”的新契约冲突。

本轮方案将这些问题视为权限实现缺陷，而不是通过扩大“低风险”定义去掩盖。修复顺序固定为：先让 smart 与低风险确定性策略在 Spawn 生效，再引入可复用语义规则，最后迁移安装事务和删除旧分支。

### 3.6 三个线程合并后的实现对照

| 领域 | 当前已具备 | 仍需收口 |
| --- | --- | --- |
| DeepAgents `execute` | Spawn/Kernel backend 都实现 `SandboxBackendProtocol`，不会因关闭 Kernel 而移除 `execute` | 统一为 runner-neutral backend，避免文件路由和命令执行各自维护一套权限语义 |
| 权限 handoff | `ExecutionPermit`、`AuthorizedExecution`、`SandboxGrantProfile` 已绑定 command/profile/revision/runner，并在 spawn 前复核 | 所有执行入口都必须走同一 handoff；禁止 legacy backend 直接执行或自行重建环境 |
| Spawn | 宿主进程、独立 PuddingClaw HOME/TMP、超时、输出上限和进程组生命周期 | 明确 Spawn 是“无 OS 隔离的宿主模式”，把低风险 smart 路径从旧 Docker-only 条件中解耦 |
| Kernel | macOS Seatbelt 与 Linux/WSL2 bwrap/seccomp runner、canonical roots、network/profile digest | 真实 Linux/WSL2 越权 E2E、helper 发行形态、symlink/nested deny 和进程树边界 |
| Skill runtime / Secret | 受管宿主 runtime、Skill Secret 加密存储与临时环境注入链路已存在 | runtime owner 必须结构化传递；不能依赖命令字符串猜 Skill；Secret 不得进入 command、日志或模型上下文 |
| Managed CLI | macOS 已使用 Host Toolchain + Seatbelt 运行 Provider CLI 与 browser authorization，Catalog/API/Agent 共用同一 composition | Linux/WSL2 接入 bwrap/seccomp；补 browser runner 跨进程恢复与平台 E2E |
| Docker | 源码仍保留旧 Docker/Adaptive 实现，但新 Run、Skill runtime 和 macOS Managed CLI composition 均不实例化它们 | 删除未再引用的兼容 class、镜像与配置；用户项目显式 Docker 仍作为普通外部工具保留 |
| Windows | 原生 Windows 的 `spawn` 与 WSL2 部署方向已定义 | 首发 Kernel 只承诺 Windows via WSL2；原生 AppContainer/DACL/Job Object 不是本轮发布门槛 |

因此，代码中暂时存在的 `DockerWorkspaceBackend`、`AdaptiveWorkspaceBackend`、`request_skill_runtime` 或 `runtime_image_digest` 不能被解释为产品仍有第三种执行模式；它们是迁移期兼容面，必须在对应能力迁移后删除或降级为独立 typed managed runtime。

## 4. 目标领域模型

### 4.1 配置模型

```python
ExecutionMode = Literal["spawn", "kernel"]

class TerminalConfig:
    execution_mode: ExecutionMode = "spawn"
    default_timeout_seconds: int = 120
    external_directory_writable_enabled: bool = False

ApprovalMode = Literal["smart", "strict"]

class PermissionConfig:
    approval_mode: ApprovalMode = "smart"
    rules: tuple[PermissionRule, ...] = ()
```

建议将字段命名为 `execution_mode`，不用 `sandbox_mode`。因为 `spawn` 不是沙箱，把它放进 `sandbox_mode` 会持续误导 UI、日志和策略代码。

运行时还需要区分：

```python
configured_mode: Literal["spawn", "kernel"]
effective_runner: Literal[
    "spawn",
    "kernel_macos_seatbelt",
    "kernel_linux_bwrap_seccomp",
    "kernel_windows_wsl2_bwrap_seccomp",
]
```

`configured_mode` 表达用户偏好；`effective_runner` 是当前 Run 经探测和授权后的真实执行器。日志和 Run inventory 必须同时记录，不能只记录模糊的 `mode`。

新配置不再提供 `full_access` 审批模式。Spawn 已经诚实表达“宿主可达、无 OS 隔离”，审批策略只保留 smart/strict；继续保留一个名为 full access 的第三档会再次把 runner 能力和审批偏好混为一谈。按既定迁移决策，不保留旧值兼容解释。

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
    runtime_owner: str
    environment_binding_digest: str
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

`environment` 与 `secret_values` 只存在于当前执行 handoff 的内存投影中，不进入模型消息、Session JSON、Trace 文本或普通日志。`runtime_owner` 必须由 Skill/managed adapter 的结构化上下文提供；命令字符串、当前激活 Skill 列表和解释器名称只能作为诊断信息，不能成为权限或解释器选择的唯一依据。

### 4.2.1 Harness 权限 handoff 不变量

Tool Gate 到 runner 之间只允许传递一个不可变的 `AuthorizedExecution`：

```text
Tool call
  -> normalized requirements
  -> SandboxGrantProfile
  -> ExecutionPermit(one tool call, one spawn)
  -> AuthorizedExecution
  -> SpawnRunner / KernelRunner
```

- `SandboxGrantProfile` 统一承载 canonical `read/write/delete/deny` roots、workspace/scratch、网络开关和资源上限；Spawn 使用它做审计与一致性校验，Kernel 还把它投影成 OS profile。
- `ExecutionPermit` 绑定 command digest、requirements digest、permission revision、profile digest、selected runner 和 runner binding digest，并且只能消费一次；可复用的是声明作用域内的 Grant，不是进程创建凭证。
- 真正创建进程前必须重新校验环境 revision、profile、权限 revision 和 runner binding；任一变化都 fail-closed。
- Secret 通过环境映射注入，不能拼进 shell command；执行输出返回 Agent 前必须脱敏。
- `spawn` 可以访问当前用户可达的宿主资源，但不能把 Tool Gate、`root_dir` 或 exact-directory grant 宣称成 OS 隔离；需要抗恶意脚本边界时必须选择 `kernel`。

当前代码对应入口为 `backend/harness/execution_context.py`、`execution_permits.py`、`sandbox_profiles.py` 和 `tool_execution.py`。后续改动不得绕过这条 handoff，不能在 backend 内部重新猜 runner、环境或权限。

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
- 普通目录发现、读取、工作区编辑和低风险本地命令默认直接执行；权限系统只对敏感数据、不可逆破坏、外部状态变更和凭证耦合网络等真实高风险效果介入。
- UI 必须标注“宿主模式无法由 OS 强制限制”，不能把 Tool Gate 描述成文件或网络沙箱。

### 4.5 KernelRunner

目标平台实现：

| 平台 | 目标 runner | 最低边界 |
| --- | --- | --- |
| macOS | `MacOSSeatbeltRunner` | Seatbelt deny-by-default、隔离 HOME/TMP、Read/Write/Deny、网络开关、进程组 |
| Linux/WSL2 | `LinuxBwrapSeccompRunner` | mount/user/pid/ipc/uts/network namespace、只读/可写/隐藏 bind、seccomp、`no_new_privs`、进程组；可用时叠加 Landlock |
| Windows（推荐） | WSL2 中的 `LinuxBwrapSeccompRunner` | 与 Linux 相同的 namespace/mount/seccomp 边界；Windows 桌面只负责 UI 和连接 Backend |
| Windows 原生（后续） | `WindowsAppContainerRunner` | AppContainer/LPAC、精确 DACL 投影、受限令牌、Job Object、网络 capability、句柄白名单 |

平台 runner 只有在真实 allow/deny 自检通过后才可用。实现代码存在但 probe 没有证明边界成立，仍等价于不可用。

macOS `sandbox-exec` 已被系统标记为 deprecated，应保留 fail-closed probe，并把未来 API 变化视为正常的 unavailable 情况，而不是捕获异常后直接 spawn。

### 4.5.1 统一原生 Helper 边界

Linux 的 `no_new_privs`/seccomp 涉及低层 OS API。最终发布仍应新增一个最小、可审计、按平台构建的 `puddingclaw-kernel-helper`；原生 Windows runner 开始实施时再为同一协议增加 Windows target。当前 Phase 1 暂用 Python 生成固定 seccomp BPF 并由 bubblewrap 装载，目的是先闭合 runner 语义；发布前必须完成 helper 收敛或明确接受其审计边界：

- Helper 只接收经过 schema 校验且带 digest 的 `ResolvedExecution` 投影，不接收来自模型的任意 sandbox 参数。
- Backend 通过 stdin 发送定长/分帧 JSON，Helper 通过 stdout/stderr 返回结构化启动、输出、退出和 probe 结果。
- Helper 在校验 cwd、根目录、环境、runner id 和 permit digest 后才创建子进程。
- 发布包携带对应平台/架构的签名二进制及 SHA-256 manifest；Helper digest 参与 `effective_runner` 与 permit。
- build-time 可以使用 Rust 等内存安全语言；最终用户不需要安装编译工具链。
- macOS 第一阶段可继续调用现有 Seatbelt runner，但协议与 probe 输出必须和 Helper 一致，后续可再收敛到同一二进制。

Helper 不是容器运行时、守护进程或第二套环境；它只是把 PuddingClaw 已解析好的进程创建请求投影到本机内核安全原语。

### 4.5.2 Linux/WSL2 实现细节

Linux 以 bubblewrap 作为低层无特权 launcher，但安全能力来自 Linux kernel namespace、mount、seccomp 和 LSM，不需要 Docker daemon、镜像或容器文件系统。

文件系统投影：

1. 创建新的 mount namespace，只挂载最小系统运行时（`/usr`、`/bin`、`/sbin`、库目录、`/etc` 等）；不把宿主 `/` 整体暴露给命令，工作区、scratch 和显式授权根再按 profile 叠加。
2. 将 `write_roots` 逐个覆盖为 writable bind；将 `deny_roots` 用空的只读 mount 或等价不可访问节点覆盖，保留嵌套 deny carveout。
3. 为 `/proc` 创建新 procfs，只暴露 sandbox PID namespace；提供最小 `/dev`，明确包含正常 CLI 所需的 `/dev/null`、`/dev/zero`、`/dev/urandom`。
4. 为 Run 创建独占 HOME/TMP，隐藏宿主 `/run/user/<uid>`、D-Bus、SSH agent、Docker/Podman socket、credential socket 和其他宿主 IPC endpoint。
5. 对 symlink-in-path、尚不存在但位于 writable root 内的 protected path 做 mount-time 遮蔽；不能只在 Python 中做字符串检查。

进程与权限投影：

- 使用 user、PID、IPC、UTS namespace；使用 `--new-session`、`--die-with-parent`，并丢弃所有 Linux capabilities。
- 设置 `PR_SET_NO_NEW_PRIVS`，禁止 setuid/setgid/file capability 在 `execve` 后扩大权限。
- seccomp 至少阻断 mount/umount、ptrace、process_vm_writev、bpf、perf_event_open、keyctl、kexec、reboot、内核模块、未授权 namespace 创建等逃逸面；允许浏览器自身安全沙箱真正需要的 syscall 必须单独建 profile 并有 E2E。
- 禁止 sandbox 进程再次创建 user namespace，避免嵌套 namespace 绕过 launcher 假设。

网络投影：

- `network_allowed=false`：创建 network namespace，并用 seccomp 阻断 AF_INET/AF_INET6；仅保留必要的进程内/本地 Unix 能力，同时确保宿主 socket 没有被 mount 进来。
- `network_allowed=true`：显式共享宿主网络 namespace；这代表本次命令获得一般网络能力，不宣称域名级限制。
- 将来若实现域名白名单，使用独立 managed proxy；不能把 DNS 解析结果临时写进 seccomp 规则冒充稳定域名策略。

兼容与依赖：

- 优先使用安装目录外、受信 PATH 中满足最低版本的系统 `bwrap`；发行包同时携带经过 digest 校验的备用 binary，避免要求普通用户先懂系统依赖。
- 不允许从 workspace、Skill 或当前 cwd 解析 `bwrap`。
- WSL2 走同一 Linux probe；WSL1 因缺少所需 namespace 能力直接报告 `unsupported_environment`。
- Landlock 按运行时 ABI/errata 探测后作为纵深防御叠加。它不作为 bwrap 失败后的静默替代，因为较旧 ABI 可能无法等价表达嵌套 Read/Write/Deny。

Linux probe 必须至少验证：允许读系统 runtime、允许 workspace 写、拒绝其他项目读写、拒绝宿主 HOME secret、拒绝 host PID/IPC、网络开关、超时杀死完整进程树，以及 symlink/嵌套 deny 不可绕过。

Phase 1 的安全审查结论：当前代码可以作为 Linux/WSL2 runner 的开发基线，但不能宣告生产级强隔离。发布前仍必须完成 root-owned helper/bwrap 信任链、参数级 `clone` namespace 过滤或等价 native helper、目录 inode/TOCTOU 锁定、WSL1/DrvFS 探测，以及完整的 nested namespace、mount API、setuid、pidfd、symlink/rename 和进程树 E2E。没有这些证据，Kernel runner probe 必须保持 fail-closed。

### 4.5.3 Windows 推荐部署：WSL2

Windows 第一阶段不直接实现原生目录沙箱，而是把 PuddingClaw Backend、Skill runtime 和 execution helper 部署在 WSL2；Windows Electron/Frontend 继续原生运行。对 Backend 而言这是正常的 Linux 主机，因此 `spawn` 和 `kernel` 都复用 Linux 实现，不引入第三套运行时语义。

```text
Windows Desktop UI
        |
        | authenticated localhost API/SSE
        v
PuddingClaw Backend in WSL2
        |
        +--> SpawnRunner（WSL2 Linux 宿主）
        |
        `--> LinuxBwrapSeccompRunner
```

部署与发现：

- Windows 设置页检测当前是否为原生 Windows Backend；原生本地启动默认 `spawn`，并把 `kernel` 标记为需要 WSL2 Backend。
- WSL2 bootstrap 本轮降级为文档化部署路径：说明如何在 WSL2 中安装 Backend、平台 helper、受控 Python/Node runtime 和用户级服务；不在产品内承诺一键安装，也不在 WSL2 内再安装 Docker。
- Backend 仅绑定 WSL2 loopback，Windows UI 通过 localhost forwarding 连接；每次安装生成本机认证 token，API/SSE/WebSocket 都必须鉴权。
- 禁止默认绑定 `0.0.0.0`，防止 WSL2 mirrored networking 或局域网暴露 Backend。
- UI 记录 distro identity、Backend version、helper digest、runtime binding 和 probe revision，升级时按事务切换。

工作区与目录：

- 推荐把仓库放在 WSL2 自己的 ext4/VHDX 文件系统，例如 `~/Code/...`，获得稳定的 inode、权限、symlink、inotify 和性能语义。
- Windows UI 使用稳定的 WSL distro + Linux path 标识项目，可通过 `\\wsl$\<distro>\...` 展示或选择，但 Backend 的 source of truth 始终是 canonical Linux path。
- `/mnt/c`、`/mnt/d` 等 DrvFS workspace 不是首发 Kernel 的默认支持路径；其大小写、ACL、symlink、rename 和 metadata 语义不同，必须单独 probe。没有通过时提示迁移/复制到 WSL 文件系统，不能假装与 ext4 等价。
- Windows 文件选择器授权的外部目录需要显式转换成 DrvFS path，并在目录级 probe 通过后才加入 profile；转换失败或语义不安全时拒绝 Kernel 访问。
- 不把整个 `/mnt/c/Users/<user>`、Windows credential、浏览器 profile 或 Windows Docker socket 映射进 Kernel profile。

运行时和特殊能力：

- Python、Node、Skill venv、Chromium/Playwright 和 Managed CLI 全部安装、解析、执行在同一 WSL2 distro，runtime identity 记录 `linux-wsl2 + distro-id + arch + ABI`。
- 禁止在 WSL2 安装依赖后调用 Windows `python.exe`/`node.exe`，反向也一样；跨 PE/ELF ABI 投影一律视为错误。
- Chromium HTML E2E 默认使用 WSL2 内的 headless Chromium，不依赖 WSLg GUI。
- OAuth/Lark 等浏览器授权由 Backend 生成一次性 URL，Windows UI 用默认浏览器打开；callback 通过已鉴权的 localhost bridge 回到 WSL2，并测试端口变化、重启和超时。
- 需要访问 Windows 原生应用/文件的能力必须经过 typed broker，而不是给一般 Shell 暴露 PowerShell、`cmd.exe` 或任意 `wsl.exe` 反向桥接。

Windows 用户体验：

- 设置页在 Windows 原生 Backend 上默认选择 `spawn`；用户选择 `kernel` 时，展示“完整 Kernel 支持需要 WSL2 Backend”的部署文档入口、工作区迁移建议和 probe 说明。
- 用户暂不部署 WSL2 时，将原生 Kernel 标记为稳定的 `unsupported_environment`；第一次执行建议“将本项目切换为 `spawn`”，也保留仅本次 Run 回退和拒绝选项。
- 不能把 WSL2 bootstrap 失败自动转换为 Windows 原生 spawn；安装失败与运行时安全降级是两种不同状态。
- WSL1 明确不支持，提示升级到 WSL2。

WSL2 E2E 必须覆盖：Windows UI 创建 Run、DeepAgents `execute` 挂载、ext4 workspace 读写与越权拒绝、DrvFS 拒绝/受控授权、HTML Chromium、OAuth callback、Backend 重启、WSL distro shutdown/restart，以及 Kernel 不可用时项目级/Run 级两种回退选择。

### 4.5.4 原生 Windows Kernel 后续路线

原生 Windows 不是本轮 Docker 沙箱移除的发布门槛。原因不是 Windows 缺少内核原语，而是完整目录策略需要同时处理 AppContainer/LPAC identity、DACL 投影与崩溃恢复、Restricted Token、Job Object、handle allowlist 和网络 capability；只使用 Restricted Token 无法诚实实现 Read/Write/Deny。

后续若实施 `WindowsAppContainerRunner`，必须满足：

- AppContainer/LPAC 作为资源和网络边界，Restricted Token 仅作纵深防御。
- Run-scoped SID 的 ACL 事务有原始 security descriptor/file ID journal、逆序恢复和崩溃恢复。
- Job Object 在首进程 resume 前完成绑定，禁止 breakaway 并启用 `KILL_ON_JOB_CLOSE`。
- 只继承显式 handle；默认不给 credential、注册表、COM broker、UI、clipboard、camera 或 microphone capability。
- 用 `os.replace`、Git、SQLite、pytest 临时文件验证 rename/delete/atomic-write，而非只测创建文件。
- 在安全和兼容 E2E 完整通过前，设置页不得把它标成稳定 Kernel runner。

### 4.5.5 支持矩阵与发布门槛

| 环境 | Kernel 交付状态 | 不可用条件 |
| --- | --- | --- |
| macOS | 本次交付 | Seatbelt binary/probe 失败 |
| Linux glibc 常见发行版 | runner 已实现，稳定交付待 E2E | user namespace、mount namespace、seccomp 或 bwrap probe 失败 |
| Linux musl/NixOS | 本次交付，按 helper/bwrap 自包含包验证 | packaged helper 不兼容或内核能力 probe 失败 |
| Windows + WSL2 ext4 workspace | runner 已实现，稳定交付待 WSL2 E2E；bootstrap 为文档级前置条件 | distro/kernel 禁止所需 namespace，或 WSL Backend/helper probe 失败 |
| Windows + WSL2 DrvFS workspace | 有条件支持 | 单目录 DrvFS 语义 probe 未通过时要求迁移到 WSL ext4 |
| WSL1 | 不支持 Kernel | 固定 `unsupported_environment`，建议本项目切换为 spawn |
| Windows 10/11 原生 Backend | 本轮只支持 spawn | 选择 Kernel 时引导 WSL2；未部署则建议本项目切换为 spawn |
| Windows AppContainer Kernel | 后续增强 | 达到完整 ACL/Job/network/handle E2E 后再转稳定 |

“稳定交付”表示功能实现、打包、设置页探测、CI 和安全 E2E 同时完成。WSL2 bootstrap 本轮是文档化前置条件；仅能在开发机 WSL shell 中手动启动命令，不算 Windows 兼容完成。

## 5. 内核不可用时的用户确认协议

### 5.1 触发时机

采用惰性触发：Run 创建时可以探测并记录状态，但只有在第一个未被策略直接 BLOCK 的 Shell/managed CLI/Chromium 执行真正需要创建进程时才询问。

这样不会让只读问答、文件工具或不需要 Shell 的 Run 无故弹窗。

Probe 必须同时给出稳定性分类：

| 分类 | 典型原因 | 默认建议 |
| --- | --- | --- |
| `stable` | Windows 原生未部署 WSL2、WSL1、helper 缺失、系统永久禁用 user namespace、平台不受支持 | 将本项目切换为 `spawn` |
| `transient` | Helper 正在升级、临时文件/资源错误、一次 probe 超时、运行中 runner 消失 | 仅本次 Run 回退到 `spawn` |

分类只决定 UI 的推荐项，不能替用户批准。用户始终可以选择项目级、Run 级或拒绝。

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
    "project_id": "...",
    "workspace_identity": "sha256:...",
    "configured_mode": "kernel",
    "fallback_runner": "spawn",
    "platform": "darwin|linux|win32",
    "availability_class": "stable|transient",
    "reason_code": "unsupported_environment|dependency_missing|probe_failed|runner_disappeared|policy_projection_failed",
    "reason": "用户可读且已脱敏的说明",
    "probe_fingerprint": "sha256:...",
    "config_revision": 12
  },
  "decisions": [
    {"type": "switch_project_to_spawn"},
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
kernel       waiting_hitl
      +-----------+-----------+
      |           |           |
switch project  fallback_once reject
to spawn          |           |
      |           |           v
      |       persist Run   command error
      |       override
      v           v
persist project execution_mode=spawn
      |           |
      +-----+-----+
            v
      resolve again
            |
            v
      issue spawn permit
```

项目级批准写入正常项目配置，而不是伪装成永久 permission grant：

```python
ProjectExecutionPreference:
    project_id
    workspace_identity
    execution_mode = "spawn"
    source = "kernel_unavailable_user_choice"
    reason_code
    probe_fingerprint
    approved_request_id
    config_revision
    updated_at
```

配置解析优先级固定为：项目显式模式 > 全局默认模式。用户以后在项目设置中重新选择 `kernel` 时，先做 probe，再清除旧的 spawn preference。

仅本次批准才写 `RunExecutionOverride`：

```python
run_id
project_id
configured_mode = "kernel"
effective_runner = "spawn"
scope = "run"
reason_code
probe_fingerprint
config_revision
approved_request_id
approved_at
```

恢复执行时必须重新读取相应的项目配置或 Run override。仅收到 `switch_project_to_spawn`/`fallback_once` resume payload 而没有服务端持久化记录，视为未批准。

### 5.4 生命周期规则

- 项目级选择对该项目的后续 Run 生效，不影响其他项目和全局默认值；设置页持续明确显示“宿主执行（未隔离）”。
- Run 级批准不跨 Session、Goal 或下一个 Run。
- 项目已经显式切换为 spawn 后不再做 Kernel fallback 询问；只有用户重新选择 kernel 才重新探测。
- 同一 Run、同一 probe fingerprint 的临时回退不重复询问。
- Kernel 曾可用但执行前失效时，旧 permit 作废并重新询问。
- Run override 在配置 revision、工作区 identity 或 probe fingerprint 改变时不可复用；项目级 spawn preference 是正常配置，不绑定旧 probe 生命周期。
- 拒绝只阻止需要执行进程的动作，不强制终止整个对话。
- Headless worker 可以直接读取项目已显式保存的 `execution_mode=spawn`；若项目仍配置 kernel，则必须显式获得 run-scoped fallback grant，否则返回 `waiting_for_user`/`kernel_unavailable`，不能自动 spawn。

### 5.5 Permit 约束

`ExecutionPermit.selected_runner` 必须从笼统字符串升级为稳定 runner id。最重要的规则是：

> Kernel permit 永远不能被 SpawnRunner 消费。

无论项目级还是 Run 级批准，回退后都必须从 `ResolvedExecution` 重新编译 profile 和 permit，不能修改现有 permit 的 runner 字段后继续执行。

Permit 是单次进程创建 handoff，不是可复用 grant。目录授权、网络授权或项目级配置可以按其声明作用域复用；但已经签发给某个 tool call、命令 digest、profile digest、runner binding 和 permission revision 的 `ExecutionPermit`，只能被消费一次。第二次 spawn 即使字段完全相同也必须失败，并回到 Tool Gate 重新生成新的 handoff。

## 6. 目录与权限语义

### 6.1 三层边界，不再让 `root_dir` 代替权限

文件系统必须拆成三个彼此独立的概念：

```text
路径与路由层：DeepAgents 虚拟路径、真实宿主路径、backend 路由
权限决策层：操作效果、目标敏感度、用户意图、grant 与审批策略
执行隔离层：Spawn 宿主进程或 Kernel OS profile
```

DeepAgents 的 `root_dir + virtual_mode=True` 只适合作为内置 `read_file`、`write_file`、`edit_file`、`ls`、`glob`、`grep` 的稳定路径和 backend 路由抽象。它不是完整安全模型，也不能约束宿主 `execute`。因此不能继续出现 `glob("../**/*.pdf")` 被虚拟 root 拦截、但 `execute("rg ... ..")` 可以读取同一目录的分裂语义。

目标 backend 保留两条路径平面：

```text
内部虚拟平面
├── /workspace
├── /skills
└── /scratch

执行环境文件平面
├── spawn：当前桌面用户可访问的真实宿主路径
└── kernel：本次 OS profile 已投影的 mount/path
```

`cwd` 和 `/workspace` 是默认探索起点，不是 Spawn 的权限上限。Spawn 下内置文件工具必须支持真实绝对路径、`..` 向上遍历和跨相邻源码目录搜索；Kernel 下相同操作只能在已挂载命名空间内解析，不能通过 `..` 越过 mount root。

不要把 DeepAgents `root_dir` 简单设置为宿主 `/`：这会混淆内部虚拟路由，并且无法正确表达 Windows 多盘符。应由 PuddingClaw 自己的 host filesystem adapter 解析 canonical host path，再交给统一权限决策器。

### 6.2 文件工具与 Shell 使用同一能力判定

权限判断以实际效果为单位，而不是以工具名为单位：

| 等价能力 | 必须得到相同结果 |
| --- | --- |
| `glob` / `grep` / `ls` | Shell 中的 `find` / `rg` / `ls` |
| `read_file` | Shell 中的 `cat` / `sed -n` / PDF 文本提取 |
| `write_file` / `edit_file` | Shell 中的重定向、格式化器和补丁工具 |
| `delete` | Shell 中的 `rm`、覆盖和批量清理 |
| typed HTTP/Connector | Shell 中具有相同凭证、目标和副作用的网络调用 |

实现上应先把文件工具或命令解析成 runner-neutral 的 `RequestedEffects`，再由同一个策略函数判定：

```python
class RequestedEffects:
    read_paths: tuple[Path, ...]
    write_paths: tuple[Path, ...]
    delete_paths: tuple[Path, ...]
    executes_code: bool
    network: NetworkIntent | None
    credential_profiles: tuple[CredentialRef, ...]
    external_mutations: tuple[ExternalMutation, ...]
```

如果 Shell 静态分析无法可靠证明效果，策略可以把“不确定性”作为风险输入，但不能因为请求来自 `execute` 就一律询问。

### 6.3 Agent 探索默认放行

低风险能力是 Agent 完成任务的工作集，严格模式和智能模式都不得逐次干预：

- 普通目录的 `pwd`、`ls`、`tree`、`stat`、`du`、`find`、`glob`、`grep`、`rg`；
- 从当前 workspace 向父目录查找项目边界、依赖、源码、文档和用户给出的文件；
- 读取普通源码、配置、日志、图片、PDF、Office 文档和 Git 元数据；
- 在当前 workspace、scratch 或用户明确指定的输出目标内创建、编辑和格式化文件；
- 运行无外部副作用的本地分析、测试、构建、lint、类型检查和内容转换；
- 读取公开且不携带凭证的资源；typed GET/HEAD 可以证明无状态变更时不询问。

“向上查找”不是一种独立危险能力。Spawn 应从当前 cwd 开始正常解析 `..`，必要时向上寻找 `.git`、`pyproject.toml`、`package.json`、`Cargo.toml` 等项目标记，并继续在用户可读目录中探索。不能要求用户预先枚举每个待读目录，也不能为每层父目录生成 grant。

以下敏感目标不因“只读”自动降为低风险：

- `.ssh`、`.gnupg`、`.aws`、`.kube`、浏览器 Profile、系统 keychain 投影；
- `.env`、token、cookie、私钥、云凭证和 PuddingClaw 内部 credential/session store；
- 能恢复认证状态或代表用户身份的数据库、配置和 IPC endpoint。

普通搜索遇到敏感目录时应跳过并记录结构化原因；只有任务确实需要时才申请精确访问。目录名本身也可能泄露信息，因此 `ls/glob/grep` 与内容读取使用同一敏感路径覆盖规则。

### 6.4 风险分级和审批动作

策略只保留四种结果：

| 等级 | 示例 | 智能模式 | 严格模式 |
| --- | --- | --- | --- |
| 低风险 | 普通探索、项目内编辑、本地测试构建、无凭证公开只读获取 | 自动放行 | 自动放行 |
| 中风险 | 无法完整解析的 effectful Shell、安装依赖、扩大 Kernel RW mount、工作区外批量写入 | 结合任务意图、目标和可恢复性自动判断 | 询问 |
| 高风险 | 敏感凭证读取、不可逆批量删除、系统配置/服务变更、代表用户修改外部状态、凭证加网络 | 询问 | 询问 |
| 禁止 | 绕过 Tool Gate、伪造 permit、未声明注入 secret、Kernel 越界、命中固定 deny policy | 拒绝 | 拒绝 |

严格模式不是“每个工具都问”，智能模式也不是“所有事情都放行”。两者只在中风险和无法可靠确定效果的操作上有差异；低风险均不干预，高风险均要求明确授权，固定禁止项均 fail-closed。

用户当前请求已经明确指定某个效果时，该请求可以作为该效果的 authority，不能紧接着重复询问同一件事。例如用户明确要求“修改这个仓库并运行测试”，项目内写入和测试执行不应再弹审批；但它不能隐含授权推送代码、删除仓库或发送凭证。

### 6.5 模式规则引擎与审批记忆

低风险应由确定性策略直接放行，不需要先弹一次窗再记忆。规则引擎服务的是中风险、用户偏好和组织约束，例如“这个项目允许 `pdftotext` 处理任意普通文档”“所有 `curl` Shell 调用都询问”“任何递归删除都拒绝”。

#### 6.5.1 规则结构

全局配置和 Project Registry 共用同一个 schema：

```json
{
  "permissions": {
    "approval_mode": "smart",
    "rules": [
      {
        "tool": "execute",
        "pattern": "pdftotext *",
        "decision": "allow",
        "scope": "project",
        "constraints": {
          "network": false,
          "credentials": false,
          "write_scope": "workspace_or_scratch",
          "destructive": false
        }
      },
      {"tool": "execute", "pattern": "curl *", "decision": "ask", "scope": "project"},
      {"tool": "execute", "pattern": "rm -rf *", "decision": "deny", "scope": "project"}
    ]
  }
}
```

规则至少包含：

- `tool`：稳定工具 id 或 effect family，必须在加载时校验，拼写错误不能静默失效；
- `pattern`：面向用户展示的程序/子命令模式，不直接作为 Shell 正则执行；
- `decision`：`allow | ask | deny`；
- `scope`：`global | project | session`；
- `constraints`：允许规则可覆盖的最大 effect envelope；省略时使用安全默认值，而不是无限能力；
- `source/revision/created_by`：用于策略来源说明、审计和失效。

Project 规则保存在 PuddingClaw 的 Project Registry，不默认写入用户仓库。只有用户显式选择导出/团队共享时才生成可提交的项目策略文件，避免一次本机点击偷偷修改源码仓库。

#### 6.5.2 匹配单位不是原始命令字符串

`execute` 先解析为规范化 `CommandPatternIdentity`：

```python
class CommandPatternIdentity:
    executable_realpath: Path
    executable_digest_or_runtime_binding: str
    program: str
    subcommand_class: str | None
    argv_shape: tuple[str, ...]
    effect_envelope: RequestedEffects
```

批准 `pdftotext report-a.pdf -` 时可生成 `pdftotext *`；批准 `git commit -m ...` 时可生成 `git commit *`。`python3 *` 只代表该 runtime binding 下、且不超过 rule constraints 的 Python 执行，不能覆盖携带网络、读取凭证、工作区外破坏性写入或 shell 注入的 Python 调用。

管道、重定向、命令替换、多个 segment 和 shell wrapper 必须逐段解析并合并 effects。`pdftotext *` 不能批准 `pdftotext a.pdf - | curl ...`，`python3 *` 也不能批准被替换 executable 或不同 Skill runtime。无法稳定解析时不得生成宽泛 project allow rule。

#### 6.5.3 求值顺序

求值按以下顺序执行：

```text
固定 hard deny / Kernel 物理越界
  > 显式 deny rule
  > 凭证 + 网络 / 不可逆高风险下限
  > 显式 ask rule
  > 满足 constraints 的 allow rule
  > smart/strict 风险默认值
```

同一配置层内允许使用“后匹配覆盖先匹配”表达从宽到窄的例外，但它不能跨越上面的安全层级：后置 allow 不能覆盖 hard deny、显式上级 deny 或高风险下限。配置合并必须保留来源顺序和 provenance，不能用数组整体替换导致上级 deny 静默消失。

#### 6.5.4 三种批准作用域与单次 Permit

只有策略结果为 ASK 时，卡片才提供：

- `仅本次`：创建 exact action grant，只允许当前 Run/Tool Call；
- `本会话`：创建 session pattern grant，在当前 Session 内匹配相同 `CommandPatternIdentity + constraints`；
- `本项目`：向 Project Registry 写入 project rule，并递增 project permission revision。

“允许规则可复用”和“ExecutionPermit 单次消费”必须同时成立：每次命中 session/project grant 后仍重新解析命令、重新计算 effects、绑定当前 runner/profile/policy revision，并签发新的单次 Permit。不能缓存或重放上一次进程创建凭证。

高风险卡片可以只提供“仅本次”，固定 deny 不显示批准按钮。涉及凭证与网络的批准使用 6.7 的专用耦合 scope，不能退化成 `python3 *`、`curl *` 等普通命令记忆。

### 6.6 Spawn 与 Kernel 的一致性

Runner 本身不增加审批。相同审批策略、相同实际副作用必须得到相同决策：

| 行为 | `spawn` | `kernel` |
| --- | --- | --- |
| 普通文件发现和读取 | 在宿主可读范围自动放行 | 在已挂载范围自动放行 |
| workspace 普通写入 | 自动放行 | 已挂载为 RW 时自动放行 |
| workspace 外读取 | 自动放行，敏感覆盖规则除外 | 不可见；需要增加只读 mount 后自动放行 |
| workspace 外写入 | 按效果和用户意图判断 | 需要 RW mount，挂载审批与写入效果合并展示 |
| Kernel 内临时文件 | 不适用 | 自动放行 |
| 宿主文件删除 | 按破坏范围判断 | 若通过 RW mount 影响宿主，使用完全相同的判断 |
| 网络 | 使用统一网络策略；Spawn 无 OS 级网络边界 | 使用统一网络策略，并编译为 profile 网络开关 |
| HOME/TMP | 使用宿主兼容环境 | 隔离到 Run scope |

Kernel 访问未挂载目录时产生的是一次“扩大沙箱可见范围”决策，不是对每次 `read_file` 的审批。批准后，同一 mount/grant 生命周期内的普通探索不再重复询问。Spawn 没有 mount 边界，因此不能模拟这类目录审批。

Kernel 的 profile 仍使用显式、可验证的权限投影：

```python
read_roots: tuple[Path, ...]
write_roots: tuple[Path, ...]
delete_roots: tuple[Path, ...]
deny_roots: tuple[Path, ...]
```

OS profile 内优先级固定为 `Deny > Delete > Write > Read > Default deny`。目录必须 canonicalize，拒绝 symlink root；进程创建前重新检查 inode/file-id 或 canonical identity。该 default deny 是 Kernel 的隔离实现，不是 Spawn 的产品探索策略。

### 6.7 凭证与网络必须耦合

只要同一执行计划既能接触凭证又能访问网络，就不能分别复用两个宽泛授权。审批对象必须同时绑定：

- typed adapter 或完整 command preview；
- credential profile id 与 fingerprint；
- 目标 endpoint/网络能力和请求动作类别；
- 是否会写入、发送、发布、购买或代表用户改变外部状态；
- 当前 Run/Tool Call、policy revision 和 runner binding。

授权可以按用户选择保存为精确 scope 的 grant，但每次实际进程创建仍签发并单次消费新的 `ExecutionPermit`。更换 credential、endpoint、动作类别、命令 digest 或 runner 后必须重新决策。

没有凭证的公开只读网络访问可以被证明为低风险时自动放行；无法证明 method/body/重定向目标的任意 Shell 网络命令至少属于中风险。读取凭证后再启动一个表面无关的网络命令，也必须由同一 Run 的 effect/taint 追踪升级为耦合审批，不能通过拆成两个 tool call 绕过。

### 6.8 外部目录工具降级为兼容入口

`execute_external_directory` 不再代表一种独立权限或 Docker 路径，只保留为 typed convenience tool：

- spawn：直接使用 canonical 宿主目录；普通读取和探索不要求 external-directory grant，写入按统一效果策略判断；
- kernel：把该目录加入本次 profile 的精确 Read/Write root，首次扩大 mount 时完成边界授权；
- 两种模式最终都使用同一份风险决策和目标 canonicalization；Kernel 额外编译并消费
  profile-bound permit，Spawn 仍由 Tool Gate + 宿主进程生命周期控制，不能把它描述成 OS
  级 permit 边界。

这里必须明确物理边界：Spawn 的 external-directory 目标、Shell grant 和路径分析只是
Tool Gate 的效果/审计语义，不是宿主 OS 隔离。被批准的 Spawn 脚本可以通过动态路径、子进程
或环境变量触达其他宿主路径；因此不把 Spawn 的 exact-directory grant 宣传为 hostile-code
边界，需要抗逃逸保证时必须选择 Kernel。

### 6.9 开源实现对齐基线

本节基于 2026-08-11 的本地源码快照进行实现级对照：Claude Code/Claw Code `b71afddae100`、Codex `1c042dd4d823`、OpenCode `550d1ffd2471`、OpenClaw `cd7b7f639da0`、grok-build `75e73f3d6ac0`、Hermes Agent `03fa32c92dd4`、DeepAgents `0.5.2`。对齐指吸收已经被实际产品验证的权限分层和低干预原则，不表示复制某个项目的全部默认值或安全取舍。

这些项目表面模式名称不同，但都可以还原为四层共同模型：

| 共同层 | 开源项目的成熟做法 | PuddingClaw 收敛结果 |
| --- | --- | --- |
| 工作档位 | 区分默认、自动编辑、询问、只读或 bypass，不把所有工具当成同一风险 | 只保留 smart/strict 审批偏好；低风险在两者下都自动，runner 另选 Spawn/Kernel |
| 规则引擎 | `allow/ask/deny` 配合 tool/program pattern，deny 或 hardline 优先 | typed rule + `CommandPatternIdentity` + effect constraints + provenance |
| 审批记忆 | once/session/project 或持久 allowlist，复用语义模式而不是完整命令字符串 | 仅 ASK 提供 once/session/project；Project Registry 与配置规则共用 schema |
| Workspace 语义 | 普通读取自动，中等档位项目内写入自动，外部副作用再升级 | 普通宿主读取、向上探索和项目内写入自动；外部写入按显式用户目标、规模和可恢复性至少作为中风险判断 |

PuddingClaw 不直接采用“workspace 外读取先询问一次”或“任意命令批准后按程序名永久放行”两种做法：前者与 Spawn 的宿主探索语义冲突，后者无法防止 `python3`、Shell pipe 或替换 executable 扩大副作用。对应能力分别改为“普通外部读取直接允许、敏感路径覆盖”和“pattern 必须受 runtime identity 与 effect constraints 约束”。

| 项目 | 源码事实 | PuddingClaw 的对齐 | 明确差异 |
| --- | --- | --- | --- |
| Claude Code/Claw Code | 运行时把 read-only、workspace-write、danger-full-access 与 prompt 分层；支持 allow/deny/ask 工具模式，deny 在规则求值前优先；workspace-write 可直接满足普通写工具 | 普通读和 workspace 写是基础能力；模式规则可以按工具和程序前缀表达，deny 保留最高优先级 | PuddingClaw 不复制其多套 permission mode，产品只保留 Spawn/Kernel runner 与 smart/strict 审批两个正交轴 |
| Codex | 文件系统策略具有 `read/write/deny`；默认 read-only profile 把文件系统 root 投影为可读，workspace-write 再增加精确写 root，因此 `ls/find/rg` 不被 cwd/root_dir 人为截断 | Spawn 普通宿主读取和向上探索默认允许；Kernel 使用 Root Read + 精确 Write/Deny 的 OS profile 思路 | PuddingClaw 不照搬所有 Codex protected-path 列表；敏感路径由自己的 credential/control-plane 分类维护 |
| OpenCode | Agent 默认 `* = allow`、`read = allow`，对 `.env` 使用 ask；Shell 会扫描 workspace 外目录并以 `external_directory` 请求扩展权限 | 默认允许工具和普通读取，敏感文件单独升级；Shell 与文件工具必须共享路径判定 | Spawn 不照搬“所有外部目录先 ask”，因为 Spawn 的宿主可达范围本来就是产品能力；Kernel 才把 external directory 解释为新增 mount |
| OpenClaw | 本地 host exec 在未配置更窄策略时使用 `security=full`、`ask=off`；sandbox host 是另一条执行边界 | Spawn 低风险宿主执行不逐次询问；runner 与 approval 分开建模 | PuddingClaw 仍保留不可绕过的高风险 Tool Gate，尤其是凭证加网络、不可逆破坏和外部状态变更，不把 `full/off` 解释成无条件安全 |
| grok-build | `PermissionRule` 原生表达 `allow/deny/ask`、tool filter 和 glob/domain pattern；具有 auto/ask/always-approve 等模式及 remembered approvals，并把缺失 action 默认成 deny | 引入同构的 typed rule、pattern、scope、provenance 与项目级记忆；配置错误 fail-closed | PuddingClaw 的 allow rule 还必须绑定 effect constraints 和 runtime identity，不能只凭字符串 glob 放行任意脚本副作用 |
| Hermes Agent | 审批中心围绕 dangerous command pattern、hardline block、smart approval、session/permanent allowlist；普通命令不因经过 Shell 就自动询问 | execute 按实际效果和危险性分级；智能模式可自动批准低风险或可恢复操作，严格模式只增加中风险询问 | PuddingClaw 不依赖命令正则作为唯一真相，而是优先使用 typed plan、路径 canonicalization、credential/network taint 和 runner profile |
| DeepAgents | `virtual_mode=True` 官方说明主要服务虚拟路径语义，不提供进程隔离；`LocalShellBackend.execute` 可访问宿主文件系统而不受 virtual root 限制；`FilesystemPermission` 只覆盖内置文件工具 | 保留 `/workspace`、`/skills`、`/scratch` 作为稳定命名空间，但由 PuddingClaw 统一授权器覆盖文件工具和 execute | 不把 DeepAgents `root_dir` 当产品安全边界；新增 host filesystem adapter 和 Kernel mount adapter 消除 read/write 与 execute 的权限分裂 |

对应源码锚点：

- Claude Code/Claw Code：`源码合集/claude-code/rust/crates/runtime/src/permissions.rs` 中的 `PermissionMode`、`PermissionPolicy` 和 deny/ask/allow 求值顺序。
- Codex：`源码合集/codex/codex-rs/protocol/src/permissions.rs` 中的 `FileSystemAccessMode`、`read_only_file_system_entries()`、`FileSystemSandboxPolicy::workspace_write()`。
- OpenCode：`源码合集/opencode/packages/opencode/src/agent/agent.ts` 中的默认 permission rules，以及 `packages/opencode/src/tool/shell.ts` 中的 external-directory scan/ask。
- OpenClaw：`源码合集/openclaw/src/agents/bash-tools.exec-run.ts` 中 host/sandbox 的 `security`、`ask` 与 approval policy 合并逻辑。
- grok-build：`源码合集/grok-build/crates/codegen/xai-grok-config-types/src/permission.rs` 的规则 schema，以及 `xai-grok-workspace/src/permission/` 的配置合并、preflight 和 shell access 求值。
- Hermes Agent：`源码合集/hermes-agent/tools/approval.py` 中的 dangerous/hardline detection、smart approval、session YOLO 和 permanent allowlist。
- DeepAgents：安装包 `deepagents/backends/filesystem.py`、`deepagents/backends/local_shell.py`，以及 `源码合集/deepagents-in-action/content/ch11-filesystem-permissions.md` 对内置文件工具覆盖范围的说明。

从这些实现提炼出的共同基线是：

1. 发现和普通读取是 Agent 的基础能力，不按单个 `ls/grep/glob` 调用审批。
2. cwd/workspace 是默认工作起点，真正的边界来自统一权限策略或 OS sandbox，而不是搜索工具自己的 root。
3. 写入、执行和网络不应按工具名粗暴二分，应根据目标、可恢复性和外部副作用判断。
4. 敏感路径、危险命令和外部状态变更采用覆盖规则；低风险默认路径保持顺畅。
5. 宿主执行和沙箱执行可以拥有不同的物理可达范围，但不应维护两套相互漂移的审批语义。

PuddingClaw 在此基线上额外增加两项约束：凭证与网络的跨工具耦合审批，以及绑定 runner/profile/policy revision 的单次 `ExecutionPermit`。这两项用于弥补单纯目录 allowlist 或危险命令正则无法覆盖的跨调用风险。

## 7. Host runtime 与 Skill 重构

### 7.1 删除 Skill 的 Docker runtime 选择

- 删除 `request_skill_runtime` 工具。
- 删除 `SkillRuntimeBindingStore` 中 `host | docker` 选择；如需保留 store，应改为保存明确的宿主 runtime binding id，而不是 runner 类型。
- Skill 不再选择隔离技术。Skill 声明的是所需软件、网络、系统能力和 runtime owner，平台负责解析执行方式。

迁移期间如果代码仍暴露 `request_skill_runtime`，它只能表示显式 managed runtime 能力，不得改变普通 Skill 的默认路径，也不得作为 Kernel 失败后的 fallback。它的审批、绑定和执行必须与普通 `install_packages`/Skill execution 分开记录；后续删除该工具时，不能删除 DeepAgents 的 `execute` 或宿主 runtime。

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

`install_packages` 在 Spawn 和 Kernel 下都必须可用，不能再按 runner 直接 DENY。它先生成完整 `DesiredDependencyMutation`，包含 owner、ecosystem、lock/desired-set diff、目标环境、来源 registry、网络和 lifecycle script 风险，再进行一次高信号决策：

- 已由用户当前请求明确要求、来源受信且无 lifecycle script/凭证的可恢复安装，smart 可以自动执行；
- 其他安装至少属于中风险，strict 询问，smart 根据 effect plan 判断；
- 带安装脚本、未知 registry、提权、全局环境写入或 credential 的安装升级为高风险；
- 批准只绑定这一份 desired-set diff，不生成 `pip *`、`npm install *` 等可复用宽泛规则；
- 每次安装事务仍使用新的单次 Permit，并在 manifest 原子切换前完成验证。

## 8. 现有特殊能力的迁移

### 8.1 Chromium HTML 报告 E2E

删除 `/opt/puddingclaw/bin/validate-html-report-e2e.mjs` 的 Docker 特判，改为 typed `HtmlValidationPlan`（当前 workspace 内已落地，外部目录已接入 Kernel runner）：

- 使用宿主托管的 Node + Playwright/Chromium binding。
- 解析 Chromium executable、浏览器 cache、字体、临时目录和报告目录。
- spawn 模式直接运行。
- kernel 模式把 executable/cache/fonts 设为只读，profile HOME/TMP 和输出目录设为可写；按验证需求决定网络是否关闭。
- 浏览器启动失败返回明确 dependency/profile 错误，不能自动换 runner。
- Spawn 读取外部 HTML 不要求目录级 grant；Kernel 则先把输入目录投影为只读 mount。两种模式的截图/报告输出都按统一写入效果判断，typed validator 不能绕过敏感路径或外部状态规则。

验收必须覆盖真实 HTML 生成、Chromium 启动、页面加载、截图/检查和产物读取，不只测命令拼接。

### 8.2 Lark 与其他托管 CLI/Connector

将 `ManagedCliService` 从 `ProjectSandboxManager` 解耦，依赖通用的 `ManagedCommandRunner`：

- adapter 产出 argv、环境、credential view、状态目录、网络和交互需求。
- Host runtime resolver 提供可执行文件。
- runner 只消费已解析 plan。
- spawn 模式使用宿主进程。
- kernel 模式只开放该 connector 的 credential/state 目录及必要网络。
- credential 只注入 typed managed command，不进入普通 Shell 全局环境。
- 凭证和网络必须耦合审批：如果 adapter 同时声明 `requires_profile` 与 `requires_network`，审批对象必须绑定 adapter/command preview、credential profile fingerprint、网络能力和破坏性标记；不能把“允许网络”或“允许凭证”单独复用到另一侧。
- adapter 声明需要凭证但没有提供 credential state contract，或未声明需要凭证却注入 credential state，都必须 fail-closed。

`backend/api/connectors.py` 不再检查 Docker 是否开启，也不再通过 Docker image digest 构造 Connector registry。

### 8.3 公共 HTTPS curl

- ShellPolicyAnalyzer 继续识别 method、body、重定向、目标和 credential/secret 来源，而不是看到网络命令就一律询问。
- 可证明为公开、无凭证、只读 GET/HEAD 的请求属于低风险：spawn 直接使用宿主网络，kernel 为本次 plan 开放网络，两者都不弹审批。
- 无法证明请求无状态变更的任意 Shell 网络命令至少属于中风险；携带凭证、上传本地内容或改变外部状态时使用 6.7 的耦合审批。
- Spawn UI 必须明确没有域名级 OS 限制；Kernel 的布尔网络开关同样不等价于域名白名单。
- 如果产品要强制“只访问批准域名”，必须增加受控代理/DNS 层；Seatbelt 或普通 spawn 的布尔网络开关本身做不到域名级限制。

### 8.4 外部目录

`execute_external_directory` 保留用户体验，但删除其 Docker 专用 backend 要求：

- Spawn 普通读取/探索不要求 external-directory grant；写入根据目标、用户意图、规模和可恢复性判定。
- Kernel profile 加入 canonical 精确目录；扩大只读/RW mount 时建立有作用域的 grant，并在执行前验证 revision。
- writable draft 继续使用精确 lease 和 stage/commit/rollback，避免验证失败留下半完成外部变更。
- Spawn 直接执行并明确提示 Tool Gate 不是 OS 隔离；Kernel 由 profile 强制 mount 边界。

### 8.5 用户项目中的 Docker

本方案不移除以下能力：

- 用户项目自己的 Dockerfile/Compose。
- MinerU 等独立服务依赖的 Docker。
- 用户明确要求运行的 Docker CLI。

但在 `kernel` 模式中开放 Docker socket 等于把宿主控制权交给沙箱进程，不能作为普通路径 grant。第一版应拒绝并提示用户切换到 `spawn`；`spawn` 模式下仍按 Tool Gate 策略执行 Docker 命令。

## 9. 配置、数据与 UI 迁移

### 9.1 新安装

- 默认 `execution_mode=spawn`。
- 默认 `approval_mode=smart`；strict 作为高级审批偏好，不改变低风险自动放行基线。
- 设置页只显示“宿主执行”和“内核沙箱”两张卡。
- 智能/严格属于独立审批策略设置，不与 Spawn/Kernel 组合成四套产品模式；无论选择哪种策略，低风险探索都不询问。
- 审批卡只在 ASK 时出现，并根据风险提供“仅本次 / 本会话 / 本项目”；不安全的 scope 不显示，固定 deny 不提供绕过入口。
- 删除 Docker connection/context/image/CPU/memory/pids/network 等沙箱设置。
- Kernel 卡显示平台 runner、probe 状态和最近失败原因。

### 9.2 旧配置

本次产品切换不保留旧配置兼容层。含有 `sandbox_mode`、`docker_enabled`、`on_unavailable`、`sandbox_mode=docker/auto` 或 `approval_mode=full_access` 的配置不会被猜测迁移；配置校验分别只接受 `execution_mode=spawn|kernel` 和 `approval_mode=smart|strict`，由用户在设置页明确选择后再继续。

### 9.3 历史 Run 和 manifest

- 历史记录中的 `adaptive`、`docker` 等内部旧 runner 名称只读保留，供审计和 UI 展示；新 Run 只写 `spawn|kernel`。
- 新 Run 只写 `configured_mode=spawn|kernel` 与真实 `effective_runner`。
- 旧 `runtime_image_digest` manifest 不直接复用；迁移器标记 stale，由宿主安装事务按需重建。
- 不批量删除用户 artifact；确认新环境可重建后再提供可恢复的清理流程。

## 10. 分阶段实施方案

### Phase 0：锁定契约和回归样例

1. 为当前关键行为补 characterisation tests。
2. 固定 DeepAgents execute 挂载、permit revalidation、Skill Python import、Node/Python 独立失效、外部目录、HTML E2E、Connector auth 的样例。
3. 建立“Docker 未安装/daemon 未启动”测试环境，作为后续所有阶段的常规门禁。
4. 建立开源行为对齐样例：普通 `ls/grep/glob/rg/find` 不询问、Spawn 向上和绝对路径探索、敏感路径覆盖、项目内编辑与测试自动执行、dangerous effect 才进入审批。

退出条件：能用测试证明当前哪些能力依赖 Docker，哪些只是被错误路由到 Docker。

### Phase 1：建立统一 execution contract 和正式 SpawnRunner

1. 新增 `ResolvedExecution`、`RuntimeBinding`、`ExecutionRunner`。
2. 把 `RestrictedHostWorkspaceBackend` 重构/重命名为正式 `SpawnRunner` 或组合式 backend。
3. `PermissionedCompositeBackend.default` 始终使用实现 `SandboxBackendProtocol` 的 execution backend。
4. 将路径映射、环境投影、超时、输出处理移到 runner-neutral 层。
5. 增加 DeepAgents 工具列表断言：spawn 与 kernel 都包含 `execute`。

退出条件：没有 Docker daemon 时，默认 spawn Run 可看到并成功调用 DeepAgents `execute`。

### Phase 1A：Spawn 低干预权限收敛（审核通过后最高优先）

1. 从 `_reviewer_eligible`、`_smart_sandbox_result`、`_smart_network_result` 移除对 Docker/Kernel 的产品语义依赖，让 Spawn 使用相同的低风险和 smart effect 判定；涉及 Kernel containment 的判断改成显式读取 isolation facts。
2. 将普通 execute 从“完整命令 exact-once”升级为 `CommandPatternIdentity + effect constraints`，并支持 once/session/project 三种 ASK 批准作用域。
3. 新增全局与 Project Registry 共用的 typed permission rules；加载时校验 tool、pattern、scope 和 constraints，保留 provenance，固定 deny 和高风险下限不可被 allow 覆盖。
4. 保持 Permit 单次消费：session/project 记忆只负责免除重复 HITL，每次执行仍重新解析、重新决策和签发 Permit。
5. Spawn 普通外部读取不再走 HostFileBroker exact-file/directory grant；Kernel 仅在首次扩大 mount 时授权。
6. 删除 Spawn 下 `install_packages` 的 runner DENY，接入 `DesiredDependencyMutation` 与宿主 runtime binding。
7. 删除 `full_access` 审批模式和旧兼容解析；新安装默认 `spawn + smart`。

实现收敛说明：Spawn 的普通宿主 `read_file/ls/glob/grep/read_resource` 直接读取，不再制造外部文件 Grant；`execute` 在 Permission Manifest 中标记为 `runtime_evaluated`，而不是预告“必然 HITL”。`pdftotext ... -`、普通只读命令和受约束的 `pypdf` AST 转换可证明为纯读取时直接执行；任意内联解释器不能因为“危险正则未命中”就被推断为安全。无法证明效果的动态解释器只产生一次高信号 Tool Gate ASK，`execute_external_directory(mode=read_only)` 则只接受明确只读命令集，不能把 Spawn 的产品标签冒充 OS 写保护。Kernel 的父目录只读 Grant 由 middleware、HostFileBroker 与 `read_resource` 统一消费，避免前置通过后执行层再次拒绝。

退出条件：重放“读取外部 PDF → `pdftotext` → Python 提取统计 → 写入 workspace → 运行验证”流程时，smart 模式为 0 次审批；strict 模式也不对其中已确定为低风险的步骤审批。将任一步替换为带 credential 上传、未知外部写入或破坏性命令时，审批/拒绝仍准确触发。

### Phase 2：双模式配置和 Kernel fallback HITL

1. 配置/API/UI 改为 `execution_mode=spawn|kernel`，默认 spawn。
2. 增加 runner availability registry 和结构化 reason code。
3. 增加 `kernel_fallback_request` registry、API、前端卡片、项目级 execution preference 和 Run override。
4. 调整 Tool Gate 顺序：策略 BLOCK 先返回；真正需要执行时解析 effective runner；随后显示与真实 runner 一致的权限卡。
5. fallback 后重新解析 execution 并签发 spawn permit。

退出条件：Kernel 不可用时，项目级切换、Run 级回退、拒绝、并发 resolve、resume payload 伪造、配置变化和 Headless 场景都不会静默 spawn。

### Phase 3：Runner-neutral Read/Write/Deny 与平台内核适配

1. 升级 `SandboxGrantProfile`。
2. 让 macOS Seatbelt 消费统一 profile。
3. 实现 Linux bwrap+seccomp runner 及真实 probe；可选叠加 Landlock。
4. 打包 WSL2 helper/Backend bootstrap、Windows UI discovery、localhost 鉴权和 ext4 workspace 引导。
5. 参数化同一套越权测试到 macOS、原生 Linux 和 Windows/WSL2，并增加 Windows UI 到 WSL Backend 的端到端测试。
6. 原生 `WindowsAppContainerRunner` 只建立独立设计/实验分支，不进入本轮稳定路径。

退出条件：macOS、原生 Linux 和 Windows/WSL2 都只有在 allow/deny probe 真实通过后才报告 kernel available；Windows 原生 Backend 会明确引导 WSL2，而不是报告一个不完整的 Kernel runner。

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

当前进度：`execute_external_directory`、HTML E2E，以及 macOS 上的 Managed Provider CLI、browser authorization CLI、Connector catalog/API/Agent 注入已完成第一版迁移；真实飞书 Toolchain 安装、凭证读取、token refresh CAS 回写和第二次独立状态查询已通过。Phase 5 尚未满足跨平台退出条件：Linux/WSL2 managed runner、browser job 跨进程恢复及 Docker 未安装门禁仍需补齐。

### Phase 6：删除 Docker 沙箱实现和兼容代码

1. 删除 `AdaptiveWorkspaceBackend`、`DockerWorkspaceBackend`、`ProjectSandboxManager`。
2. 删除 sandbox Dockerfile、镜像构建/探测脚本、sandbox compose 生命周期。
3. 删除配置、API、UI、prompt 和测试中的 sandbox Docker 选项。
4. 删除 `runtime_image_digest` 的新运行路径，只保留历史数据 reader/migrator。
5. 将 Docker E2E 改为 spawn/kernel contract E2E。

退出条件：源码静态扫描中，Sandbox execution 路径不再出现上述 class 或 runner 值；保留的 Docker 引用均有明确的非沙箱 owner；macOS、原生 Linux、Windows/WSL2 三条首发路径全部通过发布门禁。

### Phase 7：资源和网络硬化

Docker 删除后仍需显式补齐其曾提供的资源控制：

- macOS：rlimit + watchdog + 进程组。
- Linux：rlimit/cgroup v2（可用时）+ namespace + seccomp。
- Windows/WSL2：由 Linux cgroup/rlimit/进程 namespace 承载；原生 Windows runner 后续使用 Job Object。
- 所有平台：输出上限、超时、取消传播和孤儿进程清理。
- 域名级网络策略需要独立的 managed proxy，不与 kernel filesystem sandbox 混为一谈。

## 11. 文件级改动清单

| 区域 | 主要文件 | 目标改动 |
| --- | --- | --- |
| 配置 | `backend/config.py`、`backend/config.json*` | 只保留 `execution_mode=spawn|kernel`、`approval_mode=smart|strict`，新增 typed permission rules，不保留旧值兼容 |
| 设置页 | `frontend/src/lib/settingsApi.ts`、`frontend/src/app/settings/page.tsx` | 两张模式卡、Kernel probe、fallback 说明，删除 Docker 表单 |
| Backend | `backend/harness/workspace_backends.py` | 统一 execution backend + runner composition，最终删除 Docker/Adaptive/Manager |
| Kernel | `backend/harness/kernel_sandbox.py` | runner registry、平台实现、真实 probe |
| Profile | `backend/harness/sandbox_profiles.py` | Read/Write/Delete/Deny、稳定 digest、spawn 前验证 |
| Permit | `backend/harness/execution_permits.py`、`execution_context.py` | 绑定 resolved execution、runtime、runner 和 fallback revision |
| Tool Gate | `backend/harness/tool_execution.py` | Spawn 启用 smart；统一 effect 判定；新增 pattern identity/constraints；支持 once/session/project；安装事务删除 runner DENY |
| Agent wiring | `backend/graph/deepagents_manager.py` | 始终挂 execution backend；删除 Docker mount envelope；记录 configured/effective mode |
| Composite | `backend/graph/permissioned_filesystem_backend.py` | 外部目录走统一 execution contract，删除 Docker 错误文案 |
| Permission policy | `backend/graph/permission_policy.py` | typed allow/ask/deny rules、来源优先级、风险下限、pattern grant binding；移除 `full_access` |
| HITL/API | `backend/api/permissions.py` 及 project/session/resume manager | 三档批准作用域、Project Registry rule 持久化、policy revision、项目 execution preference 与 Kernel fallback authority |
| Windows/WSL2 | Windows/Electron 启动层、WSL2 部署文档与 discovery | 文档化安装/升级 Backend、localhost 鉴权、distro/workspace identity、probe 和故障引导 |
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
- 用户选择“将本项目切换为 spawn”后，只有该项目的后续 Run 使用 spawn，其他项目不受影响且不再重复询问。
- 用户选择“仅本次 Run 回退”后，项目仍保持 kernel，下一个 Run 重新按 probe 状态处理。
- 用户拒绝后没有进程被创建。
- 伪造 resume payload、重复 resolve、旧 config revision、旧 probe fingerprint 均不能授权 spawn。
- Kernel permit 不能被 SpawnRunner 消费。

### 12.3 目录边界

- Spawn 下 `ls/grep/glob/read_file` 与 Shell `find/rg/cat` 对同一路径得到一致结果；普通绝对路径和 `..` 向上探索不产生审批。
- Spawn 下普通 PDF/文档读取、项目内编辑以及本地测试/构建不产生审批。
- 严格模式和智能模式都自动放行低风险探索；两者只在中风险和不确定效果上产生差异。
- Kernel 允许 workspace/scratch 和显式外部 grant。
- Kernel 拒绝未授权 HOME、其他项目和 PuddingClaw credential/session store。
- Kernel 新增 mount 只审批一次边界扩大；批准后的普通读取不逐文件、逐工具重复询问。
- symlink 替换、目录移动和 TOCTOU 场景在 spawn 前使 permit 失效。
- spawn UI、日志和结果不把策略授权表述为 OS 隔离。
- 同一宿主副作用经 Spawn 直接访问或 Kernel RW mount 访问时，审批结果一致。
- 凭证读取与后续网络调用即使拆成多个 tool call，也会触发同一个耦合审批策略。

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
- Spawn/Kernel 均可无审批执行可证明为无凭证、只读的公共 HTTPS GET；携带凭证或产生外部副作用时进入耦合审批。
- Kernel 模式不开放 Docker socket；spawn 模式仍能在用户授权后运行项目 Docker 命令。

### 12.6 兼容与运维

- 历史 Run 可读。
- legacy 配置不会静默降级。
- WSL2 部署文档覆盖 bootstrap/升级/distro restart；WSL2 Backend 的 localhost 鉴权、workspace identity、Kernel probe 和 OAuth callback 有真实 E2E。
- Windows 原生 Backend 的 Kernel 入口明确引导 WSL2；未部署时可以项目级切换 spawn，不会每个 Run 重复询问。
- 取消/超时后没有孤儿进程。
- 日志始终记录 configured mode、effective runner、probe、fallback authority、runtime binding 和 permit digest，但不记录 secret。

### 12.7 审批规则与记忆

- 新 Session 默认 `spawn + smart`；Spawn 下 smart reviewer、workspace execute 和公开只读网络路径不会因 runner 检查失效。
- 低风险命令直接 ALLOW，不创建“先批准一次”的伪记忆；普通 `pdftotext`、Python 本地解析和测试构建均为 0 弹窗。
- 一个中风险命令选择“仅本次”后只消费一次；选择“本会话”后相同 pattern/constraints 在当前 Session 复用；选择“本项目”后新 Session 也能命中 Project Registry rule。
- `pdftotext a.pdf -` 与 `pdftotext b.pdf -` 可以命中同一安全 pattern；`pdftotext a.pdf - | curl ...`、替换 executable、增加 credential/network 或扩大 write scope 不命中。
- `python3 *` allow 只覆盖同 runtime binding 和 constraints 内的效果，不能批准读取 secret 后联网、工作区外破坏性写入或不同 Skill interpreter。
- hard deny、显式 deny、凭证加网络和不可逆高风险不能被 session/project allow rule 覆盖。
- 配置中未知 tool、非法 glob、缺失 decision、无效 scope 或不完整 constraints 会在加载时失败并给出可诊断错误，不静默失效或 fail-open。
- 多层配置合并保留上级 deny 与规则 provenance；后层配置不能通过空数组静默清空强制策略。
- project rule、session grant 或 approval mode 变化会递增 permission revision，使旧 Permit 失效；新的执行仍签发并单次消费 Permit。
- Spawn 下 `install_packages` 不因 runner 被拒绝；批准与实际执行绑定同一 desired dependency diff、runtime owner、registry 和 lifecycle-script 风险。
- PDF 回放基线：读取外部 PDF、文本提取、Python 分析、workspace 输出和验证在 smart/strict 中均为 0 次审批；加入真实中风险步骤时至多产生一次有意义审批，而不是按文件和命令字符串重复询问。

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
4. Kernel 不可用时，未经用户明确选择项目级 spawn 或 Run 级临时回退绝不 spawn。
5. 同一份 `ResolvedExecution` 可被不同 runner 消费，runner 不推断或修改 Skill runtime。
6. 权限批准与 OS 隔离相互独立，permit 在进程创建前重新验证。
7. Python、Node、CLI 和 Browser 分别拥有宿主 runtime identity，不再依赖 Docker image digest。
8. HTML 验证、外部目录、Managed CLI、Lark 和 curl 都不依赖 Docker 沙箱。
9. macOS、Linux、Windows/WSL2 只有 probe 通过才报告 Kernel 可用；Windows 原生首发明确引导 WSL2，不虚构 Restricted Token 目录边界。
10. 删除的是 PuddingClaw 沙箱 Docker，不是用户项目或独立服务对 Docker 的合法使用。
11. Spawn 的普通宿主探索、向上搜索、项目内编辑和低风险本地执行不弹审批；严格模式也不改变这一基线。
12. DeepAgents 虚拟目录只承担稳定路径和 backend 路由，不能成为文件工具独有且 execute 可绕过的权限边界。
13. 文件工具与 Shell 对相同 read/write/delete/network/credential effect 使用同一个授权结果。
14. Smart 在 Spawn 下完整生效；审批便利不依赖 Docker/Kernel，也不被描述为 OS 隔离。
15. 可复用的是带 pattern、constraints、scope 和 revision 的授权规则；每次进程创建 Permit 始终单次消费。
16. 新安装默认 `spawn + smart`，审批策略只保留 smart/strict，低风险在两者下都不干预。
17. `install_packages` 通过统一宿主 runtime 安装事务运行，不再因 Spawn 默认模式被拒绝。

## 15. 平台设计依据

- [OpenAI Codex Linux sandbox README](https://github.com/openai/codex/blob/main/codex-rs/linux-sandbox/README.md)：现实项目中的 bwrap-first、只读根、嵌套路径策略、`no_new_privs`、seccomp、network namespace 和 WSL1/WSL2 边界。
- [Bubblewrap 官方仓库](https://github.com/containers/bubblewrap)：mount/user/PID/network namespace、seccomp、`--new-session` 和可见文件系统由显式 bind 构造的安全模型。
- [Linux Kernel Landlock 文档](https://docs.kernel.org/userspace-api/landlock.html)：运行时 ABI/errata 探测、无特权进程自我限制以及 `no_new_privs` 前提。
- [Linux Kernel no_new_privs 文档](https://docs.kernel.org/userspace-api/no_new_privs.html)：阻止 `execve` 通过 setuid/setgid/file capability 扩权，并允许无特权安装 seccomp filter。
- [Microsoft AppContainer isolation](https://learn.microsoft.com/en-us/windows/win32/secauthz/appcontainer-isolation)：原生 Windows 后续方案中文件、credential、network、process 和 device isolation 的能力边界。
- [Microsoft Restricted Tokens](https://learn.microsoft.com/en-us/windows/win32/secauthz/restricted-tokens)：Restricted Token 的双重 access check 及其仍依赖 securable object DACL 的事实。
- [Microsoft Job Objects](https://learn.microsoft.com/en-us/windows/win32/procthread/job-objects)：进程树、资源限制、breakaway 与 `KILL_ON_JOB_CLOSE` 语义；也说明 Job Object 不是文件系统权限模型。
