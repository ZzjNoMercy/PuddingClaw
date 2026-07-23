# Goal 回合路由、Todo 持久化与前端投影整体修复方案

> 状态：Implemented，E2E 已验收
> 日期：2026-07-23
> 范围：Goal Mode 后续消息路由、Run/Goal 归属、Completion Gate、Todo 持久化、取消恢复、前端 Todo 投影与 E2E
> 关联方案：[跨 Run 完整上下文、Evidence 投影与 Harness 能力解耦方案](./cross-run-context-evidence-and-capability-decoupling-plan.md)
> 核心原则：**关联 Goal 不等于执行 Goal；当前用户消息决定本轮行为；工具成功即代表状态已经持久化。**

## 实施进度

> 最后更新：2026-07-23

- [x] 完成事故复盘、第一性原理约束和整体方案评审
- [x] 审计现有实现、兼容边界和已有测试
- [x] 实现 Goal 回合前置路由与 RunKind/context Goal 数据模型
- [x] 实现 Goal Inspection 只读上下文、能力约束和验收/续跑硬门
- [x] 实现 Todo ledger revision、幂等原子 patch 与取消持久性
- [x] 扩展 SSE/API Todo 快照契约
- [x] 修复前端 Todo 权威投影、取消对账和状态文案
- [x] 完成单元、集成、前端与关键 E2E
- [x] 完成对抗式审查和逐项完成审计

实施记录只在获得代码或测试证据后勾选，计划项本身不作为完成证明。

### 实施与验收证据

| 范围 | 落地结果 | 证据 |
| --- | --- | --- |
| 回合路由 | 前端用 `context_goal_id` 传 standing Goal；后端在创建 Run 前区分 inspect/continue/revise/control/standalone/clarify | `backend/harness/goal_turn_router.py`、`backend/graph/deepagents_manager.py` |
| Run 归属 | `goal_id` 仅代表执行所有权；inspection 只有 `context_goal_id`，不进入 `goal.run_ids` | `backend/harness/models.py`、`backend/harness/coordinators.py` |
| Inspection 权限 | Tool Manifest 使用“现有 Goal/Todo/Evidence”显式 allowlist，并在 ToolCall 层二次拒绝写操作、delegation、SQL 生成/验证/执行和 SourceReference 注册 | `backend/graph/middlewares/toolset.py`、`backend/tools/toolsets.py` |
| Completion 边界 | inspection 不运行 Goal grader、不自动续跑、不发送 `verification_report` | `backend/graph/deepagents_manager.py` |
| Todo durability | `update_todos` 先在 Session 写锁内执行幂等 patch，再返回 Tool success；Graph values 不再具有第二写入口 | `backend/graph/middlewares/harness_todos.py`、`backend/graph/session_manager.py` |
| 前端投影 | `run_started` 不再清空 Todo；按 authority/revision 丢弃旧事件；所有 stream 终态回拉 `/todos/current` | `frontend/src/lib/store.tsx`、`frontend/src/lib/todoProjection.ts` |
| 自动化测试 | 后端正式测试域 1138 项全部通过（含 Docker Chromium 闸门）；前端 10 个单测、TypeScript、Next.js production build 通过 | 2026-07-23 最终验收记录 |
| 集成 E2E | inspection 路由、真实 manager continuation 和“Todo 成功后立即取消”三个持久化 E2E 通过 | `backend/tests/integration/test_goal_turn_todo_durability_e2e.py` |
| 真实联调 E2E | active Goal 23 条 Todo（9 completed/2 in_progress/12 pending）前后不变；Goal round=3、execution run_count=3 不变；最新 Run=`goal_inspection`；SSE 无 verification/auto-continue/todo mutation | Session `session-e851ed9c80c9`，Run `run-ef30b67316ed4690` 及其后续 inspection Run |
| 浏览器冒烟 | Chromium 以 1440×1000 打开真实前端并完成页面渲染检查 | `output/playwright/goal-todo-e2e-home.png` |

### 对抗式审查发现与处置

1. **发现旧 Graph values 仍是第二 Todo 写入口**：即使 Tool 已即时持久化，延迟 values 仍可能用旧完整列表覆盖新 revision。已删除该写路径，values 现在只读取权威 ledger、发 SSE，并对不一致记录指标。
2. **发现 inspection 仍发送 `not_required` 验收事件**：虽然没有 grader，但前端仍可能显示验收状态。已增加 RunKind 硬门；inspection 仅在持久 RunRecord 中保留“不需要验收”，不发送 `verification_report`。
3. **发现混合语句可能被首个正则抢占**：例如“先总结，然后继续执行”。已改为混合读写意图交给语义路由，低置信度仍进入 clarify。
4. **发现旧 list replacement 可覆盖 transactional ledger**：已拒绝对存在 operation receipt 的 ledger 做列表替换，并增加 revision conflict、并发 stable-ID merge、旧 Goal revision 拒绝测试。
5. **权限对抗结果**：inspection 的 `update_todos`、文件写入、外部提交和 `task` delegation 均被 Capability Manifest 与 ToolCall Gate 双层拒绝。
6. **发现“业务只读”被误当成“控制面只读”**：SQL execute 会落 Result Store，result source 会注册 SourceReference，generate/validate 也会创建 Ledger 记录。四者现均声明为 `internal_mutation`，inspection 改为显式证据读取 allowlist，避免未来新增“看似只读”工具自动进入总结 Run。

### 验收备注与设计偏差

- 仓库主测试域在启用浏览器闸门后结果为 **1138 passed**。Artifact contract 使用自包含合成夹具，不再依赖或驱动修改 `designs/product-configuration-analysis` 下的用户业务报告。
- 从 `backend/` 无选择执行裸 `pytest` 还会被 `backend/skills` 下同名 `test_skill.py` 和 skill-benchmark 专用 import path 阻断；正式项目测试域应使用 `pytest tests`。
- 未引入可关闭 atomic Todo commit 或 inspection read-only 的 Feature Flag：二者是数据一致性/权限不变量，不应允许退回不安全旧行为。Router 保留结构化审计和安全 fallback，但不提供“失败即旧式自动续跑”的回滚开关。

## 0. 审核摘要

本次问题不是单点缺陷，而是四个控制面职责被错误耦合：

1. Session 存在 active Goal 时，前端无条件把所有后续消息作为 Goal Run 发送；
2. 后端用 standing Goal 的原始 objective 覆盖当前用户消息，并继承完整 Goal 验收合同；
3. `update_todos` 先更新 Graph state，再依赖后续流事件同步 Session，存在取消窗口；
4. 前端收到 `run_started` 后无条件清空 Todo，取消前未收到新快照时长期显示为空。

因此，用户发送“总结一下已经完成的工作”时发生了以下错误链路：

```text
只读进度提问
  → 自动挂载 active Goal
  → objective 被替换为原始产物任务
  → 继承 artifact 验收合同
  → Completion Gate 判定原任务未完成
  → needs_revision
  → Agent 继续执行而不是回答
```

本方案采用四项配套修复：

1. 在创建 Run 前增加独立的 `GoalTurnRouter`；
2. 将“Goal 执行归属”与“Goal 只读上下文引用”拆成两个字段；
3. 将 Todo 更新改为带 revision 和幂等键的事务化即时落盘；
4. 前端按服务端权威 Todo ledger 投影，不再在 `run_started` 时清空。

Prompt 优先级修复作为立即生效的防御层保留，但不能替代上述结构性约束。

---

## 1. 事故复盘与根因

### 1.1 “总结一下”被当成 Goal 续跑

当前前端在存在 active Goal 时使用近似以下判断：

```ts
const goalModeForRun =
  requestedGoalMode || goalForRun?.status === "active";
```

这意味着 active Goal 存续期间，所有普通消息都会携带 `goal_id`。系统没有在发送前区分：

- 查询 Goal 状态；
- 继续 Goal；
- 修改 Goal；
- 暂停或取消 Goal；
- 与 Goal 无关的新问题。

后端收到 `goal_id` 后，进一步把当前 Run 的 objective 设为 standing Goal objective，而不是当前用户消息。随后 `start_run()` 继承冻结的 Goal contract。最终，一个只需要对话回答的请求被编译成完整 artifact Run。

### 1.2 模型没有“当前消息优先”的控制信号

当前 Agent 能看到 standing Goal，但系统没有提供明确的回合意图，也没有告诉模型：

- 当前消息是本轮最高优先级指令；
- active Goal 默认只是上下文；
- 只有继续意图才允许推进原任务；
- 进度、总结、解释类提问必须先回答，不得自行执行。

因此模型把 standing Goal 解释成了持续执行授权。

### 1.3 Todo 更新存在取消窗口

`backend/graph/middlewares/harness_todos.py` 当前先根据 `runtime.state.todos` 计算新列表，再返回 LangGraph `Command(update={"todos": ...})`。Session 台账由 `backend/graph/deepagents_manager.py` 在后续 stream values 同步点写入。

风险窗口为：

```text
模型调用 update_todos
  → Graph state 已准备更新
  → 用户立即取消
  → 下游 values/flush 未执行
  → Session ledger 仍为旧版本
```

需要额外注意：当前正式 `todos_updated` SSE 的代码路径是在 `session_manager.update_todos()` 之后发出。如果日志确认客户端确实收到正式 `todos_updated`，但 Session 仍回退到旧状态，则还存在旧快照覆盖或并发写入问题。新方案必须用 ledger revision 和 operation receipt 同时覆盖这两类风险。

### 1.4 Todo 只在前端“消失”

`frontend/src/lib/store.tsx` 在每次 `run_started` 时无条件执行：

```ts
todosMapRef.current[sendSessionId] = [];
setTodos([]);
```

如果新 Run 被快速取消，且没有完成一次权威 `todos_updated`，缓存会一直保持空列表。Session 中的 Goal ledger 仍然存在，但界面看起来像没有继承。

---

## 2. 第一性原理与系统不变量

修复后必须满足以下不变量。

### 2.1 用户意图不变量

1. 当前用户消息决定本轮行为；
2. standing Goal 只能提供背景，不能自动授予继续执行权；
3. 低置信度分类不得默认进入可能修改状态的执行路径；
4. 前端提示不能替代后端权威路由。

### 2.2 Goal 生命周期不变量

1. 只有 `goal_execution` Run 可以推进 Goal；
2. 只读查询不能加入 `goal.run_ids`；
3. 只读查询不能消耗 Goal 自动续跑轮数；
4. 只读查询不能继承 Goal completion contract；
5. 只有 execution Run 可以进入 Completion Gate 和自动续跑。

### 2.3 Todo 一致性不变量

1. `update_todos` 返回成功前必须完成持久化；
2. 已成功的 Todo 操作不能因为 Run 取消而回滚；
3. 同一个 `tool_call_id` 重试不能重复应用；
4. 旧 revision 不能覆盖新 ledger；
5. Graph state、Session ledger 和前端投影最终必须收敛到同一 revision。

### 2.4 UI 投影不变量

1. `run_started` 不能制造空白 Todo 状态；
2. 前端只接受相同或更高 revision 的权威快照；
3. stream 正常结束、取消、失败或断网后都要重新对账；
4. UI 必须区分“正在读取 Goal 进度”和“正在执行 Goal”。

---

## 3. 目标状态机

### 3.1 Goal Turn Intent

新增独立枚举：

```python
class GoalTurnIntent(StrEnum):
    INSPECT_GOAL = "inspect_goal"
    CONTINUE_GOAL = "continue_goal"
    REVISE_GOAL = "revise_goal"
    CONTROL_GOAL = "control_goal"
    STANDALONE_TASK = "standalone_task"
    CLARIFY = "clarify"
```

语义如下：

| Intent | 含义 | 是否执行 Goal | 是否允许写状态 |
| --- | --- | ---: | ---: |
| `inspect_goal` | 总结、进度、剩余工作、失败原因、证据查询 | 否 | 否 |
| `continue_goal` | 从当前状态继续原 Goal | 是 | 是 |
| `revise_goal` | 修改 Goal objective 或范围后执行 | 是 | 是 |
| `control_goal` | 暂停、恢复、取消等明确控制动作 | 走控制 API | 由动作决定 |
| `standalone_task` | 与 active Goal 无关的新任务 | 否 | 按新任务决定 |
| `clarify` | 无法可靠判断 | 否 | 否 |

### 3.2 Run 类型

新增：

```python
class RunKind(StrEnum):
    GOAL_EXECUTION = "goal_execution"
    GOAL_INSPECTION = "goal_inspection"
    STANDALONE = "standalone"
```

扩展 `RunRecord`：

```python
run_kind: RunKind = RunKind.STANDALONE
goal_id: str | None = None
context_goal_id: str | None = None
goal_turn_intent: GoalTurnIntent | None = None
goal_turn_confidence: float | None = None
goal_turn_classifier: str | None = None
```

字段语义必须严格拆分：

- `goal_id`：本 Run 对 Goal 具有执行归属；
- `context_goal_id`：本 Run 只允许读取该 Goal 的上下文；
- 二者不能因为 active Goal 存在而自动同时赋值。

### 3.3 三类 Run 的行为矩阵

| Run 类型 | `goal_id` | `context_goal_id` | Goal contract | Completion Gate | 自动续跑 | Todo 修改 |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| `goal_execution` | 有 | 可有 | 继承 | 允许 | 允许 | 允许 |
| `goal_inspection` | 无 | 有 | 不继承 | 禁止 | 禁止 | 禁止 |
| `standalone` | 无 | 无 | 按当前请求 | 非 Goal 模式 | 禁止 | 按任务决定 |

历史数据兼容：

- 缺少 `run_kind` 且有 `goal_id`：迁移为 `goal_execution`；
- 缺少 `run_kind` 且没有 `goal_id`：迁移为 `standalone`；
- `context_goal_id` 默认 `null`；
- 迁移只影响读取投影，不重写历史文件也可工作。

---

## 4. Goal Turn Router

### 4.1 执行位置

Router 必须在创建 Run、选择 objective 和编译 contract 之前执行：

```text
当前用户消息
  → 读取 active Goal 摘要
  → GoalTurnRouter
  → 决定 RunKind / Goal 关系
  → 决定本轮 objective
  → start_run
  → Agent
```

不得继续使用以下顺序：

```text
先挂 Goal
  → 先继承 objective/contract
  → 再让 TaskProfile Router 分类
```

### 4.2 输入契约

Router 只读取有界控制面信息，不读取完整私有推理和大工具日志：

```json
{
  "current_message": "总结一下你已经完成的工作",
  "active_goal": {
    "goal_id": "goal-xxx",
    "objective": "刷新 V2 报告",
    "status": "active",
    "revision": 1,
    "todo_counts": {
      "pending": 5,
      "in_progress": 0,
      "completed": 3,
      "cancelled": 0
    },
    "latest_run_status": "cancelled"
  }
}
```

### 4.3 输出契约

```json
{
  "intent": "inspect_goal",
  "target_goal_id": "goal-xxx",
  "confidence": 0.97,
  "reason": "用户只要求总结已有进度，没有要求继续执行",
  "classifier": "llm"
}
```

输出必须通过 schema 校验；非法输出进入安全 fallback。

### 4.4 混合判断策略

高精度确定性规则优先，LLM 只处理模糊表达。

明确只读示例：

- “总结一下已经完成的工作”；
- “现在做到哪了”；
- “列出剩余工作”；
- “为什么刚才失败”；
- “展示当前 Todo”；
- “已经查了哪些数据”。

明确继续示例：

- “继续执行”；
- “从中断处恢复”；
- “把剩余工作完成”；
- “接着做”；
- “继续推进这个 Goal”。

明确修订示例：

- “时间范围改为 2022–2026”；
- “不要生成 HTML，只输出数据”；
- “在原任务上增加 E2E 截图”。
- 中断正在执行的 `copy_file` 后说“不要复制这种依赖”。

模糊示例：

- “继续看看”；
- “那这个呢”；
- “处理一下”；
- “可以了”。

模糊表达交给 LLM，但 Router 输入不能只有 Goal 摘要，还必须包含最近 Run 的
status/outcome/error/handoff 和最近工具动作的 bounded projection（工具名、目标、状态，不含原始
工具输出）。这使“这种依赖”“不要这样”等省略表达可以关联到中断前动作。

若置信度低于阈值，普通模糊表达返回 `clarify`；明确的命令式纠偏则使用安全降级：保留原 Goal
全文，原样追加用户约束并标注其针对的最近动作，然后进入 `revise_goal`。Router 超时、异常或
schema 错误仍不能无条件默认进入 `continue_goal`。

### 4.5 与 TaskProfile Router 的职责边界

`GoalTurnRouter` 决定：

- 是否执行 standing Goal；
- Run 与 Goal 的关系；
- 是否需要修订 Goal；
- 是否为只读观察。

现有 TaskProfile Router 继续决定：

- 当前 Run 的工作性质；
- delivery form；
- verification pack；
- Skill 候选。

TaskProfile Router 的输入必须是“当前 Run 的真实 objective”：

- inspection：当前用户消息；
- execution：standing Goal 最新 objective；
- revision：修订后的 Goal objective；
- standalone：当前用户消息。

---

## 5. Goal Inspection 执行模型

### 5.1 有界只读上下文

Inspection Run 注入：

```json
{
  "current_user_request": "总结一下已经完成的工作",
  "turn_intent": "inspect_goal",
  "goal_context": {
    "goal_id": "goal-xxx",
    "objective": "刷新 V2 报告",
    "status": "active",
    "revision": 1,
    "todos": [],
    "latest_run_statuses": [],
    "handoff_summaries": [],
    "evidence_refs": [],
    "artifact_refs": []
  }
}
```

只注入结构化交接和 Evidence 引用，不回放私有推理，不把历史能力或权限带入当前 Run。

### 5.2 Prompt 防御层

所有存在 standing Goal 的回合先注入通用优先级：

```text
当前用户消息是本轮最高优先级指令，standing Goal 默认仅作为背景。
只有用户明确要求继续、恢复或完成剩余工作时，才允许推进 Goal。
用户要求总结、解释、查看进度、列出剩余工作或询问失败原因时，
只回答该问题，不得自行继续执行 Goal。
```

Inspection Run 再注入：

```text
本轮已被控制面判定为只读 Goal 查询。
不得修改 Todo、文件、数据库或外部状态；不得触发 Goal 续跑。
基于提供的 Goal ledger、handoff 和 Evidence 回答当前用户问题。
```

Execution Run 注入：

```text
本轮已被控制面判定为继续执行 Goal。
从持久化 Todo、Evidence 和已有产物处继续，避免重复已完成工作。
```

Prompt 是防御性约束，不能作为唯一安全边界。

### 5.3 Capability 与权限约束

Inspection Run 必须从 Capability Manifest 中排除：

- `update_todos`；
- 文件写入、删除与 patch；
- Artifact 发布与外部 commit；
- 任何外部状态修改工具；
- 会启动 Goal 执行或子任务执行的控制工具。

允许：

- 读取 Goal/Todo/Handoff；
- `read_evidence`；
- 只读 Session/Artifact 元数据；
- 必要时读取已经 materialize 的 SQL result。

如果模型尝试调用写工具，Tool Gate 必须以 `inspection_run_read_only` 拒绝，并记录 invariant 指标。

### 5.4 验收与自动续跑

Completion Gate 增加硬条件：

```python
if run.run_kind != RunKind.GOAL_EXECUTION:
    return None
```

Goal 自动续跑增加同样条件：

```python
can_auto_continue = (
    run.run_kind == RunKind.GOAL_EXECUTION
    and run.goal_id is not None
    and continuation_reason is not None
)
```

Inspection Run 不得产生：

- `deterministic_checks_completed: needs_revision`；
- Goal Rubric grader；
- `verification_report`；
- `goal_run_continued`。

出现任一事件都应记录为系统 invariant violation。

---

## 6. Todo 事务化即时持久化

### 6.1 单一权威写入口

新增 SessionManager 原子接口：

```python
apply_todo_patch(
    session_id: str,
    authority: TodoAuthority,
    operations: list[TodoPatchOperation],
    operation_id: str,
    expected_revision: int | None,
) -> TodoLedgerSnapshot
```

禁止工具继续基于 Graph state 计算完整列表后再调用 list replacement 写入。Graph state 是运行时投影，不是跨 Run 权威。

### 6.2 Ledger 数据结构

```json
{
  "scope": "goal:goal-xxx:revision:1",
  "revision": 7,
  "items": [],
  "applied_operations": {
    "call_xxx": {
      "revision": 7,
      "applied_at": 1784769000.0,
      "result_digest": "sha256:..."
    }
  }
}
```

兼容期间继续维护顶层 `todos` 作为 projection cache，但它不能成为 lifecycle owner。

### 6.3 原子更新顺序

在 Session 写锁内：

1. 读取 authority 对应的最新 ledger；
2. 检查 `operation_id/tool_call_id` 是否已经执行；
3. 在最新 ledger 上应用增量操作；
4. 校验 stable ID、状态迁移、completion contract 与 evidence refs；
5. 增加 revision；
6. 原子写 Session；
7. 记录 operation receipt；
8. 返回持久化后的完整快照。

工具调用顺序变为：

```text
apply_todo_patch 持久化成功
  → update_todos Tool 返回成功
  → Command 使用持久化快照更新 Graph state
  → 发出带 revision 的 todos_updated
```

如果持久化失败：

- Tool 返回 error；
- Graph state 不更新；
- 不发送成功的 `todos_updated`。

### 6.4 幂等与冲突策略

- 相同 `operation_id` 重试：返回原 receipt 和同一结果，不重复执行；
- stable-ID 的状态更新：允许在最新 ledger 上重放；
- create：继续按规范化 content 和 operation ID 去重；
- reorder：若 `expected_revision` 过期则拒绝，要求重新读取；
- 旧的 list snapshot 禁止覆盖更高 revision；
- 子 Agent 若需要更新 Todo，必须通过同一原子入口和 authority 校验。

### 6.5 取消语义

取消 Run 只终止尚未完成的执行，不回滚已经成功提交的工具副作用。

因此：

- `update_todos` 已返回成功：Todo 必须保留；
- Tool 仍在运行且未形成 receipt：本次更新不成立；
- UI 必须以服务端 receipt/revision 判断，而不是根据工具卡片是否出现。

---

## 7. SSE 与 API 契约

### 7.1 `run_started`

扩展事件：

```json
{
  "event": "run_started",
  "data": {
    "run": {
      "run_id": "run-xxx",
      "run_kind": "goal_inspection",
      "goal_id": null,
      "context_goal_id": "goal-xxx"
    },
    "todos": [],
    "todos_authority": {
      "kind": "goal",
      "goal_id": "goal-xxx",
      "goal_revision": 1,
      "ledger_revision": 7
    }
  }
}
```

### 7.2 `todos_updated`

```json
{
  "event": "todos_updated",
  "data": {
    "todos": [],
    "authority": {
      "kind": "goal",
      "goal_id": "goal-xxx",
      "goal_revision": 1
    },
    "ledger_revision": 8,
    "operation_id": "call_xxx",
    "source_run_id": "run-xxx",
    "persisted_at": 1784769000.0
  }
}
```

### 7.3 当前 Todo 快照接口

新增或复用轻量接口：

```http
GET /api/sessions/{session_id}/todos/current
```

响应：

```json
{
  "session_id": "session-xxx",
  "todos": [],
  "authority": {
    "kind": "goal",
    "goal_id": "goal-xxx",
    "goal_revision": 1
  },
  "ledger_revision": 8
}
```

该接口读取 `_current_todo_projection()`，不得直接返回顶层 legacy cache。

---

## 8. 前端 Todo 投影

### 8.1 删除无条件清空

删除 `run_started` 中：

```ts
todosMapRef.current[sendSessionId] = [];
setTodos([]);
```

改为根据 `todos_authority + ledger_revision` 应用服务端快照。

### 8.2 前端缓存结构

从：

```ts
Record<string, TodoItem[]>
```

调整为：

```ts
interface TodoProjection {
  items: TodoItem[];
  authority: TodosAuthority;
  ledgerRevision: number;
  syncState: "synced" | "refreshing" | "stale";
}
```

### 8.3 更新规则

1. 相同 Goal revision：只接受相同或更高 ledger revision；
2. 新 Goal revision：使用权威快照替换；
3. Inspection Run：继续显示 `context_goal_id` 的 ledger；
4. Execution Run：显示 `goal_id` 的 ledger；
5. Standalone Run：显示 Run-owned ledger；
6. 没有新快照时保留旧内容并标记 `refreshing`，不得显示空列表；
7. 旧 revision SSE 延迟到达时必须忽略。

### 8.4 结束与取消对账

所有 stream 终态的 `finally` 必须调用当前 Todo 快照接口，包括：

- completed；
- cancelled；
- failed；
- AbortError；
- 网络断开；
- SSE 非正常结束。

对账完成前保留当前 Todo，显示“正在同步”，不能清空。

### 8.5 UI 状态文案

建议：

- `goal_execution`：“正在执行 Goal”；
- `goal_inspection`：“正在读取 Goal 进度”；
- `standalone`：“Agent 正在处理”；
- Completion repair：“检测到执行任务尚未完成，继续处理”；
- 最终验收：“正在验收 Goal 交付”。

只读查询界面不得出现“验收”“修复缺口”“自动进入下一轮”等提示。

---

## 9. 代码修改点

### 9.1 `backend/harness/models.py`

- 新增 `GoalTurnIntent`；
- 新增 `RunKind`；
- 扩展 `RunRecord` 的 `run_kind/context_goal_id/router metadata`；
- 增加向后兼容迁移 validator；
- 增加 `requires_goal_execution` 属性。

### 9.2 新增 `backend/harness/goal_turn_router.py`

- 高精度确定性分类；
- LLM 模糊分类；
- schema 校验；
- confidence threshold；
- safe fallback；
- Trace/metrics payload。

### 9.3 `backend/graph/deepagents_manager.py`

- 在 `start_run()` 前调用 GoalTurnRouter；
- 不再因 active Goal 自动覆盖当前消息；
- 按路由结果选择 objective 和 RunKind；
- Inspection Run 注入只读 Goal projection；
- Completion Gate、grader 和 auto-continue 增加 RunKind 硬门；
- `run_started` 携带 Todo 权威快照；
- `todos_updated` 携带 revision 和 operation receipt。

### 9.4 `backend/harness/coordinators.py`

- `start_run()` 接收 RunKind 和 context Goal；
- 只有 `goal_execution` 调用 `goal.attach_run()`；
- 只有 execution Run 继承冻结 Goal contract；
- Inspection Run 使用当前消息生成普通 answer profile；
- Goal 轮数和模型调用预算不统计 inspection。

### 9.5 `backend/graph/middlewares/harness_todos.py`

- 从 Graph-state-first 改为 ledger-first；
- 调用 `apply_todo_patch()`；
- 使用持久化结果更新 Command；
- Tool 返回 operation receipt；
- Inspection Run 拒绝调用。

### 9.6 `backend/graph/session_manager.py`

- 增加 Todo ledger revision；
- 增加原子 patch 和幂等 receipt；
- 禁止低 revision list replacement；
- 增加当前 Todo 快照读取；
- legacy `todos` 只保留为 projection cache。

### 9.7 API

- 增加 `/todos/current`；
- Harness Run API 暴露 RunKind 和 context Goal；
- SSE schema 增加 Todo authority/revision；
- 保持旧客户端字段可选兼容。

### 9.8 `frontend/src/lib/store.tsx`

- active Goal 不再直接决定 `goalModeForRun`；后端 Router 为权威；
- `run_started` 不清空 Todo；
- Todo cache 保存 authority/revision；
- finally 阶段统一对账；
- 丢弃旧 revision 事件；
- 根据 RunKind 展示状态文案。

---

## 10. 实施顺序

### P0：立即止血

1. 增加 standing Goal 当前消息优先 Prompt；
2. 删除前端 `run_started` 清空 Todo；
3. stream finally 重新读取 Todo；
4. `update_todos` 在成功返回前即时落盘；
5. 增加取消窗口回归测试。

P0 可以快速改善体感和数据一致性，但不能视为最终闭环。

### P1：控制面解耦

1. 新增 GoalTurnRouter；
2. 新增 RunKind 和 context Goal；
3. Inspection capability 只读化；
4. Completion Gate 和 auto-continue 只接受 execution；
5. TaskProfile Router 改用真实的本轮 objective。

### P2：并发、观测与兼容

1. Todo ledger revision；
2. operation receipt 与幂等；
3. 旧 snapshot 冲突保护；
4. Router shadow mode；
5. invariant metrics 和告警；
6. 完成数据迁移与兼容测试。

---

## 11. 测试与验收矩阵

### 11.1 GoalTurnRouter 单元测试

| 场景 | 期望 |
| --- | --- |
| active Goal +“总结已完成工作” | `inspect_goal` |
| active Goal +“现在做到哪了” | `inspect_goal` |
| active Goal +“列出剩余任务” | `inspect_goal` |
| active Goal +“为什么刚才失败” | `inspect_goal` |
| active Goal +“继续执行” | `continue_goal` |
| active Goal +“把剩余做完” | `continue_goal` |
| active Goal +“时间范围改为 2022–2026” | `revise_goal` |
| 中断 `copy_file(echarts.min.js)` +“不要复制这种依赖” | `revise_goal`，原样追加约束并继续 |
| 同上且 Router 超时 | contextual fallback → `revise_goal`，不得 `clarify` |
| active Goal +“这个依赖要不要复制？”且 Router 不可用 | `clarify`，不得把疑问当命令 |
| active Goal +“继续看看” | LLM 分类或 `clarify` |
| 无 active Goal +“总结这段对话” | `standalone_task` |
| Router 超时/非法 JSON + 普通模糊表达 | `clarify` 或安全只读，不得执行 |

中英文表达、否定表达和反例都要覆盖，例如：“不要继续，先总结”。该请求必须判为 `inspect_goal`。

### 11.2 后端集成测试

1. Inspection Run 有 `context_goal_id`，没有 `goal_id`；
2. Inspection Run 不继承 Goal artifact contract；
3. Inspection Run 不加入 `goal.run_ids`；
4. Inspection Run 不增加 Goal round/model call budget；
5. Inspection Run 不产生 completion/verification/auto-continue 事件；
6. Inspection Run 调用 `update_todos` 被 Tool Gate 拒绝；
7. Continue Run 继承 Goal objective、Todo 和合同；
8. Revise Run 增加 Goal revision 并使用新 objective；
9. Standalone Run 不污染 active Goal；
10. Router 判断与最终 RunRecord 可审计一致。

### 11.3 Todo 持久化测试

1. Tool 成功返回后立即取消，Session ledger 仍包含更新；
2. Tool 在持久化前被取消，不产生成功 receipt；
3. 相同 operation ID 重试只应用一次；
4. 两个并发 stable-ID 更新不会互相覆盖；
5. reorder 遇到 revision 冲突时拒绝；
6. 旧 list snapshot 不能覆盖更高 revision；
7. evidence contract 不满足时持久化和 Graph state 都不更新；
8. Goal revision 切换后不能写入旧 ledger。

### 11.4 前端测试

1. 同一 Goal 新 Run 启动时 Todo 不闪空；
2. Inspection Run 保持 Goal Todo 可见；
3. 取消后自动恢复服务端 Todo；
4. 网络断开后自动恢复；
5. Session 切换后缓存与服务端一致；
6. 页面刷新后状态一致；
7. 旧 revision 的 `todos_updated` 被忽略；
8. authority 切换时使用服务端新快照；
9. Inspection UI 不显示验收和自动续跑文案。

### 11.5 关键 E2E

```text
1. 创建 Goal；
2. 创建 8 条 Todo；
3. 完成其中 3 条；
4. 在更新完成后立即手动终止；
5. 刷新页面，3 条仍为 completed；
6. 输入“总结一下已经完成的工作”；
7. Agent 返回 3 条已完成、5 条剩余；
8. 全程没有 completion gate、grader、auto-continue 和 Todo 修改；
9. 输入“继续完成剩余工作”；
10. 创建 goal_execution Run；
11. 继承同一个 Todo ledger 和原合同；
12. 从剩余工作继续，不重复已完成查询。
```

E2E 还必须验证：

- Session JSON ledger revision；
- RunRecord 的 RunKind/Goal 字段；
- SSE 事件序列；
- 前端 Todo 可见状态；
- Completion Gate 只在 execution Run 出现；
- 取消后重载结果一致。

---

## 12. 可观测性与告警

新增指标：

```text
goal_turn_route_total{intent,classifier}
goal_turn_router_fallback_total{reason}
goal_inspection_mutation_denied_total{tool}
goal_inspection_verification_violation_total{event}
todo_patch_committed_total{authority}
todo_patch_idempotent_replay_total
todo_ledger_revision_conflict_total{operation}
todo_projection_stale_event_total
todo_cancel_reconciliation_mismatch_total
```

新增 Trace 事件：

```json
{
  "type": "goal_turn_routed",
  "intent": "inspect_goal",
  "classifier": "llm",
  "confidence": 0.97,
  "target_goal_id": "goal-xxx",
  "run_kind": "goal_inspection"
}
```

指标和 Trace 不保存私有推理，只保存枚举、置信度、目标 ID 和结构化 reason code。

核心告警：

- Inspection Run 出现 verification 事件；
- Inspection Run 成功调用修改型工具；
- `todos_updated.revision` 小于前端当前 revision；
- Tool 返回 Todo 成功但找不到 operation receipt；
- stream 结束后前端 projection 与 `/todos/current` 不一致。

---

## 13. 灰度与回滚

### 13.1 Feature Flags

建议增加：

```text
harness.goal_turn_router.enabled
harness.goal_turn_router.shadow_mode
harness.goal_inspection.read_only_enforced
harness.todos.atomic_patch_enabled
frontend.todo_revision_projection_enabled
```

### 13.2 上线顺序

1. 先上线 Todo revision 字段和兼容读取；
2. 上线即时持久化与前端取消对账；
3. Router 进入 shadow mode，记录新旧决策差异；
4. 对高置信度 inspect/continue 开启真实路由；
5. 开启 Inspection 只读 Tool Gate；
6. 最后移除前端 active Goal 自动绑定逻辑。

### 13.3 安全回滚

- Router 可回滚到旧行为，但 Todo 即时持久化和 revision 不应回滚；
- 新字段均提供默认迁移值，旧 Session 可继续读取；
- 前端未知 RunKind 时按 `goal_id` 推断旧行为；
- API 新增字段保持 optional，避免新后端立即破坏旧客户端。

---

## 14. 非目标与约束

本方案不处理：

- 改写 Goal 原始验收标准本身；
- 将私有 Chain-of-Thought 暴露给 Inspection Run；
- 取消后回滚已经成功完成的文件或外部副作用；
- 让 LLM 直接获得 Goal 生命周期写权限；
- 用关键词表完全替代语义判断；
- 通过隐藏 UI 事件掩盖错误状态机。

暂停、恢复和取消仍应优先走显式控制 API。LLM Router 负责理解自然语言意图，但不能绕过控制面状态机。

---

## 15. 审核决策项

实施前需要确认以下默认决策：

1. **低置信度默认策略**：普通模糊表达进入 `clarify`；有 standing Goal 且消息是明确命令式纠偏时，
   允许 contextual fallback 形成 append-only Goal 修订，不得丢弃原目标；
2. **Inspection 是否创建 Run**：建议创建轻量可审计 Run，但不加入 `goal.run_ids`；
3. **Inspection 数据库权限**：建议默认不开放新查询，只允许读取已有 result/evidence；
4. **Todo 成功语义**：建议工具成功即 durable commit，Run 取消不回滚；
5. **前端过渡展示**：建议保留旧 Todo 并标记“正在同步”，不使用空列表；
6. **Router 实现**：确定性规则只处理无歧义控制词；LLM 使用 Goal + 最近执行上下文处理省略表达；
   仅真正模糊的低置信度消息询问用户。

---

## 16. 完成定义

只有同时满足以下条件，本修复才算完成：

- “总结进度”不会创建 Goal execution Run；
- “总结进度”不会触发任何 Goal 验收或自动续跑；
- “继续执行”仍能继承原 Goal、Todo、Evidence 和验收合同；
- Todo 工具成功后立即取消也不会丢更新；
- 新 Run 开始、取消、切换 Session、刷新页面时 Todo 均不会消失；
- 前后端对同一 ledger revision 达成一致；
- 单元、集成、前端和关键 E2E 全部通过；
- 对抗式审查未发现只读 Run 越权修改、旧 revision 覆盖或验收旁路。

最终决策句：

> **Goal 是持久目标，不是每一条后续消息的隐式执行命令；Todo 是持久控制面台账，不是可延迟刷新的临时 Graph 状态。**
