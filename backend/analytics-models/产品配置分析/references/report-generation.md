# 产品配置分析报告生成规范

## 目录

- [使用方式](#使用方式)
- [Agent 执行契约](#agent-执行契约)
- [数据资产与颗粒度](#数据资产与颗粒度)
- [字段映射](#字段映射)
- [维度与度量目录](#维度与度量目录)
- [必需章节](#必需章节)
- [图表规则](#图表规则)
- [生成流程](#生成流程)
- [质量检查](#质量检查)

## 使用方式

生成产品配置分析 HTML 报告前，先读取本文件，再读取模型 `default_template` 指向的 HTML。

模板当前路径：

`../../../designs/product-configuration-analysis/产品配置分析模型模板 v2.html`

图表数据与 ECharts 配置：

`../../../designs/product-configuration-analysis/product-config-charts.js`

不要把模板中的示例数字视为查询结果。查询失败或字段缺失时，保留版式并明确标注“数据不足 / 未查询到”，不得伪造数据。

HTML 是输出视图，不是计算指令。Agent 不得逐个搜索并替换 HTML 中的示例数字；必须先生成完整的结构化数据包，再由统一渲染入口刷新页面。

## Agent 执行契约

### 状态机

Agent 必须按顺序执行，禁止跨阶段：

```text
S0 解析任务
  → S1 编译报告计划
  → Gate 1：计划覆盖全部必需章节和图表
  → S2 执行查询与计算
  → Gate 2：每个结果都有口径和证据
  → S3 组装 report_payload
  → Gate 3：payload 结构、数组长度和数值规则通过
  → S4 渲染 HTML
  → Gate 4：页面、24 个图表和运行时检查通过
  → S5 交付
```

任何 Gate 失败时只能回到上一阶段修复。不得跳过失败项后继续交付。

### S1：编译报告计划

先把 HTML 反向编译成任务清单，而不是直接修改 HTML。每个必需章节和图表生成一个计算任务：

```json
{
  "task_id": "chart.l2TrendChart",
  "section": "adas",
  "output_path": "charts.adas.l2_trend",
  "grain": ["launch_year"],
  "dimensions": ["launch_year", "adas_level"],
  "measures": ["series_coverage_rate", "config_rate"],
  "filters": {"vehicle_scope": "passenger_vehicle"},
  "status": "pending"
}
```

计划必须包含：

- 8 个章节任务。
- 24 个图表任务。
- 封面、KPI、结论、竞企表格和口径说明任务。
- 每个任务的颗粒度、维度、度量、筛选、输出路径和空数据策略。

### S2：查询与计算

- 先计算所有章节共用的分母数据集，再计算配置标记和聚合，避免每张图重复定义口径。
- 每个查询结果必须保存 `query_id`、SQL/工具调用摘要、行数、颗粒度、筛选、数据截止日和异常。
- 每个派生指标必须保存 `value`、`unit`、`numerator`、`denominator`、`grain` 和 `source_query_ids`。
- 图表只能消费已完成的计算结果，不得在 ECharts 配置中临时计算业务指标。
- Agent 必须完成整个计划后才能进入 payload 组装阶段。

### S3：统一数据包

统一输出一个 `report_payload`，最小结构如下：

```json
{
  "schema_version": "1.0",
  "report": {
    "report_title": "2024 年产品配置现状及趋势分析",
    "report_date": "2025-02-23",
    "data_cutoff": "2024-12-31",
    "scope": {},
    "metrics": {},
    "sections": {},
    "tables": {},
    "methodology": {}
  },
  "charts": {},
  "evidence": {},
  "quality": {
    "plan_tasks": 0,
    "completed_tasks": 0,
    "missing_tasks": [],
    "warnings": []
  }
}
```

约束：

- `report` 负责文本、KPI、章节和表格。
- `charts` 只保存 ECharts 所需的类别、系列和数值，不保存结论文本。
- `evidence` 以 `query_id` 为键保存证据与口径，供审计和回查。
- `quality.completed_tasks` 必须等于 `quality.plan_tasks`；否则只能输出草稿状态。
- 空数据使用 `status: "no_data"` 和 `reason`，不得放入模板示例值。

### S4：一次性刷新 HTML

正确流程：

1. 复制默认 HTML 模板为新的报告文件，不修改基准模板。
2. 把 `report_payload` 注入报告文件的 JSON 数据节点。
3. 文本渲染器根据字段路径刷新标题、KPI、章节文案和表格。
4. 图表渲染器把 `payload.charts` 传给 ECharts，销毁旧实例后重建全部图表。
5. 写入 `rendered_at`、payload 校验摘要和数据截止日。
6. 在浏览器加载生成文件并执行 Gate 4。

禁止流程：

- 直接改 `product-config-charts.js` 中的示例数组。
- 使用正则逐个替换页面里的数字。
- 查询一张图就刷新一次 HTML。
- 保留没有证据来源的模板示例卡片或表格。
- 让 Agent 自由增删章节或改变图表 DOM id。

### 四个 Gate

**Gate 1 — 计划完整性**

- 章节集合必须等于 reference 的必需章节集合。
- 图表 DOM id 集合必须等于 reference 的 24 张图表集合。
- 每个任务必须声明输出路径与空数据策略。

**Gate 2 — 计算可审计性**

- 所有比率都有分子、分母和颗粒度。
- 所有结论至少引用一个 `query_id`。
- 所有图表系列可追溯到计算结果，不接受手填数组。

**Gate 3 — Payload 校验**

- 必需字段存在，类型正确。
- 同一图表的 categories 与各 series 长度一致。
- 百分比在 `[0, 100]`，计数非负，日期可解析。
- 100% 堆叠图逐列合计满足误差规则。
- 示例状态和真实报告状态不可混用。

**Gate 4 — 页面验收**

- 封面只显示报告标题与报告日期。
- 8 个章节和 24 个图表容器存在。
- 有数据的 24 张图均生成 canvas/SVG；无数据图显示明确空状态。
- 控制台无 JavaScript 错误，ECharts 实例数量正确。
- 页面中不存在模板示例值标记或未解析的 `{{variable}}`。

## 数据资产与颗粒度

### 数据表职责

| 数据表 | 颗粒度 | 用途 |
| --- | --- | --- |
| `vehicle_model_base` | 一行一个款型 | 分母、时间/品牌/车系/能源/级别/轴距/电机总功率/价格/销售状态筛选 |
| `vehicle_params` | 一行一个款型配置项 | 判断具体配置、配置值、配置供应商或硬件型号 |

### 标准键

- 款型：`brand + serial_name + car_name`
- 车系：`brand + serial_name`；跨源分析时使用 `dimension:vehicle_series` 的 `entity_key`
- 默认车型范围：狭义乘用车，排除 `vehicle_level = '皮卡'`
- 默认时间口径：使用 `launch_date / launch_year`，不得从款型名称推断年份

### 配置查询路径

1. 用 `vehicle_model_base` 形成唯一款型分母。
2. 用完整款型键连接 `vehicle_params`。
3. 按目标配置的 `type_name + type_value` 规则生成搭载标记。
4. 先在款型颗粒度去重，再聚合到车系、品牌、价格带或年份。

## 字段映射

### 报告元数据

| 模板字段 | 类型 | 来源/生成规则 |
| --- | --- | --- |
| `report_title` | string | 根据分析范围生成，封面主标题 |
| `report_date` | date | 报告生成日期，只用于封面 |
| `data_cutoff` | date | 数据最大有效日期，写入数据口径章节 |
| `scope.market` | string | 默认“中国乘用车” |
| `scope.year` | integer/range | 用户指定；缺失时取完整可比年度 |
| `scope.energy_type` | string[] | 使用 `dimension:energy_type` 精确值 |
| `scope.price_band` | string[] | 使用 `dimension:price_band` |

### 物理维度字段

| 逻辑维度 | 首选字段 | 回退规则 |
| --- | --- | --- |
| 品牌 | `vehicle_model_base.brand` | `vehicle_params.brand` |
| 车系 | `vehicle_model_base.serial_name` | `vehicle_params.serial_name` |
| 款型 | `vehicle_model_base.car_name` | `vehicle_params.car_name` |
| 上市日期/年/月 | `launch_date / launch_year / launch_month` | EAV `type_name='上市时间'` 后解析 |
| 能源类型 | `energy_type` | EAV `type_name='能源类型'` |
| 车型级别 | `vehicle_level` | EAV `type_name='级别'` |
| 轴距/轴距段 | `wheelbase_mm` + `dimension:wheelbase` | EAV `type_name='轴距[mm]'` 后数值化 |
| 电机总功率/功率段 | `motor_power_kw` + `dimension:motor_power` | EAV `type_name='电动机总功率[kW]'` 后数值化；不得由前后电机功率相加 |
| 指导价/价格带 | `price / price_band` | EAV `type_name='厂商指导价'` 后数值化 |
| 销售状态 | `sale_status` | 无可靠值时标记未知 |
| 配置类别 | `vehicle_params.category` | 不可从 `type_name` 猜测 |
| 配置项 | `vehicle_params.type_name` | 必须使用数据库实际枚举 |
| 配置值 | `vehicle_params.type_value` | 空值、`-`、`无`、`未配备`、`不配备`默认视为未搭载 |

### 报告模块字段

| 模板对象 | 必需内容 |
| --- | --- |
| `metrics` | 核心 KPI 值、单位、统计年、颗粒度、分子、分母 |
| `iteration_trend` | 年份、能源组、更新次数、更新周期、竞品排行 |
| `size_power_matrix` | 轴距段、电机功率段、排量段、组合数量/占比 |
| `voltage_platform` | 能源范围、平台电压、年份/价格带、款型数、占比 |
| `adas_analysis` | 智驾等级/功能、车系覆盖率、款型配备率、价格分布、芯片/雷达供应商 |
| `cockpit_comfort` | 配置项、年度配备率、价格带覆盖率、芯片型号、屏幕尺寸 |
| `methodology` | 数据源、数据截止日、车型范围、颗粒度、分子分母、缺失值处理 |

## 维度与度量目录

### 已注册语义资产

当前模型已注册：

- 度量：`measure:config_rate`、`measure:charging_c_rate`
- 维度：`dimension:launch_time`、`dimension:price_band`、`dimension:wheelbase`、`dimension:motor_power`、`dimension:brand`、`dimension:energy_type`、`dimension:vehicle_level`、`dimension:vehicle_series`
- 颗粒度：`grain:car_model`、`grain:series`

这些资产足以支持基础配置率、轴距和电机功率分析，但不足以单独表达完整行业报告。下表中已注册的维度必须优先使用其语义资产；其余报告级逻辑字段需要在查询结果中显式计算，在正式创建对应语义资产前不要写入模型 `semantic_assets` 列表。

### 最低必需维度

| 维度 | 示例 | 建议实现 |
| --- | --- | --- |
| 款型 | 2025 款某配置版 | 使用 `grain:car_model` 与完整款型键 |
| 配置类别/配置项/配置值 | 智能驾驶 / 激光雷达 / 1 个 | EAV `category/type_name/type_value` |
| 更新类型 | 新增、改款、换代 | 由同车系上市序列派生，需保留规则说明 |
| 轴距段 | 2800–2850mm | 使用 `dimension:wheelbase` |
| 电机功率段 | 150–200kW | 使用 `dimension:motor_power` |
| 排量段 | 1.5L、2.0L | 从发动机排量标准化 |
| 电压平台 | 400V、800V、900V | 从平台电压/电池电压配置标准化 |
| 智驾能力 | L2、L2+、高速 NOA、城市 NOA | 按配置识别规则派生，不只匹配名称 |
| 智驾硬件 | 芯片、激光雷达及供应商 | 从配置值标准化厂商与型号 |
| 座舱舒适配置 | HUD、冰箱、后排屏、按摩、零重力座椅 | 每项使用明确配置识别规则 |
| 指导价统计位置 | 最小值、Q1、中位数、Q3、最大值 | 从款型指导价分布派生 |

### 最低必需度量

| 度量 | 公式/口径 |
| --- | --- |
| `model_count` | `COUNT(DISTINCT brand, serial_name, car_name)` |
| `series_count` | `COUNT(DISTINCT brand, serial_name)`；跨源时统计 `entity_key` |
| `new_model_count` | 统计期内首次上市的唯一款型数 |
| `renewal_count` | 按明确的新增/改款/换代规则统计更新事件数 |
| `renewal_cycle_days` | 同一分析对象相邻有效更新日期的天数；报告中说明均值或中位数 |
| `equipped_model_count` | 分母款型中满足配置识别规则的唯一款型数 |
| `config_rate` | `equipped_model_count / eligible_model_count` |
| `series_coverage_rate` | 至少一个款型搭载的车系数 / 分母车系数 |
| `internal_share` | 某配置子类型款型数 / 已搭载该大类配置的款型数 |
| `market_share` | 某配置款型数 / 当前市场范围全部有效款型数 |
| `year_over_year_change` | 本期值 / 上期值 - 1；百分比变化 |
| `percentage_point_change` | 本期比例 - 上期比例；单位百分点 |
| `median_price`、`price_q1`、`price_q3` | 在去重款型价格上计算，不在 EAV 行上计算 |
| `average_screen_size` | 有效屏幕尺寸数值的平均值，同时披露有效样本数 |

## 必需章节

最终 HTML 必须按以下顺序生成：

1. **执行摘要**：封面只显示 `report_title` 与 `report_date`；封面下方展示 3–5 个 KPI 和 3–5 条结论。
2. **新车迭代**：更新次数、更新周期、能源结构与重点竞企节奏。
3. **尺寸与动力**：轴距、电机功率及尺寸 × 功率组合。
4. **高压平台**：纯电与全新能源分别统计年度趋势和价格带结构。
5. **智能驾驶**：L2/L2+、高阶 NOA、指导价分布、智驾芯片和激光雷达。
6. **座舱与舒适**：座舱芯片、HUD、屏幕尺寸、冰箱、后排屏、零重力和按摩座椅。
7. **核心配置率查询**：按配置项、款型/车系口径及价格段/轴距段/级别查询历年搭载规模与配置率。
8. **附件 · 数据口径**：必须置于报告最后；集中说明数据源、截止日期、范围、颗粒度、分子分母、排除项、缺失与异常。

若某专题无可靠字段，章节仍保留，但用缺失说明替代图表，并在数据口径章节记录原因。

## 图表规则

### 通用规则

- 使用模板内置 ECharts，不使用图片模拟数据图表。
- 每张图必须有标题、单位、时间范围、图例、来源/口径说明和无数据状态。
- 比率统一使用 0–100% 轴；原始数据使用数值比例或百分数时必须统一后再渲染。
- 趋势图按时间升序；价格带按数值区间升序；品牌/供应商横条按值降序。
- 100% 堆叠图每个时间点合计允许有四舍五入误差，绝对误差不得超过 0.2 个百分点。
- 图表中的款型、车系口径不得混用；同图同时展示时必须在系列名中标注。
- 纯电与全新能源必须使用两个独立分母，不得合并为一条无口径说明的趋势。
- 少于 3 个有效时间点时不画趋势线，改用单期条形图或数据卡片。
- 箱线图必须从款型级价格样本计算 `[下须, Q1, median, Q3, 上须]`；上下须采用 1.5×IQR 范围内的最远实际观测值，范围外价格作为独立离群点系列展示，不得把绝对最高价直接当作上须。
- 热力图单元格使用唯一款型数或占比，并明确一种；不得在同一图混用。
- 多系列图表统一使用图例聚焦交互：首次点击某个图例只显示该系列，再次点击同一图例恢复全部系列；不得触发其他图表重绘。

### 模板图表契约（24 张）

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
| `adasChipShareChart` | 横向条形图 | 智驾芯片型号/平台、内部占比 |
| `lidarShareChart` | 横向条形图 | 激光雷达供应商、内部占比 |
| `cockpitChipRateChart` | 折线图 | 年份、芯片型号披露率 |
| `hudRateChart` | 折线图 | 年份、HUD 配备率 |
| `screenSizeChart` | 折线图 | 年份、平均屏幕尺寸、有效样本数 |
| `cockpitChipShareChart` | 横向条形图 | 座舱芯片型号、内部占比 |
| `fridgeRateChart` | 折线图 | 年份、车载冰箱配备率 |
| `rearScreenRateChart` | 折线图 | 年份、后排多媒体屏配备率 |
| `zeroGravityRateChart` | 折线图 | 年份、零重力座椅配备率 |
| `rearMassageRateChart` | 折线图 | 年份、后排按摩座椅配备率 |
| `coreConfigTrendChart` | 柱线图 | 配置项、年份、款型/车系数量、配置率 |
| `coreConfigHeatmapChart` | 热力图 | 配置项、统计口径（款型/车系）、年份、价格段/轴距段/级别、配置率 |

核心配置率查询的款型配备率与车系覆盖率必须使用独立计算的分子、分母和热力图矩阵，不得复用同一组数值。切换下拉框只更新本模块的两张图；滚动页面或进入视口不得触发查询或全页重绘。

## 生成流程

1. 解析用户问题，确定市场、时间、能源、品牌/车系、级别、价格带和报告深度。
2. 读取模型已注册的度量、维度、颗粒度和 guardrails。
3. 读取本 reference 与默认 HTML 模板，编译完整报告计划并通过 Gate 1。
4. 先查询共用分母与数据覆盖，再执行全部章节计算并通过 Gate 2。
5. 基于计算结果生成证据、结论与统一 `report_payload`，通过 Gate 3。
6. 复制基准模板并一次性注入 payload，文本与图表统一刷新。
7. 浏览器加载生成文件并通过 Gate 4 后才可交付。

## 质量检查

- [ ] 封面只包含报告标题和报告日期。
- [ ] 8 个必需章节均存在且顺序正确，“附件 · 数据口径”位于最后。
- [ ] 24 个图表容器均存在；有数据时全部生成 canvas 或 SVG。
- [ ] 所有示例数字已被真实查询结果或缺失状态替换。
- [ ] 每个比例都能说明颗粒度、分子和分母。
- [ ] 每个款型统计都使用完整款型键去重。
- [ ] 车系覆盖率与款型配备率未混用。
- [ ] 纯电与全新能源分母未混用。
- [ ] 价格单位统一为万元，尺寸/功率/电压单位明确。
- [ ] 时间序列连续性、缺失值和异常值已说明。
- [ ] ECharts 无运行时错误，图表在常用桌面宽度下不溢出。
