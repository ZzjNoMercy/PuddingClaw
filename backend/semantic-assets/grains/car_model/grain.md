---
formatter: semantic-asset
name: 款型颗粒度
type: grain
description: 配置率等指标按款型作为统计对象时的去重和判定口径。
aliases:
  - 款型维度
  - 按款型
  - 车型款型
tags:
  - 汽车产品配置
  - vehicle_params
version: 0.1.0
created: 2026-07-08 00:00:00
updated_at: 2026-07-08 00:00:00
---

# 款型颗粒度

## 统计对象

统计对象是 `car_name`。

## 计算规则

- 分母按当前筛选范围内的 `distinct car_name` 计算。
- 分子按当前筛选范围内命中目标条件的 `distinct car_name` 计算。
- 同一 `car_name` 有多条配置记录时，只能计一次。

## 适用场景

用户说“按款型”“款型维度”“车型配置率”时，优先使用该颗粒度。
