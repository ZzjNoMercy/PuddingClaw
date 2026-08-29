# PuddingClaw Rubric、独立验证与完成控制面演进方案

> 状态：开发分支已实现并通过完整回归，待人工审核
>
> 日期：2026-08-29
>
> 适用范围：普通 Run 质量复核、Goal Rubric 闭环、确定性验证、真实环境复验、DeepAgents grader agent 集成、在线验收观测与发布回归
>
> 分支状态：已在独立分支 `codex/rubric-verification-control-plane` 落地实现。本方案仍作为审核、回归和发布门禁文档；当前验证结果为后端 `2831 passed, 31 skipped`、前端生产构建通过、变更范围 Ruff 与 Python compileall 通过。合并前仍应按 Phase 拆分检查，避免把当前较大的跨层 diff 直接视为可发布状态。
>
> 与既有方案的关系：本文扩展而不推翻 [`docs/goal模式验收执行顺序优化方案.md`](../goal模式验收执行顺序优化方案.md) 已确定的 Goal 完成申请协议；离线 Dataset / Experiment / Evaluator 平台仍以 [`docs/plans/2026-08-02-agent-evaluation-platform-langsmith-first-plan.md`](./2026-08-02-agent-evaluation-platform-langsmith-first-plan.md) 为准。

## 1. 决策摘要

PuddingClaw 应把现有 Rubric Middleware 从“包含完成控制、确定性检查、grader 调用、修正循环和结果聚合的应用级大子类”，演进为三个边界清晰的层次：

```text
Completion / Review Subject
  → Verification Orchestrator
      ├─ Deterministic Verifier
      ├─ Environment Verifier（按策略启用）
      └─ Semantic Rubric Grader（DeepAgents nested grader agent）
  → Report Merger
  → Settlement Controller
```

三层分别回答不同问题：

| 层 | 回答的问题 | 权威主体 |
|---|---|---|
| 执行与提交 | Agent 是否形成了一个可验收的候选结果 | Agent + `GoalCompletionRequest` / Run output boundary |
| 验证与评审 | 冻结候选结果满足了哪些确定性、环境和语义标准 | Verifier / grader，形成不可变记录 |
| 验收结算 | 当前权威状态是否仍允许正式发布并完成 Goal | PuddingClaw 服务端状态机 |

核心原则：

1. **Grader verdict 是验收证据，不是 Goal 状态写权限。**
2. **确定性事实、环境观察和语义判断必须分开产生，再由服务端合并。**
3. **普通 Run 可以使用独立 grader，但默认不进入 Goal 闭环。**
4. **Goal Rubric 继续保持显式完成申请、revision 校验和原子提交。**
5. **所有验证都必须绑定冻结输入；输入变化后旧结果必须失效或变为 stale。**
6. **DeepAgents 负责 nested grader agent 的安全运行，PuddingClaw 负责产品状态机。**

本轮不建议直接在现有 `PuddingClawRubricMiddleware` 上继续堆条件分支，也不建议仅升级 DeepAgents 依赖后现场修补。先冻结协议、抽离状态和补齐回归测试，再迁移 SDK hook。

## 2. 背景与参考

### 2.1 当前 PuddingClaw 基线

当前代码已经实现以下关键边界：

- Goal Agent 必须显式调用 `update_goal(completed=true)`；
- `GoalCompletionRequest` 绑定 `goal_id + objective_revision + run_id + tool_call_id`；
- 完成申请后若继续成功调用其他工具，旧申请失效；
- Rubric Goal 先执行确定性检查，再调用 LLM grader；
- `grader_error`、`infrastructure_error` 与业务缺口分离；
- 最终发布通过 Session 写锁同时提交 request、report、Run、Goal 和 assistant message；
- 普通 Run 默认 `verification_mode=agent`，发生成功 mutation 后可单调升级为 `proportional`；
- 只有显式 Goal 且完成策略为 `rubric` 时，才进入独立 reviewer / repair loop。

当前关键实现：

- `backend/graph/middlewares/goal_completion.py`
- `backend/graph/deepagents_manager.py::PuddingClawRubricMiddleware`
- `backend/harness/coordinators.py::HarnessRunCoordinator.complete_from_final_state`
- `backend/graph/session_manager.py::commit_accepted_completion`
- `backend/harness/models.py::{VerificationMode, GoalCompletionRequest, RubricEvaluationReport}`

### 2.2 DeepAgents 官方变化

DeepAgents 近期通过两步把 rubric grader 变成正式 SDK 集成面；当前 PuddingClaw 已完成对应迁移：

1. `07c0340bc`（`feat(sdk): add rubric grader integration hooks`）为 `RubricMiddleware` 增加：
   - `grader_middleware`；
   - `grader_context_schema`；
   - `grader_state_schema`；
   - `prepare_messages_for_grader`；
   - `build_grader_state`。
2. `a1af029e6`（`refactor(code): use SDK rubric grader hooks`）是 release 之后的 `deepagents-code` 参考实现，展示如何使用正式 hook 并移除应用层 `_grader_input()` 私有覆盖，同时保留 grader 工具预算、路径策略、重试、interrupt、transcript 过滤和稳定 operation ID。

这里必须区分 SDK 能力和参考仓库代码：PuddingClaw 锁定的 `deepagents==0.7.11` 已包含 `07c` 的公开 hook 面；`a1af029e6` 不是当前可导入的 PuddingClaw runtime 模块，也不应作为依赖直接 import。PuddingClaw 只吸收其边界与行为设计，在自身 adapter / manager 中实现。

官方变化表达的边界是：

```text
应用决定：grader 看什么、有哪些工具、预算和运行上下文
SDK 决定：如何安全构造输入、运行 nested agent、解析结构化结果、重试和发事件
```

PuddingClaw 当前锁定 `deepagents==0.7.11`（`requirements.txt`、`pyproject.toml` 与 `uv.lock` 一致），通过公开 hook 传入 grader middleware、context/state schema、transcript projection 和 grader state。外层仍保留 completion request、revision、report merge、RunReviewPolicy 与 atomic settlement；不以 import `deepagents-code` 参考实现替代产品控制面。

### 2.3 两个 PuddingTeams 工程方案的参考意义

参考任务：

- `codex://threads/01a02efe-5bf9-7d51-ae5c-1226cd9bc829`（“分析 LongHorizon-Harness 项目”）
- `codex://threads/01a04dfd-7768-70f1-aa63-be14903b1d03`（“分析多 Agent 协作协议启发”）

可复用的第一性原则：

```text
Submission 是执行者提交的候选事实
Verification 是独立观察形成的验证事实
Acceptance / Settlement 是控制面的最终决策
```

映射到 PuddingClaw：

| PuddingTeams 概念 | PuddingClaw 对应对象 |
|---|---|
| Worker completed / ExecutionReceipt | Agent Run 输出、Tool receipts、subagent receipts |
| WorkItemSubmission | `GoalCompletionRequest` 或普通 Run candidate output |
| VerificationRecord | 确定性检查、环境复验、grader evaluation 的不可变记录 |
| Manager acceptance | `SessionManager.commit_accepted_completion()` |
| Goal completion review | Goal Rubric report + 当前 revision 的原子结算 |

不直接照搬的部分：

- PuddingClaw 不是 Manager/Worker WorkPlan 产品，不引入 WorkItem、Delegation、Manager acceptance 等 Teams 领域对象；
- 普通 Run 不为了验证而伪造 Goal 或 completion request；
- 单 Agent 本地执行可复用现有 Tool Receipt、Artifact、Validation Receipt 和 Trace，不建立平行账本；
- Environment Verifier 是可选验证方法，不要求所有任务都启动第二个具备真实环境权限的 Agent。

## 3. 当前问题

### 3.1 Rubric Middleware 承担过多产品责任

当前 `PuddingClawRubricMiddleware` 同时承担：

- Goal/Run/completion request 门控；
- current-Run transcript 选择；
- 确定性检查；
- LLM grader prompt 和调用；
- 手工 JSON 提取和 Pydantic 校验；
- 确定性 verdict 与 grader verdict 对账；
- 修正消息构造；
- stagnation / budget 控制；
- Goal completion request 状态更新；
- DeepAgents hook 控制流适配。

这使 SDK 升级、普通 Run 复核、独立环境验证和离线重放都必须修改同一个大类。

### 3.2 确定性标准与语义标准仍混在同一 LLM 输入

当前实现把确定性检查 JSON 追加到 grader payload，并要求 LLM 原样采用其结果；之后再通过 `_reconcile_deterministic_grader_response()` 修复 LLM 的漏项或冲突。

这会产生不必要的风险：

- LLM 被要求裁判它无权裁判的确定性事实；
- 相同 criterion 在确定性层和 LLM 层重复表达；
- 需要额外代码修正 LLM 对权威事实的错误复述；
- transcript token 增大；
- report 无法清晰表达每项结果的真正 verifier。

### 3.3 当前 transcript scoping 没有完整过滤控制消息

当前逻辑能找到本 Run 的真实用户边界，但截取后仍可能保留：

- 先前 `rubric_grader` revision message；
- `puddingclaw_completion_gate` 控制消息；
- Goal continuation / completion reminder；
- 其他带内部 `lc_source` 的协议消息。

grader 应评估用户要求、候选输出和允许的执行证据，不应把 Harness 控制指令当作用户标准或完成证据。

### 3.4 Grader 不是完整的受管 Agent

当前 `_grade/_agrade` 直接调用 plain chat model 并手工解析 JSON，未完整获得新版 SDK 提供的：

- nested grader middleware；
- typed runtime context/state；
- provider/tool structured-output 策略；
- criterion coverage retry；
- grader tool budget 和 HITL；
- grader trace metadata；
-稳定 operation ID；
- SDK input sanitization 扩展顺序。

### 3.5 普通 Run 缺少独立但轻量的质量复核

现有 `agent/proportional` 主要表达主 Agent 自查和 mutation 后的相称验证，不启动独立 grader。用户无法在不创建 Goal 的情况下选择：

- 只记录、不阻塞的 shadow review；
- 当前回答发布前的一次独立复核；
- 对某次历史回答手动“验证结果”。

直接给所有普通 Run 打开 Goal Rubric 又会导致成本、延迟和自动修正行为过重。

### 3.6 验证记录的新鲜度不足

现有 report 能绑定 Run、contract 和 Goal revision，但缺少统一冻结快照来表达：

- grader 实际看到了哪一版 transcript；
- 使用了哪些 EvidenceRef；
- Artifact 当时的 hash；
- workspace / external resource 当时的 fingerprint；
- 使用了哪个 grader model、prompt、policy 和工具集合；
- 验证后相关状态是否发生变化。

因此在线验收结果不容易安全重放，也难以直接转为发布回归样本。

## 4. 目标与非目标

### 4.1 目标

1. 保持 Goal 标准验收与 Rubric 验收的既有产品语义。
2. 允许普通 Run 显式选择 shadow 或 one-shot review，但不创建 Goal。
3. 把确定性、环境和语义验证拆成独立 VerificationRecord。
4. 建立不可变 EvaluationInputSnapshot 和 freshness / staleness 规则。
5. 使用 DeepAgents 正式 grader hooks 运行 nested grader agent。
6. 减少对 DeepAgents 私有方法的覆盖，并为剩余语义差异建立明确 Adapter。
7. 让在线验证事件可直接进入 Trace、Analytics 和离线回归。
8. 保持最终 Goal 完成权威在 PuddingClaw 服务端原子结算入口。

### 4.2 非目标

- 不把所有普通聊天默认升级为独立 grader。
- 不让 grader 直接修改 Goal、Run 或 completion request。
- 不把离线 Dataset / Experiment 平台合并进在线 completion state machine。
- 不引入多 grader panel 或多数投票。
- 不默认给 grader 写 workspace、发布、购买或外部 mutation 权限。
- 不为了统一概念而删除现有 Evidence Ledger、Artifact、Validation Receipt 或 Trace。
- 不在本轮引入 PuddingTeams 的 WorkPlan / WorkItem / Delegation 领域模型。

## 5. 产品模式

### 5.1 普通 Run 默认模式

```text
RunReviewPolicy = off
VerificationMode = agent / proportional
```

- 不启动独立 grader；
- 主 Agent 按任务风险自行测试、构建和读回产物；
- mutation 可继续触发 `agent → proportional`；
- Run `completed` 只表示本次执行结束并返回结果，不承诺跨 Run Goal 已完成。

### 5.2 普通 Run Shadow Review

```text
RunReviewPolicy = shadow
```

- 回答照常发布；
- 发布后从冻结快照启动一次独立 grader；
- 结果只写入 Trace、Analytics、RunReviewReport 和回归候选队列；
- 不修改用户已收到的回答；
- 不改变 Run outcome；
- 不创建 Goal 或 completion request；
- grader error 不影响主请求成功状态。

主要用途：

- 统计误拒、漏放和 grader 稳定性；
- 建立真实生产回归样本；
- 比较 grader model / prompt 版本；
- 在正式阻塞用户前验证 one-shot 策略质量。

### 5.3 普通 Run One-shot Review

```text
RunReviewPolicy = blocking_one_shot
```

- 主 Agent 形成 candidate response 后运行一次独立复核；
- 不创建 Goal；
- 默认不进入无限或多轮自动修正；
- `satisfied`：正式发布；
- `needs_revision`：发布回答并附“独立复核发现风险”，不自动改写候选；
- `grader_error/infrastructure_error`：不得伪装成业务失败；默认降级发布并显示复核未完成，严格配置可要求用户重试；
- 结果绑定当前 Run，不产生 Goal 完成承诺。

当前实现把 blocking one-shot 保持为实验能力，并采用 request-scoped explicit-only 约束：只有普通 Run 的单次请求明确选择 `blocking_one_shot` 时才可进入；全局配置只允许 `off/shadow`，Goal、task classifier、mutation 比例或 grader verdict 均不得隐式升级。它只执行一次有界复核裁决，不自动修正、不创建 Goal、不产生 `GoalCompletionRequest`。默认仍为 `off`；正式扩大启用范围前须满足第 17 节的 shadow 数据门槛。

### 5.4 Goal Standard

保持现有语义：

```text
Agent 显式 completion request
→ state-safety 检查
→ 原子完成并发布
```

- 不生成空 Rubric report；
- 不启动独立 grader；
- Agent 自查可以很强，但仍属于执行阶段；
- completion request 是唯一候选提交边界。

### 5.5 Goal Rubric

保持为实验性最高验收等级：

```text
Agent 显式 completion request
→ 冻结 EvaluationInputSnapshot
→ deterministic verification
→ optional environment verification
→ semantic rubric grader
→ report merge
→ revision / failure / accepted proposal
→ SessionManager 原子结算
```

- 只在有效 completion request 上启动；
- 允许有限修正循环；
- 当前 Goal revision、Run 和 request 必须持续一致；
- grader `satisfied` 只是 accepted proposal；
- 只有 Settlement Controller 可以完成 Goal 并发布正式终态。

## 6. 目标领域模型

### 6.1 `RunReviewPolicy`

建议新增独立枚举，不继续膨胀 `VerificationMode`：

```python
class RunReviewPolicy(StrEnum):
    OFF = "off"
    SHADOW = "shadow"
    BLOCKING_ONE_SHOT = "blocking_one_shot"
```

原因：

- `VerificationMode` 当前表示 Run 自查/Goal reviewer ownership；
- `RunReviewPolicy` 表示普通 Run 是否额外启动独立复核；
- 两者混为一个枚举会让 `proportional`、`shadow` 和 `rubric` 的控制权语义再次耦合。

### 6.2 `EvaluationSubject`

```python
class EvaluationSubjectKind(StrEnum):
    RUN_OUTPUT = "run_output"
    GOAL_COMPLETION_REQUEST = "goal_completion_request"

class EvaluationSubject(BaseModel):
    kind: EvaluationSubjectKind
    session_id: str
    run_id: str
    query_id: str
    goal_id: str | None = None
    goal_revision: int | None = None
    completion_request_id: str | None = None
```

约束：

- `RUN_OUTPUT` 不得包含 Goal 完成权威；
- `GOAL_COMPLETION_REQUEST` 必须绑定 goal、revision 和 request；
- subject 创建后不可换绑另一个 Run 或 revision。

### 6.3 `EvaluationInputSnapshot`

```python
class EvaluationInputSnapshot(BaseModel):
    schema_version: str
    snapshot_id: str
    subject: EvaluationSubject
    contract_id: str | None = None
    contract_version: str | None = None
    contract_hash: str
    transcript_projection_version: str
    transcript_projection: dict
    transcript_digest: str
    candidate_message_id: str | None = None
    candidate_content_digest: str
    candidate_tool_calls_digest: str
    evidence_refs: list[EvidenceRef]
    evidence_digest: str
    artifact_fingerprints: list[dict]
    workspace_fingerprint: str | None = None
    grader_policy_version: str
    grader_policy_hash: str
    permission_epoch: str
    created_at: float
```

快照会在 Session JSON 中保存有界的 `transcript_projection` 和稳定引用，同时保存 candidate、evidence、policy、permission 的 digest；不复制无限 transcript、Tool output 或大文件正文。digest 不是唯一数据：投影本身必须可重建、可审计，并参与 canonical `snapshot_id`。

必须冻结：

- 用户目标和适用 contract；
- candidate final response；
- grader 可见 transcript 投影；
- candidate message identity、内容 digest 和 tool-call digest；
- EvidenceRef 集合；
- Artifact hash / mtime / revision；
- Goal revision 和 completion request；
- grader policy / prompt / tool allowlist 版本。

### 6.4 `VerificationRecord`

```python
class VerificationMethod(StrEnum):
    DETERMINISTIC = "deterministic"
    ENVIRONMENT = "environment"
    SEMANTIC_RUBRIC = "semantic_rubric"

class VerificationRecordStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    SATISFIED = "satisfied"
    NEEDS_REVISION = "needs_revision"
    FAILED = "failed"
    NOT_EVALUATED = "not_evaluated"
    GRADER_ERROR = "grader_error"
    INFRASTRUCTURE_ERROR = "infrastructure_error"
    STALE = "stale"

class VerificationRecord(BaseModel):
    verification_id: str
    snapshot_id: str
    method: VerificationMethod
    status: VerificationRecordStatus
    criteria: list[CriterionEvaluation]
    evidence_refs: list[EvidenceRef]
    operation_id: str
    attempt_no: int
    input_digest: str
    verifier_model: str | None = None
    verifier_policy_version: str
    verifier_policy_hash: str
    result_digest: str
    tool_receipt_ids: list[str]
    latency_ms: int | None = None
    usage: dict = {}
    error_kind: str | None = None
    started_at: float
    completed_at: float | None = None
```

约束：

- record 只能评价对应 snapshot；
- verifier 不能写 `accepted_for_goal_revision`；
- record 创建后不可覆盖，重试使用新的 `attempt_no` / record；
- `stale` 不回写或删除旧 record，而是在 Session JSON 的 `verification_invalidations` 中追加不可变失效 marker；读取方把存在有效 marker 的 record 视为不可用于结算；
-工具输出必须引用 Tool Receipt，不把 grader 自述当作环境证据。

### 6.5 持久化、操作队列与重启恢复

当前实现把控制面数据写入 Session JSON，而不是以 Trace/Analytics 或未绑定的 sidecar 作为权威：

- `evaluation_snapshots` 保存冻结输入及有界 projection；
- `verification_records` 保存不可变结果；
- `verification_operations` 保存 `pending/running/completed`、`attempt_no`、owner/lease 和结果引用；
- `verification_invalidations` 只追加失效 marker，不修改历史 record；
- `verification_proposals` 与 `run_review_reports` 保存同一 snapshot 绑定的派生结果。

普通 Run review 的后台任务不是 SSE 流的所有权。主 Run 完成并发布后，shadow review 可在独立任务中继续；进程重启时，服务启动流程扫描全部 Session JSON（不依赖 sidebar projection），发现 ordinary Run 的 `pending/running` review operation 后重新排队。operation lease 过期可被重新 claim；重复请求复用未完成 operation，已完成 operation 才创建新的 attempt。恢复不会重新发布 Run，也不会把普通 Run 变成 Goal。

### 6.6 Report 合并

`RubricEvaluationReport` 应成为合并投影，而不是唯一原始记录：

```text
RubricEvaluationReport
  ← Deterministic VerificationRecord
  ← Environment VerificationRecord（可选）
  ← Semantic Rubric VerificationRecord
```

合并规则：

1. 每个 criterion 只允许一个权威 verifier kind；
2. deterministic criterion 不发送给 LLM grader；
3. environment criterion 只有绑定当前 fingerprint 的记录才有效；
4. semantic grader 不得覆盖 deterministic/environment verdict；
5. required criterion 缺失时 fail closed；
6. `grader_error` 不生成业务 gap；
7. `infrastructure_error` 不冒充任务不合格；
8. report 可引用多个 record，但必须全部属于同一 snapshot。

## 7. Grader Agent 设计

### 7.1 角色

Semantic Rubric Grader 是当前 Agent Graph 内的 nested agent，不是新的用户会话，也不拥有 Goal 写权限。

它负责：

- 阅读冻结的用户目标、candidate response 和允许的 transcript；
- 评审仅属于 `semantic_rubric` 的 criteria；
- 必要时使用受限只读工具补充证据；
- 返回结构化逐项 verdict；
- 发出可观测生命周期事件。

它不负责：

- 修改源码或业务产物；
-接受 completion request；
- 修改 Goal revision；
- 决定 Run/Goal 终态；
- 发布最终 assistant message；
- 推翻确定性或环境 verifier 的结论。

### 7.2 Transcript Projection

新增纯函数或独立组件：

```python
prepare_messages_for_grader(messages, subject, snapshot_context)
```

处理顺序固定为：

1. 按 current Run / current Goal revision 确定边界；
2. 必要时用 durable objective 重建缺失的起始消息；
3. 移除内部控制消息；
4. 移除不属于当前 subject 的历史用户任务；
5. 保留候选回答及必要的可审计执行消息；
6. 返回新列表，不修改 outer Agent state；
7. 交给 DeepAgents SDK 构造和 sanitization grader payload。

初始内部消息 denylist 至少包含：

- `rubric_grader`；
- `puddingclaw_completion_gate`；
- `puddingclaw_goal_completion_protocol`；
- Goal continuation / auto-continue 控制消息；
- model response recovery 控制消息；
- 仅用于 UI/Trace 的系统投影消息。

denylist 应基于稳定 `lc_source` / typed metadata，不使用自然语言前缀判断。

### 7.3 Grader State 和 Operation ID

建议 nested grader state 至少包含：

```python
class PuddingClawGraderState(AgentState[GraderResponse]):
    evaluation_snapshot_id: NotRequired[str]
    verification_operation_id: NotRequired[str]
    completion_request_id: NotRequired[str]
```

operation ID 建议为：

```text
sha256(snapshot_id + method + attempt)
```

而不是仅依赖进程内 iteration。这样重启、重试和离线重放仍能稳定关联，工具预算也不会跨验证轮次串账。

### 7.4 Grader Middleware

初始 grader middleware：

1. `GraderTraceContextMiddleware`
   - 写入 model、snapshot、operation、contract 元数据；
   - 不负责控制结果。
2. `GraderToolBoundaryMiddleware`
   - 默认只允许显式注册的 read-only verifier tools；
   - 拒绝写文件、发布、删除、购买和外部 mutation。
3. `GraderToolBudgetMiddleware`
   - 按 operation ID 统计；
   - 搜索、文件读取、浏览器观察分别限额。
4. `GraderContextBudgetMiddleware`
   - 限制累计工具结果和 prompt 大小；
   - 大结果只通过 EvidenceRef / ArtifactRef 读取。
5. `GraderPermissionMiddleware`
   - 外部系统默认只读；
   - 无法证明只读的能力默认不挂载；
   - 需要用户授权的观察动作可 HITL，但不得借验证扩大主任务权限。

grader transport retry 只能重试模型调用；可被 grader 调用的工具必须只读或幂等，避免重试重复副作用。

## 8. Environment Verifier

### 8.1 与 Rubric Grader 的区别

```text
Rubric Grader：判断语义标准是否满足
Environment Verifier：重新观察真实环境并生成事实记录
```

两者可以共享 nested-agent runtime，但必须保留不同 method、policy、tool allowlist 和记录类型。

例如：

- “报告是否清楚回答用户问题”属于 semantic rubric；
- “pytest 是否通过”属于 deterministic/environment；
- “网页布局是否与目标一致”属于 environment + semantic；
- “生产发布是否成功”必须重新查询外部状态，且发布动作本身不属于 verifier 权限。

### 8.2 初始验证 profile

```text
none
deterministic_only
independent_evidence_review
environment_verified
```

推荐映射：

| 任务 | 初始 profile |
|---|---|
| 普通解释、闲聊 | none |
| 简单代码修改 | deterministic_only |
| 重要分析、来源报告 | independent_evidence_review |
| 代码、网页、文档、设计最终交付 | environment_verified |
| 生产发布、购买、删除 | environment_verified + HITL settlement |

profile 只能由用户显式选择、Goal 冻结策略或受信任的确定性 task profile 选择。LLM grader 自身无权升级权限。

当前实现的 Environment Verifier 是 receipt-only 边界，而不是一组可由 grader 任意调用的环境工具：

- verifier 只接受不可变 `EnvironmentObservation`，不接受应用回调、写文件/发布/删除等 mutation capability；
- observation 必须声明并由调用边界强制 `read_only_enforced=true`，且绑定当前 snapshot/input digest；
- `environment_verified` 只有在存在独立的 observation 与 Tool Receipt 时才成立；主 Agent 或 grader 的“我已测试/已发布”自述不能替代回执；
- 缺少回执、只读约束或 fingerprint/freshness 绑定时，应报告 infrastructure error，而不是把它降级成业务缺口或验证通过；
- 生产发布、购买、删除等动作仍由外层执行/HITL settlement 负责，Verifier 只能在动作后重新观察结果。

因此，当前 profile 名称表达验证策略，不代表已经为每类任务接通真实生产能力；新增 capability adapter 必须沿用 receipt-only、默认只读和权限不升级的边界。

### 8.3 Freshness

以下变化会使相关 environment record 变为 stale：

- artifact hash 变化；
-验证覆盖的文件在记录后再次写入；
- workspace baseline / revision 变化；
- external resource version / ETag 变化；
- Goal objective revision 变化；
- completion request 被 invalidated；
- contract version 变化。

仅 UI 内容变化或无关文件变化是否导致 stale，由 record 中声明的 observation scope 决定，不能默认让整个 workspace 的任意变化使所有记录失效。

## 9. Settlement Controller

### 9.1 为什么 grader 不能直接完成 Goal

grader 只能看到冻结的评估输入，无法独立保证评估完成时系统权威状态仍未变化。

典型竞争条件：

```text
Goal revision 1：生成 CSV
→ Agent 提交 completion request R1
→ grader 开始评估 snapshot S1
→ 用户把 Goal 修改为 revision 2：再增加图表
→ grader 对 S1 返回 satisfied
```

若 grader 直接写 Goal，revision 1 的 verdict 会错误完成 revision 2。

另一个场景：

```text
Agent 提交 R1
→ 又调用工具修改产物
→ R1 被 invalidated
→ 旧 grader 稍后返回 satisfied
```

旧结果必须保留为历史记录，但不能用于完成 Goal。

### 9.2 原子结算前置条件

`commit_accepted_completion()` 继续作为唯一 Goal 正式发布入口，并在 Session 写锁内检查：

这意味着最终裁决仍由外层控制面完成：它检查 completion request 是否存在且未失效、确定性/环境证据是否来自当前冻结 snapshot、Goal revision 是否仍匹配。snapshot、records 和 proposal 先作为不可变 staged verification 数据持久化；最终写锁重新校验它们，并原子提交 request 状态、Run、Goal 和 assistant message。grader 或 environment receipt 只能提供证据，不能自行提交最终状态。

1. Session / query / Run identity 一致；
2. Run 是当前 Goal 的 current run；
3. Goal 仍 active；
4. Goal 没有 pending control request；
5. Run 和 Goal revision 与 request 一致；
6. completion request 仍为 `requested/evaluating`；
7. request policy 与 Goal completion policy 一致；
8. 没有未提交 external mutation lease；
9. Rubric 模式存在同 snapshot、当前 revision 的 satisfied report；
10. 所有 required verification record 都有效且非 stale；
11. candidate final message 已形成；
12. request 状态、Run、Goal 和 message 在同一写入中提交；已 staged 的 records/report 必须在锁内重新验证 snapshot、candidate、revision、freshness 和引用完整性。

该 staged proposal 设计允许进程在验证完成、最终结算前崩溃：重试只能复用同一冻结 snapshot 的不可变 records/proposal，或在输入变化后生成新 snapshot；旧 proposal 不能跨 candidate/revision 结算。这样避免重复 grader 调用，同时保持最终发布事务单一。

普通 Run review 不进入该入口；它只提交 RunReviewReport 和观测事件。

## 10. DeepAgents 升级与 Adapter 边界

### 10.1 不能直接升级的原因

当前安装版本为 `deepagents==0.7.11`，已包含 `07c0340bc` 的公开 Rubric hook。升级迁移已落地，但仍需要精确 lock 和 drift guard；`a1af029e6` 仅作为 release 后 `deepagents-code` 的不可导入参考实现。新版 Rubric SDK 调用形态与历史 PuddingClaw 私有覆盖至少存在以下风险：

- 新版 `_grade(..., context=...)` 与当前 `_grade(state, iteration)` 不兼容；
- 新版 `_compose_update(state, evaluation)` 与当前额外接收 `graded_result` 的覆盖不兼容；
- `_build_grader_payload`、coverage retry、structured-output diagnostics 已继续演进；
- Hook 顺序和 `after_agent` reverse stack order 会影响 Trace、cost tracking 和 terminal response suppression；
- `RubricMiddleware` 仍为 beta API，必须锁定精确版本并增加 drift guard。

### 10.2 目标 Adapter

建议新增独立模块，而不是继续扩张 `deepagents_manager.py`：

```text
backend/graph/verification/
  models.py
  snapshots.py
  transcript_projection.py
  orchestrator.py
  run_review.py
  deterministic.py
  environment.py
  report_merger.py
  records.py
  events.py
```

DeepAgents adapter 当前位于 `deepagents_manager.py` 的 Goal middleware 与 `run_review.py` 的普通 Run orchestrator；它只负责：

- 构造 SDK `RubricMiddleware` / nested grader；
- 传入 grader middleware、context/state schema；
- 调用 transcript projection；
- 注入 operation state；
- 将 SDK evaluation 转换成内部 `VerificationRecord`。

它不负责 Goal 状态机、completion request 持久化、contract 编译或原子提交。

### 10.3 计划删除的私有覆盖

在 SDK hook 迁移完成后，目标删除或迁出：

- `_plain_grader_model`；
- `_response_text`；
- `_parse_grader_response`；
- 应用层 `_grader_input` 等价逻辑；
- 直接 plain model `_grade/_agrade`；
- 在 prompt 中要求 LLM 复制 deterministic verdict 的逻辑；
- `_reconcile_deterministic_grader_response` 的大部分职责。

预计仍需保留在 PuddingClaw 外层的语义：

- completion request 门控；
- deterministic-first；
- current Goal revision 检查；
- PuddingClaw 自有 repair / stagnation / model-call budget；
- report merge；
- atomic settlement。

如果新版 SDK 没有稳定的 decision-policy 扩展点，不继续覆盖其 `_compose_update` 私有方法；优先：

1. 把 SDK Rubric 当作一次 grader engine 使用，由外层 orchestrator 决定是否 `jump_to=model`；或
2. 向 DeepAgents 上游提交公开 `evaluation_decision_policy` / `compose_evaluation_update` hook。

## 11. 可观测事件协议

新增带 `schema_version` 的稳定事件，不以 UI 文案作为协议：

```text
verification.snapshot.created
verification.deterministic.started
verification.deterministic.completed
verification.environment.started
verification.environment.completed
verification.grader.started
verification.grader.tool.completed
verification.grader.completed
verification.record.stale
verification.revision.requested
verification.settlement.started
verification.settlement.committed
verification.settlement.rejected
```

统一 envelope：

```json
{
  "schema_version": "1",
  "event_id": "...",
  "event_type": "verification.grader.completed",
  "timestamp": 0,
  "session_id": "...",
  "query_id": "...",
  "run_id": "...",
  "goal_id": null,
  "goal_revision": null,
  "completion_request_id": null,
  "snapshot_id": "...",
  "verification_id": "...",
  "operation_id": "...",
  "status": "satisfied",
  "method": "semantic_rubric",
  "model": "...",
  "policy_version": "...",
  "latency_ms": 0,
  "usage": {},
  "error_kind": null
}
```

事件用途：

- Trace 时间线；
- UI activity 投影；
- 成本、延迟、错误率和修复率统计；
- shadow 与 blocking verdict 对比；
- 生产失败样本进入离线 Dataset；
- grader model / prompt 发布回归。

事件不是完成权威。analytics 丢失不得改变 Goal 状态，事件重放也不得重新执行 settlement。

## 12. 状态迁移

### 12.1 普通 Shadow Review

```text
Run running
→ candidate response
→ Run completed + message published
→ snapshot created
→ grader running
→ RunReviewReport persisted
```

无论 grader 结果如何，Run outcome 不回写为失败。

shadow 的 `snapshot`、`operation` 和 `RunReviewReport` 均持久化到 Session JSON。SSE 只发布开始/进度事件，不承载后台任务的生命周期；断线、进程退出或 Session 重启后，仍以 durable operation 的 `pending/running` 状态恢复，不能依赖客户端重放事件来补做 review。

### 12.2 普通 Blocking One-shot

```text
Run running
→ candidate response withheld
→ review running
   ├─ satisfied → Run completed + publish
   ├─ needs_revision → publish-with-warning
   └─ control error → degraded publish + “复核未完成”
```

不创建 Goal，不进入 GoalCompletionRequest 状态机。

### 12.3 Goal Rubric

```text
Goal active / Run running
→ completion request requested
→ Run evaluating / request evaluating
→ snapshot frozen
→ deterministic verification
   ├─ needs_revision → request needs_revision / jump_to model
   ├─ infrastructure_error → stop, Goal remains active
   └─ satisfied
→ optional environment verification
→ semantic grader
→ merged report
   ├─ needs_revision → bounded repair
   ├─ grader_error → Goal remains active, retry review available
   └─ satisfied proposal
→ atomic settlement
→ request accepted + Run completed + Goal completed + message published
```

不新增 `GoalStatus=reviewing`。评审中状态继续由 active Goal、Run `evaluating` 和 request `evaluating` 派生。

## 13. 分阶段实施

### Phase 0：兼容性冻结与基线测试（✅ 已完成）

目标：不改变行为，建立升级护栏。

- 锁定 DeepAgents `0.7.11` 依赖与行为测试；
- 为所有已覆盖私有方法记录签名和调用契约；
- 增加 middleware hook 顺序 snapshot；
- 增加 grader event、cost tracking、stream suppression 回归；
- 增加当前 Goal standard / rubric 端到端 golden tests；
- 建立 DeepAgents 目标版本的兼容性 spike，不修改生产 lock。

验收：现有行为无变化；升级风险以自动测试表达。

### Phase 1：Snapshot、Record 与事件协议（✅ 已完成）

目标：先建立新数据边界，继续调用旧 grader。

- 新增 `EvaluationSubject`；
- 新增 `EvaluationInputSnapshot`；
- 新增 `VerificationRecord`；
- 把现有 deterministic 和 rubric evaluation 投影成 record；
- 增加稳定事件 envelope；
- report merger 先以兼容模式读取旧 state 和新 record；
- UI 仍维持当前表现。

验收：相同 Goal 结果与当前一致，新记录可重放、可追踪。

### Phase 2：确定性与语义 criteria 分离（✅ 已完成）

目标：LLM 不再复制确定性结论。

- contract 编译时生成 verifier-owned criterion 集；
- deterministic verifier 只处理自己的 criteria；
- semantic rubric 只包含 LLM criteria；
- report merger 合并并 fail closed；
- 删除 deterministic JSON 注入和大部分 reconciliation；
- 保持现有 Goal completion UX。

验收：grader 无 deterministic criteria 时仍可完成；故意返回冲突结果不能覆盖确定性事实。

### Phase 3：DeepAgents 正式 Hook 迁移（✅ 已完成）

目标：使用 nested grader agent，去除脆弱私有覆盖。

- 将 DeepAgents 精确锁定到已验证版本 `0.7.11`（已包含 `07c` hooks）；
- 接入 grader middleware/context/state hooks；
- 接入 transcript projection 和 build grader state；
- 使用 SDK structured output 和 coverage retry；
- 保留 PuddingClaw 外层 decision/settlement；
- 删除 plain model JSON 解析路径；
- 增加 SDK drift guard。

说明：`a1af029e6` 的 `deepagents-code` 代码不作为 importable runtime dependency；只复用其迁移设计。

验收：Goal Rubric 行为等价；成本、事件、interrupt 和 grader error 分类不回归。

### Phase 4：普通 Run Shadow Review（✅ 已完成）

目标：先积累真实数据，不影响用户。

- 新增 `RunReviewPolicy.SHADOW`；
- 支持全局实验开关和单次显式选择；
- shadow 结果进入 Trace、Analytics 和 RunReviewReport；
- 不修改 Run outcome；
- 不创建 Goal；
- 支持用户手动“验证此回答”。

实现包含 Session JSON durable operation、lease/attempt、RunReviewReport，以及服务启动后的 pending review 恢复。

验收：shadow 开关关闭时零额外调用；开启时主响应延迟不受 grader 影响。

### Phase 5：Environment Verifier（✅ 已完成基础边界）

目标：对需要真实状态保证的交付形成独立环境事实。

- 定义 verifier capability profile；
- 以 receipt-only `EnvironmentObservation` 建立只读能力边界；
- 代码/Web/文档 profile 的真实 capability adapter 仍须逐项接入并提供独立回执；
- 生成 fingerprint-bound VerificationRecord；
- 相关产物变化后自动 stale；
- 不允许 verifier 修改业务产物。

验收：Worker/主 Agent 声称“测试通过”但没有独立回执时，严格 profile 不得通过。

### Phase 6：Blocking One-shot Review（✅ 已实现实验能力）

目标：在 shadow 数据证明可靠后，为普通 Run 提供显式发布前复核。

- 仅允许普通 Run 的单次请求显式开启；全局配置不得设为 blocking，也不得隐式升级；
- 恰好一次 grader 裁决，不自动修正候选；
- 明确 grader error 降级策略；
- UI 固定标注“质量复核 · 实验性”；
- 不自动升级为 Goal。

验收：普通 Run 不产生 Goal 状态；关闭后完全恢复默认路径。当前仍以实验性能力发布，是否扩大默认覆盖范围由第 17 节门槛决定。

## 14. 测试矩阵

### 14.1 Goal 完成权威

- 无 completion request，即使 grader satisfied 也不能完成 Goal；
- request revision 与当前 Goal revision 不一致时拒绝；
- request 后成功调用工具会 invalidated；
- invalidated request 的迟到 grader 结果不能结算；
- current run 已变化时拒绝旧 Run；
- pending Goal control request 时拒绝完成；
- staged external mutation 存在时拒绝完成；
- standard completion 不创建 Rubric report；
- rubric completion 必须有当前 snapshot 的 satisfied report；
-原子写失败时 message、Run、Goal、request 均不得部分提交。

### 14.2 Verifier 权威

- semantic grader 不能覆盖 deterministic failure；
- environment record 与 artifact hash 不一致时 stale；
- required criterion 缺失时 fail closed；
- contract 外 criterion 不被接受；
-重复 criterion 不静默去重为通过；
- grader error 与 task gap 分开；
- infrastructure error 不消耗业务修正轮次；
-工具调用记录绑定正确 operation ID；
- transport retry 不重复非幂等工具。
- 旧 `RubricEvaluationReport` 的 legacy projection 可读但不能作为当前 VerificationRecord；
- legacy projection 或 grader verdict 不能绕过 `commit_accepted_completion()` 完成 Goal；
- 旧 record 失效后原 record 内容不变，只新增 append-only invalidation marker；
- snapshot 保存的 transcript projection、candidate identity 和 digest 可在重启后重建并校验。

### 14.3 Transcript 安全

- grader 看不到 completion reminder；
- grader 看不到上一轮 rubric revision 控制消息；
- grader 看不到其他 Run 的用户请求；
- current objective 在 summary 后可重建；
- projection 不修改 outer state；
- untrusted transcript 不能伪造 snapshot/system context delimiter；
- grader state callback 不能覆盖 SDK-owned `messages`。

### 14.4 普通 Run Review

- `off` 不产生额外模型调用；
- `shadow` 不阻塞发布、不改变 Run outcome；
- shadow grader error 不污染主回答；
- one-shot 不创建 Goal 或 completion request；
- one-shot 只执行一次裁决，不自动改写回答或进入修正循环；
- blocking one-shot 只有显式 policy 才能启动，普通 Run/Goal/mutation 不得隐式升级；
- shadow 的后台 operation 在 SSE 断线或进程重启后可从 Session JSON 恢复，且不会重复发布主回答；
- 重复手动验证复用 pending/running operation，完成后重试才产生新的 attempt；
- `EnvironmentObservation` 缺少 read-only enforcement 或独立 Tool Receipt 时不得形成 environment_verified；
-用户关闭实验开关后 active Goal Rubric 策略不被静默修改。

### 14.5 DeepAgents 升级

- sync / async grader 等价；
- `GraphBubbleUp` 保持控制流，不记录为 grader error；
- coverage retry 仍生效；
- grader context schema 正确传播；
- hook 顺序、cost drain、Trace span 和 SSE event 不回归；
- SDK 默认 `max_iterations` 漂移会触发测试失败。

## 15. 数据迁移与兼容

1. 旧 Session 不回填伪造的 VerificationRecord。
2. 旧 `RubricEvaluationReport` 保持可读，标记 `source_format=legacy_state_projection`；legacy 只作为兼容读取/展示投影，不能写入新 authority，也不能 settle Goal 或普通 Run review。
3. 新 report 优先引用 snapshot/record；不存在时走旧解析 Adapter，但 Adapter 只读且不能调用 settlement。
4. `GoalCompletionRequest` 现有字段保持兼容，可新增 `evaluation_snapshot_id`，不改变 request identity。
5. active Rubric Goal 在升级期间继续使用创建时冻结的 completion policy 和 contract。
6. 不因全局实验开关变化而静默升级或降级 active Goal。
7. DeepAgents 依赖使用精确 lock；不得只修改 `pyproject.toml` 的下限而不更新 lock 和 requirements。
8. 新旧双读期间保留 legacy projection 路径；它永远不具备 settle 权限，只有在兼容窗口结束后才可删除。

## 16. UI 与产品文案

### 普通 Run

- 默认不显示验收 UI；
- shadow review 只在详情中显示“后台质量复核”；
-手动入口：“验证此回答”；
- blocking one-shot 固定标注“质量复核 · 实验性”；
- grader error 显示“复核未完成”，不显示“任务失败”。

### Goal

- 标准验收：`Agent 完成声明`；
- Rubric：`Rubric 闭环验收 · 实验性`；
- 环境复验：显示实际运行的检查与证据时间；
- stale：显示“产物已变化，需要重新验证”；
- Goal completed 只在原子结算成功后显示。

内部 iteration、operation ID、criterion ID 和 tool 名默认收进 Trace/详情，不进入最终用户回答。

## 17. 指标与发布门槛

上线 shadow 后至少观察：

- grader invocation rate；
- satisfied / needs_revision / error 分布；
- 同 snapshot 重评一致率；
- 人工推翻率；
- shadow 发现真实缺陷的比例；
- criterion 缺项率；
-平均/尾部延迟；
- token 与工具成本；
- environment record stale 率；
- Rubric 修正后实际通过率；
- 用户在标准完成后重开 Goal 的比例；
- Rubric 误拒后用户关闭验收的比例。

Blocking one-shot 的启用门槛：

1. shadow 样本达到审核设定的最低数量；
2. 误拒率和 grader error 率低于审核阈值；
3. grader 模型 / prompt 版本已进入离线回归；
4. `off` 可即时回退；
5. 不影响 Goal atomic settlement；
6. 有明确成本和延迟展示。

## 18. 分支与合并策略

这个改动应单独拉分支，原因不是单纯“代码多”，而是它同时跨越：

- DeepAgents 依赖升级；
- Middleware hook 与 Agent loop；
-持久化 schema；
- Goal / Run / request 状态机；
- Trace / Analytics 事件协议；
- SSE / UI；
-普通 Run 产品策略；
-可能的真实环境工具权限。

建议：

```text
main
 └─ codex/rubric-verification-control-plane
      ├─ P0 compatibility tests
      ├─ P1 snapshot + records + events
      ├─ P2 criterion ownership split
      ├─ P3 DeepAgents hooks migration
      ├─ P4 shadow review
      ├─ P5 environment verifier
      └─ P6 blocking one-shot
```

执行要求：

- 当前实现已在该独立分支完成；合并前仍按 Phase 做审查、测试和必要的 commit 拆分；
- 每个 Phase 独立 commit，能单独测试和回滚；
- 不在同一个 commit 中同时升级依赖、改持久化 schema 和开放 UI；
- P0-P3 可以在同一 feature branch 连续完成，但每阶段必须保持测试可运行；
- P4-P6 使用 feature flag，默认关闭；
- 若周期较长，优先把 P0/P1/P2 小步合回 `main`，避免长期巨型分支漂移；
- 不与无关 Harness 重构或前端改版混合。

因此不是建议长期维护一个一次性大爆炸分支，而是：**使用独立主题分支隔离风险，并按可验证阶段小步合并。**

## 19. 审核决策点

以下决策已经在开发分支落地；仍需审核确认的是发布门槛和未接通的真实 capability：

- [x] 普通 Run 支持 shadow、手动“验证此回答”和实验性 blocking one-shot；blocking 仅 explicit-only，不由系统隐式升级；
- [x] Goal Rubric 保持原有 completion request / revision / atomic settlement 权威；普通 Run review 不进入 Goal 状态机；
- [x] Environment Verifier 先落实 receipt-only、默认只读边界；真实 capability adapter 按 profile 逐项接入；
- [x] grader error 在 blocking one-shot 下按显式 fallback 策略处理，不伪装成业务失败；
- [x] VerificationRecord、operation、proposal、report 和 invalidation 进入 Session JSON；失效采用 append-only marker；
- [x] snapshot 保存有界 transcript projection，同时保存 canonical digest；
- [x] DeepAgents 初始目标版本锁定 `0.7.11`，其中已包含 `07c` hooks；`a1af029e6` 仅作不可 import 的 release 后参考实现；
- [ ] 是否向 DeepAgents 上游推动正式 evaluation decision-policy hook；
- [ ] shadow 进入 blocking 的最低样本数和误拒率阈值。

## 20. 推荐审核结论

建议批准以下方向：

1. `GoalCompletionRequest`、current revision 和 atomic settlement 保持现有权威地位；
2. grader agent 只产生 VerificationRecord / proposal，不直接完成 Goal；
3. 确定性、环境、语义 verifier 分权；
4. 使用 DeepAgents `0.7.11` 正式 grader hooks，吸收 `a1af029e6` 的设计但不 import `deepagents-code`；
5. 普通 Run 已具备 shadow、手动 review 和 explicit-only blocking one-shot，blocking 仍标记实验性并受 shadow 门槛约束；
6. Environment Verifier 以 receipt-only、默认只读 capability 边界渐进扩展；
7. Session JSON 作为 durable authority，operation 支持 lease/attempt 和重启恢复，record stale 只追加 invalidation；
8. 当前改动保持在独立主题分支，按 Phase 小步审查、测试、提交和合并。

不建议批准以下做法：

- 给所有普通 Run 默认开启 grader；
- 让 grader verdict 直接写 Goal completed；
- 一次性升级 DeepAgents 并重写全部验收链；
- 继续让 LLM 复制确定性 verdict；
- 用 Trace/Analytics 事件替代 Session 权威状态；
- 没有 fingerprint/freshness 绑定就宣称“真实环境已验证”。
