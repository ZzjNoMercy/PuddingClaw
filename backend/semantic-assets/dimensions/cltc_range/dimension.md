---
formatter: semantic-asset
id: dimension:cltc_range
name: CLTC纯电续航
type: dimension
version: "0.1.0"
description: CLTC纯电续航的双物理字段映射与合并策略。
aliases: ["CLTC续航", "CLTC纯电续航里程", "纯电续航", "CLTC range"]
tags: ["汽车产品配置", "vehicle_params", "电池/补能"]
resolution:
  mode: source_field
  bindings:
    - asset_ref: dbs_77982e981bac4a6fa8.vehicle_params
      fields:
        type_name: "CLTC纯电续航[km]"
        value: type_value
eav_equivalence:
  - concept: cltc_pure_electric_range_km
    type_names: ["CLTC纯电续航[km]", "CLTC纯电续航里程[km]"]
    match: any
    value_resolution: coalesce_by_priority
created: "2026-07-31 00:00:00"
updated_at: "2026-07-31 00:00:00"
---

# CLTC 纯电续航

## 字段口径

当前数据源同时存在两个等价的历史物理字段：

- `CLTC纯电续航[km]`
- `CLTC纯电续航里程[km]`

查询 CLTC 纯电续航时必须同时覆盖两个字段。按款型聚合时优先读取
`CLTC纯电续航[km]`，为空时回退到 `CLTC纯电续航里程[km]`；不得把两个值相加。
