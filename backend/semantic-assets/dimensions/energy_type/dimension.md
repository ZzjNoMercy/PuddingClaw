---
formatter: semantic-asset
name: 能源类型
type: dimension
description: 车型能源类型的标准取值口径，优先使用 vehicle_params_wide。
aliases:
  - 动力类型
  - 能源形式
  - 新能源
  - 燃油
  - 纯电
tags:
  - 汽车产品配置
  - vehicle_params
  - vehicle_params_wide
  - 能源
version: 0.1.0
created: 2026-07-08 00:00:00
updated_at: 2026-07-09 00:00:00
---

# 能源类型

## 字段口径

优先使用 `vehicle_params_wide.energy_type`。

只有当查询的表范围没有 `vehicle_params_wide`，或需要回查原始明细时，才回退到 `vehicle_params`。

能源类型取 `vehicle_params` 表中 `type_name = '能源类型'` 的 `type_value`。

## 精确枚举值

数据库中的能源类型取值必须使用以下精确值：

| 能源类型 | 说明 |
| --- | --- |
| `纯电` | 纯电动汽车 |
| `插电混合` | 插电式混合动力 |
| `增程式纯电动` | 增程式电动车 |
| `油电混合` | 油电混合动力，非插电 |
| `汽油` | 传统燃油车 |
| `汽油电驱` | 汽油电驱 |
| `汽油+48V轻混系统` | 汽油 + 48V 轻混 |
| `汽油+24V轻混系统` | 汽油 + 24V 轻混 |
| `汽油+天然气` | 双燃料 |
| `柴油` | 传统柴油车 |
| `柴油+48V轻混系统` | 柴油 + 48V 轻混 |
| `氢燃料` | 氢燃料电池车 |
| `天然气` | 天然气车 |

## 常用规则

查询单一能源类型时，必须同时限定：

如果使用 `vehicle_params_wide`：

```sql
energy_type = '<精确能源类型>'
```

如果回退到 `vehicle_params`：

```sql
type_name = '能源类型'
AND type_value = '<精确能源类型>'
```

查询新能源时，默认使用：

```sql
type_value IN ('纯电', '插电混合', '增程式纯电动')
```

## 禁止规则

- 不要把 `纯电` 写成 `纯电动`。
- 不要把 `插电混合` 写成 `插电混动` 或 `PHEV`。
- 不要用 `LIKE '%纯电%'` 查询纯电车型，这会误匹配 `增程式纯电动`。
- 不要只写 `type_value = '纯电'`，必须同时写 `type_name = '能源类型'`。
- 如果可用表中包含 `vehicle_params_wide`，不要回到 `vehicle_params` 用 EAV 自关联计算能源类型，应直接使用 `energy_type`。
