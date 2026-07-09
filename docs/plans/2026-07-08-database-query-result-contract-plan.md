# Database Query Result Contract Plan

## Background

`database_knowledge_query` currently mixes three different concepts:

- SQL execution result
- Rows shown to the model/user
- Trace preview payload

This caused a concrete failure in `session-2894a01983f1.json`: SQL returned 22 rows, but the tool output and trace only included the first 20 preview rows. The omitted rows contained `腾势`, so the assistant concluded incorrectly that `腾势` had no June launches.

The failure was not that SQL missed the data. The data was lost between SQL execution and model context.

## First Principles

The LLM should not read a dataset to derive data facts. It should orchestrate SQL, then explain facts computed by the database or deterministic backend code.

Facts must come from complete computation:

- `COUNT`, `SUM`, `AVG`, `MIN`, `MAX`, and `GROUP BY` must be executed by SQL over the full result set.
- Preview rows are only for display and spot-checking.
- The model must not infer absence from preview rows.

Result visibility must be explicit:

- Every result must say whether rows are complete or preview-only.
- Every preview must include `total_row_count`, `preview_count`, and `omitted_count`.
- Any omitted rows must be represented by deterministic summaries where possible.

Large-detail workflows must preserve data without pushing all rows into the model:

- The backend owns execution, profiling, pagination, and export.
- The frontend only displays pages and actions.
- The LLM explains profiles and chooses follow-up actions; it does not count rows itself.

## Target Architecture

```text
User question
  -> intent classification
    -> aggregate query
    -> detail query
    -> exploratory query
  -> NL2SQL / SQL generation
  -> read-only SQL validation
  -> execution
  -> result profiling
  -> result contract
  -> LLM explanation + UI rendering
```

## First Version Scope

The first implementation should prioritize correctness over full product coverage.

In scope:

- Prevent silent data loss between SQL execution, tool output, trace, and model context.
- Add deterministic profile summaries for SQL results.
- For budget-safe result sets, include full rows instead of preview-only rows.
- For larger result sets, include preview rows plus profile summaries and explicit omitted counts.
- Add LLM guardrails so preview rows are not used for absence claims.
- Add Vanna SQL-generation rules for aggregate/detail separation.
- Add user-configurable Smart Database Q&A settings in the Settings page.

Out of scope for the first version:

- User-configurable semantic-model profile dimensions.

These out-of-scope items remain in the plan as later phases.

Deferred but required for large-detail correctness:

- Full result-store API.
- Frontend pagination UI.
- Async CSV/Parquet export jobs.

For the concrete `session-2894a01983f1.json` failure mode, first-version behavior should be enough:

- If SQL returns 22 short rows, include all 22 rows because the serialized result fits the model-context budget.
- If a future threshold still previews only 20 rows, profile must state `品牌: 比亚迪 20, 腾势 2`.
- The model must not say `腾势` has no results.

## Query Intent Policy

### Aggregate / Analytical Questions

Examples:

- "多少"
- "总计"
- "平均"
- "占比"
- "趋势"
- "按品牌统计"
- "排名"

Expected behavior:

- Generate aggregate SQL.
- Prefer `GROUP BY` and aggregate functions.
- Do not use `LIMIT` to approximate aggregates.
- If user asks top-N, use `ORDER BY ... LIMIT N` only after the relevant aggregation.

### Detail / Listing Questions

Examples:

- "列出所有"
- "导出明细"
- "展示每条记录"
- "所有车型"

Expected behavior:

- Execute the detail SQL safely.
- Count full result size.
- Return preview rows plus profile.
- If result is large, return `result_id` for pagination/export.
- Do not put all rows into the model context.

### Exploratory Questions

Examples:

- "看看这个数据"
- "分析一下"
- "有什么特点"

Expected behavior:

- Run profile queries first.
- Suggest drill-down dimensions.
- Only query detail after the user asks for a specific slice.

## Vanna SQL Generation Rules

Add an explicit analytics rule block to Vanna prompt context or training documentation.

Draft:

```python
ANALYTICS_SYSTEM_PROMPT = """
你是一个数据分析助手。请遵循以下规则生成 SQL：
1. 汇总/统计/趋势类问题，优先使用聚合函数和 GROUP BY。
2. 不要对聚合结果使用 LIMIT 来近似回答。
3. 明细查询只在用户明确要求时生成。
4. 如果生成的 SELECT 可能返回大量行（如没有聚合、没有 WHERE 过滤），请添加说明建议用户聚合或分页。
5. 对明细查询不要为了减少结果而擅自加 LIMIT；结果规模、预览、分页和导出由执行层处理。
6. 当用户按月份查询 ISO 日期字符串时，优先使用日期函数或 '-MM-' 形式匹配，不要只使用中文月份字符串如 '%6月%'。
"""
```

These rules are not a substitute for execution-layer checks. They only reduce bad SQL generation. The backend result contract still has to enforce completeness metadata and profiling.

## Result Contract

`database_knowledge_query` should return a structured result internally and format a safe Markdown view for the model.

Proposed internal shape:

```json
{
  "sql": "SELECT ...",
  "columns": ["车型名称", "品牌", "上市日期", "价格"],
  "row_count": 1220,
  "is_complete": false,
  "preview_count": 50,
  "omitted_count": 1170,
  "limited_by_sql_runner": false,
  "preview_rows": [],
  "profile": {
    "group_counts": {
      "品牌": {
        "比亚迪": 20,
        "腾势": 2
      }
    },
    "date_ranges": {
      "上市日期": {
        "min": "2022-06-01",
        "max": "2026-06-23"
      }
    },
    "numeric_ranges": {
      "价格": {
        "min": 7.88,
        "max": 34.98
      }
    }
  },
  "result_id": "optional-result-id",
  "result_store": {
    "enabled": true,
    "artifact_path": "backend/data/database-query-results/qr_abc123.jsonl",
    "expires_at": "2026-07-15T00:00:00Z"
  },
  "actions": [
    {"type": "paginate", "label": "分页查看明细"},
    {"type": "export_csv", "label": "导出 CSV"},
    {"type": "aggregate", "label": "按维度聚合"}
  ],
  "llm_guardrail": "preview_rows are samples for display only. Do not infer that omitted groups do not exist."
}
```

Markdown shown to the LLM should include:

- SQL
- total row count
- preview count
- omitted count
- profile summary
- preview rows
- explicit guardrail

Example:

```text
结果共 22 行，下面展示 20 行，省略 2 行。
品牌分布：
- 比亚迪：20
- 腾势：2

注意：下方表格是预览，不得根据预览断言未展示品牌不存在。
```

## Profiling Policy

Profile must be deterministic backend computation, not LLM reasoning and not subagent work.

Reason:

- Profile is factual.
- It must be reproducible.
- It must not hallucinate.
- It must be testable.

The LLM may explain profile results, but it must not generate facts by counting preview rows.

If profile reveals that preview rows omitted a relevant group, the assistant should mention that group from profile. It should not need a subagent to rediscover it. If the user's requested answer needs row-level details for the omitted group and those details are not present because the result is preview-only, the assistant should request a page/detail fetch or use a follow-up query.

For first version, this is avoided for budget-safe result sets by including all rows when the serialized result fits the model-context budget.

### Field Selection

Start with simple heuristics:

- Prefer known dimension names:
  - `品牌`, `brand`
  - `车系`, `serial_name`
  - `车型名称`, `car_name`
  - `分类`, `category`
  - `配置名称`, `type_name`
- Date-like names:
  - `日期`, `时间`, `date`, `time`, `created_at`, `上市日期`
- Numeric-like names:
  - `价格`, `金额`, `销量`, `数量`, `price`, `amount`, `count`, `qty`

For small results already materialized in memory:

- Compute profile from result rows directly.

For large results:

- Prefer SQL-side profiling over materializing all rows.
- Wrap the generated SQL as a subquery:

```sql
SELECT "品牌", COUNT(*)
FROM (<original_sql>) AS q
GROUP BY "品牌"
ORDER BY COUNT(*) DESC
LIMIT 100;
```

For date/numeric ranges:

```sql
SELECT MIN("上市日期"), MAX("上市日期")
FROM (<original_sql>) AS q;
```

## Completeness Policy

Use a token/size budget first. Row count is only a fallback guardrail.

Why:

- 100 rows with 4 short columns may be cheap.
- 20 rows with long text/blob-like fields may be too large.
- The model-context risk is determined by serialized payload size, not only row count.

Decision order:

1. Serialize candidate rows in the same compact JSON/Markdown shape that will be sent to the tool output and trace.
2. Estimate token cost from serialized characters. First version can use a conservative approximation such as `ceil(char_count / 3)` for mixed Chinese/ASCII text, then replace it with the deployed tokenizer if available.
3. Include full rows only when all budget checks pass.
4. Otherwise return preview rows plus deterministic profile and, when supported, `result_id`.

Suggested defaults:

- `full_rows_token_budget <= 10000`: full rows may enter tool output and trace.
- `preview_rows_token_budget <= 3000`: preview rows should stay within this budget.
- `profile_token_budget <= 3000`: profile summaries should stay within this budget.
- `row_count <= 200`: soft cap for full-row inclusion, even when token estimate is low.
- `column_count <= 20`: soft cap for full-row inclusion.
- `max_cell_chars <= 500`: truncate or exclude oversized cells from model-facing rows, while keeping raw data in result store/export.
- If any full-row budget check fails: preview rows enter context, profile enters context, full result may be cached under `result_id`.
- `row_count > 5000`: profile enters context, preview enters context, detail pagination/export only unless the SQL is an aggregate result.

These are defaults. They should be configurable.

Important: `LIMIT` used for preview must not change the factual answer. It only controls display.

For detail questions, profile is not the final answer. It is a coverage and routing mechanism. If full rows do not fit the budget, the LLM should use `result_id` pagination/export to obtain the necessary detail, or tell the user the complete detail is available as a paged/exportable result rather than pretending that the preview is complete.

## Smart Database Q&A Workspace

Add a dedicated `智能问数` product area. It should not be only a settings form.

The persisted-result browser belongs in the main `/analytics` 智能问数 page, not in Settings. Settings should continue to use the existing Settings/config path for tunable values only.

The area should contain two related but separate surfaces:

- `/analytics` -> `查询结果`: persisted database query results created by `database_knowledge_query`.
- `/settings?category=databaseQa` -> `智能问数设置`: token budgets, persistence, TTL, page size, export/profile switches.

The `查询结果` surface is the main product entry for stored query artifacts. Trace can link to it, but Trace should not be the only place where users can inspect persisted database results.

Switch semantics:

- `result_store_enabled`: controls whether large/incomplete detail query results are materialized to `backend/data/database-query-results` and receive a `result_id`. Turning it off disables follow-up pagination/export for new large detail results.
- `export_enabled`: controls the CSV export button and `GET /api/analytics/query-results/{result_id}/export.csv`. Turning it off keeps existing persisted results readable by page but blocks CSV export.

Router boundary:

- The table Router runs inside `database_knowledge_query`, before Vanna SQL generation. Its selected tables and prompt context enter the Vanna SQL-generation context and Trace.
- The Router result does not exist before the main Agent chooses and calls `database_knowledge_query`, so it is not part of the main Agent's first model-input context.
- For business database questions, the main Agent should call `database_knowledge_query` once with the user's original question. It should not first use the same tool to list tables, inspect schema, enumerate brands/categories, or discover `type_name`, unless the user explicitly asks for metadata.

Suggested configurable fields:

```text
analytics.database_qa.full_rows_token_budget = 10000
analytics.database_qa.preview_rows_token_budget = 3000
analytics.database_qa.profile_token_budget = 3000
analytics.database_qa.full_rows_hard_row_cap = 200
analytics.database_qa.full_rows_hard_column_cap = 20
analytics.database_qa.max_cell_chars_for_llm = 500
analytics.database_qa.result_store_enabled = true
analytics.database_qa.result_store_ttl_hours = 168
analytics.database_qa.default_page_size = 100
analytics.database_qa.max_page_size = 500
analytics.database_qa.export_enabled = false
analytics.database_qa.profile_enabled = true
```

Frontend controls:

- Numeric inputs for token budgets, row cap, column cap, cell length cap, page size, and TTL.
- Toggles for result store, export, and profiling.
- Helper text should state that larger budgets may improve direct answers for detail queries but increase model-context cost and latency.

Recommended first-version defaults:

- Keep `full_rows_token_budget=10000`.
- Keep `result_store_ttl_hours=168` by default, equal to 7 days.
- Keep `result_store_enabled=true`, but Phase 1 may only wire config and result metadata; page-fetch/export APIs are completed in Phase 3.
- Keep `export_enabled=false` until the export endpoint exists.

## Pagination / Result Store

Large detail results should not be fully sent to the frontend or LLM.

Decision:

- Store query-result metadata in PostgreSQL.
- Store full detail rows in a file-backed temporary result store, preferably JSONL for first version and Parquet later if needed.
- Return `result_id` to the LLM and frontend when the full detail does not fit the model-facing budget.
- Do not store result data in the LLM context, frontend memory, or `agent-workspaces`.

Suggested storage layout:

```text
backend/data/database-query-results/{result_id}.jsonl
backend/data/database-query-results/{result_id}.parquet
```

Suggested metadata table:

```text
analytics_query_results
- id
- session_id
- tool_call_id
- question
- sql
- columns_json
- row_count
- profile_json
- artifact_path
- artifact_format
- created_at
- expires_at
- status
```

Expiration:

- Default TTL is 7 days, configured by `analytics.database_qa.result_store_ttl_hours = 168`.
- `expires_at` must be written to metadata when the result is created.
- Cleanup should remove both metadata and artifacts after expiry.
- Trace should continue to display expired result metadata, but page fetch/export actions must clearly report that the persisted artifact has expired.

API shape:

```text
POST /api/analytics/database-query
  -> result_id, row_count, profile, preview_rows

GET /api/analytics/query-results/{result_id}?page=2&page_size=50
  -> rows for page 2

GET /api/analytics/query-results/{result_id}/export.csv
  -> CSV download
```

Pagination must have deterministic ordering.

If generated SQL has no stable `ORDER BY`, backend should either:

- Add an ordering based on projected columns where safe, or
- Warn that pagination order is not stable and require export/materialization for full browsing.

LLM tool behavior:

- If the user asks for detail and full rows fit the budget, answer directly from full rows.
- If the user asks for detail and full rows do not fit, use `result_id` to fetch pages until the requested answer is complete.
- If the requested detail is too large to reasonably narrate in chat, provide a concise summary and point to the paged/exportable result.
- The LLM must not treat `profile` as the final answer for "哪些/列出/明细/价格表" questions.

## Trace Integration

Trace must monitor and expose the persistent result lifecycle, not only the preview payload.

For every database query result, trace should include:

```json
{
  "result_id": "qr_abc123",
  "row_count": 1220,
  "is_complete": false,
  "preview_count": 50,
  "omitted_count": 1170,
  "profile": {},
  "result_store": {
    "enabled": true,
    "artifact_path": "backend/data/database-query-results/qr_abc123.jsonl",
    "artifact_format": "jsonl",
    "expires_at": "2026-07-15T00:00:00Z",
    "ttl_hours": 168
  },
  "actions": [
    {"type": "fetch_page", "available": true},
    {"type": "export", "available": true}
  ]
}
```

Trace responsibilities:

- Show whether the model saw full rows or preview rows.
- Show whether a persistent result artifact was created.
- Show artifact path, format, TTL, and expiry time.
- Show page fetch/export actions associated with the `result_id`.
- Mark expired artifacts as expired instead of silently hiding them.
- Never imply that `rows_preview` is complete when `is_complete=false`.

## Frontend Role

The frontend is presentation, not analysis.

Responsibilities:

- Show total rows and preview count.
- Show profile cards/charts.
- Show preview table.
- Provide next/previous page controls when `result_id` exists.
- Provide export action when allowed.
- Visibly label preview-only results.
- Add a `查询结果` view under `智能问数` for persisted database query results.

The frontend should not:

- Compute analytical facts from partial pages.
- Hold thousands of rows just to paginate client-side.

### Smart Database Query Results UI

Add a persisted results browser under the non-settings `/analytics` 智能问数 page.

Suggested layout:

```text
智能问数
  ├─ 查询结果
  │   ├─ result list: latest query results, status, row_count, created_at, expires_at
  │   ├─ result detail: SQL, source, profile, completeness, artifact status
  │   ├─ paged table: columns + rows from GET /api/analytics/query-results/{result_id}
  │   └─ actions: refresh page, page size, export CSV, copy result_id

Settings
  └─ 智能问数设置
      ├─ context budgets
      ├─ result-store TTL/page size
      └─ profiling/export switches
```

Minimum first UI:

- List recent persisted query results.
- Open one result by `result_id`.
- Display paginated rows through the backend page API.
- Display `row_count`, current page, `has_next`, `expires_at`, and expired state.
- Add CSV export button after export API exists.

Trace integration:

- Trace should show `result_id` and link/copy target for the `智能问数 -> 查询结果` view.
- Trace remains an observability surface. The persisted result viewer is the durable user-facing surface.

## LLM Guardrails

The tool output should include hard guidance when rows are preview-only:

```text
Do not infer absence from preview rows. Use profile.group_counts for category coverage.
```

When `is_complete=false`, the LLM must avoid:

- "全部都是..."
- "没有..."
- "仅有..."
- "未发现..."

unless the statement is supported by profile or an aggregate query.

## Vanna / SQL Generation Rules

Add training documentation or prompt context:

```text
Data analysis SQL rules:
1. For counts, totals, averages, ratios, trends, and distributions, generate aggregate SQL.
2. Never use LIMIT to approximate an aggregate answer.
3. For detail-list requests, generate detail SQL. The execution layer will handle count, preview, pagination, and export.
4. When filtering by month on ISO date strings, prefer patterns such as '-06-' or date functions instead of only '%6月%'.
5. Return only read-only PostgreSQL SELECT/WITH SQL.
```

## Implementation Plan

### Phase 1: First-Version Correctness

- [x] Add Vanna analytics SQL-generation rules to prompt context/training documentation.
- [x] Add Smart Database Q&A config schema and defaults.
- [x] Add Settings page section `智能问数` for token budgets and result handling controls.
- [x] Extend `SqlExecutionResult` with:
  - `total_row_count`
  - `preview_count`
  - `omitted_count`
  - `is_complete`
  - `profile`
- [x] Change `run_readonly_sql` so `row_count` means total matched rows, not preview row count.
- [x] Add serialized-result token/size budget estimation for model-facing rows.
- [x] Include all rows in tool output and trace only when the full result fits the configured budget.
- [x] For larger results, include profile plus preview rows.
- [x] Add result-store metadata to trace payloads, including `result_id`, artifact path, TTL, and `expires_at`.
- [x] Rename trace fields where needed:
  - `rows_preview`
  - `preview_count`
  - `omitted_count`
- [x] Add model-facing guardrail text:
  - Preview rows are samples.
  - Do not infer absence from preview.
  - Use profile for group coverage.

### Phase 2: Deterministic Profiling Hardening

- [x] Add deterministic SQL/result profiling in `run_readonly_sql`.
- [x] Compute group counts for low-cardinality dimensions.
- [x] Compute date and numeric ranges.
- [x] Include omitted-group summaries when previews hide categories.
- [ ] Add tests for the `session-2894a01983f1` failure mode:
  - total 22 rows
  - preview 20 rows
  - omitted includes `腾势`
  - model-facing output includes `品牌分布：腾势 2`

### Phase 3: Result Store And Pagination

- [x] Add PostgreSQL result metadata table.
- [x] Add file-backed JSONL result artifact storage under `backend/data/database-query-results/`.
- [x] Return `result_id` for detail results above preview threshold when the full result is materialized.
- [x] Add query-result page API.
- [x] Add `database_query_result_page` agent tool for paged LLM follow-up reads.
- [x] Add synchronous CSV export API.
- [ ] Add async export job API for Parquet or very large CSV exports.
- [x] Add expiry/cleanup helper for result artifacts using `analytics.database_qa.result_store_ttl_hours`.

### Phase 4: Smart Database Query Results UI

- [ ] Show "共 X 行，展示 Y 行，省略 Z 行".
- [ ] Show profile summary above preview table.
- [x] Add `/analytics` `智能问数 -> 查询结果` persisted-result browser.
- [x] Show pagination controls in the persisted-result table when `result_id` exists.
- [x] Show export action in the persisted-result table when CSV export API exists.
- [ ] Add warning badge when result is preview-only.

### Phase 5: LLM And Prompt Hardening

- [ ] Add Vanna SQL generation rules for aggregate/detail separation.
- [ ] Add model-facing guardrail text in tool output.
- [ ] Add regression tests ensuring the assistant does not conclude "no 腾势" from preview-only rows.

## Acceptance Criteria

- SQL returning 22 short rows with the last 2 rows from `腾势` must not lead to "腾势不存在".
- Tool output must show:
  - total rows: 22
  - preview rows: 20 or 22 depending budget
  - omitted rows: 2 if previewed
  - brand distribution including `腾势: 2`
- Trace payload must not imply preview rows are full rows.
- Trace payload must expose persistent result metadata when a result artifact exists.
- Persisted query results must default to 7-day expiry and honor the `智能问数` TTL setting.
- Large detail queries must provide profile and pagination/export affordances.
- Aggregate questions must be answered by aggregate SQL, not by preview rows.

## Open Questions

- Should `full_rows_token_budget=10000` be the only default, or should it switch by model context size?
- Should result store initially support JSONL only, or JSONL plus Parquet?
- Should exports be CSV only initially, or CSV plus Parquet?
- Should profile fields be purely heuristic first, or user-configurable per data model?

## Development Progress

### 2026-07-08

Completed:

- Added `analytics.database_qa` defaults and Settings API read/write support.
- Added Settings page category `智能问数设置`.
- Changed SQL execution to compute total row count separately from model-facing preview rows.
- Added token/size budget checks before full rows enter model context and trace.
- Added deterministic profile summaries for group counts, date ranges, and numeric ranges.
- Added `analytics_query_results` metadata model.
- Added JSONL result artifact storage under `backend/data/database-query-results/`.
- Added query-result page API: `GET /api/analytics/query-results/{result_id}`.
- Added agent tool `database_query_result_page` for LLM follow-up pagination.
- Added result-store metadata to database query trace payloads.
- Added model-facing guardrail text for preview-only results.

Verified:

- `backend/.venv/bin/python -m py_compile backend/config.py backend/api/config_api.py backend/api/analytics.py backend/analytics/nl2sql/schemas.py backend/analytics/nl2sql/sql_runner.py backend/analytics/nl2sql/service.py backend/analytics/nl2sql/result_store.py backend/tools/database_knowledge_tool.py backend/knowledge/models.py`
- `cd frontend && npx tsc --noEmit`
- Smoke-tested `run_readonly_sql(...)` against local `knowledge_bases`.
- Smoke-tested result-store JSONL write and paged read, then removed the smoke artifact and metadata.
- Agent E2E tested through `POST /api/agent`:
  - `e2e-dbqa-20260708-2`: aggregate query over `vehicle_params`, returned `7157625` rows and trace included `total_row_count`, `preview_count`, `is_complete`, and profile fields.
  - `e2e-dbqa-20260708-4`: detail query over 300 `vehicle_params` rows, returned `result_id=qr_20b694df718f45118dca8402`, `fetch_page` action, preview/omitted counts, guardrail text, and artifact path under `backend/data/database-query-results/`.
  - Paged read verified through `GET /api/analytics/query-results/{result_id}?page=2&page_size=5`.

Completed in follow-up:

- Added regression fixture for the 20 BYD + 2 Tengshi omitted-preview failure mode.
- Added query-result list API.
- Added synchronous CSV export API: `GET /api/analytics/query-results/{result_id}/export.csv`.
- Added `/analytics` `智能问数 -> 查询结果` persisted-result browser with list, paginated table, page-size control, and CSV export action.
- Moved persisted-result browsing out of Settings; Settings now only owns Smart Database Q&A tunable configuration.
- Wired `export_enabled` through result metadata, frontend export-button state, and backend CSV export authorization.
- Hardened the main Agent database-question routing prompt and `database_knowledge_query` tool schema so business questions are passed directly into the tool instead of first doing schema/brand/type_name probe calls.
- Investigated the latest DBQA session: no pagination was expected because the result was complete and only 6 rows; the real issue was a long single `database_knowledge_query` call.
- Added stage timing metadata for router, reference/entity recall, SQL generation, SQL execution, and result-store persistence so future Trace payloads can identify which stage is slow.
- Changed SQL execution to fetch the first `materialize_limit + 1` rows before running an outer `COUNT(*)`; small aggregate results no longer pay an extra full count query.
- Normalized PostgreSQL statement-timeout failures into a clear SQL execution timeout message instead of exposing a raw asyncpg/SQLAlchemy error blob to the frontend.

Still not completed:

- Async Parquet / very-large CSV export jobs.
- Warning badge polish for preview-only result cards.
- SQL optimization/index strategy for expensive EAV queries such as `COUNT(DISTINCT car_name)` grouped by extracted model year.
