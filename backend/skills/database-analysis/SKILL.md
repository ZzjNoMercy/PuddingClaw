---
name: database-analysis
description: Analyze configured relational data sources through staged schema, SQL generation, validation and execution.
toolsets:
  - database_analysis
  - semantic_lookup
---

# Database Analysis

For a business question, generate SQL, validate it, then execute it. Inspect
schema only when the user needs metadata or an observed physical mapping needs
evidence. Use semantic lookup whenever a published entity dimension is needed
for normalization.

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
- EAV enum values must pass the server's semantic-evidence guard. For other
  entity mappings, perform semantic lookup or inspect retrieved database/schema
  evidence and retain that provenance. Never guess a mapping merely because a
  table or column name looks plausible.

After generation, report the resolved tables and any model-declared alias
resolution, then validate and execute the registered generation.
