---
name: build-semantic-dimension
description: Build or refresh a reusable semantic dimension from one or more data assets. Use when a user needs cross-source entity normalization, a lookup/crosswalk, a calendar dimension, or an intermediate dimension table for analytics, such as matching sales series to product configuration series, unifying customers, stores, SKUs, channels, or dates across tables.
toolsets:
  - semantic_dimension_build
  - semantic_lookup
---

# Build Semantic Dimension

Build semantic dimensions as reusable analysis assets, not physical changes to fact tables.

## Workflow

1. Inspect each candidate input with `inspect_dimension_build_input`. An uploaded spreadsheet attachment is a valid temporary input and must **not** be imported into the knowledge base merely to construct a dimension.
2. For a **new dimension** or an explicit **canonical baseline rebuild**, call `request_dimension_build_rule` with `operation="refresh"`; the HITL card selects the canonical input. For **adding a file/table as a new source column to an already published dimension**, call it with `operation="append_source"`; the tool injects and locks the current `active_crosswalk.json` as canonical, so the HITL card only maps the new source fields. Never treat a monthly source file as a replacement canonical baseline during an append.
3. After the request resumes with `build_rule`, put that exact JSON into `input_snapshot.build_rule` and call `enqueue_semantic_dimension_build` with the returned adapter. The rule is the only input contract the worker may consume.
4. For a new dimension, create `semantic-assets/dimensions/<dimension_id>/dimension.md` and its `references/` directory from the semantic-dimension template **before** enqueueing. For an existing dimension, do not recreate its package. A newly inspected source can either append a registered source or register a new source; neither action creates a new canonical field unless the user explicitly requests a schema extension.
5. The background Job writes artifacts to a staging directory, validates them, and stops at `waiting_for_publish_confirmation`. It must never alter the active `dimension.md` or active Crosswalk.
6. When the user asks for progress, call `get_semantic_dimension_build_job`. Report coverage, candidates, unmatched values, collisions, and the delta from the active version.
7. Only when the user explicitly says to publish a completed Job: re-read the Job summary, confirm it is waiting for publish, then call `publish_semantic_dimension_build`. This controlled tool writes `generated_crosswalk.json`, overlays `manual_overrides.json`, materializes `active_crosswalk.json`, updates `source_registry.json`, snapshots a version, and updates `dimension.md` in Beijing time. Never use `write_file` for publication.
8. For a database intermediate table, create or refresh only the declared `analytics_dim_<dimension_id>` target after validation. Keep the portable JSONL/JSON artifact as the audit and migration source.

### Registered Fast Path

For an explicit all-brand refresh of the registered `vehicle_series` dimension, do not rediscover its directory, bindings, schema, or source assets in the chat turn. Its contract is already registered: enqueue `vehicle_series_full` with `requested_scope: {brands: all}` and let the background builder load and validate the declared inputs. Return the job id immediately.

## Rules

- Keep scripts in this Skill. Keep semantic asset folders limited to semantic documentation and completed data artifacts.
- A generic `entity_crosswalk_v1` refresh takes its canonical universe from the HITL-confirmed binding. In `append_source` mode it instead reads the already published `active_crosswalk.json` as the fixed canonical universe; the new source can only add bindings or diagnostics and cannot add, rename, or remove canonical entities. Its Crosswalk uses the fixed `entity-resolution-crosswalk/v1` schema.
- `active_crosswalk.json` is the only runtime Crosswalk. `generated_crosswalk.json` is a rebuildable baseline; user corrections are recorded as operations in `manual_overrides.json`, not by editing either Crosswalk directly. `source_registry.json` records reusable source identity mappings such as 上险量 `品牌 + 1-子车型` or 订单 `品牌名称 + 车系名称`.
- For legacy `vehicle_series_full`, `vehicle_params_wide.brand + serial_name` remains the registered fast-path canonical universe. This is an adapter-specific rule, not a platform-wide default.
- A canonical entity without a current source binding is `canonical_only`: keep it in the Crosswalk, but mark it `join_eligible=false` for that source. Keep source-side `candidate` and `unmatched` keys as diagnostics rather than attaching them to a guessed entity.
- A source brand may be a parent group or alias. An unresolved brand can still bind when its normalized series key has exactly one exact match across the canonical universe. Never use global fuzzy matching or choose among duplicate global series names.
- Never join two fact sources by guessed raw text in a formal analysis. Use `entity_key` or another declared canonical key.
- Only `auto_matched` and `accepted` records with `join_eligible=true` may enter formal numerator, denominator, or joined detail.
- Automatically publish only deterministic high-confidence matches. Put fuzzy, conflicting, and missing values in reviewable `candidate` or `unmatched` outputs.
- Do not turn deterministic normalized-exact matching into a fuzzy auto-join. Add a domain adapter when special normalization rules are required; keep the generic contract and artifact schema unchanged.
- Do not execute an adapter automatically from an analysis request. Require a user action or an explicitly authorized workflow.
- A successful build is not a publish. Staging artifacts are never eligible for formal analysis until the user explicitly confirms publication.

## Resources

- Read `references/dimension-builder-contract.md` before creating a new adapter or intermediate table.
- The local `scripts/` folder contains builder and validator implementations for background workers. Do not execute them directly from this Skill in an Agent conversation.
- `vehicle_series_full.py` is the configuration-first full-build adapter. `vehicle_series_demo.py` is a small two-source entity-lookup fixture. They demonstrate the contract; do not copy their vehicle-specific prefix rules into unrelated domains.
