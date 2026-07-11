# Dimension Builder Contract

## Inputs

Each builder accepts explicit bindings, never a blind database scan:

```yaml
source_bindings:
  - asset_ref: table_asset:tbl_xxx
    key_fields: [品牌, 1-子车型]
  - asset_ref: dbs_xxx.vehicle_params_wide
    key_fields: [brand, serial_name]
canonical:
  source_of_truth: dbs_xxx.vehicle_params_wide
  key: entity_key
  fields: [canonical_brand, canonical_series]
```

When `source_of_truth` is declared, enumerate that asset's complete canonical grain first. Other bindings may attach to a canonical entity only after a deterministic or accepted match; they must not create, rename, or remove canonical entities. A canonical record without a binding for one source uses `canonical_only` and is not join-eligible for that source.

An unresolved source-side parent brand or alias may still bind through a globally unique normalized **exact** canonical value. This fallback must prove that exactly one canonical record matches; fuzzy matching or duplicate values remain candidate/unmatched diagnostics.

## Decision ownership

Keep four concerns deliberately separate so a portable builder remains reviewable:

1. **Input inspection is deterministic.** `inspect_dimension_build_input` reads only an attachment header, an asset Profile/columns, or database table columns. It returns real selectable field names and does not select a key.
2. **The Agent suggests, not decides.** The Skill may use `dimension.md`, user intent and inspected fields to propose the canonical input, positional key mapping and source behavior.
3. **The registry reuses published source contracts.** `source_registry.json` stores a stable `source_id`, display name and identity fields. A new monthly file can therefore be appended to `insurance_sales` using its recorded `品牌 + 1-子车型` contract without being treated as a new business source.
4. **HITL is authoritative.** The user confirms the baseline, fields and whether each noncanonical input is `append` or `new`. The resolved request must preserve `source_id`, `source_name` and `source_mode` verbatim through its API DTO before validating the rule. A registered source is valid only with `append`; an unknown source is valid only with `new`.

## Output record

Use a portable JSONL or JSON crosswalk. A record must retain raw bindings and audit evidence.

```json
{
  "entity": {"entity_key": "brand::series", "canonical_brand": "品牌", "canonical_series": "车系"},
  "bindings": [{"source_ref": "table_asset:tbl_xxx", "key_fields": {"品牌": "品牌", "1-子车型": "车系"}}],
  "resolution": {"status": "auto_matched", "join_eligible": true, "method": "normalized_exact", "confidence": 1.0, "evidence": ["..."]}
}
```

When a canonical source of truth is declared, the primary `records` array contains only canonical entities. Put source `candidate`, `unmatched`, and rejected review entries in a separate `source_diagnostics` array (and, if exported, a separate diagnostics CSV). Runtime lookup may index both arrays, but canonical record counts and the primary Crosswalk CSV must equal the canonical-source grain.

Allowed statuses: `canonical_only`, `auto_matched`, `accepted`, `candidate`, `rejected`, `unmatched`, `inactive`.

## Intermediate table

When a lookup must be queried repeatedly, a builder may materialize an `analytics_dim_<dimension_id>` table. It must contain the canonical key, canonical attributes, source binding fingerprint, status, confidence, build version and refreshed timestamp. The portable crosswalk remains the source for migration and audit.

## Verification

Always calculate:

- Distinct source keys, resolved keys, eligible keys, candidate keys, unmatched keys and collisions.
- Weighted coverage when a metric column is present.
- Whether both sources have bindings to the same canonical key.
- Delta since the previous artifact when a prior build exists.

Never silently drop unmatched values. Formal results must state the eligible coverage.

## Versioned publication

The builder output is a generated baseline, not the mutable runtime state:

```text
generated_crosswalk.json + manual_overrides.json = active_crosswalk.json
```

`manual_overrides.json` stores operations keyed by `source_ref + source_key`: `bind` moves a source record to a selected `entity_key`; `exclude` records an explicit non-association. A new build replaces only the generated baseline, then replays the overrides. `source_registry.json` records the reusable source identity mapping; a new monthly sales file can append the registered sales source, while an order table registers a separate source without extending the canonical entity schema.
