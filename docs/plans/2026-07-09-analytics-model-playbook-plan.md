# Analytics Model Playbook 方案

创建时间：2026-07-09

状态：准备实施

## 背景

智能问数已经有几类独立资产：

- 数据资产：数据库连接、表、字段、DDL、Profile。
- 语义资产：度量值、维度、颗粒度、reference。
- SQL 守卫：把高风险 SQL 反模式沉淀成可迁移 Markdown 文档。
- 查询结果持久化：大明细结果可以落盘、分页读取和导出。
- 报告模板需求：后续需要按 Markdown、HTML 等格式输出分析报告。

下一阶段需要支持“模型”。这里的模型不是大模型，也不是固定后端 workflow，而是一个可迁移的分析应用包。

典型场景：

- 用户选择“产品配置分析”模型，输入“刷新产品配置分析”，Agent 按产品配置分析的 Playbook 和模板刷新结果。
- 用户选择“乘用车上险量分析”模型，输入“输出 2026 年 5 月上险报告”，Agent 自动识别参数，按月报模板完成分析。

## 核心判断

第一版不要做传统固定流水线。

传统 workflow 的问题：

```text
step1 -> step2 -> step3 -> render
```

这种方式确定性强，但会把 Agent 降级成参数填充器，失去 AI Native 的意义。

第一版应该做 AI 可执行的分析 Playbook：

```text
模型声明分析目标、资产边界、推荐步骤、质量规则、输出模板；
Agent 根据用户问题动态决定查哪些、跳过哪些、补查哪些、是否分页、是否导出。
```

模型的职责是约束和增强 Agent，而不是替代 Agent 的规划能力。

## 目标

- [ ] 定义 `analytics-model` 文档格式。
- [ ] 增加模型目录和 registry。
- [ ] 支持模型引用数据资产、语义资产、SQL 守卫和模板。
- [ ] 支持前端创建、导入、编辑、刷新、选择模型。
- [ ] 支持主 Agent / 问数 Agent 在运行时加载已选模型。
- [ ] 支持模型 Playbook 进入上下文，指导 Agent 做动态分析。
- [ ] 支持 Trace 显示本轮加载了哪个模型、哪些资产、哪些模板。

## 非目标

第一版不做：

- 后端固定 DAG workflow 引擎。
- 复杂任务编排 DSL。
- 模板自动渲染引擎强绑定。
- 自动生成所有 SQL 的确定性流程。
- 多模型冲突合并。
- 权限系统。

## 概念定义

### 分析模型

分析模型是一个目录，至少包含 `model.md`。

它定义：

- 这个分析模型解决什么业务问题。
- Agent 在这个模型下应该如何思考。
- 可以使用哪些数据表。
- 应该优先注入哪些语义资产。
- 应该启用哪些 SQL 守卫。
- 有哪些可用输出模板。
- 有哪些分析 Playbook。

### Playbook

Playbook 是“给 Agent 的分析套路”，不是固定执行流水线。

它应该描述：

- 触发示例。
- 必要参数。
- 推荐分析模块。
- 质量规则。
- 缺失数据时如何处理。
- 什么时候应该补查。
- 什么时候应该导出或分页。

### 模板

模板定义输出结构，例如：

- 产品配置刷新报告。
- 月度上险报告。
- 品牌专题报告。
- HTML 看板。

模板不负责执行查询，只负责约束最终产物形态。

## 目录结构

第一版目录：

```text
backend/analytics-models/
  product_config_analysis/
    model.md
    templates/
      refresh.md
      config_report.md
    examples/
      questions.md
  passenger_insurance_analysis/
    model.md
    templates/
      monthly_report.md
    examples/
      questions.md
```

虚拟挂载：

```text
/analytics-models -> backend/analytics-models
```

这样 Agent 和 skill 可以像读写语义资产、SQL 守卫一样读写模型文件。

## model.md 格式

示例：

```markdown
---
formatter: analytics-model
id: product_config_analysis
name: 产品配置分析
version: 0.1.0
description: 面向车型配置率、配置分布、价格段、能源类型、上市时间等问题的分析模型。

data_assets:
  tables:
    - vehicle_params_wide
    - vehicle_params

semantic_assets:
  measures:
    - config_rate
    - charging_rate
  dimensions:
    - launch_time
    - energy_type
    - vehicle_level
    - price_band
    - brand
  grains:
    - car_model
    - series

guardrails:
  - config_rate_model_key_group
  - config_rate_use_wide_denominator
  - launch_time_no_car_name_year

templates:
  refresh: templates/refresh.md
  config_report: templates/config_report.md

default_template: refresh

analysis_playbooks:
  refresh:
    trigger_examples:
      - 刷新产品配置分析
      - 重新生成产品配置分析
    required_slots: []
    suggested_sections:
      - 核心配置率
      - 品牌分布
      - 车系分布
      - 能源类型结构
      - 价格段结构
      - 异常项说明
    quality_rules:
      - 配置率必须说明分母、分子和排除规则。
      - 涉及上市时间必须使用上市时间语义资产。
      - 配置明细较多时优先返回摘要，并通过 result_id 支持分页查看。
---

你是产品配置分析 Agent。

分析原则：

1. 配置率类问题优先使用 vehicle_params_wide 做目标款型集合筛选。
2. 具体配置判断再回到 vehicle_params 查询配置项。
3. 默认排除车型级别为皮卡的记录，除非用户明确要求包含皮卡。
4. 涉及上市时间必须使用上市时间维度，不允许从款型名称推断年份。
5. 输出需要包含口径、核心结论、数据证据和异常说明。
```

## 上险量分析模型示例

```markdown
---
formatter: analytics-model
id: passenger_insurance_analysis
name: 乘用车上险量分析
version: 0.1.0
description: 面向乘用车月度上险量、品牌排名、车系排名、结构变化和同比环比的分析模型。

data_assets:
  tables:
    - insurance_volume_wide
    - vehicle_params_wide

semantic_assets:
  measures:
    - insurance_volume
    - market_share
    - yoy_growth
    - mom_growth
  dimensions:
    - month
    - brand
    - series
    - energy_type
    - price_band
    - vehicle_level

guardrails: []

templates:
  monthly_report: templates/monthly_report.md

default_template: monthly_report

analysis_playbooks:
  monthly_report:
    trigger_examples:
      - 输出2026年5月上险报告
      - 生成5月乘用车上险分析
      - 刷新2026年5月上险报告
    required_slots:
      - year
      - month
    suggested_sections:
      - 总体规模
      - 同比环比
      - 品牌排名
      - 车系排名
      - 能源类型结构
      - 价格段结构
      - 异常变化解释
    quality_rules:
      - 每个结论必须有数据支撑。
      - 排名类结果必须说明 TopN 口径。
      - 如果 year 或 month 缺失，先向用户追问，不要猜测。
      - 如果数据缺失，必须显式说明缺失项。
---

你是乘用车上险量分析 Agent。

当用户要求生成月度上险报告时：

1. 先识别 year/month。
2. 根据报告模板规划查询，不要机械执行所有模块。
3. 如果用户只关心某个品牌或车系，可以缩小查询范围。
4. 如果发现结果异常，需要补充查询同比、环比或细分维度。
5. 输出必须符合模板，但允许根据数据情况增删小节。
```

## 模板示例

`templates/monthly_report.md`：

```markdown
# {year}年{month}月乘用车上险报告

## 口径

- 时间范围：
- 数据来源：
- 车型范围：
- 排除规则：

## 核心结论

1.
2.
3.

## 总体规模

## 同比环比

## 品牌表现

## 车系表现

## 能源类型结构

## 价格段结构

## 异常与注意事项
```

## 运行时行为

用户选择模型后，本轮 Agent 上下文应该增加：

```text
当前分析模型：产品配置分析
模型说明：...
可用数据表：vehicle_params_wide, vehicle_params
强制/优先语义资产：...
启用 SQL 守卫：...
可用模板：refresh, config_report
Playbook：...
```

执行链路：

1. 前端记录当前选中的 `model_id`。
2. 发送 Agent 请求时带上 `analytics_model_id`。
3. 后端加载模型 registry。
4. 根据 `model_id` 读取 `model.md`。
5. 将模型的正文和 Playbook 注入 Agent / 问数 Agent 上下文。
6. 将模型引用的数据表作为表路由优先范围。
7. 将模型引用的语义资产作为优先注入资产。
8. 将模型引用的 SQL 守卫作为本轮启用范围。
9. Trace 记录模型加载情况。

## Trace 要求

Trace 中需要能证明模型真的生效：

```json
{
  "analytics_model": {
    "id": "product_config_analysis",
    "name": "产品配置分析",
    "version": "0.1.0",
    "loaded": true,
    "data_assets": {
      "tables": ["vehicle_params_wide", "vehicle_params"]
    },
    "semantic_assets": {
      "measures": ["config_rate"],
      "dimensions": ["launch_time", "energy_type"]
    },
    "guardrails": ["config_rate_use_wide_denominator"],
    "templates": ["refresh"]
  }
}
```

如果模型加载失败，Trace 必须展示：

- 文件路径。
- 解析错误。
- 是否 fallback 到无模型模式。

## 前端交互

智能问数页面增加“模型”分类，和数据资产、语义资产、问数 Agent 并列。

模型页能力：

- 搜索模型名称。
- 筛选模型类型或标签。
- 新建模型。
- 导入 ZIP / 文件夹。
- 刷新模型 registry。
- 点击卡片进入详情。
- 查看文件树。
- 编辑 `model.md`。
- 编辑模板文件。
- 保存后弹窗提示保存成功。

问数 Agent 对话入口：

- 支持选择当前模型。
- 展示当前模型名称。
- 用户不选择模型时，保持现有通用问数逻辑。
- 用户选择模型后，Agent 回答时应优先遵守模型上下文。

## 后端模块建议

新增模块：

```text
backend/analytics/models/
  registry.py
  schemas.py
```

核心接口：

```python
def load_analytics_models() -> list[AnalyticsModel]
def refresh_analytics_model_registry() -> AnalyticsModelRegistry
def get_analytics_model(model_id: str) -> AnalyticsModel | None
```

API：

```text
GET  /api/analytics/models
GET  /api/analytics/models/{model_id}
POST /api/analytics/models
PUT  /api/analytics/models/{model_id}
POST /api/analytics/models/refresh
POST /api/analytics/models/import
```

文件系统：

- `/api/files` 允许读写 `analytics-models/`。
- `write_file` 允许写 `analytics-models/`。
- DeepAgents `FilesystemBackend` 挂载 `/analytics-models/`。
- Trace runtime inventory 显示 `/analytics-models/` 是否 mounted。

## 与现有资产的关系

### 与语义资产

模型引用语义资产，但不复制语义资产内容。

好处：

- 语义资产仍是口径来源。
- 多个模型可以复用同一度量值、维度、颗粒度。
- 更新语义资产后，所有引用模型自动受益。

### 与 SQL 守卫

模型引用守卫 ID。

守卫仍由 `sql-guardrails/rules/**/guardrail.md` 管理。

模型只声明“这个分析模型下应该启用哪些守卫”。

### 与数据资产

模型声明可用表或推荐表。

第一版不做强权限隔离，但应作为表路由优先范围，减少 Agent 查错表。

### 与模板

模板归属模型目录，因为模板通常和具体分析模型强相关。

后续如果出现跨模型复用模板，再抽到共享模板目录。

## 实施计划

### 阶段 1：文档资产和 registry

- [ ] 创建 `backend/analytics-models/`。
- [ ] 增加示例 `product_config_analysis/model.md`。
- [ ] 增加模型 schema。
- [ ] 扫描 `backend/analytics-models/**/model.md`。
- [ ] API 返回模型列表和详情。

### 阶段 2：文件系统和前端管理

- [ ] 挂载 `/analytics-models/`。
- [ ] 允许 Agent / skill 读写模型文件。
- [ ] 前端增加模型页。
- [ ] 支持创建、导入、编辑、保存、刷新。

### 阶段 3：Agent 上下文注入

- [ ] 对话请求支持 `analytics_model_id`。
- [ ] 主 Agent / 问数 Agent 加载模型上下文。
- [ ] `database_knowledge_query` 能看到当前模型的表范围、语义资产和守卫。
- [ ] Trace 展示模型加载和注入情况。

### 阶段 4：真实模型验证

- [ ] 用“产品配置分析”验证“刷新产品配置分析”。
- [ ] 用“产品配置分析”验证配置率类问数。
- [ ] 用“乘用车上险量分析”验证“输出 2026 年 5 月上险报告”。
- [ ] 根据失败样例补充 Playbook、语义资产或守卫。

## 风险与处理

### 风险：模型正文过长

处理：

- 第一版控制 `model.md` 内容长度。
- 模板文件只在命中对应 Playbook 时注入。
- examples 默认不注入，只作为编辑参考。

### 风险：模型和语义资产冲突

处理：

- 模型只引用语义资产，不复制口径。
- 如果模型正文和语义资产冲突，语义资产优先。
- Trace 显示冲突来源。

### 风险：Agent 仍不遵守 Playbook

处理：

- 高风险规则沉淀到 SQL 守卫。
- 常见失败样例补充到模型 examples。
- 必要时把特定 Playbook 的关键步骤写成更强约束。

### 风险：用户误以为模型是固定报表

处理：

- 前端文案明确：模型是分析上下文和 Playbook，不是固定任务流。
- 模板是输出结构，不代表每次必须机械执行所有章节。

## 第一版验收标准

- 前端能看到模型列表。
- 能新建和编辑 `model.md`。
- 能导入模型目录或 ZIP。
- Agent 请求能携带 `analytics_model_id`。
- Trace 能看到模型已加载。
- “刷新产品配置分析”能读取模型 Playbook 和模板。
- 问数结果能体现模型引用的语义资产和守卫。

