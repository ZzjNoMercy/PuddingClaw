# Managed Lark 重新授权与 Token 生命周期优化方案

> 状态：Implemented（2026-07-28 当前阶段）
> 日期：2026-07-28
> 关联：[ADR-004](../adr/ADR-004-user-runtime-toolchain-and-credential-profiles.md)、[ADR-005](../adr/ADR-005-managed-external-authorization-flow.md)

## 1. 决策摘要

在用户认知中，“重新授权”是对现有连接的直接覆盖，不是先删除旧配置再从零创建。因此 PuddingClaw 将其定义为一个事务式替换操作：

```text
保留当前 Credential Profile
  -> 在隔离 staging 中完成新授权
  -> 验证新身份与 scope
  -> 原子覆盖 Profile Vault
```

在新授权成功提交前，Backend 不执行 `lark-cli auth logout` 或 `lark-cli config remove` 去破坏持久 Profile。只有用户明确表达“注销、撤销、删除连接”时，才进入独立的破坏性 `revoke` 流程。

本方案同时解决以下问题：

- 主动重新授权错误地被 Agent 编排为“先删除、再配置”。
- 全量重配与残留 Authorization Flow 冲突，返回 `authorization flow base Profile state changed`。
- User OAuth 链接过期后需要用户和 Agent 多绕一轮才能获得新二维码。
- Agent 根据 CLI `--help` 猜测 `--force-init`、读取临时容器路径或重复删除配置。
- access token、refresh token、Bot/App 配置具有不同生命周期，却被混成一个 Profile 状态。
- 多个临时 Runner 可能同时刷新同一个 refresh token。

## 2. 用户语义

Backend 必须区分三种意图，不能把它们交给 Agent 自行翻译成破坏性命令。

| 用户表达 | 规范语义 | Backend 模式 | 是否删除旧 Profile |
|---|---|---|---|
| “重新授权”“重新登录”“刷新飞书授权” | 覆盖 User OAuth 授权 | `user_reauthorize` | 否 |
| “重新配置”“换应用”“重建飞书连接” | 覆盖完整 App + User 配置 | `full_replace` | 否 |
| “注销”“撤销”“删除飞书连接” | 主动销毁连接 | `revoke` | 是，需破坏性确认 |

### 2.1 默认认知

没有出现“删除、注销、撤销”等明确破坏性措辞时，“重新授权”一律解释为覆盖更新：

```text
重新授权 != auth logout
重新授权 != config remove
重新授权 == 在保留旧连接的前提下建立新授权并原子覆盖
```

### 2.2 明确撤销后再创建

如果用户明确要求“立即撤销旧授权，然后重新创建”，这是两个有顺序的目标：

1. 对 `revoke` 单独进行破坏性确认。
2. 撤销成功后创建新的 `full_replace` Flow。

此路径明确放弃旧 Profile 的回滚能力，不能与普通“重新授权”合并。

## 3. 控制面接口

长期目标是让 Agent 表达授权目标，而不是编排 Provider CLI：

```text
start_managed_authorization(
  provider="lark",
  profile_id="lark_default",
  mode="user_reauthorize | full_replace",
  domains=["all"]
)

resume_managed_authorization(flow_id)

revoke_credential_profile(profile_id)
```

Lark Adapter 负责把语义动作映射为受管命令、阶段和校验：

- `user_reauthorize`：复用已验证的 App/Bot 配置，仅启动 User consent。
- `full_replace`：从空 staging 开始 App configuration，再启动 User consent。
- `revoke`：执行 Provider 本地撤销，并同步取消所有相关 Flow/Runner。

现有 `lark-cli` 命令可以暂时作为兼容入口，但 Middleware 必须把它们归一化为上述语义动作。Agent 不拥有 Profile 选择、continuation、device code、Browser Runner 或最终提交权。

## 4. Profile 与 Flow 的职责分离

Credential Profile 是长期、用户级、跨 Project/Session 复用的身份连接；Authorization Flow 是短期替换事务。

### 4.1 Profile 健康状态

```text
Profile:
  unconfigured | active | repair_required | revoked

Bot/App health:
  ready | repair_required | unavailable

User OAuth health:
  ready | authorization_required | expired | revoked
```

`reauthorizing` 不再作为长期凭证健康状态。它属于 Authorization Flow。这样主动重新授权等待期间，旧的、仍然有效的 User 身份不会被错误标记为不可用。

### 4.2 Flow 状态

```text
created
  -> starting
  -> awaiting_user
  -> collecting
  -> verifying
  -> completed | failed | cancelled
```

Device-code/browser 链接过期是一次 phase attempt 的结果，不应自动终止整个用户授权意图。

### 4.3 Phase attempt

```text
Authorization Flow auth_xxx
  phase: user_consent
  attempt 1: expired
  attempt 2: awaiting_user
```

Flow 保持稳定的 `flow_id`，每次生成新链接增加 `attempt` 和 `revision`。Frontend 使用 `(flow_id, phase_id, attempt)` 判断卡片新旧关系。

## 5. User-only 重新授权事务

当 App/Bot 配置健康时，主动重新授权直接进入 User consent，不重复 Step 1：

```text
读取并锁定现有 Profile revision
  -> 将 Provider State 复制到 encrypted staging
  -> Backend 仅在 staging 中执行 auth logout
  -> 发起新的 User OAuth device flow
  -> 返回 URL/二维码并暂停 Agent
  -> 用户自然语言回复“好了”
  -> Backend 使用加密 continuation collect
  -> 独立执行 auth status --verify
  -> 验证 Bot、User、tokenStatus 和请求的 domain/scope
  -> 原子覆盖 Profile Vault
```

不变量：

- 持久 Vault 在最终提交前保持字节级不变。
- staging 中必须清除旧 User 登录态，防止旧 Token 冒充本次授权成功。
- staging logout 由 Backend 执行，Agent 不得执行或看到其 continuation。
- 新授权失败、取消或链接过期时，旧 Vault 不受影响。
- 只有本次 continuation 产生的新登录态通过独立验证，才能返回 `authorization_completed: true`。

User-only 卡片应显示实际阶段数：

```text
飞书用户授权 · 1/1
应用配置已经验证，本次只更新你的用户授权。
```

不得继续显示“第 2/2 步”，以免用户误以为遗漏了第一步。

## 6. Full replacement 事务

“重新配置、换应用、重建连接”使用完整替换，但仍不先删除旧 Profile：

```text
保留旧 Vault
  -> 创建空 staging
  -> Step 1/2: app_configuration
  -> Step 2/2: user_consent
  -> 独立验证完整身份
  -> 原子覆盖 Vault
```

如果同一 Profile 存在旧的非终态 Flow，`full_replace` 必须原子 supersede：

```text
取得 Profile 写锁
  -> 旧 Flow cancelled(reason=superseded)
  -> 删除旧 continuation/staging
  -> 终止旧 Browser Runner
  -> 释放旧 Browser Job lease
  -> 创建新的 full_replace Flow
```

不能把新的 App 配置请求续接到旧的 User consent Flow，也不能向 Agent 返回裸 `ValueError: authorization flow base Profile state changed`。

## 7. 链接过期与自然语言续跑

用户回复“好了”时，Backend 执行 `resume_managed_authorization(flow_id)`。如果当前 attempt 已过期：

1. 将旧 attempt 标记为 `expired`。
2. 删除旧 device code/PKCE continuation。
3. 在同一个 Flow/phase 内创建 `attempt + 1`。
4. 立即生成并返回新的 URL/二维码。
5. 旧卡片标记为失效，新卡片保持消息级可见。

示例响应：

```json
{
  "status": "awaiting_user_browser",
  "reason": "authorization_attempt_expired",
  "flow_id": "auth_xxx",
  "attempt": 2,
  "supersedes_attempt": 1,
  "authorization_request": {
    "verification_url": "https://accounts.feishu.cn/...",
    "expires_at": "2026-07-28T16:30:00+08:00"
  },
  "output": "上一授权链接已过期，已为你生成新链接。"
}
```

该续发必须发生在一次 Backend 调用内。不得返回失败后要求 Agent 再执行一条 `auth login`，也不得让用户继续使用旧链接。

为避免 `profile_lock(thread_local=False)` 重入死锁，刷新 attempt 的实现应在锁内完成状态终结和 restart intent 落盘，释放锁后再使用 fresh execution plan 启动 Provider 调用，最后重新取得锁提交新 attempt。

## 8. Revoke 的原子联动

`config remove` 或等价 Provider 撤销成功时，Backend 必须在同一个受管操作中完成：

- Profile 状态变为 `revoked`。
- 活跃 Authorization Flow 变为 `cancelled(reason=profile_revoked)`。
- continuation 和 staged state 被删除。
- Browser Runner 被终止。
- Browser Job lease 被释放。
- Frontend pending authorization 卡片被标记为失效。

撤销后再执行 full replacement 会创建全新的 Flow，不得复用撤销前的 base state revision。

## 9. Token 生命周期

本阶段不开发定时任务。采用请求驱动的三层机制。

### 9.1 请求前刷新

Provider 操作前，如果 Adapter 判断 access token 已到刷新窗口：

```text
Profile lock
  -> 重新读取最新 Vault revision
  -> 通过 Provider CLI/Adapter 执行 refresh
  -> 独立验证新状态
  -> 原子保存新 access/refresh token
  -> 执行业务命令
```

### 9.2 认证错误后刷新并重试一次

只有被证明属于 User credential 的错误才触发刷新或重新授权：

- `token_expired`
- `refresh_token_expired`
- `invalid_token`
- `invalid_grant`
- `login_required`
- `not_logged_in`

处理顺序：

```text
Provider operation
  -> proven User credential failure
  -> 在 Profile lock 下尝试 refresh
  -> 成功：提交新状态并重试原命令一次
  -> refresh 永久失败：启动 user_reauthorize Flow
```

Bot scope 不足、User scope 不足、Provider 5xx、网络超时和删除确认 exit 10 不属于 Token 修复，不能误触发 OAuth。

### 9.3 并发刷新

同一 owner/provider/profile 的 refresh exchange 必须串行化：

- 文件锁跨越 refresh-token exchange 和 Vault commit。
- 后续 Runner 取得锁后先检查 Profile revision。
- 如果其他 Runner 已提交新状态，直接采用新 revision，不再次消费旧 refresh token。
- 刷新结果使用原子写入，失败时不覆盖旧 Vault。

该机制借鉴 Grok Build 的用户级 auth store、refresh lock、请求前刷新和 401 重试，但继续保留 PuddingClaw 的加密 Vault 与临时 Provider Runner，不把 Token 暴露给普通 Project/Session 容器。

## 10. Adapter 与 Backend 边界

通用 Backend 状态机负责：

- Profile/Flow/attempt 数据模型。
- 锁、幂等、supersede、过期续发和原子提交。
- continuation/staging 加密。
- Browser Runner 生命周期。
- 结构化前端事件。
- 重启恢复和旧状态迁移。

Lark Adapter 负责：

- `user_reauthorize` 与 `full_replace` 的阶段图。
- App/Bot/User 身份评估。
- Provider 命令和安全 argv。
- URL/provider error 校验。
- refreshable 与 terminal refresh error 分类。
- 最终 Bot/User/domain/scope 验证。

Skill/Tool Guide 只负责帮助 Agent理解用户业务语义，不能决定阶段推进、凭证提交或异常恢复。

## 11. Agent 与 Tool Guide 规则

`backend/prompts/deepagents/TOOL_GUIDES.md` 应补充：

- 用户要求重新授权已有 Profile 时，表达 `user_reauthorize` 目标；兼容期内只执行标准的 `lark-cli auth login --domain all --no-wait --json`。
- 重新授权前后不得主动执行 `auth logout` 或 `config remove`。
- 只有用户明确要求撤销时，才调用 destructive revoke。
- `credential_profile_incomplete`、Bot/App 验证失败或 Backend 返回 `full_replace_required` 时才升级为完整替换。
- URL 过期由 Backend 自动生成新 attempt，Agent 不再执行第二条 login。
- 不使用 `--force-init`、裸 `--device-code`、后台 shell、容器内文件路径或手工二维码命令。
- Backend 返回 `awaiting_user_browser` 后结束当前 Agent 轮次；自然语言“好了”足以续跑。

这些规则是减少模型绕路的 UX 约束；Backend 仍需独立保证全部安全不变量。

## 12. 结构化错误与恢复动作

不得把内部异常裸露给模型后让其猜测恢复命令。至少提供：

| 错误码 | 含义 | Backend/Agent 下一步 |
|---|---|---|
| `authorization_attempt_expired` | 当前浏览器链接过期 | Backend 已自动返回新 attempt |
| `authorization_flow_superseded` | Flow 被新的替换事务取代 | 展示新 Flow，旧卡失效 |
| `full_replace_required` | App/Bot 配置无法支持 User-only 授权 | 发起 `full_replace` |
| `authorization_profile_conflict` | 授权期间 Vault 被其他事务更新 | 不覆盖；重新评估最新 Profile |
| `credential_profile_incomplete` | Provider-native Secret State 缺失 | 发起 `full_replace`，不重试 User login |
| `authorization_flow_missing` | 没有可续跑 Flow | 重新解释用户意图并发起新 Flow |

每个错误信封必须含结构化 `next_action`，但 `next_action` 是 Backend 语义动作，不是要求 Agent猜测额外 CLI flag。

## 13. 迁移与当前损坏状态恢复

Backend 启动和下一次授权请求时，应审计：

```text
Profile.status == revoked/repair_required
AND active Flow.base_state_revision != current Profile state revision
```

发现此类 `revoked Profile + stale Flow` 时：

1. 将旧 Flow 标记为 `cancelled(reason=stale_base_state)`。
2. 清理其 continuation、staging 和 Browser Job。
3. 保留 Profile 当前真实状态。
4. 下一次 `full_replace` 创建全新 Flow。

这使当前测试环境可以直接重新要求配置，无需人工删除 `~/.puddingclaw` 内部文件。

## 14. 测试与验收标准

### 14.1 User-only 重新授权

- 健康 Profile 一跳进入 User consent，卡片显示 `1/1`。
- 等待期间持久 Vault 字节不变。
- staging logout 恰好执行一次。
- 旧 Token 不得成为本次成功证据。
- 成功后原子覆盖 Vault 并返回 `authorization_completed: true`。
- 失败/取消后旧的健康 Profile 仍可使用。

### 14.2 过期与幂等

- `auth resume` 遇到过期 attempt，在同一次响应中返回新 URL/QR。
- `flow_id` 不变，`attempt` 单调递增。
- 重复说“好了”不会创建并发 attempt。
- 旧卡片失效，新卡片保持可见。
- Bot/App 损坏时返回 `full_replace_required`，不死锁、不无限重试。

### 14.3 Full replacement 与 revoke

- `full_replace` 自动 supersede 旧 User Flow。
- 旧 Browser Runner、lease、continuation 和 staging 全部清理。
- `config remove` 成功后同步取消 Flow。
- `revoked Profile + stale Flow` 可自动迁移。
- `--force-init` 和裸 device-code 继续被 Adapter 拒绝。

### 14.4 Token refresh

- 请求前刷新成功后保存新 access/refresh token。
- 结构化 User 401 只刷新并重试一次。
- 两个 Runner 并发时只发生一次 refresh exchange。
- refresh 永久失败自动进入 User-only Flow。
- scope、Bot、网络和普通 Provider 错误不会误触发 OAuth。

### 14.5 恢复

- Backend 重启后恢复同一个 pending Flow/attempt。
- 旧 ToolMessage 或旧卡片不能推进被 supersede 的 attempt。
- Profile/Toolchain/Adapter contract 变化时 fail closed，并返回结构化恢复动作。

建议的相关回归范围：

```bash
cd backend
.venv/bin/python -m pytest \
  tests/test_runtime_identity.py \
  tests/test_managed_cli_argv_fidelity.py \
  tests/test_context_optimizations.py -q
```

前端至少覆盖授权卡片的 `(flow_id, phase_id, attempt)` 新旧投影、过期卡片退役和动态 `1/1`/`2/2` 文案。

## 15. 落地顺序

1. 修复 Flow supersede、revoke 联动和 stale Flow 自动迁移。
2. 实现过期 attempt 自动续发。
3. 实现 User-only `1/1` 与 Profile/Flow 状态分离。
4. 更新 Tool Guide 和 ADR-005 的规范性条款。
5. 实现请求前刷新、认证错误后刷新一次、跨 Runner refresh lock。
6. 引入语义授权工具，逐步停止 Agent 编排原始 Provider CLI。

## 16. 明确不做

- 不放行 `--force-init`。
- 不把 `.lark-cli` 或 Vault 挂入普通 Project/Session 容器。
- 不让 Agent接触 refresh token、device code 或 PKCE verifier。
- 不用 `config remove` 作为重新授权的修复步骤。
- 不延长 Provider 下发的 device-code TTL。
- 不新增前端确认按钮，自然语言反馈足够。
- 本阶段不开发定时任务或后台定时刷新器。

## 17. “扩展、技能、连接器、MCP、CLI”的产品定义

界面不能直接把底层实现方式当成用户概念。PuddingClaw 采用以下分层：

| 产品概念 | 用户关心的问题 | 当前实现示例 |
|---|---|---|
| 扩展 | PuddingClaw 新增了什么能力 | 技能、连接器；未来可包含专家/工作流 |
| 技能（Skill） | Agent 是否知道怎样完成某类任务 | `lark-doc`、`lark-im` 等 `SKILL.md` 指南 |
| 连接器（Connector） | Agent 连接到哪个外部系统、使用哪个账号/身份、授权是否有效 | 飞书连接器及其默认 Credential Profile |
| MCP | 能力通过什么标准协议提供 | 智慧芽等 MCP Server；未来也可以成为某个连接器的驱动 |
| CLI | Backend 通过什么本地程序执行 Provider 操作 | `@larksuite/cli` 提供的 `lark-cli` |

因此：

```text
连接器 != MCP
连接器 != CLI
连接器 = 面向用户的外部系统连接与身份生命周期
MCP / CLI / REST SDK / Desktop Bridge = 连接器可选的实现驱动
```

这与 Grok/Codex 一类产品中提到的 Apps、Connectors 或 MCP tools 是同一层次问题：产品层展示“接入了哪个业务系统”，执行层才关心它经由 MCP、CLI 还是 API。不能因为某个 Provider 当前由 MCP 实现，就把用户的账号连接、授权状态和生命周期缩减成一条 MCP Server 配置。

### 17.1 当前飞书连接器到底是什么

当前 PuddingClaw 的飞书连接器是一套组合能力：

```text
飞书连接器（产品对象）
  ├── Toolchain：Node 包 @larksuite/cli，入口 lark-cli
  ├── Driver：Backend-owned LarkCliAdapter
  ├── Identity：lark_default Credential Profile
  ├── Secret State：~/.puddingclaw 下的加密 Vault
  ├── Authorization：Managed Authorization Flow
  └── Usage Guidance：lark-* Skills
```

它当前**不是 MCP Server**。Agent 在对话里看起来运行 `lark-cli ...`，但命令会被 Backend Adapter 接管，在短生命周期的 Provider/Browser-auth Runner 中执行；普通 Project/Session 容器不读取凭证。飞书 Skills 负责告诉 Agent“何时、如何调用”，但不保存账号连接，也不拥有授权状态。

界面只把“飞书”展示为一个连接器，不把 20 多个 `lark-*` Skills 展示为 20 多个连接器；Skills 仍在“技能”页统一管理。

### 17.2 Connector Adapter 契约

Backend 应形成通用 `ConnectorAdapter` 契约，Lark 是第一个 Adapter，而不是把飞书特例散落在 Agent 提示词或前端：

```text
ConnectorDefinition
  id / provider / display_name / icon
  driver_kind: managed_cli | mcp | http_api | desktop_bridge
  capabilities
  identity_kinds
  authorization_plan(profile_health, requested_mode)
  verify_profile(profile_id)
  start_or_resume_authorization(...)
  revoke(profile_id)
  public_status(profile_id)
```

通用 Backend 状态机管理 Flow、attempt、Profile、并发锁、原子提交和结构化投影；Adapter 只负责 Provider 命令、授权阶段、结果解析、凭证路径及错误分类。以后接入钉钉、企业微信或其他 CLI/MCP 服务时，前端复用同一套连接器页面和状态，不需要让 Agent 手动发明另一套生命周期。

## 18. 扩展页信息架构优化

参考图的优点是把“专家、技能、连接器”分开，但当前 PuddingClaw 只有稳定的 Skills 与一个飞书连接器，没有成熟的专家市场。第一版不复制空壳栏目，采用更克制的两层结构：

```text
侧栏
  扩展

扩展页顶部
  技能 | 连接器

技能
  已安装技能 / 导入技能 / 搜索

连接器
  已连接 / 可连接 / 需要处理
```

调整现有 `/skills` 页面而不立即改 URL：

- 页面名称从“技能/MCP”提升为“扩展”。
- 顶部分段从 `技能 | MCP` 改成 `技能 | 连接器`。
- MCP Server 不再作为与 Skill 平级的唯一产品类别；只有注册了 Connector Definition 的 MCP 集成才进入连接器目录，并声明 `driver_kind=mcp`。纯工具型、无账号生命周期的 MCP Server 保留在设置/高级扩展配置中，不能自动伪装成“已连接账号”。
- 当前仅展示一个飞书连接器，不用为了版式填充静态或不可用的连接器。
- “专家”不在本轮加入；只有形成可持久化、可召唤的 Agent preset 后，才作为第三个扩展类型进入信息架构。

### 18.1 连接器目录

飞书卡片使用参考图的紧凑双列/自适应卡片语言，但状态必须来自 Backend 结构化数据：

```text
┌────────────────────────────────────────────┐
│ [飞书图标]  飞书                    ● 已连接 │
│ 文档、消息、日历、多维表格等飞书能力        │
│ 身份：Bot 可用 · 用户授权有效                │
│                                    [  >  ] │
└────────────────────────────────────────────┘
```

响应式规则：

- 宽屏两列；窄屏一列。
- 卡片整块可打开状态弹窗，右侧箭头仅作可发现性提示。
- 不使用 `+` 表示已经配置的连接器；未配置时才显示“连接”。
- 绿色圆点只代表 Backend 最近一次验证后 Profile 可执行，不能仅凭 CLI 已安装或本地配置文件存在就显示绿色。
- `authorizing` 使用蓝色进行中状态；`authorization_required/expired` 使用橙色；`repair_required` 使用红色；`unconfigured/revoked` 使用灰色。
- 搜索作用于连接器名称、Provider 与能力描述；只有一个连接器时仍保留一致页面结构，但不展示无意义的分类筛选。

### 18.2 飞书连接器状态弹窗

当前只接入一个飞书连接器，点击目录卡后不跳转到独立详情页，使用居中的连接器状态弹窗。它参考示例图的轻量层级，但不能只显示营销描述和“解绑”；核心任务是让用户一眼看懂“环境是否可用、哪一层授权有效、下一步是什么”。

弹窗使用四段结构：

```text
┌──────────────────────────────────────────────────┐
│                                            [ × ] │
│                    [飞书图标]                    │
│                       飞书                       │
│     消息、文档、日历、多维表格等飞书能力         │
│                                                  │
│  运行环境                              ● 可用    │
│  托管 CLI · v1.0.78 · 所有项目可用               │
│                                                  │
│  授权状态                                        │
│  应用/Bot 配置                         ✓ 已就绪  │
│  用户数据授权                          ✓ 有效    │
│  最近验证                              2 分钟前  │
│                                                  │
│  [重新授权]                   [去使用飞书]        │
│  完整重新配置 · 断开连接…                        │
└──────────────────────────────────────────────────┘
```

#### 运行环境

“运行环境”展示的是 PuddingClaw 管理的共享 Toolchain，不是当前 Project/Session 容器：

- 驱动类型：托管 CLI。
- CLI 名称与版本：`lark-cli vX.Y.Z`。
- 可用范围：所有项目可用。
- 环境健康：`available / installing / update_required / broken / unavailable`。
- 最近环境检查时间；Toolchain revision 只在“技术详情”折叠区显示。

不能显示 `/home/puddingclaw`、临时 Runner ID 或 Docker 容器名。这些不是用户需要管理的持久环境；弹窗中的“环境可用”表示共享 Toolchain 已安装并通过 Adapter 合约检查。

#### 授权状态

授权必须拆成两行，不能合并成一个模糊的绿色圆点：

- 应用/Bot 配置：`unconfigured / ready / repair_required`。
- 用户数据授权：`authorization_required / authorizing / ready / expired / repair_required`。
- Profile 标签与最近验证时间。
- 有效身份摘要，例如“Bot 可用 · 用户授权有效”。

目录卡上的总状态由 Backend 汇总，但弹窗保留两层身份的真实差异。例如 Bot 仍可用而 User OAuth 过期时，总状态显示“需要重新授权”，弹窗明确指出只有“用户数据授权”过期。

#### 动态操作

底部按钮随结构化状态变化，避免任何状态都固定显示“解绑/去试试”：

| 当前状态 | 主操作 | 次操作 |
|---|---|---|
| 环境未安装/损坏 | 安装或修复环境 | 关闭 |
| 未配置 | 连接飞书 | 关闭 |
| 等待浏览器授权 | 回到对话继续（预填自然语言反馈） | 打开授权链接 |
| User OAuth 过期 | 重新授权 | 去使用 Bot 能力（若可用） |
| 已连接 | 去使用飞书 | 重新授权 |
| 完整配置损坏 | 完整重新配置 | 关闭 |

“完整重新配置”和“断开连接…”放入次级文字操作或更多菜单，避免和高频“去使用”争夺主视觉。断开连接保持红色破坏性语义并二次确认。

“去使用飞书”关闭弹窗并创建/切换到空任务，在 composer 中预填可编辑提示，例如“使用飞书帮我……”，不自动发送、不替用户选择具体副作用操作。

#### 安全与信息边界

- 不显示 token、App Secret、device code、PKCE verifier 或容器内真实凭证目录。
- 不把 `/home/puddingclaw/.lark-cli` 描述成持久存储位置；它只是 Runner 内的临时 Provider-native 路径。
- 可以显示 Profile 的用户标签，但内部 `profile_id` 默认放在“技术详情”折叠区。
- “重新授权”映射到 `user_reauthorize`，直接进入 `1/1` User consent。
- “完整重新配置”映射到 `full_replace`，进入 `1/2` App configuration 和 `2/2` User consent。
- “断开连接”映射到 destructive `revoke`，必须展示影响范围并确认。
- 弹窗按钮只发起语义操作；浏览器授权完成后的续跑使用自然语言反馈。弹窗可预填“我已完成飞书授权，请继续验证”，但不直接把按钮点击当作授权成功证据，也不新增每一步强制确认按钮。
- 点击遮罩或关闭按钮只关闭弹窗，不取消正在等待的授权 Flow；取消 Flow 必须使用明确操作。

#### 弹窗视觉与可访问性

- 桌面端宽度建议 `640–720px`，最大高度不超过视口并允许内容滚动；移动端使用近全屏 bottom sheet。
- Logo、名称和一句能力摘要居中；运行环境和授权状态使用左对齐信息行，避免大段居中文案影响扫描。
- 遮罩降低背景干扰，但不销毁扩展页状态。
- 打开后焦点进入弹窗，`Esc` 关闭，焦点被限制在弹窗内，关闭后返回原连接器卡片。
- 状态不能只依赖颜色，必须同时有图标和文字。

### 18.3 授权过程的跨页面投影

授权卡片与扩展页必须投影同一个 Backend Flow，不得维护两套前端状态：

| Backend 状态 | 对话授权卡片 | 连接器状态弹窗 |
|---|---|---|
| `awaiting_user` | 固定可见二维码和当前步骤 | “等待完成第 N/M 步” |
| `verifying` | “正在验证” | “正在验证新授权” |
| `completed` | 完成摘要 | “已连接”，更新最近验证时间 |
| attempt 过期 | 原卡退役，新二维码保持可见 | “链接已更新，请使用新链接” |
| `failed` 可恢复 | 结构化下一步 | “需要处理”，提供语义恢复动作 |
| `superseded/cancelled` | 旧卡失效 | 只显示当前有效 Flow |

前端实体键使用 `(connector_id, profile_id, flow_id, phase_id, attempt)`。扩展页刷新后从 Backend 恢复真实状态，不能依赖当前对话内的 ToolMessage 才知道连接器正在授权。弹窗关闭再打开时必须显示 Backend 当前投影，而不是恢复旧的组件局部状态。

### 18.4 技能目录优化

技能页借鉴参考图的目录式卡片，但不把本地技能管理能力降级成纯商店：

- 页头包含搜索、“已安装”和“导入技能”；暂不展示没有真实数据源的推荐市场。
- 默认使用响应式卡片网格，展示图标、名称、简短说明、来源与启用状态。
- 点击已安装技能进入现有文件树、Markdown 预览和编辑器；目录与编辑器是同一页面的两种状态。
- 未安装条目只有在 Skill Manager 提供真实目录数据时才显示“安装”，不得使用静态占位卡片。
- 一次导入多个 Skills 时，目录层面显示一个批次进度/结果，不在对话或扩展页制造 N 张授权卡。
- 飞书 Skills 与飞书连接器建立可导航关联：连接器状态弹窗可展示“提供 24 个已安装技能”，点击跳转到过滤后的技能目录；连接器健康不由 Skills 数量推断。

### 18.5 页面共同视觉规则

- 保留当前工作台侧栏，扩展内容作为主工作区，不额外复制一套全局导航。
- 顶部分段、搜索框和主要操作保持单行；窄屏时搜索下移，操作不挤压标题。
- 内容宽度、卡片圆角、边框、悬停和状态颜色复用现有 design tokens，参考图只作为信息密度和层级参考，不逐像素复制其品牌样式。
- 空状态只说明真实缺失项并给出一个下一步，不展示假数据填满页面。
- 目录卡负责发现和状态概览，状态弹窗负责环境、身份、授权、诊断和危险操作；不得把 revoke 按钮直接放在目录卡上。

## 19. 连接器 API 与只读投影

为了让扩展页不解析 CLI 输出，新增产品级 API：

```text
GET  /api/connectors
GET  /api/connectors/{connector_id}
POST /api/connectors/{connector_id}/authorize
POST /api/connectors/{connector_id}/resume
POST /api/connectors/{connector_id}/revoke
```

列表/详情响应至少包含：

```json
{
  "connector_id": "lark",
  "display_name": "飞书",
  "driver_kind": "managed_cli",
  "environment": {
    "health": "available",
    "runtime": "node",
    "executable": "lark-cli",
    "version": "1.0.78",
    "availability_scope": "all_projects",
    "last_checked_at": 1785200000
  },
  "profile": {
    "id": "lark_default",
    "label": "默认飞书连接",
    "health": "ready",
    "app_identity": "ready",
    "user_identity": "ready",
    "last_verified_at": 1785200000
  },
  "active_flow": null,
  "capabilities": ["消息", "文档", "日历", "多维表格"]
}
```

这里的 `driver_kind` 仅用于技术信息和诊断，不能驱动用户侧页面分组。前端也不能根据 `managed_cli`、`mcp` 自行推断授权步骤；步骤完全来自 Adapter 生成、Backend 状态机持久化的 Flow 投影。

## 20. 扩展页验收标准与落地顺序

### 20.1 验收标准

- 扩展页顶部为“技能 / 连接器”，不再把 MCP 当成唯一连接器类型。
- 当前只出现真实可用的飞书连接器。
- CLI 已安装但 Profile 未授权时，飞书不能显示绿色已连接。
- 状态弹窗同时展示共享运行环境、Bot 配置和 User OAuth，且三者不能混成一个状态。
- Bot 配置和 User OAuth 状态分开展示，用户能理解为什么完整配置有两步。
- 从对话或扩展页发起授权，会投影到同一个 Flow；刷新页面状态不丢失。
- 主动重新授权直接覆盖，等待与失败期间旧 Profile 不被删除。
- 链接过期后卡片和详情页同时切换到新 attempt。
- 飞书当前实现明确标记为“托管 CLI”，不得误标为 MCP。
- 凭证页不泄露 Secret State、容器路径或可复用认证材料。

### 20.2 落地顺序

1. 先完成第 3～15 节的授权语义 API、事务状态机与结构化投影。
2. 建立 `ConnectorDefinition/ConnectorAdapter` 注册表，注册 Lark。
3. 提供连接器列表、详情、授权、续跑和 revoke API。
4. 将现有扩展页顶部分段改为“技能 / 连接器”。
5. 实现飞书目录卡、状态弹窗与语义操作入口。
6. 让对话授权卡片和连接器状态弹窗共用 Flow projection store。
7. 补前端状态映射、刷新恢复、过期 attempt 和 destructive revoke 测试。
