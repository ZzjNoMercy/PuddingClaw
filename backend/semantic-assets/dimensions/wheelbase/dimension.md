---
formatter: semantic-asset
name: 轴距
type: dimension
description: 按款型轴距（毫米）进行筛选、分组和区间分析，优先使用 vehicle_model_base.wheelbase_mm。
aliases:
  - 轴距区间
  - 轴距段
  - 轴距分布
  - wheelbase
  - wheelbase band
tags:
  - 汽车产品配置
  - vehicle_params
  - vehicle_model_base
  - 车身尺寸
  - 轴距
version: 0.1.0
resolution_mode: derived
resolution:
  mode: derived
  bindings:
    - asset_ref: dbs_77982e981bac4a6fa8.vehicle_model_base
      display_name: insight_data · vehicle_model_base
      fields:
        value: wheelbase_mm
  source_fields: [wheelbase_mm]
  expression: 按款型轴距（毫米）划分固定的 50 mm 区间；区间下界包含、上界不包含。
created: 2026-07-14 00:00:00
updated_at: 2026-07-14 00:00:00
---

# 轴距

## 字段口径

优先使用 `vehicle_model_base.wheelbase_mm`。该字段由 `vehicle_params` 中
`type_name = '轴距[mm]'` 的 `type_value` 清洗并物化得到，单位为毫米。

只有当查询的表范围没有 `vehicle_model_base`，或需要回查原始明细时，才回退到 `vehicle_params`：

```sql
type_name = '轴距[mm]'
```

回退时必须先移除非数字字符，再把空字符串转成 `NULL`，最后转换为整数。`待查` 等无法转换的值必须记为未知轴距，不得转换为 `0`。

## 数据颗粒度

轴距是款型属性，唯一统计键为：

```text
brand + serial_name + car_name
```

同一车系可能包含多个轴距。不得只按 `brand + serial_name` 选择最新款、任意款、最小值、最大值或平均值作为整个车系的轴距。

只有确认某车系全部纳入统计的款型轴距完全一致时，才可以把轴距安全地折叠到车系颗粒度；否则必须保留款型颗粒度，或报告无法精确映射。

## 固定分段依据

默认分段沿用乘用车市场轴距占比趋势图口径。所有区间均为左闭右开，只有最后一档无上界：

| 展示名称 | 精确条件 |
| --- | --- |
| `2600以下` | `wheelbase_mm < 2600` |
| `2600-2650` | `wheelbase_mm >= 2600 AND wheelbase_mm < 2650` |
| `2650-2700` | `wheelbase_mm >= 2650 AND wheelbase_mm < 2700` |
| `2700-2750` | `wheelbase_mm >= 2700 AND wheelbase_mm < 2750` |
| `2750-2800` | `wheelbase_mm >= 2750 AND wheelbase_mm < 2800` |
| `2800-2850` | `wheelbase_mm >= 2800 AND wheelbase_mm < 2850` |
| `2850-2900` | `wheelbase_mm >= 2850 AND wheelbase_mm < 2900` |
| `2900-2950` | `wheelbase_mm >= 2900 AND wheelbase_mm < 2950` |
| `2950-3000` | `wheelbase_mm >= 2950 AND wheelbase_mm < 3000` |
| `3000以上` | `wheelbase_mm >= 3000` |
| `未知` | `wheelbase_mm IS NULL` |

因此，轴距恰好为 `2600` mm 时归入 `2600-2650`，恰好为 `3000` mm 时归入 `3000以上`。

如果用户明确指定其他区间，以用户指定区间为准，并在结果中说明分段口径发生变化。

## SQL Hint

```sql
CASE
  WHEN wheelbase_mm IS NULL THEN '未知'
  WHEN wheelbase_mm < 2600 THEN '2600以下'
  WHEN wheelbase_mm < 2650 THEN '2600-2650'
  WHEN wheelbase_mm < 2700 THEN '2650-2700'
  WHEN wheelbase_mm < 2750 THEN '2700-2750'
  WHEN wheelbase_mm < 2800 THEN '2750-2800'
  WHEN wheelbase_mm < 2850 THEN '2800-2850'
  WHEN wheelbase_mm < 2900 THEN '2850-2900'
  WHEN wheelbase_mm < 2950 THEN '2900-2950'
  WHEN wheelbase_mm < 3000 THEN '2950-3000'
  ELSE '3000以上'
END AS wheelbase_band
```

## 占比口径

“市场轴距占比”默认是销量或上险量占比，不是款型数量占比：

```text
某年某轴距段销量（或上险量） ÷ 同年统计范围内全部销量（或上险量）
```

销售或上险来源必须在具体款型层面与产品配置库关联。优先使用完整的
`brand + serial_name + car_name` 映射；只有车系内轴距唯一时才允许使用车系级映射。

未知轴距记录不得从总分母中静默删除。未知记录不为零时，应增加“未知”分段，使堆叠占比合计为 100%，并同时报告轴距覆盖率。

如果用户明确要求“款型结构占比”，才使用款型数作为分子和分母，并明确标注为款型数占比。

## 禁止规则

- 不要把轴距单位当成厘米或米；数据库单位是毫米。
- 不要直接用字符串比较轴距大小。
- 不要按车系取最新款轴距代替同车系全部款型。
- 不要把 `NULL`、空字符串或 `待查` 转成 `0`。
- 不要为了让已知分段合计为 100% 而从分母删除未知轴距记录。
- 如果可用表中包含 `vehicle_model_base`，不要回到 `vehicle_params` 用 EAV 自关联计算轴距，应直接使用 `wheelbase_mm`。
