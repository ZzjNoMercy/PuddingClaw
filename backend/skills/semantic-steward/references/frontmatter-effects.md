# Frontmatter effects

Frontmatter is a Backend-consumed machine projection inside the same authoritative Markdown file. The user normally reviews its effect summary, not YAML.

## Common fields

| Field | Actual effect | Maintenance rule |
| --- | --- | --- |
| `formatter` | Selects the semantic asset loader | Backend may fill from the explicit target path |
| `type` | Controls runtime asset routing; relations are excluded from free-text retrieval and grains require explicit hits | Backend may fill a missing value from the explicit Measure path, but must reject conflicts |
| `name` | Strong retrieval and display signal | Agent proposal; show in body title/effect summary |
| `description` | Catalogue summary and retrieval tokens | Agent proposal; body must explain the same meaning |
| `aliases` | Strong alternate retrieval phrases | Agent proposal; show effect summary |
| `tags` | Retrieval and catalogue filtering hints | Agent proposal; show effect summary |
| `version` | Display/export/audit metadata, not concurrency protection | Agent proposal; CAS uses file digest |

## Kind-specific runtime fields

| Kind | Fields | Actual effect |
| --- | --- | --- |
| Dimension | `resolution_mode`, `resolution` | Select value resolution and bind fields or calendar rules. `entity_lookup` must use the dedicated builder. |
| Relation | `relation_type`, `relation` | Select endpoints, join keys, cardinality, join type, grain mapping, and rules. |
| Analytics Model | `id`, `data_assets`, `semantic_assets`, `asset_relations`, `guardrails`, `templates`, `default_template`, `references` | Set model identity, available scope, approved graph, SQL controls, output resources, and model-local context documents. `id` is filled from the explicit target directory; all other business fields are Agent proposals. |

Grain has no additional structured runtime block today. Its identity, deduplication, and rollup contract remains in the authoritative Markdown body.

Unknown frontmatter also contributes to the semantic runtime fingerprint. Do not add decorative or speculative fields merely because a Schema mentions them.

## Business-effect rule

Never derive business-effective frontmatter from prose with a deterministic parser or an LLM and then silently publish it. The Agent may propose it from the conversation and evidence; the Backend validates shape and conflicts. The body must communicate any field that changes selection, calculation, joins, grain, filters, or guardrails.

Use the per-kind authoring reference and existing Registry contracts. Do not add fields merely to make all five kinds look structurally uniform.
