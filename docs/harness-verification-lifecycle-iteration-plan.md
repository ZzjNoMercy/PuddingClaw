# PuddingClaw Harness 验收生命周期迭代方案

> **已被取代（2026-07-26）**：本文保留为上一版“Goal 默认强制验收”设计记录，不再作为实现基线。当前待审核方案见 [Goal 完成协议与 Rubric 验收分层方案](./goal模式验收执行顺序优化方案.md)。
>
> 状态：待审核
>
> 目标：修正 Agent 输出、确定性就绪检查、Rubric 语义验收、Run/Goal 边界及消息持久化的顺序。本文是 PuddingClaw 自身方案，不以外部框架作为架构母版。

## 0. 核心决策：先修完成协议顺序

本轮迭代首先只抓一条主链路：**任何启用验收的 Run，都不能把模型返回的 terminal AIMessage 直接当成用户最终回复。**

正确协议固定为：

```text
Model / Tools 执行
→ Agent 返回 terminal AIMessage（申请完成，暂不发布）
→ Deterministic Readiness Gate
   ├─ 未通过：gaps 写回同一图状态 → Model / Tools 继续
   └─ 通过
→ Rubric Semantic Grader
   ├─ 未通过：gaps 写回同一图状态 → Model / Tools 继续
   └─ 通过
→ Commit 最终消息、证据、RunOutcome、Goal decision
→ 发布 final_response
→ done
```

这里不存在用户可见的“候选版本”。AIMessage 始终是正常的 Agent 消息，只是在 Harness 接受前尚未获得发布权。

从用户视角看，执行、自查、Readiness、Rubric 与修正共同构成一次连续的 Agent 响应。验收不是对话中的第二个产品流程，也不应制造“任务已经回答完、系统又开始验收”的观感。

当前大部分异常都是这条顺序被破坏后的派生结果：

- “已经完成但 Agent 还在运行”：最终消息发布早于 `after_agent`；
- “验收失败后又开始下一轮”：Rubric 次数提前结束 Run；
- “run-4 没做完、run-5 才补齐”：未通过没有留在同一 Run；
- “历史只剩继续”：前端尝试按验收状态补救提前发布；
- “Goal 一直 active”：完成工作与最终验收被拆在不同 Run，后一 Run 又在验收前中断。

合同作用域、跨 Run evidence 和报告数据内容检查属于后续正确性增强；在完成协议顺序修复前，它们不能解决用户先看到错误终态的问题。

## 1. 已确认的问题

### 1.1 输出早于验收

当前主模型 token 在 Agent 节点运行期间直接通过 SSE 发给前端，并同步写入 assistant snapshot；`PuddingClawRubricMiddleware.after_agent` 只能在 Agent 节点返回 AIMessage 后介入。

因此实际顺序是：

```text
模型生成“已完成”
→ SSE 发布并写 snapshot
→ after_agent 开始确定性检查与 Rubric
→ 未通过后再回到模型
```

这会让用户先看到任务完成，随后 Agent 又继续执行。前端增加“候选/失败候选”标签只能掩盖症状，不能修正顺序。

### 1.2 Rubric 修正次数错误地成为 Run 终止条件

当前 `max_iterations` 默认值较小。达到上限后中间件移除 `jump_to=model`，当前 Run 结束，Goal 再开启下一 Run。

Rubric 修正次数不应独立终止 Run。真正的 Run 边界应是：

- 全部验收通过；
- 用户暂停或取消；
- HITL 等待跨连接恢复；
- 基础设施异常；
- 单 Run 模型调用预算耗尽。

### 1.3 Agent 执行完成与 Harness 外部验收被混为一谈

本次 lidar/HUD 是 Agent 在执行层读回产物后自行发现并补齐的，这正属于 Agent 的执行与自查职责。若内容仍缺失，Agent 就不应停止工具循环，也不应提交 terminal AIMessage 申请结束。

Readiness 与 Rubric 不负责替 Agent 把工作做完。它们只在 Agent 认为工作已经完成后提供外部完成控制。当前真正的问题是 Agent 每次返回 terminal 文本时，SSE 已经把完整报告发布给用户，随后 Harness 才开始验收。

### 1.4 动态合同跨 Run 单调膨胀

`RunRubricCompiler.merge_contracts()` 会把某一 Run 激活的 pack 合入 Goal contract。一次 Web、SQL 或 Code Tool 的成功调用可能使后续 Run 永久携带相应 criterion。

其中跨 Run 证据策略还不一致：

- Web、Analytics、Artifact verifier 可以读取 `goal_evidence_refs`；
- Code validation 只检查当前 Run 的成功命令；
- Todo 使用 Goal 修订版台账。

结果是同一份 Goal contract 中，不同 criterion 对“前序 Run 已完成的工作”采用不同语义。

### 1.5 工具调用不等于实质工作类型

当前 pack 激活主要由成功 Tool action 驱动。偶然调用一次 Web 工具，未必意味着最终结论依赖网页；读取或验证一次代码文件，也未必意味着本轮需要新增 Code pack。

Pack 应由“结果是否成为本轮或 Goal 的实质证据/产物”激活，而不是仅由工具名称激活。

### 1.6 历史消息被前端验收状态误过滤

Session JSON 中的消息、segments、tool calls 和 interruption 状态仍然存在，但前端曾依据 `verification_state` 永久隐藏历史内容，导致刷新后只剩用户消息。

`verification_state` 是控制面状态，不能成为删除或永久过滤会话历史的依据，也不应产生“候选/失败候选”产品包装。

## 2. 目标状态机

```text
EXECUTING
  Agent 使用工具、修改产物并做自查
      ↓ 返回 terminal AIMessage（仍为 Run 内部消息，不发布）
READINESS_CHECK
  Todo / Artifact / Evidence / Code / Tool Protocol 形式检查
      ├─ failed → 注入结构化 gaps → EXECUTING（同一 Run）
      └─ passed
            ↓
SEMANTIC_GRADING
  task_fulfillment / metric_consistency / time_scope / custom rubric
      ├─ needs_revision → 注入结构化 gaps → EXECUTING（同一 Run）
      └─ satisfied
            ↓
COMMITTING
  固化最终 AIMessage、RunOutcome、Goal decision、证据与产物
            ↓
COMPLETED
  发布最终回复与 done
```

异常分支：

```text
模型预算耗尽 → RunOutcome.budget_exceeded → Goal 决定是否开启下一 Run
用户暂停/取消 → 保留 checkpoint、Session 历史和 Trace，不伪造完成
HITL → Run 保持等待状态，恢复后从原图状态继续
基础设施异常 → RunOutcome.infrastructure_error
```

## 3. 三层职责边界

### 3.1 Agent 执行与自查

Agent 负责查询、编辑、运行验证命令、读回产物以及根据缺口修正。Agent 的自查是提高一次通过率的执行行为，不拥有最终完成权。

对于报告刷新任务，读取目标 HTML/JS、核对全部图表区块、确认 lidar/HUD 等要求是否齐全、核对年份与数据，并在缺失时继续查询和修改，都发生在这一层。只要 Agent 自己的执行计划或 Todo 尚未闭环，就不应返回 terminal AIMessage。

模型返回 terminal AIMessage 只表示“申请进入完成检查”，不等于用户可见的最终回答。

### 3.2 Completion Readiness Gate

Readiness Gate 在调用 LLM Rubric 前执行，全部为可复现的确定性检查。

通用检查：

- Todo 是否全部完成或明确取消；
- 声明交付目标是否真实落盘，hash、size 和目标身份是否一致；
- Web/SQL/RAG 关键结论是否拥有结构化 evidence ref；
- 代码改动是否存在相称的测试、构建或静态检查结果；
- Tool protocol 是否闭合，不能存在缺失 ToolMessage 的调用。

Readiness Gate 未通过时不调用 Rubric，直接将结构化 gaps 返回 Agent，并在同一 Run 继续。

领域内容完整性默认仍由 Agent 执行自查负责。若未来需要对某类报告提供硬保证，可在 P2 增加专用 deterministic verifier，但它不是本轮发布顺序修复的前置条件，也不属于通用 Rubric 的职责。

### 3.3 Rubric 语义验收

只有 Readiness Gate 通过后才执行 LLM Rubric，负责不能完全由代码判断的语义标准：

- 是否真正完成用户目标；
- 指标名称、口径、维度与结论是否一致；
- 趋势分析是否合理反映图表数据；
- 是否遵守用户要求的时间范围；
- 高级用户自定义的语义验收规则。

Rubric 未通过时同样返回 Agent 继续当前 Run，不创建“失败候选”实体。

## 4. 发布与持久化顺序

### 4.1 SSE 发布边界

- Reasoning、Tool、Readiness、Rubric 和修正 Activity 可以继续实时进入 Trace，并统一归入当前 Agent 响应的可折叠“处理过程”；
- 主对话流可以展示正常的阶段性 Agent content，但不得包含“任务已完成、最终产物如下”等终态承诺；
- terminal AIMessage 在验收完成前由 Harness 输出层暂存；
- Readiness 或 Rubric 未通过时，不发布该 terminal message，只把 gaps 写回图状态；
- 验收通过后发送一次 `final_response`，随后发送 `done`；
- 前端不再理解 `candidate / failed candidate` 等概念。

### 4.2 Session 与 checkpoint

- LangGraph checkpoint：同一 Run 内 HITL 和执行循环权威；
- Session JSON：用户消息、最终助手消息、Goal/Run/Todo/permission 等跨请求产品状态权威；
- Trace：保存完整内部模型消息、工具执行、Readiness 与 Rubric 过程，仅用于审计；
- 未通过的内部 AIMessage 不应提前成为 Session 中的最终展示消息；
- Run 被用户停止或异常中断时，既有会话历史必须保留，前端不得按验收状态永久过滤。

### 4.3 原子完成

最终回复、accepted report、RunOutcome 和 Goal 状态应在同一个完成事务中固化。前端只有在收到正常完成事件后才结束运行态并展示最终回复，避免“答案已显示但 Agent 仍运行”。

## 5. Run、Goal、合同与证据作用域

### 5.1 合同分层

- `Declared Goal Contract`：由用户目标、Harness 默认规则和高级自定义规则产生，跨 Run 稳定；
- `Effective Run Contract`：Declared Contract + 本 Run 实质工作激活的 pack + 当前未解决缺口；
- `Goal Aggregate Decision`：基于当前 Goal revision 下所有有效 Run evidence 作最终判断。

后续 Run 不应直接复制上一个 effective contract 再单调扩展，而应从 declared goal contract 重新编译。

### 5.2 Material Activation

Tool 调用成功只产生 activation candidate。满足以下条件之一后才正式激活 pack：

- Tool 结果被最终回答引用；
- Tool 结果进入 `report_payload`；
- Tool 产生或修改目标 artifact；
- Tool 结果被登记为 completion evidence；
- 用户目标明确要求该工作类型。

### 5.3 统一跨 Run 证据策略

每个 criterion 必须显式声明：

- `run_only`：必须由当前 Run 重新完成；
- `goal_inheritable`：同一 Goal revision 下可以继承；
- `artifact_bound`：只要目标 artifact hash 未变化即可继承；
- `freshness_bound`：在时间或数据版本窗口内有效。

建议：

| Criterion | 建议作用域 |
|---|---|
| todo_reconciliation | goal_inheritable，检查当前 Goal 台账 |
| web_evidence_traceability | goal_inheritable，且最终内容必须真实引用 |
| analytics_evidence_traceability | goal_inheritable，绑定数据源/result identity |
| artifact_delivery | artifact_bound，重新核验目标 hash |
| code_validation | artifact_bound；代码 hash 不变可继承，变化后必须重跑 |
| report_data_consistency | artifact_bound，绑定 report payload 与目标 hash |
| task_fulfillment / metric_consistency / time_scope | 最终接受时重新评审 |

## 6. 预算与循环策略

- 删除“Rubric 最大修正轮数决定 Run 结束”的语义；
- 统一由 `run_model_call_limit` 控制同一 Run 的执行与修正总预算；
- Grader infrastructure error 可配置有限重试，但不能被解释为业务验收失败；
- 连续出现完全相同 gaps 时可触发 stagnation 保护，结果为 blocked/infrastructure decision，而不是伪装成完成；
- 仅当 Run 预算耗尽且 Goal 仍有总预算时，Lifecycle Control 才开启下一 Run。

## 7. 前端产品行为

- 运行中统一显示“Agent 处理中”或当前有价值的执行动作，不在主界面强调“第 N 轮验收、验收失败、正在反复修正”；
- 思考、工具调用、Readiness、Rubric 与内部修正统一放入可折叠“处理过程”，保持同一套时间线和交互；
- 不显示“候选版本、失败候选、验收后点击查看”等卡片；
- 不因 verification report 自动打开右侧抽屉；
- 最终回答只有在 accepted completion 后一次性发布；
- 最终回答中的验收信息默认压缩为一句自然语言，例如“验证通过：后端 24 项测试通过，前端生产构建通过”；
- 不在正常对话流展示 Rubric 条目、修正轮次、grader 原文或内部 completion report；这些只保存在 Trace，并由用户主动查看；
- 历史消息按 Session JSON 恢复，不依据 `verification_state` 删除；
- 中断或异常显示对应状态，但不得把内部草稿冒充最终完成结果；
- Trace/验收抽屉仍可由用户主动查看详细过程。

## 8. 实施优先级

### P0：修正 Run 内闭环与发布顺序

1. 在 DeepAgents SSE 适配层截住 terminal AIMessage，验收前不发布最终 token、不写展示消息；
2. 保持 `after_agent` 为完成拦截点，严格执行 `Readiness → Rubric → jump_to model / commit`；
3. Readiness/Rubric 失败统一 `jump_to=model`，复用同一 `run_id / query_id / checkpoint`；
4. `max_iterations` 不再终止业务 Run，终止权归 Run budget、用户控制、HITL 或基础设施状态；
5. 验收通过后原子固化最终消息、报告、RunOutcome 和 Goal 状态，再发送 `final_response → done`；
6. 前端删除候选分类逻辑，只按 Activity 与最终完成事件渲染；最终验收结果只显示一句摘要，历史消息按 Session 原样恢复；
7. 强化 Agent 的结束纪律：执行计划或 Todo 未闭环时不得返回 terminal AIMessage；报告内容自查仍在 Model/Tools 循环内完成。
8. 将 Readiness/Rubric 事件接入现有思考/工具调用折叠时间线，主界面统一保持“Agent 处理中”。

### P1：修正合同和跨 Run 证据

1. 拆分 declared goal contract 与 effective run contract；
2. 将 activation 改为 material activation；
3. 为所有 deterministic criterion 增加 evidence scope；
4. 统一 Web/Analytics/Artifact/Code 的继承与失效规则；
5. 迁移现有 Goal contract，避免历史 pack 永久污染后续 Run。

### P2：分析产物内容硬保证（可选增强）

1. 定义版本化 `report_payload` schema；
2. 建立 SQL result → payload → chart key → artifact hash 的 lineage manifest；
3. 实现 `report_data_consistency` deterministic verifier；
4. 验证图表数据、文本结论和年份范围；
5. 在 Harness 设置中允许高级用户增加领域级内容规则。

## 9. 验收测试矩阵

| 场景 | 预期结果 |
|---|---|
| HTML 要求 lidar/HUD，但 JS 缺 key | Agent 自查继续查询和修改，不提交 terminal AIMessage，不进入 Harness 验收 |
| Todo 未收口 | Readiness 失败；同一 Run 修正 |
| 文件未落盘 | Readiness 失败；不得发布最终回复 |
| 文件齐全但趋势结论与数据不一致 | Readiness 通过后 Rubric 失败；同一 Run 修正 |
| Rubric 连续两次未通过 | 不结束 Run；继续消耗 Run model-call budget |
| Run 模型预算耗尽 | 当前 Run budget_exceeded；Goal 在总预算允许时开启下一 Run |
| Run-1 Web 证据有效，Run-2 未重新检索 | 可按 evidence scope 继承，不强制重复联网 |
| 代码 hash 未变化 | 可继承验证证据；hash 变化后必须重新测试 |
| 用户在验收前停止 | 不发布最终完成；历史和 Trace 保留 |
| 刷新或后端重启 | Session 历史、Goal、Todo、RunOutcome 恢复一致 |
| 验收通过 | 最终回复、accepted report、RunOutcome 与 Goal achieved 同步出现 |
| 内部经历多轮 Readiness/Rubric 修正 | 正常对话流只表现为仍在运行；最终通过后仅显示一句验收摘要 |
| 用户展开“处理过程” | 可查看工具调用、检查、Rubric 和修正事件，但不会看到伪终态候选报告 |

## 10. 主要改动位置

- `backend/graph/deepagents_manager.py`：SSE 缓冲、消息发布、snapshot 与 Run 内回跳；
- `PuddingClawRubricMiddleware`：Readiness/Rubric 顺序和修正终止条件；
- `backend/harness/deterministic_checks.py`：内容完整性与 evidence scope；
- `backend/harness/rubric_compiler.py`：Declared/Effective contract 和 material activation；
- `backend/harness/coordinators.py`：Goal aggregate decision 与跨 Run 边界；
- `backend/graph/session_manager.py`：最终消息与 RunOutcome 原子持久化；
- `frontend/src/lib/store.tsx`：完成事件与运行状态同步；
- `frontend/src/components/chat/ChatMessage.tsx`：移除候选包装并恢复历史渲染。

## 11. 完成定义

本轮迭代只有同时满足以下条件才算完成：

- 用户不会在验收前看到“任务完成”；
- Readiness 缺口和 Rubric 缺口均在同一 Run 内自动修正；
- Agent 未完成 lidar/HUD 等内容自查时不会申请结束；
- Rubric 次数不再独立终止 Run；
- 跨 Run contract 不再因偶然工具调用永久膨胀；
- 所有 criterion 的证据继承规则一致且可解释；
- 历史消息刷新后不消失；
- 最终回复出现时，Run 和 Goal 已经同步收口；
- 用户默认只看到一句验收摘要，不看到内部候选、Rubric 明细和反复修正过程。
- 用户主动展开“处理过程”时仍可审计完整验收事件，折叠状态下不会感知到独立且漫长的验收流程。
