---
formatter: semantic-asset
name: 电机功率
type: dimension
description: 按新能源款型电动机总功率（kW）进行筛选、分组和固定区间分析；占比分母为电机总功率有效的新能源唯一款型。
aliases:
  - 电机总功率
  - 电动机总功率
  - 电机功率段
  - 电机功率区间
  - motor power
  - motor power band
tags:
  - 汽车产品配置
  - vehicle_params
  - vehicle_model_base
  - 动力性能
  - 电机功率
version: 0.1.2
resolution_mode: derived
resolution:
  mode: derived
  bindings:
    - asset_ref: dbs_77982e981bac4a6fa8.vehicle_model_base
      display_name: insight_data · vehicle_model_base
      fields:
        value: motor_power_kw
        energy_type: energy_type
  source_fields: [motor_power_kw, energy_type]
  expression: 先限定新能源且电动机总功率有效的款型，再按 50 kW 固定区间计算占比；缺失值不进入分母，区间下界包含、上界不包含。
created: 2026-07-14 00:00:00
updated_at: 2026-07-14 00:00:00
---

# 电机功率

## 字段口径

优先使用 `vehicle_model_base.motor_power_kw`。该字段由 `vehicle_params` 中
`type_name = '电动机总功率[kW]'` 的 `type_value` 数值化并物化得到，单位为 kW。

这是数据源直接提供的整车电动机总功率，不是计算字段。不得使用前、后电动机最大功率相加，也不得用发动机最大功率、系统综合功率或峰值功率替代。

只有当查询范围没有 `vehicle_model_base`，或需要回查原始明细时，才回退到 `vehicle_params`：

```sql
type_name = '电动机总功率[kW]'
```

回退时仅接受完整的非负数字字符串，可包含小数点；空值、`-`、`待查` 或其他无法安全转换的值记为未知，不得转换为 `0`。

## 默认能源范围

电机功率段及其占比默认只分析新能源款型。使用 `vehicle_model_base` 时必须先限定：

```sql
energy_type IN ('纯电', '插电混合', '增程式纯电动')
```

新能源的精确取值以 `dimension:energy_type` 为准。不得只写 `energy_type = '新能源'`，因为数据库中保存的是具体能源类型，不是汇总标签。

以下传统能源类型默认不进入电机功率段占比的分子或分母：

```text
汽油
汽油+48V轻混系统
油电混合
汽油电驱
汽油+24V轻混系统
```

即使传统能源款型的 `motor_power_kw` 有值，也不得混入默认新能源电机功率结构。用户明确要求分析其他能源类型时，应作为单独口径展示，不得与默认新能源分母混算。

## 数据颗粒度

电机总功率是款型属性，唯一统计键为：

```text
brand + serial_name + car_name
```

同一车系可能同时存在单电机、双电机或不同功率版本。不得按车系取最新款、任意款、最大值、最小值或平均值代替该车系全部款型。

## 固定分段依据

默认分段沿用产品配置分析模板的电机总功率趋势图口径。所有区间均为左闭右开，最后一档无上界：

| 展示名称 | 精确条件 |
| --- | --- |
| `0-50kW` | `motor_power_kw >= 0 AND motor_power_kw < 50` |
| `50-100kW` | `motor_power_kw >= 50 AND motor_power_kw < 100` |
| `100-150kW` | `motor_power_kw >= 100 AND motor_power_kw < 150` |
| `150-200kW` | `motor_power_kw >= 150 AND motor_power_kw < 200` |
| `200-250kW` | `motor_power_kw >= 200 AND motor_power_kw < 250` |
| `250-300kW` | `motor_power_kw >= 250 AND motor_power_kw < 300` |
| `300-350kW` | `motor_power_kw >= 300 AND motor_power_kw < 350` |
| `350-400kW` | `motor_power_kw >= 350 AND motor_power_kw < 400` |
| `400-450kW` | `motor_power_kw >= 400 AND motor_power_kw < 450` |
| `450-500kW` | `motor_power_kw >= 450 AND motor_power_kw < 500` |
| `500kW以上` | `motor_power_kw >= 500` |
| `未知` | `motor_power_kw IS NULL OR motor_power_kw < 0` |

因此，恰好为 `150` kW 时归入 `150-200kW`，恰好为 `500` kW 时归入 `500kW以上`。

如果用户明确指定其他区间，以用户区间为准，并在结果中说明分段口径发生变化。

## SQL Hint

```sql
CASE
  WHEN motor_power_kw IS NULL THEN '未知'
  WHEN motor_power_kw < 0 THEN '未知'
  WHEN motor_power_kw < 50 THEN '0-50kW'
  WHEN motor_power_kw < 100 THEN '50-100kW'
  WHEN motor_power_kw < 150 THEN '100-150kW'
  WHEN motor_power_kw < 200 THEN '150-200kW'
  WHEN motor_power_kw < 250 THEN '200-250kW'
  WHEN motor_power_kw < 300 THEN '250-300kW'
  WHEN motor_power_kw < 350 THEN '300-350kW'
  WHEN motor_power_kw < 400 THEN '350-400kW'
  WHEN motor_power_kw < 450 THEN '400-450kW'
  WHEN motor_power_kw < 500 THEN '450-500kW'
  ELSE '500kW以上'
END AS motor_power_band
```

## 占比与覆盖率口径

“电机功率段款型占比”默认使用完整款型键去重：

```text
某功率段新能源唯一款型数 ÷ 当前统计范围内电机总功率有效的新能源唯一款型数
```

分子和分母必须应用完全相同的年份、品牌、车系、车型级别、价格带及销售状态等筛选条件；唯一差异只能是分子增加电机功率段条件。

电机总功率为 `NULL`、负数或其他非法值的新能源款型不进入功率段占比的分子和分母。所有已知功率段应合计为 100%（允许四舍五入误差），传统能源款型同样不进入该合计。

缺失记录可以单独统计为“未知”数量，但“未知”只用于数据质量说明，不是功率段占比的一部分。

结果必须同时披露电机总功率覆盖率：

```text
电机总功率有效的新能源唯一款型数 ÷ 当前统计范围内新能源全部唯一款型数
```

功率段占比的分母必须是电机总功率有效的新能源唯一款型；同时必须披露覆盖率，避免有效样本占比被误解为新能源全部款型占比。不得把新能源款型结构占比描述为全市场销量占比。

若用户要求销量或上险量加权占比，必须接入相应事实表并在完整款型颗粒度关联；没有销量或上险量数据时，不得用款型数量占比冒充市场销量占比。

## 禁止规则

- 不要将前电动机最大功率与后电动机最大功率相加生成总功率。
- 不要用 `最大功率[kW]`、`最大净功率[kW]` 或 `系统综合功率[kW]` 替代电动机总功率。
- 不要直接用字符串比较功率大小。
- 不要按车系取任意一个款型的功率代替全车系。
- 不要把 `NULL`、空字符串、`-` 或 `待查` 转成 `0`。
- 不要把传统能源款型混入默认新能源电机功率段占比的分子或分母。
- 不要把电机总功率缺失或非法的新能源款型计入功率段占比的分母。
- 不要省略覆盖率；功率段占比只代表电机总功率有效的新能源样本。
- 如果可用表包含 `vehicle_model_base`，不要回到 `vehicle_params` 用 EAV 自关联计算电机功率，应直接使用 `motor_power_kw`。
