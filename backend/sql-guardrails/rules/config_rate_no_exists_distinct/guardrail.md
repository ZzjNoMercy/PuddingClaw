---
formatter: sql-guardrail
id: config_rate_no_exists_distinct
name: 配置率禁止多层 EXISTS DISTINCT 慢查询
enabled: true
version: 0.1.0
type: forbid_exists_distinct_pattern
scope:
  table_scope:
    mode: any
    values:
    - vehicle_params
  semantic_assets:
  - measure:config_rate
params:
  table: vehicle_params
  distinct_column: car_name
  min_exists_count: 2
action:
  type: rewrite
  message: 配置率不要使用 DISTINCT car_name + 多层 EXISTS/NOT EXISTS 自关联。请一次扫描相关 type_name，并按
    brand, serial_name, car_name 聚合 BOOL_OR flags。
updated_at: '2026-07-09 15:46:40'
---

# 配置率禁止多层 EXISTS DISTINCT 慢查询

## 业务约束

配置率不要使用 DISTINCT car_name + 多层 EXISTS/NOT EXISTS 自关联。请一次扫描相关 type_name，并按 brand, serial_name, car_name 聚合 BOOL_OR flags。

## 命中范围

- 表范围：any / vehicle_params
- 语义资产：measure:config_rate

## Detector 参数

```yaml
table: vehicle_params
distinct_column: car_name
min_exists_count: 2
```

## 推荐处理

配置率不要使用 DISTINCT car_name + 多层 EXISTS/NOT EXISTS 自关联。请一次扫描相关 type_name，并按 brand, serial_name, car_name 聚合 BOOL_OR flags。

## 风险说明

- frontmatter 是机器执行配置，正文只用于人工审核和 LLM 理解。
- 修改正文不会改变执行逻辑；需要同步修改 frontmatter。
