---
name: build-logical-dataset
description: Build or extend a reusable logical dataset by vertically combining periodic spreadsheet table assets. Use when a user asks to merge, concatenate, union, combine monthly or weekly files, or append a later period into an existing logical dataset.
toolsets:
  - logical_dataset
---

# Build Logical Dataset

Create a reusable **virtual** data asset, never an invisible one-off Pandas concat.
Creating or appending writes a small `dataset.json` definition containing sources, schema policy, lineage rules and routing metadata. Do not expect it to read every row or generate Parquet at creation time.

## Workflow

1. When the user supplied a new Excel/CSV/TSV attachment, call `ensure_attachment_table_asset` **first**. It deduplicates against the knowledge base, imports the attachment when needed, and returns the registered sheet/table `asset_id` values. Do not use an attachment path as a long-term logical-dataset source.
2. Call `list_logical_dataset_candidates` to find registered candidate table assets, including the assets returned from the import step.
3. Compare the returned field lists. Do not guess that similarly named monthly files have the same schema.
4. Call `request_logical_dataset_rule` with the candidate asset IDs, names, fields, suggested name, and operation:
   - `create`: provide at least two raw table candidates; the user selects both participating sources and one baseline.
   - `append`: provide `target_asset_id` for the existing logical dataset plus at least one **new raw table** candidate. The target is the fixed schema baseline, never an append source. If it appears in a discovery result, it may be passed for display metadata but must not be selected as a source.
5. Wait for the HITL card. The user must choose the participating tables, one baseline table, and the schema strategy.
6. After it resumes, call `apply_logical_dataset_rule` with the exact returned `dataset_rule`. Do not change field policy or source order yourself.
7. Report the resulting asset ID, estimated rows, retained fields, registered sources, selected strategy and that the dataset is virtual.

## Field Strategies

- `strict`: only for identical field sets; field order may differ.
- `baseline_fill_missing`: preserve baseline fields only; discard fields found only in other tables; missing baseline fields become null.
- `union_fill_missing`: preserve the union of all fields; every source missing a field receives null for that field.

## Rules

- The baseline table is a **schema decision**, not a claim that its data is more truthful.
- Never silently discard extra fields or silently widen the logical schema.
- Preserve row lineage automatically; `_pc_source_*` fields are system-managed and must not be supplied by source tables.
- Appending a period preserves the logical dataset asset ID and all already registered sources. The confirmed `source_asset_ids` for an append contain only newly added raw assets; `target_asset_id` identifies the existing logical dataset separately.
- A logical dataset is a data asset. Bind dimensions and use it in models only after it is created, rather than binding every monthly raw file independently.
- An attachment becomes reusable only after `ensure_attachment_table_asset` succeeds. This preserves a stable `table_asset` reference, profile lifecycle, deletion behavior and later source append/reuse.
- For trend, period comparison, YoY/MoM and cross-source aggregation, prefer the logical dataset. When analysis actually reads it, the runtime expands its sources into one DataFrame on demand.
- For an explicitly named file or a single-period detail request, a raw source may be used directly.
