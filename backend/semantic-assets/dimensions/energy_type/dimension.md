---
formatter: semantic-asset
name: 能源类型
type: dimension
description: 车型能源类型的标准取值口径，优先使用 vehicle_model_base。
aliases:
- 动力类型
- 能源形式
- 新能源
- 燃油
- 纯电
tags:
- 汽车产品配置
- vehicle_params
- vehicle_model_base
- 能源
version: 0.2.0
resolution_mode: source_field
resolution:
  mode: source_field
  bindings:
  - asset_ref: dbs_77982e981bac4a6fa8.vehicle_model_base
    display_name: insight_data · vehicle_model_base
    fields:
      value: energy_type
created: 2026-07-08 00:00:00
updated_at: '2026-07-13 00:00:00'
---

# 能源类型

## 字段口径

优先使用 `vehicle_model_base.energy_type`。

只有当查询的表范围没有 `vehicle_model_base`，或需要回查原始明细时，才回退到 `vehicle_params`。

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

## 传统能源与新能源分类

当用户按“传统能源”和“新能源”查询、筛选、分组或对比时，必须严格使用以下业务映射，不得根据车型名称或通用行业认知自行扩展：

| 能源大类 | `energy_type` 精确取值 |
| --- | --- |
| 传统能源 | `汽油`、`汽油+48V轻混系统`、`油电混合`、`汽油电驱`、`汽油+24V轻混系统` |
| 新能源 | `纯电`、`插电混合`、`增程式纯电动` |

未列入上述映射的能源类型，包括 `柴油`、`柴油+48V轻混系统`、`汽油+天然气`、`天然气`、`氢燃料` 及空值，默认不归入“传统能源”或“新能源”。只有用户另行明确分类口径时才可纳入。

## 常用规则

查询单一能源类型时，必须同时限定：

如果使用 `vehicle_model_base`：

```sql
energy_type = '<精确能源类型>'
```

如果回退到 `vehicle_params`：

```sql
type_name = '能源类型'
AND type_value = '<精确能源类型>'
```

使用 `vehicle_model_base` 查询新能源时，默认使用：

```sql
energy_type IN ('纯电', '插电混合', '增程式纯电动')
```

使用 `vehicle_model_base` 查询传统能源时，默认使用：

```sql
energy_type IN ('汽油', '汽油+48V轻混系统', '油电混合', '汽油电驱', '汽油+24V轻混系统')
```

回退到 `vehicle_params` 查询新能源时，使用：

```sql
type_name = '能源类型'
AND
type_value IN ('纯电', '插电混合', '增程式纯电动')
```

回退到 `vehicle_params` 查询传统能源时，使用：

```sql
type_name = '能源类型'
AND
type_value IN ('汽油', '汽油+48V轻混系统', '油电混合', '汽油电驱', '汽油+24V轻混系统')
```

## 禁止规则

- 不要把 `纯电` 写成 `纯电动`。
- 不要把 `插电混合` 写成 `插电混动` 或 `PHEV`。
- 不要用 `LIKE '%纯电%'` 查询纯电车型，这会误匹配 `增程式纯电动`。
- 不要只写 `type_value = '纯电'`，必须同时写 `type_name = '能源类型'`。
- 如果可用表中包含 `vehicle_model_base`，不要回到 `vehicle_params` 用 EAV 自关联计算能源类型，应直接使用 `energy_type`。
