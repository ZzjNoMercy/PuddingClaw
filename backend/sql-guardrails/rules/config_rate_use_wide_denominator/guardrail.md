---
formatter: sql-guardrail
id: config_rate_use_wide_denominator
name: 配置率优先使用宽表分母
enabled: true
version: 0.1.0
type: require_table_when_available
scope:
  table_scope:
    mode: all
    values:
    - vehicle_params
    - vehicle_params_wide
  semantic_assets:
  - measure:config_rate
params:
  required_table: vehicle_params_wide
  fallback_table: vehicle_params
action:
  type: rewrite
  message: 配置率必须使用 vehicle_params_wide 先筛选分母款型，再 JOIN vehicle_params 判断配置明细。
updated_at: '2026-07-09 15:46:40'
---

# 配置率优先使用宽表分母

## 业务约束

配置率必须使用 vehicle_params_wide 先筛选分母款型，再 JOIN vehicle_params 判断配置明细。

## 命中范围

- 表范围：all / vehicle_params, vehicle_params_wide
- 语义资产：measure:config_rate

## Detector 参数

```yaml
required_table: vehicle_params_wide
fallback_table: vehicle_params
```

## 推荐处理

配置率必须使用 vehicle_params_wide 先筛选分母款型，再 JOIN vehicle_params 判断配置明细。

## 风险说明

- frontmatter 是机器执行配置，正文只用于人工审核和 LLM 理解。
- 修改正文不会改变执行逻辑；需要同步修改 frontmatter。
