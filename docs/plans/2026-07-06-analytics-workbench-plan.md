# 智能问数工作台开发计划

日期：2026-07-06

## 背景

知识库已经覆盖 PDF / Markdown / 多模态 RAG。Excel / CSV / TSV 进入 Pandas Query Engine 后，继续把它们塞在“知识库”页面会混淆两类能力：

- 知识库：文档资产、全文/语义检索、多模态 RAG。
- 智能问数：结构化数据资产、表格 profile、数据模型、指标口径、专门的数据分析 Agent。

因此新增与“知识库”并列的“智能问数”板块，承载 Table Asset Catalog + Profile 生成，以及后续数据模型、指标、Vanna/NL2SQL。

## 核心层级修正

### 数据资产层

对象：Excel / CSV / TSV / 数据库表。

职责：

- 记录资产来源、文件、sheet、列、行数、采样、数据类型。
- 生成机器可读 `profile.json`。
- 生成资产级 `reference.md`，描述这张表自身，而不是定义全局指标。

### 语义模型层

对象：Data Model。

职责：

- 选择多个数据资产作为一个可问数的数据模型。
- 定义表之间关系、默认时间字段、主键、事实表、维度表。
- 屏蔽底层 Excel / CSV / 数据库差异。

### 指标层

对象：Measure。

职责：

- 定义业务指标口径，例如周销量、环比、配置率。
- 指明适用的数据模型、Excel/CSV 资产或结构化数据库。
- 保存自然语言定义、公式、字段需求、示例问法。
- 不挂在单个 Excel 文件下面。

### 分析包层

对象：Analysis Package。

职责：

- 打包一个数据模型 + 一组指标 + 业务 reference。
- 用户选择后即可问数。

## 本轮目标

- [ ] 修正 `docs/知识库与结构化数据统一架构方案.md` 中把 measures/examples 混入表格资产层的描述。
- [ ] 新增前端 `/analytics` 页面。
- [ ] 侧边栏新增“智能问数”，与“知识库”并列。
- [ ] 新增后端 Table Asset Catalog API，前端真实同步已导入表格资产。
- [ ] 页面骨架包含：
  - 表格资产 catalog。
  - Profile 生成状态。
  - 数据模型。
  - 指标库。
  - 专门问数 Agent 入口。
- [ ] 上传入口仍保持在“知识库”统一入口；智能问数只消费已导入的 Excel / CSV / TSV。

## 后续阶段

1. 将当前文件扫描式 Table Asset Catalog 升级为 PostgreSQL 持久 catalog。
2. 导入 Excel/CSV 时自动写入 table asset 记录。
3. 后台生成 profile/reference。
4. Pandas tool 从 catalog 选表，而不是扫描 `/knowledge`。
5. 新增数据模型与指标管理 API。
6. 专门问数 Agent 读取模型/指标/profile 后选择 pandas 或 Vanna。
