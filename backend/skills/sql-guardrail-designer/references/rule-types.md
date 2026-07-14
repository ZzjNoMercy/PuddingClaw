# SQL Guardrail Rule Types

## forbid_sql_pattern

Use when SQL must not contain a regex-matched shape.

Required params:

```yaml
params:
  pattern: "\\bcar_name\\b\\s+(?:LIKE|ILIKE)\\s+['\\\"]\\d{2}款%"
```

Optional params:

```yaml
params:
  unless_contains: "type_name = '上市时间'"
  flags:
    - case_sensitive
```

Default matching is case-insensitive. `case_sensitive` makes matching case-sensitive.

## require_sql_contains

Use when a semantic context requires SQL to include a specific fragment.

```yaml
params:
  contains: "type_name = '可调悬架种类'"
  when_contains_any:
    - 空气悬架
    - 空气悬挂
```

`when_contains_any` checks the generated SQL text. Use it only as a narrow trigger.

## require_table_when_available

Use when route selected multiple tables and the SQL must use a preferred table.

```yaml
scope:
  table_scope:
    mode: all
    values:
      - vehicle_params
      - vehicle_model_base
params:
  required_table: vehicle_model_base
  fallback_table: vehicle_params
```

## require_group_by

Use when aggregation grain must include specific columns.

```yaml
params:
  require_columns:
    - brand
    - serial_name
    - car_name
  forbidden_columns_only:
    - car_name
```

## forbid_exists_distinct_pattern

Use for EAV slow-query guardrails, especially repeated `EXISTS` with `COUNT(DISTINCT ...)`.

```yaml
params:
  table: vehicle_params
  distinct_column: car_name
  min_exists_count: 2
```

## Scope

Guardrails match routed tables and semantic assets only.

```yaml
scope:
  table_scope:
    mode: any
    values:
      - vehicle_params
  semantic_assets:
    - measure:config_rate
```

`table_scope.mode`:

- `any`: trigger if any listed table is routed.
- `all`: trigger only if all listed tables are routed.

Do not use data source filters. Data source selection is only a UI convenience for choosing tables.

