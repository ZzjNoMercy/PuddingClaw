# Goal 完成协议与 Rubric 验收分层方案

> 状态：待审核
>
> 日期：2026-07-26
>
> 决策摘要：Goal 默认采用 Agent 显式提交完成；完成申请是独立控制对象；标准与 Rubric 共用同一完成协议，只在“谁有权接受完成申请”上分叉。Rubric 验收保留为可选的最高级别验收，并在前端明确标注为实验性功能。
>
> 本文取代本文件此前的“所有 Goal Run 自然停止后强制进入 Readiness + Rubric”方案。实现前以本文为当前设计基线。

## 1. 结论

PuddingClaw 的 Goal 保留跨 Run 持续执行能力，但完成协议改为：

```text
Agent 完成任务并执行相称的自查
→ update_goal(completed=true)
→ Harness 幂等持久化 GoalCompletionRequest
→ Agent 生成最终回复并自然停止
→ 根据 Goal.completion_policy 决定申请的裁决方式
   ├─ standard：仅通过状态安全检查后接受
   └─ rubric：经确定性检查和独立 Rubric 通过后接受
→ 接受后原子提交 Run 终态、Goal 终态和最终消息
```

产品只提供两种完成方式：

1. **标准验收**：默认。Agent 对完成负责，Harness 只保证状态与协议安全。
2. **Rubric 验收（实验性）**：可选的最高级别验收。Harness 使用确定性检查、结构化证据和独立模型 Rubric 重新裁判完成声明。

不再设置第三档“增强验收”。测试多少、是否构建、是否运行静态检查属于 Agent 根据任务风险选择的自查力度，不构成第三种 Harness 模式。

本方案的核心不是重写 Goal 状态机，而是将“完成申请”从 `RubricEvaluationReport / GoalVerificationDecision` 中抽出，成为标准与 Rubric 共用的下层协议。

## 2. “标准”不等于“少验证”

以下完成摘要属于标准验收：

```text
相关矩阵共 232 passed，Ruff 和 git diff --check 通过。
```

这里的测试可以很全面，但仍由主 Agent 在执行阶段主动运行。只要没有在 Agent 提交完成后再启动独立 grader、重新聚合证据并自动驳回，就属于标准验收。

因此应区分：

- **Agent 自查强度**：由任务风险决定，可以很强；
- **Harness 验收级别**：由是否启用独立 Rubric 裁判决定。

`git diff --check` 只能证明补丁格式没有明显空白错误，不能单独证明功能正确；测试、构建、运行时行为和产物读回仍应由 Agent 按任务需要执行并如实汇报。

## 3. 外部框架源码复核

本次复核基线：

| 项目 | 本地提交 | 完成协议 | 结论 |
|---|---:|---|---|
| Codex CLI | `61a44880a8` | `update_goal({"status":"complete"})` | Agent 按提示完成审计后显式提交；工具处理器本身不再运行独立 grader |
| Grok Build | `b189869` | `update_goal(completed=true)` | 支持 classifier 开关；关闭时直接完成，开启时进入 skeptic 验证 |
| Claude Code 历史源码 | `4b9d30f` | `TaskUpdate(status="completed")` | Agent 主动标记；可配置 `TaskCompleted` hook 阻断，verification agent 仅在特定功能开关和场景下提示 |
| PuddingClaw | `c224a38` + 当前工作区 | Agent 自然停止触发 | 当前显式 Goal 默认绑定确定性检查、修复回跳、LLM Rubric 和跨 Run 聚合，完成链过重 |

关键源码：

- Codex：`/Users/pet/Code/AI/Agent/源码合集/codex/codex-rs/ext/goal/src/spec.rs`
- Codex 完成审计：`/Users/pet/Code/AI/Agent/源码合集/codex/codex-rs/ext/goal/templates/goals/continuation.md`
- Grok Build 工具：`/Users/pet/Code/AI/Agent/源码合集/grok-build/crates/codegen/xai-grok-tools/src/implementations/grok_build/update_goal/mod.rs`
- Grok Build 完成分流：`/Users/pet/Code/AI/Agent/源码合集/grok-build/crates/codegen/xai-grok-shell/src/session/acp_session_impl/goal.rs`
- Claude Code：`/Users/pet/Code/AI/Agent/源码合集/claude-code/src/tools/TaskUpdateTool/TaskUpdateTool.ts`
- PuddingClaw 当前完成判定：`backend/harness/coordinators.py`
- PuddingClaw 当前 Rubric 入口：`backend/graph/deepagents_manager.py`

### 3.1 采用与不采用

采用：

- Grok Build 的统一完成表达：`update_goal(completed=true)`；
- Codex/Grok/Claude 的共同原则：完成必须由 Agent 显式提交；
- Grok Build 的可选高级 classifier 思路；
- Claude Code 的可选 completion hook 思路；
- 工具响应必须反映真实状态变化，不能在 Harness 尚未处理时虚报成功。

不采用：

- 不把 Grok Build 的多 skeptic panel 复制为默认能力；
- 不把 Codex 较长的完成审计提示机械复制进每轮上下文；
- 不把 Claude Code 的 Todo/Task 状态直接等同于整个 Goal 完成；
- 不删除 PuddingClaw 已有 Rubric、Evidence Ledger 和 deterministic verifier。

### 3.2 PuddingClaw 当前源码结论

对当前代码与现有控制面测试的复核结论是：**标准模式可以复用现有状态机骨架，但不能通过“关闭 Rubric”直接实现。**

已适配、可复用的部分：

- `RunStatus` 与 `GoalStatus` 的非法迁移保护；
- `goal_id + objective_revision + run_id` 身份关系；
- Goal 的跨 Run attach/release、暂停、恢复、取消和预算；
- `RUNNING → EVALUATING → RUNNING/COMPLETED` 对 Rubric 修复回跳的支持；
- Session JSON 写锁、Evidence Ledger、Validation Receipt 和外部 mutation lease。

尚未适配、必须解耦的部分：

1. `HarnessRunCoordinator.start_run()` 目前将所有显式 Goal Run 映射为 `VerificationMode.GOAL`，“是否 Goal”与“是否 Rubric”仍是同一个开关。
2. `PuddingClawRubricMiddleware` 已能在 `verification_mode != goal` 时旁路，这一机制可保留。
3. `complete_from_final_state()` 目前无论是否真正运行 Rubric，都构造 `RubricEvaluationReport`，再通过 `GoalVerificationDecision` 推进 Goal 终态。
4. 当 `verification_enabled=false` 且 Goal 自然停止时，当前实现会生成 `NOT_REQUIRED` report 并直接转为 `ACHIEVED`；这不等于新方案的标准验收，因为它缺少 `update_goal(completed=true)` 显式申请。
5. `commit_accepted_completion()` 目前强制要求 accepted report 和 accepted Goal decision，标准模式不能靠伪造空 `NOT_REQUIRED` report 绕过。
6. 当前流式结束路径会将所有 `RUNNING` Run 转为 `EVALUATING`，新实现中 `EVALUATING` 只应属于需要独立验收的完成申请。
7. 当前还没有模型可见的 `update_goal` 工具实现。

已运行现有关键测试：

```text
test_run_verification_mode_is_owned_by_explicit_goal_state
test_goal_without_verification_keeps_decision_and_run_acceptance_consistent
test_non_goal_run_does_not_enter_completion_repair_loop

3 passed
```

它们确认了现状，但新协议实现后前两个测试的预期必须重写。

### 3.3 第一性原理：四类事实不得混同

```text
Goal
= 用户希望跨 Run 持续达成的目标

Run
= 一次具体执行尝试

GoalCompletionRequest
= Agent 针对当前 Goal revision 提交的完成声明

RubricEvaluationReport
= 仅在 rubric 策略下，独立验收器对某次完成声明的裁决证据
```

四者的权威边界：

- Run 成功结束不等于 Goal 完成；
- Agent 自然停止不等于已提交完成申请；
- 完成申请被 Harness 接收不等于已原子发布最终回复；
- 标准模式接受完成申请不需要 Rubric report；
- Rubric report 不能替代 Goal revision、Run identity 和状态安全检查。

因此新架构必须是：

```text
Goal 生命周期状态机
        ↑
GoalCompletionRequest 完成申请状态机
        ↑
standard state-safety 或 rubric verification
```

## 4. 统一工具协议

### 4.1 模型可见调用

统一使用：

```text
update_goal(completed=true)
```

实际工具输入：

```json
{
  "completed": true,
  "message": "可选的简短完成摘要"
}
```

同时保留阻塞上报：

```json
{
  "blocked_reason": "连续尝试后仍无法继续的真实阻塞",
  "message": "已尝试的方法和所需外部条件"
}
```

### 4.2 工具规则

- `completed` 只接受 `true`；进度更新时省略该字段，不发送 `false`。
- 只有当前 Goal 的主 Agent 可以提交完成；review/subagent 不能结束 Goal。
- 调用绑定 `goal_id + objective_revision + run_id + tool_call_id`。
- 同一个 `tool_call_id` 幂等；重复投递只返回同一 completion request，不得重复提交终态或重复启动 Rubric。
- Goal 不存在、非 active、revision 已过期或调用来自错误 Run 时拒绝。
- `update_goal` 必须是一次独立的主 Agent Tool Call；不允许与仍在执行的兄弟 Tool Call 共同构成完成声明。
- 完成申请后只允许 Agent 生成最终回复；若又发生实质性工具操作或变更，当前申请失效，Agent 必须重新调用 `update_goal(completed=true)`。
- 工具结果只能确认“完成申请已持久化”或返回同步协议拒绝；不得在最终消息尚未形成时声称 Goal 已完成。
- Agent 自然停止但未调用 `update_goal(completed=true)` 时，只结束当前 Run，Goal 保持 active。
- 自然停止本身不触发 Rubric，也不作为“未完成就自动开始下一 Run”的通用信号。新 Run 只由用户显式继续、Goal revision、可恢复的控制面错误或明确的 Run budget boundary 启动。

### 4.3 工具 ack 与最终提交是两个时点

`update_goal` 是模型回合中的 Tool Call。该 Tool 返回时，Agent 还没有生成 Tool Result 之后的最终 Assistant Message，因此工具不可能同时原子提交 Goal、Run 和最终消息。

第一阶段，工具返回真实的申请状态：

```text
# standard
Completion request recorded. Finish the final response.

# rubric
Completion request recorded for Rubric verification. Finish the candidate final response.
```

第二阶段，Agent 自然停止后，Harness 已拥有最终回复，才能根据申请状态执行原子终结事务。只有该事务成功后，API/SSE/UI 才可声明 Goal 已完成。

### 4.4 状态词汇

对外统一：

- Goal 终态：`completed`
- 完成动作：`completed=true`
- 前端文案：`已完成`

Rubric 仍使用独立判定词汇：

- `satisfied`
- `needs_revision`
- `failed`
- `verification_incomplete`
- `grader_error`
- `infrastructure_error`

本方案只面向新 Session 和新 Goal。实现时直接统一模型、API、SSE 和 UI 的完成词汇，不为旧 Session 的 `achieved` 状态增加读取映射或数据迁移。

## 5. 标准验收

### 5.1 定位

标准验收是默认完成方式。Agent 对任务质量和自查负责，Harness 不重新判断“用户目标是否真的完成”，只验证完成请求能否安全地改变状态。

### 5.2 唯一硬阻断项

调用 `update_goal(completed=true)` 时先检查身份与协议安全，Agent 自然停止后再检查终结安全。两阶段合计仅允许以下硬阻断：

1. 当前 Goal 存在且为 active；
2. 调用绑定当前 objective revision 和合法 Run；
3. 调用来自主 Agent，且 `update_goal` 是该 Assistant Message 中唯一的 Tool Call；
4. 完成申请后没有新的实质性 Tool Call 或 mutation 使申请失效；
5. 没有仍在执行的 Tool Call；
6. 没有未决 HITL、权限审批或外部 mutation；
7. 当前 Run 未处于 cancelled、system failed 或 budget exceeded；
8. 完成申请、Goal 终态、Run 终态和最终消息可以原子持久化。

上述检查只处理状态安全，不重新评审任务质量。

### 5.3 不得作为标准模式硬门槛

以下信息可以形成提示或完成证据，但不能阻止标准完成：

- 是否运行测试、构建、Ruff 或 `git diff --check`；
- 是否存在未提交修改；
- 是否拥有完整 Evidence Ledger；
- 所有 Todo 是否都有结构化证据；
- 是否调用过 Web、SQL、RAG、代码或浏览器工具；
- 是否存在旧 Run 遗留但已失效的 pending Todo；
- 最终文本是否满足 LLM grader 的主观判断。

若项目确实需要强制命令，可后续增加可选的 `GoalCompleted` hook。Hook 属于项目策略，默认不配置，不能继续向通用 Harness 内堆叠隐式硬规则。

### 5.4 完成提交后的结果

标准路径中，`REQUESTED` 是 completion request 的子状态，不是 `GoalStatus`：

```text
Goal ACTIVE + Run RUNNING
→ update_goal(completed=true)
→ CompletionRequest REQUESTED
→ Agent 生成最终回复并自然停止
→ STATE_SAFETY_CHECK
   ├─ rejected
   │    → CompletionRequest REJECTED
   │    → Goal 仍 ACTIVE
   │    → 返回明确协议错误，不发布伪终态
   └─ passed
        → CompletionRequest ACCEPTED
        → 原子提交 Run COMPLETED + Goal COMPLETED + 最终消息
```

若 Agent 自然停止但不存在 completion request：

```text
Run COMPLETED
Goal ACTIVE
```

这两个 `COMPLETED` 不矛盾：Run 终态只表示本次执行已成功结束，不自动表示跨 Run Goal 已完成。

标准验收成功后，前端只展示“已完成”和 Agent 已实际执行的自查摘要，不生成空 `RubricEvaluationReport`，不生成 `GoalVerificationDecision`，不展示 VerificationCard。原子发布权威来自已接受的 `GoalCompletionRequest`。

### 5.5 标准模式的 Prompt 责任

标准模式不生成或持久化结构化 Rubric，但必须有稳定的完成审计 Prompt。它的职责是让同一 Agent 从原始 Goal 中动态推导临时检查清单，而不是产生第二套验收对象。

建议在 Goal 创建/续跑上下文和 `update_goal` 工具描述中注入同一原则：

```text
在调用 update_goal(completed=true) 前：
1. 从原始 Goal、用户明确要求和引用文件中确定所有必需结果，不得缩小范围。
2. 检查当前真实状态，不得用计划、意图或总结代替已实现结果。
3. 执行与改动风险相称的验证，只报告实际执行过的检查。
4. 若存在未完成要求、失败检查或证据不足，继续工作并保持 Goal active。
5. 只有所有必需项完成且没有已知遗留工作时，调用 update_goal(completed=true)。
6. 完成申请记录后只生成最终回复；若还需要调用工具或修改产物，先继续工作，并在最后重新提交完成申请。
```

不强制 Agent 将临时检查清单输出给用户，不为它分配 criterion id，不绑定 Evidence Ledger，也不计分。一旦需要结构化、持久化和独立裁判，就应进入 Rubric 验收。

## 6. Rubric 验收（实验性）

### 6.1 定位

Rubric 验收是可选的最高级别验收，用于重要报告、关键代码交付、复杂跨 Run 任务或用户明确要求独立复核的场景。

它保留当前已建设的能力：

- declared/effective verification contract；
- deterministic checks；
- Evidence Ledger 与跨 Run evidence；
- LLM Rubric grader；
- 结构化 criteria、gaps 和 report；
- 有限的定向修复。

Rubric 验收不是默认 Goal 语义，也不能由任务关键词自动升级。

### 6.2 唯一触发点

Rubric 不再因 terminal AIMessage、自然停止或 `after_agent` 本身自动触发。`after_agent/aafter_agent` 可继续作为执行位置，但只有读到当前 Run 的有效 completion request 时才可运行验收。

唯一触发条件：

```text
Goal.completion_policy == rubric
AND Agent 调用 update_goal(completed=true)
```

这次调用的语义是“持久化一次 Rubric 完成申请”，不是立即宣告 Goal 已完成。Agent 在 Tool Result 之后生成 candidate final response，随后 Harness 进入验收。

### 6.3 状态机

```text
Goal ACTIVE + Run RUNNING
→ update_goal(completed=true)
→ CompletionRequest REQUESTED
→ Agent 生成 candidate final response 并自然停止
→ CompletionRequest EVALUATING + Run EVALUATING
   → deterministic checks
   → LLM Rubric
   ├─ satisfied
   │    → CompletionRequest ACCEPTED
   │    → 原子提交 Run COMPLETED + Goal COMPLETED
   │       + candidate final response + accepted report
   ├─ needs_revision
   │    → CompletionRequest NEEDS_REVISION + Run RUNNING
   │    → 返回结构化 gaps
   │    → 同 Run 最多执行有限修复
   │    → 修复后必须重新 update_goal(completed=true)
   │    → Goal 始终保持 ACTIVE，直到新申请被接受
   └─ grader/control/infrastructure error
        → CompletionRequest REJECTED
        → 显示“Rubric 验收器异常”
        → Goal 保持 ACTIVE
        → 不伪装成业务缺口，不消耗业务修复次数
```

`REQUESTED / EVALUATING / NEEDS_REVISION / ACCEPTED / REJECTED / INVALIDATED` 属于 `GoalCompletionRequest.status`，不新增到 `GoalStatus`。Goal 的持久业务状态在验收期间仍是 `ACTIVE`；前端“Rubric 验收中”由 active Goal 与 evaluating request 派生。

### 6.4 修复与停止规则

- 默认最多自动修复 1 次；设置中允许调整，但必须有硬上限。修复后的新验收必须绑定新 completion request，不得静默复用旧申请。
- 相同 gap 指纹连续不变时立即停止自动修复，不跨 Run 无限重复。
- Rubric 驳回不自动取消 Goal。
- 验收器或基础设施异常不写入业务 `gaps`，改写入 `control_notices`。
- 控制面异常不消耗 Goal round，也不自动转 `blocked`。
- 达到 Rubric 尝试上限后，Goal 保持 active 并停止自动续跑；用户可以继续、修改目标、重试验收或关闭 Rubric 验收。
- 用户暂停、取消、修改 objective revision 时，正在进行的验收结果不得接受为新 revision 的完成证据。

### 6.5 合同与证据冻结

- `completion_policy` 在 Goal 创建时冻结；修改全局设置只影响新 Goal。
- Rubric contract 按 objective revision 冻结。
- `update_goal(completed=true)` 时生成本次 acceptance snapshot，包含 contract version、Goal revision、有效 evidence refs、artifact identity 和支持 Run。
- 驳回后的新证据可以进入下一次提交，但不能回写篡改已经形成的旧报告。

## 7. 前端方案

### 7.1 输入区

Goal Mode 开启时展示完成方式：

```text
完成方式
● 标准验收（推荐）
○ Rubric 验收  [实验性]
```

Rubric 验收说明：

> 使用确定性检查、结构化证据和独立模型 Rubric 复核完成结果。更慢、成本更高，且仍可能误判；建议仅用于重要交付。

选择结果随 Goal 创建请求一起发送并冻结。active Goal 的 chip 或详情卡必须显示当前完成方式，避免用户误以为设置修改会改变正在运行的 Goal。

### 7.2 设置页

现有“Goal Run Rubric 验收”改为：

```text
Rubric 验收  [实验性]
默认：关闭
```

关闭时折叠所有 Rubric 参数。开启后才展示：

- 最大 Rubric 尝试次数；
- 相同缺口最多自动修复次数；
- 自定义 Rubric 规则；
- grader 模型；
- 预计的额外延迟和模型调用说明。

“实验性”必须是可见 badge，不能只埋在帮助文本中。

### 7.3 GoalCard 与 VerificationCard

标准验收：

- GoalCard 显示 active / 已完成 / 暂停 / 阻塞；
- 完成后可显示 Agent 自查摘要，例如测试数量和构建结果；
- 不创建空 Rubric Report；
- 不显示 VerificationCard。

Rubric 验收：

- GoalCard 显示“Rubric 验收中”“待修正”“已完成”；
- VerificationCard 展示 criteria、evidence、gaps、attempt 和 control notice；
- verifier 异常显示为系统异常，不显示为“任务不合格”；
- 前端不自动打开面板，不在聊天正文反复插入修复过程。

## 8. 后端落点

### 8.1 新增或调整的数据

建议新增：

```text
GoalCompletionPolicy = "standard" | "rubric"
GoalCompletionRequest
  - request_id
  - goal_id
  - objective_revision
  - run_id
  - tool_call_id
  - completed
  - policy
  - status: requested | evaluating | needs_revision | accepted | rejected | invalidated
  - message
  - invalidated_reason
  - acceptance_snapshot_id
  - verification_report_id
  - requested_at
  - decided_at
```

`GoalCompletionRequest` 既是幂等记录，也是标准与 Rubric 共用的发布权威。它必须保留每次申请，不能只在 Goal 上覆盖一个可变对象。

Goal 增加：

```text
completion_policy
latest_completion_request_id
```

Run 增加：

```text
completion_requested_at
completion_request_id
```

Rubric report 模型继续保留，但只有 `completion_policy=rubric` 的 completion request 才可关联它。标准验收不创建空 report，也不伪造 `NOT_REQUIRED` 裁决。

### 8.2 三个正交维度

`RunKind`、`GoalCompletionPolicy` 和内部 `VerificationMode` 必须各自只表达一个事实：

| 维度 | 回答的问题 | 建议值 |
|---|---|---|
| `RunKind` | 本 Run 是否执行 Goal | `goal_execution / goal_inspection / standalone` |
| `GoalCompletionPolicy` | Goal 完成申请由谁裁决 | `standard / rubric` |
| `VerificationMode` | 本 Run 当前使用什么内部验证强度 | `agent / proportional / rubric` |

建议将当前 `VerificationMode.GOAL` 直接改名为 `RUBRIC`，避免再用“是否 Goal”表达“是否独立验收”。既然本方案仅面向新 Session/Goal，不需为旧枚举值增加生产兼容分支。

| Goal 完成方式 | Run 有效 `VerificationMode` | 运行期行为 |
|---|---|---|
| 标准验收，无 mutation | `agent` | Agent 自查，不启动 reviewer |
| 标准验收，已发生 mutation | `agent → proportional` | 保留 receipt/状态安全信息，不构成第三种 Goal 验收模式 |
| Rubric 验收 | `rubric` | 完成申请后允许 deterministic checks + grader + 有限修复 |

### 8.3 当前代码需要解耦的位置

1. `backend/harness/coordinators.py`
   - 当前显式 Goal Run 只要 `verification_enabled` 就强制映射为 `VerificationMode.GOAL`；
   - 改为仅 `completion_policy=rubric` 时编译和冻结 contract；
   - `complete_from_final_state()` 不再用 `NOT_REQUIRED` report 自动完成标准 Goal；
   - 将 `GoalCoordinator.apply_run_report()` 收缩为 Rubric 路径，新增通用 completion request 终结入口。
2. `backend/graph/deepagents_manager.py`
   - 当前 `PuddingClawRubricMiddleware.after_agent/aafter_agent` 在 Goal Run 自然停止后介入；
   - 改为检查持久化的 completion request，只处理 Rubric 模式的显式提交；
   - 流式结束时不再把所有 Run 无条件推入 `EVALUATING`；
   - 没有 completion request 时可发布本 Run 的进度回复，但 Goal 保持 active。
3. `backend/graph/session_manager.py`
   - 增加 completion request 的幂等写入和 Goal/Run/最终消息原子提交；
   - 将 `commit_accepted_completion()` 的权威从“必须有 accepted Rubric report”改为“必须有同 Run/revision 的 accepted completion request”；
   - Rubric 模式额外要求 request 绑定的 report 已接受；
   - 保持 `goal_id + objective_revision` CAS 约束。
4. `backend/harness/models.py`
   - 增加 `GoalCompletionPolicy` 与 completion request 模型；
   - `VerificationMode` 不再承担“是不是 Goal”的双重语义；
   - Goal 对外终态从 `ACHIEVED` 直接统一为 `COMPLETED`。
5. Agent Tool 注册与权限
   - 新增主 Agent 可见的 `update_goal`；
   - 工具绑定 session/query/run/goal/revision；
   - 不向 subagent/reviewer 工具集暴露 `update_goal`，工具处理器仍必须再做身份校验。
6. `frontend/src/app/settings/page.tsx`
   - Rubric 默认关闭；
   - 改名并增加“实验性”徽标和风险说明。
7. `frontend/src/components/chat/ChatInput.tsx`
   - Goal 创建时允许选择标准验收或 Rubric 验收。
8. `frontend/src/components/citations/SourcesPanel.tsx`
   - 标准完成不展示 VerificationCard；
   - Rubric 模式保留现有详细报告。

### 8.4 配置建议

```json
{
  "harness": {
    "goals": {
      "enabled": true,
      "default_completion_policy": "standard"
    },
    "completion": {
      "rubric": {
        "enabled": false,
        "experimental": true,
        "max_iterations": 2,
        "max_stagnant_repairs": 1
      }
    }
  }
}
```

`rubric.enabled` 表示产品是否允许用户选择 Rubric 验收；不表示所有 Goal 默认启用。

### 8.5 原子终结事务的最小不变式

无论 standard 还是 rubric，最终发布入口只允许在同一 Session 写锁中完成以下操作：

1. 重新读取权威 Goal、Run 和 completion request，不信任调用方的旧内存快照。
2. 校验 Goal 仍为 `ACTIVE`、Run 仍绑定当前 Goal revision、request 仍是当前有效申请。
3. 校验没有 running Tool Call、pending HITL/审批、未终结 mutation/publish lease。
4. standard 要求 request 通过 state-safety；rubric 额外要求绑定 report 已对当前 revision 做出 accepted 裁决。
5. 同时写入 request `ACCEPTED`、Run `COMPLETED`、Goal `COMPLETED`、最终 Assistant Message 和后续 SSE 所需的持久数据。
6. 清除 `active_goal_id/current_run_id`，收回或终结不应跨终态存活的执行 lease。
7. 仅在事务成功后发出 `goal_status_changed/completed`、`final_response` 和 `done`；SSE 不是事务的一部分，也不能先于权威状态。

任一校验失败都不得部分写入 Goal 终态。工具 ack、grader verdict 和前端动画都不能替代这个事务。

## 9. 生效边界

### 9.1 新 Session 与新 Goal

- 默认 `completion_policy=standard`；
- 只有用户明确选择 Rubric 验收时才使用 `rubric`；
- 不因任务复杂、工具类型、失败次数或模型判断自动升级。

### 9.2 不迁移旧状态

- 不迁移已有 active Goal；
- 不迁移历史 Session JSON；
- 不为旧 `achieved` 状态增加兼容读取；
- 不要求新前端继续接受旧 Goal 状态协议；
- 验收测试统一使用实现后新建的 Session 和 Goal；
- 旧测试数据如与新协议冲突，直接清理或重新生成，不编写生产兼容分支。

## 10. 测试矩阵

### 10.1 标准验收

- active Goal 调用 `update_goal(completed=true)` 后先持久化 request，工具 ack 不得声称 Goal 已完成；
- Agent 生成最终回复并自然停止后，request、Run、Goal 和最终消息原子完成；
- 没有完成调用时，terminal AIMessage 和 SSE done 不得完成 Goal；
- 没有完成调用时，当前 Run 可以 `completed`，但 Goal 必须保持 `active`；
- 重复 tool call id 幂等；
- 旧 revision、错误 Run、非 active Goal 调用被拒绝；
- subagent/reviewer 不可见或不可成功调用 `update_goal`；
- `update_goal` 与兄弟 Tool Call 并发时拒绝或使完成申请无效；
- completion request 后又发生 mutation 时，旧 request 失效并要求 Agent 重新提交；
- 正在运行的 Tool、HITL、权限或外部 mutation 阻止状态提交；
- 缺少测试、存在未提交修改或没有 Evidence Ledger 不阻止标准完成；
- 标准模式不进入 `EVALUATING`、不调用 grader、不产生空 Rubric report 或 VerificationCard；
- 完成摘要只包含真实执行过的测试和检查。

### 10.2 Rubric 验收

- 自然停止不触发 Rubric；
- 显式完成调用只触发一次 Rubric；
- deterministic 和 grader 全部通过后完成；
- task gap 返回 Agent，Goal 保持 active，Run 回到 running；
- Agent 修复后必须创建新 completion request，不复用旧裁决快照；
- 相同 gap 达到阈值后停止自动修复；
- grader error、协议错误和基础设施错误进入 `control_notices`，不冒充 task gap；
- verifier 异常不消耗 Goal round；
- 旧 revision 的通过结果不能完成新 revision；
- Rubric 设置变更不影响 active Goal；
- 前端始终显示“Rubric 验收 · 实验性”。

### 10.3 新 Session 边界

- 新 Session 创建的 Goal 只写入新完成协议和状态词汇；
- 测试不得依赖旧 Session、旧 active Goal 或旧 `achieved` fixture；
- 普通非 Goal Run 行为保持不变；
- 用户暂停、取消、恢复和修改 Goal 的行为保持独立。

## 11. 实施顺序

### P0：完成协议

1. 增加 `GoalCompletionPolicy` 和可追溯的 `GoalCompletionRequest`；
2. 增加仅主 Agent 可用的 `update_goal(completed=true)` 工具，实现身份绑定与幂等 ack；
3. 将自然停止、Run 终态和 Goal 完成解耦；
4. 将最终发布权威从必选 Rubric report 改为 accepted completion request；
5. 实现标准模式的两阶段申请和原子终结；
6. 新 Goal 默认标准验收；
7. `VerificationMode.GOAL` 改为 `RUBRIC`，Goal 完成状态的 API/UI 词汇统一为 `completed`。

### P1：Rubric 改为显式触发

1. 将 Rubric 从普遍 `after_agent` 拦截改为消费 completion request；
2. 保留现有 contract、deterministic checks、Evidence Ledger 和 grader；
3. `needs_revision` 时将 Run 恢复为 running，修复后由 Agent 显式提交新 completion request；
4. 收紧修复次数和控制面异常处理。

### P2：前端产品化与观测

1. 增加完成方式选择；
2. 增加“Rubric 验收 · 实验性”标识和默认关闭；
3. 标准模式收起 VerificationCard；
4. 记录标准完成后的用户重开率、Rubric 误拒率、额外模型调用、耗时和实际修复率。

### P3：逐步完善 Rubric 验收

- 根据真实误拒案例完善 criterion 和 evidence scope；
- 增加领域 verifier，但不得反向进入标准模式；
- 评估可选 project completion hook；
- 在指标证明稳定前始终保留“实验性”标识。

## 12. 非目标

- 本轮不删除 Rubric 或 Evidence Ledger；
- 不删除 Goal 的跨 Run 延续、预算、暂停、恢复和 revision；
- 不把所有普通 Run 自动创建为 Goal；
- 不引入多 skeptic panel；
- 不为标准验收建立新的隐式评分器；
- 不用前端文案掩盖后端状态错误；
- 不迁移或兼容旧 Session、旧 active Goal 和旧 `achieved` 状态。

## 13. 审核清单

请按以下默认建议审核：

- [ ] 对外统一使用 `update_goal(completed=true)`；
- [ ] `GoalCompletionRequest` 成为独立、可追溯、幂等的完成控制对象；
- [ ] 工具 ack 只声称 request 已记录，仅原子终结事务可声称 Goal 已完成；
- [ ] Goal 默认使用标准验收；
- [ ] 标准模式使用固定完成审计 Prompt，但不生成或持久化 Rubric；
- [ ] Rubric 验收是最高级别且默认关闭；
- [ ] 前端固定显示“Rubric 验收 · 实验性”；
- [ ] Rubric 只由显式完成调用触发；
- [ ] Run 自然结束与 Goal 完成已解耦；
- [ ] 标准模式只保留状态安全硬门槛；
- [ ] verifier 异常不作为任务失败、不消耗 Goal round；
- [ ] 标准模式不展示 VerificationCard；
- [ ] 方案仅以新建 Session 和 Goal 为验收范围，不实现旧状态兼容。

审核通过后，按 P0 → P1 → P2 实施；P3 作为 Rubric 验收的持续实验迭代。
