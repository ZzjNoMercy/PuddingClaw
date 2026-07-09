---
formatter: semantic-asset
name: 品牌
type: dimension
description: 品牌维度的标准取值口径，优先使用 vehicle_params_wide。
aliases:
  - 厂牌
  - 汽车品牌
  - 子品牌
  - 下属品牌
  - brand
tags:
  - 汽车产品配置
  - vehicle_params
  - vehicle_params_wide
  - 品牌
version: 0.1.0
created: 2026-07-09 00:00:00
updated_at: 2026-07-09 00:00:00
---

# 品牌

## 字段口径

优先使用 `vehicle_params_wide.brand`。

只有当查询的表范围没有 `vehicle_params_wide`，或需要回查原始明细时，才使用 `vehicle_params.brand`。

如果某些导入数据没有独立 `brand` 字段，应先补充数据资产或实体字典，不要从 `car_name`、`serial_name` 或款型名称中猜测品牌。

## 常用规则

查询单一品牌时：

```sql
brand = '<品牌名称>'
```

查询多个品牌或集团下属品牌时，必须显式使用品牌集合：

```sql
brand IN ('比亚迪', '腾势', '方程豹', '仰望')
```

如果用户说“比亚迪及下属品牌”，默认至少包括：

- `比亚迪`
- `腾势`
- `方程豹`
- `仰望`

如用户补充其他品牌，以用户指定集合为准。

## 与配置率组合

品牌作为筛选或分组维度时，必须在分子和分母使用同一品牌范围。

对于 `vehicle_params` 配置率查询，推荐先按 `car_name` 聚合配置 flags，再保留或聚合品牌维度。不要因为一个款型有多行配置而重复计数。

如果可用表中包含 `vehicle_params_wide`，配置率的品牌筛选和分组优先在 `vehicle_params_wide` 完成。

## 禁止规则

- 不要从 `car_name` 推断品牌。
- 不要只召回或统计主品牌后忽略用户明确提到的下属品牌。
- 不要用预览行中的品牌分布推断全量结果品牌分布。
