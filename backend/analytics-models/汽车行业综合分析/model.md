---
formatter: analytics-model
id: "汽车行业综合分析"
name: "汽车行业综合分析"
type: analysis_model
version: "0.2.1"
description: "分析汽车行业销量、产品规划、技术规划、发展趋势等"
tags: ["汽车销量","汽车产品配置分析"]
data_assets:
  tables: ["table_asset:tbl_23fff15978050fdc18330ab2","dbs_77982e981bac4a6fa8.vehicle_params","dbs_77982e981bac4a6fa8.vehicle_model_base","table_asset:tbl_concat_847eed5f3f93dd93e4cb7111"]
semantic_assets:
  measures: ["measure:charging_c_rate","measure:config_rate","measure:launch_cycle","measure:launch_update_count","measure:销量","measure:sales"]
  dimensions: ["dimension:launch_time","dimension:price_band","dimension:wheelbase","dimension:brand","dimension:energy_type","dimension:vehicle_level","dimension:vehicle_series"]
  grains: ["grain:car_model","grain:series"]
asset_relations: []
guardrails: ["air_suspension_reference_type_value","config_rate_model_key_group","config_rate_no_exists_distinct","config_rate_use_model_base_denominator","launch_time_no_car_name_year","postgres_count_distinct_nullable_tuple_after_left_join"]
templates: {}
default_template: null
created: "2026-07-09 15:42:24"
updated_at: "2026-07-15 14:42:21"
---

# 汽车行业综合分析

## 模型目标

描述这个分析模型解决的业务问题。

## 适用问题

- 写出用户可能提出的问题。

## 分析原则

- 明确优先使用的数据资产和语义资产。
- 明确必须说明的口径、分母、分子和排除规则。
- 明确缺少关键参数时是否追问。

## 跨源实体解析

当任务需要把销量/上险量表与产品配置库联合分析时，先读取：

`/semantic-assets/dimensions/vehicle_series/dimension.md`

再读取其中与当前数据资产相符的 `references/*.json` crosswalk。销量侧用 `品牌 + 1-子车型`，配置侧用 `brand + serial_name` 查找同一 `entity_key`；不得直接按原始名称或仅按车系名连接。

只有 `join_eligible=true` 的 `auto_matched` 或 `accepted` 记录可用于正式统计。`candidate` 与 `unmatched` 必须保留为覆盖范围说明，不能静默合并。

## 输出要求

- 输出核心结论、数据证据和异常说明。
- 如果引用模板，按模板组织最终结果。
