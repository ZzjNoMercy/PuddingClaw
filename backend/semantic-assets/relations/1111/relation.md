---
formatter: asset-relation
name: '车系款型基础表关联'
type: relation
description: 将款型基础表按品牌和车系关联到规范车系维度
aliases: []
tags: []
version: 0.1.0
created: '2026-07-13 05:14:35'
updated_at: '2026-07-13 05:14:35'
relation_type: dimension_binding
relation:
  asset:
    ref: dbs_77982e981bac4a6fa8.vehicle_model_base
    display_name: insight_data · vehicle_model_base
    key_fields:
    - brand
    - serial_name
  dimension:
    ref: dimension:vehicle_series
    display_name: 车系
    key_fields: []
    output_key: entity_key
  cardinality: many_to_one
  grain:
  - brand
  - serial_name
  use_statuses:
  - auto_matched
  - accepted
  rules: []
---

# 1111

## 类型

资产关联

## 业务口径

将 `vehicle_model_base` 的每个款型按 `brand + serial_name` 关联到规范车系维度。同一车系可以包含多个款型，禁止把该关系理解为车系与款型一对一。

## 关联方式

dimension_binding

前置的 `relation` 是机器可读定义；下方补充业务边界、基数、粒度和重复计数风险。

## 查询规则

- 明确需要使用的字段或 type_name 口径。
- 明确禁止从名称猜测字段含义。
- 如需分组、筛选或去重，在这里写清楚。

## SQL Hint

```sql
-- 可选：写入 SQL 片段或字段映射提示。
```
