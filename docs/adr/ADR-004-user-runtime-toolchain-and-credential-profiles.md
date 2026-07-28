# ADR-004：用户级 Toolchain、Credential Profile 与沙箱容器生命周期

| 字段 | 内容 |
|------|------|
| 编号 | ADR-004 |
| 标题 | 建立用户级共享 Toolchain 与 Credential Profile，解耦 Session/Project 沙箱 |
| 状态 | **Accepted** |
| 日期 | 2026-07-27 |
| 作者 | PuddingClaw Team |
| 相关模块 | `backend/harness/workspace_backends.py`, `backend/harness/tool_execution.py`, `backend/projects/registry.py`, `backend/runtime_identity/*` |

---

> Credential Profile 的交互式创建、修复、多阶段授权、自然语言续跑和前端授权卡生命周期由 [ADR-005](ADR-005-managed-external-authorization-flow.md) 进一步约束。

## 0. 实施状态

截至 2026-07-27，控制面核心已经开始落地：

- `backend/runtime_identity/adapters.py`：Adapter-first 的独立 argv 解析、飞书命令分类和未知全局工具安装拒绝。
- `backend/runtime_identity/toolchains.py`：用户级 Node Toolchain、跨进程安装锁、release staging 和 `current` 原子切换。
- `backend/runtime_identity/profiles.py`：Profile 注册表、Project binding、Keychain/fallback master key、AES-256-GCM 保险库和归档安全校验。
- `backend/runtime_identity/service.py`：冻结 owner/Profile revision/Toolchain revision 的执行计划、Installer/Provider 分发、BrowserAuth 生命周期回收、exit-10 确认协议和输出脱敏。
- `backend/harness/tool_execution.py`：在 workspace handler 前认领 managed CLI，审批后只交给控制面；浏览器授权只能总结并结束当前 Agent 轮次。
- `backend/harness/workspace_backends.py`：独立 Installer/Provider runner 契约、普通容器遮蔽 `.lark-cli`、空闲删除和启动 GC。

尚未完成：旧 runtime-home 凭证迁移 UI、Credential Profile/Toolchain 设置页、人工清理旧 volume，以及真实飞书账号的端到端验收。这些不能由启动 GC 自动推断或删除。

## 1. 结论

PuddingClaw 建立一个**用户级资源域**，但不建立一个包办所有任务的长期“顶层容器”。

- 通用 CLI/包属于用户级 Toolchain，跨所有 Session 和 Project 共享。
- 飞书等第三方授权属于用户级 Credential Profile，跨 Project 默认复用。
- Project 只保存 Profile 引用，不拥有、复制或直接读取凭证。
- 普通 Session/Project 容器继续隔离工作区和项目依赖，但不再保存重要的 CLI 或授权状态。
- 凭证只在受控的 provider CLI 调用期间挂载，普通项目命令永远看不到 token。
- 空闲容器必须删除而不是仅停止；用户拥有的持久状态放在宿主用户的 PuddingClaw 数据目录，项目依赖继续使用可重建的 Docker volume。

边界公式：

```text
Session 决定临时工作区
Project 决定项目依赖与默认 Profile 引用
User 决定 Toolchain 和 Credential 所有权
Credential Profile 决定第三方 App、租户、用户身份与权限
```

这里遵循“**使用权不等于读取权**”：Session/Project 中的 Agent 可以请求执行飞书操作，但该请求会在进入普通 workspace 容器前被 Backend 截获并转交专用 Lark runner。普通容器中的项目脚本不能 `cat` token，也不能绕过 Backend 直接获得 Secret State。

## 2. 当前问题

当前 `/home/puddingclaw` 整体挂载到按 workspace、镜像 ID 和 runtime contract 生成的 Docker volume：

```text
puddingclaw-runtime-home-<workspace-image-contract-hash>
  ├── .npm-global/       # lark-cli 等全局包
  ├── .lark-cli/         # 飞书 App 配置、登录态、缓存和日志
  └── .cache/
```

无 Project 会话的 workspace 是：

```text
backend/data/agent-workspaces/unscoped/<session-id>
```

因此每个临时对话都会得到不同的容器和 runtime-home volume。现有 idle timer 只执行 `docker stop`，不会删除容器，导致 Docker Desktop 中持续积累 `puddingclaw-project-*`。

## 3. 持久化位置

### 3.0 用户数据根目录

Toolchain、Credential Profile 注册表和加密凭证保险库属于宿主用户，统一使用宿主用户 Home 下的 PuddingClaw 数据目录，而不是仓库、Project、Session workspace 或 Docker VM：

```text
~/.puddingclaw/
```

Backend 启动时将 `~` 展开为宿主用户 Home 的绝对路径。开发、便携部署和测试可通过 `PUDDINGCLAW_HOME` 覆盖，但必须是宿主绝对路径。以当前用户为例，默认实际路径是：

```text
/Users/pet/.puddingclaw/
```

Windows 对应 `%USERPROFILE%\.puddingclaw\`。设置页必须提供“打开 PuddingClaw 数据目录”，用户不需要理解 Docker volume。

### 3.1 用户级 Toolchain

这里的 **Toolchain 不是 Python 函数，也不是容器**。它是一个由 PuddingClaw 管理的、跨容器共享的 CLI 运行环境，物理上首先是宿主目录：

宿主路径：

```text
~/.puddingclaw/runtime/toolchains/node/<runtime-contract-hash>-<arch>/
```

目录内容类似：

```text
node/<contract>-<arch>/
├── bin/
│   ├── lark-cli
│   └── 其他全局 CLI
├── lib/node_modules/
│   └── @larksuite/cli/
└── toolchain-manifest.json
```

完整概念由三部分组成：

```text
Toolchain Resource   宿主共享目录，保存 CLI 和运行库
ToolchainManager     Backend Python 类，负责定位、加锁、版本与挂载契约
Installer Container 临时执行 npm/pipx/uv 等真实安装命令
```

建议的 Backend 接口是类而不是散落函数：

```python
class ToolchainManager:
    def resolve(self, ecosystem: str, runtime_contract: str, arch: str) -> ToolchainRef: ...
    def mount(self, ref: ToolchainRef, mode: Literal["ro", "rw"]) -> MountSpec: ...
    def install(self, ref: ToolchainRef, request: PackageInstallRequest) -> InstallResult: ...
    def list_packages(self, ref: ToolchainRef) -> list[InstalledPackage]: ...
```

`ToolchainManager.install()` 自身不在 Backend 宿主进程里运行 npm；它持有锁并创建 Installer Container，由后者把安装结果写入 Toolchain Resource。

容器内固定挂载点：

```text
/opt/puddingclaw/toolchain/node
```

环境变量：

```text
npm_config_prefix=/opt/puddingclaw/toolchain/node
NODE_PATH=/opt/puddingclaw/toolchain/node/lib/node_modules
PATH=/opt/puddingclaw/toolchain/node/bin:...
```

规则：

- 普通 Session/Project 容器只读 bind-mount 该目录。
- `npm install -g`、CLI 更新只能由受控 installer 容器以读写方式挂载。
- installer 按 Toolchain 目录加跨进程锁，防止并发更新损坏。
- 目录 key 不包含 workspace、session、project 或镜像 ID；只包含兼容性边界 `RUNTIME_CONTRACT + CPU architecture`。
- 项目 `node_modules`、Python venv 等仍使用 Project 专属依赖卷，不进入 Toolchain。

### 3.2 Credential Profile 注册表

非敏感元数据保存在宿主用户数据根目录：

```text
~/.puddingclaw/users/<owner-user-id>/credentials/credential-profiles.json
~/.puddingclaw/users/<owner-user-id>/credentials/project-bindings.json
```

注册表只保存：

```json
{
  "profile_id": "lark_default",
  "owner_user_id": "local",
  "provider": "lark",
  "label": "我的飞书",
  "brand": "feishu",
  "app_id_fingerprint": "sha256:...",
  "remote_identity_hint": "ou_...masked",
  "sharing_policy": "user",
  "status": "active",
  "created_at": 0,
  "updated_at": 0
}
```

注册表禁止保存 App Secret、access token、refresh token 或完整授权响应。

### 3.3 飞书 Credential Profile

每个 Profile 使用独立的宿主保险库目录：

```text
~/.puddingclaw/users/<owner-user-id>/credentials/lark/<profile-id>/
  ├── profile.json       # 非敏感、脱敏后的连接元数据
  └── vault.enc          # 加密后的 Adapter Credential State archive
```

保险库主密钥优先保存在操作系统 Keychain/Credential Manager，service 为 `PuddingClaw Credential Vault`、account 为 `owner_user_id`。无系统 Keychain 的环境才允许使用权限为 `0600` 的本地 fallback key，并在设置页显示安全降级提示。主密钥不得与 `vault.enc` 一起同步、导出或进入 Project。

`vault.enc` 不能直接挂载给 CLI。受控 Lark runner 的调用过程是：

1. Backend 在内存中解密保险库。
2. 将 Secret State 流式注入 runner 的 tmpfs，不写入宿主临时明文文件。
3. Runner 根据 `CredentialStateSpec` 在 HOME tmpfs 中恢复 Adapter 声明的精确目录。Lark v1 状态契约为：

```text
/home/puddingclaw/.lark-cli
/home/puddingclaw/.local/share/lark-cli  # Linux keychain 旧/降级路径
```

   同时由 Backend 强制注入：

```text
LARKSUITE_CLI_DATA_DIR=/home/puddingclaw/.lark-cli/.credential-data
```

   这使新版本 Linux 文件 Keychain 的 `master.key` 和 `*.enc` 收敛到 `.lark-cli` 内；旧路径仍作为 Adapter 精确白名单保留，不能扩展成任意 HOME 归档。

4. `lark-cli` 退出后，Backend 只从 `CredentialStateSpec.paths` 流式读取更新后的状态，在内存中重新加密并原子替换 `vault.enc`。
5. 销毁 runner 和 tmpfs，释放 Profile 锁。

`/home/puddingclaw/.lark-cli` 仍是 `lark-cli` 在容器内看到的路径，但持久真相源是宿主 Home 下的加密保险库。普通工作区容器不挂载保险库或解密后的 Secret State。

`CredentialStateSpec` 是 Adapter-first 的安全契约，而不是 Runner 的自由参数。它声明 schema version、HOME 相对状态根、Backend-owned 环境变量和稳定 fingerprint；同一对象统一驱动 BrowserAuth/Provider 的目录创建、归档导入导出、Vault 校验、普通容器秘密路径遮蔽以及 Browser job 跨重启恢复。状态路径必须规范化、不可重叠，归档只允许普通文件和目录，并拒绝绝对路径、父目录跳转、链接、设备文件和重复成员。运行中的 Browser job 把 fingerprint 同时冻结在 Profile lease 和容器 label 中，部署后契约不一致时 fail closed，不能用新规范收集旧容器。

### 3.4 Credential Profile 到底是什么

Credential Profile 是 PuddingClaw 中一个**有名字的第三方身份连接**。它把“哪个 PuddingClaw 用户，通过哪个第三方 App，登录了哪个远端账号/租户，并把凭证保存在哪里”绑定成一个可选择、可验证、可撤销的逻辑对象。

以飞书为例，一个 Profile 可以理解为设置页中的一张账号连接卡片：

```text
名称：我的飞书
Profile ID：lark_default
Provider：Lark/Feishu
App：cli_xxx（只显示脱敏标识）
远端身份：张三 / ou_xxx（只显示安全摘要）
状态：有效 / 已过期 / 待配置 / 已撤销
使用范围：用户默认
被哪些 Project 引用：PuddingClaw、销售分析
凭证位置：用户 Home 下的 PuddingClaw 加密凭证保险库
```

它由两部分组成：

```text
Credential Profile
├── Registry Record
│   ├── profile_id、显示名称、owner_user_id
│   ├── provider、brand/tenant/app 的脱敏摘要
│   ├── 状态、默认项、sharing policy、Project 引用
│   └── encrypted vault 的内部引用
│
└── Secret State
    └── provider CLI 的真实配置、access token、refresh token
        持久化时只存在 vault.enc；运行时仅短暂存在于 provider runner tmpfs
```

Profile **不是**：

- 不是 Agent Profile、模型 Profile、任务 Profile 或 Skill。
- 不是 Docker 容器；删除容器不会删除 Profile。
- 不是 Project 的一份配置副本；Project 只引用 `profile_id`。
- 不是单个 access token；token 刷新后仍是同一个 Profile。
- 不是一次授权链接或 device code；这些只是建立/更新 Profile 的临时材料。

一个 Profile 在语义上绑定：

```text
owner_user_id
+ provider（lark）
+ brand/tenant
+ third-party app identity
+ remote user identity（如果是用户授权）
```

如果 App、租户或远端用户身份发生变化，应创建或显式切换 Profile，不能把旧 Profile 静默改造成另一个身份。仅 token 到期、刷新或重新授权不创建新 Profile。

生命周期：

```text
待配置
  -> 已配置但未登录
  -> 有效
  -> 已过期（可重新授权并恢复为同一个 Profile）
  -> 已撤销
  -> 已删除（需确认所有 Project 引用）
```

PuddingClaw 的 Profile 可以映射到 provider CLI 自带的 named profile，但不能依赖 provider 的命名或存储实现；PuddingClaw 的 `profile_id`、owner 校验、Project binding 和影响范围始终由 Backend 注册表负责。

## 4. Profile 选择规则

选择由 Backend 根据可信运行时上下文完成，Agent 不猜测路径或 Profile：

```python
profile_id = (
    request.explicit_credential_profile_id
    or project_bindings.get(project_id, {}).get("lark")
    or user_defaults.get(user_id, {}).get("lark")
)
```

优先级：

1. 用户在本次请求/UI 明确选择的 Profile。
2. 当前 Project 显式绑定的 Lark Profile。
3. 当前 PuddingClaw 用户的默认 Lark Profile。
4. 不存在时创建待配置的 `lark_default`，再启动首次授权。

### 4.1 无 Project 对话

始终使用当前用户默认 Profile：

```text
local -> lark_default
```

Session 只影响 workspace，不影响 CLI 或授权选择。

### 4.2 Project 对话

Project 没有显式绑定时，同样使用用户默认 Profile，因此在任一 Project 中完成的默认飞书授权可被其他 Project 和无 Project 对话使用。

Project 需要不同租户、App、用户身份或权限范围时，用户显式创建/选择另一个 Profile，并保存引用：

```json
{
  "proj_xxx": {
    "lark": "lark_company_a"
  }
}
```

绑定只是引用；凭证仍归用户所有。其他 Project 只有在用户显式选择或绑定后才使用该非默认 Profile。

### 4.3 在 Project 中发起授权

- `auth login`、token 刷新：更新当前已解析的 Profile。
- 首次配置且不存在 Profile：创建用户默认 Profile。
- 已有共享 Profile 时执行 `config init --new`：不得静默覆盖；默认创建新 Profile，并在 UI 展示会受影响的已绑定 Project。
- `logout`、`config remove`、删除 Profile：展示该 Profile 的绑定 Project，按 Profile 范围确认，不能按当前 Project 误判影响范围。

## 5. 调用链

### 5.1 普通命令

```text
Agent execute("npm test")
  -> WorkspaceBackend
  -> Session/Project 容器
       /workspace                         当前工作区
       /opt/puddingclaw/toolchain/node    共享 Toolchain，只读
       不挂载 Credential Profile
```

### 5.2 安装或更新通用 CLI

```text
Agent execute("npm install -g @larksuite/cli")
  -> ToolExecutionMiddleware 结构化识别全局安装
  -> Skill Manager/安装审批（如适用）
  -> Toolchain installer lock
  -> 临时 installer 容器
       ~/.puddingclaw/runtime/toolchains/... 读写挂载
       无 Credential Secret State
  -> 安装完成后销毁容器
```

安装一次后，所有现有和未来容器都能从共享 PATH 找到 `lark-cli`。

Installer 容器设置：

```text
npm_config_prefix=/opt/puddingclaw/toolchain/node
```

其中 `/opt/puddingclaw/toolchain/node` 是宿主 `~/.puddingclaw/runtime/toolchains/node/<contract>-<arch>` 的读写 bind mount。因此 `@larksuite/cli` 最终直接持久化为：

```text
~/.puddingclaw/runtime/toolchains/node/<contract>-<arch>/bin/lark-cli
~/.puddingclaw/runtime/toolchains/node/<contract>-<arch>/lib/node_modules/@larksuite/cli/
```

它不进入 installer 容器 layer，容器删除后仍存在。

### 5.3 飞书命令

```text
Agent execute("lark-cli auth status --json --verify")
  -> ToolExecutionMiddleware 在普通 workspace 容器执行前解析并截获 argv
  -> CredentialProfileResolver(user_id, project_id, explicit_profile_id)
  -> 获取 Profile 级锁
  -> 临时 Lark runner
       ~/.puddingclaw/runtime/toolchains/...  只读挂载
       当前 Profile 解密状态                仅注入 tmpfs
       当前 workspace                    按命令所需最小权限挂载
       entrypoint=lark-cli               不启动通用 shell
  -> 将更新后的 tmpfs 状态重新加密、原子写回 vault.enc
  -> 返回脱敏后的结构化结果
  -> 销毁 runner，释放锁
```

因此从 Agent/UI 看，仍然是在当前 Session 或 Project 中调用 `lark-cli`；从安全实现看，真正接触凭证的是短生命周期的专用 runner。runner 挂载当前 workspace，所以飞书命令读取或生成的业务文件仍出现在当前 Project/Session 中。普通容器只负责非 provider 命令；即使项目代码能找到 `lark-cli` 二进制，也没有 Credential Secret State，带身份的调用必须经过上述拦截链路。

允许识别少量 allowlisted 环境变量前缀，例如 `LARKSUITE_CLI_NO_UPDATE_NOTIFIER=1`。禁止通过 `sh -c`、管道、命令替换或拼接绕过专用 runner；需要重定向的日志由 runner 自己合并。

Provider Runner 使用个人自治策略：Adapter 已冻结的 Lark 非删除操作默认获得 Provider 网络并直接执行，不再逐次发起联网 HITL。它覆盖消息发送、文档创建/更新、多维表格更新、上传和分享权限修改。共享 Toolchain 安装仍需确认；删除资源、清空内容、移除本地配置和注销登录仍需确认。

当非删除命令返回 lark-cli 的 exit 10 时，Backend 只接受 `ok=false`、`type=confirmation`、`subtype=confirmation_required`、`risk=high-risk-write` 且 action 与冻结 argv 一致的结构化信封，然后对同一 argv 仅追加一次 Backend-owned `--yes`。删除类 action 则把 canonical argv、Profile revision、Toolchain revision 和 confirmation action 绑定到 HITL，批准后才能重试。模型提供的任何 `--yes` 形态都被 Adapter 拒绝。

浏览器授权按 CLI 能力分两类。`auth login --no-wait --json` 继续采用非阻塞 split-flow；`config init --new` 是真实阻塞命令，由专用 BrowserAuth Runner 保持。两者输出 `Status: awaiting_user_browser` 后，Graph 都只允许模型生成一次二维码/链接总结并结束当前轮；退出码 0 只表示授权流程已发起。

### 5.4 如何截获：控制面路由，不做容器内 hook

截获发生在 Backend 的 `ToolExecutionMiddleware`，时点早于 `WorkspaceBackend.execute`：

```text
模型发出 execute tool call
  -> Harness policy / permission review
  -> ToolExecutionMiddleware 结构化解析 command
       ├─ global toolchain mutation -> ToolchainInstaller
       ├─ provider command -> ProviderCommandRunner / BrowserAuthRunner
       ├─ managed skill add -> Skill Manager
       └─ ordinary command -> WorkspaceBackend
```

`ToolExecutionMiddleware` 只负责通用解析与分发，不理解飞书、GitHub、AWS 等 CLI 的业务语义。每个需要共享安装、用户凭证或交互式生命周期的 CLI 必须提供一个 Backend `ManagedCliAdapter`；飞书只是首个 Adapter。

```python
class ManagedCliAdapter(Protocol):
    adapter_id: str
    executables: set[str]

    def recognize(self, argv: list[str]) -> bool: ...
    def classify(self, argv: list[str]) -> ManagedCliAction: ...
    def resolve_profile_kind(self, action: ManagedCliAction) -> str | None: ...
    def required_mounts(self, action: ManagedCliAction) -> MountContract: ...
    def network_contract(self, action: ManagedCliAction) -> NetworkContract: ...
    def lifecycle(self, action: ManagedCliAction) -> LifecycleContract: ...
    def redact_output(self, output: bytes) -> bytes: ...
    def verify_state(self, profile_id: str) -> VerificationContract: ...
```

职责分层：

```text
ToolExecutionMiddleware
  ├── 解析独立 argv、拒绝不可证明的 shell 组合
  ├── 处理通用权限与审计
  └── 查询 ManagedCliRegistry

ManagedCliRegistry
  └── executable -> ManagedCliAdapter

ManagedCliAdapter（每个 CLI 独立）
  ├── 定义子命令分类和是否需要 Credential Profile
  ├── 定义安装/更新、网络、交互等待和验证协议
  ├── 定义 provider 状态如何导入/导出保险库
  └── 定义脱敏规则
```

Adapter 规则是受测试约束的 Backend 代码/声明，不写进第三方 Skill，也不依赖模型理解。Skill 只教 Agent 什么时候及如何使用 CLI；Adapter 决定命令实际在哪里执行、能否接触凭证以及如何持久化。

每条 Adapter action 至少声明：

```text
rule_id
executable + argv matcher
route
credential_mode
toolchain_mode
network_mode
interactive_lifecycle
```

飞书 `LarkCliAdapter` 的首批 action：

| 解析后的独立命令 | Route | Toolchain | Credential | 说明 |
|---|---|---:|---:|---|
| `lark-cli --version/--help/schema ...` | Provider Runner | 只读 | 无 | 无身份查询 |
| `lark-cli config show`、`auth status`、业务命令 | Provider Runner | 只读 | 当前 Profile | 普通短命令 |
| `lark-cli config init --new`、需等待浏览器的登录 | Browser-auth Runner | 只读 | 当前 Profile | 返回 awaiting 状态并保持进程 |
| `lark-cli update` | Coordinated Updater | 读写 | 按子步骤 | CLI 更新走 Installer；Skills 更新走 Skill Manager |

`npm install -g @larksuite/cli` 命中的是“全局 Toolchain 变更”规则；安装完成后，后续所有 `lark-cli ...` 命中的是“受管 provider executable”规则。两者是两次独立路由。

通用全局包安装属于独立的 `ToolchainPackageAdapter`，负责识别 npm/pipx/uv 等全局包变更；它不理解飞书凭证。CLI Adapter 在 manifest 中声明自己的发行包，例如：

```json
{
  "adapter_id": "lark-cli",
  "executables": ["lark-cli"],
  "distribution": {
    "ecosystem": "npm",
    "package": "@larksuite/cli"
  },
  "credential_provider": "lark"
}
```

#### 全局 Toolchain 与 Project 依赖如何区分

共享 Toolchain 是 Adapter-first 的白名单资源，不是任意用户级 package prefix。Backend 首先用 `ManagedCliRegistry` 判断请求是否命中受信 Adapter，再由该 Adapter 声明安装方式、目标 Toolchain 和允许的 argv。`-g/--global` 只是 npm 型 Adapter 对自身安装命令的校验细节，不是进入控制面的前置条件，也不是通用路由信号。

| 命令形态 | 作用域 |
|---|---|
| Adapter manifest 声明的 `npm ... -g <distribution>` | Adapter 指定的用户共享 Node Toolchain |
| Adapter manifest 声明的 `uv tool`、`pipx`、二进制下载或自更新命令 | Adapter 指定的用户共享 Toolchain |
| 未命中 Adapter 的 `npm install/add ... -g` | 不进入共享控制面；拒绝跨容器安装并提示本地安装或新增受信 Adapter |
| `npm install/add ...`（无 global flag） | 当前 Project 依赖 |
| `npm ci` | 当前 Project 依赖 |
| `pipx install/uninstall/upgrade ...` | 用户共享 Python CLI Toolchain |
| `uv tool install/upgrade/uninstall ...` | 用户共享 Python CLI Toolchain |
| `uv sync`、`pip install -r ...`、项目 `.venv` | 当前 Project 依赖 |
| `cargo install ...` | 用户共享 Cargo CLI Toolchain（启用对应 Adapter 后） |
| `apt/apk` 系统包 | 不进入用户 Toolchain；应更新 sandbox image |

对使用 npm 发行的某个已注册 Adapter 而言，`-g/--global` 可以是该 Adapter 的必要安装参数；它必须是 argv 中 package-manager 语义上的 flag，不能从包名、脚本文本或引号内容中误匹配。但其他 Adapter 可以使用 `uv tool`、`pipx`、已校验二进制下载或 CLI 自更新，完全不需要 `-g`。`pip install --user` 在容器环境中作用域含糊，默认不作为共享 Toolchain 接口。

共享安装授权条件：

```text
命令/安装请求精确命中受信 Adapter
+ Adapter 声明目标为 user Toolchain
+ argv、distribution、来源和版本符合 Adapter policy
+ 安装所需网络与变更审批通过
= 允许 Toolchain Installer 写入
```

例如 `LarkCliAdapter` 声明发行包为 `@larksuite/cli`、安装方式为 npm global，因此它生成/认可的 `npm install -g @larksuite/cli` 可以进入共享 Toolchain；未知的 `npm install -g some-package` 不会因为存在 `-g` 就进入共享控制面。

CLI 自更新是例外：例如 `lark-cli update` 没有 `-g`，但 `LarkCliAdapter` 知道它会修改自身发行包，因此显式分类到 Coordinated Updater。Provider Adapter 的显式 action 分类优先于通用 Package Adapter。

未注册 Adapter 的 CLI 仍可作为普通项目命令运行，但不能获得用户 Credential Profile，也不能自动进入共享身份域。要让新的 CLI 共享授权，必须新增并测试对应 Adapter，不能只增加一条 Prompt 或 Skill 规则。

Adapter 属于 PuddingClaw 受信控制面，需要由 Backend 持续维护版本兼容性、安装来源、命令分类、凭证格式、交互生命周期、验证和脱敏测试。未来可以通过签名 Plugin 分发 Adapter，但普通 Skill、远端 Markdown 或刚安装的 npm 包不得自行注册 Adapter。

它不是 shell alias、PATH 替身、LD_PRELOAD 或容器内 hook。Backend 使用 shell parser/argv parser 识别独立命令，至少接受：

```text
lark-cli ...
LARKSUITE_CLI_NO_UPDATE_NOTIFIER=1 lark-cli ...
```

并把 `2>&1` 之类展示性重定向归一化为 runner 的输出合并选项。包含管道、`;`、`&&`、命令替换、任意 `sh -c` 或无法证明入口仍是 `lark-cli` 的命令，不得携带 Credential Profile 进入普通容器；应拒绝或要求 Agent 改用独立的受支持命令。

Provider 路由决定只来自可信控制面：tool name、解析后的 argv、当前 `user_id`、`project_id` 和注册表中的 `profile_id`。模型提供的宿主路径、Docker volume 名或 HOME 值不参与判断。

### 5.5 `lark-cli config init --new` 如何执行

首次配置/重新配置使用 browser-auth route。`config init --new` 本身会等待用户浏览器操作，因此由 Backend 管理一个身份固定、生命周期有界的专用 runner；它不是普通 workspace 后台进程，也不会把 Secret State 交给 Agent：

```text
1. Agent: execute("lark-cli config init --new")
2. Middleware 截获，不进入当前 Project/Session workspace 容器
3. Backend 解析 Credential Profile
4. Credential Broker 在内存中解密 ~/.puddingclaw/.../vault.enc（首次为空）
5. 创建临时 browser-auth runner：
     image       = PuddingClaw sandbox image
     toolchain   = ~/.puddingclaw/runtime/toolchains/...（只读）
     credentials = runner tmpfs /home/puddingclaw/.lark-cli
     workspace   = 当前 Session/Project（按最小需要挂载）
     network     = Provider 默认网络
     entrypoint  = credential-runner -> lark-cli（argv 数组，无通用 shell）
6. credential-runner 从 Backend 的受控 stdin 接收 Secret State，写入 tmpfs
7. lark-cli 输出严格匹配飞书/Lark 配置页 origin 和 path 的授权 URL；Backend 返回 `Status: awaiting_user_browser`
8. Graph 允许一次用户可见二维码/链接总结后强制结束当前轮，禁止模型继续执行下一条工具
9. Backend-owned Lifecycle Worker 轮询 runner 的私有 tmpfs；用户完成浏览器操作后立即导出 `.lark-cli`，不依赖用户再发一条消息
10. Broker 先把 Secret State 加密并原子写入 `vault.enc`，再读回校验；只有成功后才 ACK 并删除 runner/tmpfs
11. 若 Vault 写入、读回或 Backend 进程失败，runner 保留可恢复状态；Backend 重启后按 owner/provider/profile/job labels 恢复 worker
12. 后续 `config show`/`auth status --verify` 从同一 Profile 验证状态；`auth login --no-wait --json` 仍使用 device-code split-flow
```

Secret State 通道不能复用普通 stdout/stderr，因为工具日志和 Session JSON 会持久化。当前实现通过 `docker exec -i` 的二进制 stdin/stdout 单独传输 tar archive；业务命令 stdout/stderr 走另一条调用并在返回前脱敏。不得把 archive、vault key 或明文 token 写入 Docker logs。

Lifecycle Worker 是 BrowserAuth 资源的完成回调与故障恢复机制，不是 cron、scheduler 或用户定时任务。本 ADR 当前不创建或执行任何定时飞书任务。

### 5.6 容器在方案中的角色

| 容器类型 | 生命周期 | 挂载 Toolchain | 接触凭证 | 挂载 Workspace | 职责 |
|---|---|---:|---:|---:|---|
| Workspace container | Session/Project 活跃期，空闲删除 | 只读 | 否 | 是 | 普通项目命令、测试、计算 |
| Installer container | 单次安装，`--rm` | 读写 | 否 | 通常只读 | 安装/更新通用 CLI |
| Provider runner | 单条 provider 命令，`--rm` | 只读 | tmpfs 中短暂接触 | 按需 | 执行带身份的 `lark-cli` 命令 |
| Browser-auth runner | 授权窗口内有界存活 | 只读 | tmpfs 中短暂接触 | 只读 | 承载阻塞式 config init；Lifecycle Worker 负责完成回收，Graph 负责跨轮暂停 |

不存在一个共享所有进程和 HOME 的“顶层容器”。顶层是 Backend 管理的用户级资源域：宿主 `~/.puddingclaw`、Profile registry、Credential Broker 和 Toolchain；容器只是按任务临时获得最小挂载的执行隔离单元。

## 6. 容器生命周期与清理

重要状态迁出 runtime-home 后，工作区容器可安全删除并重建。

### 6.1 Workspace/Project 容器为什么仍然必要

目标架构遵循“状态持久化在挂载资源，计算发生在可销毁容器”：容器不是用来长期保存安装结果的虚拟机，而是执行和安全边界。

只有用户级通用 CLI 进入共享 Toolchain。项目依赖仍保持项目作用域：

| 内容 | 存储/作用域 | 示例 |
|---|---|---|
| 沙箱基础运行时 | Sandbox image | Node 22、Python 3.12、Chromium、系统库 |
| 用户级通用 CLI | `~/.puddingclaw/runtime/toolchains` | `lark-cli`、其他已接入的全局 CLI |
| Project Node 依赖 | Project workspace/依赖卷 | `react`、`next`、项目自己的 npm packages |
| Project Python 环境 | Project workspace/依赖卷 | `.venv`、requirements/uv dependencies |
| 用户凭证 | `~/.puddingclaw/users/.../credentials` 加密保险库 | Lark OAuth/Profile |
| 工作文件 | 当前 Session/Project workspace | 源码、报告、生成文件 |

Workspace/Project 容器仍负责：

- 把当前 workspace 作为唯一普通读写范围。
- 提供确定的 Node/Python/浏览器和系统库版本。
- 执行项目脚本、测试、构建和数据处理。
- 隔离不同 Project 的依赖、进程、环境变量和后台任务。
- 执行 CPU、内存、PID、capability、network 和 filesystem 限制。
- 只读消费共享 Toolchain，但不能篡改它，也不能读取 Credential Secret State。

因此容器即使空闲后被删除也不丢状态；下一次按相同 image、workspace、Project dependency mounts 和共享 Toolchain 重建即可。长生命周期只是性能优化，不是持久化语义。

规则：

- Session/Project 容器空闲 30 分钟：执行 `docker rm -f`，不再只 `docker stop`。
- Backend 启动时 GC：删除所有带 `com.puddingclaw.managed=true` 且类型为 workspace、当前无活动 Run 的 stopped 容器。
- 无 Project 的 workspace 文件仍保存在 `backend/data/agent-workspaces/unscoped/<session-id>`；容器删除不删除对话文件。
- Project 依赖卷以及 `~/.puddingclaw` 中的共享 Toolchain/加密 Credential Profile 不随容器删除。
- 临时 installer、provider runner、browser-auth 容器统一使用 `--rm` 和有界 TTL。
- 新增标签：`com.puddingclaw.kind=workspace|installer|provider-runner|browser-auth`、`com.puddingclaw.owner=<hash>`、`com.puddingclaw.workspace=<hash>`。

设置页提供独立操作：

- 清理 stopped/idle 工作区容器；可自动执行。
- 查看共享 Toolchain 及 CLI 版本。
- 查看 Credential Profiles、验证状态、默认 Profile 和 Project 绑定。
- 删除 Profile/凭证必须显式确认；容器 GC 不得修改 `~/.puddingclaw/users/*/credentials`。

## 7. 旧数据迁移

升级时不得直接删除现有 `puddingclaw-runtime-home-*`。

迁移步骤：

1. 枚举带 `com.puddingclaw.managed=true` 的 runtime-home volumes。
2. 从 `.npm-global` 收集已安装通用 CLI，重新安装到 `~/.puddingclaw/runtime/toolchains`；不直接合并多个 npm 目录。
3. 对每个发现的 `.lark-cli` 候选，在隔离 runner 中执行 `config show` 和 `auth status --json --verify`。
4. 只把验证成功的候选加密导入 `~/.puddingclaw/users/<owner-user-id>/credentials/lark/<profile-id>/vault.enc`；多个有效身份必须让用户选择名称和默认项，禁止按文件时间静默覆盖。
5. 验证新 runner 可以跨无 Project/Project 会话复用 CLI 和授权。
6. 将旧容器和旧 runtime-home volumes 标记为 `legacy`，在设置页展示可回收空间。
7. 只有用户确认后才删除旧 volume；容器可在无活动 Run 后自动删除。

## 8. 安全不变量

- Project 路径、Session ID 和容器名称不是凭证所有权边界。
- 普通 workspace 容器永远不挂载 Credential 保险库，也不能获得解密后的 Secret State。
- Agent 只能选择注册表中的 `profile_id`，不能提供保险库路径或宿主路径。
- Backend 根据可信 `user_id/project_id` 解析 Profile，并校验 owner。
- Profile 锁覆盖 config、login、refresh、logout 和所有可能写 token 的命令。
- BrowserAuth Profile lease 跨 Agent 轮次持久化；同一 Profile 在授权完成或过期前不能执行其他 provider 命令。
- BrowserAuth 回收必须遵循“导出 -> Vault 原子写入并读回 -> ACK -> 删除 runner”，禁止提前删除 tmpfs。
- Lark Provider 非删除操作默认联网自动执行；删除类操作及 Toolchain 安装仍需 HITL。
- 工具结果默认脱敏；secret/token 不进入 SSE、Session JSON、Evidence、日志或模型上下文。
- Toolchain 更新不能修改 Credential 保险库；Credential 操作不能修改 Toolchain。
- 容器/volume 清理必须依赖 PuddingClaw 标签和注册表引用，禁止按名称前缀盲删；该清理流程无权删除 `~/.puddingclaw`。

## 9. 实施顺序与验收

### 阶段 A：共享 Toolchain

1. 新增 `PUDDINGCLAW_HOME` 解析和稳定 Toolchain 目录。
2. 普通容器只读 bind-mount，installer 容器读写 bind-mount。
3. 将全局 npm/Python CLI 安装路由到 installer。
4. 验证一个无 Project 会话安装后，另一个无 Project 会话和两个 Project 都能直接调用。

### 阶段 B：Credential Profile

1. 新增 Profile registry、默认项和 Project binding。
2. 新增专用 provider runner 与 argv 拦截。
3. 实现 Keychain-backed 加密保险库与 tmpfs materialization，普通容器不再看到 `.lark-cli`。
4. 验证跨 Project 复用、显式覆盖、多 Profile、刷新、登出和删除影响范围。

### 阶段 C：生命周期与迁移

1. idle-stop 改为无活动 Run 后 idle-remove。
2. Backend 启动执行 managed-container GC。
3. 实现旧 runtime-home 扫描、验证、导入与人工确认清理。
4. 设置页增加 Toolchain、Profile、Project binding 和旧资源回收视图。

最终验收标准：

- 临时对话不再永久增加 Docker 容器。
- 通用 CLI 只安装一次，跨全部容器可用。
- 默认飞书授权跨无 Project 与所有未覆盖 Project 可用。
- Project 可显式选择不同 Profile，但不会隐式创建项目私有授权。
- 普通项目命令无法读取 `.lark-cli` 或 token。
- 删除工作区容器不影响 CLI、授权、Project 文件或项目依赖。

## 10. 被否决方案

### 共享整个 `/home/puddingclaw`

否决。会混合缓存、项目状态、CLI、配置和凭证；普通项目脚本可直接读取 token。

### 只把凭证保存在 Docker named volume

否决。用户无法直观定位、备份或迁移，Docker Desktop reset/volume prune 会丢失账号连接，而且随机 volume 名会重新引入 Session/Project 生命周期耦合。Docker 只承载解密后的临时运行视图；加密持久真相源固定在宿主 `~/.puddingclaw`。

### 每个 Session/Project 保留独立 runtime-home

否决。重复安装、重复授权、镜像升级丢状态，并持续产生 Docker volume。

### 单一长期用户容器执行所有命令

否决。跨项目进程、环境变量和后台任务会相互污染，隔离边界过弱。

### 把凭证复制进 Project

否决。Project 不是身份所有者，复制会造成泄漏、撤销和刷新状态分叉。
