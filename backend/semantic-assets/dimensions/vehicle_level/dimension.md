---
formatter: semantic-asset
name: 车型级别
type: dimension
description: 车型级别的标准取值口径，优先使用 vehicle_params_wide。
aliases:
  - 级别
  - 车型等级
  - 车型分类
  - 车型级别查询
tags:
  - 汽车产品配置
  - vehicle_params
  - vehicle_params_wide
  - 级别
version: 0.1.0
created: 2026-07-08 00:00:00
updated_at: 2026-07-09 00:00:00
---

# 车型级别

## 字段口径

优先使用 `vehicle_params_wide.vehicle_level`。

只有当查询的表范围没有 `vehicle_params_wide`，或需要回查原始明细时，才回退到 `vehicle_params`。

车型级别取 `vehicle_params` 表中 `type_name = '级别'` 的 `type_value`。

## 精确枚举值

数据库中的车型级别取值必须使用以下精确值：

| 车型级别 | 说明 |
| --- | --- |
| `紧凑型车` | 紧凑型轿车 |
| `紧凑型SUV` | 紧凑型运动型多用途车 |
| `中型SUV` | 中型运动型多用途车 |
| `中型车` | 中型轿车 |
| `中大型SUV` | 中大型运动型多用途车 |
| `中大型车` | 中大型轿车 |
| `小型车` | 小型轿车 |
| `小型SUV` | 小型运动型多用途车 |
| `中大型MPV` | 中大型多用途汽车 |
| `紧凑型MPV` | 紧凑型多用途汽车 |
| `中型MPV` | 中型多用途汽车 |
| `大型车` | 大型轿车 |
| `微型车` | 微型轿车 |
| `大型MPV` | 大型多用途汽车 |
| `大型SUV` | 大型运动型多用途车 |
| `皮卡` | 皮卡车 |

## 常用规则

查询某个车型级别时，必须同时限定：

如果使用 `vehicle_params_wide`：

```sql
vehicle_level = '<精确车型级别>'
```

如果回退到 `vehicle_params`：

```sql
type_name = '级别'
AND type_value = '<精确车型级别>'
```

查询 SUV 大类时，不要模糊 `LIKE '%SUV%'`，应根据语义选择精确枚举值集合，例如：

```sql
type_value IN ('小型SUV', '紧凑型SUV', '中型SUV', '中大型SUV', '大型SUV')
```

## 禁止规则

- 不要把 `紧凑型SUV` 写成 `紧凑SUV`。
- 不要把数据库不存在的英文或简称值写入 SQL。
- 不要只写 `type_value = '皮卡'`，必须同时写 `type_name = '级别'`。
- 不要用 `car_name LIKE '%皮卡%'` 代替车型级别筛选。
- 如果可用表中包含 `vehicle_params_wide`，不要回到 `vehicle_params` 用 EAV 自关联计算车型级别，应直接使用 `vehicle_level`。
