---
formatter: semantic-asset
name: 价格段
type: dimension
description: 基于厂商指导价划分价格区间，优先使用 vehicle_params_wide。
aliases:
  - 价格区间
  - 价位
  - 售价区间
  - 指导价区间
tags:
  - 汽车产品配置
  - vehicle_params
  - vehicle_params_wide
  - 价格
version: 0.1.0
resolution_mode: derived
resolution:
  mode: derived
  bindings:
    - asset_ref: dbs_77982e981bac4a6fa8.vehicle_params_wide
      display_name: insight_data · vehicle_params_wide
      fields:
        value: price
  source_fields: [price]
  expression: 按厂商指导价（万元）划分价格区间；优先读取已固化的 price_band。
created: 2026-07-08 00:00:00
updated_at: 2026-07-09 00:00:00
---

# 价格段

## 字段口径

优先使用 `vehicle_params_wide`：

- 指导价：`vehicle_params_wide.price`
- 价格段：`vehicle_params_wide.price_band`

只有当查询的表范围没有 `vehicle_params_wide`，或需要回查原始明细时，才回退到 `vehicle_params`。

价格段基于 `vehicle_params` 表中 `type_name = '厂商指导价'` 的 `type_value` 划分。

数据库中厂商指导价的单位是万元。

## 常用价格段

- 5-10万元
- 10-15万元
- 15-20万元
- 20-30万元
- 30-40万元
- 40-50万元
- 50万以下

如用户指定其他价格段，以用户指定区间为准。

## 计算规则

按款型颗粒度分析时，价格段直接由该 `car_name` 的 `厂商指导价` 决定。

按车系颗粒度分析时，同一车系下可能存在低配款型在较低价格段、高配款型在较高价格段的跨价格段情况。除非用户另有要求，默认以该车系最低配款型的价格段为准，并在答案中说明该默认口径。

## SQL Hint

如果使用 `vehicle_params_wide`：

```sql
WHERE price_band = '<价格段>'
```

或直接比较：

```sql
WHERE price >= 20 AND price < 30
```

如果回退到 `vehicle_params`：

```sql
WHERE type_name = '厂商指导价'
```

价格比较时应将 `type_value` 转成数值后再判断区间。

## 禁止规则

- 不要把厂商指导价当成元，数据库单位是万元。
- 不要直接用字符串比较价格大小。
- 不要在车系颗粒度下默认使用所有款型价格段重复计入，除非用户明确要求按款型展开。
- 如果可用表中包含 `vehicle_params_wide`，不要回到 `vehicle_params` 用 EAV 自关联计算价格段，应直接使用 `price` / `price_band`。
