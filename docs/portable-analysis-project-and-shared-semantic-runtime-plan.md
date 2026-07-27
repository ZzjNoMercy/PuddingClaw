# 可迁移分析项目与 SQL/Pandas 同源语义运行时方案

> 状态：P0 已完成；统一资产注入层与分析项目导出器已于 2026-07-26 落地  
> 范围：分析模型、语义资产、SQL Guardrails、文件数据资产、SQL/Pandas 语义注入  
> 非目标：削弱 PuddingClaw 原生资产 ID、权限系统、工具体系或执行能力

## 1. 结论

PuddingClaw 内部继续使用自己定义的 `table_asset:*`、`dbs_*`、Profile、虚拟路径和工具协议。这些能力是平台原生资产系统的一部分，不需要为了迁移而改造成最低公分母。

可迁移性由独立的**分析项目导出编译层**承担：将平台原生模型及其依赖编译为一个可以直接作为 Codex、Claude Code 或其他文件系统型 Agent 项目目录打开的分析工程，而不是要求目标平台先“导入”或注册模型。

目标链路：

```text
PuddingClaw 原生分析模型
  + 平台资产 ID / Profile / Guardrails / Templates
                 │
                 ▼
          Analysis Project Exporter
                 │
                 ▼
可直接打开的分析项目目录
  ├─ 模型与语义资产
  ├─ SQL Guardrails 与外部校验脚本
  ├─ 可选复制的数据文件
  ├─ 当前机器的本地路径绑定
  └─ Agent 入口说明与自测
```

同时，SQL 和 Pandas 必须消费同一个 `SemanticQueryContext`。本方案的 P0 已将该能力抽到 `backend/analytics/semantic_runtime/`，并接入 SQL 与 Pandas；分析项目导出器也已落地到 `backend/analytics/project_export/`，模型详情页可选择复制数据文件或保留本机绝对路径并下载 ZIP。SQL Guardrail 的确定性 detector 与 scope runtime 已抽成平台/导出包同源模块；导出项目复制这份运行时代码，而非维护第二套近似实现。

## 2. 第一性原理与边界

分析项目负责描述：

1. 分析什么：模型目标、默认范围、输出规范。
2. 业务口径是什么：Measure、Dimension、Grain、Reference、Relation。
3. 数据应满足什么结构：逻辑数据角色、Schema Profile、字段映射。
4. 什么 SQL 不允许执行：业务 Guardrails 与方言 Guardrails 依赖。
5. 如何证明结果可靠：测试案例、Invariant 和证据要求。

PuddingClaw 平台负责：

1. 原生资产 ID、资产目录、Profile 生成和数据权限。
2. 数据库、Pandas、文件、浏览器等工具实现。
3. HITL、沙箱、Evidence、Goal、Rubric 和 E2E。
4. 导出时解析平台资产并生成外部可读目录。

外部 Agent 只需要文件系统和常规命令执行能力。它不需要认识 PuddingClaw 的内部 API，也不需要实现模型导入流程。

## 3. 当前源码现状

### 3.1 平台原生 ID 可以保留

当前模型直接引用：

```yaml
data_assets:
  tables:
    - dbs_77982e981bac4a6fa8.vehicle_params
    - table_asset:tbl_concat_847eed5f3f93dd93e4cb7111
```

这些引用由 `backend/analytics/models/registry.py` 解析为数据库表、逻辑数据集或文件表格资产。它们在 PuddingClaw 内部有明确含义，继续保留没有问题。

问题不在 ID 本身。现已增加独立导出编译阶段，将 ID 解析成目标项目可直接读取的相对路径、绝对路径、Profile 或外部绑定说明；平台 ID 只作为 provenance 保留。

### 3.2 当前模型/语义资产导入不是本方案目标

现有 `AnalyticsModelRegistry.import_zip()` 和 `SemanticAssetRegistry.import_zip()` 只是把文件复制进注册目录。可迁移分析项目不依赖目标平台实现这套注册器。

导出的目录本身就是交付物。用户在 Codex 中选择该目录为项目目录即可开展分析。

### 3.3 SQL 已有语义注入

`backend/analytics/nl2sql/service.py` 当前会：

1. 根据 `model_id` 获取模型允许的语义资产。
2. 根据 `measure_ids` 精确加载已选资产。
3. 未显式选择时，在模型范围内进行语义匹配。
4. 将完整 Measure、Dimension、Reference 定义格式化后注入 SQL 生成 Prompt。
5. 将解析结果写入 `semantic_trace`，供 SQL Guardrail 使用。

关键入口：

- `_resolve_request_semantic_assets()`
- `_format_analytics_model_for_sql_prompt()`
- `format_semantic_assets_for_prompt()`

### 3.4 P0 实施前 Pandas 未接入模型和语义资产

`backend/tools/pandas_knowledge_tool.py` 的参数目前只有：

```text
query
file_hint
sheet_name
preview_rows
```

`PuddingClawPandasQueryEngine` 的代码生成 Prompt 目前只有：

```json
{
  "question": "...",
  "dataframe_profile": {
    "shape": "...",
    "columns": "...",
    "dtypes": "...",
    "preview": "..."
  }
}
```

它没有接收：

- `analytics_model_id`
- 已选 Measure/Dimension/Grain/Reference
- 数据角色和字段绑定
- 跨源 Relation
- 语义资产优先级
- 统一的 semantic trace

因此，同一个“销量”“能源类型”“车系”“配置率”问题，SQL 和 Pandas 可能依据不同信息独立推断，当前不能保证口径一致。

## 4. 导出后的分析项目目录

```text
product-configuration-analysis/
├── AGENTS.md
├── README.md
├── analysis-project.yaml
├── model/
│   └── model.md
├── semantic/
│   ├── measures/
│   ├── dimensions/
│   ├── grains/
│   └── relations/
├── guardrails/
│   ├── rules/
│   ├── compiled/
│   │   ├── rules.json
│   │   └── rules.lock.json
│   └── runtime/
│       ├── validate_sql.py
│       └── guardrail_runtime.py
├── profiles/
├── templates/
├── data/
├── tests/
├── bindings.example.yaml
└── bindings.local.yaml
```

### 4.1 `analysis-project.yaml`

它是项目的机器可读索引，不替代 Markdown 语义资产：

```yaml
format: analysis-project/v1
id: automotive.product-configuration
version: 1.0.0
entry_model: ./model/model.md

semantic_assets:
  measures:
    - ./semantic/measures/config_rate/measure.md
  dimensions:
    - ./semantic/dimensions/energy_type/dimension.md

data_sources:
  - role: product_model_base
    profile: ./profiles/product-model-base.json
    binding: product_model_base
    required: true
  - role: insurance_sales
    profile: ./profiles/insurance-sales.json
    binding: insurance_sales
    required: false

guardrails:
  rules: ./guardrails/compiled/rules.json
  validator: ./guardrails/runtime/validate_sql.py

tests:
  root: ./tests
```

### 4.2 `AGENTS.md`

`AGENTS.md` 是 Codex 入口适配，不是业务事实源。它只描述执行顺序：

1. 读取 `analysis-project.yaml`。
2. 读取 entry model。
3. 按问题选择语义资产。
4. 读取 `bindings.local.yaml` 定位数据。
5. SQL 生成后运行本地 Guardrail Validator。
6. 按 tests 和 acceptance 要求验证结果。

其他平台可以增加自己的入口文件，但不复制业务定义，例如 `CLAUDE.md`。这些入口都由同一项目清单生成。

## 5. 文件资产导出策略

导出时由用户逐个资产或统一选择以下模式。

### 5.1 复制到项目

适合希望项目自包含、跨机器直接使用的场景：

```yaml
bindings:
  insurance_sales:
    kind: spreadsheet
    path: ./data/2023年11月乘用车市场上险量.xlsx
    sheet_name: 工作表1
    profile: ./profiles/insurance-sales.json
    sha256: "..."
```

导出器执行：

1. 根据 `table_asset:*` 找到真实存储文件。
2. 复制到 `data/`。
3. 导出 Profile。
4. 计算 SHA-256、大小、Sheet 和行列数。
5. 将运行路径改写为项目相对路径。

### 5.2 不复制，保留绝对路径

适合同一台机器上用 Codex 打开模型目录，或数据文件过大/敏感的场景：

```yaml
bindings:
  insurance_sales:
    kind: spreadsheet
    path: /Users/pet/Code/.../2023年11月乘用车市场上险量.xlsx
    sheet_name: 工作表1
    profile: ./profiles/insurance-sales.json
    sha256: "..."
    portable: false
    provenance:
      puddingclaw_asset_id: table_asset:tbl_23fff15978050fdc18330ab2
      virtual_path: /knowledge/imported/20260710/2023年11月乘用车市场上险量.xlsx
```

导出器不能只记录路径，还必须记录 hash、文件名、大小、Sheet 和 Profile。外部 Agent 使用前先检查文件存在性和 hash。

### 5.3 本地绑定与共享定义分离

```text
analysis-project.yaml / model / semantic / guardrails
    可共享、可提交版本库

bindings.local.yaml
    当前机器路径和连接配置，默认加入 .gitignore

bindings.example.yaml
    不含本地绝对路径和秘密的绑定示例
```

数据库密码不得进入项目。数据库绑定只保存环境变量名：

```yaml
bindings:
  product_database:
    kind: postgresql
    connection_env: PRODUCT_DATABASE_URL
    tables:
      product_model_base: vehicle_model_base
      product_params: vehicle_params
```

## 6. Guardrails 的内外同源适配

### 6.1 原则

不维护一套 PuddingClaw Guardrail 和另一套手写外部 Guardrail。正确链路是：

```text
一份结构化 Guardrail 定义
          │
          ▼
Portable Guardrail IR
          ├─ PuddingClaw Internal Adapter
          └─ Analysis Project CLI Adapter
```

### 6.2 统一规则定义

```yaml
id: launch_time_no_car_name_year
type: forbid_sql_pattern
version: 1
required: true
portability: portable

scope:
  semantic_assets:
    - dimension:launch_time

params:
  pattern: "\\bcar_name\\b.*款"
  unless_contains: "type_name = '上市时间'"

action:
  type: block
  message: 上市时间不能从款型名称推断
```

Markdown Frontmatter 或单独 YAML 是执行事实源，正文只做解释和示例。

### 6.3 外部 Validator

导出项目提供稳定 CLI：

```bash
python guardrails/runtime/validate_sql.py \
  --sql-file generated.sql \
  --context semantic-context.json \
  --dialect postgresql \
  --format json
```

标准输出：

```json
{
  "passed": false,
  "violations": [
    {
      "rule_id": "launch_time_no_car_name_year",
      "severity": "error",
      "message": "上市时间不能从款型名称推断",
      "action": "block",
      "suggested_fix": "使用真实上市时间字段"
    }
  ]
}
```

导出器不是按模型重新编写 Python，而是：

1. 找出模型实际启用的规则。
2. 编译 `rules.json` 和带 hash 的 `rules.lock.json`。
3. 裁剪或复制规则所需的通用 detector runtime。
4. 运行内外一致性测试。
5. 将兼容性报告写入项目。

### 6.4 可移植等级

```yaml
portability: portable | adapter_required | platform_only
```

- `portable`：外部 runtime 可以完整执行。
- `adapter_required`：依赖特定 Schema Profile、语义上下文或方言 adapter。
- `platform_only`：只能在 PuddingClaw 执行。

required 规则如果不能导出为确定性执行，导出器必须阻止导出或要求用户显式降级，不能静默变成提示文字。

### 6.5 内外一致性测试

每条规则提供正反例：

```yaml
cases:
  - name: 从款型名称推断上市年份
    sql: SELECT * FROM vehicle_model_base WHERE car_name LIKE '25款%'
    expected: blocked
  - name: 使用真实上市时间字段
    sql: SELECT * FROM vehicle_params WHERE type_name = '上市时间'
    expected: passed
```

同一测试集同时运行于内部引擎和导出 runtime，结果必须一致。

## 7. SQL/Pandas 同源语义运行时

### 7.1 目标

同一轮分析中，无论数据来自 PostgreSQL、Excel、CSV、Parquet 还是数据库查询结果，业务口径只能解析一次，并以不可变上下文传给所有执行器。

```text
用户问题 + 已选分析模型 + 已选语义资产
                    │
                    ▼
           SemanticContextCompiler
                    │
                    ▼
           SemanticQueryContext
             ├─ SQL Adapter
             ├─ Pandas Adapter
             ├─ Guardrail Adapter
             └─ Evidence/Trace Adapter
```

### 7.2 `SemanticQueryContext`

建议定义平台无关的结构：

```python
@dataclass(frozen=True)
class SemanticQueryContext:
    context_id: str
    model_id: str | None
    model_version: str | None
    question: str
    semantic_assets: list[ResolvedSemanticAsset]
    references: list[ResolvedSemanticAsset]
    relations: list[ResolvedRelation]
    source_roles: list[ResolvedSourceRole]
    guardrails: list[ResolvedGuardrail]
    assumptions: list[str]
    unresolved_required: list[str]
    semantic_hash: str
```

其中：

- `context_id` 是由语义内容确定性生成的身份，用于 Trace 对账；P0 不把它当作进程内缓存查询键。
- `semantic_hash` 只绑定模型和已解析语义资产，不包含物理数据源。
- SQL 和 Pandas 分别生成 `binding_hash`，允许它们绑定不同物理来源但共享同一业务语义。
- `execution_context_id` 由 `semantic_hash + binding_hash` 生成，用于区分具体执行上下文。
- `unresolved_required` 非空时，显式模型模式不得继续执行。
- Source role 既可以绑定数据库表，也可以绑定 Excel/DataFrame。

### 7.3 解析只能发生一次

这里要求的是从 SQL Generator 中抽出一层真正与执行器无关的**统一资产注入层**，不是让 Pandas 调用 SQL Generator，也不是把 SQL Prompt 原样复制给 Pandas。

建议源码边界：

```text
backend/analytics/semantic_runtime/
├── schemas.py          # SemanticQueryContext 及已解析资产结构
├── compiler.py         # 模型范围、显式选择、required/optional 编译
├── resolver.py         # Measure/Dimension/Reference/Relation 统一解析
├── bindings.py         # DB、table_asset、DataFrame 的 Source Binding
├── trace.py            # context_id、semantic_hash、binding_hash、证据投影
└── adapters/
    ├── sql.py           # SQL Prompt/Guardrail 投影
    └── pandas.py        # Pandas Prompt/字段约束投影
```

现有职责迁移关系：

| 当前位置 | 调整后位置 |
| --- | --- |
| `nl2sql/service.py::_resolve_request_semantic_assets` | `semantic_runtime/compiler.py` |
| `analytics/semantic_assets/resolver.py` 中通用解析 | `semantic_runtime/resolver.py`，或由其封装现有 Registry |
| `nl2sql/service.py::_format_analytics_model_for_sql_prompt` | 拆为公共模型编译 + `adapters/sql.py` |
| `format_semantic_assets_for_prompt` | 拆为结构化 Context + SQL/Pandas 各自 renderer |
| SQL 内部生成 `semantic_trace` | `semantic_runtime/trace.py` 统一生成 |

语义资产 Registry 仍然是资产存储层；统一资产注入层负责把 Registry、分析模型、数据绑定和当前问题编译成一次性的运行上下文。

新增公共编译入口：

```python
compile_semantic_query_context(
    question,
    model_id,
    selected_semantic_asset_ids,
    selected_source_refs,
) -> SemanticQueryContext
```

它负责：

1. 读取模型允许的语义资产。
2. 解析显式选择的资产和 References。
3. 解析 Relations 和数据角色。
4. 绑定本轮数据库表或表格资产。
5. 选择模型实际启用的 Guardrails。
6. 生成稳定 hash 和 Trace。

SQL 和 Pandas 不再分别调用 fuzzy resolver。通用探索模式可以 fuzzy；显式模型模式使用模型范围和 required/optional 规则。

统一层的返回值必须是结构化对象，而不是已经拼好的 Prompt 文本。这样 SQL、Pandas、未来的 DuckDB/Polars/Spark Adapter 才能按自己的执行语言选择字段和规则，同时共享完全相同的业务口径。

### 7.4 SQL Adapter

将现有 `backend/analytics/nl2sql/service.py` 中的语义解析职责移到公共编译器。NL2SQL 只负责把统一上下文投影为 SQL 所需内容：

```python
render_sql_semantic_context(context)
```

只注入：

- SQL 相关的 Measure、Dimension、Reference、Grain；
- 已绑定数据库表和物理字段；
- 相关 Relation；
- SQL Guardrails。

不再把完整模型 Frontmatter、HTML 模板和报告工作流全部注入 SQL Prompt。

### 7.5 Pandas Adapter

`PandasKnowledgeInput` 建议增加：

```python
asset_id: str | None
model_id: str | None
selected_semantic_asset_ids: list[str]
```

Agent 调用时，可信运行时状态中的 `analytics_model_id` 优先于工具参数；Compiler 根据模型和已选资产确定性重建相同的 `semantic_hash`。待后续建设跨 Worker 的 Context Ledger 后，才增加按 `semantic_context_id` 读取持久上下文的能力，P0 不使用不可靠的进程内缓存。

`PuddingClawPandasQueryEngine` 增加：

```python
PuddingClawPandasQueryEngine(
    df,
    semantic_context=context,
    source_binding=resolved_table_asset,
)
```

Pandas Prompt 需要新增：

```json
{
  "question": "...",
  "dataframe_profile": {},
  "semantic_context": {
    "measures": [],
    "dimensions": [],
    "grains": [],
    "references": [],
    "field_bindings": {},
    "required_filters": [],
    "prohibited_inferences": []
  }
}
```

Pandas 只接收与当前 DataFrame 字段相关的语义投影，不注入整个模型全文。

### 7.6 Trace 与证据一致性

SQL 和 Pandas 结果都必须返回：

```json
{
  "semantic_context_id": "semctx-...",
  "semantic_context_hash": "sha256:...",
  "semantic_asset_ids": [],
  "source_refs": [],
  "calculation_grain": "...",
  "code_or_sql": "..."
}
```

联合分析时，若两边 context hash 不一致，应阻止生成最终业务结论或重新编译共同上下文。

### 7.7 Guardrails 与 Pandas 的边界

SQL Pattern Guardrails 只校验 SQL，不机械应用于 Pandas 代码。业务口径约束应来自同一语义资产，并分别投影：

- SQL：物理表、JOIN、GROUP BY、SQL Pattern。
- Pandas：字段映射、过滤、去重键、分组颗粒度、枚举分类。
- 最终结论：模型 Invariant 和 Evidence 校验。

例如“传统能源不包含柴油”应由 `dimension:energy_type` 的结构化分类声明约束 SQL 和 Pandas，而不是只写成某条 SQL 正则 Guardrail。

## 8. 事实源与冗余清理

| 内容 | 唯一事实源 |
| --- | --- |
| 公式、分子、分母、颗粒度 | Measure |
| 分类、枚举、禁止推断 | Dimension |
| 物理字段、EAV `type_name`、Excel 列映射 | Schema Profile / Source Binding |
| 逻辑关联和规范键 | Relation |
| SQL 结构性禁止规则 | Guardrail |
| PostgreSQL 通用陷阱 | Dialect Guardrail Pack |
| 报告章节和图表契约 | Template / Output Specification |
| PuddingClaw 工具、权限、Goal、HITL、E2E | 平台 Adapter |
| `table_asset:*`、`dbs_*`、虚拟路径 | 平台原生模型与 Provenance |
| 当前机器绝对路径 | `bindings.local.yaml` |

应该优先清理：

1. 同一分类同时存在于模型正文、Dimension、Reference 和 Guardrail。
2. Python `DEFAULT_GUARDRAILS` 与 Markdown Guardrail 双重事实源。
3. 模型声明 Guardrail，但运行时加载全部全局规则。
4. SQL 服务注入整个模型 Frontmatter 和报告正文。
5. SQL 与 Pandas 分别解析或猜测业务口径。

## 9. 兜底策略分级

不应删除所有兜底，而应按运行模式隔离。

### 通用探索模式

允许：

- fuzzy 选择语义资产；
- 从多个可用数据源推荐候选；
- 无已发布业务口径时明确假设后做一般分析。

### 显式分析模型模式

必须 fail-closed：

- required 语义资产缺失；
- required 数据绑定缺失；
- required Guardrail detector 不可用；
- unknown required Invariant；
- SQL/Pandas context hash 不一致；
- 外部绝对路径文件 hash 不匹配。

可选依赖缺失只允许降级并形成结构化告警，不得静默跳过。

## 10. 实施计划

### P0：公共语义上下文

实施状态：已完成本轮范围。

1. 已抽出 `SemanticQueryContext`、Compiler、统一 ID 归一化和 SQL/Pandas Adapter。
2. SQL 已改为消费公共上下文，并保留原有 `matched/references/analytics_model` Trace 结构。
3. Pandas 已增加 `asset_id/model_id/selected_semantic_asset_ids`，Agent 模式优先使用可信运行时模型。
4. Pandas 代码生成与答案合成均注入同源语义定义。
5. SQL/Pandas 返回同一 `semantic_hash`，物理来源分别记录 `binding_hash`。
6. SQL 技术修复沿用原始 `semantic_question`，避免错误文本改变 Reference 解析。

本轮有意未实现进程内 Context Cache/Ledger。当前 Compiler 是确定性纯编译入口，相同输入产生相同 `context_id/semantic_hash`，避免多 Worker 和重启导致伪共享。

验收：同一问题分别走 SQL 和 Pandas，使用相同 Measure、Dimension、Reference 和 Grain，Trace hash 一致。

### P0：分析项目导出器

实施状态：已完成。

1. 已从模型递归收集语义资产及其 References、Relations、Guardrails、Templates、Profiles 和逻辑数据集来源。
2. 已提供“复制数据文件”和“保留本机绝对路径”两种导出策略；数据库密码和 URL 不进入导出包。
3. 已生成 `analysis-project.yaml`、`AGENTS.md`、README、local/example bindings 和 PuddingClaw provenance。
4. 已生成逐文件 checksum 清单、项目完整性校验器、编译后的 Guardrail 规则和便携 SQL 校验脚本。
5. 已在模型详情页增加“导出项目”UI，导出前展示真实依赖、体积、告警和缺失项；未保存草稿与缺失必需依赖会阻止导出。
6. ZIP 在后端以临时文件流式生成，数据文件不进入 Python 或浏览器 JS 内存；响应结束后清理临时文件。
7. 导出预览生成内容寻址的 `plan_id`；下载必须绑定该快照，模型或依赖变化后拒绝混合版本导出。
8. 项目校验器除包内文件 checksum 外，还验证 mutable binding 的必需项、数据文件 hash、Profile、逻辑数据集 DAG/Materializer 与 Guardrail rules lock。
9. 归档目标统一做相对路径与冲突检查，模型/语义/守卫来源限制在声明根目录，拒绝符号链接逃逸。

验收：导出目录可在 Codex 中直接作为项目打开，不依赖 PuddingClaw 导入 API。

### P1：Portable Guardrail Runtime

实施状态：确定性 SQL detector/scope 子集已完成；声明式 IR 与非 SQL invariant 继续演进。

1. 已将五类确定性 SQL detector 与 scope 判断抽到无平台依赖的 `guardrail_runtime.py`。
2. 平台 detector 和导出 CLI 消费同一运行时源文件，并以 parity tests 防止漂移。
3. 外部 CLI 对缺少 scope context、未知 detector 统一 fail-closed；`warn` 不阻断，`block/rewrite` 阻断。
4. 模型启用的非 advisory 守卫若无便携 detector，导出计划直接标记缺失依赖；不会静默降级。
5. 后续再将 semantic enum consistency 与 acceptance invariants 编译进统一声明式 IR。

### P1：事实源治理

1. 将业务分类和公式从重复 Markdown 收敛到语义资产。
2. 将物理字段映射收敛到 Profile/Binding。
3. 将通用 PostgreSQL 规则拆为 Dialect Pack。
4. 让模型 Guardrail 列表真正控制运行时规则选择。
5. unknown required detector/invariant 改为显式失败。

### P2：跨平台入口与模板

1. 由同一清单生成 `AGENTS.md` 和其他平台入口。
2. 模板、JS、CSS 全部使用项目相对路径。
3. 提供离线 self-check 命令。
4. 支持将同一分析项目提交 Git 或直接复制目录。

## 11. 测试矩阵

| 类别 | 必需测试 |
| --- | --- |
| 导出完整性 | 所有模型引用均在项目中或有 binding |
| 文件复制模式 | 相对路径存在且 hash 一致 |
| 绝对路径模式 | 路径、hash、Profile 一致；失效时明确报错 |
| SQL/Pandas 同源 | context ID/hash、资产 ID、颗粒度一致 |
| Guardrail parity | 内部与外部 Validator 对同一案例判定一致 |
| 必需能力 | unknown required detector/invariant 导出失败 |
| 模板 | HTML/JS/CSS 相对资源可加载 |
| 外部项目 | 在无 PuddingClaw API 的目录环境完成样例分析 |

## 12. 待审核决策

1. 文件资产默认选择“复制到项目”还是沿用上次选择。
2. `bindings.local.yaml` 是否默认加入导出目录的 `.gitignore`。
3. 外部 Guardrail Runtime 首版只支持 Python，还是同时提供独立二进制。
4. `platform_only` required Guardrail 是否一律禁止导出，还是允许用户显式降级为 advisory。
5. SQL/Pandas 同源语义运行时是否作为导出功能的前置依赖。建议是：**作为前置依赖**，否则平台内部和导出项目可能继续产生两套口径。

## 13. 推荐落法

本方案建议按以下主线推进：

```text
先实现 SQL/Pandas 同源 SemanticQueryContext
→ 再让 Guardrails 消费统一上下文
→ 再实现文件系统原生 Analysis Project Exporter
→ 最后迁移产品配置分析模型作为首个标准样例
```

原因是导出器只能导出当前已经明确的运行契约。如果 SQL 和 Pandas 在平台内部仍各自推断语义，导出项目只会把这种不一致一并带到外部。
