# Orchestrator（多工具协调）

> 占比：13% — 复杂工作流的核心范式

## 定义

**核心职责**：Coordinate multiple tools, agents, or phases.

**适用场景**：
- 多 Agent 工作流（不同 Agent 负责不同子任务）
- 多阶段流水线（Phase 间有依赖关系和状态传递）
- 模式切换（根据上下文选择不同的执行模式）
- 故障恢复（检测失败并切换到备用路径）

Orchestrator 的本质是调度中心。它不直接执行具体操作，而是决定由谁来做、什么时候做、做完后交给谁。核心能力是状态管理和控制流。

---

## SKILL.md 结构指导

### 保留在 SKILL.md 中的内容

- **Mode Selection**：如何根据输入选择执行模式
- **Orchestration Route**：各模式下的执行路径
- **Handoff Logic**：工具/Agent/Phase 之间的交接规则
- **Resume Rules**：中断后如何恢复执行

### 推入 `references/` 目录的内容

- 角色详情（每个 Agent/工具的能力描述）
- Phase 深层逻辑（每个阶段的详细执行规范）
- 状态定义（状态枚举和转换规则）

```
skill-name/
├── SKILL.md          # 模式选择 + 编排路由 + 交接逻辑 + 恢复规则
└── references/
    ├── roles/        # 各角色/Agent 详情
    ├── phases/       # 各 Phase 详细规范
    └── states.md     # 状态定义和转换规则
```

---

## 关键设计原则

### 1. 显式边界（Explicit Boundaries）

每个工具/Agent/Phase 的职责边界必须显式定义。禁止隐式假设"某个 Agent 应该能处理这个"。

### 2. 故障恢复（Failure Recovery）

Orchestrator 必须为每个关键节点定义故障处理策略：
- 检测条件：什么信号表示失败
- 恢复策略：重试 / 回退 / 切换备用路径
- 升级策略：何时放弃自动恢复，请求人工介入

### 3. 状态管理（State Management）

跨 Phase 的状态必须显式传递，不依赖隐式上下文。关键状态包括：
- 当前所处的 Phase/Mode
- 已完成的步骤
- 累积的中间产出

### 4. 模式选择（Mode Selection）

当存在多种执行路径时，SKILL.md 必须提供清晰的模式选择逻辑：
```
输入分析 → 模式 A 条件满足？
  ├── 是 → 执行模式 A 路径
  └── 否 → 模式 B 条件满足？
       ├── 是 → 执行模式 B 路径
       └── 否 → 默认模式
```

### 5. Handoff 协议

工具/Agent 之间的交接必须定义：
- 交接触发条件
- 交接时传递的数据/状态
- 接收方的前置检查

---

## 反模式警告

### 1. 巨型 SKILL.md

**症状**：把所有角色的详细逻辑、所有 Phase 的具体步骤都写在 SKILL.md 中。
**后果**：文件过长，Agent 难以快速定位当前需要的信息。
**修复**：SKILL.md 只保留路由和协调逻辑，详细实现推入 references/。

### 2. 隐式 Handoff

**症状**：Phase 之间的交接没有显式定义，依赖 Agent 的上下文理解。
**后果**：状态丢失、重复工作、执行路径不可预测。
**修复**：为每个 Handoff 点定义触发条件、传递数据和接收检查。

### 3. 没有故障恢复

**症状**：只定义了 happy path，没有失败检测和恢复策略。
**后果**：任何一个环节失败都会导致整个流程中断。
**修复**：为每个关键节点添加故障检测、恢复策略和升级路径。

---

## 典型代表

### opus-sonnet-collab（Opus-Sonnet 协作编排）

- 模式选择：根据任务复杂度选择 Opus 主导或 Sonnet 主导
- 多 Agent 协调：Opus 做架构设计，Sonnet 做实现
- Handoff 逻辑：Opus 输出设计文档 → Sonnet 接收并实现 → Opus 审查
- 故障恢复：Sonnet 实现卡住时升级给 Opus
- 典型 Orchestrator：多角色 + 显式 Handoff + 模式切换

### workflow-orchestrator（工作流编排器）

- 管理多阶段工作流的执行顺序
- 支持并行和串行两种执行模式
- 状态管理：跟踪每个阶段的完成状态
- Resume 规则：中断后从最后完成的阶段恢复

### switch-model（模型切换编排）

- 根据任务类型选择最佳模型
- 模式选择：代码任务→Coder模型 / 对话任务→Chat模型
- Handoff：模型切换时保持上下文连续性

---

## 与相邻范式的区别

### Orchestrator vs Operator

| 维度 | Orchestrator | Operator |
|------|-------------|----------|
| 管理对象 | 多工具/多 Agent | 单一工具链 |
| 复杂度 | 高（分支/并行/状态） | 低（线性流水线） |
| 控制层 | 模式选择 + Handoff | 步骤序列 |
| 故障处理 | 切换路径/升级 | 重试/中止 |

### Orchestrator vs Partner

| 维度 | Orchestrator | Partner |
|------|-------------|---------|
| 协调对象 | 工具/Agent | 人类用户 |
| 交互频率 | 自动化为主 | 人机对话为主 |
| 控制权 | Agent 主导调度 | 人类主导方向 |
| 检查点 | 自动化门禁 | 人工确认点 |
