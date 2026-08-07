---
formatter: analytics-model
id: "产品配置分析"
name: "产品配置分析"
type: analysis_model
version: "0.4.0"
description: "汽车行业已上市车型的配置分析，当用户询问单一车型的配置、多车型的配置对比以及行业的配置率发展趋势等场景使用。"
tags: ["汽车产品配置","配置率","行业趋势报告","ECharts"]
data_assets:
  tables: ["dbs_77982e981bac4a6fa8.vehicle_params","dbs_77982e981bac4a6fa8.vehicle_model_base"]
semantic_assets:
  measures: ["measure:charging_c_rate","measure:config_rate","measure:launch_update_count","measure:launch_cycle"]
  dimensions: ["dimension:launch_time","dimension:price_band","dimension:wheelbase","dimension:motor_power","dimension:battery_energy","dimension:cltc_range","dimension:brand","dimension:energy_type","dimension:vehicle_level","dimension:vehicle_series"]
  grains: ["grain:car_model","grain:series"]
asset_relations: ["relation:车系配置关联"]
guardrails: ["air_suspension_reference_type_value","config_rate_model_key_group","config_rate_no_exists_distinct","config_rate_use_model_base_denominator","launch_time_no_car_name_year","postgres_count_distinct_nullable_tuple_after_left_join"]
references:
  analysis_rules:
    path: references/analysis-rules.md
    use_when: "所有产品配置分析任务"
templates:
  monthly_product_config_report:
    path: templates/monthly_product_config_report/index.html
    guide: templates/monthly_product_config_report/TEMPLATE.md
    assets:
      - templates/monthly_product_config_report/report-renderer.js
      - templates/monthly_product_config_report/echarts-6.1.0.min.js
    format: html
    use_when:
      - "刷新月报"
      - "刷新月度产品配置分析报告"
      - "生成产品配置分析月报"
      - "更新本月产品配置分析报告"
      - "按照月报模板说明重新更新图表"
    do_not_use_when:
      - "单一车型配置查询"
      - "多车型配置对比"
      - "临时专题分析或普通问答"
  topic_product_config_report:
    path: templates/topic_product_config_report/index.html
    guide: templates/topic_product_config_report/TEMPLATE.md
    assets:
      - templates/topic_product_config_report/report-theme.css
      - templates/topic_product_config_report/report-renderer.js
      - templates/topic_product_config_report/echarts-6.1.0.min.js
    format: html
    use_when:
      - "生成产品配置专题分析 HTML"
      - "生成可视化专题分析报告"
      - "把本次产品配置分析导出为 HTML"
      - "制作单次产品配置数据报告"
    do_not_use_when:
      - "刷新月报或生成月度产品配置分析报告"
      - "普通问数、单一车型配置查询或只需要文字结论"
      - "用户明确指定 Markdown、表格或其他非 HTML 交付格式"
acceptance:
  invariants:
  - type: classification_mapping_declaration
    target: dimension:energy_type
created: "2026-07-13 06:25:03"
updated_at: "2026-08-07 00:00:00"
---


# 产品配置分析

## 模型目标

汽车行业已上市车型的配置分析，当用户询问单一车型的配置、多车型的配置对比以及行业的配置率发展趋势等场景使用。

## 适用问题

- 单一车型或多个车型的配置查询与横向对比。
- 指定配置在不同年份、品牌、车系、能源、级别或价格带的配备率趋势。
- 新车迭代、尺寸动力、高压平台、智能驾驶、座舱舒适等行业专题分析。
- 按用户要求生成轻量、可交互的产品配置专题分析 HTML 报告。
- 生成包含完整章节、ECharts 图表和数据口径的月度产品配置分析 HTML 报告。

## 模板路由

- 模板必须根据用户 Query 的输出意图选择，不设置全局默认模板。
- 当用户明确要求“刷新月度产品配置分析报告”“生成产品配置分析月报”“更新本月报告”或同义表达时，选择 `templates.monthly_product_config_report`。
- 当用户明确要求把一次具体产品配置分析制作或导出为 HTML、可视化专题报告或单次数据报告时，选择 `templates.topic_product_config_report`。
- 选择任一模板后，必须依次读取 `references.analysis_rules.path`、所选模板的 `guide` 和 `path`，再执行查询与报告生成。
- 月报意图优先于专题意图；“刷新月报”“生成月报”等表达不得选择专题模板。
- 单一车型配置查询、多车型配置对比、普通问数、只需要文字结论或没有明确 HTML 交付意图的临时分析不选择模板，直接按用户要求输出。
- 比较每个模板的 `use_when` 与 `do_not_use_when`；无法唯一匹配时先确认用户需要的交付物类型。

## 分析原则

- 优先用 `vehicle_model_base` 筛选款型分母，再回连 `vehicle_params` 判断具体配置。
- 默认款型键为 `brand + serial_name + car_name`，不得只按 `car_name` 去重。
- 配置率、覆盖率、内部占比和市场占比必须分别说明分子、分母与颗粒度。
- 对未注册为语义资产的报告逻辑字段，按 reference 的字段映射查询，不得把逻辑字段名当作数据库物理列。

## 默认分析范围

- 默认分析中国狭义乘用车。
- 用户未明确要求“包含皮卡”或“只看皮卡”时，必须排除车型级别为 `皮卡` 的车型。
- 该默认范围适用于配置查询、更新次数、上市周期、行业趋势和报告生成。
- 使用 `vehicle_model_base` 时按 `vehicle_level` 排除；回退到 `vehicle_params` 时，必须在款型聚合中读取 `type_name = '级别'` 并排除 `type_value = '皮卡'`。
- 用户明确指定车型范围时，以用户要求为准。

## 输出要求

- 先输出结论，再给数据证据、口径和异常说明。
- 使用专题 HTML 模板时，章节与图表数量按问题裁剪，不得套用月报的固定 8 章和 20 图结构。
- 专题 HTML 只替换报告副本的 `#report-payload` 完整 JSON，不修改模板 DOM、样式、渲染器或写入任意 ECharts option。
- 选择月度模板时，标题使用“YYYY年MM月产品配置分析报告”，分析周期使用 `YYYY-MM`。
- 报告封面只显示报告标题与报告日期。
- 使用月度 HTML 模板时必须保留全部必需章节；无数据图表必须显示缺失原因，不得用示例值冒充真实结果。
- 生成 HTML 时只替换报告副本的 `#report-payload` 完整 JSON，不修改模板 DOM id 或 `report-renderer.js`。
- 月度报告固定输出 20 张图；不生成智驾芯片份额、激光雷达供应商份额、车机芯片披露率和车机芯片型号结构图。
