---
formatter: semantic-asset
name: 配置率
type: measure
description: 统计某配置在目标颗粒度集合中的搭载比例，默认排除车型级别为皮卡的车型。
aliases:
  - 搭载率
  - 渗透率
  - 配备率
tags:
  - 汽车产品配置
  - vehicle_params
  - vehicle_model_base
version: 0.1.0
created: 2026-07-08 00:00:00
updated_at: 2026-07-09 00:00:00
---

# 配置率

## 业务口径

配置率 = 搭载目标配置的统计对象数量 / 目标范围内统计对象总数量。

统计对象由本轮命中的颗粒度资产决定。

如果用户没有显式指定颗粒度，默认使用款型颗粒度，即按 `brand + serial_name + car_name` 去重。

除非用户明确要求“包含皮卡”或“只看皮卡”，配置率默认排除车型级别为 `皮卡` 的统计对象。

排除皮卡时必须使用车型级别维度口径。

如果可用表中包含 `vehicle_model_base`，分母和常用维度筛选必须优先使用 `vehicle_model_base`，例如上市时间、能源类型、车型级别、品牌、车系、价格、价格段、销售状态。

只有目标配置是否搭载这种配置明细判断，才回到 `vehicle_params` 读取 `type_name` / `type_value`。

对于没有 `vehicle_model_base` 的回退场景，才允许在 `vehicle_params` 这种 EAV 明细表中用同一个 `car_flags` CTE 聚合出 `is_pickup`，再在分子和分母里统一排除：

```sql
WITH car_flags AS (
  SELECT
    brand,
    serial_name,
    car_name,
    BOOL_OR(type_name = '级别' AND type_value = '皮卡') AS is_pickup
  FROM vehicle_params
  WHERE car_name IS NOT NULL
    AND brand IS NOT NULL
    AND serial_name IS NOT NULL
    AND type_name IN ('级别')
  GROUP BY brand, serial_name, car_name
)
SELECT COUNT(*) FILTER (WHERE NOT is_pickup)
FROM car_flags
```

## 分母

满足当前筛选条件的统计对象数量，默认排除车型级别为 `皮卡` 的统计对象。

如果问题涉及上市时间、能源类型、车型级别、品牌、车系、价格、价格段、销售状态等常用维度，分母必须优先来自 `vehicle_model_base`：

```sql
WITH denominator AS (
  SELECT brand, serial_name, car_name
  FROM vehicle_model_base
  WHERE launch_year = 2026
    AND energy_type = '纯电'
    AND vehicle_level IS DISTINCT FROM '皮卡'
)
SELECT COUNT(*) FROM denominator;
```

如果问题要求按上市年份、品牌、车系、价格段等维度分析，分母必须在同一维度、筛选范围和颗粒度内计算。

如果用户显式要求包含皮卡，则可以取消默认排除，但必须在答案中说明“本次包含皮卡”。

## 分子

分母范围内，存在目标配置有效值的统计对象数量。

分子必须继承分母的全部筛选条件，包括默认排除皮卡规则。不得出现分母排除皮卡、分子未排除皮卡，或分子排除皮卡、分母未排除皮卡的口径不一致。

当分母使用 `vehicle_model_base` 时，分子应从分母 CTE 出发 join `vehicle_params`，不要重新在 `vehicle_params` 上计算上市时间、能源类型、级别、品牌、价格段等维度：

```sql
WITH denominator AS (
  SELECT brand, serial_name, car_name
  FROM vehicle_model_base
  WHERE launch_year = 2026
    AND energy_type = '纯电'
    AND vehicle_level IS DISTINCT FROM '皮卡'
),
numerator AS (
  SELECT DISTINCT d.brand, d.serial_name, d.car_name
  FROM denominator d
  JOIN vehicle_params vp
    ON vp.brand = d.brand
   AND vp.serial_name = d.serial_name
   AND vp.car_name = d.car_name
  WHERE vp.type_name = '<配置字段>'
    AND vp.type_value IS NOT NULL
    AND vp.type_value NOT IN ('', '-', '无', '未配备', '不配备')
)
SELECT
  COUNT(*) AS denominator,
  (SELECT COUNT(*) FROM numerator) AS numerator,
  ROUND((SELECT COUNT(*) FROM numerator) * 100.0 / NULLIF(COUNT(*), 0), 2) AS config_rate_pct
FROM denominator;
```

有效配置判断：

- `type_name` 命中目标配置名称或用户指定配置名称。
- `type_value` 非空。
- `type_value` 不等于 `'-'`。
- `type_value` 不等于 `'无'`、`'未配备'`、`'不配备'`。

## 示例

用户问“激光雷达配置率”时，目标配置可以使用：

- `type_name = '激光雷达数量'` 且 `type_value` 有有效值。
- 或 `type_name = '激光雷达型号'` 且 `type_value` 有有效值。

如果同时存在多个激光雷达相关配置项，必须按当前颗粒度去重，避免同一统计对象因多个配置项被重复计数。

## SQL 生成要求

在可用表包含 `vehicle_model_base` 时，配置率 SQL 必须采用“款型基础表分母 + 配置明细分子”的结构：

1. `denominator` CTE 从 `vehicle_model_base` 取目标统计对象。
2. `numerator` CTE 从 `denominator` join `vehicle_params` 判断目标配置。
3. 最终从 `denominator` 计算分母，从 `numerator` 计算分子。

示例：用户问“2026 年上市的纯电新车中，空气悬架的配备率是多少，排除皮卡”：

```sql
WITH denominator AS (
  SELECT brand, serial_name, car_name
  FROM vehicle_model_base
  WHERE launch_year = 2026
    AND energy_type = '纯电'
    AND vehicle_level IS DISTINCT FROM '皮卡'
),
numerator AS (
  SELECT DISTINCT d.brand, d.serial_name, d.car_name
  FROM denominator d
  JOIN vehicle_params vp
    ON vp.brand = d.brand
   AND vp.serial_name = d.serial_name
   AND vp.car_name = d.car_name
  WHERE vp.type_name = '可调悬架种类'
    AND vp.type_value LIKE '%空气悬架%'
    AND vp.type_value IS NOT NULL
    AND vp.type_value NOT IN ('', '-', '无', '未配备', '不配备')
)
SELECT
  COUNT(*) AS total_count,
  (SELECT COUNT(*) FROM numerator) AS equipped_count,
  ROUND((SELECT COUNT(*) FROM numerator) * 100.0 / NULLIF(COUNT(*), 0), 2) AS config_rate_pct
FROM denominator;
```

回退场景：如果没有 `vehicle_model_base`，在 `vehicle_params` 上同时筛选上市时间、能源类型、级别、品牌、价格段、配置项等多个条件时，不要使用多层 `EXISTS` / `NOT EXISTS` 自关联，也不要用 `COUNT(DISTINCT ...)` 在多层子查询上直接统计。

推荐一次扫描相关 `type_name`，按 `brand + serial_name + car_name` 聚合成 flags，再用 `COUNT(*) FILTER (...)` 计算分母、分子和配置率：

```sql
WITH car_flags AS (
  SELECT
    brand,
    serial_name,
    car_name,
    BOOL_OR(type_name = '上市时间' AND type_value >= '2026-01-01' AND type_value < '2027-01-01') AS is_target_launch_time,
    BOOL_OR(type_name = '能源类型' AND type_value = '纯电') AS is_target_energy,
    BOOL_OR(type_name = '级别' AND type_value = '皮卡') AS is_pickup,
    BOOL_OR(type_name = '<配置字段>' AND type_value IS NOT NULL AND type_value NOT IN ('', '-', '无')) AS has_target_config
  FROM vehicle_params
  WHERE car_name IS NOT NULL
    AND brand IS NOT NULL
    AND serial_name IS NOT NULL
    AND type_name IN ('上市时间', '能源类型', '级别', '<配置字段>')
  GROUP BY brand, serial_name, car_name
)
SELECT
  COUNT(*) FILTER (WHERE is_target_launch_time AND is_target_energy AND NOT is_pickup) AS denominator,
  COUNT(*) FILTER (WHERE is_target_launch_time AND is_target_energy AND NOT is_pickup AND has_target_config) AS numerator,
  ROUND(
    COUNT(*) FILTER (WHERE is_target_launch_time AND is_target_energy AND NOT is_pickup AND has_target_config) * 100.0
    / NULLIF(COUNT(*) FILTER (WHERE is_target_launch_time AND is_target_energy AND NOT is_pickup), 0),
    2
  ) AS config_rate_pct
FROM car_flags;
```

## References

分析特定配置时，必须先检查本目录 `references/` 下是否存在匹配的专用口径。

如果存在匹配 reference：

- 以 reference 中的配置识别规则为准。
- 本文档只负责配置率通用分子、分母、去重、颗粒度和百分比计算。
- 不得仅根据用户输入的配置名称直接匹配 `type_name`。
- 不要在 reference 中重复维护上市时间、价格段等已有维度口径；这些口径应来自对应维度资产。

## 禁止规则

- 不要用行数直接除以行数计算配置率。
- 不要把同一统计对象的多个配置项重复计入分子。
- 不要只按 `car_name` 去重或分组，默认款型颗粒度必须使用 `brand + serial_name + car_name`。
- 不要在聚合结果上使用 `LIMIT` 近似回答。
- 除非用户明确要求包含皮卡，不要把车型级别为 `皮卡` 的统计对象计入配置率分母或分子。
- 不要用 `car_name LIKE '%皮卡%'` 判断皮卡，必须使用车型级别维度：`type_name = '级别' AND type_value = '皮卡'`。
- 如果可用表中包含 `vehicle_model_base`，不要用 `vehicle_params` 的 EAV flags 计算上市时间、能源类型、级别、品牌、价格段等分母维度。
