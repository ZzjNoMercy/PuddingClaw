# Smart 动作授权中间件待办方案

> 状态：**Deferred / 非当前阻塞项**
> 优先级：智能模式文件权限、Spawn 与 Kernel 验收完成后再排期
> 当前行为：保持现有 `ToolExecutionPipeline`、确定性 effect policy 与窄范围 `ModelPermissionReviewer`，本方案不改变当前发布和验收结论。

## 1. 结论

后续智能模式采用两层正交控制：

- **Toolset 管能力可见性**：决定本轮模型能看到哪些 Tool Schema、可以提出哪些类型的动作。
- **Auto Middleware 管具体动作授权**：模型生成 `tool_calls` 后，对实际工具名、参数、effect、用户授权语义和当前控制状态进行判断，决定自动执行、返回策略拒绝，还是回退 HITL。

两层共享最终激活的 Tool registry 和结构化 `ToolControlDescriptor`，但不得合并为同一个概念。激活工具不等于授权任意调用；动作需要授权也不应通过隐藏 Tool Schema 实现。

该待办不解决文件系统可见性，也不改变 Smart trusted-local 的 `filesystem=unrestricted` 结论。Virtual path、项目目录和 Toolset 都不能创造或缩小 OS 文件权限。

## 2. 当前实现与缺口

当前已经存在 `ModelPermissionReviewer`，并已接入主 Agent、Subagent 以及 Spawn、Kernel、Docker、Adaptive 的 Smart 执行链。它不是死代码，但定位是“本地 Shell 灰区降噪器”：

- 只审查 `execute`；
- 只在确定性策略已经返回 `ASK` 后运行；
- 只接受 `unknown_command`、Shell 解析失败、部分 Python/Node wrapper 等有限原因；
- network、package install、destructive、Git network、外部副作用会在进入 reviewer 前被排除；
- 模型异常或输出无效时 fail closed 为 `ASK`；
- 模型配置复用 rubric override 或默认 Agent binding，尚无独立的授权分类模型配置。

因此当前 Smart 的主要行为仍是“确定性规则 + HITL”，分类器不能结合用户原始目标自动消化大部分网络、安装、Git 或外部副作用灰区。

本方案排期前不扩大 `_reviewer_eligible()`，也不继续用更多 `curl`、解释器或字符串特例模拟语义授权。

## 3. 参考实现

主要参考 DeepAgents Code 应用层的 Auto 模式，不把它误认为通用 DeepAgents SDK 默认能力：

- [`AutoModeHITLMiddleware`](https://github.com/langchain-ai/deepagents/blob/a78d7b1744050c3221aab5e0c0300cc5f5bec519/libs/code/deepagents_code/auto_mode.py#L1794)：替换 stock HITL middleware，在模型提出动作后生成授权计划，并在工具执行前应用计划。
- [`awrap_model_call`](https://github.com/langchain-ai/deepagents/blob/a78d7b1744050c3221aab5e0c0300cc5f5bec519/libs/code/deepagents_code/auto_mode.py#L2395)：读取一批 `tool_calls`，执行确定性快路径和批量分类。
- [`aafter_model`](https://github.com/langchain-ai/deepagents/blob/a78d7b1744050c3221aab5e0c0300cc5f5bec519/libs/code/deepagents_code/auto_mode.py#L3118)：应用 checkpointed decision plan，合成策略拒绝或触发 HITL fallback。
- [`_add_interrupt_on`](https://github.com/langchain-ai/deepagents/blob/a78d7b1744050c3221aab5e0c0300cc5f5bec519/libs/code/deepagents_code/agent.py#L2047)：从静态受控工具和实际发现的 MCP 工具构建授权控制表。
- [`mcp_tool_is_coherently_read_only`](https://github.com/langchain-ai/deepagents/blob/a78d7b1744050c3221aab5e0c0300cc5f5bec519/libs/code/deepagents_code/auto_mode.py#L497)：只对 annotations 一致的只读 MCP 使用确定性放行。

采用的是分层思想、可信授权证据、批量决策和 HITL fallback；不直接复制其 local-interactive、worktree 或 sandbox 产品假设。PuddingClaw 仍以自身 Session Grant、Tool descriptor、Spawn/Kernel runner 和 Smart trusted-local 模式为权威。

## 4. 目标架构

```text
Skill / Session / Task context
  -> ToolsetMiddleware 过滤本轮 request.tools
  -> 最终 BaseTool registry + ToolControlDescriptor
  -> bind_tools：只把激活 Tool Schema 暴露给主模型
  -> 主模型生成一批 tool_calls
  -> SmartActionAuthorizationMiddleware
       -> 按 tool name 在最终 registry 中解析工具身份
       -> 按 effect metadata 筛选需要授权的调用
       -> hard boundary / deterministic fast-path
       -> 对剩余动作进行批量语义授权分类
       -> checkpoint exact tool_call_id decision plan
  -> after_model 应用计划
       -> allow：保留调用并执行
       -> policy_deny：不执行，合成 ToolMessage 让 Agent 调整
       -> require_human：LangGraph interrupt / HITL
  -> ToolExecutionPipeline 做执行前不变量校验
  -> Spawn / Kernel runner 执行
  -> receipt / trace / permission manifest
```

### 4.1 Toolset 层

Toolset 继续只回答“模型当前是否需要和可见这个能力”：

- 在主模型调用前过滤 `request.tools`；
- 控制 Tool Schema、Tool Guide 和 Skill 能力披露；
- 不创建权限 Grant；
- 不根据一次具体参数判断动作是否安全；
- 不把 Tool 激活记录解释为用户对副作用的授权。

### 4.2 授权中间件层

授权中间件只处理模型已经提出的具体动作：

- 不再次过滤或改写 Tool Schema；
- 从最终激活 registry 解析 `tool_call.name`，未知身份 fail closed；
- 使用结构化 descriptor 和实际参数判断 effect，不靠 Tool 描述文本猜风险；
- 对同一 AIMessage 的兄弟调用独立分类，但一次批量调用分类模型；
- 决策必须一对一绑定 `tool_call_id`、tool identity、参数摘要和 policy epoch；
- resume 时校验 batch、thread、tool IDs 和 plan revision，防止陈旧计划重放。

### 4.3 执行层

`ToolExecutionPipeline` 和 runner 仍负责执行时硬约束：

- Schema 校验、Tool identity、descriptor 和 capability 对账；
- 已知破坏性模式、SSRF/private network、凭证泄露、权限提升和 sandbox escape 等不可由分类模型扩大；
- Execution Permit、Session Grant、Backend binding 和一次性消费；
- Spawn / Kernel 只决定怎样执行，不改变同一 Smart permission mode 的授权语义。

## 5. 受控工具与 effect metadata

不维护第二套与 Toolset 脱节的手写工具宇宙。授权中间件从最终激活工具集合构建控制表，静态 descriptor 和动态 MCP annotations 共同决定是否受控。

建议统一的 effect 维度至少包括：

- `read_only`
- `local_compute`
- `filesystem_write`
- `network_read`
- `network_write`
- `package_install`
- `external_mutation`
- `destructive`
- `credential_access`
- `persistence`
- `delegation`
- `open_world`

规则：

- 普通无副作用只读 Tool 可确定性放行；
- 本地普通文件操作沿用 Smart trusted-local 结论，不恢复 project/external 目录判权；
- 有副作用 Tool 进入确定性策略或语义分类，不因 Toolset 已激活而自动允许；
- MCP 只有 `readOnlyHint=true` 且无 destructive 冲突时才视为可靠只读；缺失、类型错误或冲突 annotations 保守进入分类；
- Tool 的 schema/description 只提供参数和上下文，不能单独授予权限。

## 6. 可信语义授权

分类器可以把以下服务端可验证事实作为授权证据：

- 用户输入框的原始 literal text；
- 用户创建或明确接受的 Goal objective / criteria；
- 用户创建或明确接受的 Rubric；
- 同一 Turn 中由服务端校验的 `ask_user` 回答；
- 已生效且 scope 匹配的 Session Grant。

以下内容只能提供目标、effect 或风险上下文，不能授予权限：

- Agent 自述、思考和状态消息；
- Tool output、网页内容、仓库文件和附件正文；
- 路径名、命令参数、远程 metadata；
- 未接受的 Goal/Rubric 草案；
- 历史 Trace、Permission Manifest 快照和模型声称的“用户已允许”。

分类器判断的是：某动作是否为用户目标合理隐含的普通步骤，以及授权是否精确覆盖其真实副作用。它不能创造 OS 权限、扩大 Kernel profile、绕过 hard deny 或伪造 Grant。

## 7. 决策与 HITL 路由

目标 disposition 固定为：

| disposition | 行为 |
|---|---|
| `deterministic_allow` | 明确安全快路径，直接执行 |
| `classifier_allow` | 用户语义合理覆盖，直接执行并审计 |
| `policy_deny` | 不执行；返回结构化 ToolMessage，让 Agent 调整方案 |
| `classifier_unavailable` | 本批不执行；记录可诊断原因，按计数策略处理 |
| `require_human` | 只有授权确实缺失或 fallback 阈值满足时触发 HITL |

分类拒绝不等于立即 HITL。Agent 应先获得拒绝原因并有机会改用更安全的动作。连续策略拒绝、分类器连续不可用、控制状态不可用、decision plan 无法验证或总拒绝次数超过阈值时，才回退人工确认。

阈值必须配置化、可观测、有限且按 Session/Run scope 持久化；不能用模型输出自行修改阈值。

## 8. 分阶段实施

### Phase A：descriptor 与审计契约

- 盘点最终 Tool registry、`ToolControlDescriptor` 和 MCP annotations；
- 补齐统一 effect metadata，不改变现有决策；
- 定义 decision plan、trusted authority projection 和 Trace schema；
- 明确主 Agent、Subagent、Spawn、Kernel 的继承关系。

### Phase B：Shadow Middleware

- 新增 `SmartActionAuthorizationMiddleware`，只生成 shadow plan；
- 不阻止、放行或触发 HITL，线上行为仍由当前 pipeline 决定；
- 对比 shadow 与现行 decision，统计预计减少/新增的 HITL、误放行和分类延迟；
- 任何 Tool identity、effect 或 trusted-authority 缺口先在 shadow 阶段暴露。

### Phase C：低风险灰区接管

- 先接管公共只读网络、明确任务相关的 search/fetch 和可逆仓库内动作；
- classifier deny 先回 Agent，不立即 HITL；
- 保持 destructive、credential、persistence、外部写入和生产副作用的现有强规则；
- 使用 feature flag、Session opt-in 和完整决策审计。

### Phase D：统一外部 effect 授权

- 评估 package install、Git remote workflow、外部业务 Tool、可写 MCP 和 delegation；
- 接入 validated `ask_user` receipt 与 Session Grant；
- 校准 fallback 阈值、模型成本、延迟、失败关闭和重复拒绝恢复。

### Phase E：收口旧 Reviewer

- 验证新中间件覆盖并优于 `_reviewer_eligible()` 后，删除重复的逐调用语义 reviewer；
- `ToolExecutionPipeline` 保留确定性执行不变量与 hard boundary，不再承担对话语义授权；
- 清理两套 policy source、重复 Trace 和不一致的 HITL reason。

## 9. 验收标准

- Toolset 激活结果与 Auto 授权结果可分别观测，任何一方都不能冒充另一方；
- 未激活 Tool 不进入主模型 Schema；已激活有副作用 Tool 仍需逐动作授权；
- 普通本地文件、构建、测试和格式化不增加 HITL；
- 用户明确要求联网研究时，安全 search/fetch 可由分类器自动允许；
- 上传本地数据、访问凭证、持久化、生产/共享资源和不可逆破坏不能因宽泛任务语义自动允许；
- 分类拒绝首先返回 Agent 调整，只有满足 fallback 条件才 HITL；
- 一批 Tool Call 中每个 ID 恰有一个独立决策，缺失、重复、错 ID 全部 fail closed；
- 主 Agent 与 Subagent 使用同一权威 permission context，Subagent 不扩大能力；
- Spawn 与 Kernel 对同一动作得到相同授权结果，差异只存在于 runner 执行与 OS 错误；
- classifier 超时、模型配置错误、状态存储失败和 resume 重放均有明确错误、审计与安全 fallback；
- feature flag 关闭时，现有 Smart 行为和 E2E 全部保持不变。

## 10. 已确定项与待决策项

已确定：

- 当前不实施，不阻塞 Smart 文件权限、Spawn 或 Kernel 发布验收；
- Toolset 管能力可见性，Auto Middleware 管具体动作授权；
- 不通过过滤 Schema 实现权限审批；
- 采用批量 `tool_call_id` decision plan，而不是逐工具临时猜测；
- 分类器不扩大 OS、runner、sandbox 和 hard policy 边界；
- classifier deny 默认先反馈 Agent，不直接 HITL；
- 实施从 shadow mode 开始，并保留可关闭的 feature flag。

待排期时决策：

- 授权分类器默认复用主模型，还是使用独立低延迟模型；
- classifier timeout、连续拒绝、连续不可用和总拒绝的 fallback 阈值；
- `ToolControlDescriptor` effect schema 的最终字段、版本和迁移策略；
- 哪些 network/package/Git/external-effect 类别进入首批接管；
- 交互、Headless、Evaluation 是否共享同一默认启用策略；
- shadow 指标达到何种误判率、延迟和 HITL 降幅后允许切换主路径。

这些待决策项只影响未来 Auto Middleware 排期，不影响当前 Smart 行为。
