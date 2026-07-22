---
formatter: sql-guardrail
id: voltage_platform_800v_physical_value
name: 800V 平台使用真实枚举值
enabled: true
version: 0.1.0
type: forbid_sql_pattern
scope:
  table_scope:
    mode: any
    values: []
  semantic_assets:
  - measure:config_rate
  intent_any:
  - 高压平台
  - 电压平台
  - 800V
params:
  pattern: '[''\"]800V[''\"]'
action:
  type: rewrite
  message: 高压平台物理枚举必须使用 '800V平台'，不能精确匹配不存在的 '800V'。
updated_at: '2026-07-21 00:00:00'
---

# 800V 平台使用真实枚举值

## 业务约束

数据库中的高压平台物理枚举为 `800V平台`；精确匹配 `800V` 会静默漏数。

## 推荐处理

由 Generator 使用 Profile 中的真实枚举重新生成 SQL，Agent 不得手改 SQL。
