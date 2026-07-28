# ADR-005：Backend 托管的外部授权状态机与自然语言续跑

| 字段 | 内容 |
|------|------|
| 编号 | ADR-005 |
| 标题 | 建立 Backend 托管的多阶段外部授权状态机，使用 Provider Adapter 适配具体协议 |
| 状态 | **Accepted** |
| 日期 | 2026-07-28 |
| 作者 | PuddingClaw Team |
| 相关决策 | [ADR-004：用户级 Toolchain、Credential Profile 与沙箱容器生命周期](ADR-004-user-runtime-toolchain-and-credential-profiles.md) |
| 相关模块 | `backend/runtime_identity/*`, `backend/harness/tool_execution.py`, `frontend/src/components/chat/*` |

---

## 1. 结论

PuddingClaw 使用一个 **Backend 托管、支持任意阶段数的外部授权状态机**，统一管理 CLI、OAuth、MCP 或其他第三方连接器的交互式授权生命周期。

- Backend 管理流程状态、阶段顺序、暂停/恢复、过期、并发、Profile 锁、Secret、验证、事务提交和 UI 事件。
- Provider/CLI Adapter 声明具体服务的阶段、命令、输出解析、身份验证和凭证状态契约。
- Agent 负责理解用户意图、选择身份与授权范围，并用自然语言解释当前阶段；Agent 不拼接续跑凭证、不手抄 URL、不编排底层授权命令。
- Frontend 使用通用消息级授权卡展示 `第 N/M 步`、当前动作、二维码、链接和完成后的自然语言提示；不增加“我已完成”等流程按钮。
- 用户用“好了”“完成了”“已经授权”等自然语言继续对话。该反馈只触发当前阶段的恢复与验证，不能直接证明授权成功。
- 遇到 `awaiting_user` 后必须结束当前 Agent 轮次。前一阶段未验证成功时，Backend 不允许启动下一阶段。

飞书是首个落地的复合流程 Adapter：

```text
第 1/2 步：创建或绑定飞书应用
    config init --new
    产生 App ID + App Secret，使 Bot 身份可验证

第 2/2 步：授权应用访问用户数据
    auth login --domain ...
    产生用户 OAuth token，使 User 身份可验证
```

这两个步骤授权的对象不同，不是同一权限重复确认。

## 2. 第一性原理与不变量

### 2.1 用户反馈不是完成证据

用户说“好了”只表示“请检查刚才的外部操作”。流程只能在 Provider Adapter 的远端/本地验证成功后推进：

```text
自然语言反馈
    -> 恢复当前 flow/phase
    -> 收集 Provider 结果
    -> verify
    -> 成功才进入下一阶段
```

进程退出码为 0、二维码已经显示、链接已经打开、后台 Job 已启动，都不能替代 Provider 验证。

### 2.2 阶段顺序必须由 Backend 强制

Skill 或 Prompt 可以解释顺序，但不能成为唯一控制面。模型可能漏读、误解、猜参数或连续调用后续命令，因此必须满足：

- 每个阶段都有 Backend 可校验的前置条件。
- `awaiting_user` 状态下禁止启动依赖它的后续阶段。
- 未知或非法状态迁移 fail closed。
- 同一 Profile 同一时间最多存在一个可写授权流程。
- 重启后由持久化 flow lease 恢复，不能依赖模型上下文重建状态。

### 2.3 Agent 不持有续跑秘密

`device_code`、PKCE verifier、临时 registration token、App Secret 等材料只保存在 Backend 的受保护 flow state 或 Credential Profile staging state 中：

- 不进入模型上下文。
- 不进入前端 Session timeline。
- 不由 Agent 从上一轮工具输出复制到下一条命令。
- 不写入普通 Project/Session 容器。
- UI 只收到完成用户操作所需的公开 URL、二维码 payload、user code 和过期时间。

### 2.4 授权流程与 Credential Profile 是不同对象

Credential Profile 是长期第三方身份连接；Authorization Flow 是创建、修复或更新 Profile 的短期事务：

```text
Credential Profile
    长期、可复用、加密持久化

Authorization Flow
    短期、可过期、可取消、可恢复
    成功后原子提交到 Profile
```

取消或过期的 Flow 不应破坏仍可使用的旧 Profile。

显式重新授权是一个新的写事务，不是“检查旧 Token 是否仍有效”：

- `auth resume` 必须绑定一个活动的 Flow；不存在活动 Flow 时不得用旧 Profile 返回成功。
- User 重新授权从旧 Profile 的隔离副本开始，并先清除副本中的旧 User 登录态；旧 Vault 只作为失败回滚，不得成为本次成功证据。
- 只有本次 continuation 产生新 User 登录态、独立验证通过并原子提交后，才能返回 `authorization_completed: true`。

授权事务必须使用两个彼此隔离的加密槽：

```text
baseline (.state.enc)
    已验证的上一阶段结果；Step 2 未验证前不可覆盖

candidate (.candidate.state.enc)
    collect/runner 导出的未验证结果；可跨进程崩溃恢复
```

Candidate 只有在独立 `verify` 确认目标身份完整有效后，才能晋升为 staged commit state。验证超时、429、5xx 或未知错误不是授权失败证据，必须保留 baseline、candidate 与 continuation，稍后只重试验证，不能再次消费一次性 device code。

Candidate 与 continuation 的文件名和加密 AAD 都必须绑定 `flow + phase + attempt`（candidate 至少绑定 `flow + attempt`）。新 attempt 即使在 registry 提交前后崩溃，也不能读取上一 attempt 的候选状态，旧页面也不能配上新 device code。若 candidate 来源于 pending/瞬态 continuation，Backend 还必须持久化一个严格枚举的 origin disposition，避免后续独立验证丢失原始 pending 语义。

### 2.5 活动 Flow 独占 Profile 的 Provider 执行权

同一 Profile 存在非终态 Authorization Flow 时，该 Flow 不只拥有“写锁”，还独占所有可能导入、导出或重写 Provider 原生凭证状态的命令执行权。不能根据命令名把 `config show`、`auth status` 等操作假定为纯读取：第三方 CLI 可能在读取时迁移配置、刷新 token、重写 keychain 引用，或导出字节级不同的归档。

因此必须满足：

- 普通 Provider 命令在活动 Flow 期间不进入 CLI Runner，只返回当前授权阶段的结构化投影。
- `config show`、`auth status` 不能用于判断 staged 阶段是否完成；它们读取的是 durable Profile，不是候选状态。
- 阶段推进只允许通过 Adapter 声明的 Backend-owned entry/resume 动作，由 Backend 在 Profile 锁内收集并验证 staging。
- Step 1 验证成功但 Step 2 尚未完成时，结果只保存在加密 staging；不得为了让 `config show` 可见而提前提交 durable Vault。
- Step 2 失败、过期或可重试错误只重置当前 User attempt，保留已验证的 Step 1 staging，后续从 Step 2 重试。
- 唯一的管理面逃生口是用户已明确批准的 Profile 撤销/删除；Backend 必须先终止 Browser Runner、取消 Flow，再执行绑定到冻结 argv 的破坏性操作。

`stale_base_state` 是并发覆盖保护，不能通过继承未知 Flow 的 staged 状态或无条件放宽 CAS 来“修复”。正确做法是杜绝当前 Flow 自己改写 durable baseline；真正由其他事务造成的基线变化仍然 fail closed。

## 3. 为什么不能只依赖 Skill

Provider Skill 仍然重要，但它只负责业务语义：

- Bot 与 User 身份的区别。
- 哪些业务操作应该使用哪种身份。
- 需要哪些 domain/scope。
- 权限不足时如何向用户解释。
- Provider 特有的注意事项。

以下能力不能依赖 Skill：

- 是否允许进入下一阶段。
- 当前等待哪个用户操作。
- 续跑 token 保存在哪里。
- URL/二维码如何投影到前端。
- Profile 如何原子修复。
- 进程重启后如何恢复。
- 同一 Profile 的并发授权如何互斥。

原因是 Skill 会更新，也可能被不同 Agent 以不同方式理解。产品级生命周期必须由 Backend 的类型、状态和测试保证。

## 4. 通用领域模型

### 4.1 Profile 状态

Profile 使用稳定的长期状态：

```text
unconfigured     尚未建立凭证
active           身份验证通过，可执行 Provider 操作
repair_required  凭证状态存在但不完整或无法读取
expired          凭证过期，可能需要刷新或重新授权
revoked          本地或服务端授权已撤销
```

浏览器等待状态不应继续混入 Profile 长期状态；它属于 Authorization Flow。

一个 Profile 可以同时包含生命周期不同的身份评估：

```text
Bot/App    ready | repair_required | unavailable
User OAuth active | authorization_required | expired | revoked
```

User Flow 等待或失败时，已验证的 Bot 身份保持可用；Bot 权限或 AppSecret 问题也不得错误触发 User OAuth。

### 4.2 Authorization Flow 状态

```text
created
starting
awaiting_user
collecting
verifying
completed
failed
expired
cancelled
```

合法主路径：

```text
created
  -> starting
  -> awaiting_user
  -> collecting
  -> verifying
  -> 下一阶段 starting，或 completed
```

失败、过期和取消可以从任意非终态进入，但必须保留可审计原因。

### 4.3 阶段定义

Adapter 为每个授权目标声明一个或多个阶段：

```python
@dataclass(frozen=True)
class AuthorizationPhaseSpec:
    phase_id: str
    title: str
    description: str
    prerequisites: tuple[str, ...]
    interaction: Literal[
        "browser_url",
        "device_code",
        "redirect_callback",
        "secret_input",
        "none",
    ]
```

阶段数由 Adapter 决定，通用层不得假设只有一或两步。

### 4.4 Flow 记录

Backend 持久化非敏感元数据，并将敏感 continuation state 加密保存：

```json
{
  "flow_id": "auth_flow_xxx",
  "owner_user_id": "local",
  "provider": "lark",
  "adapter_id": "lark-cli",
  "profile_id": "lark_default",
  "purpose": "repair",
  "status": "awaiting_user",
  "phase_id": "user_consent",
  "phase_index": 2,
  "phase_count": 2,
  "profile_revision": 123,
  "adapter_contract_fingerprint": "sha256:...",
  "expires_at": 0,
  "created_at": 0,
  "updated_at": 0
}
```

公开记录不保存 `device_code` 或 Secret。敏感 continuation state 与 Profile vault 使用同等级保护。

## 5. Adapter 契约

Backend 提供通用状态机；每个 Provider/CLI Adapter 实现协议差异：

```python
class ManagedAuthorizationAdapter(Protocol):
    def assess_profile(self, profile: CredentialProfile) -> AuthAssessment: ...
    def phases(self, request: AuthorizationRequest) -> tuple[AuthorizationPhaseSpec, ...]: ...
    def start_phase(self, context: AuthorizationContext) -> AuthorizationPhaseResult: ...
    def collect_phase(self, context: AuthorizationContext) -> AuthorizationPhaseResult: ...
    def verify_phase(self, context: AuthorizationContext) -> PhaseVerification: ...
    def verify_profile(self, profile: CredentialProfile) -> IdentityState: ...
```

Adapter 还负责：

- CredentialStateSpec 与状态目录。
- 精确 argv 或 API 请求构造。
- Provider 输出解析和错误分类。
- 远端身份、租户、App 和 scope 的安全摘要。
- Provider 网络域和 runner 要求。
- 适合用户展示的阶段名称与说明。
- 将 Provider 错误严格分类为 `pending / slow_down / denied / expired / retryable / failed`。只有 Provider 明确返回 denied、expired 或 invalid 才能终结当前 attempt；未知非零、超时、429 和 5xx 必须保持可恢复，不能伪装成“链接过期”。
- 识别普通 Provider 操作中已被证明的 User Token 失效，并启动只包含 `user_consent` 的修复 Flow；scope 不足和 Bot 权限不足不属于 Token 修复。

通用 OAuth Device Flow、Authorization Code/PKCE 和 API Key 可以提供基础 Adapter；飞书这种“应用注册 + 用户 OAuth”的复合流程只实现少量 Provider 特化。

## 6. Agent 契约与自然语言续跑

### 6.1 首次发起

Agent 表达授权目标，不直接编排 CLI：

```text
确保 lark_default 已具备 Bot 和 User 身份，并为 User 请求 all domain。
```

Backend：

1. 解析 Profile。
2. 调用 Adapter assess。
3. 创建或复用当前 Authorization Flow。
4. 启动第一个未满足的阶段。
5. 返回结构化 `managed_authorization_request`。
6. Harness 结束当前 Agent 轮次。

### 6.2 用户自然语言反馈

当 Session 存在 pending flow 时，Backend 将以下非敏感上下文注入下一轮：

```json
{
  "pending_authorization": {
    "flow_id": "auth_flow_xxx",
    "provider": "lark",
    "phase_id": "app_configuration",
    "step": 1,
    "total": 2,
    "status": "awaiting_user"
  }
}
```

用户可以回复：

```text
好了
已经完成
我扫完了
授权成功了
```

Agent 将明确的完成反馈映射为语义动作 `resume_managed_authorization(flow_id)`。Agent 不提供 device code 或下一条 CLI argv。

如果用户在 pending flow 期间提出无关问题，不自动推进。自然语言反馈只是 resume 意图，Backend 仍必须 collect + verify；验证失败时继续停留在当前阶段或生成新 Flow，不能根据用户措辞强行推进。

### 6.3 阶段间自动衔接

用户已经请求完成整个授权流程，因此：

- 第 1 阶段验证成功后，可以自动启动第 2 阶段。
- 不需要额外询问“是否继续下一步”。
- 第 2 阶段一旦进入 `awaiting_user`，必须再次结束当前 Agent 轮次。
- 最终 Profile 验证成功后，才能宣布整体完成。

## 7. 结构化 UI 协议

Backend 不再要求模型从 CLI 文本中手抄 URL。公开事件示例：

```json
{
  "type": "managed_authorization_request",
  "flow_id": "auth_flow_xxx",
  "provider": "lark",
  "profile_id": "lark_default",
  "status": "awaiting_user",
  "phase": {
    "id": "user_consent",
    "step": 2,
    "total": 2,
    "title": "授权应用访问你的飞书数据",
    "description": "允许“小布丁”以你的身份访问已申请的飞书资源"
  },
  "verification_url": "https://accounts.feishu.cn/...",
  "user_code": "ABCD-EFGH",
  "qr_payload": "https://accounts.feishu.cn/...",
  "expires_at": "2026-07-28T12:00:00+08:00",
  "completion_hint": "完成后告诉我，我会验证结果并继续"
}
```

约束：

- URL 是经过 Adapter 校验的 opaque string，Frontend 不从大段日志正则提取。
- Backend 或 Frontend 使用可信 QR 库根据 `qr_payload` 生成等比例二维码；不要求 Agent 再调用 Provider CLI 生成图片。
- `qr_payload` 默认等于公开 verification URL，不包含 Secret。
- `device_code`、PKCE verifier 等 continuation material 不在事件中。
- 事件带明确 phase 和 step/total，避免两个阶段都显示“飞书授权配置”。

## 8. 前端交互

### 8.1 授权卡是消息级一等组件

授权卡与 Skill Plan Card 一样渲染在消息层，不能嵌套在会随 streaming 结束自动折叠的 ThoughtChain 内。

Pending 卡片必须保持可见：

```text
第 1/2 步 · 创建或绑定飞书应用
选择或创建 CLI Bot，并安全保存应用凭证。

[二维码]
[授权链接]

完成后告诉我，我会验证结果并进入下一步。
```

第二阶段：

```text
应用配置已验证：小布丁

第 2/2 步 · 授权应用访问你的飞书数据
这是用户身份授权，与上一步的应用配置不同。

[二维码]
[授权链接]

完成后告诉我，我会验证最终状态。
```

### 8.2 不增加流程按钮

本决策明确不增加“我已完成”“继续”等强制按钮：

- PuddingClaw 保持自然语言 Agent 交互。
- Pending flow 上下文让“好了”等回复具有明确指向。
- Backend 验证而不是按钮点击决定是否完成。
- 未来可以提供“取消授权流程”等辅助操作，但不能成为正常续跑的必经路径。

### 8.3 完成后的投影

阶段完成后，历史卡片变为紧凑摘要：

```text
✓ 第 1/2 步 · 飞书应用已配置：小布丁
✓ 第 2/2 步 · 用户授权已完成：钟智杰
```

不得把过期二维码长期固定在页面底部，也不得把仍等待用户的卡片自动折叠。

## 9. 飞书 Adapter 的最终流程

### 9.1 预检

权威检查是：

```text
lark-cli auth status --json --verify
```

`config show` 只用于诊断配置摘要；其中 `appSecret: "****"` 可能只是引用存在，不能证明底层 Secret 可读取。

Adapter 将检查结果归一化为：

```text
bot.ready && bot.verified
user.ready && user.verified && tokenStatus == valid
```

### 9.2 第 1 阶段：应用配置

前置条件：

```text
bot 未配置，或 Profile == repair_required
```

Adapter 启动受管 `config init --new`，BrowserAuth Runner 捕获公开 URL/二维码材料并返回 `awaiting_user`。用户自然语言反馈后，Backend 收集后台 Job 并验证 Bot/App 状态。

验证成功后显示应用名称，再自动进入第 2 阶段。

### 9.3 第 2 阶段：用户授权

前置条件：

```text
bot.ready && bot.verified
```

Adapter 使用 `auth login --domain ... --no-wait --json` 发起 Device Flow，把 device code 保存在 Backend flow state，只把公开 verification URL 投影给前端。

用户自然语言反馈后，Backend 使用保存的 continuation state 完成轮询，然后执行最终 `auth status --json --verify`。

### 9.4 完成条件

如果本次目标需要 Bot 和 User，则只有以下条件全部满足才能完成：

```text
bot.status == ready
bot.verified == true
user.status == ready
user.verified == true
user.tokenStatus == valid
请求的 domain/scope 已满足
```

## 10. Profile 修复与重配

### 10.1 不通过失败请求发现损坏

Adapter 的 `assess_profile` 应直接识别：

- config 引用存在但 Secret State 缺失。
- archive contract/fingerprint 不匹配。
- token 无法刷新。
- Bot/User 验证失败。

不得先执行一次注定失败的 `auth login`，再根据 `missing client_secret` 才进入修复状态。

### 10.2 事务式替换

修复流程使用 baseline/candidate 双槽 staging state：

```text
保留旧 vault
  -> 创建 staged Authorization Flow 与已验证 baseline
  -> 完成所有必要阶段
  -> 将 runner 输出原子写入 candidate
  -> 独立 verify candidate
  -> 验证成功后晋升为 commit state
  -> 原子替换 Profile vault
  -> 删除 flow continuation、baseline 与 candidate
```

用户明确拒绝或当前 attempt 明确无效时，只清除该 attempt 的 continuation/candidate，保留上一阶段 baseline；只有完整 Flow 被显式取消或替换时才清理全部 staging state。任何情况下都不先执行裸 `config remove`。

只有用户明确要求删除或撤销 Profile 时，才走 destructive approval；“重新授权”不等于授权删除旧 Profile。

## 11. Harness 控制边界

Harness 在收到结构化 `managed_authorization_request(status=awaiting_user)` 后：

1. 允许 Agent 生成一次面向用户的阶段说明。
2. 禁止执行依赖当前阶段完成的后续 Provider 操作。
3. 结束当前 Agent 轮次。
4. 保留 Flow lease 和后台 Job。

Harness 不应阻止生成授权卡所需的工作，因为 URL 和 QR payload 必须在进入暂停边界前由 Backend 一次性生成。不能再采用“Tool 返回 URL -> 模型调用第二个 qrcode 工具 -> Harness 又禁止后续工具”的冲突流程。

## 12. 并发、幂等与恢复

- 同一 owner/provider/profile 只能有一个写入型 Flow。
- 重复发起相同目的时返回现有 pending Flow，而不是创建新二维码使旧链接失效。
- Flow 固定 Profile revision、Toolchain revision 和 Adapter contract fingerprint。
- 用户重复说“好了”时，collect/resume 必须幂等。
- Runner 必须用容器内 timeout 先终止 provider 进程，再从仍在运行的隔离容器导出 credential state，最后才销毁容器；不能 stop/restart 承载凭证 tmpfs 的容器，也不能把“进程超时”等同于“没有授权结果”。
- 已存在 candidate 时，resume 只验证 candidate，不重复兑换或消费 device code。
- 已完成阶段不能因重放消息再次执行。
- 链接过期时明确显示“本阶段链接已过期”，创建新阶段 attempt；不伪装成普通命令失败。
- Backend 重启后从 Flow registry 和 runner label 恢复；状态契约不一致时 fail closed。
- Session 可以显示 Flow，但凭证所有权仍属于用户级 Profile；跨 Session 继续同一 Flow 必须经过 owner/profile 校验。

## 13. 当前实现问题与迁移要求

当前实现需要消除以下行为：

1. `LarkAuthorizationCard` 位于 ThoughtChain 折叠区，streaming 结束后二维码必然被收起。
2. 前端只识别 `open.feishu.cn/page/cli`，不识别 `accounts.feishu.cn/.../device/verify`。
3. URL 和二维码依赖解析 CLI 大段文本，模型可能复制出多余引号或 JSON 结尾。
4. Skill 要求调用 `auth qrcode`，Harness 又在 `awaiting_user` 后禁止后续工具，产生永久 running 占位。
5. Agent 可以在 App 未 verified 时过早调用 `auth login`。
6. Agent 可以猜测 `config show --json`、`--device-code ... --json` 等不受支持形式。
7. 损坏 Profile 的恢复曾依赖失败请求和裸 `config remove`。
8. Device code 暴露在模型可见 timeline 中，并由模型跨轮复制。
9. Step 2 失败曾清除 Step 1 staging，使用户被迫重复两层授权。
10. Provider runner 超时曾在导出 credential state 前销毁容器，浪费已经完成的用户授权。
11. 原始 Provider stderr、任意 provider code 或基于原文的哈希不得进入持久化诊断；诊断只允许枚举 reason、数值状态码、exit code 和布尔状态。

迁移后，旧的文本解析逻辑只用于历史 Session 兼容，新 Flow 必须全部走结构化协议。

## 14. 实施顺序

### 阶段 A：领域模型与 Lark Adapter

1. 新增 Authorization Flow registry、状态枚举和阶段定义。
2. 为 Lark Adapter 实现 assess/start/collect/verify。
3. 将 device code 和 BrowserAuth continuation state 移入 Backend。
4. 实现 Profile staging 和原子 repair。

### 阶段 B：Harness 与 Agent

1. 注入 pending flow 非敏感上下文。
2. 提供语义 resume 操作，不让 Agent 构造 device-code argv。
3. 在每个 `awaiting_user` 边界可靠结束 Agent 轮次。
4. 删除 Skill/Tool Guide 中与 Backend 状态机冲突的命令级续跑要求；保留业务语义说明。

### 阶段 C：Frontend

1. 新增消息级 `ManagedAuthorizationCard`。
2. 支持任意 `step/total` 和 Provider 文案。
3. 使用结构化 URL/QR payload，不再从日志正则提取。
4. Pending 始终可见，completed 投影为紧凑摘要。
5. 保留自然语言输入，不增加正常流程按钮。

### 阶段 D：兼容与清理

1. 兼容读取历史 Lark 文本授权输出。
2. 清理悬空 running qrcode tool 的历史投影。
3. 增加过期、重试、重启恢复和并发测试。
4. 完成真实飞书两阶段端到端验收。

## 15. 验收标准

### 顺序与暂停

- App 未 verified 时，Backend 拒绝启动 User OAuth 阶段。
- 每次显示二维码后，本 Agent 轮次结束。
- 用户自然语言回复后先验证当前阶段，再进入下一阶段。
- 第一阶段成功后可自动发起第二阶段，但第二阶段二维码出现后必须再次暂停。

### 前端

- 授权卡在 ThoughtChain 折叠时仍可见。
- 清楚显示 `第 N/M 步`、授权对象和当前目的。
- config init 与 auth login 两种 URL 都能展示等比例二维码和可点击链接。
- Pending 卡不自动折叠；完成后显示阶段摘要。
- 正常流程不依赖按钮。

### Backend 与安全

- Agent/session/tool output 中不存在 device code、App Secret、token 或 PKCE verifier。
- 模型不需要调用 `auth qrcode` 或构造 `--device-code` 命令。
- Profile repair 中途失败不会删除旧 vault。
- 重复 resume 幂等，同一 Profile 不产生并发写入 Flow。
- Backend 重启后能恢复 pending Flow，契约不匹配时 fail closed。

### 飞书端到端

- 第 1 阶段明确显示“创建或绑定飞书应用”。
- 第 2 阶段明确显示“授权应用访问你的飞书数据”。
- 最终同时验证 Bot 名称、用户名称、token 状态和请求 scope/domain。
- 完整成功路径不包含用于探路的失败命令、裸 `config remove` 或悬空 running 工具。

## 16. 被否决方案

### 只加强 Prompt/Skill

否决。无法强制阶段顺序、保存 continuation secret、处理并发和重启恢复。

### 在 Backend 写死飞书两步

否决。授权阶段数和协议属于 Adapter；通用状态机必须支持 GitHub、Google、Slack、API Key 等不同流程。

### 让 Agent 手工适配每个 CLI

否决。会持续产生参数猜测、URL 抄写、Secret 进入上下文和阶段越界。新增 Provider 应实现 Adapter，而不是只增加 Agent 规则。

### 使用前端按钮作为唯一续跑入口

否决。增加交互重量并破坏自然语言 Agent 体验。用户自然语言触发 resume，Backend 验证结果。

### 在等待状态后再调用二维码工具

否决。暂停边界与二维码生成产生时序冲突。结构化授权事件必须在进入 `awaiting_user` 前一次性携带 UI 所需的公开信息。

### 重配前先删除旧 Profile

否决。外部授权可能取消、超时或失败；必须使用 staging + verify + 原子替换。
