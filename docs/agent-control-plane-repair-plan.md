# Agent 控制面一致性修复方案

> 状态：审核修订版<br>
> 范围：止血包、权限授权、Run/Goal 上下文、Skill 路由与激活、动态工具门控、子代理、SQL 生成与验收、Artifact 事务、模型流恢复<br>
> 原则：Agent 是编排者，Generator 是 SQL 作者，Validator 是放行权威

## 1. 背景与结论

当前问题不是单个判断条件写错，而是多个控制面分别维护“当前 Agent 能做什么”的事实，且这些事实的作用域和生命周期不一致：

- 权限系统把稳定的 Session 能力绑定到易变的 Docker 容器实例，容器变化后重复触发 HITL；
- Session 历史会跨 Run 回放 reasoning 和原始工具结果，旧 Run 的 Skill、curl 命令和结论会污染新任务；
- Skill 激活以 Session 为事实来源，工具门控却在每轮模型调用时动态裁剪，导致无关 Run 继承旧 Skill；
- system prompt 无条件描述数据库工具，实际 Tool Schema 又要求先读 Skill 才开放，模型会在“提示说存在、Schema 里不存在”的矛盾中乱试；
- Skill 成功解锁后没有明确的能力变更回执，模型容易继续锚定开场时“工具不可用”的判断；
- 子代理只接收一段自由文本任务，缺少父 Run 的模型上下文、验收合同、输出 Schema 和资源限制，精确数据任务会退化为长时间的摘要接力；
- SQL 工具虽然已有 Generator → Validator → Execute 的雏形，但 Agent、技术修订、持久化与恢复的边界还没有形成不可绕过的控制链。
- 验收器对成功验证产生假阴性，终态标准在中间循环抖动，动态 Evidence 又绕过语义熔断，最终被跨 Run 自动续跑放大；
- 外部 Artifact lease、临时验证文件和 patch/commit 接口缺少面向 Agent 的唯一恢复路径；
- 模型流式连接中断会直接终止长 Run，缺少有限重试、segment 替换和可恢复基础设施状态。

本方案不再增加一个新的关键词 Router 或“伪路由工具”。修复方向是建立一条统一、可持久化、可追溯的能力控制链：

```text
用户请求
  → Run/Goal 解析与上下文挂载
  → 非阻塞语义分类（只提供建议，不扩大权限）
  → Skill 读取与作用域激活
  → 动态 Capability Manifest 与真实 Tool Schema 对齐
  → Agent 编排
      ├─ 直接工具
      ├─ 受约束的子代理
      └─ SQL：Generator → Validator → Execute
  → 结构化 Evidence / Handoff
  → 验收与下一 Run 的受控继承
```

## 2. 第一性原理与不可破坏约束

### 2.1 单一权威

每类事实只能有一个放行权威：

| 事实 | 权威 | 非权威信息 |
| --- | --- | --- |
| 某操作是否允许执行 | Permission Policy + 有效 Grant | LLM 判断、Skill 候选、UI 状态 |
| 某工具当前是否可调用 | 当前模型调用的 Tool Schema | 静态 prompt、历史调用、Session 旧状态 |
| 某 Skill 是否已激活 | Run/Goal 作用域的 Skill Activation | 曾经读过 Skill、Router 候选 |
| SQL 内容 | Generator 产生的不可变 Generation | Agent 自写或修改的 SQL |
| SQL 是否可执行 | Validator 签发的 Validation Receipt | Generator 自检、Agent 判断 |
| 任务是否完成 | 验收合同 + 成功 Evidence | 工具调用次数、Agent 自述 |

### 2.2 建议不能扩大能力

- 语义 Router 只返回任务理解、Skill 候选和执行建议；
- 成功读取对应 `SKILL.md` 后才能激活 Skill 工具集；
- Skill 激活不能自动授予联网、文件写入或危险命令权限；
- UI 展示不能成为后端授权依据；
- 低置信度或无匹配 Skill 时交给通用 Agent，不视为错误。

### 2.3 作用域优先于便利性

- 权限 Grant 按能力定义稳定作用域；
- Skill 激活默认属于 Run，只有明确的 Goal 连续性才允许继承；
- reasoning、原始工具输入输出和控制命令不跨 Run；
- Session 只保存被明确建模、可校验、可过期的持久状态。

### 2.4 恢复必须基于结构化状态

页面刷新、compact、Backend 重启、容器重建和 HITL 恢复后，系统应从结构化状态恢复：

- Grant 的语义身份与审计链；
- Goal/Run 关系；
- Todo 与完成证据；
- Skill Activation 和 Skill Session State；
- SQL Generation、Validation Receipt 和结果 Evidence；
- Artifact lease/hash 等控制标识。

不得依赖模型从旧 reasoning 或原始工具日志中“猜回”状态。

### 2.5 所有 fail-closed 硬门必须可在有界步骤内闭合

新增安全门、验收门或结构化协议时，不能只定义“什么情况拒绝”，还必须同时交付：

1. **可执行修复协议**：明确缺少什么、由哪个角色或工具修复、成功后生成什么 Receipt/Evidence；
2. **至少一条可达闭合路径**：Agent 能在预先定义的有限步骤内获得所需 Profile、Schema、授权或 Validator 结果；
3. **语义停滞熔断**：相同缺口在限定次数内没有改善时停止自动修复，不因新增日志或 Evidence 假装取得进展；
4. **明确降级边界**：自动闭合不可达时，进入结构化 HITL、返回可恢复 blocker，或缩小任务范围并明确标注；不得静默放行，也不得无限 refuse/retry。

每个 fail-closed 机制上线前必须完成“闭合性推演”：从失败状态出发，证明 Agent 在 N 步内能够成功、转入用户决策，或诚实终止。SQL 语义 Validator、Todo Evidence 绑定、子代理 Result Envelope 和 Artifact 事务均受此约束。

## 3. 当前故障的因果链

### 3.1 Prompt 与工具门控矛盾

`backend/prompts/deepagents/TOOL_GUIDES.md` 无条件要求先调用数据库生成器，`backend/graph/deepagents_prompt_builder.py` 又无条件把该文档拼入 system prompt；但 `backend/graph/middlewares/toolset.py` 只有在成功读取数据库 Skill 后才把相关工具加入 Tool Schema。

结果是模型在开场收到两份互相冲突的事实：

- prompt：数据库工具可用且应优先使用；
- Tool Schema：数据库工具不存在。

模型随后尝试猜工具名、改派子代理或重复读取资源，是系统矛盾的合理后果，而不是单纯的模型“不听话”。

### 3.2 Skill 软提示不是可靠控制链

`SkillIntentRouterMiddleware` 只消费任务画像中的 Skill 候选。若非阻塞语义分类超时、低置信度或没有识别“刷新 HTML + 重算数据”的数据库性质，就不会注入“先读 Skill”的提示。

该 Router 应继续作为建议层，而不能承担工具可发现性的唯一入口。主 Agent 必须始终能从动态 Skill Catalog 发现 Skill；能力清单还必须准确描述“推荐但尚未激活”的状态。

### 3.3 解锁后缺少能力变更回执

成功读取 `SKILL.md` 后，`ToolsetMiddleware` 会在下一次模型调用重新计算可见工具，但没有告诉模型 Tool Schema 已经发生变化。模型可能继续沿用第一次调用时“工具不存在”的内部结论。

### 3.4 Session 历史造成跨 Run 污染

`backend/graph/session_manager.py` 当前会把历史工具结果序列化为 LLM 上下文，并回放历史 `reasoning_content`。因此旧 Run 中的：

- `read_file("/skills/aihot/SKILL.md")`；
- AI HOT curl 命令；
- Skill 工具结果；
- 权限申请与失败修复过程；

都可能在无关的新 Run 中重新进入模型上下文。与此同时，`loaded_skill_ids` 以 Session 全局状态恢复，进一步把上下文污染变成真实工具激活。

### 3.5 子代理把控制缺口放大为长循环

当前子代理的任务边界主要是一段 `description`：

- 没有不可变的父 Run 目标、Todo 切片和完成条件；
- 没有保证挂载用户选择的分析模型及其语义资产；
- Skill 是否可用依赖 Session 缓存或子代理是否自行读 Skill；
- 精确查询结果被压缩成自由文本摘要，父 Agent 无法可靠复用；
- 缺少 wall-clock timeout、模型调用上限、停滞检测和主动取消协议；
- 子代理可以直接向用户提问，而不是把阻塞原因交回父 Agent；
- 前端只能显示“子代理处理中 + description”，看不到阶段、工具、进度和阻塞点。

### 3.6 SQL 权威链没有完全闭合

现有 `database_sql_generate`、`database_sql_validate`、`database_sql_execute` 已实现按 `generation_id` 传递 SQL 的主要路径，prompt 也已要求 Agent 不修改 SQL。但还需要把这些约束从提示升级为运行时不可绕过的协议：

- 技术错误必须自动反馈给 Generator，而不是让 Agent 改 SQL；
- Validator 的放行结果必须绑定 SQL hash、Generation 版本和语义合同；
- HITL、compact、进程重启后仍能恢复 Generation；
- 子代理同样不能成为 SQL 作者或用自由文本转抄精确结果。

## 4. 目标状态模型

### 4.1 Goal、Run、Session 的边界

```text
Session
  ├─ Permission Grants（按能力定义稳定作用域）
  ├─ Skill Session State（结构化、可过期，不激活工具）
  └─ Goals
       ├─ Goal revision 1
       │    ├─ Run A
       │    └─ Run B（明确继续 A，可继承受控状态）
       └─ Goal revision 2（需求改变，重新评估继承）
```

- **Session**：对话容器和长期审计边界；
- **Goal revision**：用户目标的一个稳定版本，目标发生实质变化时递增；
- **Run**：一次用户输入触发的执行生命周期；
- **Task/Subagent Run**：父 Run 内部的受约束委托，不自动成为新的 Session 或 Goal。

### 4.2 统一控制状态

建议为每个 Run 持久化以下控制对象：

```json
{
  "run_id": "run-...",
  "goal_id": "goal-...",
  "goal_revision": 3,
  "task_profile": {
    "work_natures": ["分析并刷新产品报告"],
    "delivery_forms": ["artifact"],
    "skill_candidates": [
      {
        "skill_id": "database-analysis",
        "confidence": 0.93,
        "evidence": "需要重算报告中的年度数据库指标"
      }
    ],
    "execution_route": "skill_first",
    "native_fallback": true
  },
  "skill_activations": [],
  "capability_revision": 0,
  "verification_contract_id": "verify-...",
  "handoff_summary_id": null
}
```

非阻塞语义 Router 可以异步补全 `task_profile`，但不能阻塞权限、附件、用户选择的分析模型和基础上下文挂载，也不能在完成前扩大工具能力。

## 5. 权限 Grant 与重复 HITL 修复

### 5.1 根因

`RunPermissionContext.grant_bindings()` 当前把 `backend_id` 等所有运行信息写进每一种 Grant 的 bindings；Grant 的匹配和 Session 去重又要求完整 bindings 全等。Docker 容器 ID 变化后，原有 Session 联网授权就被视为不同授权。

### 5.2 能力特定的 Binding Projection

新增统一策略入口，例如：

```python
PermissionBindingPolicy.project(
    grant_type,
    scope,
    target_kind,
    runtime_context,
) -> StableBindings
```

不同能力只绑定真正影响安全边界的稳定字段：

| Grant 类型 | 应绑定 | 不应绑定 |
| --- | --- | --- |
| Session 全来源联网 | session、policy epoch/version、backend mode、workspace | Docker/container instance ID |
| Session 指定来源联网 | 上述字段 + 规范化 host/source | 临时 IP、单次 tool call ID |
| 外部目录/文件只读 | session、workspace、规范化资源身份、policy version | 容器实例 ID |
| 同命令放行 | session/run 作用域、规范化命令指纹、风险标签 | 展示文本、临时 tool call ID |
| 写入/安装/危险操作 | 更严格的 run、workspace、target、command fingerprint | 不相关的后端实例信息 |

所有新 Grant 增加：

```json
{
  "binding_schema_version": 2,
  "semantic_key": "sha256:...",
  "stable_bindings": {},
  "runtime_observations": {
    "backend_id_at_approval": "container-..."
  }
}
```

`runtime_observations` 仅用于审计，不参与复用判断。

### 5.3 语义去重与旧 Grant 迁移

迁移过程必须可回滚、保留审计：

1. 为所有有效旧 Grant 计算 v2 `semantic_key`；
2. 按 semantic key 分组；
3. 选择最新且仍有效的 Grant 作为权威记录；
4. 其他记录标记 `superseded_at`、`superseded_by`、`supersede_reason`；
5. 合并关联 request/tool call 审计引用，不删除历史；
6. API 和 UI 默认只返回未被 supersede 的有效 Grant；
7. 写路径使用 Session 锁或存储层唯一约束，避免并发 HITL 创建两个同义 Grant。

容器重建不应使 Session 联网授权失效；但 backend mode、workspace 或 policy epoch 变化必须重新评估，不能借迁移放宽边界。

### 5.4 并发 HITL 合并

同一时刻出现两个联网工具请求时：

- 后端以 semantic key 聚合为一个 pending decision；
- 用户选择“本 Session 所有网络来源”后，所有兼容 pending request 原子重评估并继续；
- “仅允许本次”只解决对应 request，不影响其他 pending request；
- 权限卡片显示实际授权的能力类型，不能把联网能力误标成“同命令授权”。

## 6. 跨 Run 上下文隔离与合法连续性

### 6.1 禁止跨 Run 回放的内容

新 Run 不得直接收到旧 Run 的：

- reasoning / chain-of-thought；
- Skill 文件读取结果；
- 原始工具输入输出；
- curl、shell、安装和权限命令；
- Permission/HITL 卡片内容；
- Tool Schema/解锁提示；
- Router 内部提示和验收内部推理。

同一 Run 内可以保留必要的工具协议消息以维持调用闭环；跨 Run 必须转换成结构化证据。

### 6.2 用 Run Handoff 替换原始日志回放

新增 `RunHandoffSummary`：

```json
{
  "source_run_id": "run-a",
  "goal_id": "goal-x",
  "goal_revision": 3,
  "terminal_status": "completed|cancelled|failed|interrupted",
  "objective": "刷新 2021-2026 产品配置报告",
  "completed_todos": [],
  "durable_facts": [],
  "evidence_refs": [],
  "artifact_refs": [
    {"path": "...", "sha256": "...", "lease_id": "..."}
  ],
  "sql_generation_refs": [],
  "unresolved_gaps": [],
  "created_at": "..."
}
```

继承规则：

| 场景 | 可继承 |
| --- | --- |
| 同一 Run 恢复 | 完整协议状态、Todo、工具闭环、有效控制 ID |
| 同 Goal 同 revision 的“继续” | Handoff、证据引用、未完成 Todo、仍有效的 scoped activation |
| Goal revision 改变 | 只继承仍被新目标显式引用的 Evidence/Artifact，重新评估 Skill |
| 无关新 Run | 用户可见对话与高层事实；不继承工具、reasoning、Skill activation |

### 6.3 Compact 与中断

compact 摘要必须采用结构化保留字段，而不是自由文本尽力复述：

- `goal_id`、`goal_revision`、`run_id`；
- Todo ID、状态和依赖；
- Grant 引用而非 Grant 展示文本；
- Artifact path/hash/lease/expiry；
- SQL generation_id/hash/validation receipt；
- Evidence/source ID；
- 当前阻塞条件和下一动作。

用户主动终止 Run 时：

- 立即向子代理和运行中工具传播 cancellation；
- 生成 `terminal_status=cancelled` 的 Handoff；
- 不把未完成 reasoning 当作可继承结论；
- 已成功产生的独立 Evidence 可以保留，但必须标明来源 Run 已取消。

## 7. Skill 路由、激活与 Session State

### 7.1 路由与激活分离

- 动态扫描已安装 Skill Catalog；
- 语义 Router 可返回多个候选、置信度和证据；
- `SkillIntentRouter` 直接消费持久化候选，不再二次关键词分类；
- 主 Agent 仍能看到动态 Catalog 并自行选择读取 Skill；
- 用户明确指定已安装 Skill 时优先级最高；
- 用户明确指定但未安装时进入安装恢复路径；
- 无匹配或低置信度时正常进入 native fallback；
- 只有成功读取匹配版本的 `SKILL.md` 才创建 Activation。

### 7.2 Run/Goal 作用域 Activation

```json
{
  "activation_id": "skillact-...",
  "skill_id": "database-analysis",
  "skill_content_sha256": "sha256:...",
  "scope": "run|goal_revision",
  "run_id": "run-...",
  "goal_id": "goal-...",
  "goal_revision": 3,
  "activated_by_tool_call_id": "toolcall-...",
  "toolset_ids": ["database"],
  "activated_at": "...",
  "expires_at": null
}
```

继承规则：

- 同一 Run 始终有效；
- 明确的同 Goal 同 revision 延续，可继承 `goal_revision` scope；
- 新 Goal、无关 Run 或 Goal revision 变化时不自动继承；
- Skill 内容 hash 改变后必须重新读取；
- Router 候选与 Session State 都不能直接激活工具。

现有 Session 全局 `loaded_skill_ids` 不应直接迁移为新 Activation。它只保留为 legacy audit；新版本上线后的首个 Run 按当前任务重新激活。

### 7.3 结构化 Skill Session State

用于保存合法的“每 Session 一次”状态，例如 Skill 版本自检：

```json
{
  "skill_id": "aihot",
  "state_key": "version_check",
  "schema_version": 1,
  "skill_content_sha256": "sha256:...",
  "value": {"remote_version": "0.3.6"},
  "evidence_ref": "evidence-...",
  "source_run_id": "run-...",
  "created_at": "...",
  "expires_at": "..."
}
```

约束：

- 仅允许 Skill 声明过的 state schema；
- 必须有 freshness/expiry 或明确的永久语义；
- Skill hash 变化时旧 state 默认失效；
- 当前 Run 必须先激活该 Skill，才能消费它的 Session State；
- State 只提供事实，绝不回放产生该事实的 curl/工具日志；
- State 不授予联网或其他权限。

这能保留 AI HOT “本 Session 已检查版本”的连续性，同时彻底阻断 AI HOT 工具上下文进入无关报告 Run。

## 8. Prompt、Tool Schema 与能力回执

### 8.1 静态 Tool Guide 改为条件协议

`TOOL_GUIDES.md` 只描述协议，不宣称工具当前一定存在：

```text
当 database-analysis capability 已激活且相关工具出现在 Tool Schema 时，
必须遵循 Generate → Validate → Execute。

当任务需要数据库能力但尚未激活时，先从动态 Skill Catalog 读取对应
SKILL.md。不得猜测或调用 Tool Schema 中不存在的工具。
```

Skill 名称不得写入 `INTENT_REGISTRY`；上述示例中的具体 Skill 来自动 Catalog/候选，运行时生成。

### 8.2 每次模型调用生成 Capability Manifest

在工具过滤完成后，生成紧凑、瞬态的 Manifest：

```json
{
  "capability_revision": 4,
  "active_skills": ["database-analysis"],
  "available_business_tools": [
    "database_sql_generate",
    "database_sql_validate",
    "database_sql_execute"
  ],
  "recommended_inactive_skills": [],
  "selected_analytics_model": "model-..."
}
```

实现上应由 `ToolsetMiddleware` 的同一份决策结果同时驱动：

1. 实际 `request.tools`；
2. 注入给模型的 Capability Manifest；
3. Trace 中的 tool schema 快照。

不能由三个模块各自重新推导，否则仍会漂移。

### 8.3 Skill 解锁回执

成功读取 Skill 并导致工具集变化后，下一次模型调用注入一次性回执：

```text
能力已更新：Skill database-analysis 已激活。
新增可用工具：database_sql_generate、database_sql_validate、database_sql_execute。
请基于新的 Tool Schema 继续，不要沿用此前“工具不可用”的判断。
```

回执要求：

- 使用 `capability_revision` 与 `last_announced_revision` 保证恰好一次；
- 只作为当前 Run 的瞬态控制消息，不写入普通聊天历史；
- 记录到 Trace；
- 前端可显示“数据库能力已就绪”，但 UI 文案不参与控制判断。

## 9. 子代理执行合同与可观测性

### 9.1 委托仍由 LLM 决定，Backend 负责补全合同

是否拆分任务可以由主 Agent 判断，但不能把一句自由文本直接当作完整委托。调用 `task` 时，Backend 根据父 Run 自动生成不可变 `DelegationContract`：

```json
{
  "subagent_run_id": "subrun-...",
  "parent_run_id": "run-...",
  "parent_tool_call_id": "toolcall-...",
  "goal_id": "goal-...",
  "goal_revision": 3,
  "objective": "查询报告图表所需的 2021-2026 精确数据",
  "todo_slice": ["todo-4", "todo-5"],
  "selected_analytics_model": "model-...",
  "semantic_context_refs": ["semantic-..."],
  "allowed_skill_activations": ["skillact-..."],
  "allowed_toolsets": ["database-read"],
  "expected_output_schema": "DatabaseEvidenceBatch/v1",
  "completion_conditions": [],
  "limits": {
    "wall_clock_seconds": 600,
    "model_calls": 12,
    "tool_calls": 30,
    "idle_seconds": 90
  }
}
```

用户选择的分析模型、Playbook、数据资产摘要和语义索引必须按引用挂载给子代理；这属于模型上下文底线，不依赖任务分类是否命中。

### 9.2 精确任务禁止自由文本交付

对数据库、Artifact 修改、文件 hash、来源证据等精确任务：

- 子代理返回结构化 Result Envelope；
- 数据结果使用 Evidence ID、字段 Schema、行数、hash 和来源引用；
- SQL 只返回 generation_id / validation receipt，不转抄 SQL；
- Artifact 只返回路径、hash、lease 与变更摘要；
- 父 Agent 只消费注册过的结果，不从自然语言摘要中解析数字。

自由文本仅作为给父 Agent 的简要说明，不是数据权威。

### 9.3 资源限制、取消与阻塞

- 子代理必须有 wall-clock timeout、模型调用上限、工具调用上限和 idle timeout；
- 父 Run 取消时级联取消所有子代理；
- 子代理不能直接向用户提问，应返回 `blocked` 和结构化 `question_for_parent`；
- 父 Agent 决定是否已有答案、换策略或发起用户 HITL；
- 超时必须返回部分 Evidence、未完成项和结构化 `TimeoutHandoff`，不能只返回“失败”；
- Runtime 收到超时后必须回收控制权，并向主 Agent 注入明确指令：“子代理已超时，请接管并由你继续推进以下剩余任务”；
- 主 Agent 接管后优先直接使用现有工具和 Evidence 推进，不得把同一 Todo 原样再次委托给相同类型子代理；
- 相同 Todo 不得无限重复委托，父 Agent 必须记录尝试次数和差异化策略。

超时回退不是普通自然语言摘要，而是控制协议：

```json
{
  "status": "timed_out",
  "subagent_run_id": "subrun-...",
  "completed_todo_ids": ["todo-4"],
  "remaining_todo_ids": ["todo-5"],
  "evidence_refs": ["evidence-..."],
  "last_successful_action": "已完成最新日期查询",
  "blocking_or_timeout_reason": "wall_clock_limit",
  "recommended_parent_action": "continue_directly",
  "retry_same_delegation_allowed": false
}
```

主 Agent 下一次模型调用必须同时收到：

1. `TimeoutHandoff`；
2. 当前仍可用的 Tool Schema / Capability Manifest；
3. 已完成 Evidence 和剩余 Todo；
4. 强制性的 `continue_directly` 控制提示。

只有主 Agent 判断原委托边界或工具集发生实质变化，并生成新的 DelegationContract 时，才允许再次委托；不能用“重新派一次”代替接管。

默认限制应按任务类型配置，而不是写死一个全局值。结合本次数据库批次约 15 分钟的实测长尾，数据库批次首版采用 10 分钟 / 12 次模型调用作为软预算；达到软预算时优先生成 TimeoutHandoff 并让主 Agent 接管，而不是把正常长尾直接判成业务失败。上线后按 §13.2 的 P50/P95、有效 Evidence 产出率和接管成功率调整；确需更长预算的任务必须在 UI 显式展示。

### 9.4 前端可观测性

前端把当前单行“子代理处理中”升级为可展开的嵌套时间线：

```text
子代理处理中 · 查询 2021-2026 图表数据 · 02:13
  ├─ 已挂载：产品配置分析模型
  ├─ 已完成：确认数据最新日期
  ├─ 正在执行：年度更新量查询（2/5）
  ├─ 最近活动：database_sql_execute · 8 秒前
  └─ 软预算：10 分钟 · 12 次模型调用 · 超限后主 Agent 接管
```

事件至少包括：

- `subagent_started`；
- `context_mounted`；
- `subagent_stage_changed`；
- `tool_started/tool_completed/tool_failed`；
- `permission_waiting`；
- `progress_updated`；
- `subagent_blocked`；
- `subagent_fallback_to_parent`；
- `subagent_completed/failed/cancelled/timed_out`。

发生超时时，聊天区和嵌套时间线应先显示“子代理已超时，主 Agent 正在接管剩余任务”，随后进入主 Agent 的正常处理状态，不能继续显示子代理转圈，也不能让用户误以为整个 Run 已失败。

只展示普通过程说明、阶段和工具行为，不展示隐藏 chain-of-thought。页面刷新后从持久化事件恢复，不把已完成任务继续显示为转圈。

## 10. SQL 控制链：Agent 编排，Generator 写，Validator 放行

### 10.1 不可绕过的状态机

```text
Agent 提交 QueryIntent
  → Generator 生成 SqlGeneration（不可变）
  → Validator 校验语义、范围与安全
      ├─ technical_reject → 结构化反馈 Generator 自动修订
      ├─ business_ambiguity → Agent 用自然语言向用户澄清
      └─ approved → ValidationReceipt
  → Execute 只接受匹配的 generation_id + receipt
  → ResultEvidence
  → Agent 解释结果并组织交付
```

### 10.2 QueryIntent

Agent 只描述业务问题，不提交 SQL：

```json
{
  "question": "计算 2020-2026 年传统能源与新能源平均更新周期",
  "analytics_model_id": "model-...",
  "semantic_asset_refs": [
    "measure:launch_cycle",
    "dimension:launch_time",
    "dimension:energy_type"
  ],
  "filters": ["排除皮卡"],
  "grain": ["year", "energy_category"],
  "output_contract": {
    "metrics": ["avg_launch_cycle_days"],
    "time_range": [2020, 2026]
  }
}
```

若 Agent 认为外部环境或用户反馈会改变口径，应把反馈写入 QueryIntent/Revision Instruction，再交给 Generator；不得直接手改 SQL。

### 10.3 SqlGeneration 与 Validation Receipt

```json
{
  "generation_id": "sqlgen-...",
  "parent_generation_id": null,
  "query_intent_hash": "sha256:...",
  "sql_sha256": "sha256:...",
  "generator_version": "...",
  "semantic_contract": {},
  "created_at": "..."
}
```

```json
{
  "validation_receipt_id": "sqlval-...",
  "generation_id": "sqlgen-...",
  "sql_sha256": "sha256:...",
  "validator_version": "...",
  "checks": {
    "safety": "passed",
    "schema_scope": "passed",
    "semantic_contract": "passed",
    "guardrails": "passed"
  },
  "expires_at": null
}
```

Execute 必须验证：

- receipt 对应当前 generation；
- SQL hash 完全一致；
- Validator 版本和策略仍有效；
- analytics model / semantic contract 未被替换；
- 执行权限仍有效。

### 10.4 自动修订与 HITL 边界

技术问题自动闭环，不打扰用户：

- SQL 语法；
- 字段或表错误；
- nullable tuple / guardrail；
- 数据库方言；
- 超时后的可优化执行计划。

这些错误以结构化 `revision_instruction` 返回 Generator，生成新的 child generation，再由 Validator 校验。

只有真正改变业务口径的问题才进入 HITL，例如：

- “更新周期”按车系还是款型；
- 某能源类型是否归入新能源；
- 缺失值是否视为未配备；
- 时间范围或排除条件存在冲突。

用户看到的是口径差异和影响，不需要阅读长 SQL。

### 10.5 持久化与恢复

现有内存 Generation Registry 应升级为 Run/Goal 作用域的持久化 Ledger。compact、HITL、Backend 重启或子代理切换后，必须能按 ID 恢复 Generation 和 Receipt；原始 SQL 可保存在受控 Ledger/Trace 中，默认不注入聊天上下文。

## 11. 实施顺序

### P0-0：止血包

目标：在不等待状态机重构和数据迁移的前提下，先停止当前用户可见的死循环、重复 HITL 和验收刷屏。该批次必须可独立发布、独立回滚。

1. 将 completion gate 的失败签名改为语义签名，只包含 `criterion_id + failure_kind + normalized_gap + missing_requirement`；
2. 将 `publication_reference` 从中间 revision 拆出，只在终态候选回答上执行；
3. 短期扩展 `_command_performs_validation`，兼容 `node --check`、明确的 Python Validator 和现有 HTML/JS 校验入口；
4. `deterministic_checks_completed` 使用稳定活动 ID，同一 Run 原位更新，不重复追加“发现完成条件缺口”；
5. 未运行的 LLM criteria 统一标记 `not_evaluated`，最终报告不制造假失败；
6. Grant 查询/匹配侧按语义 key 复用并隐藏重复有效项；本批次不要求先完成 Grant 存储迁移。

P0-0 只负责立即止血，不替代后续 Validation Receipt、Grant schema migration 和跨 Run 状态模型。其测试必须证明旧数据和旧 Grant 在未迁移时也能安全工作。

主要文件：

- `backend/graph/deepagents_manager.py`
- `backend/harness/verification_activations.py`
- `backend/harness/deterministic_checks.py`
- `backend/harness/coordinators.py`
- `backend/graph/session_manager.py`
- `frontend/src/lib/store.tsx`

### P0-A：权限 Binding 与 Grant 迁移

目标：立即解决重复 HITL 和容器重建后授权失效。

1. 引入 capability-specific binding projection；
2. 新增 binding schema version 和 semantic key；
3. 改造 Grant add/match/consume/revoke；
4. 实现旧 Grant 的 supersede 迁移；
5. pending HITL 按 semantic key 原子合并；
6. UI 对有效 Grant 做防御性去重。

主要文件：

- `backend/graph/permission_policy.py`
- `backend/graph/session_manager.py`
- Permission API / schema / migration
- 前端权限 Store 与卡片组件

### P0-B：跨 Run 上下文隔离

目标：立即阻断 AI HOT、curl、reasoning 和旧 Skill 工具结果污染。

1. 停止在新 Run 回放历史 reasoning；
2. 删除跨 Run 的 `_tool_result_context()` 原始序列化路径；
3. 新增 Evidence Ledger 与 Run Handoff；
4. continuation 仅按 Goal/revision 继承；
5. compact 和取消写入结构化控制状态。

主要文件：

- `backend/graph/session_manager.py`
- Run/Goal model 与 persistence
- compact/summarization middleware
- Trace/Evidence schema

### P0-C：验收证据闭环与可执行反馈

目标：在 P0-0 已止血的基础上，以结构化 Receipt 完成长期权威链，修复“实际验证成功但 Harness 永远不认”的死路，并防止失败报告把 Agent 引向无关返工。

1. 以结构化 `ValidationReceipt` 替代仅按命令名猜测测试、构建或静态检查；
2. Receipt 必须绑定最终 Artifact ID、内容 hash、Validator 类型、命令退出状态和检查摘要；
3. 短期兼容 `node --check`、`npx eslint/tsc`、明确的 Python Validator 等常见校验路径，但不把继续扩大命令白名单作为长期架构；
4. scratch 副本验证只有在内容 hash 与最终目标 Artifact Receipt 一致时才可继承；宿主机路径的 `execute` 拦截继续保留；
5. 确定性前置检查未通过时，只报告真实 blocker；未运行的 LLM criteria 标记为 `not_evaluated`，不能伪装成验收失败；
6. revision 反馈必须包含可执行的修复协议、当前认可的 Validator 和目标 Artifact；
7. `artifact_delivery` 中“最终回答引用产物”拆为终态发布条件，不在中间 repair 循环制造噪音；
8. Todo 支持完成证据和重开机制；名为“验证闭环”的 Todo 只有绑定验收 Receipt 才能完成；
9. Agent 在拿到最终 Receipt 前只能说“自检完成”，不能向用户发布“验证通过”。

建议的 Receipt：

```json
{
  "validation_receipt_id": "validation-...",
  "run_id": "run-...",
  "goal_id": "goal-...",
  "goal_revision": 1,
  "validator_kind": "html_structure|javascript_syntax|project_test|static_check",
  "validator_version": "...",
  "artifact_refs": [
    {
      "artifact_id": "artifact-...",
      "content_sha256": "sha256:..."
    }
  ],
  "command_evidence_ref": "evidence-...",
  "exit_code": 0,
  "checks_passed": 26,
  "checks_failed": 0,
  "created_at": "..."
}
```

主要文件：

- `backend/harness/verification_activations.py`
- `backend/harness/deterministic_checks.py`
- `backend/harness/coordinators.py`
- `backend/graph/deepagents_manager.py`
- Todo model / `update_todos` / Harness Todo middleware
- Artifact Receipt 与前端验收状态组件

### P0-D：模型流式传输中断恢复

目标：模型 API、Gateway 或网络在流式响应中途断开时，不让整个长任务丢失，也不把传输故障误报为业务验收缺口。

当前 `ModelClient.astream()` 只允许在首 token 之前从 Gateway 回退到直连 Provider；一旦已经输出部分 token，
`RemoteProtocolError`、连接重置或 read timeout 会直接向上抛出。这样虽然避免了重复文本，但会让 Run 在 model
节点带 traceback 终止，长任务已经形成的 Todo、Artifact、Lease 和 Evidence 无法获得清晰的可恢复语义。

修复要求：

1. 将 `RemoteProtocolError`、连接重置、流式 read timeout 和兼容的 Provider 连接异常归类为 `model_transport_interrupted`；
2. model 节点按同一输入执行有限重试，首版最多 2 次并使用短指数退避；用户主动取消不得触发重试；
3. 已经展示的部分文本标记为 interrupted，新 attempt 成功后原位替换该 segment，禁止追加造成重复内容；
4. 截断内容不得作为降级完成结果、最终回答、Tool Call、Todo Evidence 或 Verification Evidence；
5. 只有完整 AIMessage 中解析成功的完整 Tool Call 才可进入工具节点，残缺 Tool Call 永不执行；
6. 前序已经成功的工具调用和 Artifact Receipt 保持幂等复用，模型调用重试不得重放已完成副作用；
7. Gateway 到直连 Provider 的切换只在模型、参数和工具协议兼容时允许，并记录实际 route 与 attempt；
8. 重试耗尽后将 Run 标记为 `infrastructure_error/model_transport_interrupted`，持久化 Goal、Todo、Artifact Lease、数据库 Evidence 和 checkpoint，允许用户或有限控制重试继续；
9. 传输中断不进入业务 revision，不生成“发现完成条件缺口”，不消耗业务验收轮次，也不触发无界跨 Run 续跑；
10. 检查并显式配置 Gateway 与上游的流式 read/idle timeout，健康检查 timeout 不能代替长连接 timeout。

恢复事件建议：

```json
{
  "type": "model_transport_interrupted",
  "run_id": "run-...",
  "model_node_id": "...",
  "attempt": 1,
  "route": "gateway",
  "provider": "deepseek",
  "model": "deepseek-v4-pro",
  "chunks_received": 42,
  "tokens_received": 318,
  "first_token_ms": 1250,
  "last_chunk_at": "...",
  "provider_request_id": "...",
  "error_class": "RemoteProtocolError",
  "retryable": true,
  "next_action": "retry_same_model_node"
}
```

主要文件：

- `backend/llm/model_client.py`
- `backend/graph/deepagents_manager.py`
- model node retry/checkpoint middleware
- Gateway/Higress 路由与 timeout 配置
- `frontend/src/lib/store.tsx`
- Trace 与 Run outcome schema

### P1-A：Skill 作用域与 Capability Manifest

目标：消除跨 Run Skill 激活和 prompt/schema 矛盾。

1. 将 `loaded_skill_ids` 替换为 scoped Activation；
2. 增加 Skill Session State；
3. 静态 Tool Guide 改成条件协议；
4. Toolset 单点生成 Schema、Manifest 和 Trace；
5. 增加一次性 unlock receipt；
6. 保持非阻塞语义 Router，不回退到关键词 Router。

主要文件：

- `backend/graph/middlewares/toolset.py`
- `backend/graph/middlewares/skill_intent_router.py`
- `backend/graph/deepagents_prompt_builder.py`
- `backend/prompts/deepagents/TOOL_GUIDES.md`
- `backend/graph/session_manager.py`

### P1-B：SQL Authority 闭环

目标：让 Generator 与 Validator 成为运行时权威，而非 prompt 建议。

1. 固化 QueryIntent / Generation / Receipt / Evidence schema；
2. Execute 强制校验 receipt 和 hash；
3. 技术错误自动回 Generator；
4. 业务歧义才进入 HITL；
5. 持久化 Generation Ledger；
6. 子代理只传结构化数据库 Evidence。

主要文件：

- `backend/tools/database/sql_generate_tool.py`
- `backend/tools/database/sql_validate_tool.py`
- `backend/tools/database/sql_execute_tool.py`
- `backend/graph/database_sql_revision_resume.py`
- 数据库 generation service / guardrails

### P2：子代理合同、限制与可观测性

目标：复杂任务可拆分，但不再黑盒、失真或无限运行。

1. 自动构造 DelegationContract；
2. 强制挂载分析模型上下文和 scoped Skill；
3. 结构化 Result Envelope；
4. timeout、调用上限、idle、cancel 和 blocked 协议；
5. 持久化嵌套事件；
6. 前端可展开进度与工具时间线。

主要文件：

- `backend/graph/deepagents_manager.py`
- subagent middleware / task tool adapter
- stream event 与 Trace schema
- `frontend/src/lib/store.tsx`
- 消息流与工具活动组件

## 12. 测试矩阵

### 12.1 P0-0 止血与权限

- 使用本次 `msg 12` 的验收事件序列回放时，相同 `code_validation` 缺口最多触发一次定向修复，随后明确停止；
- 中间 revision 不评估 `publication_reference`，`artifact_delivery` 不再 pass/fail 抖动；
- `node --check` 和受控 Python Validator exit 0 能形成短期兼容 Evidence，未评审标准显示 `not_evaluated`；
- 同一 `deterministic_checks_completed` 在 UI 原位更新，不重复追加生命周期活动；
- 旧 Session 中语义重复的有效 Grant 在查询和 UI 层只表现为一个，不要求先完成存储迁移；
- Session 联网授权后重建 Docker，curl/Tavily/fetch 不再请求 HITL；
- backend mode、workspace、policy epoch 改变时不错误复用；
- 两个并发联网请求只产生一个有效 Session Grant；
- “仅允许本次”不放行第二个 request；
- 旧重复 Grant 迁移后只显示一个有效记录，完整审计仍可查；
- 撤销 Session Grant 后下一次联网重新触发 HITL。

### 12.2 跨 Run 与 compact

- Run A 使用 AI HOT，Run B 刷新产品报告：B 的输入中没有 AI HOT Skill、curl 或旧 reasoning；
- Run A 完成联网搜索，Run B 追问 A 的结论细节：B 能从用户可见对话、Run Handoff 和按需 Evidence 引用继续回答，不需要回放原始 ToolMessage；
- 追问需要精确原始证据时，只按 Evidence ID 定向读取对应结果，不把 Run A 的全部工具上下文重新注入；
- 同 Goal 的“继续”能恢复 Todo、Artifact hash、SQL generation_id 和未完成项；
- 新 Goal 不继承旧 Skill Activation；
- compact 后控制 ID/hash/Todo 保留，原始命令和 reasoning 不进入新上下文；
- 主动取消后子代理停止，未完成结论不被当成事实。

### 12.3 Skill 与工具门控

- 任务分类漏判时，主 Agent 仍能从动态 Catalog 发现并读取 Skill；
- 未激活时 prompt 不宣称数据库工具已经可调用；
- 读取 Skill 后下一轮 Tool Schema 增加工具，且 unlock receipt 只出现一次；
- 同 Goal revision 延续按策略继承，新 revision 重新评估；
- Skill 文件 hash 变化后旧 Activation/Session State 失效；
- 明确指定未安装 Skill 时进入安装引导，不静默失败。

### 12.4 SQL

- nullable tuple guardrail 失败自动生成 child generation，Agent 不接触 SQL 文本；
- Agent 无法直接把 raw SQL 交给 Execute；
- SQL hash 与 receipt 不匹配时拒绝执行；
- 技术错误自动修订，业务口径冲突才询问用户；
- Backend 重启、HITL 恢复、compact 后 generation/receipt 仍可使用；
- 子代理返回 Evidence ID 和精确数据，不返回可被误抄的摘要数字。

### 12.5 子代理

- 子代理收到父 Goal、Todo、分析模型和语义资产；
- wall timeout、模型调用上限、工具调用上限和 idle timeout 生效；
- 子代理超时后生成 `TimeoutHandoff`，主 Agent 明确收到 `continue_directly` 并继续剩余 Todo；
- 超时后不会把相同 Todo 原样再次派给相同类型子代理；
- 部分 Evidence 在主 Agent 接管后仍可复用，未完成内容不会被误标为完成；
- 子代理提问被转换为 `blocked/question_for_parent`；
- 父 Run 取消会级联取消；
- 前端实时显示阶段、完成数、最近工具和剩余限制；
- 页面刷新后已完成任务不转圈，运行中任务继续显示最新事件；
- 不展示隐藏 reasoning。

### 12.6 验收证据闭环

- HTML/JS Artifact 使用 `python3 validate.py`、`node --check` 或专用 Validator 成功后形成结构化 Validation Receipt；
- 任意脚本 exit 0 不能自动冒充验证，Receipt 必须声明 Validator 类型并绑定 Artifact hash；
- scratch 副本与最终目标 hash 一致时验证可继承，hash 不一致时拒绝；
- 验证脚本本身写入 scratch 不得被误判为新的交付代码产物并无限抬高 `latest_write_at`；
- deterministic blocker 存在时，只返回 blocker；LLM grader criteria 显示 `not_evaluated`；
- 相同 blocker 重复出现时进入明确的 Harness/Validator 协议错误，不生成额外虚假业务缺口；
- `artifact_delivery` 的最终回答引用只在终态检查，中间 revision 不提示；
- “验证闭环” Todo 无 Receipt 时不能完成；Receipt 失效或 Artifact hash 改变后自动重开；
- 数据验收发现某图表口径错误时，只重开对应 Section Todo，不把全部已完成 Todo 清零；
- 最终 UI 区分“Agent 自检完成”“Harness 验收中”“验收通过”。

### 12.7 端到端回归

建立固定回归序列：

1. 在同一 Session 查询 AI 新闻并授权联网；
2. 重建 Docker；
3. 发起产品配置报告刷新并选择分析模型；
4. Agent 激活数据库 Skill；
5. Generator/Validator 完成多个图表查询；
6. 子代理仅承担可并行、结构化的查询批次；
7. 中途 compact、刷新页面，并执行一次“继续”；
8. 验证无重复 HITL、无 AI HOT 污染、无 SQL 手改、无黑盒长转圈；
9. 最终 Artifact hash、数据 Evidence 和自然语言验收说明完整。

### 12.8 模型流式传输恢复

- 首 token 前 Gateway 失败，允许兼容直连 fallback；
- 已输出部分文本后断流，重试成功时原位替换 segment，不重复展示；
- Tool Call 参数流式传输一半时断开，工具不执行；
- 前序工具已经成功、后续 model 节点断流时，不重放前序工具；
- 同一 model 节点连续断流达到上限后，Run 进入可恢复 infrastructure 状态；
- Todo、Artifact Lease、SQL Evidence 和 checkpoint 在恢复后保持一致；
- 用户主动取消时不重试、不自动续跑；
- Gateway read/idle timeout、上游超时和本地网络断开能在 Trace 中区分；
- 传输异常不会生成 completion gap、rubric gap 或消耗业务验收迭代。

### 12.9 外部 Artifact 事务与 scratch

- 中文文件名 stage 后保留可读 basename，同时 lease 中继续使用规范化绝对路径作为身份；
- `commit_external_artifact(lease_id)` 不要求 Agent 回填 lease 已持有的源 hash；
- staged 内容 hash 与最终提交内容 hash 一致，宿主目标并发变化时仍能正确拒绝；
- 验证脚本和其他临时文件默认写入独立 scratch validation 目录，不进入交付目录 lease；
- 目录提交计划发现未声明的 `validate.py` 等临时文件时，返回可执行的排除/移动方案，不允许提交；
- `/scratch` 已存在文件可通过带 `expected_sha256` 的 replace/upsert 原子覆盖，不需要不断换文件名；
- 同一目标混用单文件 lease 与目录 lease 时，事务管理器明确提示 rebase/合并路径，不进入连续冲突。

## 13. 上线、观测与回滚

### 13.1 Feature Flags

建议拆分开关：

- `permission_binding_v2`；
- `run_context_isolation_v2`；
- `scoped_skill_activation_v2`；
- `capability_manifest_v1`；
- `sql_authority_ledger_v1`；
- `subagent_contract_v1`；
- `model_stream_recovery_v1`；
- `harness_hotfix_v1`；
- `external_artifact_txn_v2`。

按 P0 → P1 → P2 灰度，避免一次迁移多个状态模型后难以定位。

### 13.2 必须增加的指标

- 每 Run 重复 HITL 数、Grant dedupe 命中率；
- 容器变化后的 Grant reuse/reject 原因；
- 跨 Run 被过滤的 tool/reasoning 条目数；
- Router 候选、Skill 激活、Tool Schema revision 的时间线；
- unlock 后首次调用对应工具的耗时；
- 子代理 wall time、模型/工具调用数、idle、取消率；
- SQL technical revision 次数、business HITL 次数、receipt 拒绝原因；
- compact/restart 后控制状态恢复成功率；
- 模型流中断率、断流 route/provider、首 token 前后失败分布；
- model 节点重试次数、重试成功率、segment replacement 次数；
- 重试耗尽后的状态恢复成功率，以及误进入业务验收循环的次数；
- completion semantic signature 命中率、P0-0 阻断的重复 revision/跨 Run 续跑次数；
- 跨 Run 追问的 Evidence 定向读取率与原始 ToolMessage 回放率；
- 子代理按任务类型的 P50/P95 wall time、模型调用数、有效 Evidence 产出率和超时接管成功率；
- Artifact patch/commit/rebase 冲突率、临时文件误入提交计划的拦截次数。

日志必须显式标注角色，例如 `task_classifier`、`agent`、`subagent`、`generator`、`validator`、`rubric`、`permission_reviewer`，不再靠调用时间位置推断。

### 13.3 回滚原则

- Grant v2 回滚时保留 v1 审计，不把 superseded Grant 恢复成多个有效记录；
- Context isolation 可回滚展示，但禁止恢复跨 Run reasoning 回放；
- Skill scoped activation 回滚时也不得重新启用 Session 全局 `loaded_skill_ids` 作为工具放行依据；
- SQL Ledger 回滚只允许停止新任务，不允许让 Agent 绕过 Validator 执行 raw SQL；
- Model stream recovery 回滚可关闭自动重试，但必须保留中断分类、状态持久化和“不把截断内容当结果”的安全边界。

## 14. 验收标准

方案完成的判定不是“测试通过”一句话，而是以下用户可感知结果同时成立：

1. 用户已给 Session 联网权限后，容器重建和后续兼容联网工具不再重复询问；
2. AI HOT 等旧 Skill 不会在无关新 Run 中被调用或出现在模型上下文；
3. 模型看到的能力说明与实际 Tool Schema 始终一致，Skill 解锁后能立即继续；
4. 选择分析模型后，主 Agent 和相关子代理都可靠挂载模型上下文；
5. 子代理的任务、阶段、工具、进度、超时和结果可观察、可取消、可恢复；超时后主 Agent 会明确接管并继续推进，而不是停住或重复派单；
6. Agent 不写、不改 SQL；技术问题由 Generator/Validator 自动闭环；
7. 用户只在业务口径确实不明确时看到 HITL，并看到自然语言差异而非长 SQL；
8. 页面刷新、compact、继续、Backend 重启后不丢 Todo、hash、Generation 和 Evidence；
9. 新增机制不依赖硬编码 Skill 名称，未匹配 Skill 的任务仍由通用 Agent 正常完成；
10. 模型流中断能够有限重试或进入可恢复基础设施状态，不丢失任务状态、不重复工具副作用、不污染验收；
11. 任意新增 fail-closed 门都有可执行 repair contract、有限闭合路径、语义熔断和明确降级边界；
12. 外部 Artifact 工具保留可读文件名、临时验证文件不污染交付目录，patch/commit 冲突能够按结构化 next action 恢复。

## 15. 本轮审核建议

审核时优先确认以下决策，确认后再进入代码实施：

1. **Skill Goal 继承边界**：仅同 `goal_id + revision` 自动继承，还是还要求当前 Run 的 Router/Agent 再确认相关性；本方案建议两者都要求，以降低误激活。
2. **Session 全来源联网的稳定边界**：建议绑定 session + workspace + backend mode + policy epoch，不绑定容器实例。
3. **子代理默认限制**：数据库批次首版采用 10 分钟、12 次模型调用、90 秒 idle 的软预算；允许按任务配置提升，但必须在 UI 可见，并根据 P50/P95 实测分布调整。
4. **SQL Ledger 保留期**：建议至少覆盖 Goal 生命周期和审计要求，完成后原始 SQL 留在受控 Trace/Ledger，不进入聊天上下文。
5. **P0 发布方式**：先独立发布 P0-0 止血包；随后权限迁移、上下文隔离、验收 Receipt 和流恢复分别上线、分别观测，不合并成一次不可拆分的大版本。

以上五项确认后，可以按 P0-0、P0-A、P0-B、P0-C、P0-D、P1-A、P1-B、P2 的顺序逐批实现与验收。

## 16. 本轮新增修复项：验收死路与语义漏检

### 16.1 现场结论

`session-9ea2a3e43160` 的报告刷新任务证明主 Agent 直接执行数据库工具的总体效率明显优于子代理绕行：主流程能够完成 SQL 生成、guardrail 修订、执行、外部 Artifact 提交和文件自检。

但“主流程能跑完”不等于“验收链正确”。本轮同时暴露两类问题：

1. **直接阻塞验收的硬 Bug**：HTML/JS 自检已经成功，Harness 却无法把 Python/Node 校验识别为 code validation Evidence；
2. **未被上一轮验收捕获的真实质量问题**：高压平台和后排多媒体屏使用了错误的数据库值/字段口径，Todo 与 Agent 自检均错误地认为已经闭环。

因此不能把结论简化为“只有验收器有问题、数据交付完全正确”。准确判断是：

> `code_validation` 识别过窄，是当前 Run 无法通过 Harness 的直接原因；SQL 语义 Validator 不完整，是错误数据能够带着 completed Todo 进入交付物的根因。

### 16.2 `code_validation` 的确定性死路

本轮已经执行并成功完成：

- `python3 validate.py`：26/26 checks passed；
- Node 语法/结构检查：`BUILD PASSED: 0 errors`；
- 两者 exit code 均为 0。

但 `backend/harness/verification_activations.py::_command_performs_validation` 当前只接受有限的测试、构建和静态检查命令族。Python Validator 和 Node 自定义检查不会激活 code pack；随后 `backend/harness/deterministic_checks.py::_evaluate_code_validation` 找不到 material execute Evidence，只能持续返回：

```text
当前 Run 修改了代码，但尚未成功完成测试、构建或静态检查。
```

对于没有现成 pytest/npm test 项目的 HTML/JS 数据 Artifact，这会形成实际死路。Agent 只能重复自检或猜测其他业务缺口。

修复要求：

- 短期扩充明确、安全的常见校验入口，让当前任务可闭合；
- 长期切换到 P0-C 的 Validation Receipt，不再把命令词法识别当作验证权威；
- 成功 Receipt 必须绑定被验证 Artifact 的最终 hash，不能只证明某条命令 exit 0；
- Validator 脚本属于临时验证资源，不应自动作为新的 material code artifact 触发下一轮验证要求。

### 16.3 失败报告不得制造假缺口

本轮确定性检查因相同 `code_validation` blocker 停滞后直接结束，LLM grader 实际没有执行。但最终报告又将 `task_fulfillment`、`metric_consistency`、`time_scope`、`report_integrity` 记为“验收器未返回必需标准”。

这些标准不是失败，而是尚未评审。错误报告会让 continuation Agent 误以为需要重新检查全部数据与报告内容。

修复要求：

- deterministic gate 阻塞时，报告只包含真实 blocker；
- 未运行标准使用 `passed=null, status=not_evaluated`；
- `failed`、`grader_error`、`not_evaluated`、`infrastructure_error` 必须是不同状态；
- 停滞检测若发现 Agent 已产生成功验证输出但 Evidence 仍未变化，应优先报告 Harness 协议/识别问题，而不是再次要求业务返工；
- revision prompt 附带当前认可的验证协议和目标 Artifact，使 Agent 可以执行一次闭合。

### 16.4 终态条件不得污染中间 revision

`artifact_delivery` 同时检查产物存在、hash、权限和最终回答是否引用路径。其中“最终回答引用”只有在准备发布最终答案时才有意义，在中间 repair 轮次检查会产生必然失败。

修复要求：

- 把 Artifact 存在/hash/权限作为持续确定性检查；
- 把最终回答引用拆成 `publication_reference` 终态标准；
- 只有 Agent 已提交最终候选回答时才检查 publication 标准；
- UI 不把中途缺少最终回答引用展示成业务返工。

### 16.5 Todo 完成必须区分工作完成与验收完成

一般工作 Todo 可以先于最终 Harness 验收完成，但以下 Todo 必须绑定 Evidence：

- 查询/重算类 Todo：绑定 SQL Result Evidence 和语义合同；
- Artifact 写入 Todo：绑定目标 Artifact Receipt；
- 测试/构建 Todo：绑定 Validation Receipt；
- “验证闭环” Todo：绑定最终 Verification Report 或完整 required criteria receipts。

本轮 Agent 只读取 HTML/JS 并确认其中存在预期数字，就发布“验证通过”并关闭“确保所有数据一致”Todo；随后实际又发现：

- 高压平台 SQL 精确匹配 `400V`/`800V`，遗漏主流值 `400V平台`/`800V平台`；
- 后排多媒体屏使用 `后排多媒体屏`，真实字段为 `后排多媒体屏幕数量`。

修复要求：

- `update_todos(complete)` 对声明了完成合同的 Todo 校验 Evidence refs；
- 验收发现局部错误时重开对应 Section Todo，并保留已完成 Todo；
- Verification Receipt 失效或 Artifact hash 变化后自动重开验证 Todo；
- Agent 自检和 Harness 验收采用不同状态与文案，不能提前宣称“验证通过”。

### 16.6 SQL Validator 必须覆盖语义，不只覆盖安全

上一轮 `database_sql_validate` 能通过错误 SQL，是因为它主要校验只读、安全、表范围和已有 guardrail，并未证明数据库枚举值与业务字段匹配。

对于未被语义资产完整注册的 EAV 配置字段，Generator/Validator 必须在生成或放行前获得结构化 Profile：

```json
{
  "field": "高压快充平台",
  "physical_type_name": "高压快充平台",
  "observed_values": [
    {"value": "400V平台", "count": 1993},
    {"value": "800V平台", "count": 972}
  ],
  "matching_rule": "normalized_voltage_prefix",
  "profile_evidence_ref": "evidence-..."
}
```

修复要求：

- Generator 不确定物理字段或枚举值时自动请求 Schema/Profile，而不是猜测；
- Validator 校验 SQL 使用的字段、枚举和匹配规则是否受 Profile/语义资产支持；
- `database_sql_execute` 必须消费匹配 SQL hash 的 Validation Receipt；
- 安全校验通过但语义校验缺失时不得执行；
- 技术语义错误自动反馈给 Generator 生成 child generation，不要求 Agent 手改 SQL；
- 报告图表数据通过 Result Evidence 与目标数据数组建立逐项映射，避免只检查“文件里存在某个数字”。

这里的 fail-closed 必须遵守 §2.5 的闭合性约束，不能复制 `code_validation` 的死路。Profile 缺失时采用有界恢复协议：

1. Generator/Validator 最多执行两次 Schema/Profile 自动发现和规范化修订；
2. 仍无法建立语义 Receipt 时，将状态区分为“物理字段不存在”“枚举稀疏”“业务口径歧义”和“Profile 服务异常”；
3. 业务歧义进入自然语言 HITL，由用户确认候选口径及影响后签发受限 Receipt；
4. 基础设施异常返回可恢复 blocker；允许交付不依赖该指标的降级结果时，必须明确缩小范围并标注未验证项；
5. 未获得 Receipt 或用户明确确认前，不允许静默执行争议 SQL；但也不得在同一缺口上无限生成 child generation。

### 16.7 本轮新增完成标准

本轮修复完成必须同时满足：

1. 同一个 HTML/JS 刷新用例中，Python/Node/专用 Validator 能产生绑定最终 Artifact hash 的 Receipt；
2. Harness 不再因命令未命中白名单而拒绝真实成功的验证；
3. deterministic blocker 阻塞时，最终报告不产生未运行 LLM criteria 的假失败；
4. 中间 revision 不再出现“最终回答未引用产物”；
5. “验证闭环” Todo 在 Receipt 产生前无法完成；
6. 高压平台 `400V平台`/`800V平台` 和后排多媒体屏幕真实字段用例能被 Validator 在执行前识别；
7. continuation 只收到精确 repair contract，不再因验收报告失真而重查无关章节；
8. 最终用户看到的是简洁、自然语言的验收摘要，而不是命令白名单、内部 criterion 或工程化 Harness 细节。

### 16.8 完成条件循环、语义熔断与跨 Run 续跑

本轮 Trace 进一步证明，“发现完成条件缺口，继续处理”不是 Agent 自发生成的过程说明，而是每次
`deterministic_checks_completed(status=needs_revision)` 触发的 Harness 生命周期事件。门控在 Agent
准备自然结束时执行 required criteria；若存在失败项，则注入一条修订 `HumanMessage` 并
`jump_to=model`，迫使 Agent 在同一个 Run 内继续执行。

`msg 12` 的五轮确定性检查如下：

| 轮次 | 失败项 | 结果 |
|---|---|---|
| 1 | `artifact_delivery` + `code_validation` | `needs_revision` |
| 2 | `code_validation` | `needs_revision` |
| 3 | `artifact_delivery` + `code_validation` | `needs_revision` |
| 4 | `code_validation` | `needs_revision` |
| 5 | `code_validation` | `failed` |

这条循环由四层问题共同构成：

1. `code_validation` 对真实成功的 Python/Node 自检产生假阴性，使 required criterion 持续失败；
2. `artifact_delivery` 在中间修订阶段使用 `_last_ai_text` 检查“最终回答引用”，导致失败集合在两种状态间抖动；
3. Run 内停滞签名对完整 failure evaluation 做 hash，包含会随工具调用持续增长的 Evidence，语义相同的缺口也可能被误判为“发生变化”；
4. Run 被判 `verification_failed` 后，Goal 层将其视为可自动续跑原因，在没有新 UserMessage 的情况下创建下一 Run。当前虽受 `max_rounds` 限制，并非数学意义上的无限循环，但不可闭合缺口会被放大到多轮长时间空转。

修复要求：

- 将停滞签名改为稳定的语义签名，只包含 `criterion_id + failure_kind + normalized_gap + missing_requirement`；严禁纳入 Tool Call、Evidence 列表、Artifact Receipt 时间戳等易变字段；
- 同一个 Run 中，同一语义缺口最多允许一次定向修复；再次出现且 criterion 状态未改善时，必须停止并返回明确 blocker；
- 相同 Goal、相同 revision、相同语义缺口已导致上一 Run 失败时，不得再次以普通 `verification_failed` 自动创建下一 Run；只有 `grader_error`、`infrastructure_error` 等可恢复控制面错误可按有限次数自动重试；
- `artifact_delivery` 持续阶段只检查目标覆盖、存在性、权限、内容 hash 和 Receipt；“最终回答引用”拆为终态 `publication_reference`，不得参与中间 failure signature；
- revision prompt 对每个 criterion 返回结构化 repair contract：缺失条件、已观察但未被认可的 Evidence、认可的闭合方式和目标 Artifact；禁止只返回“尚未完成测试、构建或静态检查”；
- 若 Agent 已产生成功校验输出，但 Harness 连续无法形成 Receipt，应将问题升级为 `validator_protocol_error`，不得要求 Agent继续改业务产物；
- `deterministic_checks_completed` 在 UI 中使用稳定活动 ID，例如 `verification-completion-{run_id}`，同一 Run 原位更新状态，不重复追加多条“发现完成条件缺口”；
- UI 终态区分“正在修复完成条件”“完成条件仍未满足”“验收控制异常”和“验收通过”，并隐藏重复的内部门控事件。

本节完成标准：

1. 同一语义缺口在一个 Run 中最多显示一次修复状态和一次终止状态；
2. `artifact_delivery` 不再在中间轮次 pass/fail 抖动；
3. 新增工具 Evidence 不会重置语义停滞计数；
4. `code_validation` 假阴性不会触发跨 Run 自动续跑；
5. 没有新用户输入时，不会因相同业务缺口连续创建多个 Run；
6. Trace 保留每轮原始 criterion 证据，聊天区只显示合并后的用户可理解状态。

### 16.9 外部 Artifact 事务易用性与临时文件隔离

本轮还暴露了四个会持续诱发 Agent 误操作的工具合同问题：

1. stage 时使用 ASCII 正则清洗 basename，中文文件名被剥成 `__2026.html`，降低可读性并增加 patch 定位错误；
2. `commit_external_artifact` 要求 Agent 回填 `expected_source_sha256`，但该值实际是 lease 创建时的源 hash，Agent 容易误填修改后 staged hash；
3. Agent 将 `validate.py` 写入目录 lease 快照，提交计划一度准备把临时脚本加入 `designs/`；虽然并发 manifest 防护最终拦截，但错误反馈没有给出清晰恢复动作；
4. `write_file` 对已存在 scratch 文件拒绝覆盖，长补丁又容易因旧 `old_string` 冲突，迫使 Agent不断换文件名并留下垃圾文件。

修复要求：

- basename 使用 Unicode 规范化和跨平台安全转义，保留 CJK 可读名称；文件身份始终使用 lease 中的规范化绝对目标路径，不依赖 display basename；
- `commit_external_artifact` 以 `lease_id` 作为主要参数，目标路径和源 hash 由服务端 lease 权威提供；旧参数进入兼容期后废弃；
- 可选的 staged hash 只用于确认 Agent 想提交的草稿版本，字段名必须明确为 `expected_staged_sha256`，不能与 source hash 混用；
- 每个 Artifact/Directory lease 显式区分 `delivery_root` 与 `validation_scratch`；Validator 默认只能在后者创建临时资源；
- 目录提交计划对未声明新增文件默认 fail-closed，并返回结构化恢复动作：`exclude_from_plan`、`move_to_validation_scratch` 或 `declare_as_delivery`；
- `/scratch` 提供受版本保护的 `replace_file/upsert_file(expected_sha256)`；不得把宿主外部文件的覆盖权限一并放宽；
- `patch_file` 在写入前预检全部 hunks，一次返回完整冲突列表、附近文本和安全拆分建议；默认仍保持原子性，不提交半截修改；
- 同一目标存在单文件 lease 与目录 lease 时，事务管理器必须检测分叉并给出唯一 rebase 路径，禁止 Agent在两套 lease 间来回尝试；
- 所有冲突错误同时返回机器可读 `error_code`、`conflict_target`、`current_hash`、`expected_hash` 和 `next_action`，供 Agent直接恢复。

本节完成标准：

1. 中文文件名在 stage、patch、commit 和 UI 中保持可识别；
2. Agent 无需记忆或重复填写 lease 的源 hash；
3. 临时验证脚本无法进入未显式声明的交付计划；
4. scratch 脚本可安全原子重写，不产生递增垃圾文件；
5. patch/commit 冲突后 Agent 能按一个结构化 next action 恢复，不连续猜测。

## 17. 授权边界内常规文件工程能力与子代理恢复协议（已实施）

### 17.1 问题定义与第一性原则

最新 `产品配置图表重算` Session 证明，当前 Harness 不是“限制写入过严”这么简单，而是缺少与
用户意图同粒度的合法写入原语。用户明确要求“以现有 HTML/JS 创建 V2”，系统却只能提供：

- 读取完整正文后 `write_file`；
- `inspect_file_version → patch_file`；
- 只读挂载的 `execute_external_directory`；
- 对已存在文件执行需要额外授权的 `delete_file`。

这会把正常的 `copy → patch → validate` 强制展开为大正文穿越模型、删除重建、临时文件和子代理
绕行。修复必须遵守以下原则：

1. **用户授权的是作用域，不是某一种低级工具。** 已授权 exact-directory write 后，该目录内可恢复、
   可审计的常规工程操作不应逐条 HITL。
2. **操作权限与完成证据分离。** “允许复制/覆盖”不等于“产物已正确”；Artifact hash、Validation
   Receipt 和 E2E 仍独立验收。
3. **窄原语优先，通用 shell 后置。** 能用 `copy_file`、原子 replace、Result Export 表达的操作，
   不应迫使 Agent 获得更宽的可写 shell。
4. **安全边界由执行环境保证，不由模型字符串保证。** 禁止 `..` 或绝对路径只能改善错误提示，
   不能作为目录逃逸的主要控制。
5. **失败必须可恢复且有界。** 冲突、HITL、调用上限和验证异常都应返回唯一 `next_action`，不能诱发
   原样重派、删除重写或无限续跑。

### 17.2 最新 Session 的 HITL 审计

本 Session 共观察到 10 条已授予记录和 1 条子代理内未决请求：

| 类别 | 数量 | 判断 | 后续策略 |
|---|---:|---|---|
| 外部文件/目录只读 | 2 | 首次边界确认合理 | Session 内复用 |
| V2 JS 精确写入 | 1 | 目标明确，但可由声明产物合同覆盖 | 对 declared target 自动放行 |
| 原始 HTML 精确写入 | 1 | 错误目标；用户要求创建 V2，不是修改 V1 | 不自动放行，并由目标解析阻止 |
| JS/HTML 只读校验命令 | 4 | 无网络、只读挂载、已注册 Validator | 自动放行并直接生成 Receipt |
| 为覆盖而删除 V2 JS | 1 | 工具缺口诱发的危险绕行 | A2 上线后保留 HITL并增加告警 |
| `cp + validate` 复合命令 | 1 | 在只读挂载中必然无法完成，却仍请求授权 | A3/A1 分流，旧路径确定性拒绝 |
| 创建 V2 HTML 的子代理写请求 | 1（未决） | 用户已声明精确交付目标，不应再次 HITL | C1 继承/上抛；声明目标自动放行 |

自动放行只覆盖下列可证明安全的集合：

- 当前 Goal revision 的 `declared_artifact_targets` 中精确匹配的 create/replace；
- 已存在 exact-directory write Grant 内、由受控原语产生的新增或修改；
- 已授权只读目录中，单一 argv、无管道/重定向/逻辑运算符的注册 Validator；
- 同一 Session 中已授权的 exact-file/exact-directory read。

以下操作继续 HITL 或拒绝：

- delete、递归删除、覆盖未声明目标；
- 扩大到父目录或兄弟目录；
- 网络、包安装、设备/Socket、Git 发布等额外副作用；
- 复合 shell、自定义 Validator、无法形成确定性变更清单的命令；
- 修改 V1 等与“创建 V2”合同不一致的源文件。

### 17.3 A 组：外部文件写路径

#### A3（P0）：`copy_file` 一等原语

先提供最窄、直接命中本次问题的原语：

```text
copy_file(
  source_path,
  target_path,
  expected_source_sha256? = null,
  if_exists = "fail"
) -> {
  source_sha256,
  target_sha256,
  mutation_receipt_id,
  validation_receipt_ids[]
}
```

约束：

- source 必须在 read Grant 内，target 必须是 declared target 或 write Grant 内精确路径；
- 默认 create-only，目标存在即 `conflict`，不得隐式覆盖；
- 文件正文不进入模型上下文；
- 服务端按 bytes 复制、原子落盘、记录 source/target hash；
- JS/JSON/Python 等 code-like 目标在提交前自动运行注册 Validator；
- `.min.js`、vendor bundle 等依赖默认复用，不因“HTML+JS V2”自动派生副本；
- `copy_file` 的 Mutation Receipt 必须被 Artifact Delivery 与后续验证直接识别。

#### A2（P0）：版本保护的原子覆盖

不采用 `delete + write`。建议为写工具增加明确模式，而不是让“创建”和“替换”语义混在一起：

```text
write_file(
  file_path,
  content_or_source_ref,
  mode = "create" | "replace",
  expected_sha256? = null
)
```

- `create`：目标必须不存在；
- `replace`：必须携带 `expected_sha256`，服务端 compare-and-swap；
- 候选内容先验证，验证通过后原子 replace；
- 任一失败保持原文件不变；
- `delete_file` 后紧跟同路径 `write_file` 时发出
  `overwrite_via_delete_deprecated` 控制面告警，但不自动代替用户批准删除。

#### A4（P1）：有界自动 rebase

“自动 rebase”不能偷偷重新解释业务修改。服务端只允许一次机械重放：

- 当前文件变化后，所有 replacement hunk 在新版本中仍唯一匹配；
- hunks 互不重叠；
- 新候选通过相同 Validator；
- 返回 `rebased_from_sha256`、`rebased_to_sha256` 和完整 Mutation Receipt。

任一条件不满足即返回结构化冲突：

```json
{
  "error_code": "patch_rebase_conflict",
  "current_sha256": "sha256:...",
  "failed_hunks": [2],
  "nearby_context_ref": "evidence-...",
  "next_action": "inspect_conflicting_region"
}
```

不得在工具内部调用 LLM，不得自动重写 old/new string，不得重试超过一次。

#### A1（P1，独立开关）：授权目录内可写执行

只有 A3/A2 仍无法覆盖的批量工程操作才进入此通道。exact-directory write Grant 可启用
`execute_external_directory(mode="writable")`，但必须同时满足：

- Docker root filesystem 只读、无网络、无其他宿主挂载，工作目录固定为唯一 bind mount；
- mount namespace 从机制上阻止写出授权根；字符串检查仅作早期拒绝；
- 禁止设备、Socket、setuid、后台进程、二次 mount 和符号链接型交付目标；
- 命令执行前解析能力，执行后扫描目录差异；
- 服务端生成 `added/modified/deleted` 计划，删除项始终二次批准；
- 只提交 declared targets 或 Grant 范围内已确认的变更；
- 失败或超时丢弃容器层草稿，不留下半提交；
- `cp/mv/mkdir` 等低风险命令可在边界内自动执行，未知/复合高风险命令仍 ASK。

因此，A1 不能只实现“禁止 `..`、禁止绝对路径”；真正的安全边界是唯一可写挂载与服务端事务提交。

### 17.4 B 组：通用 `source_ref → file/slot` 物化通道

数据库结果只是结构化数据源的一种，不得把直通能力实现为数据库专用捷径。平台先定义统一的
`SourceReference`，再由数据库、附件、搜索、HTTP/API、知识库和已有 Artifact 分别提供适配器：

```json
{
  "source_ref": "source-...",
  "kind": "database_result | attachment_table | search_result | api_response | artifact | evidence",
  "content_sha256": "sha256:...",
  "media_type": "application/json",
  "schema_ref": "schema-...",
  "size_bytes": 123456,
  "row_count": 337,
  "producer_receipt_ids": ["validation-..."],
  "expires_at": null
}
```

`SourceReference` 必须是服务端持久化、不可变且可寻址的能力引用。模型只接收摘要、schema、抽样和
ref id，不接收完整 payload。每个 producer 负责证明来源真实性；物化层只负责格式转换、权限和目标
提交，不能重新解释业务语义。

#### B1（P0）：统一 Source Materialization

```text
materialize_source_ref(
  source_ref,
  destination = {
    kind: "file",
    target_path,
    mode: "create" | "replace",
    expected_sha256?
  },
  renderer = "identity | json | csv | js_array | text",
  projection?,
  expected_schema_ref?,
  expected_item_count?
)
```

- payload 从 Source Store 流向目标或 scratch，不经过 Agent 消息；
- source hash、renderer version、目标 hash、schema 和数量写入 Materialization Receipt；
- target 仍服从 declared target/write Grant 和 A2 原子提交协议；
- 大结果分页只用于人工/Agent 抽样，不再作为完整写入手段；
- renderer 是平台注册的确定性适配器，不允许模型提交任意转换脚本；
- `identity` 保持原始 bytes；JSON/CSV/JS Array 等转换必须有稳定序列化规则；
- Source 到目标的 Receipt 可被报告一致性、Artifact Delivery 和 E2E 验收直接消费；
- source 过期、hash 不符、schema 不符或数量不符时 fail-closed，且目标保持不变。

各业务工具只负责签发 SourceReference。例如数据库层提供：

```text
database_query_result_source(result_id) -> SourceReference
```

附件表格、搜索结果和 API 响应采用相同模式，不再分别实现
`database_*_export`、`attachment_*_export` 或 `search_*_export`。

#### B2（P1）：统一类型化 Slot Materialization

模板只接受显式类型化 Slot，例如：

```js
bevHeatmapRaw: /*{{SLOT:bev_heatmap|js_array}}*/ [],
```

```text
materialize_source_ref(
  source_ref,
  destination = {
    kind: "slot",
    template_path,
    template_sha256,
    slot_id,
    output_path,
    output_mode: "create" | "replace",
    expected_output_sha256?
  },
  renderer = "js_array"
)
```

Slot 物化必须：

- 精确匹配一个 Slot；
- 校验 source schema 与 slot type；
- 使用服务端 renderer 转义/序列化；
- 填充完成后再执行整文件语法校验；
- Receipt 记录 template hash、source hash、renderer version 和 output hash。

普通裸占位符继续 fail-closed；不得允许任意文本替换冒充类型化 Slot。

#### B3：职责边界

```text
Producer                         Platform                         Consumer
Database / Attachment / API  →  immutable SourceReference  →  file / typed slot
                                      │
                                      ├─ deterministic renderer
                                      ├─ permission check
                                      ├─ atomic commit
                                      └─ Materialization Receipt
```

- Producer 不知道最终文件路径和模板结构；
- Consumer 不知道 SQL、HTTP 或附件解析细节；
- Renderer 不做业务推断，只执行声明式格式转换；
- Harness 只按 Receipt 验证来源、转换和交付闭环；
- Skill/分析模型只定义业务口径和 projection，不获得宿主文件写权限。

#### B4：产品配置查询的完整例子

以“查询 2021–2026 年空气悬架配置率并生成 V2 图表”为例，数据与文件链路为：

```text
database_sql_execute
  → result_id = qr-config（337 行完整结果落 Result Store）
  → database_query_result_source("qr-config")
  → source_ref = source-db-qr-config
       {row_count: 337, schema_ref, content_sha256, producer_receipt_ids}
  → materialize_source_ref(
       source_ref="source-db-qr-config",
       destination={
         kind: "slot",
         template_path: "product-config-charts-v2.js",
         template_sha256: "sha256:...",
         slot_id: "config_rows",
         output_path: "product-config-charts-v2.js",
         output_mode: "replace",
         expected_output_sha256: "sha256:..."
       },
       renderer="js_array",
       projection=["year", "brand", "config_name", "config_rate"],
       expected_item_count=337
     )
  → JS 整文件语法验证
  → Mutation Receipt + Materialization Receipt + Validation Receipt
```

模板中的消费点是唯一且类型化的：

```js
const configRows = /*{{SLOT:config_rows|js_array}}*/ [];
```

模型消息只包含 `source_ref`、列、schema、337 行计数、hash 和 Receipt id，不包含 337 行正文。
物化服务读取 Result Store 的不可变 payload，执行确定性 projection/转义并原子替换 V2 JS。若结果被
篡改、行数不是 337、模板 hash 已变化、Slot 重复或最终 JS 语法不合法，提交前即 fail-closed，旧
V2 文件字节保持不变。同一通道对附件表格、API 响应和已有 Artifact 只需更换 Producer adapter。

### 17.5 C 组：子代理挂起、授权与接管

#### C1（P0）：HITL 上抛而非子代理失败

DelegationContract 必须携带父 Run 的授权上下文快照与 declared targets。子代理调用工具时：

1. 已有父级 Grant 或声明目标可覆盖：直接执行，不再创建卡片；
2. 真正需要新权限：子代理进入 `waiting_for_permission`，请求路由到父 Run 的用户队列；
3. 用户批准后恢复同一个 subagent checkpoint；
4. 用户拒绝后返回 `blocked(permission_denied)`，而不是 `failed(GraphInterrupt)`；
5. 只有主 Agent 确实开始执行剩余 Todo 时才发
   `subagent_fallback_to_parent`。

UI 对应状态为：

- 工具主行：`子代理执行中`；
- 辅助状态：`上下文已挂载` / `等待授权`；
- 失败：`子代理执行未完成`；
- 收到真实 fallback 事件后：`主 Agent 正在接管剩余任务`。

#### C2（P1）：按任务复杂度分配预算并禁止原样重派

- 调用预算由任务类型、Todo 数、预计工具调用和数据规模共同计算；
- 数据导出/模板填充应优先走 B1/B2，不应靠提高模型调用上限解决；
- timeout/limit/permission blocker 产生稳定 delegation fingerprint；
- 同一 fingerprint 不得原样重派；
- parent 接管时只接收 remaining Todo、已完成 Evidence 和唯一 next action；
- 若没有任何子任务完成，UI/Trace 不得使用“已完成后接管”等暗示性文案。

### 17.6 D 组：与既有控制面修复的衔接

- manifest 移除模型可见 `run_id`，保持跨 Run prompt prefix 稳定；
- semantic assets 只注入选择所需白名单投影，完整正文仍由生成器解析；
- informational 追问保持 read-only，不参与验收、不修改 Todo；
- Goal continuation 只在“继续/恢复/完成剩余”等执行意图下创建新 Goal Run；
- session 目录 Grant 可复用，但必须继续绑定 permission policy epoch 与 workspace identity；
- 外部命令验证成功必须形成绑定最终 Artifact hash 的 Receipt，避免重复校验 HITL。

### 17.7 建议实施批次与评审闸门

| 批次 | 内容 | 进入下一批的条件 |
|---|---|---|
| P0-1 | A3、A2、目标解析排除 vendor、声明目标精确授权 | V2 用例不读大正文、不删除、不重复 HITL |
| P0-2 | 通用 SourceReference + B1、C1 | 任意大 payload 不穿模型；子代理权限请求可挂起恢复 |
| P1-1 | B2、A4、C2 | 大数据填充、冲突恢复和预算终止均有结构化 Receipt |
| P1-2 | A1 可写目录执行（Feature Flag） | 威胁模型、事务回滚、逃逸与删除测试全部通过 |
| 配套 | D 组缓存/追问/验证继承 | 跨 Run 命中率与轻量追问 E2E 达标 |

A1 必须单独评审和灰度，不能与 A3 一起默认开启。A3 解决 `V2=copy+patch` 的直接痛点，
A2 消灭 delete+write，二者已经覆盖大多数常规单文件工程操作。

### 17.8 E2E 与对抗式验收矩阵

1. **V2 正常路径**：从 V1 创建 V2 HTML+JS，只产生两个 declared targets；vendor ECharts 继续复用；
   `copy_file → patch → HTML 基础验证` 完成，全程不读取完整源正文；仅当冻结合同明确要求
   E2E 时继续执行 browser E2E。
2. **权限最小化**：首次目录 read 后复用；declared target create/replace 无重复 HITL；delete 和修改 V1
   仍触发 ASK。
3. **覆盖安全**：目标 hash 不匹配时原文件字节不变；禁止 delete+write 绕过。
4. **复制安全**：目标已存在时 create-only copy 失败；source 并发变化时 hash 条件失败。
5. **目录逃逸**：`../`、绝对路径、symlink、hardlink、子进程、重定向、管道、命令替换均不能写出授权根。
6. **事务回滚**：批量操作中任一验证失败，宿主目录零部分提交。
7. **Source 直通**：数据库、附件和 API 的 337 行及 10 万行 payload 均不进入模型输入；
   数量/hash 与各自 Producer Evidence 一致。
8. **Slot 注入**：错误类型、重复 Slot、缺失 Slot、恶意字符串和非法 JS 均 fail-closed。
9. **子代理 HITL**：请求在父 Run 展示，批准后恢复原 checkpoint；拒绝后状态为 blocked，不是假失败。
10. **真实接管**：未发 fallback 事件不显示“主 Agent 接管”；发出后只显示一次且 remaining Todo 正确。
11. **预算终止**：达到调用上限后不原样重派，parent 只执行剩余范围。
12. **最终 E2E**：打开 V2 HTML，2021–2026 selector、图表渲染、数据截止日期、脚本引用和控制台错误
    全部验证，Receipt 绑定最终 HTML/JS hash。

### 17.9 已确认的实施决策

1. **A3/A2 先于 A1**；A1 由
   `harness.terminal.external_directory_writable_enabled=false` 独立灰度，默认关闭。
2. declared target 自动权限只覆盖 create/replace，明确排除 delete；目录计划含删除时必须二次授权。
3. 采用显式 `replace_file(expected_sha256)`，避免给旧 `write_file` 增加含混的双重语义。
4. B1 首批注册 `identity/json/csv/js_array/text` 五种确定性 renderer。
5. 子代理授权挂起复用同一个 parent tool call/subrun checkpoint；父 Run 取消时级联
   `cancelled(parent_cancelled)`，不保留可脱离父 Run 复活的独立后台任务。
6. HTML 使用一等 `validate_html_report`，并由冻结合同参数
   `browser_e2e_required` 选择验证等级：所有 HTML 默认执行结构、重复 ID 和本地资源引用检查；
   只有用户或 Goal 明确要求 E2E/真实浏览器验证时才启动平台固定 adapter 与离线只读 Chromium。
   模型省略 `browser_e2e` 参数，不能自行升降级；兼容旧 Run 的精确命令仅允许单个注册 Validator，或
   `pwd && ls ... && <注册 Validator>` 这一种只读诊断包装。重定向、管道、fallback、
   路径逃逸及用户自定义脚本继续 ASK/拒绝；无 E2E 合同的旧命令直接 DENY，防止绕过参数启动
   Chromium。

### 17.10 实施状态与验证证据

| 能力 | 实施状态 | 主要证据 |
|---|---|---|
| A3 copy、A2 CAS replace、A4 单次机械 rebase | 完成 | `test_host_file_broker.py`、`test_versioned_patch.py` |
| A1 隔离 writable draft + Feature Flag | 完成，默认关闭 | `test_external_directory.py`、`test_tool_execution_pipeline.py` |
| 通用 SourceReference、file/typed-slot materialization | 完成 | `test_source_materialization.py` |
| 数据库 Producer adapter | 完成 | `test_database_query_result_contract.py` |
| 子代理 HITL 上抛、动态预算、禁止原样重派 | 完成 | `create_deep_agent + CompiledSubAgent + InMemorySaver + Command(resume)` 原生恢复 E2E；`test_delegation_control.py` |
| UI 子代理执行/Goal 控制/验收终态引导 | 完成 | Goal 区分生命周期与 Run 执行态：空闲 active 显示“待启动”，运行中可真正暂停，paused/blocked 可恢复并启动；运行中编辑执行 `pause → resume → start` 立即应用新 revision；播放按钮通过结构化 `goal_control_action=start` 直接创建 Goal Run，不伪造用户消息、不经过语义 Router；验收终态提示“继续完成剩余工作”或“重试验收”并持久化；`goalControls.test.ts`、`test_agent_api.py`、`subagentActivity.test.ts`、`verificationActivity.test.ts`、前端 production build |
| D 组缓存、上下文感知 Goal 路由、Todo 即时持久化 | 完成 | informational 保持只读；执行中纠偏读取最近 Run/工具投影，Router 失败时 append-only 修订而非僵硬 `clarify`；`test_context_optimizations.py`、`test_goal_turn_router.py`、Goal/跨 Run E2E |
| 合成 V1→V2 + 337 行配置结果 typed slot + JS/浏览器校验 | 完成 | `test_v2_artifact_pipeline_e2e.py`、Docker Chromium validator |
| HTML 分级一等验证、失败分类与免循环终止 | 完成 | `validate_html_report` 默认生成 `html_structure` Receipt；冻结合同显式要求时生成 `browser_runtime` Receipt；`artifact_failure` 保持 hash-sticky，`invocation_failure`/`infrastructure_failure` 可由同 hash 后续成功覆盖；控制面错误直接终止为基础设施异常 |
| copy authority 与旧写入跨 Run 继承 | 完成 | Broker Mutation Receipt authority 进入 ArtifactReference；旧 `write_file` 仅对同 Goal/修订、精确声明目标和字节相等 hash 做确定性回填 |
| 确定性短路后的模型标准状态 | 完成 | 未运行的 LLM criteria 输出 `passed=null` / `not_evaluated`，不计入失败项或修复循环 |
| 外部目录授权 TOCTOU 防护 | 完成 | Broker 以授权目录 inode + `dirfd/O_NOFOLLOW` 提交；`test_authorized_directory_inode_is_bound_across_validation_and_commit` |
| Goal inspection 的现有证据只读边界 | 完成 | 显式 allowlist；SQL generate/validate/execute/source 均按内部变更处理；`test_goal_inspection_exposes_only_read_only_tools` |

最终综合回归为后端正式测试域 **1168 项通过、1 项按环境开关 skip**；另以
`PUDDINGCLAW_RUN_BROWSER_E2E=1` 单独启用 Docker Chromium 闸门，E2E **1 项通过**。
前端 16 项状态投影测试、TypeScript
检查和 Next.js production build 通过。开启 `PUDDINGCLAW_RUN_BROWSER_E2E=1` 后，Docker
Chromium 在合成临时报告上验证完整 2021–2026 selector、数据截止日期、图表 surface、脚本
引用、控制台/网络错误及最终 HTML/JS/vendor hash；Browser ValidationReceipt 已写入当前
Run 的 VerificationActivation 和 Session Evidence Ledger。未开启环境开关时该用例明确标记
为 skip，不会 false-green。该测试不读取、不修改
`designs/product-configuration-analysis` 中的业务报告。A1 仍保持默认关闭，待灰度环境观察后再单独开启。
