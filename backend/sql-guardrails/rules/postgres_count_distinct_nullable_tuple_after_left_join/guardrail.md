---
formatter: sql-guardrail
id: postgres_count_distinct_nullable_tuple_after_left_join
name: PostgreSQL LEFT JOIN 后禁止直接 COUNT DISTINCT 右表 nullable tuple
enabled: true
version: 0.1.0
type: forbid_sql_pattern
scope:
  table_scope:
    mode: any
    values: []
  semantic_assets: []
params:
  pattern: "(?=[\\s\\S]*\\bLEFT\\s+JOIN\\b)(?=[\\s\\S]*\\bCOUNT\\s*\\(\\s*DISTINCT\\s*\\([^)]*\\.[^)]*,[^)]*\\)\\s*\\))"
  unless_pattern: "\\bCOUNT\\s*\\(\\s*DISTINCT\\s*\\([^)]*\\.[^)]*,[^)]*\\)\\s*\\)\\s*FILTER\\s*\\(\\s*WHERE\\s+[^)]*\\.[A-Za-z_][\\w]*\\s+IS\\s+NOT\\s+NULL\\s*\\)"
action:
  type: rewrite
  message: "PostgreSQL 会把 ROW(NULL, NULL, ...) 当作一个 distinct tuple。LEFT JOIN 后不要直接 COUNT(DISTINCT (right.col1, right.col2...))，否则未命中行可能多算 1。请改用 FILTER (WHERE right.key IS NOT NULL)、COUNT(right.key)，或先在子查询/CTE 中过滤非空后再计数。"
updated_at: '2026-07-09 19:40:00'
---

# PostgreSQL LEFT JOIN 后禁止直接 COUNT DISTINCT 右表 nullable tuple

## 业务约束

这是一条 PostgreSQL 通用 SQL 语义规则，不绑定任何业务度量值或语义资产。

在 PostgreSQL 中，`COUNT(DISTINCT (a, b, c))` 统计的是 row/composite value。`LEFT JOIN` 未命中时，右表字段会变成 `(NULL, NULL, NULL)`，这个 composite value 会作为一个 distinct row value 参与计数，导致右表命中数可能多算 1。

## 命中范围

- 表范围：any / 不限制
- 语义资产：不限制

## 禁止写法

```sql
SELECT COUNT(DISTINCT (r.brand, r.serial_name, r.car_name))
FROM left_table l
LEFT JOIN right_table r ON ...
```

## 推荐写法

使用 `FILTER` 排除右表未命中行：

```sql
COUNT(DISTINCT (r.brand, r.serial_name, r.car_name))
FILTER (WHERE r.brand IS NOT NULL)
```

或直接统计非空右表 key：

```sql
COUNT(r.primary_key)
```

或先在右表/分子 CTE 内过滤非空并去重，再统计：

```sql
WITH right_dedup AS (
  SELECT DISTINCT brand, serial_name, car_name
  FROM right_table
  WHERE brand IS NOT NULL
)
SELECT COUNT(*)
FROM right_dedup;
```

## Detector 参数

```yaml
pattern: "(?=[\\s\\S]*\\bLEFT\\s+JOIN\\b)(?=[\\s\\S]*\\bCOUNT\\s*\\(\\s*DISTINCT\\s*\\([^)]*\\.[^)]*,[^)]*\\)\\s*\\))"
unless_pattern: "\\bCOUNT\\s*\\(\\s*DISTINCT\\s*\\([^)]*\\.[^)]*,[^)]*\\)\\s*\\)\\s*FILTER\\s*\\(\\s*WHERE\\s+[^)]*\\.[A-Za-z_][\\w]*\\s+IS\\s+NOT\\s+NULL\\s*\\)"
```

## 推荐处理

命中后重写 SQL，避免在 `LEFT JOIN` 后直接统计右表 nullable tuple。优先改为 `FILTER (WHERE right.key IS NOT NULL)` 或分子 CTE 先去重再计数。

## 风险说明

- 这是正则级 SQL 守卫，不是完整 SQL AST 解析；目标是先拦住高风险常见形态。
- SQL 中已经明确 `FILTER (WHERE right.key IS NOT NULL)` 的 tuple count 会通过 `unless_pattern` 放行。
