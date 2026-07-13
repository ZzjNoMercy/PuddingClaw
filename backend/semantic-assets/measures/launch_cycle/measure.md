---
formatter: semantic-asset
name: 上市周期
type: measure
description: 计算同一品牌、同一车系相邻两次上市事件之间的自然日间隔。
aliases:
  - 上市间隔
  - 上新间隔
  - 上新周期
  - 上市间隔天数
tags:
  - 汽车产品
  - 车系上新
  - vehicle_params
version: 0.6.0
created: 2026-07-13 00:00:00
updated_at: 2026-07-13 00:00:00
---

# 上市周期

## 业务口径

上市周期是同一品牌、同一车系相邻两次上市事件之间相差的自然日数。

- 本次上市时间：当前上市事件的上市时间。
- 上次上市时间：同一品牌、同一车系内，早于本次上市时间的最近一次有效上市时间。
- 上市周期：本次上市时间减去上次上市时间，结果单位为天。
- 同一品牌、同一车系的第一次上市没有上次上市时间，上市周期为空。
- 上市周期的年份归属取本次上市时间所在年份，即日期相减表达式中较晚的上市日期所在年份。

例如，`2026-03-21 - 2025-10-10` 形成的上市周期整体归属于 `2026` 年，不归属于 2025 年，也不按自然年拆分。

## 核心强制规则：先计算 LAG，后按年份筛选

这是上市周期最重要的计算顺序，生成 SQL 时必须遵守：

1. 从全部有效历史上市事件构建完整序列。
2. 在完整序列中计算 `LAG(launch_date)`，得到上次上市时间。
3. 计算本次上市时间与上次上市时间的周期天数。
4. 最后才按本次上市时间对应的周期归属年份筛选或汇总。

严禁在 `LAG(launch_date)` 之前使用 `launch_year BETWEEN ...`、`EXTRACT(YEAR FROM launch_date) ...` 或等价条件裁剪上市事件。

错误结构：

```sql
WITH filtered_events AS (
  SELECT ...
  FROM launch_events
  WHERE EXTRACT(YEAR FROM launch_date) BETWEEN 2020 AND 2026
),
launch_sequence AS (
  SELECT
    ...,
    LAG(launch_date) OVER (...) AS previous_launch_date
  FROM filtered_events
)
```

正确结构：

```sql
WITH launch_sequence AS (
  SELECT
    ...,
    LAG(launch_date) OVER (...) AS previous_launch_date
  FROM all_launch_events
),
cycle_calc AS (
  SELECT
    ...,
    EXTRACT(YEAR FROM launch_date) AS launch_cycle_year,
    launch_date - previous_launch_date AS launch_cycle_days
  FROM launch_sequence
)
SELECT ...
FROM cycle_calc
WHERE launch_cycle_year BETWEEN 2020 AND 2026;
```

例如某车系在 `2019-10-10` 和 `2020-03-21` 上市。如果先筛选 2020 年，2019 年事件会被删除，2020 年事件将被错误识别为首次上市。必须先在完整序列中算出 `2020-03-21 - 2019-10-10`，再将该周期归入 2020 年。

## 统计颗粒度

默认按 `brand + serial_name + 上市时间` 识别一次车系上市事件。

一个款型通常只有一个上市时间。上市周期不是在同一个款型内寻找多个上市时间，而是汇集同一品牌、同一车系下全部款型的上市时间，再计算相邻上市事件之间的间隔。

同一品牌、同一车系、同一上市时间下存在多个款型时，只保留一个车系上市事件，不得因同一天上市的款型数量重复计算上市周期。

品牌是车系身份的一部分。不同品牌下名称相同的车系不得合并计算。

当用户要求按传统能源、新能源或具体能源类型对比上市周期时，必须按 `dimension:energy_type` 的业务映射确定当前款型的能源组；事件颗粒度改为 `brand + serial_name + energy_group + 上市时间`，并在 `brand + serial_name + energy_group` 内分别计算相邻上市事件。不得用传统能源事件作为新能源事件的上次上市时间，反之亦然。

同一车系同一天在同一能源组内上市多个款型时只保留一个事件；同一天同时存在传统能源和新能源款型时，可在两个能源组中各形成一个事件。

## 字段口径

数据必须从 `vehicle_params` 的上市时间明细中提取，不使用 `vehicle_params_wide.launch_date` 计算本度量值：

- 品牌：`brand`。
- 车系：`serial_name`。
- 款型：`car_name`，仅用于识别和排查重复明细，不作为默认上市事件颗粒度。
- 上市时间：`type_name = '上市时间'` 对应的 `type_value`。

`type_value` 必须先转换为有效日期，再参与排序和日期差计算。无法转换为日期的值应排除，并在结果中说明数据质量问题。

`vehicle_params_wide` 是款型级物化快照，适合按上市时间筛选款型，但本度量值需要从 `vehicle_params` 汇集同一车系下全部上市时间并重建车系上市事件序列。

## 计算规则

1. 从 `vehicle_params` 中筛选所有 `type_name = '上市时间'` 的款型记录。
2. 排除品牌、车系或上市时间为空的记录。
3. 将每个款型的上市时间转换为日期类型。
4. 汇集同一 `brand + serial_name` 下全部款型的上市时间。
5. 按 `brand + serial_name + 上市时间` 去重；多个款型在同一天上市时只形成一个车系上市事件。
6. 在每个 `brand + serial_name` 分组内，按上市时间从早到晚排序。
7. 取当前上市事件之前最近的一次上市时间作为上次上市时间。
8. 用本次上市时间减去上次上市时间，得到上市周期，单位为自然日。
9. 以本次上市时间的年份作为上市周期归属年份：`launch_cycle_year = YEAR(launch_date)`。

按年份筛选或汇总上市周期时，必须先基于完整上市事件序列计算上次上市时间和上市周期，再按本次上市时间对应的 `launch_cycle_year` 筛选。不得先筛选年份再计算相邻间隔，否则会丢失跨年周期的上次上市事件。

## SQL Hint

以下 SQL 仅用于表达计算结构。生成查询时应根据实际数据库方言选择日期转换和日期差函数：

```sql
WITH launch_events AS (
  SELECT DISTINCT
    brand,
    serial_name,
    CAST(type_value AS DATE) AS launch_date
  FROM vehicle_params
  WHERE type_name = '上市时间'
    AND brand IS NOT NULL
    AND serial_name IS NOT NULL
    AND type_value IS NOT NULL
    AND type_value <> ''
),
launch_sequence AS (
  SELECT
    brand,
    serial_name,
    launch_date,
    LAG(launch_date) OVER (
      PARTITION BY brand, serial_name
      ORDER BY launch_date
    ) AS previous_launch_date
  FROM launch_events
)
SELECT
  brand,
  serial_name,
  launch_date,
  EXTRACT(YEAR FROM launch_date) AS launch_cycle_year,
  previous_launch_date,
  launch_date - previous_launch_date AS launch_cycle_days
FROM launch_sequence
WHERE previous_launch_date IS NOT NULL;
```

## 聚合规则

用户要求按品牌、车系、时间范围等维度汇总时，应先逐个上市事件计算上市周期，再对周期结果进行聚合。

平均上市周期根据分析场景使用以下两种明确口径，不得混用。

### 按车系统计

计算单个车系完整观察区间的平均上市周期时，如果该车系有 `N` 次去重后的上市事件，则形成 `N - 1` 个有效周期：

```text
车系平均上市周期
= 该车系有效周期天数之和 / (该车系上市次数 - 1)
```

上市次数小于 2 的车系没有平均上市周期，结果为空，不得除以 0。

当汇总多个车系的有效周期时，必须对每个车系分别扣除首次上市，分母为：

```text
车系有效周期总数 = SUM(每个车系的 MAX(上市次数 - 1, 0))
```

不得使用“所有车系的上市总次数 - 1”作为跨车系汇总分母。

### 按年份统计

计算某一年的平均上市周期时，分母使用该年份全部去重后的上市次数，包括各车系在数据中的首次上市：

```text
年度平均上市周期
= 归属于该年份的周期天数之和 / 该年份上市次数
```

首次上市没有上次上市时间，明细周期仍为空；但在年度均值口径中计入该年份上市次数分母，其周期天数对分子贡献按 0 处理。不得把首次上市的明细周期改写成 0。

例如某车系在 2025 年上市一次、2026 年上市一次，则跨年周期归属于 2026 年。2025 年和 2026 年的上市事件都分别计入各自年度的上市次数分母；2025 年首次上市对周期天数分子的贡献为 0。

按传统能源、新能源等能源大类计算年度趋势时，在每个 `year + energy_group` 内分别使用：

```text
年度分能源平均上市周期
= 该年份该能源组的周期天数之和 / 该年份该能源组的上市次数
```

- “车系平均上市周期”：有效周期天数之和除以该车系上市次数减 1。
- “年度平均上市周期”：归属于该年的周期天数之和除以该年全部上市次数。
- “最长上市周期”：取有效上市周期最大值。
- “最短上市周期”：取有效上市周期最小值。
- “某年上市周期”：以本次上市时间所在年份归属；跨年间隔整体计入本次上市年份。
- “按能源大类的年度平均上市周期”：在各能源大类自己的车系事件序列内计算周期，按本次上市年份归属后，以该年该能源组全部上市次数为分母。
- 用户未指定聚合方式且问题要求明细时，返回每次上市事件对应的上市周期，不自动求和。

上市周期是时间间隔，不得直接求和作为车系的常规指标。

## 禁止规则

- 不要执行或生成 DAX。
- 不要使用 `vehicle_params_wide.launch_date` 计算上市周期；必须从 `vehicle_params` 获取同一车系下全部 `type_name = '上市时间'` 的明细。
- 不要假设 `vehicle_params` 中存在“上次上市时间”字段，必须根据同一车系的上市事件顺序计算。
- 不要跨品牌或跨车系寻找上次上市时间。
- 不要直接按款型明细计算，否则同一上市事件可能被重复统计。
- 不要将同一品牌、同一车系、同一上市时间下的多个款型识别为多次上市。
- 上市时间为空、格式无效或无法转换为日期时，不得参与计算。
- 第一次上市的上市周期必须为空，不得填充为 `0`。
- 如果本次上市时间早于上次上市时间，应视为排序或数据质量异常，不得输出负数周期。
- 不得将上市周期直接求和作为默认汇总结果。
- 不要按上次上市时间的年份归属上市周期，也不要把一个跨年周期拆分到多个年份。
- 不要在构建完整上市事件序列之前按年份过滤上市时间。
- 任何出现在 `LAG(launch_date)` 上游 CTE 中的年份范围过滤都属于口径错误，即使当前数据结果碰巧没有变化也不得接受。
- 按能源大类分析时，不要跨能源大类寻找上次上市事件。
- 汇总多个车系或能源组时，不要用上市总次数统一减 1；必须对每个独立事件序列分别减 1。
- 按年份汇总时，不要用当年上市次数减 1，也不要只用有效周期数作分母；必须使用当年全部去重上市次数作分母。
- 首次上市的周期明细必须保持为空；仅在年度平均周期的分子求和中按 0 贡献处理。
