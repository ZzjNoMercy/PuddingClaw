---
name: database-analysis
description: Analyze configured relational data sources through staged schema, SQL generation, validation and execution.
toolsets:
  - database_analysis
  - semantic_lookup
---

# Database Analysis

For a business question, use the enabled path. The Agent path is:
`database_evidence_search` → write SQL from the returned evidence →
`database_sql_validate` → `database_sql_execute` with the submission and
Validation Receipt. Evidence search never generates SQL and similar SQL is
reference-only. Legacy generation tools are retained server-side for
standalone compatibility but are not exposed in the Agent toolset; do not try
to call them from this workflow.

Validator warnings are structured quality signals. Let the Agent decide whether
to inspect more evidence, revise SQL, execute, ask the user, or stop. Missing or
conflicting EAV/semantic evidence never blocks an otherwise authorized read-only
SQL submission. Hard rejection is reserved for permission/scope violations and
dangerous SQL. Do not put a SQL repair instruction or business enum choice into
the evidence tool. Only a server-declared infrastructure failure may fallback
to the legacy path.

## Question and physical routing

- For a standalone request, preserve the user's business intent and wording.
  Resolve arbitrary business phrasing from the selected model's table names,
  schema, descriptions, semantic assets, and retrieved evidence; do not build
  an exhaustive synonym list.
- The Agent may select a table subset only from data assets declared by the
  active analytics model. The database source's selected-table allowlist is an
  additional boundary, not a replacement for model scope.
- For a Goal, compile a focused business sub-question without changing the
  metric, population, grain, filters, or time range. Physical table/column
  selection is allowed, but do not put an Agent-written `SELECT`, `JOIN`, CTE,
  or replacement SQL program in `question`.
- EAV profiles and entity mappings are evidence for Agent reasoning, not an
  exhaustive business rule registry. Inspect or retrieve more evidence when it
  helps, but the Agent owns the final SQL interpretation and may proceed after
  explaining its chosen business mapping.

After generation, report the resolved tables and any model-declared alias
resolution, then validate and execute the registered generation.

After the Agent path, report relevant evidence provenance and Validator warnings
when they affect confidence. Execute only the exact `sql_submission_id` paired
with its `validation_receipt_id`; execution rechecks current authorization and
SQL safety, not Evidence freshness or semantic equivalence.

For migration qualification, historical generation records can be converted
to JSONL replay cases and checked with
`python -m analytics.nl2sql.agent_sql_replay <cases.jsonl>`. This is a static
admission preflight; a live Validator adapter is required before claiming
column, semantic, EAV, or database-result parity.
