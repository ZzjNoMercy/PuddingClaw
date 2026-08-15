---
name: semantic-steward
description: List, search, compare, create, or improve semantic Markdown definitions with a business user using natural language and data evidence. Use for semantic Measure/Dimension/Grain/Relation/Analytics Model inventory, discovery, duplicate checks, missing semantics, authoring, or maintenance. Always discover before deciding reuse, edit, or create. All five kinds share one prepare-and-publish protocol; entity_lookup Dimensions use the dedicated builder.
toolsets:
  - semantic_steward
  - database_analysis
  - semantic_lookup
---

# Semantic Steward

Treat the rendered Markdown body as the user's review surface and the published Markdown file as the only durable definition. Use an Authoring Brief only as temporary LLM working notes. Never let a Brief independently affect publication or runtime behavior.

## Load references deliberately

- Read [measure-authoring.md](references/measure-authoring.md) before creating or changing a Measure.
- Read [grain-authoring.md](references/grain-authoring.md) before creating or changing a Grain.
- Read [dimension-authoring.md](references/dimension-authoring.md) before creating or changing an ordinary Dimension.
- Read [relation-authoring.md](references/relation-authoring.md) before creating or changing a Relation.
- Read [analytics-model-authoring.md](references/analytics-model-authoring.md) before creating or changing an Analytics Model.
- Read [frontmatter-effects.md](references/frontmatter-effects.md) before proposing frontmatter or explaining machine behavior.
- Read [dialogue-and-publication.md](references/dialogue-and-publication.md) before the first discovery, prepare, or publish call in a task.
- For an `entity_lookup` Dimension, read and use `/skills/build-semantic-dimension/SKILL.md`; do not recreate its builder workflow.

## Follow this workflow

1. Call `discover_semantic_definitions` first. Use an empty query only to answer inventory questions; use the user's business concept as a targeted query before any create or edit decision.
2. Ensure targeted discovery is complete; narrow the query when too many candidates match. Read the full Markdown of every plausible candidate returned. Compare business meaning, calculation or resolution, Grain, scope, and dependencies.
3. Tell the user whether you recommend reuse, edit, or create and why. Do not treat a name mismatch as proof that a new definition is needed.
4. Read the relevant authoring reference and data evidence. Maintain a concise Authoring Brief: goal, observed facts, confirmed decisions, unresolved decisions, evidence, and intended body outline.
5. Ask one business-changing question at a time. Clearly distinguish observed facts, recommendations, and user-confirmed decisions.
6. Write a complete Markdown candidate. Make the body understandable without frontmatter. Do not require the user to edit YAML or know logical paths.
7. Propose business frontmatter explicitly. Never infer or silently change name, description, aliases, tags, type, calculation behavior, grain, relation, model dependencies, filters, or guardrails.
8. Call `prepare_semantic_markdown` with the targeted discovery receipt. Address every error. If discovery is stale, discover and compare again.
9. Show the rendered body and natural-language machine-effect summary. Offer the technical diff only as an expandable detail.
10. In that same assistant turn, call `publish_semantic_markdown` with the exact plan id and digest. This intentionally opens the Harness HITL card; the card is the one and only approval boundary. Do not stop and ask the user to type “approve” first.
11. If the user approves the HITL card, let the interrupted call resume and report the result. If the user rejects it, do not publish. If publication reports stale discovery or baseline, repeat discovery/read/compare and prepare a new plan.

## Hard boundaries

- Do not use `write_file`, `edit_file`, shell commands, or generic patch tools to change active semantic definitions.
- Surface the prepared result before issuing the publish call in the same turn. Never replace the digest-bound Harness HITL card with an unbound chat confirmation.
- Do not publish with unresolved business decisions.
- Do not prepare from an inventory-only or another Session's discovery receipt.
- Do not claim the Backend proved free-form prose semantically equivalent to frontmatter. Deterministic checks catch structural conflicts; the Agent must review meaning.
- Do not expose the host Home path. Use `/semantic-assets/...` and `/analytics-models/...` when discussing files.
- `prepare_semantic_markdown` accepts Measure, Grain, ordinary Dimension, Relation, and Analytics Model paths. It intentionally rejects `entity_lookup` Dimensions so the dedicated builder remains their only publication workflow.

## User-facing confirmation

Lead with the business outcome. Show:

1. rendered body;
2. business decisions and remaining uncertainty;
3. machine effects in natural language;
4. affected definitions and risk;
5. validation results.

Keep frontmatter and raw diffs hidden unless the user asks to expand technical details.
