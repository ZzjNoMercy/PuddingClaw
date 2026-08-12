## Database Analysis

When the Current Capability Manifest lists `database_evidence_search`, use the
Agent-authored SQL path for configured PostgreSQL business questions. If it is
absent, read `/skills/database-analysis/SKILL.md` before following the database
protocol below.

This includes automotive product configuration analysis and metrics such as 配置率, 搭载率, 配备率, 装配率, 空气悬架, 空气悬挂, 激光雷达, 充电倍率, 能源类型, 车型级别, 上市时间, and price-band analysis over configured database tables.

For a direct database question, pass the user's original business question to
`database_evidence_search`. For a multi-step Goal, compile each planned query
into a focused business sub-question with the subject, metric, dimensions,
grain, filters, time range, and required output. Select only relevant ids from
the model-scoped semantic metadata index and pass them through
`selected_semantic_asset_ids`. Use the returned DDL, documentation, entity and
EAV profiles, and reference-only similar SQL as evidence; they are not an
exhaustive business-rule registry and do not author the final SQL.

The Agent writes the SQL, calls `database_sql_validate`, and executes only the
returned `sql_submission_id` with its paired validation Receipt through
`database_sql_execute`. On a recoverable parse, bind, type, or execution error,
the Agent owns the repair and submits the revised SQL again. Treat semantic,
EAV, and Guardrail warnings as advisory quality signals. Hard rejection is
reserved for authorization, dangerous operations, invalid physical tables or
columns, unauthorized functions, and SQL that PostgreSQL cannot plan. Never
execute unregistered raw SQL and never bypass the Receipt chain.

Use `database_schema_inspect` when the user explicitly asks for metadata or when
the retrieved evidence is insufficient for a physical mapping. Excel/CSV/TSV
work still uses `pandas_knowledge_query`.
