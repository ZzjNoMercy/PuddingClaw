# Frontmatter effects

Frontmatter is a Backend-consumed machine projection inside the same authoritative Markdown file. The user normally reviews its effect summary, not YAML.

## Measure fields in the first vertical

| Field | Actual effect | Maintenance rule |
| --- | --- | --- |
| `formatter` | Selects the semantic asset loader | Backend may fill from the explicit target path |
| `type` | Controls runtime asset routing; relations are excluded from free-text retrieval and grains require explicit hits | Backend may fill a missing value from the explicit Measure path, but must reject conflicts |
| `name` | Strong retrieval and display signal | Agent proposal; show in body title/effect summary |
| `description` | Catalogue summary and retrieval tokens | Agent proposal; body must explain the same meaning |
| `aliases` | Strong alternate retrieval phrases | Agent proposal; show effect summary |
| `tags` | Retrieval and catalogue filtering hints | Agent proposal; show effect summary |
| `version` | Display/export/audit metadata, not concurrency protection | Agent proposal; CAS uses file digest |

Unknown frontmatter also contributes to the semantic runtime fingerprint. Do not add decorative or speculative fields merely because a Schema mentions them.

## Business-effect rule

Never derive business-effective frontmatter from prose with a deterministic parser or an LLM and then silently publish it. The Agent may propose it from the conversation and evidence; the Backend validates shape and conflicts. The body must communicate any field that changes selection, calculation, joins, grain, filters, or guardrails.

Use `inspect_frontmatter_contract` when that tool becomes available for a new asset kind. Until then, rely only on fields documented here and existing Registry contracts.
