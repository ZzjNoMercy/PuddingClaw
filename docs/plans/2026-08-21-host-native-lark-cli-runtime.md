# 飞书 CLI 本机原生运行时收敛

状态：已实施（Spawn / Kernel 本机路径）  
日期：2026-08-21

## 结论

飞书 CLI 是用户级本机工具，不是每次 Agent 调用都要重新物化的应用包。PuddingClaw 的本机运行边界统一为：

1. 只解析一个全局安装的官方 `lark-cli`；
2. PuddingClaw 为每个 owner/profile 提供稳定、权限为 `0700` 的配置目录；
3. Backend 以精确 argv 直接启动 CLI，不经过项目 Shell，也不进入 Spawn/Kernel 的临时 HOME；
4. App Secret 与用户 token 由官方 CLI 的 platform keychain 层保存和刷新；
5. PuddingClaw 只保存非秘密的 Profile 状态，以及设备授权流程中短期 continuation secret；
6. HITL、危险动作确认、命令白名单、输出脱敏继续由 PuddingClaw 控制。

受信任 Adapter 已经把命令、Provider 端点和 Profile 绑定冻结，因此普通状态查询与非删除型飞书操作不再因为“凭证 + Provider 网络”重复触发 Harness HITL。安装、删除、撤销以及无法由 Adapter 证明安全的动作仍保留独立确认；飞书 OAuth 浏览器同意属于 Provider 自身的用户授权，不等同于 Harness 命令审批。

Virtual path、项目目录、Spawn/Kernel runner 都不参与飞书凭证存储。Spawn 和 Kernel 看到的是同一套 CLI、同一登录态和同一授权规则。

## 第一性原理

需要平台托管的是“动作授权”，不是“复制一个 HOME”。

- 可执行文件身份：全局绝对路径 + `lark-cli --version`；
- 配置身份：`owner_user_id + credential_profile_id`；
- 飞书秘密真相源：官方 CLI 的 platform keychain 层；macOS 使用本地加密文件并由系统 Keychain 保护主密钥，Windows 使用 DPAPI，其他平台服从官方实现；
- 并发边界：Puddingclaw Profile lock + 官方 token store 自己的 account CAS；
- 临时授权秘密：PuddingClaw 加密 Flow Store；
- 项目文件权限：仍由当前 smart/strict permission mode 决定。

因此，旧链路中的“Vault tar → 临时 HOME → Host credential projection → CLI → tar → Vault CAS”不是本机安全边界。它制造了两份 token、两个 CAS 和两套恢复语义，现已从飞书本机执行主路径移除。

## 开源与官方依据

### larksuite/cli（官方）

- 官方安装流程使用用户级 CLI，并把 Agent Skills 全局安装到 Agent skill 根：[`README`](https://github.com/larksuite/cli#installation--quick-start)。
- 官方配置实现明确支持 `LARKSUITE_CLI_CONFIG_DIR`：[`internal/core/config.go`](https://github.com/larksuite/cli/blob/main/internal/core/config.go)。
- 官方 token store 通过 platform keychain 层按 `appId:userOpenId` 保存、刷新和 CAS：[`internal/auth/token_store.go`](https://github.com/larksuite/cli/blob/main/internal/auth/token_store.go)。macOS 的值落在 `~/Library/Application Support/lark-cli/*.enc`，主密钥默认由系统 Keychain 保护，并支持显式 file fallback：[`internal/keychain/keychain_darwin.go`](https://github.com/larksuite/cli/blob/main/internal/keychain/keychain_darwin.go)。
- 官方 Agent Skill 规定机器调用使用 `LARKSUITE_CLI_NO_UPDATE_NOTIFIER=1` 与 `LARKSUITE_CLI_NO_SKILLS_NOTIFIER=1`：[`skills/lark-shared/SKILL.md`](https://github.com/larksuite/cli/blob/main/skills/lark-shared/SKILL.md)。
- 官方 Skills 安装/更新调用 `npx skills add larksuite/cli -g -y`：[`internal/selfupdate/updater.go`](https://github.com/larksuite/cli/blob/main/internal/selfupdate/updater.go)。

### DeerFlow（本地源码）

参考快照：`a5acc25de6742b2166b3f41c97bd895822277b94`。

参考文件：

- `backend/packages/harness/deerflow/integrations/lark_cli.py`
- `backend/packages/harness/deerflow/integrations/lark_broker.py`
- `backend/packages/harness/deerflow/sandbox/tools.py`
- `backend/packages/harness/deerflow/sandbox/security.py`

DeerFlow 的实际权限边界不是“凭证 + 网络 = 每次 HITL”：

- 本机 `bash` 是否可见由 Sandbox Provider 决定；可信本机环境可以显式开放，隔离环境直接提供 Bash；
- 命中 `lark-cli` 后，运行时只注入对应用户的持久凭证目录；
- Broker 接收 argv 后以 `shell=False` 原样启动 CLI，默认 `deny_subcommands=()`，不存在普通查询、发送、创建或上传前的逐命令 HITL；
- Broker 的可选 denylist 用于收窄命令面，不是用户审批系统；
- OAuth 同意发生在飞书页面，安装发生在管理员入口，两者与日常 Provider 命令分离。

PuddingClaw 采用同一原则：全局共享 CLI、用户级持久目录、精确 argv 的 host subprocess，并以 Adapter 的确定性分类替代 DeerFlow 默认空 denylist。因此 `auth status --verify`、查询以及非删除型写操作直接执行；安装、删除、登出、配置移除和 CLI 返回的破坏性确认仍进入独立 HITL。没有照搬的部分：DeerFlow 的 `LARKSUITE_CLI_DATA_DIR` 和远端 broker 是其部署/sidecar 契约；官方 stock CLI 当前以 `LARKSUITE_CLI_CONFIG_DIR + OS keychain` 为准，本机 PuddingClaw 不再人为增加 broker。

| 动作 | DeerFlow | PuddingClaw smart mode |
|---|---|---|
| `auth status --verify`、读取/查询 | 直接执行 | 直接执行 |
| 发送、创建、更新、上传 | 直接执行 | Adapter 命令面内直接执行 |
| 未识别或含 Shell 拼接的飞书命令 | Broker 默认不分类；可由 denylist 拒绝 | Adapter fail-closed 拒绝，不回落 Shell |
| 删除、登出、配置移除 | Broker 默认直接执行 | 独立破坏性 HITL |
| OAuth 用户同意 | 飞书页面完成 | 飞书页面完成，不叠加 Harness HITL |
| CLI 安装/更新 | 管理员入口 | 独立安装 HITL |

## 存储边界

| 数据 | 真相源 | 是否进入 Agent argv/env | 是否进入 Pudding Vault archive |
|---|---|---:|---:|
| 飞书 Profile 非秘密配置 | `users/<owner>/integrations/lark-cli/profiles/<profile>/config` | 否 | 否 |
| 飞书 App Secret / 用户 token | 官方 platform keychain 层：macOS 加密文件 + Keychain 主密钥；Linux 加密文件 + 本地主密钥；Windows DPAPI | 否 | 否 |
| OAuth device code | PuddingClaw Authorization Flow Store，短期加密 | 否（恢复时作为官方参数提交） | 不进入 Profile archive |
| Provider API key | 现有 Provider Registry / CredentialVault | 仅受控注入 | 不变 |
| MinerU API key | 现有 Skill Secret Store | 仅受控注入 | 不变 |

“统一存储”指统一的安全原则和 UI 生命周期，不表示把所有秘密硬塞进一个 JSON 或同一个数据库。官方 CLI 能自行可靠刷新 token 时，应让它继续拥有 token；PuddingClaw 不复制 bearer credential。

## 安装与更新

- 二进制：PuddingClaw 的受控安装动作执行精确版本的 `npm install --global @larksuite/cli@<version>`；普通 Agent 命令不能自行替换它。
- Skills：官方上游发布方式是 `npx skills add larksuite/cli -g -y`；PuddingClaw 内仍由现有 Skill Manager 获取并提交到 Pudding 用户 Skill 根，避免依赖某个外部 Agent 的 `~/.agents/skills` 扫描规则。飞书凭证服务不复制 Skills。
- 环境探测：Connector 状态直接运行全局二进制的 `--version`，不再读取 Pudding 共享 Node Toolchain manifest。
- 更新校验：审批计划绑定绝对可执行路径和版本，不再绑定无关的 Python/Kernel runtime image。

## 旧数据迁移

首次调用某个 Profile 时：

1. 若存在旧 `vault.enc` 且原生目录尚未初始化，严格校验旧 tar 路径和文件类型；
2. 配置文件复制到新的 Profile config 目录；
3. 旧的加密 file-keychain 数据只复制到官方 native credential 目录，不解密 token；
4. CLI 成功执行后删除旧 `vault.enc/profile.json`，避免长期存在第二份 token；
5. 后续调用只读写官方原生状态。

迁移不接受 symlink、路径穿越或覆盖已有 native credential 文件。

## 保留与删除

保留：

- `ManagedCliAdapter` 的精确命令解析和能力分类；
- smart mode HITL 与破坏性二次确认；
- Profile 元数据和项目到 Profile 的选择；
- 授权 URL 校验、设备码加密、输出脱敏；
- 通用 Skill runtime、Provider key、Skill Secret 基础设施。

从飞书本机主路径删除：

- 每次调用创建 private HOME；
- token tar import/export；
- `HostFileBroker` 式凭证搬运；
- 飞书 CLI 的 Pudding Node Toolchain release/lease/rollback；
- Spawn 与 Kernel 各自维护飞书登录态；
- 用 managed runtime image digest 判断飞书二进制是否变化。

远端 Docker/Kubernetes 若未来需要跨机器运行，应单独采用 remote broker/sidecar 威胁模型，不能反向污染本机 Spawn/Kernel 路径。

## macOS 与后续平台验证

macOS 当前为主验收平台。官方 CLI 默认把秘密写入本地加密文件，并从系统 Keychain 读取加密主密钥；若 headless/launchd 上下文无法访问 Keychain，应走官方支持的 `lark-cli config keychain-downgrade`，将同一主密钥显式物化为权限受限的本地文件。UI 必须提示安全边界变化，PuddingClaw 不静默降级。

Linux 与 Windows 需要分别按官方当前 platform keychain 实现做真机验证；Linux 当前使用 `~/.local/share/lark-cli` 下的 AES-GCM 加密文件与本地主密钥，Windows 当前使用 DPAPI + HKCU。两者属于跨平台发布验收，不改变上述架构。

旧 Pudding Vault 保存的是 Linux file-keychain 表示，可无解密迁移到 Linux，或在 macOS 上转换成官方 `master.key.file + *.enc` 表示；它不能按字节安全转换成 Windows DPAPI 数据。Windows 遇到旧 Profile 时必须失败关闭、保留旧 Vault，并要求新建原生 Profile 后重新授权，不能假装迁移成功后删除旧数据。

## 验收项

- Connector 在不存在 Pudding Toolchain release 时仍能识别全局 CLI；
- 两个 Profile 的 config 目录不同；
- Spawn/Kernel 对同一 Profile 得到同一身份状态；
- 普通调用不创建/更新 `credentials/lark/<profile>/vault.enc`；
- 旧 archive 只迁移一次，成功后被移除；
- `config init`、`auth login` 的公开 URL 可恢复，device code 不出现在 ToolMessage；
- 删除/撤销等高风险动作仍需现有 HITL；
- Provider key 与 MinerU key 的现有注入、脱敏和撤销流程无回归。
