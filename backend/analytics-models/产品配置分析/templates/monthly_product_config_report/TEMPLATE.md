# 月度产品配置分析报告模板

这是 `monthly_product_config_report` 的唯一使用说明，包含触发条件、生成流程、Payload、页面结构、图表和验收契约。不要在模型顶层 reference 中追加本模板专属规则。

## 何时使用

仅当用户 Query 明确要求以下交付物或同义表达时使用：

- 刷新月度产品配置分析报告。
- 生成产品配置分析月报。
- 更新本月产品配置分析报告。

单一车型配置查询、多车型配置对比、临时专题分析或普通问答不得使用本模板。使用前先读取 `model.md` 注册的通用 `references.analysis_rules.path`。

## 模板文件

- HTML 基准模板：`index.html`。
- 文本与图表渲染器：`report-renderer.js`。
- 本地 ECharts：`echarts-6.1.0.min.js`。

模板不含示例数据。`#report-payload` 内的 `null`、空数组和 `status: "pending"` 是待生成占位，不是查询结果。

## 不可变约束

Agent 必须复制 `index.html` 为新报告，完成全部查询和计算后，一次性替换页尾 `<script id="report-payload" type="application/json">` 内的完整 JSON。

不得修改：

- 基准模板本身。
- HTML 的 DOM id、章节顺序或样式。
- `report-renderer.js`。
- ECharts option 或页面中的单个数字。

## 月度命名

- 基准模板名称固定为“月度产品配置分析报告模板”，不得写入某个具体月份的数据。
- 每次刷新必须生成新报告，不得覆盖基准模板或上月报告。
- `report.title`：`YYYY年MM月产品配置分析报告`。
- `report.scope.period`：`YYYY-MM`，必须与标题月份一致。
- `report.report_date`：报告生成日期。
- `report.data_cutoff`：本期实际数据截止日。
- 推荐输出目录：`YYYY-MM`。
- 推荐文件名：`product-configuration-analysis-YYYY-MM.html`。

## Agent 执行契约

```text
S0 解析 Query，确认命中月报模板
  → S1 从 8 个章节和 20 个图表编译报告计划
  → Gate 1：计划完整
  → S2 查询与计算
  → Gate 2：结果有口径和证据
  → S3 组装 report_payload
  → Gate 3：Payload 合法
  → S4 复制并刷新 HTML
  → Gate 4：页面和运行时验收通过
  → S5 交付
```

任何 Gate 失败时只能回到上一阶段修复，不得跳过失败项继续交付。

### S1：编译报告计划

每个必需章节和图表生成一个计算任务：

```json
{
  "task_id": "chart.l2TrendChart",
  "section": "adas",
  "output_path": "charts.l2TrendChart",
  "grain": ["launch_year"],
  "dimensions": ["launch_year", "adas_level"],
  "measures": ["series_coverage_rate", "config_rate"],
  "filters": {"vehicle_scope": "passenger_vehicle"},
  "status": "pending"
}
```

计划必须覆盖 8 个章节、20 个图表、封面、KPI、结论、竞企表格和口径说明，并为每个任务声明颗粒度、维度、度量、筛选、输出路径和空数据策略。

### S2：查询与计算

- 遵守模型顶层通用分析规则。
- 每个查询结果保存 `query_id`、SQL/工具调用摘要、行数、颗粒度、筛选、数据截止日和异常。
- 每个派生指标保存 `value`、`unit`、`numerator`、`denominator`、`grain` 和 `source_query_ids`。
- 完成整个计划后才能组装 Payload。

### S3：统一 Payload

```json
{
  "schema_version": "2.0",
  "report": {
    "title": "YYYY年MM月产品配置分析报告",
    "report_date": null,
    "data_cutoff": null,
    "scope": {"period": "YYYY-MM"},
    "metrics": [],
    "insights": [],
    "sections": {},
    "features": {},
    "tables": {},
    "methodology": []
  },
  "controls": {},
  "charts": {},
  "evidence": {},
  "quality": {
    "status": "draft",
    "plan_tasks": 0,
    "completed_tasks": 0,
    "missing_tasks": [],
    "warnings": []
  }
}
```

约束：

- `report` 负责文本、KPI、章节和表格。
- `controls` 负责核心配置和热力图切片的下拉选项。
- `charts` 以 DOM id 为键，只保存渲染所需的 `kind`、`categories`、`series`/`variants` 和 `query_ids`。
- `evidence` 以 `query_id` 为键保存证据与口径。
- `quality.completed_tasks` 必须等于 `quality.plan_tasks`；否则只能输出草稿。
- 空数据使用 `status: "no_data"` 和 `reason`。
- `status: "ready"` 的图表必须提供非空 `query_ids`，且各系列长度与 `categories` 一致。

### 文本与卡片

```text
report.title
report.report_date
report.data_cutoff
report.scope.market / energy / period / grain
report.metrics[]          = {label, value, unit, note, tone}
report.insights[]         = {kicker, title, summary, query_ids}
report.sections.*         = {headline, summary, query_ids}
report.features.adas[]    = {label, value, unit, title, summary, progress, query_ids}
report.features.cockpit[] = {label, value, unit, title, summary, progress, query_ids}
report.methodology[]      = {code, title, description}
```

`value` 必须是数字或经过明确格式化的文本。`progress` 只用于卡片顶部的 0–100 视觉进度条，不参与业务计算。

### 表格

```text
report.tables.<table_id> = {
  status: "ready" | "no_data" | "pending",
  columns: [{key, label}],
  rows: [{<key>: <value>}],
  reason: <required when no_data or pending>,
  query_ids: [<query_id>]
}
```

### 图表

普通折线、柱状、组合图和 100% 堆叠图：

```text
charts.<DOM id> = {
  status: "ready",
  kind: "line" | "combo" | "percent_stack" | "boxplot",
  categories: [<category>],
  series: [{name, type, data: [<number>], axis, stack, color}],
  query_ids: [<query_id>]
}
```

热力图变体：

```text
{
  status: "ready",
  kind: "heatmap_variants",
  variants: [{
    label,
    kind: "heatmap",
    filters: {<filter>: <value>},
    x_categories: [<category>],
    y_categories: [<category>],
    values: [[<x_index>, <y_index>, <number>]]
  }],
  query_ids: [<query_id>]
}
```

查询成功但无行时使用：

```text
{status: "no_data", kind: <kind>, reason: <verified empty reason>, query_ids: [<query_id>]}
```

不得用空数组加 `status: "ready"` 伪装无数据。

### S4：刷新 HTML

1. 复制 `index.html` 为新的报告文件。
2. 把完整 `report_payload` 注入报告文件的 `#report-payload` JSON 节点，不做局部替换。
3. 文本渲染器刷新标题、KPI、章节文案和表格。
4. 图表渲染器销毁旧实例后重建全部图表。
5. 写入 `rendered_at`、Payload 校验摘要和数据截止日。
6. 在浏览器加载生成文件并执行 Gate 4。

禁止使用正则逐个替换页面数字、查询一张图就刷新一次 HTML、保留无证据的示例内容，或自由增删章节和图表 DOM id。

## 页面字段映射

### 报告元数据

| 模板字段 | 类型 | 来源/生成规则 |
| --- | --- | --- |
| `title` | string | 根据月份和分析范围生成封面主标题 |
| `report_date` | date | 报告生成日期，只用于封面 |
| `data_cutoff` | date | 数据最大有效日期，写入数据口径章节 |
| `scope.market` | string | 默认“中国乘用车” |
| `scope.period` | string | `YYYY-MM`，与标题月份一致 |
| `scope.energy` | string | 使用 `dimension:energy_type` 归一后的展示文本 |
| `scope.grain` | string | 说明默认分母是款型还是车系 |
| `scope.price_band` | string[] | 使用 `dimension:price_band` |

### 页面对象

| 模板对象 | 必需内容 |
| --- | --- |
| `report.metrics[]` | KPI `label/value/unit/note/tone`，口径和分子分母放入 evidence |
| `report.insights[]` | 执行摘要 `kicker/title/summary`，每条必须有证据 |
| `report.sections.*` | 六个分析章节的 `headline/summary`，高压平台另含 `note` |
| `report.features.adas[]` | 智驾 KPI 卡片，不含芯片平台或雷达供应商份额 |
| `report.features.cockpit[]` | HUD、屏幕和舒适配置 KPI 卡片，不含车机芯片披露率/型号份额 |
| `report.tables.*` | `status/columns/rows/reason/query_ids` |
| `charts.<DOM id>` | `status/kind/categories/series/query_ids`；交互图使用 `variants` |
| `report.methodology[]` | `code/title/description`，覆盖数据源、截止日、范围、颗粒度、分子分母和缺失处理 |

## 必需章节

1. **执行摘要**：封面只显示标题与报告日期；下方展示 3–5 个 KPI 和 3–5 条结论。
2. **新车迭代**：更新次数、更新周期、能源结构与重点竞企节奏。
3. **尺寸与动力**：轴距、电机功率及尺寸 × 功率组合。
4. **高压平台**：纯电与全新能源分别统计年度趋势和价格带结构。
5. **智能驾驶**：L2/L2+、高阶 NOA、指导价分布和主要企业 NOA 搭载。
6. **座舱与舒适**：HUD、屏幕尺寸、冰箱、后排屏、零重力和按摩座椅。
7. **核心配置率查询**：按配置项、款型/车系口径及价格段/轴距段/级别查询历年搭载规模与配置率。
8. **附件 · 数据口径**：置于最后，集中说明数据源、截止日期、范围、颗粒度、分子分母、排除项、缺失与异常。

若某专题无可靠字段，章节仍保留，用明确缺失说明替代图表，并在数据口径章节记录原因。

## 图表契约

### 通用显示规则

- 使用模板内置 ECharts，不使用图片模拟数据图表。
- 每张图必须有标题、单位、时间范围、图例、来源/口径说明和无数据状态。
- 比率统一使用 0–100% 轴；趋势按时间升序，价格带按区间升序，品牌/供应商横条按值降序。
- 100% 堆叠图每个时间点合计的绝对误差不得超过 0.2 个百分点。
- 款型和车系口径不得混用；同图展示时必须在系列名标注。
- 少于 3 个有效时间点时不画趋势线，改用单期条形图或数据卡片。
- 箱线图必须从款型级价格样本计算 `[下须, Q1, median, Q3, 上须]`，上下须使用 1.5×IQR 范围内最远实际观测值，离群价格单独展示。
- 热力图单元格只使用唯一款型数或占比中的一种。
- 多系列图例首次点击只显示该系列，再次点击恢复全部系列，不得触发其他图表重绘。

### 20 个图表

| DOM id | 图表类型 | 必需输入 |
| --- | --- | --- |
| `renewalChart` | 堆叠柱 + 双折线 | 年份、两类更新次数、两类更新周期 |
| `wheelbaseTrendChart` | 100% 堆叠柱 | 年份、轴距段、款型占比 |
| `motorPowerTrendChart` | 100% 堆叠柱 | 年份、功率段、款型占比 |
| `sizePowerHeatmapChart` | 热力图 | 轴距段 × 功率段 × 款型数/占比 |
| `bevVoltageTrendChart` | 堆叠柱/折线 | 纯电、年份、电压平台、款型数/占比 |
| `bevVoltagePriceChart` | 堆叠柱/折线 | 纯电、价格带、电压平台、款型数/占比 |
| `nevVoltageTrendChart` | 堆叠柱/折线 | 全新能源、年份、电压平台、款型数/占比 |
| `nevVoltagePriceChart` | 堆叠柱/折线 | 全新能源、价格带、电压平台、款型数/占比 |
| `l2TrendChart` | 双折线 | 年份、车系覆盖率、款型配备率 |
| `l2PriceBandChart` | 柱线图 | 价格带、款型配备率、行业均值 |
| `l2PriceBoxplotChart` | 箱线图 | 年份、指导价五数概括 |
| `highAdasTrendChart` | 双折线 | 年份、车系覆盖率、款型配备率 |
| `hudRateChart` | 折线图 | 年份、HUD 配备率 |
| `screenSizeChart` | 折线图 | 年份、平均屏幕尺寸、有效样本数 |
| `fridgeRateChart` | 折线图 | 年份、车载冰箱配备率 |
| `rearScreenRateChart` | 折线图 | 年份、后排多媒体屏配备率 |
| `zeroGravityRateChart` | 折线图 | 年份、零重力座椅配备率 |
| `rearMassageRateChart` | 折线图 | 年份、后排按摩座椅配备率 |
| `coreConfigTrendChart` | 柱线图 | 配置项、年份、款型/车系数量、配置率 |
| `coreConfigHeatmapChart` | 热力图 | 配置项、统计口径、年份、价格段/轴距段/级别、配置率 |

核心配置率查询的款型配备率与车系覆盖率必须使用独立计算的分子、分母和热力图矩阵。切换下拉框只更新本模块的两张图。

以下图表已删除，不得出现在 HTML 或 Payload：

```text
adasChipShareChart
lidarShareChart
cockpitChipRateChart
cockpitChipShareChart
```

## 四个 Gate

### Gate 1 — 计划完整性

- 章节集合等于本文件的必需章节集合。
- 图表 DOM id 集合等于本文件的 20 个图表集合。
- 每个任务声明输出路径和空数据策略。

### Gate 2 — 计算可审计性

- 所有比率都有分子、分母和颗粒度。
- 所有结论至少引用一个 `query_id`。
- 所有图表系列可追溯到计算结果。

### Gate 3 — Payload 校验

- 必需字段存在且类型正确。
- categories 与各 series 长度一致。
- 百分比在 `[0, 100]`，计数非负，日期可解析。
- 100% 堆叠图逐列合计满足误差规则。
- 示例状态和真实报告状态不可混用。

### Gate 4 — 页面验收

- 封面只显示报告标题与报告日期。
- 8 个章节和 20 个图表容器存在。
- 有数据图生成 SVG，无数据图显示明确空状态。
- ECharts 实例数量正确，控制台无 JavaScript 错误。
- 页面不存在示例值标记或未解析的 `{{variable}}`。

## 交付检查

- [ ] `quality.status` 为 `final`。
- [ ] `quality.completed_tasks === quality.plan_tasks`。
- [ ] 每个 `ready` 图表都有 `query_ids` 且能在 `evidence` 找到。
- [ ] 同一图的 categories 和每个 series.data 长度相等。
- [ ] 8 个章节顺序正确，“附件 · 数据口径”位于最后。
- [ ] 20 个图表容器存在，4 个已删除图表不存在。
- [ ] 页面没有示例数字、未解析占位或 JavaScript 错误。
- [ ] 每个比例说明颗粒度、分子和分母。
- [ ] 款型统计使用完整款型键去重。
- [ ] 车系覆盖率与款型配备率未混用。
- [ ] 纯电与全新能源分母未混用。
- [ ] 价格、尺寸、功率和电压单位明确。
- [ ] 时间序列连续性、缺失值和异常值已说明。
