# Guardrail Markdown Template

```markdown
---
formatter: sql-guardrail
id: stable_snake_case_id
name: 人类可读名称
enabled: true
version: 0.1.0
type: forbid_sql_pattern
scope:
  table_scope:
    mode: any
    values: []
  semantic_assets: []
params:
  pattern: ""
action:
  type: rewrite
  message: 命中后给 SQL 重写模型的明确修正要求。
created: 2026-07-09 00:00:00
updated_at: 2026-07-09 00:00:00
---

# 人类可读名称

## 业务约束

说明这条守卫保护的业务口径。

## 禁止写法

```sql
-- 写出应该避免的 SQL 形态。
```

## 推荐写法

```sql
-- 写出推荐 SQL 形态或字段路径。
```

## 适用场景

- 命中的语义资产
- 相关表
- 典型用户问题

## 风险说明

- 这条守卫能覆盖什么。
- 这条守卫不能覆盖什么。
- 是否可能误伤。

## 语义资产同步建议

- 如果需要，同步更新 measure/dimension/grain Markdown。
```

