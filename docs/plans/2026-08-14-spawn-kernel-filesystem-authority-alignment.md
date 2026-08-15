# Spawn / Kernel 智能模式文件系统对齐方案

> 状态：Implemented，路径与 Effect 回归已收口
> 日期：2026-08-14
> 优先级：P0
> 本期：DeepAgents 文件工具、Shell `execute`、Spawn、Kernel、Subagent
> 非本期：Docker 完整改造、严格模式目录授权

## 1. 结论

PuddingClaw 智能模式直接对齐开源 Agent 的 full-access / trusted-local 行为：

```text
普通本地文件权限 = 运行 PuddingClaw 的 OS 用户权限
真实路径权限空间 >= Virtual path 暴露空间
```

- OS 用户能读写的普通路径，文件工具、Spawn、Kernel 和 Subagent 都能读写。
- 不要求目录属于当前项目，不要求目录登记为项目，也不要求存在 Virtual path。
- 项目只提供默认 `cwd`、相对路径基准、上下文发现和 UI 归属，不提供额外文件权限。
- Virtual path 只是方便 Agent 定位资源的别名，不参与权限判断。
- 智能模式不建立 project roots、external roots、writable roots 或 protected roots 白名单。
- 普通路径不触发 `external_directory`、`host_filesystem_access` 等目录 HITL。
- OS 自身返回的 `EACCES`、`EPERM`、只读文件系统等错误原样返回，不能改写为 Harness 目录审批。
- 网络、安装、凭证使用、破坏性操作等仍可由独立 effect policy 判断；它们不能反向创造目录权限问题。

智能模式没有 PuddingClaw 自定义文件系统例外。模板、Skills、项目和其他普通文件都按真实 OS 权限处理。

### 1.1 威胁模型与已接受风险

智能模式是显式的 trusted-local / `danger-full-access` 产品模式，不是安全沙箱。启用该模式即接受：

- Prompt injection、恶意仓库内容、网页内容或工具结果可能诱导 Agent 读取、修改或删除 OS 用户可访问的任意文件，包括其他项目、Shell 配置和凭证文件。
- 如果同一 Run 同时获得网络能力，已读取的数据可能被外传。Effect policy 可以减少误操作，但不是 DLP，也不能承诺阻止任意代码构造的隐蔽读取或外传。
- Kernel 智能模式只增强 syscall、进程、网络等隔离；由于文件系统策略固定为 `unrestricted`，它不保护用户文件免受命令修改。

风险接受不改变本方案的权限结论，也不重新引入目录 HITL。产品必须做到：

- 设置页与首次启用入口明确展示：“智能模式可读写当前系统用户可访问的所有本地文件”。
- 交互场景由用户选择智能模式或在首次使用时确认；若智能模式是安装默认值，首次实际执行前仍必须展示一次风险确认。
- Headless / automation 只有在部署配置显式选择智能模式时才能启用，不能因 Kernel 不可用、配置缺失或兼容回退而隐式扩大为 full access。
- 风险确认按文案版本持久化记录；风险范围实质变化后必须重新确认。
- Kernel 文案不得暗示智能模式会把文件限制在项目目录。
- Trace 明确记录 `filesystem_mode=unrestricted`，方便审计当前 Run 的风险状态。
- 从受限模式切换到智能模式时要求用户进行一次明确确认；同一风险版本下不对每个普通目录重复确认。

## 2. 开源对齐依据

本方案以本地 `/Users/pet/Code/AI/Agent/源码合集` 中的源码快照为评审依据。

### 2.1 OpenAI Codex

参考版本：[`eb752e43d9b7bd7dc5965ea20642bcf7f1a492d8`](https://github.com/openai/codex/tree/eb752e43d9b7bd7dc5965ea20642bcf7f1a492d8)

- [`codex-rs/protocol/src/permissions.rs`](https://github.com/openai/codex/blob/eb752e43d9b7bd7dc5965ea20642bcf7f1a492d8/codex-rs/protocol/src/permissions.rs)：`SandboxPolicy::DangerFullAccess` 映射为 `FileSystemSandboxPolicy::unrestricted()`。
- `unrestricted()` 的策略类型为 `FileSystemSandboxKind::Unrestricted`，没有 readable/writable root entries。
- [`codex-rs/sandboxing/src/manager.rs`](https://github.com/openai/codex/blob/eb752e43d9b7bd7dc5965ea20642bcf7f1a492d8/codex-rs/sandboxing/src/manager.rs) 调用 [`policy_transforms.rs`](https://github.com/openai/codex/blob/eb752e43d9b7bd7dc5965ea20642bcf7f1a492d8/codex-rs/sandboxing/src/policy_transforms.rs) 的核心判定：unrestricted filesystem + full network 时 `should_require_platform_sandbox=false`，最终选择 `SandboxType::None`，macOS 不进入 Seatbelt。
- [`codex-rs/sandboxing/src/seatbelt.rs`](https://github.com/openai/codex/blob/eb752e43d9b7bd7dc5965ea20642bcf7f1a492d8/codex-rs/sandboxing/src/seatbelt.rs)：若因 managed network 等独立原因仍需 Seatbelt，且没有 unreadable roots，full-disk policy 生成 `(allow file-read*)` 与 `(allow file-write* (regex #"^/"))`；存在 unreadable roots 时生成带排除项的全盘策略。两种情况都不依赖项目或 Virtual path。

采用结论：PuddingClaw 智能模式对应 Codex `danger-full-access`，不是 Codex `workspace-write`。

### 2.2 DeepSeek Harness

参考版本：[`47f943859bef60e4160492346772ded9b24f765a`](https://github.com/deepseek-ai/deepseek-harness/tree/47f943859bef60e4160492346772ded9b24f765a)

- [`packages/terminal/terminal-bash/src/index.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/47f943859bef60e4160492346772ded9b24f765a/packages/terminal/terminal-bash/src/index.ts)：`policy.mode === 'danger-full-access'` 时直接返回原始 shell argv，不调用 sandbox provider。
- [`packages/shell/bash-sandbox/src/index.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/47f943859bef60e4160492346772ded9b24f765a/packages/shell/bash-sandbox/src/index.ts)：full-access 分支直接调用本地 executor。
- [`packages/shell/bash-sandbox/README.md`](https://github.com/deepseek-ai/deepseek-harness/blob/47f943859bef60e4160492346772ded9b24f765a/packages/shell/bash-sandbox/README.md)：明确说明 `danger-full-access` deliberately bypasses sandbox；它不是更宽的 sandbox profile。

采用结论：智能模式直接执行，不先把普通路径编译进 sandbox roots。

### 2.3 Pi

参考版本：[`541045ae0e30ac0375fde429f8a8baa057c009db`](https://github.com/earendil-works/pi/tree/541045ae0e30ac0375fde429f8a8baa057c009db)

- [`packages/coding-agent/src/core/bash-executor.ts`](https://github.com/earendil-works/pi/blob/541045ae0e30ac0375fde429f8a8baa057c009db/packages/coding-agent/src/core/bash-executor.ts)：将 command 与 `cwd` 交给底层 `operations.exec`，没有项目目录授权模型。

采用结论：`cwd` 只影响相对路径，绝对路径直接按宿主权限访问。

### 2.4 OpenCode

参考版本：[`1f94d8a3c86b67f4f49a0e341de74e9188381b3a`](https://github.com/anomalyco/opencode/tree/1f94d8a3c86b67f4f49a0e341de74e9188381b3a)

OpenCode 的 `external_directory` 机制只作为未来严格模式参考，不进入智能模式主链路。不能把 OpenCode 的 project/external 二分嫁接到 PuddingClaw 智能模式。

补充证据：[`specs/v2/session.md`](https://github.com/anomalyco/opencode/blob/1f94d8a3c86b67f4f49a0e341de74e9188381b3a/specs/v2/session.md) 明确说明 “Bash is not sandboxed”，spawned shell 继承 host user 的 filesystem、process 与 network authority。

### 2.5 Kernel / sandbox 在各项目中的位置

| 项目 | Full-access 执行 | 受限执行 | Kernel / sandbox 的作用 |
| --- | --- | --- | --- |
| Codex | `SandboxType::None` | macOS Seatbelt、Linux sandbox | 根据 permission profile 强制进程、网络和文件约束；不是独立权限源 |
| DeepSeek Harness | 直接 local executor | sandbox provider 包装 argv | confined mode 的执行机制；`danger-full-access` 明确绕过 |
| Pi | 直接 local executor | 核心实现未内置同类 Kernel 层 | `cwd` 与进程生命周期管理 |
| OpenCode | 直接 child process | permission ask；核心 Shell 未套 OS 文件沙箱 | 其 project “sandbox”主要指 worktree，不是 Seatbelt/bwrap 权限空间 |

共同点：这些项目都没有让 Kernel 创建一套比产品 permission mode 更窄的隐式项目文件权限。Runner 执行策略，permission mode 决定能力；顺序不能反过来。

## 3. 用户可见契约

以下访问在智能模式下完全等价：

```bash
# 当前项目
cp /Users/pet/Code/AI/Agent/PuddingClaw/a.txt \
   /Users/pet/Code/AI/Agent/PuddingClaw/b.txt

# 其他项目或任意普通文件夹
cp /Users/pet/Code/AI/Agent/OtherProject/a.txt \
   /Users/pet/Documents/b.txt

# Virtual alias
cp /analytics-models/model/template.html \
   /workspace/reports/topic/index.html
```

结论均为：直接执行，由 OS 用户权限决定成败，不产生目录 HITL。

添加、移除或切换项目只能改变：

- 默认 `cwd`。
- 相对路径解析。
- 项目说明、Skills 和配置发现。
- Session / Thread 的 UI 归属。

它不能改变任何真实绝对路径的读写权限。

### 3.1 Effect policy 边界

文件路径能力与命令效果必须独立判断：路径位于项目外、Home 下或其他项目中，不构成审批原因；命令本身具有高风险效果时，仍进入现有 effect policy。

| 示例 | 文件系统结论 | Effect policy 结论 |
| --- | --- | --- |
| `cp ~/OtherProject/a.txt ~/Documents/a.txt` | 直接继承 OS 权限 | 普通复制，不产生目录 HITL |
| `cat ~/.ssh/id_rsa` | 不产生目录 HITL | 识别到凭证读取时进入 credential/sensitive-read policy |
| `rm -rf ~/OtherProject` | 不产生目录 HITL | 进入 destructive policy |
| 写 `~/.zshrc`、LaunchAgents、crontab | 不产生目录 HITL | 进入 persistence / destructive policy |
| 写 `~/.ssh/authorized_keys` 或替换私钥 | 不产生目录 HITL | 进入 credential + persistence policy |
| `curl`、上传或任意联网程序 | 文件路径不改变结论 | 进入 network policy |
| 安装系统或语言包 | 文件路径不改变结论 | 进入 package-install policy |

约束：

- Effect 审批必须显示实际原因，例如“递归删除”“修改启动配置”“读取凭证”或“联网”，不能显示成模糊的 `host_filesystem_access`。
- 一个 effect 获批后，不得再为同一操作追加 project/external directory 审批。
- 对无法静态理解的任意代码，effect analyzer 只能 best effort；不能以它为 full-access 安全保证。可靠网络隔离必须由 Kernel/network enforcement 提供。
- 本次改造只删除智能模式目录判权，不删除或放宽现有 network、package-install、destructive、credential / sensitive-read 规则。
- 若 persistence 尚未独立建模，P0 至少把高置信启动与授权入口（Shell rc、LaunchAgents、crontab、`authorized_keys`）映射到现有 destructive / credential policy；后续再拆分独立 effect 类型。

## 4. 路径模型

```text
真实绝对路径 ──────────────────────────────┐
Virtual path ── locator -> real path ──────┤
相对路径 ── cwd -> real path ──────────────┘
                                            │
                                            ▼
                                  Canonical Host Path
                                            │
                         ┌──────────────────┴──────────────────┐
                         ▼                                     ▼
                  Spawn 原样执行                     Kernel 等价宿主视图
                         │                                     │
                         └────────── OS 用户权限 ──────────────┘
```

Virtual locator 只保存：

```python
@dataclass(frozen=True)
class PathLocator:
    virtual_root: str
    host_root: Path
```

禁止在 Locator 上附加 `read_only`、`external`、`requires_approval`、`project_owned` 等权限语义。

## 5. DeepAgents 文件工具

目标调用链：

```python
real_path = locator_resolver.resolve(path, cwd=session.project_cwd)
return host_file_operations.execute(real_path, operation)
```

要求：

- `read_file`、`write_file`、`patch_file`、`copy_file`、`ls`、`glob`、`grep` 接受任意真实绝对路径。
- 普通真实路径不经过 HostFileBroker 目录审批。
- `/workspace/x` 与项目真实路径 `/Users/.../PuddingClaw/x` 解析到同一文件，但真实路径不依赖 `/workspace` 才能访问。
- 文件工具直接返回 OS 文件错误，不产生 `external` 分类。
- HostFileBroker 若保留，只负责跨环境传输、原子文件操作和回执。

## 6. Shell `execute`

`execute` 就是 Shell 执行能力，必须支持普通 `cp`、`mv`、`mkdir`、Python、Node 和第三方程序。

智能模式处理顺序：

1. 将命令中的已知 Virtual locator 投影为 runner 可访问的真实路径，或提供真实挂载点。
2. 运行独立 effect policy。
3. 直接执行命令。

禁止：

- 在执行前检查路径是否属于项目。
- 维护 `external_allowed_paths`、`workspace_prefix_allowlist` 或 Spawn 绝对路径例外。
- 因为命令访问宿主路径而返回 `host_filesystem_access`。
- 通过解析有限的 `cp` / `mv` 命令集合来决定普通目录权限；Opaque 命令同样应该工作。

## 7. Spawn

Spawn 对齐 DeepSeek Harness full-access 与 Pi：

- 以 backend OS 用户直接启动宿主进程。
- 不启用普通文件系统 fence。
- 不传 project/workspace roots。
- 不申请 external directory Grant。
- `cwd` 仅作为进程工作目录。
- Parent 与 Subagent 使用相同 filesystem mode。

Spawn 可以继续有超时、输出、进程数、网络或 effect policy，这些策略不能缩小普通文件路径范围。

## 8. Kernel

### 8.1 Kernel 是什么

PuddingClaw 的 Kernel 指“由 OS Kernel 强制的本地进程隔离 runner”：

- macOS 使用 Seatbelt `sandbox-exec`。
- Linux 使用 bubblewrap namespaces + seccomp + `no_new_privs`。

它不是模型 Kernel、Jupyter Kernel、容器 workspace，也不是 Virtual path 文件服务器。

Kernel 负责：

- 进程与子进程生命周期隔离。
- syscall、capability、namespace、跨进程访问等内核攻击面限制。
- 网络开关或网络代理约束。
- 环境变量清理、私有临时环境、资源上限、超时和进程组回收。
- 在未来严格模式中强制 read-only / workspace-write 文件策略。

Kernel 不负责：

- 判断目录是否属于项目。
- 解析 Skills 或 Virtual path 的业务含义。
- 决定普通真实路径能否读写。
- 发起 `external_directory` / `host_filesystem_access` HITL。
- 创建第二套文件工具权限。

### 8.2 智能模式 profile

智能模式仍可选择 Kernel，以获得非文件系统隔离；但文件系统子策略必须是 `unrestricted`，与 Spawn 相同：

- 使用同一用户身份或等价 UID/GID 映射。
- 向命令暴露普通宿主文件系统，不只暴露当前项目和 Virtual mounts。
- macOS Seatbelt 使用全盘 `file-read*` / `file-write*` allow，再叠加所需网络、进程和系统能力限制。
- Linux bwrap 保留 PID/network/user namespace 与 seccomp 时，文件 mount view 必须等价于宿主视图；不能隐藏 `/home`、`/mnt`、`/media`、`/opt` 等普通路径。
- `cwd` 只决定相对路径，不能要求它位于 workspace Grant 中。

建议 profile 形态：

```python
KernelExecutionProfile(
    filesystem="unrestricted",  # 智能模式固定值
    network_allowed=...,
    syscall_policy=...,
    process_limit=...,
    timeout_seconds=...,
)
```

现有 `SandboxGrantProfile` 的 workspace/read/write/deny roots 只适用于未来严格模式，不应参与智能模式编译。

智能模式下两种 runner 的差异固定为：

| 维度 | Spawn | Kernel |
| --- | --- | --- |
| 普通文件读写 | 宿主 OS 用户权限 | 同一宿主 OS 用户权限 |
| 项目 / Virtual path | 仅影响 `cwd` 与定位 | 仅影响 `cwd` 与定位 |
| syscall / capabilities | 宿主进程默认能力 | 可由 seccomp / Seatbelt 限制 |
| PID / IPC / namespace | 宿主默认 | Linux 可隔离 |
| 网络 | 由 effect policy 与宿主环境控制 | 可额外由 OS profile 强制 |
| 资源与进程回收 | 应用层限制 | 应用层 + Kernel enforcement |

因此“Kernel 更安全”指进程和系统能力的强制隔离，不表示它能少读几个普通目录。

### 8.3 当前实现偏差

当前实现把 Kernel 写成了 workspace sandbox：

- `KernelWorkspaceBackend` 默认只发布 workspace、scratch 与少量 alias roots。
- `SandboxGrantProfile` 要求 `cwd` 和读写操作被 roots 覆盖。
- macOS `render_profile()` 从 root allowlist 生成文件规则。
- Linux `_HIDDEN_ROOTS` 主动隐藏 `/home`、`/mnt`、`/media`、`/opt` 等宿主路径。

这套行为对应 `workspace-write`，不对应智能模式。它导致 Kernel 能运行命令，却无法像 Spawn 一样访问普通本地文件。

如果当前 Kernel 无法提供 unrestricted host filesystem view，则它尚未支持智能模式。过渡期的 Kernel → Spawn 回退必须保留显式提示，因为回退会失去 syscall、network 和 process containment；但不能把回退原因描述成目录未授权。

## 9. Parent / Subagent

Subagent 继承父 Run 的 `smart_local` filesystem mode：

- 不重新根据 Prompt、Skills、Virtual mounts 或项目列表推导权限。
- Spawn Subagent 和 Kernel Subagent 都使用相同普通路径语义。
- Subagent 不对父 Agent 已可访问的路径再次发起 HITL。

## 10. 当前故障原因

以下机制链已由源码和测试逐环证实；“比亚迪续航趋势整理”的完整持久化 Session 事件未独立复核，具体事件顺序依据现有对话摘录。

该摘录中的失败不是 `cp` 命令不受支持，而是命令尚未执行就被 Harness policy 拦截：

```text
Tool execute was blocked by Harness policy: host_filesystem_access
```

根因链路：

1. 模板通过 `/analytics-models/...` Virtual path 暴露。
2. 文件工具能读取该 Virtual path，但 Shell / runner 使用了另一套宿主路径与 workspace roots 规则。
3. `execute` 把模板真实路径或 Virtual mount 判成 host/external filesystem access。
4. Harness 在 Shell 启动前拒绝，因此 `cp` 从未运行。
5. Agent 被迫尝试 `read_file + write_file` 传输 ECharts 大文件，继而认为模板无法正常复制。

所以模型选择或模板结构不是主要故障；真正错误是 Virtual path 被错误地升级成权限边界，并且文件工具、Spawn、Kernel 的文件系统语义不一致。

源码还显示同一命令当前会因 runner 不同而以不同方式失败：`tool_execution.py` 的 `host_filesystem_access` 前置拒绝只覆盖 Spawn 分支；Kernel 则可能继续进入 root sandbox 后返回 EACCES。应用层 DENY 与 OS sandbox DENY 的这种漂移也必须由本方案消除。

## 11. 实施改造

### Phase 1：删除智能模式目录判权

- `WorkspacePathRouterMiddleware`：只解析路径，不再分 workspace / external 后申请权限。
- `PermissionedCompositeBackend`：普通真实路径直接调用 host file operations。
- `ShellPolicyAnalyzer`：删除智能模式 host filesystem / external path 拦截。
- 删除 `_READONLY_VIRTUAL_PREFIXES` 对普通路径权限的影响。
- 删除智能模式下 `_MANAGED_OS_PERMISSION_MARKERS` 驱动的错误归一化：`operation not permitted`、`permission denied`、`errno 1/13` 等 OS 错误必须保留原始 tool status、errno 和消息，不能改写为 `managed_resource_unavailable`、资源授权或目录审批。
- 同步覆盖 `wrap_tool_call`、`awrap_tool_call`、`backend_read` 与 `_normalize_managed_execute_result` 路径，避免同步/异步行为漂移。
- 所有删除按 permission mode 条件隔离；已经存在的受限/严格分支保持当前行为，不借本期重构扩展或重写。
- 保留 network、package-install、destructive、credential / sensitive-read effect policy，并补齐高置信 persistence 目标分类。

### Phase 2：Spawn 对齐

- 智能模式直接走本地 executor。
- Virtual path 投影只做路径转换。
- 移除 project roots、external grants 和 Virtual alias allowlist 对普通命令的限制。

### Phase 3：Kernel 对齐

- 将 `execution_mode` 与 filesystem permission mode 解耦；Kernel 选择不再隐式等于 workspace-write。
- 新增智能模式 `filesystem=unrestricted` profile；不编译 workspace/read/write/deny roots。
- macOS 生成 full-disk file allow，保留独立网络、进程和资源策略。
- Linux 移除 smart profile 的 `_HIDDEN_ROOTS` 与 workspace-only mounts，提供等价宿主文件系统视图，同时保留 namespaces/seccomp。
- `KernelWorkspaceBackend` 改为 runner 语义命名；workspace 只作为默认 `cwd`。
- Kernel 暂不具备该能力时显式回退 Spawn，回退提示说明隔离强度变化。

### Phase 4：提示与观测

- 审计 `prompts/tool_guides/core.md`、动态生成的本地路径说明和相关系统提示，定位并删除任何暗示“Virtual path 只能由文件工具访问”或“宿主真实路径天然 external”的指导；不假定源码中存在某句固定原文。
- Trace 记录 `input_path_kind=real|virtual|relative` 与最终真实路径摘要，只用于诊断。
- 删除 `authority_decision=missing`、`root_origin=project|external` 等智能模式字段。

## 12. 验收矩阵

| 场景 | File tools | Spawn | Kernel |
| --- | --- | --- | --- |
| 当前项目真实路径读写 | OS 结果 | OS 结果 | OS 结果 |
| 其他项目真实路径读写 | OS 结果 | OS 结果 | OS 结果 |
| `Documents` 等普通目录读写 | OS 结果 | OS 结果 | OS 结果 |
| `/workspace` 与项目真实路径 | 等价 | 等价 | 等价 |
| 模板真实路径读取 | OS 结果 | OS 结果 | OS 结果 |
| 模板复制到任意普通目录 | OS 结果 | OS 结果 | OS 结果 |
| OS 本身禁止的路径 | 原始 OS 错误 | 原始 OS 错误 | 原始 OS 错误 |
| Opaque Python/Node 访问普通绝对路径 | OS 结果 | OS 结果 | OS 结果 |

额外断言：

- 普通本地文件操作目录 HITL 数量为 0。
- 登记或取消登记项目后，真实绝对路径结果不变。
- 新增、删除或改名 Virtual alias 后，真实绝对路径结果不变。
- Parent 与 Subagent 结果一致。
- Spawn 与 Kernel 结果一致。
- 模板资源可使用普通 `cp` 复制，不通过上下文传输 ECharts。
- `EACCES`、`EPERM` 与 errno 1/13 在同步、异步、backend-read 和 execute 路径均保持原始错误，不进入资源或目录审批恢复流程。

### 12.1 Effect policy 反向断言

- 普通 `cp`、`mv`、读取和非递归写入不会因为绝对路径位于 cwd 外触发目录审批。
- 递归/批量删除仍进入 destructive policy；本次文件系统改造不能把它降级为普通 allow。
- 读取高置信凭证路径仍进入 credential / sensitive-read policy。
- 写 Shell rc、LaunchAgents、crontab、`authorized_keys` 等高置信持久化入口仍进入 destructive / credential policy。
- 联网与安装命令仍进入 network / package-install policy；Kernel profile 继续按最终网络决策强制执行。
- Effect 获批或拒绝后只产生对应 effect 结果，不追加 `external_directory` 或 `host_filesystem_access`。

### 12.2 模式隔离反向断言

- 共享 router、analyzer 与 backend 的修改必须以 permission mode 为条件；智能模式删除目录判权不能改变现有受限/严格分支的决策快照。
- 现有受限/严格模式测试集保持通过；本期只做非回归保护，不扩展其功能或重构其权限模型。
- Headless 未显式选择智能模式时不得获得 `filesystem=unrestricted`。
- 首次启用、受限模式切换和风险文案版本升级均要求一次风险确认；已确认版本不产生逐目录提示。

## 13. 后续待办

- 智能模式全部验收通过后，再为严格模式另立 `workspace-write` 方案。
- 严格模式不得进入本期实现范围，也不得阻塞智能模式修复。
- 后续方案应复用已经解耦的 runner / permission mode 结构，不能重新把项目或 Virtual path 变成权限边界。

本方案当前没有待决策项。以下结论均已确定：

- 项目是不是权限边界：不是。
- 是否支持“多根项目权限”：不需要，其他项目就是普通文件夹。
- Virtual path 是否决定权限：不决定。
- 智能模式是否允许普通宿主路径读写：允许，结果继承 OS 用户权限。
- Kernel 智能模式是否需要 host-filesystem passthrough：需要，这是对齐要求，不是待决策项。
- Kernel 不可用时是否静默降级：不静默；回退 Spawn 会降低隔离强度，应保留显式提示。

## 14. 完成定义

满足以下条件才可宣布对齐完成：

- 智能模式不再存在 project/external 普通目录判权。
- 文件工具、Spawn、Kernel、Parent、Subagent 都继承同一 OS 用户文件权限。
- Kernel 不再只有 workspace / Virtual mounts 可见。
- Virtual path 只承担定位和投影。
- `execute` 能直接运行涉及任意普通真实路径的 `cp` 等命令。
- OS 权限错误保持原始语义，不被转换为 Harness 目录审批或合成资源错误。
- Network、package-install、destructive、credential / sensitive-read 与高置信 persistence effect policy 回归全部通过。
- 智能模式风险提示、首次/升级确认和 Headless 显式 opt-in 已实现并可审计。
- 现有受限/严格分支的决策回归测试保持通过。
- “比亚迪续航趋势整理”回归用例在 Spawn 和 Kernel 均通过，且不出现 `host_filesystem_access`。

## 15. 实施验证记录

2026-08-14 完成最终收口：

- Smart unrestricted 下普通 `mv` 不再经过 managed-root 判定；截图中的 `/tmp/... mv ... && echo MV_OK` 在 Spawn 实际执行成功，Spawn / Kernel preflight 均为 `ALLOW`，目录 HITL 为 0。
- `/scratch` 继续作为 Run 级临时生命周期 locator；显式 `/tmp/...` 恢复为宿主真实路径，`TMPDIR` 仍可指向 `<scratch_host_path>/tmp`。
- Dynamic Python、`LD_PRELOAD`、`PATH` 等无法证明效果的调用继续进入独立 effect policy，不会因 `filesystem=unrestricted` 被降级放行。
- 单文件、非递归、无特殊权限位的 `chmod` 属于可恢复普通文件操作，Smart unrestricted 直接执行；`chmod -R`、setuid/setgid/sticky、宽泛或受保护目标仍进入 effect policy。
- `complex_shell_expansion` 只保留为 Strict / restricted 的保守语法边界。Smart unrestricted 对简单 `$(...)`、反引号和 `${...}` 做确定性展开分析：安全本地命令直接执行，联网、安装、删除按真实 effect 处理，嵌套或无法解析时才返回 `shell_effect_unprovable`。
- Strict / restricted 的 Shell directory grant 快照显式绑定 `filesystem_mode=restricted`，避免与 Smart unrestricted 授权混用。
- Backend 完整自动化回归：`2463 passed, 13 skipped`。Skip 为环境/能力条件跳过；测试结果无失败。

2026-08-15 补做全出口审计与黑盒矩阵后完成二次收口：

- 不再以几条 `cp/mv` happy path 代替权限验收；新增 Spawn / Kernel 双 runner 矩阵，覆盖 read/stat/find、mkdir、重定向、touch、cp/mv、单文件 chmod、ln、rm 单文件、sed/sort/tee、管道、分号、`||`、subshell、环境变量、`${...}`、`$(...)`、算术展开、glob、控制流、Python/Node、rsync/install、文件工具和反向 effect 用例。
- 修复 Shell 包装绕过：`( rm -rf ... )`、`if/for/while/case`、函数体、`xargs`、`eval/source`、stdin shell、进程替换与 shell heredoc 不再把递归删除、递归 chmod、联网或动态效果伪装成普通未知命令。安全控制流和普通本地文件操作仍为零 HITL。
- `complex_shell_expansion` 继续只作为 Strict / restricted 的保守边界；Smart unrestricted 支持安全命令替换、参数展开、算术展开、进程替换和可分析 heredoc，只有真实 effect 或无法完整解析时才询问。
- 补齐 `copy_file` 与 `patch_files` 特殊中间件分支：Smart trusted-local 的普通真实路径读写不再提前创建 exact-file HITL；文件工具写 `.zshrc`、`.ssh`、`.aws` 等敏感/持久化目标时，以 `persistence_write` 原因请求精确文件确认，敏感读取以 `sensitive_host_read` 原因确认，且不能扩大成“所有外部文件”。
- 文件工具真实路径矩阵覆盖 `read/write/edit/ls/glob/grep`；复制与删除统一走标准 Shell `cp` / `rm`。HostFileBroker 在 Smart 中只承担内部原子落盘、回执、回滚和冲突保护，不再构成第二个路径授权边界。
- 修正写效果元数据：`ln`、`rsync`、`patch`、`unlink`、tar/unzip 写入以及 Node `writeFileSync` 等同步 API 会记录 write capability；`rsync --delete` 与远程 transport 分别保留 destructive 与 network effect。
- UI 与 Agent 提示已删除“项目目录是工具读写边界”“Smart mutation 仍需 external Grant”等旧描述；项目只决定 cwd、相对路径、项目配置/Skills 发现和 UI 归属。`/scratch` 保留为 Run 生命周期临时 locator，不是权限边界；显式 `/tmp` 始终是真实 OS 路径。
- Spawn 真实执行 `printf → cp → mv → chmod → cmp` 成功。Kernel 使用真实一次性 permit、`filesystem=unrestricted` profile 与 macOS Seatbelt 执行相同链路成功，profile 的 read/write roots 均为空。
- 最终 Backend 全量回归：`2576 passed, 13 skipped, 31 warnings`；无失败。warnings 为既有依赖弃用、实验接口与测试线程收尾告警。Frontend `npm exec tsc -- --noEmit` 通过。

2026-08-15 根据前端黑盒 Session `本地文件权限黑盒测试` 暴露的问题完成三次收口：

- 修正测试结论的审计依据：成功输出不能反推“零 HITL”。Current Permission Manifest 新增当前 Run 已解决审批的 `recent_decisions` 审计视图，包含 reason、risk、scope、capabilities 与 action preview；它不构成可复用授权。黑盒报告必须区分 submitted、approved、rejected 与 executed。
- 修复复合命令误判：Python/Node 写文件段之后附加独立的 `echo "$(cat ...)"`，不再让前一段被误判为 WebBridge 间接访问；真正位于解释器执行段内的间接 daemon 路径仍拒绝。`chmod ... && echo "$(ls ... | awk ...)"` 中只读 `awk` 验证不再退化为 `shell_effect_unprovable`。
- Skill locator、语义激活与 Skill 执行保持正交：glob/ls、`cp`、哈希 `/skills/<id>/...` 只是文件访问，不要求或产生 activation；`read_file` 成功读取权威 `/skills/<id>/SKILL.md` 不需要预授权，但按现有 Skill 协议记录语义 activation；直接执行或通过 Python、Node、Shell、`source` 等解释器运行 Skill entrypoint 仍要求该 activation。activation 不授予额外文件权限。
- Smart trusted-local 的 Spawn 与 Kernel 均使用真实 OS `HOME`；restricted / Strict 继续使用隔离 runtime home。`/tmp` trace 记录为 real path，不再因 locator 名称碰撞误记为 virtual。
- 文件工具返回的外部路径错误会恢复完整真实路径，避免冲突消息只显示被投影后的根路径。Spawn / Kernel 仅过滤已知 macOS gRPC fork 噪声，其他 stderr 与 OS 权限错误原样保留。
- 新增前端黑盒规约：每个 runner 使用全新测试根；Smart 与 Strict 分开 Run；真实敏感路径和持久化入口只做拒绝或不可执行 dry-run；明确断言 `copy_file` / `delete_file` 不在模型工具面，并测试标准 Shell 复制/删除、Virtual locator Shell 复制、真实 HOME、Parent/Subagent、原始 EACCES 与 effect-policy 反向断言。
- 移除模型侧 `delete_file` schema、注册、toolset/permission/evaluation/verification 清单与专用 hash 协议。普通精确文件删除统一使用受控 Shell `rm`；底层 `delete_external_file` 仍保留给 HostFileBroker 的事务、回滚与内部兼容链路，不暴露给模型。
- 定向回归：`285 passed`；最终 Backend 全量回归：`2588 passed, 14 skipped, 31 warnings`；无失败。新增的真实 macOS Seatbelt 用例在脱离外层嵌套沙箱后 `1 passed`，确认 Kernel unrestricted 使用真实 HOME 并可写普通宿主路径；默认 skip 仅因为该用例要求 `PUDDINGCLAW_RUN_KERNEL_E2E=1`。Frontend `npm exec tsc -- --noEmit` 通过。

2026-08-15 根据 Spawn 前端 E2E 完整记录完成四次收口：

- Permission Manifest 显式暴露 `approval_mode`、`backend_mode` 与 `filesystem_mode`，黑盒验收不再从行为猜测 runner。
- HITL outcome 以 request id 持久化到 Session，批准与拒绝都进入当前 Run 的 `recent_decisions`；一次性 grant 消耗后不会丢失审计，也不会被误当作 active grant。
- WebBridge 检查改为按 Shell segment 的真实 executable 判定；文件名包含 `python` 且后续仅 `echo "$(cat ...)"` 不再误报，真正的 `$(curl ...)` 或解释器间接访问仍拒绝。
- Smart unrestricted 对不存在的普通真实路径返回 not-found/ENOENT，不再回退成 HostFileBroker Grant 错误；`$HOME` 等全大写环境变量不再被解析为显式 Skill id。
- Smart unrestricted 的 `patch_file` 复用 HostFileBroker 的 CAS、原子 replace、候选验证、备份与 mutation receipt，只移除目录 Grant 前置条件，不绕过提交安全链。
- 清除了前端误选择 Project scope 遗留的 `wc -c *` allow rule，并将项目 permission rule revision 从 1 推进到 2，避免污染 Kernel Smart 验收。
- 新版前端黑盒 Prompt 固化在 `docs/runbooks/smart-filesystem-blackbox-e2e-prompt.md`；不再测试已移除的 `copy_file/delete_file`，改测标准 Shell `cp`/精确文件 `rm`，并要求 effect HITL 串行处理和完整 approval/rejection 审计。
- 定向回归：`674 passed`；最终 Backend 全量回归：`2591 passed, 14 skipped, 31 warnings`，无失败。

2026-08-15 根据最新 Spawn 复测完成五次收口：

- `patch_file` 的真实外部路径提交统一进入 HostFileBroker 的 compare-and-swap、原子 replace、验证、备份与 mutation receipt 流水线；回执 operation 明确记录为 `patch`，Parent 与 Subagent 均不得返回空 receipt。
- Smart unrestricted 下，文件工具的 not-found 与 OS 错误统一恢复完整真实路径，不再泄漏内部投影 basename，也不再变成 Grant/HITL。
- `persistence_write` 的目标识别补齐 Shell 控制符边界；即使目标位于 `if false` 等不可执行分支，`.zshrc` 与 `.ssh/authorized_keys` 仍先按 effect 请求确认，分支本身不会产生真实副作用。
- 高风险 persistence、sensitive/credential、package-install 与 destructive 请求只允许 one-time approval，API 同步校验 pending request 实际提供的 scope，前端即使提交 Session/Project 也会被拒绝；普通网络 effect 可按既有策略提供 Session，但不得扩大到 Project。
- Permission Manifest 按语义签名合并 durable decision 与 legacy active-grant fallback，同一次用户决策不再重复展示。
- 黑盒 Prompt 的联网反向断言改为不可执行分支中的 POST；公开 HTTPS GET/HEAD 是 Smart 的受控只读网络，不再被错误地用作“必须 HITL”探针。
- 清理本轮误操作污染：移除 `.zshrc` 的精确测试标记，并撤销当前 Session 的 persistence grant；项目 execute rules 保持为空。
- 相关链路回归：`616 passed`；最终 Backend 全量回归：`2609 passed, 14 skipped, 31 warnings`，无失败。

2026-08-15 根据 Spawn 复测的原始 Session 记录完成六次收口：

- 外部路径错误恢复只替换独立的 backend 投影 token，不再对错误字符串做 basename 全局替换；EACCES 的外层路径与 OS 异常路径均保持同一个完整真实路径，不再出现 `.../result/private/tmp/...` 重复拼接。
- Tool result source adapter 新增显式 `is_error` 契约，并由 DeepAgents/legacy Agent 的实时流与历史重放共同传入。失败或被 Harness 拒绝的网络命令不再根据输入 URL 合成 Web source；来源只代表工具实际成功获取的外部材料。
- 黑盒 Prompt 明确拆分 Virtual locator、Skill 语义 activation 与 Skill 执行：glob/ls、复制、比较和哈希不激活；成功读取权威 `SKILL.md` 按既有协议记录 activation，但不授予文件权限或执行脚本。
- 新增最新 Session 的两个精确回归用例；相关链路 `366 passed`，最终 Backend 全量回归：`2611 passed, 14 skipped, 31 warnings`，无失败。

2026-08-15 根据首次 Kernel Smart 全量验收完成七次收口：

- `update_todos` 的模型输入由“action 与全部可选字段的笛卡尔积”改为按 action 判别的 discriminated union。`create` schema 只允许 `pending/in_progress`，`complete/cancel/start/reopen` 必须引用稳定 `todo_id`；`create+completed` 不再是机器契约中的可表达状态。服务端保留生命周期与 evidence 校验作为纵深防御。
- `WorkspacePathRouterMiddleware` 移除 Smart host read 的 Spawn-only 二次条件。Smart unrestricted 下，Spawn 与 Kernel 的普通真实路径 `read_file/ls/glob/grep` 现在进入同一 direct-host/backend 路由；敏感路径仍由独立 effect policy 处理。
- 删除旧的 Kernel `read_file` 必须绕行 bounded `read_resource` 的测试假设，新增 Kernel 四文件工具矩阵；Router 观测 route 在 Smart 中统一记录为 `smart_host_read`，不再把 Kernel 误记成 Spawn。
- 相关链路回归：`447 passed`；最终 Backend 全量回归：`2618 passed, 14 skipped, 31 warnings`，无失败。

2026-08-15 完成 Kernel 跨平台挂载与前端 Prompt 启动收口：

- Linux bubblewrap 的 unrestricted profile 在 `--bind / /` 后立即结束普通文件根投影，仅继续覆盖 `/proc`、`/dev`、`/sys` 控制面；不再把 workspace 依据空 `write_roots` 二次 `--ro-bind`，scratch 与普通外部路径也保持宿主 OS 用户原有读写语义。mount policy digest 推进为 `filesystem-mode-aware-roots-v3`，旧 permit 不会跨语义复用。
- 新增 tracked Linux 真机 E2E：同一 unrestricted bubblewrap 命令必须同时写入 workspace、scratch 和普通外部目录，并看到真实 HOME。当前 macOS 主机只验证该用例正确 skip；Linux 发布前必须在真实 Linux/bubblewrap 主机以 `PUDDINGCLAW_RUN_KERNEL_E2E=1` 执行通过。
- 修复单行 Markdown 黑盒 Prompt 的路径跨 token 吞噬：本地路径解析在反引号、引号、换行与 NUL 处终止，不能从 `/tmp/...XXXXXX` 越过闭合反引号吞到后续 `file-tool.txt`；不存在的子路径也不能降级成对现存 `/tmp` 祖先目录的声明。managed-path 判定对 malformed/overlong 用户 token 返回非 managed，不再用 `ENAMETOOLONG` 中断首轮。
- 修复真实 Kernel Skill E2E 夹具缺少 `SKILL.md` 的问题。脱离外层嵌套沙箱的 macOS Kernel 定向回归：`158 passed, 1 skipped`（skip 为 Linux 真机用例）；最终 Backend 全量回归：`2622 passed, 15 skipped, 31 warnings`，无失败。
