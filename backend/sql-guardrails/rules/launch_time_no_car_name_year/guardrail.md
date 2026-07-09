---
formatter: sql-guardrail
id: launch_time_no_car_name_year
name: 上市时间不能从款型名推断
enabled: true
version: 0.1.0
type: forbid_sql_pattern
scope:
  table_scope:
    mode: any
    values: []
  semantic_assets:
  - dimension:launch_time
params:
  pattern: \bcar_name\b\s+(?:LIKE|ILIKE)\s+['\"]\d{2}款%
  unless_contains: type_name = '上市时间'
action:
  type: rewrite
  message: 命中语义资产“上市时间”，必须改用 type_name = '上市时间' 的 type_value 过滤真实上市日期。
updated_at: '2026-07-09 15:46:40'
---

# 上市时间不能从款型名推断

## 业务约束

命中语义资产“上市时间”，必须改用 type_name = '上市时间' 的 type_value 过滤真实上市日期。

## 命中范围

- 表范围：any / 不限制
- 语义资产：dimension:launch_time

## Detector 参数

```yaml
pattern: \bcar_name\b\s+(?:LIKE|ILIKE)\s+['\"]\d{2}款%
unless_contains: type_name = '上市时间'
```

## 推荐处理

命中语义资产“上市时间”，必须改用 type_name = '上市时间' 的 type_value 过滤真实上市日期。

## 风险说明

- frontmatter 是机器执行配置，正文只用于人工审核和 LLM 理解。
- 修改正文不会改变执行逻辑；需要同步修改 frontmatter。
