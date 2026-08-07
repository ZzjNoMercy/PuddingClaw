---
formatter: analytics-template
id: topic_product_config_report
name: 产品配置专题分析报告模板
version: "1.0.0"
semantic_scope:
  enum_filters:
    dimension:energy_type:
      members: ["纯电", "插电混合", "增程式纯电动", "汽油", "汽油+48V轻混系统", "油电混合", "汽油电驱", "汽油+24V轻混系统"]
      classifications: ["新能源", "传统能源"]
---

# 产品配置专题分析报告模板

这是 `topic_product_config_report` 的唯一使用说明。它把一次具体的产品配置分析制作成轻量、可交互的 HTML 专题报告，与固定周期、固定章节的月度报告严格区隔。

## 何时使用

仅当用户明确要求以下 HTML 交付物或同义表达时使用：

- 生成产品配置专题分析 HTML。
- 生成可视化专题分析报告。
- 把本次产品配置分析导出为 HTML。
- 制作一次性的产品配置数据报告。

以下任务不得使用本模板：

- 刷新月报、生成月报或更新月度产品配置分析报告。
- 普通问数、单一车型配置查询、只需要文字结论或只需要表格。
- 用户明确指定 Markdown、Excel、PPT、PDF 或其他非 HTML 交付格式。
- 仅出现“临时分析”“专题分析”等词，但没有明确要求生成 HTML 报告。

使用前先读取 `model.md` 注册的通用 `references.analysis_rules.path`。月报意图与专题意图同时出现时，选择月度模板。

## 模板资源包

平台内运行时必须使用 `analytics_model_context.resolved_templates` 给出的完整虚拟路径；导出项目中使用 `analysis-project.yaml` 的项目相对路径。不得手工拼接或用 `glob` 猜测路径。

- HTML 基准模板：`index.html`。
- 视觉 Token 与组件样式：`report-theme.css`。
- Payload 校验、内容与图表渲染器：`report-renderer.js`。
- 本地 ECharts：`echarts-6.1.0.min.js`。

模板不含示例数据。`#report-payload` 中的空数组、`null` 和 `quality.status: "draft"` 是待生成占位，不是查询结果。

Agent 必须把四个文件复制到新报告的同一目录。任一资源缺失时停止并报告缺失路径，不得生成不完整报告。

## 不可变约束

完成查询与计算后，只对报告副本一次性替换页尾 `<script id="report-payload" type="application/json">` 内的完整 JSON。

不得修改：

- 基准模板、DOM id、资源引用或页面结构。
- `report-theme.css`、`report-renderer.js` 或 ECharts vendor。
- Payload 以外的 HTML 文本和数字。
- 任意 ECharts option、JavaScript 函数、HTML 片段或可执行表达式。

所有用户可见文本都必须作为普通字符串进入 Payload，由渲染器以 `textContent` 输出。

## 与月报的结构区隔

| 专题报告 | 月度报告 |
| --- | --- |
| 围绕一个具体问题 | 覆盖固定月度全景 |
| 章节和图表按问题裁剪 | 固定 8 章、20 图 |
| 无侧边章节导航 | 固定目录与章节顺序 |
| 推荐 1–8 个内容区块 | 必须完成全部计划任务 |
| 文件名包含专题 slug | 文件名包含 `YYYY-MM` |

不得为了显得完整而补充与用户问题无关的 KPI、图表、表格或结论。

## Agent 执行契约

```text
S0 确认用户明确需要专题 HTML，并定义一个核心分析问题
  → S1 编译最小充分的指标、结论与内容区块计划
  → Gate 1：每个区块都有颗粒度、筛选、度量和空数据策略
  → S2 查询、计算并保存证据
  → Gate 2：每个数值可追溯到 query_id
  → S3 组装并校验统一 Payload
  → Gate 3：无任意代码、ready 项证据完整
  → S4 复制模板资源并替换 #report-payload
  → Gate 4：HTML 结构、浏览器运行时、主题切换和响应式验收通过
  → S5 交付
```

任何 Gate 失败时回到上一阶段修复。查询成功但无数据时使用 `status: "no_data"` 和明确原因；不得用估算值、演示值或虚构数据补齐。

## 内容规划

专题报告默认按以下顺序组织，但只保留对当前问题有解释价值的部分：

1. 标题、分析范围、数据截止日。
2. 一段结论摘要。
3. 0–5 个关键 KPI；没有真正关键指标时可以为 0。
4. 0–5 条核心发现；每条都必须引用证据。
5. 1–8 个图表、表格或小倍数区块。
6. 口径、限制和数据异常说明。

内容必须先结论后证据。KPI、发现和区块不得重复表达同一信息。

## 统一 Payload

```json
{
  "schema_version": "1.0",
  "report": {
    "title": "专题标题",
    "subtitle": "时间范围 · 分析对象 · 核心指标",
    "summary": "一段结论摘要",
    "report_date": "YYYY-MM-DD",
    "data_cutoff": "YYYY-MM-DD",
    "scope": {
      "period": "分析周期",
      "market": "中国狭义乘用车",
      "grain": "款型",
      "filters": ["排除皮卡"]
    }
  },
  "kpis": [],
  "insights": [],
  "blocks": [],
  "methodology": [],
  "evidence": {},
  "quality": {
    "status": "draft",
    "warnings": []
  }
}
```

### KPI

```text
{
  label, value, unit?, hint?, tone?, query_ids
}
```

- `value` 必须是数字或已经明确格式化的短文本。
- `tone` 只允许 `neutral`、`accent`、`positive`、`caution`。
- 每个 KPI 必须提供非空 `query_ids`，并在 `evidence` 中存在对应记录。

### 核心发现

```text
{
  kicker?, title, summary, tone?, query_ids
}
```

- `summary` 解释“发生了什么”和“为什么重要”，不得只复述标题。
- `tone` 取值与 KPI 相同。
- 每条发现必须提供证据。

### 内容区块

所有区块共享：

```text
{
  id,
  kind,
  status: "ready" | "no_data" | "pending",
  title,
  subtitle?,
  reason?,
  query_ids
}
```

- `id` 只能包含 ASCII 字母、数字、`-`、`_`，且在报告内唯一。
- `ready` 必须有非空 `query_ids`；`no_data` 和 `pending` 必须有 `reason`。
- `kind` 只允许 `chart`、`small_multiples`、`table`。

#### 图表区块

```text
{
  kind: "chart",
  chart: {
    type: "line" | "bar" | "stacked_bar" | "combo" | "heatmap" | "scatter",
    categories?, x_categories?, y_categories?,
    series?, data?,
    x_name?, y_name?, unit?, min?, max?
  }
}
```

- `line`、`bar`、`stacked_bar`、`combo` 使用 `categories + series[]`。
- 普通 `series[]` 项为 `{name, data, type?, axis?, stack?, unit?}`；数组长度必须与 `categories` 一致。
- `combo` 的 `series.type` 只允许 `line` 或 `bar`，`axis` 只允许 `0` 或 `1`。
- `heatmap` 使用 `x_categories + y_categories + data`；`data` 为 `[xIndex, yIndex, value]`。
- `scatter` 使用 `series[].data`；每个点为 `[x, y]` 或 `[x, y, label]`。
- 不允许传入颜色、字体、tooltip formatter、label formatter 或其他 ECharts option；这些由渲染器统一决定。

#### 小倍数区块

```text
{
  kind: "small_multiples",
  items: [
    {title, note?, chart: {type: "line" | "bar", categories, series}}
  ]
}
```

每个 item 只表达一个维度成员或一个可比对象。所有 item 应使用相同时间范围和度量单位。

#### 表格区块

```text
{
  kind: "table",
  columns: [
    {key, label, align?, format?, unit?}
  ],
  rows: []
}
```

- `align` 只允许 `left`、`center`、`right`。
- `format` 只允许 `text`、`integer`、`decimal`、`percent`、`delta`。
- 行数据只能包含可序列化的普通值，不得包含 HTML。

### 方法与证据

```text
methodology[] = {title, description, query_ids?}

evidence.<query_id> = {
  question,
  grain,
  filters,
  row_count,
  data_cutoff,
  note?
}
```

每个比率必须能从证据追溯分子、分母、颗粒度和筛选；可以写在 `question` 或 `note` 中。数据库物理 SQL 不在页面正文展示，除非用户明确要求。

## 视觉与交互契约

- 暖白与近黑双主题，首次加载遵循系统主题，用户选择保存到本地。
- 内容最大宽度 1180px；小屏自动改为单列，不设置桌面最小宽度。
- 使用系统 CJK 字体；标题紧凑，正文中文行高不低于 1.7。
- KPI、发现和内容区块使用轻边框、单层卡片；不使用渐变、玻璃拟态或装饰性大阴影。
- 图表统一使用蓝、橙、绿、琥珀四色；传统能源或基准系列可以使用中性灰。
- 表格支持横向滚动、粘性表头和数值右对齐。
- 打印时固定浅色主题、隐藏主题按钮，并尽量避免卡片跨页断裂。
- 主题切换、窗口缩放、空数据和渲染错误都必须有可见且可理解的状态。

## 命名与交付

- 每次生成新报告，不覆盖模板或已有报告。
- 推荐目录：`reports/topic/`。
- 推荐文件名：`topic-product-config-<topic-slug>-YYYYMMDD.html`。
- `<topic-slug>` 使用简短 ASCII kebab-case，例如 `l2-price-band`、`nev-voltage-platform`。
- 交付时同时复制所有资源文件；不得让专题报告引用月报目录中的 ECharts 或 renderer。

## 验收清单

- 用户明确要求专题 HTML，且未命中月报。
- 只保留与核心问题直接相关的内容。
- Payload 通过渲染器校验，`quality.status` 为 `ready` 或带有明确 warnings。
- 所有 ready KPI、发现和区块都有有效 `query_ids`。
- 无示例值、虚构值、任意脚本、HTML 字符串或 ECharts option。
- 页面在浅色、深色、窄屏和打印布局下可读。
- ECharts、主题切换、表格滚动和窗口 resize 无运行时错误。
