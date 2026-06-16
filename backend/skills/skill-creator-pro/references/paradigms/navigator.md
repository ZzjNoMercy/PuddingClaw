# Navigator（路由查找）

> 占比：19% — 第二常见的 Skill 范式

## 定义

**核心职责**：Route the agent to the right information.

**适用场景**：
- 大型知识域导航（技术文档库、API 参考手册）
- 分支文档查找（根据条件选择不同的文档路径）
- 策略查找（根据场景匹配最佳实践）
- 多层级知识组织（分类→子类→具体条目）

Navigator 的本质是一个知识路由器。它不直接提供答案，而是根据用户的需求，快速定位到正确的信息源。SKILL.md 是路由表，references/ 是知识仓库。

---

## SKILL.md 结构指导

### 保留在 SKILL.md 中的内容

- **Trigger Boundary**：什么情况下激活此 Skill
- **Routing Logic**：根据输入条件，路由到哪个 reference 文件
- **Decision Tree**：核心决策树（这是 Navigator 的灵魂）

### 推入 `references/` 目录的内容

- 领域详情（具体知识条目、详细说明）
- 分类索引（按主题/场景/难度分类的内容）
- 深度参考（案例分析、扩展阅读）

```
skill-name/
├── SKILL.md          # 决策树 + 路由逻辑 + 触发边界
└── references/
    ├── category-a/   # 按主题分类
    │   ├── topic-1.md
    │   └── topic-2.md
    ├── category-b/
    │   └── topic-3.md
    └── index.md      # 分类索引（可选）
```

---

## 关键设计原则

### 1. Decision Tree 是灵魂

Navigator 的核心价值在于决策树。SKILL.md 必须包含一个清晰的决策树（或路由表），让 Agent 能在 2-3 步判断内找到目标信息。

```
用户需求 → 判断条件 A？
  ├── 是 → references/category-a/topic-1.md
  └── 否 → 判断条件 B？
       ├── 是 → references/category-b/topic-3.md
       └── 否 → 默认路径
```

### 2. 最小主文件（Lean SKILL.md）

SKILL.md 只放路由逻辑，不放具体知识。如果 SKILL.md 超过 200 行，说明有内容应该被推入 references/。

### 3. 引用导航清晰

每个 reference 文件必须有明确的命名和分类，Agent 通过文件名就能判断是否是目标文件。避免模糊命名（如 `notes.md`、`misc.md`）。

### 4. 分层检索

支持从粗到细的检索路径：先定位分类 → 再定位具体文件 → 再定位文件内的具体段落。

---

## 反模式警告

### 1. SKILL.md 变百科全书

**症状**：把所有知识都塞在 SKILL.md 中，文件超过 500 行。
**后果**：Agent 处理大量无关信息，响应变慢，路由精度下降。
**修复**：提取知识条目到 references/，SKILL.md 只保留决策树和路由表。

### 2. 隐藏分支

**症状**：决策树不完整，某些路由条件需要 Agent 自行推断。
**后果**：Agent 可能走错路径，返回错误信息。
**修复**：确保决策树覆盖所有已知分支，包括默认/兜底路径。

### 3. references/ 未分类

**症状**：所有 reference 文件平铺在一个目录下，没有分类结构。
**后果**：文件数量增长后，路由效率急剧下降。
**修复**：按主题/场景/层级组织 references/ 子目录。

---

## 典型代表

### find-skills（Skill 查找器）

- 根据用户描述的需求，路由到最匹配的 Skill
- 决策树基于 Skill 的功能分类和关键词匹配
- references/ 存储所有可用 Skill 的元数据和描述
- 典型 Navigator：纯路由，不执行操作

### para-second-brain（PARA 第二大脑）

- 根据信息类型（项目/领域/资源/归档），路由到正确的存储位置
- 决策树基于 PARA 框架的分类规则
- references/ 包含分类标准和示例

### content-strategy（内容策略导航）

- 根据内容目标和受众，路由到最佳内容策略
- 多层决策：先判断内容类型 → 再判断发布渠道 → 再匹配策略模板
- references/ 按渠道和类型分类组织

---

## 与相邻范式的区别

### Navigator vs Scout

| 维度 | Navigator | Scout |
|------|-----------|-------|
| 信息来源 | **已知**的知识库 | **未知**的环境 |
| 动作模式 | 查找 + 路由 | 侦察 + 发现 |
| 确定性 | 高（路由表预定义） | 低（结果未知） |
| 输出 | 指向已有文档/答案 | 新发现的环境信息 |

### Navigator vs Operator

| 维度 | Navigator | Operator |
|------|-----------|----------|
| 核心动作 | **查找**信息 | **执行**操作 |
| 产出物 | 定位到的知识/文档 | 处理后的文件/数据 |
| 副作用 | 无（只读） | 有（修改文件系统） |
