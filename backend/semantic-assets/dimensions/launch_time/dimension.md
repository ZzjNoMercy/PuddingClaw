---
formatter: semantic-asset
name: 上市时间
type: dimension
description: 车型上市时间的标准取值口径，优先使用 vehicle_model_base。
aliases:
  - 上市日期
  - 上市年份
  - 上市月份
tags:
  - 汽车产品配置
  - vehicle_params
  - vehicle_model_base
version: 0.1.0
resolution_mode: derived
resolution:
  mode: derived
  bindings:
    - asset_ref: dbs_77982e981bac4a6fa8.vehicle_model_base
      display_name: insight_data · vehicle_model_base
      fields:
        value: launch_date
  source_fields: [launch_date]
  expression: 基于上市日期派生上市年份和上市月份；不得从 car_name 推断。
created: 2026-07-08 00:00:00
updated_at: 2026-07-09 00:00:00
---

# 上市时间

## 字段口径

优先使用 `vehicle_model_base`：

- 上市日期：`vehicle_model_base.launch_date`
- 上市年份：`vehicle_model_base.launch_year`
- 上市月份：`vehicle_model_base.launch_month`

只有当查询的表范围没有 `vehicle_model_base`，或需要回查原始明细时，才回退到 `vehicle_params`。

上市时间取 `vehicle_params` 表中 `type_name = '上市时间'` 的 `type_value`，格式为`yyyy-mm-dd`

如果要按年份、月份、日期分析，应先将该 `type_value` 转成日期，再从日期中提取 year / month / day。

## 可派生字段

- 上市日期：`type_name = '上市时间'` 的 `type_value` 转成 date。
- 上市年份：从上市日期提取 year。
- 上市月份：从上市日期提取 month。

## 禁止规则

- 不要从 `car_name` 中的 `21款`、`25款`、`26款` 推断上市年份。
- 如果可用表中包含 `vehicle_model_base`，不要回到 `vehicle_params` 用 `type_name = '上市时间'` 自关联计算上市年份，应直接使用 `launch_year` / `launch_date`。
- 不要从款型名称推断真实上市日期。
- 如果 `type_name = '上市时间'` 没有有效值，应把该车型视为上市时间未知，而不是用款型年份兜底。
