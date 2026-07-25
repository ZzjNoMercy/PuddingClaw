---
formatter: sql-guardrail
id: semantic_enum_consistency
name: 语义资产枚举一致性
enabled: true
version: 0.1.0
type: semantic_enum_consistency
scope:
  table_scope:
    mode: any
    values: []
  semantic_assets: []
  intent_any: []
params: {}
action:
  type: rewrite
  message: SQL 字面量与语义资产声明的枚举、分类映射或禁止模式不一致。
updated_at: '2026-07-24 00:00:00'
---

# 语义资产枚举一致性

## 规则说明

本规则不含任何业务知识。检测内容由语义资产 frontmatter 的
`classifications`、`enum_universe`、`forbidden_patterns` 声明驱动：
作用于受治理列（资产 `governed` 声明的物理列或 EAV type_name）的字面量
必须与资产声明一致；业务大类的枚举必须与 `classifications` 映射一致；
ELSE 归入业务大类视为口径覆盖。

## 推荐处理

由 Generator 按冲突消息中的结构化 diff 重新生成，Agent 不得手改 SQL。
确属业务口径变更时，由用户显式确认后携带新约束重新生成。
