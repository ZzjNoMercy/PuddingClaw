# Operator（工具链封装）

> 占比：28% — 最常见的 Skill 范式

## 定义

**核心职责**：Execute a toolchain reliably.

**适用场景**：
- 重复文件操作（PDF 转换、格式批处理）
- 数据转换流水线（ETL、格式迁移）
- 批处理任务（批量重命名、批量验证）
- 确定性流水线（构建、部署、发布）

Operator 的本质是把一组确定性操作封装为可靠的自动化流程。Agent 在执行时不需要创造性判断，只需要按照预定义的步骤精确执行。

---

## SKILL.md 结构指导

### 保留在 SKILL.md 中的内容

- **Operation Boundary**：明确操作的输入/输出边界
- **Input Requirements**：输入格式、必需字段、前置条件
- **Failure Checks**：每步操作的失败检测与恢复策略
- **Tool Routing**：不同输入条件下调用哪些工具

### 推入 `scripts/` 目录的内容

- 重复执行的操作脚本（shell、python）
- 脆弱操作（涉及文件系统、网络调用的步骤）
- 可独立测试的工具链片段

```
skill-name/
├── SKILL.md          # 操作边界 + 路由 + 失败检查
├── scripts/
│   ├── convert.sh    # 核心转换脚本
│   ├── validate.py   # 输入验证
│   └── cleanup.sh    # 清理脚本
└── references/       # 格式规范、工具文档（如有）
```

---

## 关键设计原则

### 1. 脚本优先（Script-First）

任何超过 3 行的操作序列，都应该提取为独立脚本。SKILL.md 负责调度脚本，而不是内联大段 bash/python 代码。

### 2. 确定性操作（Deterministic Execution）

Operator 的每一步必须产生可预测的结果。如果某步操作的结果不确定（如依赖网络状态），必须在 SKILL.md 中声明失败检测和重试策略。

### 3. 严格验证（Validate Before & After）

- **前置验证**：执行前检查输入是否满足要求
- **后置验证**：执行后确认输出是否符合预期
- 每个关键步骤之间插入断言检查

### 4. 幂等性（Idempotent When Possible）

相同输入重复执行应产生相同结果，不应产生副作用累积。

---

## 反模式警告

### 1. 操作写成教程

**症状**：SKILL.md 花大量篇幅解释"为什么要这样做"，而不是直接描述"做什么"。
**后果**：Agent 被教学性文字干扰，执行效率下降。
**修复**：教学内容移入 `references/`，SKILL.md 只保留操作指令。

### 2. 没有 scripts/ 目录

**症状**：所有操作步骤都内联在 SKILL.md 的 markdown 中。
**后果**：操作不可独立测试、不可复用、难以维护。
**修复**：将超过 3 行的操作提取为脚本文件。

### 3. 缺少错误处理

**症状**：只描述 happy path，没有失败检测和恢复策略。
**后果**：遇到异常时 Agent 不知道如何处理，可能静默失败或产生错误输出。
**修复**：为每个关键步骤添加 failure check 和 fallback 策略。

---

## 典型代表

### pdf（PDF 转换工具链）

- 接收多种输入格式 → 统一转换为目标 PDF
- 使用 scripts/ 封装转换命令
- 输入验证 → 转换 → 输出验证的三段式结构
- 典型 Operator：确定性流水线 + 严格验证

### courseware-pipeline（课件生产流水线）

- 原始素材 → 结构化 → 排版 → 输出的多步流水线
- 每步操作都有明确的输入/输出格式约束
- 脚本化程度高，SKILL.md 专注于步骤调度

### bugfix-loop（Bug 修复循环）

- 检测 → 定位 → 修复 → 验证的循环操作
- 包含失败检测和重试逻辑
- 操作边界清晰：输入是 bug 描述，输出是修复后的代码

---

## 与相邻范式的区别

### Operator vs Architect

| 维度 | Operator | Architect |
|------|----------|-----------|
| 核心动作 | **执行**操作 | **输出**系统/框架 |
| 产出物 | 处理后的文件/数据 | 设计文档/模板/规范 |
| 创造性要求 | 低（确定性执行） | 高（系统设计） |
| 典型关键词 | convert, process, batch, validate | design, generate, architect, create |

### Operator vs Orchestrator

| 维度 | Operator | Orchestrator |
|------|----------|-------------|
| 工具数量 | 单一工具链 | 多工具/多 Agent 协调 |
| 复杂度 | 线性流水线 | 分支/并行/状态管理 |
| 控制层 | 步骤序列 | 模式选择 + 交接逻辑 |
