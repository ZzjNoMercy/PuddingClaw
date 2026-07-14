---
formatter: sql-guardrail
id: config_rate_use_model_base_denominator
name: 配置率优先使用款型基础表分母
enabled: true
version: 0.2.0
type: require_table_when_available
scope:
  table_scope:
    mode: all
    values:
    - vehicle_params
    - vehicle_model_base
  semantic_assets:
  - measure:config_rate
  intent_any:
  - 配置率
  - 搭载率
  - 渗透率
  - 配备率
  - 占比
params:
  required_table: vehicle_model_base
  fallback_table: vehicle_params
action:
  type: rewrite
  message: 配置率必须使用 vehicle_model_base 先筛选分母款型，再 JOIN vehicle_params 判断配置明细。
updated_at: '2026-07-14 00:00:00'
---

# 配置率优先使用款型基础表分母

## 业务约束

配置率必须使用 vehicle_model_base 先筛选分母款型，再 JOIN vehicle_params 判断配置明细。

## 命中范围

- 表范围：all / vehicle_params, vehicle_model_base
- 语义资产：measure:config_rate

## Detector 参数

```yaml
required_table: vehicle_model_base
fallback_table: vehicle_params
```

## 推荐处理

配置率必须使用 vehicle_model_base 先筛选分母款型，再 JOIN vehicle_params 判断配置明细。

## 风险说明

- frontmatter 是机器执行配置，正文只用于人工审核和 LLM 理解。
- 修改正文不会改变执行逻辑；需要同步修改 frontmatter。
