---
name: semantic-steward
description: Guide a business user through creating or improving semantic Markdown definitions from natural language and data evidence. Use for semantic measures, dimensions, grains, relations, analytics models, duplicate definitions, missing semantics, or model maintenance. The current publish vertical supports Measure Markdown; inspect and explain other kinds but do not publish them through Measure-only tools.
toolsets:
  - semantic_steward
  - database_analysis
  - semantic_lookup
---

# Semantic Steward

Treat the rendered Markdown body as the user's review surface and the published Markdown file as the only durable definition. Use an Authoring Brief only as temporary LLM working notes. Never let a Brief independently affect publication or runtime behavior.

## Load references deliberately

- Read [measure-authoring.md](references/measure-authoring.md) before creating or changing a Measure.
- Read [frontmatter-effects.md](references/frontmatter-effects.md) before proposing frontmatter or explaining machine behavior.
- Read [dialogue-and-publication.md](references/dialogue-and-publication.md) before the first prepare or publish call in a task.
- For an `entity_lookup` Dimension, read and use `/skills/build-semantic-dimension/SKILL.md`; do not recreate its builder workflow.

## Follow this workflow

1. Read the published Markdown and relevant references when editing an existing definition.
2. Inspect existing definitions and data evidence before asking the user. Reuse an existing definition when its business meaning already matches.
3. Maintain a concise Authoring Brief in reasoning or the prepare call: goal, observed facts, confirmed decisions, unresolved decisions, evidence, and intended body outline.
4. Ask one business-changing question at a time. Clearly distinguish observed facts, recommendations, and user-confirmed decisions.
5. Write a complete Markdown candidate. Make the body understandable without frontmatter. Do not require the user to edit YAML or know logical paths.
6. Propose business frontmatter explicitly. Never infer or silently change name, description, aliases, tags, type, calculation behavior, grain, relation, model dependencies, filters, or guardrails.
7. Call `prepare_semantic_markdown`. Address every error. Treat warnings as review prompts, not facts to hide.
8. Show the rendered body and natural-language machine-effect summary. Offer the technical diff only as an expandable detail.
9. Stop and wait for explicit approval of that exact `plan_digest`.
10. Only after approval, call `publish_semantic_markdown` with the exact plan id and digest. If it reports a stale baseline, reread the Markdown and prepare a new plan.

## Hard boundaries

- Do not use `write_file`, `edit_file`, shell commands, or generic patch tools to change active semantic definitions.
- Do not call prepare and publish back-to-back before the user has reviewed the prepared result.
- Do not publish with unresolved business decisions.
- Do not claim the Backend proved free-form prose semantically equivalent to frontmatter. Deterministic checks catch structural conflicts; the Agent must review meaning.
- Do not expose the host Home path. Use `/semantic-assets/...` and `/analytics-models/...` when discussing files.
- The current `prepare_semantic_markdown` vertical accepts only `semantic-assets/measures/<id>/measure.md`. Explain this limitation for other kinds instead of forcing them through the Measure path.

## User-facing confirmation

Lead with the business outcome. Show:

1. rendered body;
2. business decisions and remaining uncertainty;
3. machine effects in natural language;
4. affected definitions and risk;
5. validation results.

Keep frontmatter and raw diffs hidden unless the user asks to expand technical details.
