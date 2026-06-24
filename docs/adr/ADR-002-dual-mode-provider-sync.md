# ADR-002：直连 / Higress 双模式与 Provider 配置同步

| 字段 | 内容 |
|---|---|
| 编号 | ADR-002 |
| 标题 | 建立 Provider 直连与 Higress Gateway 双模式，并单向同步 Provider 配置 |
| 状态 | **Proposed** |
| 日期 | 2026-06-24 |
| 相关模块 | `backend/config.py`, `backend/llm/*`, `backend/capabilities.py`, `frontend/src/app/settings/page.tsx`, Higress Console |

## 1. 问题

PuddingClaw 必须同时支持两种运行方式：

```text
直连模式：    PuddingClaw ──→ DeepSeek / OpenAI / DashScope
Gateway 模式：PuddingClaw ──→ Higress ──→ DeepSeek / OpenAI / DashScope
```

Higress 只提供统一 Base URL、模型路由、切换、限流、Token 统计和审计，不提供模型。真实模型能力和访问凭证始终来自 Provider。

如果 Provider、Base URL 和 API Key 分别在 PuddingClaw 与 Higress Console 配置，会产生重复录入、密钥轮换遗漏和故障回退不可用等问题。

但 Higress 自带 Console，**无法强制阻止用户直接在 Console 里修改 Provider 或 Key**。因此同步机制必须假设「用户可能绕过 PuddingClaw 改配置」，并通过漂移检测与受管资源标记兜底。

## 2. 决策

### 2.1 双模式

设置页提供明确的接入模式，而不是把 Higress 作为强制依赖：

- `direct`：直接使用本地 Provider Profile 调用模型。
- `gateway`：调用 Higress Gateway，由 Higress 根据模型别名路由到 Provider。

Gateway 模式可配置 `fallback_to_direct`。开启后，Higress 在首个流式 token 前不可用时，使用同一个本地 Provider Profile 直连；关闭后，网关失败直接报错。

### 2.2 Provider Profile 是唯一录入入口

PuddingClaw 中的 Provider Profile 是 Provider 凭证的事实源：

```text
ProviderProfile
├─ id
├─ provider                 # deepseek / openai / dashscope / custom
├─ display_name
├─ direct_base_url
├─ api_key_secret_ref       # 只保存密钥引用
├─ models[]
│  ├─ provider_model        # Provider 真实模型名
│  └─ gateway_alias         # 发送给 Higress 的模型别名
├─ key_version              # 密钥版本，不是密钥内容
├─ updated_at
└─ higress_sync
   ├─ managed_resource_id
   ├─ last_synced_revision
   ├─ last_synced_at
   └─ status                # synced / pending / failed / drifted
```

用户只在 PuddingClaw 中录入一次 Provider、Base URL 和 API Key。该配置同时用于：

1. `direct` 模式调用。
2. Gateway 故障时的直连回退。
3. 向 Higress 创建或更新**受管 Provider**。

PuddingClaw 同步到 Higress 的资源必须带有稳定标签：

```yaml
managed-by: puddingclaw
provider-profile-id: <uuid>
```

这些标签用于：

- Console 中标识「这是 PuddingClaw 在管理的资源」。
- 漂移检测时识别哪些 Provider 需要对比。
- 删除时避免误删用户手动创建的非受管 Provider。

### 2.3 同步方向

同步采用 **PuddingClaw → Higress 单向同步**：

```text
用户保存 Provider Profile
       │
       ├─ 本地安全保存 Provider Key
       ├─ 直连配置立即可用
       └─ 幂等写入 Higress 受管 Provider
              └─ 路由继续由 Higress Console 管理
```

不从 Higress 反向同步密钥，因为网关通常只能返回掩码，无法可靠恢复原始凭证。Higress 中由 PuddingClaw 创建的资源必须带有 `managed-by=puddingclaw` 和稳定的 Provider Profile ID。

## 3. 同步规则

### 3.1 创建与更新

1. 校验 Provider Base URL、模型名和 Key。
2. 将 Key 写入本地 Secret Store，普通配置只保存 `secret_ref`。
3. 生成单调递增的 `key_version` / profile revision。
4. 使用稳定资源 ID 幂等 upsert 到 Higress。
5. Higress 成功后标记 `synced`；失败标记 `failed`，保留本地直连能力。

不得在 Higress 同步失败时显示“全部保存成功”。UI 应分别显示“本地已保存”和“网关同步失败”。

### 3.2 切换模式

#### direct → gateway

只有满足以下条件才能启用 Gateway 模式：

- Gateway 健康检查成功。
- 当前 Provider Profile 同步状态为 `synced`。
- 当前模型存在 `gateway_alias`，且路由探测成功。

任一条件失败时保持 `direct`，不得留下“界面显示 Gateway、运行时仍直连”的隐式状态。

#### gateway → direct

直接切换到本地 Provider Profile，不删除 Higress 配置，也不删除 Provider Key。下次启用 Gateway 时只做 revision 对比和增量同步。

### 3.3 Key 轮换

用户在 PuddingClaw 中输入一次新 Key：

1. 验证新 Key 可访问 Provider。
2. 更新本地 Secret Store 和 `key_version`。
3. 同步到 Higress。
4. 用 Gateway 发起最小探测请求。
5. 成功后结束轮换。

若第 3/4 步失败，本地直连使用新 Key，Gateway 标记 `failed` 并禁止自动切入，避免两端凭证版本不一致。

### 3.4 漂移处理

Higress Console 负责路由、限流、Token 统计等网关策略，**不建议直接修改由 PuddingClaw 管理的 Provider endpoint 或凭证**。但无法强制阻止用户这样做，因此必须显式处理漂移。

#### 3.4.1 检测方式

PuddingClaw 在以下时机拉取 Higress 中对应受管 Provider 的摘要：

- 设置页打开时。
- 用户点击「立即同步」后。
- 定时后台任务（可选，默认 5 分钟）。

对比字段：

| 字段 | 来源 | 说明 |
|---|---|---|
| `provider` | Higress Provider type | 如 deepseek / openai |
| `base_url` | Higress Provider endpoint | 非敏感，可对比 |
| `models[]` | Higress 路由摘要 | 模型别名映射 |
| `key_fingerprint` | Higress 掩码 key 或 Higress 返回的 fingerprint | 若 Higress 不提供 fingerprint，则无法可靠判断 key 是否变化 |

#### 3.4.2 状态定义

- `synced`：Higress 端与本地 Profile revision 一致。
- `drifted`：Higress 端非敏感字段被修改，或检测到 key 可能变化。
- `failed`：上次同步请求失败，无法确认 Higress 状态。
- `pending_delete`：用户已删除 Profile，但 Higress 端删除失败或尚未执行。

#### 3.4.3 处理策略

| 漂移类型 | UI 提示 | 默认动作 | 用户可覆盖 |
|---|---|---|---|
| 非敏感字段不一致（base_url、模型别名） | 「Higress Console 已修改该 Provider，点击覆盖」 | 用 PuddingClaw 配置重新同步 | 可选择导入 Higress 的非敏感字段到本地 Profile |
| key 疑似变化 | 「Higress 端密钥与本地不一致，请在 PuddingClaw 重新输入 Key」 | 禁止自动切换为 gateway 模式，直到用户重新输入并同步 | 必须重新输入 |
| 受管 Provider 在 Higress 中被删除 | 「Higress 端该 Provider 已不存在，将重新创建」 | 重新 upsert | 可转为非受管 Provider 或删除本地 Profile |

#### 3.4.4 安全约束

- 不从 Higress 反向同步原始 Key。
- key 比较只能基于 Higress 提供的 fingerprint 或掩码后缀；无法获取时，key 变化标记为 `drifted` 并要求用户重新输入。
- 漂移检测失败（Higress 不可达）不影响本地 direct 模式运行。

### 3.5 非受管 Provider

如果用户坚持在 Higress Console 中管理某些 Provider，PuddingClaw 允许将其作为**非受管 Provider**使用：

- PuddingClaw 本地不保存该 Provider 的 key。
- Gateway 模式下，模型别名可以指向 Higress 中已存在的非受管 Provider。
- `fallback_to_direct` 对这些模型不生效（本地没有 key）。
- 非受管 Provider 不显示同步状态，也不参与漂移检测。

非受管 Provider 适合「团队已经在 Higress 里统一管 key」的场景，但会失去 PuddingClaw 的 fallback 能力。

### 3.6 删除

- 默认只停用 Provider Profile，不立即删除 Higress 资源和本地 Secret。
- 永久删除需二次确认：先删除 Higress 中相同 managed resource ID，再删除本地 Secret。
- Higress 不可达时记录 `pending_delete`，不得静默遗留未知状态。

## 4. 职责边界

| 能力 | PuddingClaw | Higress Console |
|---|---|---|
| Provider/Profile 录入 | 主入口 | 查看受管资源，可管理非受管资源 |
| Provider Key | 本地 Secret Store，单向同步 | 保存上游调用所需副本 |
| 直连与 fallback | 负责 | 不负责 |
| 模型别名 | 保存映射 | 执行路由 |
| 路由、权重、切换 | 展示摘要/跳转 | 事实源 |
| 限流、Token 统计、审计 | 展示摘要或链接 | 事实源 |
| Gateway Console URL | 保存并提供“打开控制台” | 提供管理界面 |

## 5. 设置页规则

### 5.1 默认模式

| 部署方式 | 默认模式 | 说明 |
|---|---|---|
| `docker compose up -d`（core） | `direct` | 不依赖 Higress，单 Provider key 即可运行 |
| `docker compose --profile full up -d` | `gateway` | Higress 已启动，默认走网关 |
| 本地开发 | `direct` | 最小启动成本 |

Full 模式默认 gateway，但 `fallback_to_direct` 默认开启，避免网关抖动时聊天直接失败。用户可在设置页切换为 direct。

### 5.2 模式选择

设置页顶部提供模式选择：

```text
[ Provider 直连 ] [ Higress Gateway ]
```

共同区域始终展示 Provider Profiles。Gateway 模式额外展示：

- Gateway API URL。
- Higress Console URL 与“打开管理后台”。
- 健康状态。
- Provider 同步状态和“立即同步”。
- 模型别名 / 路由摘要。
- `fallback_to_direct`。
- 漂移提示（如有）。

当 `fallback_to_direct=true` 时，本地 Provider Profile 和 Key 为必填。关闭 fallback 时也保留已录入凭证，但网关请求本身不传递 Provider Key，由 Higress 使用同步后的上游凭证。

## 6. 安全约束

- API Key 不通过设置读取 API 返回明文。
- 日志、异常、同步响应中禁止输出 Key。
- Higress 同步请求只在服务端执行，浏览器不得接触 Higress 管理凭证。
- `config.json` 中的明文 Key 迁移到 Secret Store 后，需提供一次性迁移与清理流程。
- 同步状态使用 key version / fingerprint 判断，不使用原始 Key 比较。
- 漂移检测只能导入 Higress 的非敏感字段；任何 key 变化都要求用户在 PuddingClaw 重新输入。

## 7. 实施顺序

- [ ] 定义 `ProviderProfile` 与 Secret Store。
- [ ] 将现有单个 `llm` / `embedding` 配置迁移为 Profile。
- [ ] 增加 Higress 管理适配器与幂等 upsert，资源带 `managed-by=puddingclaw` 标签。
- [ ] 增加同步状态、漂移检测（非敏感字段对比 + key fingerprint）和 Key 轮换状态机。
- [ ] 支持非受管 Provider（仅 Gateway 使用，无 fallback）。
- [ ] 设置页增加双模式、默认模式根据部署方式选择、Console 跳转及同步状态。
- [ ] 覆盖 direct、gateway、fallback、同步失败、漂移检测和 Key 轮换测试。

在 Higress 管理适配器完成前，Gateway 模式保持实验性，不宣称“Provider 配置已自动同步”。
