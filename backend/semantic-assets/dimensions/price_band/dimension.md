---
formatter: semantic-asset
name: 价格段
type: dimension
description: 基于厂商指导价划分价格区间，优先使用 vehicle_model_base。
aliases:
  - 价格区间
  - 价位
  - 售价区间
  - 指导价区间
tags:
  - 汽车产品配置
  - vehicle_params
  - vehicle_model_base
  - 价格
version: 0.2.0
resolution_mode: derived
resolution:
  mode: derived
  bindings:
    - asset_ref: dbs_77982e981bac4a6fa8.vehicle_model_base
      display_name: insight_data · vehicle_model_base
      fields:
        value: price
  source_fields: [price]
  expression: 按厂商指导价（万元）划分互斥且完整的价格区间；默认可读取已固化的 price_band，用户指定边界时直接使用 price 重算。
created: 2026-07-08 00:00:00
updated_at: 2026-08-07 00:00:00
---

# 价格段

## 字段口径

优先使用 `vehicle_model_base`：

- 指导价：`vehicle_model_base.price`
- 价格段：`vehicle_model_base.price_band`

只有当查询的表范围没有 `vehicle_model_base`，或需要回查原始明细时，才回退到 `vehicle_params`。

价格段基于 `vehicle_params` 表中 `type_name = '厂商指导价'` 的 `type_value` 划分。

数据库中厂商指导价的单位是万元。

## 常用价格段

- 5万元以下
- 5-10万元
- 10-15万元
- 15-20万元
- 20-30万元
- 30-40万元
- 40-50万元
- 50万元以上
- 未定价

默认区间采用左闭右开：`5-10万元` 表示 `price >= 5 AND price < 10`。`50万元以上` 表示 `price >= 50`。

`未定价` 只包含 `price IS NULL OR price <= 0`。有效正价格不得归入 `未定价`：例如 `0 < price < 5` 应归入 `5万元以下`。如用户指定其他价格段，以用户指定区间为准；落在指定区间之外的有效价格应排除或明确标为“其他”，不得伪装成未定价。

## 计算规则

按款型颗粒度分析时，价格段直接由该 `car_name` 的 `厂商指导价` 决定。

按车系颗粒度分析时，同一车系下可能存在低配款型在较低价格段、高配款型在较高价格段的跨价格段情况。除非用户另有要求，默认以该车系最低配款型的价格段为准，并在答案中说明该默认口径。

## SQL Hint

如果使用 `vehicle_model_base`：

```sql
WHERE price_band = '<价格段>'
```

或直接比较：

```sql
WHERE price >= 20 AND price < 30
```

需要输出完整默认价格段组合时，使用以下互斥 CASE 口径；标签与排序表必须包含同一组分类：

```sql
CASE
  WHEN price IS NULL OR price <= 0 THEN '未定价'
  WHEN price < 5 THEN '5万元以下'
  WHEN price < 10 THEN '5-10万元'
  WHEN price < 15 THEN '10-15万元'
  WHEN price < 20 THEN '15-20万元'
  WHEN price < 30 THEN '20-30万元'
  WHEN price < 40 THEN '30-40万元'
  WHEN price < 50 THEN '40-50万元'
  ELSE '50万元以上'
END
```

若用户明确列出一组不覆盖全域的价格段，只统计这些区间；不要用 `ELSE '未定价'` 接住区间外的有效价格。

如果回退到 `vehicle_params`：

```sql
WHERE type_name = '厂商指导价'
```

价格比较时应将 `type_value` 转成数值后再判断区间。

## 禁止规则

- 不要把厂商指导价当成元，数据库单位是万元。
- 不要直接用字符串比较价格大小。
- 不要把 `0 < price < 5` 或其他指定区间外的有效价格归入 `未定价`。
- 不要让 CASE 分类与补零/排序标签表使用不同的价格段集合。
- 不要在车系颗粒度下默认使用所有款型价格段重复计入，除非用户明确要求按款型展开。
- 如果可用表中包含 `vehicle_model_base`，不要回到 `vehicle_params` 用 EAV 自关联计算价格段，应直接使用 `price` / `price_band`。
