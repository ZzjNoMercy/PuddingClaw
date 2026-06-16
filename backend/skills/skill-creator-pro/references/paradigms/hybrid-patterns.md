# 混合范式使用指南

> 多数强 Skill 是混合的，但只有一个范式主导控制层

## 核心原则

实际的高质量 Skill 很少是纯粹的单一范式。大多数成熟 Skill 会混合使用 2-3 个范式，但关键在于：**只有一个范式决定 SKILL.md 的结构**。这个主导范式决定了文件的骨架，其他范式作为辅助特性嵌入其中。

判断主导范式的方法很简单：**哪个范式的结构特征定义了 SKILL.md 的顶层组织？**

---

## 常见混合组合

### 1. Operator + Scout

**场景**：需要在不确定环境中执行确定性操作。

**混合方式**：
- 主导：Operator（步骤序列 + scripts/）
- 辅助：Scout（在操作前添加侦察步骤）

**结构影响**：
```
SKILL.md
├── Recon Phase（Scout 贡献）
│   ├── 检查环境状态
│   ├── 验证前置条件
│   └── 确认安全条件
├── Execute Phase（Operator 主导）
│   ├── Step 1: ...
│   ├── Step 2: ...
│   └── Step 3: ...
└── Verify Phase（Scout 贡献）
    └── 验证执行结果
```

**典型案例**：bugfix-loop（先侦察 Bug 环境，再执行修复操作，最后验证修复结果）

---

### 2. Architect + Partner

**场景**：需要与用户协作完成系统设计。

**混合方式**：
- 主导：Architect（Phase 结构 + 设计门禁 + assets/）
- 辅助：Partner（在每个 Phase 结束时插入确认检查点）

**结构影响**：
```
SKILL.md
├── Phase 1: 需求分析
│   ├── 设计步骤...
│   └── ✅ 确认检查点（Partner 贡献）
├── Phase 2: 架构设计
│   ├── 设计步骤...
│   └── ✅ 确认检查点（Partner 贡献）
└── Phase 3: 模板输出
    ├── 输出步骤...
    └── ✅ 最终确认（Partner 贡献）
```

**典型案例**：prd-generator（按 Phase 自顶向下设计 PRD，每个 Phase 结束需用户确认才继续）

---

### 3. Navigator + Operator

**场景**：先查找正确的操作路径，再执行操作。

**混合方式**：
- 主导：Navigator（决策树 + references/）
- 辅助：Operator（路由到正确路径后执行操作）

**结构影响**：
```
SKILL.md
├── Decision Tree（Navigator 主导）
│   ├── 条件 A → 路径 A
│   ├── 条件 B → 路径 B
│   └── 默认 → 路径 C
└── Execution（Operator 贡献）
    ├── 路径 A: scripts/path-a.sh
    ├── 路径 B: scripts/path-b.sh
    └── 路径 C: scripts/path-c.sh
```

**典型案例**：content-strategy（先根据内容类型路由到策略，再按策略执行内容生产操作）

---

### 4. Orchestrator + Partner

**场景**：多 Agent/多阶段工作流中需要人工介入。

**混合方式**：
- 主导：Orchestrator（模式选择 + Handoff + 状态管理）
- 辅助：Partner（在关键 Handoff 点插入人工确认）

**结构影响**：
```
SKILL.md
├── Mode Selection（Orchestrator 主导）
├── Phase 1 → Phase 2 Handoff
│   ├── 自动化交接条件
│   └── ✅ 人工审查点（Partner 贡献）
├── Phase 2 → Phase 3 Handoff
│   ├── 自动化交接条件
│   └── ✅ 人工确认点（Partner 贡献）
└── Resume Rules
```

**典型案例**：opus-sonnet-collab（Opus 和 Sonnet 之间的 Handoff 需要人工确认任务分配）

---

## 辅助范式的影响总结

每个范式作为辅助角色时，会向 SKILL.md 注入特定的结构元素：

| 辅助范式 | 注入的结构元素 | 注入位置 |
|---------|---------------|---------|
| **Scout** | Recon 规则、首次检查命令、停止条件 | 操作步骤之前 |
| **Partner** | 确认检查点、开场协议 | Phase/步骤之间 |
| **Operator** | scripts/ 目录、验证步骤 | 决策/设计之后 |
| **Orchestrator** | 模式选择、Handoff 逻辑 | 顶层控制流 |
| **Navigator** | 决策树、references 路由 | 操作之前的查找阶段 |
| **Philosopher** | 宪法约束、原则检查 | 贯穿整个工作流 |

---

## 确定主导范式的方法

回答以下问题，得分最高的就是主导范式：

### 问题 1: SKILL.md 的顶层结构是什么？

| 顶层结构 | 主导范式 |
|---------|---------|
| 步骤序列 + scripts/ | **Operator** |
| 决策树 + references/ | **Navigator** |
| Phase 结构 + assets/ | **Architect** |
| 开场协议 + 确认点 | **Partner** |
| 模式选择 + Handoff | **Orchestrator** |
| 侦察流程 + 安全规则 | **Scout** |
| 宪法 + 工作流分离 | **Philosopher** |

### 问题 2: Skill 的核心价值是什么？

| 核心价值 | 主导范式 |
|---------|---------|
| 可靠地执行操作 | **Operator** |
| 快速找到正确信息 | **Navigator** |
| 产出可复用的系统 | **Architect** |
| 结构化的人机协作 | **Partner** |
| 协调多工具/多阶段 | **Orchestrator** |
| 安全地探索未知环境 | **Scout** |
| 建立行为准则 | **Philosopher** |

### 问题 3: 如果删除这个范式的特征，Skill 还能工作吗？

不能工作 → 这就是主导范式。能工作但体验降级 → 这是辅助范式。

---

## 选错主导范式的后果

### 案例 1: 把 Operator 当 Architect

**场景**：一个 PDF 转换工具，被设计成了 Architect 范式。
**后果**：
- SKILL.md 充满了"设计门禁"和"Phase 结构"，但实际只需要一个线性脚本
- assets/ 里放了空白模板，完全没有使用
- Agent 花时间走"设计流程"，实际上应该直接执行转换命令
**修复**：改为 Operator 范式，步骤序列 + scripts/

### 案例 2: 把 Navigator 当 Philosopher

**场景**：一个技术文档查找器，被设计成了 Philosopher 范式。
**后果**：
- SKILL.md 里定义了大量"查找原则"，但没有实际的决策树
- Agent 知道"应该找到正确的文档"这个原则，但不知道怎么找
- 缺少路由逻辑，每次查找都需要 Agent 自行推断路径
**修复**：改为 Navigator 范式，决策树 + references/

### 案例 3: 把 Scout 当 Operator

**场景**：一个安全审计工具，被设计成了 Operator 范式。
**后果**：
- SKILL.md 定义了固定的检查步骤序列，但实际环境千差万别
- Agent 在未知环境中机械执行预定义步骤，错过关键问题
- 没有 Anti-Guessing 规则，Agent 基于假设做出错误判断
**修复**：改为 Scout 范式，侦察优先 + 证据驱动 + 停止条件
