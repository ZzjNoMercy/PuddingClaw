---
formatter: sql-guardrail
id: air_suspension_reference_type_value
name: 空气悬架使用可调悬架种类字段
enabled: true
version: 0.1.0
type: require_sql_contains
scope:
  table_scope:
    mode: any
    values: []
  semantic_assets:
  - measure:config_rate:references/air_suspension
params:
  contains: type_name = '可调悬架种类'
  when_contains_any:
  - 空气悬架
  - 空气悬挂
action:
  type: rewrite
  message: 空气悬架必须使用 type_name = '可调悬架种类' 且 type_value 包含 '空气悬架'。
updated_at: '2026-07-09 15:46:40'
---

# 空气悬架使用可调悬架种类字段

## 业务约束

空气悬架必须使用 type_name = '可调悬架种类' 且 type_value 包含 '空气悬架'。

## 命中范围

- 表范围：any / 不限制
- 语义资产：measure:config_rate:references/air_suspension

## Detector 参数

```yaml
contains: type_name = '可调悬架种类'
when_contains_any:
- 空气悬架
- 空气悬挂
```

## 推荐处理

空气悬架必须使用 type_name = '可调悬架种类' 且 type_value 包含 '空气悬架'。

## 风险说明

- frontmatter 是机器执行配置，正文只用于人工审核和 LLM 理解。
- 修改正文不会改变执行逻辑；需要同步修改 frontmatter。
