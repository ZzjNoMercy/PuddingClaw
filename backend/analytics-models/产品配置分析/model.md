---
formatter: analytics-model
id: 产品配置分析
name: 产品配置分析
type: analysis_model
version: 0.2.0
description: 汽车行业已上市车型的配置分析，当用户询问单一车型的配置、多车型的配置对比以及行业的配置率发展趋势等场景使用。
tags:
- 汽车产品配置
- 配置率
- 行业趋势报告
- ECharts
data_assets:
  tables:
  - dbs_77982e981bac4a6fa8.vehicle_params
  - dbs_77982e981bac4a6fa8.vehicle_params_wide
semantic_assets:
  measures:
  - measure:charging_c_rate
  - measure:config_rate
  dimensions:
  - dimension:launch_time
  - dimension:price_band
  - dimension:brand
  - dimension:energy_type
  - dimension:vehicle_level
  - dimension:vehicle_series
  grains:
  - grain:car_model
  - grain:series
asset_relations:
- relation:车系配置关联
- relation:1111
guardrails:
- air_suspension_reference_type_value
- config_rate_model_key_group
- config_rate_no_exists_distinct
- config_rate_use_wide_denominator
- launch_time_no_car_name_year
- postgres_count_distinct_nullable_tuple_after_left_join
templates:
  product_config_report_html:
    path: ../../../designs/product-configuration-analysis/产品配置分析模型模板 v2.html
    reference: references/report-generation.md
    renderer: echarts
default_template: product_config_report_html
created: '2026-07-13 06:25:03'
updated_at: '2026-07-13 17:10:00'
---

# 产品配置分析

## 模型目标

汽车行业已上市车型的配置分析，当用户询问单一车型的配置、多车型的配置对比以及行业的配置率发展趋势等场景使用。

## 适用问题

- 单一车型或多个车型的配置查询与横向对比。
- 指定配置在不同年份、品牌、车系、能源、级别或价格带的配备率趋势。
- 新车迭代、尺寸动力、高压平台、智能驾驶、座舱舒适等行业专题分析。
- 生成包含完整章节、ECharts 图表和数据口径的产品配置分析 HTML 报告。

## 分析原则

- 优先用 `vehicle_params_wide` 筛选款型分母，再回连 `vehicle_params` 判断具体配置。
- 默认款型键为 `brand + serial_name + car_name`，不得只按 `car_name` 去重。
- 配置率、覆盖率、内部占比和市场占比必须分别说明分子、分母与颗粒度。
- 对未注册为语义资产的报告逻辑字段，按 reference 的字段映射查询，不得把逻辑字段名当作数据库物理列。

## 报告生成规范

生成或修改产品配置分析报告前，必须读取：

`references/report-generation.md`

该 reference 定义字段映射、必需章节、24 个图表契约和完整执行状态机。HTML 模板由 frontmatter 的 `default_template` 指定。

不得从 HTML 示例值反推分析结果，也不得边查询边改 HTML。必须先完成查询计划和全部计算，生成统一 `report_payload`，通过完整性校验后再一次性刷新 HTML。未通过 reference 中的四个 Gate 时，不得把报告标记为完成。

## 输出要求

- 先输出结论，再给数据证据、口径和异常说明。
- 报告封面只显示报告标题与报告日期。
- 使用默认 HTML 模板时必须保留全部必需章节；无数据图表必须显示缺失原因，不得用示例值冒充真实结果。
