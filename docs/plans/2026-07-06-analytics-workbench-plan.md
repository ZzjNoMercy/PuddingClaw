# 智能问数工作台开发计划

日期：2026-07-06

## 背景

知识库已经覆盖 PDF / Markdown / 多模态 RAG。Excel / CSV / TSV 进入 Pandas Query Engine 后，继续把它们塞在“知识库”页面会混淆两类能力：

- 知识库：文档资产、全文/语义检索、多模态 RAG。
- 智能问数：AI Native BI，管理数据资产、数据模型、度量值、分析模型包和专门的问数 Agent。

因此新增与“知识库”并列的“智能问数”板块。上传入口仍在知识库统一完成；智能问数负责把已导入的表格文件、数据库源和后续结构化数据组织成 BI 资产。

核心原则：

1. 数据模型 reference、度量值都是类似 Markdown 的自然语言文本，Agent 像读 Skill reference 一样读取它们。
2. 不优先做传统 BI 的强关联建模；关系、主键、字段映射只在问数效果不稳定或业务确实需要时作为补充。
3. 度量值是顶层资产，可以被多个数据模型复用。
4. 产品术语向 BI 靠齐：数据资产、数据模型、度量值、维度、分析模型包、问数 Agent。

## 核心层级修正

### 数据资产层

对象：Excel / CSV / TSV / 数据库源 / 数据库表。

职责：

- 记录资产来源、文件、sheet、列、行数、采样、数据类型。
- 生成机器可读 `profile.json`。
- 生成资产级 `reference.md`，描述这张表自身，而不是定义全局指标。
- 在 UI 中统一称为“数据资产”，不要只叫“表格资产”。

### 数据模型层

对象：Data Model。

职责：

- 选择多个数据资产作为一个可问数的数据模型。
- 保存模型 reference，用自然语言说明这组数据如何理解、适合回答哪些问题。
- 可选补充默认时间字段、主键、关系、事实表/维度表等结构提示。
- 屏蔽底层 Excel / CSV / 数据库差异。
- 第一版以“资产选择 + 模型 reference”为主，不做重型关系建模器。

### 度量值层

对象：Measure。

职责：

- 定义业务度量值口径，例如周销量、环比、配置率。
- 作为顶层 BI 资产存在，可以被多个数据模型复用。
- 可选指明适用的数据模型、Excel/CSV 资产或结构化数据库，但不强绑定。
- 保存自然语言定义、公式、字段需求、示例问法。
- 不挂在单个 Excel 文件下面。

### 分析包层

对象：Analysis Package。

职责：

- 打包一个数据模型 + 一组指标 + 业务 reference。
- 用户选择后即可问数。

## 本轮目标

### 2026-07-07 本轮推进

- [x] 收口“数据资产”信息架构：把表格文件和数据库源放在同一个资产区域。
- [x] 智能问数页面接入已配置数据库源，默认显示项目 PostgreSQL。
- [x] 智能问数页面统计项从“表格资产”改成“数据资产 / 表格文件 / 数据库源 / Profile”。
- [x] 右侧入口统一为“数据模型 / 度量值 / 问数 Agent”，文案遵循 AI Native BI 原则。
- [x] 将智能问数页面改为二级工作台布局：左侧为“数据资产 / 数据模型 / 度量值 / 问数 Agent”导航，中间呈现当前栏目内容，提高信息密度。
- [x] 当前阶段暂不新建重型关系建模器，数据库源只作为数据资产展示和后续模型选择来源。
- [x] 数据资产页的数据库源“管理”改为本页弹窗编辑，可测试连接、读取/选择表并保存。

- [x] 修正 `docs/知识库与结构化数据统一架构方案.md` 中把 measures/examples 混入表格资产层的描述。
- [x] 新增前端 `/analytics` 页面。
- [x] 侧边栏新增“智能问数”，与“知识库”并列。
- [x] 新增后端表格资产 Catalog API，前端真实同步已导入表格资产。
- [x] 将前端“表格资产 Catalog”改成“数据资产”，统一呈现表格资产和数据库源。
- [x] 将“指标库 / 指标口径”统一改成“度量值”。
- [x] 数据库源编辑弹窗只保留连接配置、测试连接、读取表和选择表。
- [x] 表级 Vanna 训练从数据库源编辑弹窗拆出，改由数据资产页具体数据库表入口打开。
- [ ] 将“数据模型”和“度量值”设计为同级目录/卡片，不把度量值挂到某个表格下面。
- [ ] 补齐问数 Agent 入口：独立于普通 Agent，默认使用数据模型、度量值和 profile/reference。
- [ ] 页面骨架包含：
  - 数据资产 catalog。
  - Profile 生成状态。
  - 数据模型。
  - 度量值。
  - 专门问数 Agent 入口。
- [ ] 上传入口仍保持在“知识库”统一入口；智能问数只消费已导入的 Excel / CSV / TSV。

## 后续阶段

1. 将当前文件扫描式数据资产 Catalog 升级为 PostgreSQL 持久 catalog。
2. 导入 Excel/CSV 时自动写入 table asset 记录。
3. 后台生成 profile/reference。
4. Pandas tool 从 catalog 选表，而不是扫描 `/knowledge`。
5. 新增数据模型与度量值管理 API。
6. 专门问数 Agent 读取数据模型/度量值/profile/reference 后选择 pandas 或 Vanna。

## 2026-07-07：数据资产持久 Catalog 迁移

目标：把智能问数的数据资产列表从“扫描知识库目录 + 临时读取数据库源”升级为 PostgreSQL 持久台账。

- [x] 新增 `knowledge_table_assets` 表，记录 Excel / CSV / TSV / sheet 级资产。
- [x] Analytics API 优先读取 PostgreSQL catalog；catalog 为空时做一次扫描兜底注册，避免已有导入文件消失。
- [x] Profile 生成后把行数、列数、字段摘要、profile 状态同步回 catalog。
- [x] Excel / CSV / TSV 导入任务完成后自动注册 table asset。
- [x] Pandas 问数工具优先从 catalog 选择表，减少重复扫描和 `.tasks` 干扰。
- [x] 保留 `/knowledge/.puddingclaw/table_profiles/*.profile.json` 作为 profile 文件缓存，不把大 JSON 强塞 UI 列表。
- [x] 修复首次 catalog 扫描的并发 upsert 问题：重复打开页面或重复扫描时，不再因为相同 `asset_id` 触发 PostgreSQL unique violation。

## 2026-07-07：Vanna 默认 NL2SQL 能力包迁移

目标：把本地已验证的 `/Users/pet/Code/AI/Agent/实战项目/NL2SQL` 中的 Vanna fork 迁入 PuddingClaw，作为智能问数面向结构化数据库的默认 NL2SQL 能力。

第一性原理：

- Pandas 负责文件型/临时表分析，Vanna 负责结构化数据库 NL2SQL。
- Vanna 不作为独立 FastAPI 服务启动，而是作为 PuddingClaw 后端 runtime / tool 能力接入。
- Vanna 是智能问数的全局 NL2SQL 能力包：运行配置放在后端 `config.json -> vanna`，复用 PuddingClaw 已有数据库源、Milvus、LLM 网关和 embedding 配置，不新增 `.env` 配置入口。
- Vanna 训练资料都是文本：DDL、业务说明、SQL 问法示例和后续 entity 文档统一走文本 embedding，默认复用 `fallback_embedding` 的 `text-embedding-v4`，不走多模态 embedding。
- 这里的 `fallback_embedding` 不是“失败兜底”的临时方案，而是 PuddingClaw 统一文本 embedding 配置入口。当前实际配置为 `text-embedding-v4`，`vanna.embedding.reuse=fallback_embedding`；如果要走 Higress，只需要把 `fallback_embedding.base_url` 或 `vanna.embedding.base_url` 配成 Higress 的 OpenAI-compatible `/v1` 根路由。
- 数据模型 reference、度量值、问法示例会进入 Vanna 训练资料；临时 Excel 不默认训练进 Vanna。
- SQL 执行必须走只读校验：默认只允许 `SELECT` / `WITH`，追加 limit、超时和危险语句拦截。
- DDL / documentation 是通用训练数据，后端可从数据库源、数据模型 reference、度量值自动导入。
- 数据库连接配置只是归属和凭证，不是业务推进对象；Vanna 训练、训练资料回看、实体导入、SQL 示例都必须围绕具体数据库表展开。
- UI 必须是“表级 Vanna 工作区”：用户先选择当前表，再维护该表的 DDL、业务说明、SQL 示例和实体字典。不能让用户在连接级别面对一堆混在一起的训练资料。
- Entity 不做纯自动导入：后端可根据表 profile 推荐“可能适合作为实体字典的列”，但 Excel / CSV profile 只用于 Pandas 分析和数据模型 reference 辅助；Vanna entity 训练只面向已配置的数据库源表。
- Entity 必须分表/分字段管理：当前写入 Vanna 时以 `schema.table.column` 作为 `table_column` 作用域，避免不同表里的同名实体混在一起。前端入口放在数据资产页的具体数据库表上，不放在连接编辑弹窗，也不放在 Excel Profile。
- Entity 推荐必须保持行业无关。像“车系 / 车型 / 参数”只能来自某个具体表的用户选择或 reference 文档，不能写进通用规则；通用逻辑只看列名、类型、唯一值数量/占比、非空率和样例值形态。

迁移任务：

- [x] 迁入本地已验证的 Vanna fork 源码，避免依赖 GitHub 已归档版本导致行为漂移。
- [x] 迁入 `Improve/clients`，保留批量训练、Milvus 向量库和多 embedding provider 适配。
- [x] 移除原 `Improve/clients` 中 `sys.path` / `os.chdir` 这类会污染 PuddingClaw 后端进程的集成方式。
- [x] 补齐 Vanna runtime 依赖声明，并把 `analytics` / `vanna` 加入后端 wheel 打包范围。
- [x] 新增 `analytics.nl2sql` runtime 入口，后续工具和 API 都从这里创建 Vanna 客户端。
- [x] 新增 `config.json -> vanna` 全局运行配置，默认使用 PostgreSQL 方言、复用全局 LLM / embedding / Milvus。
- [x] 基于数据库源配置生成 Vanna client：Milvus collection、LLM、文本 embedding 均来自 PuddingClaw 配置；数据库连接来自数据库源 catalog。
- [x] 新增数据库源 Vanna 训练 API：在数据库源下面写入所选表 DDL、业务 documentation、SQL 问法示例；SQL 示例做只读校验。
- [x] 数据资产页接入表级 Vanna 训练入口：点击数据库源下的具体表，打开独立训练工作区，可导入 DDL、保存 SQL 示例、保存业务说明、查看/删除当前训练资料。
- [x] 将 Vanna 训练 UI 从连接编辑弹窗拆出并改成表级：连接只负责归属，具体表负责 DDL、业务说明、SQL 示例、实体导入和训练资料统计。
- [x] Vanna training-data API 支持按 `table_name` 过滤；现阶段不改 Vanna collection schema，先通过训练内容里的表上下文做回看过滤，后续如需更严格可给 `vannasql/vannaddl/vannadoc` 增加显式 `table_name` 元数据字段。
- [x] Vanna embedding 服务统一为文本 embedding：默认 `text-embedding-v4`，按配置 `batch_size` 批切，避免复用多模态 embedding 或超过服务批量上限。
- [x] 新增 Entity 候选推荐 API：基于表 profile 的列名、类型、distinct 数和样例值推荐候选实体列。
- [x] 修复数据库表 Entity 候选识别的大表超时问题：不再对每个文本列做全表 `COUNT(DISTINCT)`，改为读取 PostgreSQL schema + 小样本估算，`vehicle_params` 这类大表也能快速返回候选。
- [x] 修复数据库表 Entity 推荐度口径：PostgreSQL `character varying/text/boolean` 现在会被识别为文本/枚举类型；采样路径使用样本行数计算非空率，避免候选字段被误扣分后统一显示 40%。
- [x] 清理 Vanna prompt 中迁移自旧项目的汽车领域硬编码，改为通用 `entity_type / canonical_name / aliases / table_column` 映射规则。
- [x] 新增 Entity 候选前端交互：在表格 Profile 弹窗中展示候选列、推荐原因、样例值、唯一值统计；该区域只作为建模参考，不直接写入 Vanna。
- [x] 新增数据库源表 Entity 导入 API/UI：用户在数据库源表中选择实体字段、实体类型和辅助匹配字段后，批量写入 Vanna entity collection；实体按 `schema.table.column` 分表分字段保存。
- [x] 将 Entity 导入里的“辅助匹配字段”从手动输入改成候选字段多选，避免用户输入不存在的字段；当前候选来自实体候选识别结果，后续如需覆盖全部字段再补表字段列表 API。
- [x] 将数据库表 Entity 导入改成后台任务队列：候选识别仍同步返回列推荐，用户确认字段后创建实体导入任务；后台按批读取 distinct 值并写入 Vanna entity collection，进度写入 `KnowledgeImportJob.metadata.progress_detail`，前端不再长时间等待同步 API。`vehicle_params` 约 30,000 实体时不得走长时间同步 API。
- [ ] Vanna 入库验收：在前端完成一次真实数据库源训练测试，确认 DDL、业务说明、SQL 示例、Entity 都能写入对应 Milvus collection，并能在 UI 回看/删除。
- [ ] Vanna 入库问题修复：如果测试发现 embedding、collection、字段作用域、重复写入、删除失败或 UI 状态不一致，优先修复，不进入下一阶段开发。
- [ ] 新增 `database_knowledge_query`：仅在 Vanna 入库验收通过、且用户明确通知进入下一阶段后推进；问数 Agent 在结构化数据库场景优先调用该 tool，由后端内部 Vanna service 生成 SQL，再执行只读 SQL 并返回解释。
- [ ] 新增后端表 Router：在 `database_knowledge_query` 内部基于数据资产 catalog、当前问数上下文、显式选择的数据模型/度量值/数据源和用户问题，先筛出候选数据库表，再交给 Vanna 生成 SQL；禁止让 Vanna 或 LLM 在所有库表里无约束猜表。
- [ ] UI 增加 Vanna 训练状态：未训练 / 已训练 / 需更新 / 训练失败。

### 2026-07-08：`database_knowledge_query` 开发闸门

当前结论：

- 下一阶段确实应该进入 PuddingClaw 内部 Vanna 服务与 Agent tool 开发，但必须先完成 Vanna 入库验收。
- 不启动旧 NL2SQL 项目的 FastAPI 服务；旧项目只作为 Vanna fork、prompt、SQL 输出格式和实体训练流程的参考实现。
- 默认实现路径是后端内嵌能力：

```text
Agent
  -> database_knowledge_query
    -> backend.analytics.nl2sql.service
      -> table_router
      -> Vanna / Milvus training data
      -> read-only SQL runner
      -> SQL + result table + explanation + references
```

计划拆分：

- [x] `backend/analytics/nl2sql/service.py`：封装自然语言问数主流程，统一返回 SQL、结果表、引用训练资料和错误信息。
- [x] `backend/analytics/nl2sql/table_router.py`：基于数据资产 catalog 做表候选选择；输入可来自显式表选择、数据模型、度量值、数据库源、当前页面上下文和用户问题。
- [x] `backend/analytics/nl2sql/sql_runner.py`：只读执行 SQL，负责 `SELECT/WITH` 校验、危险语句拦截、超时、limit 和结果预览。
- [x] `backend/analytics/nl2sql/schemas.py`：定义 `database_knowledge_query` 的请求、响应、引用资料和错误结构。
- [x] `backend/tools/database_knowledge_tool.py`：注册给 Agent 的稳定工具入口；不暴露旧 Vanna FastAPI 路由给 Agent。
- [x] 工具选择逻辑：数据分析问题优先走结构化工具；Excel / CSV 走 `pandas_knowledge_query`，数据库表走 `database_knowledge_query`，文档语义问答走 `llamaindex_knowledge_query`，外部实时信息走 web search。
- [x] Router 白盒化：后端开发态日志打印路由摘要，Trace 面板显示表 Router 选表、候选表、评分和决策原因；`database_knowledge_query` 给 LLM 的 tool output 只保留数据源、表、SQL 和结果摘要。
- [x] Vanna 实体召回白盒化：按 `entity_type` 分组召回和排序，每个实体类型 Top-10 进入 SQL 生成上下文，Trace 面板拆出 `database.vanna_entities` 查看实体分数。
- [x] 消除 Entity 双召回：`database_knowledge_query` 先调用 Vanna 的 `get_related_entities()` 做白盒 Trace，并把同一份 `entity_list` 传入 `VannaBase.generate_sql()`；Vanna 收到预召回实体时跳过内部二次 `get_related_entities()`，保证 Trace 里看到的实体候选和实际 SQL prompt 使用的是同一批结果。
- [x] Entity TopK 配置化：新增 `config.json -> vanna.query.entity_top_k_default` 与 `vanna.query.entity_top_k_by_type`，用于控制每个实体类型进入 SQL prompt 的数量；配置解析容错，非法值不会导致后端启动失败。
- [x] Entity TopK 前端配置：迁入 Vanna 表训练弹窗的“实体字典 / 召回配置”，支持配置默认每类 Top-K 与按实体类型覆盖；保存后写入同一份 `vanna.query` 配置。
- [ ] 使用 `vehicle_params` 做端到端验收：自然语言问题 -> 表 Router 选择表 -> Vanna 生成 SQL -> 只读执行 -> 返回表格结果、SQL、解释和引用资料。

表 Router 边界：

- Router 负责“缩小候选表范围”，不是替代 Vanna 生成 SQL。
- Vanna 负责“基于候选表和训练资料生成 SQL”，不是在全库里自由猜表。
- 如果用户已在 UI 选择了数据库表，Router 应优先使用显式选择。
- 如果来自数据模型或度量值，Router 使用 reference 中声明的数据源/表作为强信号。
- 如果没有显式上下文，Router 从数据资产 catalog 的表名、字段名、profile 摘要、业务说明和训练资料统计中召回候选表；候选不足或冲突时应要求澄清，而不是盲目执行 SQL。
- SQL 执行前必须校验引用表在 Router 允许集合内，避免越权查询。
- Entity 召回只能有一个事实来源：优先由 `database_knowledge_query` 预召回并写入 Trace，再把同一份结果传给 Vanna；只有独立调用 Vanna、没有传入 `entity_list` 时，`VannaBase.generate_sql()` 才保留原始内部召回兜底。

已验证：

- `backend/.venv/bin/python -m compileall backend/analytics/nl2sql backend/tools/database_knowledge_tool.py backend/tools/__init__.py backend/graph/deepagents_manager.py backend/graph/middlewares/tool_intent_router.py`
- `backend/.venv/bin/python -c "from pathlib import Path; from tools.database_knowledge_tool import create_database_knowledge_tool; tool=create_database_knowledge_tool(Path('.')); print(tool.name); print(tool.args_schema.model_json_schema()['properties'].keys())"`
- `backend/.venv/bin/python -c "from pathlib import Path; from tools import get_tools_by_categories; tools=get_tools_by_categories(Path('.'), {'table'}); print([t.name for t in tools if 'knowledge_query' in t.name])"`
- `backend/.venv/bin/python -c "from graph.middlewares.tool_intent_router import ToolIntentRouterMiddleware; r=ToolIntentRouterMiddleware(); print(r._classify_intent('用数据库 vehicle_params 查比亚迪周销量环比')['preferred_tools']); print(r._classify_intent('用刚才导入的 Excel 统计总行数')['preferred_tools'])"`
- `backend/.venv/bin/python -m compileall backend/analytics/nl2sql backend/tools/database_knowledge_tool.py`

本次对抗式边界检查：

- Vanna 只负责 SQL 生成，不负责全库自由猜表；表 Router 先把候选表收敛成允许集合。
- Router 结果会被拼入 Vanna prompt，同时 SQL runner 会再次校验生成 SQL 引用的表是否属于允许集合，防止 prompt 被绕过。
- SQL runner 只允许 `SELECT/WITH`，拦截多语句和写操作关键字，并用子查询包裹强制 `LIMIT`。
- 不启动旧 NL2SQL FastAPI 服务；`database_knowledge_query` 是 Agent 侧唯一稳定入口。
- Router 白盒信息必须同时存在于后端日志和 Agent Trace tool output，避免问数链路变成黑盒。
- 当前阶段只做后端 tool 与路由接入，`vehicle_params` 真实端到端问数验收仍需下一步手动跑一次。
- `cd backend && .venv/bin/python -c "from api.knowledge import router; print([r.path for r in router.routes if 'entities' in r.path])"`
- `cd frontend && npx tsc --noEmit`

### Vanna 入库验收清单

进入 NL2SQL tool 开发前，必须先完成以下测试：

- 数据库源表选择：确认目标 PostgreSQL 数据源已保存、已选择表，并且 Vanna 训练入口只对数据库源开放。
- 表级切换：在独立 Vanna 表训练工作区中切换不同已选表时，“当前表训练资料”、DDL 同步、业务说明、SQL 示例、实体导入都必须跟着当前表切换。
- DDL 入库：点击“导入 DDL”，确认 `puddingclaw_vanna_ddl` 有新增记录，UI “当前表训练资料”能看到并可删除。
- 业务说明入库：添加一段当前表/业务口径说明，确认写入 `puddingclaw_vanna_doc`，UI 只在当前表下回看并可删除。
- SQL 示例入库：添加当前表的自然语言问法 + 只读 SQL，确认写入 `puddingclaw_vanna_sql`；非 SELECT / WITH 语句必须被拦截。
- Entity 候选识别：选择一张表后识别候选实体列，候选只作为推荐，不能自动写入。
- Entity 入库：选择实体字段、实体类型和可选辅助匹配字段后导入，确认写入 `puddingclaw_vanna_entity`，并且每条实体带有 `table_column=schema.table.column`。
- Entity 删除：在 UI 删除实体后，Milvus 中对应记录不应继续被 `get_all_entities` 返回。
- Embedding 路径：确认 Vanna 训练数据使用文本 embedding `text-embedding-v4`；当前默认复用 `fallback_embedding`，是否走 Higress 取决于 `base_url` 配置。
- 重复导入行为：同一表同一字段重复导入时，要观察是否产生重复实体；如重复不可接受，再补去重策略。

已处理的验收问题：

- 修复 Vanna 列训练资料时的 embedding 初始化错误：迁移来的 `QwenEmbedding/JinaEmbedding/BGEEmbedding` 子类原本没有暴露 `batch_size` 参数，但 PuddingClaw runtime 会统一注入该配置；现已补齐并传给 `EmbeddingBase`，保证文本 embedding 能按配置批量切分。
- 修复表级 Vanna 训练写入后的 UI 状态同步：成功横幅 3 秒后自动消失；写入/删除后立即刷新并延迟复查一次；Vanna fork 使用 hash ID 时，后端改为按内容识别 DDL / SQL / 文档类型，避免 DDL 数量一直显示 0。
- 优化表级 Vanna 训练资料展示：DDL 统一识别为“表结构”，使用可滚动代码块展示；前端也按内容兜底识别 hash ID 训练记录，避免后端未重启时显示 `UNKNOWN`。
- 优化表级 SQL 示例交互：SQL 示例卡片内新增“已保存示例”列表，按当前表展示问法、SQL 内容和删除操作，不再只混在训练资料总列表中。
- 移除表级 Vanna “当前表训练资料”总览卡片：DDL、SQL 示例、业务说明分别在各自模块内展示已导入资料；前端统计改为基于 records 重新分类计算，避免后端旧实例仍返回 `unknown` 时顶部数量显示 0。
- 优化 SQL 示例 / 业务说明模块布局：主页面只保留录入区和“查看已保存”入口，已保存资料改为弹窗分页展示，默认每页 10 条，避免训练资料过多导致页面无限延伸。
- 修复 Entity 实际导入仍走同步 API 的问题：`/vanna/entities/import` 现在只创建 `vanna_entity_import` 后台任务；worker 按批写入 Vanna entity collection，并以压缩事件记录进度，避免前端代理超时和任务事件刷屏。
- 优化 Entity 导入任务可发现性：表级 Vanna 弹窗创建实体导入任务后，成功提示内直接提供“查看任务”按钮，跳转到 `/knowledge/imports/{job_id}`，避免用户再去导入任务列表里手动查找。
- 优化 Entity 导入防呆：实体任务创建后明确提示“已进入队列”，同一张表、实体字段、实体类型、辅助匹配字段和导入上限组合会禁用重复提交按钮，避免用户连续点击创建重复任务；切换字段或配置后可创建新的实体导入任务。
- 优化 Entity 入队提示位置：实体导入成功后不再使用顶部全局横幅提示，改为在“实体字典”模块内就地展示“已进入队列”和“查看任务”入口，避免用户滚动距离过长。
- 补齐 Entity 写入前去重：每批写入 Vanna entity collection 前按 `table_column + entity_type + canonical_name` 查询已存在实体；不存在则新增，已存在且 aliases 有变化则更新，完全重复则跳过。任务完成事件会记录新增、更新、跳过重复和失败数量。
- 拆分 Vanna Entity 任务详情页：`vanna_entity_import` 不再复用 PDF/Markdown 导入的“解析结果 / 切片预览 / 检索测试 / Milvus 向量”页面，而是显示表级实体导入摘要、进度、参数、统计和处理记录。
- 优化导入任务列表：列表接口返回轻量任务摘要，避免 Vanna entity 任务的大型 metadata/result 进入 `/api/knowledge/import-jobs?limit=100` 列表响应导致代理超时；前端任务列表增加“实体导入”分类和统计。
- 修复任务详情刷新闪屏：任务详情数据未返回前只展示中性加载态，不再先渲染普通文档导入模板再切换到 Vanna Entity 模板。
- 优化表级实体字典回看：当前表实体默认不再铺开大列表，只展示摘要；展开后支持按实体类型标签筛选、关键词搜索和 10 条分页，避免几百/几万实体撑爆页面。
- 修复实体搜索看起来“未筛选”的问题：列表实际按过滤结果分页渲染；当关键词命中隐藏的辅助词时，将命中的辅助词前置并显示“命中”标签，避免用户看到的行内容和搜索条件脱节。
- 移除 Entity 导入的“最多导入”用户参数：实体导入默认按当前表字段全量导入，后台 worker 分批写入并记录进度；后端仅兼容旧请求里的 `max_values`，不再用 10,000 上限导致大表导入 422。
