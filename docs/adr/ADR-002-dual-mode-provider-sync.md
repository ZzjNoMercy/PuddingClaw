# ADR-002：直连 / Higress 双模式与 Provider 配置边界

| 字段 | 内容 |
|---|---|
| 编号 | ADR-002 |
| 标题 | PuddingClaw 只保留 fallback Provider，Higress Console 作为 gateway 模式 Provider 事实源 |
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

Higress Console 的 Provider 配置能力比 PuddingClaw 更完善（多 Provider 负载均衡、权重路由、Provider 组、自动重试等）。如果 PuddingClaw 再维护一套「受管 Provider」并单向同步到 Higress，会浪费 Higress 的现有能力，也让高级路由配置必须在两个地方维护。

因此决定：**PuddingClaw 只保留一套能保证 fallback 的 Provider 配置；gateway 模式下的 Provider 路由、权重、多 Provider 策略全部交给 Higress Console 管理。**

## 2. 决策

### 2.1 双模式

设置页提供明确的接入模式：

- `direct`：直接使用本地 Fallback Provider Profile 调用模型。
- `gateway`：调用 Higress Gateway，由 Higress 根据模型名/别名路由到 Provider。

Gateway 模式可配置 `fallback_to_direct`。开启后，Higress 不可用时自动使用本地 Fallback Provider Profile 直连；关闭后，网关失败直接报错。

### 2.2 Provider 配置边界

| 配置位置 | 用途 | 事实源 |
|---|---|---|
| Higress Console | Gateway 模式下的 Provider、路由、权重、限流、密钥 | **Gateway 模式事实源** |
| PuddingClaw Secret Store | Direct 模式 / Gateway fallback 时使用的 Provider key | **Fallback 事实源** |
| PuddingClaw `config.json` | Fallback Provider 非敏感字段（provider、base_url、model） | **Fallback 事实源** |

PuddingClaw **不尝试**把本地 Fallback Provider 同步到 Higress，也不尝试覆盖 Higress Console 里的 Provider 配置。高级网关能力由用户在 Higress Console 里直接配置。

### 2.3 Fallback Provider Profile

PuddingClaw 中只保留一个简化版的 Provider Profile：

```text
FallbackProviderProfile
├─ provider                 # deepseek / openai / dashscope / custom
├─ display_name
├─ base_url
├─ api_key_secret_ref       # 本地 Secret Store 引用
├─ model                    # 直连 / fallback 时使用的模型名
└─ updated_at
```

该配置同时用于：

1. `direct` 模式调用。
2. Gateway 故障时的直连回退。

### 2.4 Gateway 模式下的模型名

Gateway 模式下，`ModelClient` 发送的 `model` 字段保持与 `config.json` 中 `llm.model` 一致（如 `deepseek-chat`）。Higress Console 负责把该模型名路由到真实 Provider。

PuddingClaw 不保存「Higress 模型别名 → Provider 模型名」的映射。如果用户想查看或修改映射，打开 Higress Console。

## 3. 同步规则

### 3.1 没有 PuddingClaw → Higress 的 Provider 同步

PuddingClaw 不再负责创建或更新 Higress Provider。用户启动 Full 模式后，需要（或已经在 Higress Console 里）自行配置 Provider、路由和密钥。

### 3.2 Higress → PuddingClaw 只读导入（可选）

为减少用户在 PuddingClaw 里重复填写非敏感字段，可以提供「从 Higress 导入 Fallback Provider」功能：

1. 用户点击「从 Higress 导入」。
2. PuddingClaw 调用 Higress API，读取 Console 中配置的 Provider 列表（非敏感字段）。
3. 用户选择一个 Provider 作为 fallback Provider。
4. PuddingClaw 填充 `provider`、`base_url`、`model`。
5. **API Key 必须由用户在 PuddingClaw 里手动输入**，Higress 只能返回掩码 key，无法反向获取明文。

导入后，PuddingClaw 本地 Profile 与 Higress 配置独立。后续 Higress 端修改不会自动同步回 PuddingClaw。

### 3.3 切换模式

#### direct → gateway

条件：

- Gateway 健康检查成功（`GET {AI_GATEWAY_URL}/health`）。
- `AI_GATEWAY_URL` 已配置。

不需要 Fallback Provider 同步到 Higress。只要网关健康即可切换。

#### gateway → direct

直接切换到本地 Fallback Provider Profile。不修改 Higress 配置。

### 3.4 Key 轮换

- **Higress 端轮换**：用户在 Higress Console 里改 key，PuddingClaw 无感知，fallback key 不会自动更新。
- **Fallback 端轮换**：用户在 PuddingClaw 里改 key，只影响 direct / fallback，不影响 Higress。

如果用户要求两端 key 一致，需要分别在两个地方更新。

## 4. 职责边界

| 能力 | PuddingClaw | Higress Console |
|---|---|---|
| Provider 录入（gateway 模式） | 不负责 | **事实源** |
| Provider 录入（direct/fallback） | **事实源** | 不负责 |
| Provider Key（gateway 模式） | 不保存 | **保存并用于上游调用** |
| Provider Key（direct/fallback） | **本地 Secret Store** | 不保存 |
| 路由、权重、多 Provider 策略 | 展示摘要/跳转 | **事实源** |
| 限流、Token 统计、审计 | 展示摘要或链接 | **事实源** |
| 模型名路由映射 | 不保存 | **事实源** |
| Gateway Console URL | 保存并提供「打开控制台」 | 提供管理界面 |
| Fallback 触发 | **负责** | 不负责 |

## 5. 设置页规则

### 5.1 默认模式

| 部署方式 | 默认模式 | 说明 |
|---|---|---|
| `docker compose up -d`（core） | `direct` | 不依赖 Higress，单 Fallback Provider 即可运行 |
| `docker compose --profile full up -d` | `gateway` | Higress 已启动，默认走网关 |
| 本地开发 | `direct` | 最小启动成本 |

Full 模式默认 gateway，但 `fallback_to_direct` 默认开启。用户需要先在 PuddingClaw 里配置好 Fallback Provider，才能在 Higress 不可用时降级。

### 5.2 模式选择

设置页顶部提供模式选择：

```text
[ Provider 直连 ] [ Higress Gateway ]
```

`direct` 模式展示：

- Fallback Provider 配置（provider、base_url、model、api_key）。

`gateway` 模式额外展示：

- Gateway API URL。
- Higress Console URL 与「打开管理后台」。
- 健康状态。
- `fallback_to_direct` 开关。
- 可选：从 Higress 导入 Fallback Provider 非敏感字段的按钮。

### 5.3 首次启用 gateway 模式时的引导

如果用户切换到 gateway 但 `AI_GATEWAY_URL` 未配置，或者 Higress 中没有对应模型路由，应提示：

> 请先在 Higress Console 中配置 Provider 和模型路由，再启用 Gateway 模式。
> [打开 Higress Console]

## 6. 安全约束

- API Key 不通过设置读取 API 返回明文。
- 日志、异常中禁止输出 Key。
- Fallback Provider Key 只保存在 PuddingClaw 本地 Secret Store。
- PuddingClaw 不存储 Higress 管理凭证；Higress API 调用使用只读或最小权限 token（如果需要）。
- 从 Higress 导入时只能读取非敏感字段，不能读取 Provider Key 明文。

## 7. 实施顺序

- [ ] 简化 `config.json` / Secret Store：只保留单个 `fallback_provider`（provider、base_url、model、api_key_ref）。
- [ ] 更新 `ModelClient`：gateway 模式下只读 `AI_GATEWAY_URL` 和 `llm.model`，不再读取本地 Provider key；fallback/direct 模式下读取 `fallback_provider`。
- [ ] 移除 Higress Provider 同步/upsert 相关代码（如果已有）。
- [ ] 增加「从 Higress 导入 Fallback Provider 非敏感字段」的适配器（可选，只读）。
- [ ] 设置页改为 direct / gateway 双模式，gateway 模式下提供 Console 跳转和导入按钮。
- [ ] 覆盖 direct、gateway、fallback 测试。

在 Higress Provider 配置文档完善前，Gateway 模式保持实验性，需要用户手动在 Higress Console 中完成 Provider 配置。
