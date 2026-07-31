---
formatter: semantic-asset
id: dimension:battery_energy
name: 电池电量
type: dimension
version: "0.1.0"
description: 动力电池电量（kWh）的标准物理字段映射。
aliases: ["电量", "电池容量", "电池包容量", "battery capacity", "battery energy"]
tags: ["汽车产品配置", "vehicle_params", "电池/补能"]
resolution:
  mode: source_field
  bindings:
    - asset_ref: dbs_77982e981bac4a6fa8.vehicle_params
      fields:
        type_name: "电池电量[kWh]"
        value: type_value
created: "2026-07-31 00:00:00"
updated_at: "2026-07-31 00:00:00"
---

# 电池电量

## 字段口径

- 用户所说的“电量”“电池容量”或“电池包容量”，在当前数据源中统一映射为
  `vehicle_params.type_name = '电池电量[kWh]'`。
- 数值读取 `type_value`，单位为 kWh。
- 不得使用不存在的 `电池容量[kWh]` 作为物理配置名称。
