---
name: sql-guardrail-designer
description: Use when the user wants to create, draft, review, or save SQL guardrails for PuddingClaw database QA, especially constraints about NL2SQL correctness, forbidden SQL patterns, required tables, GROUP BY grain, semantic asset enforcement, or slow-query guardrails. Generates Markdown guardrail assets under /sql-guardrails and requires explicit user confirmation before saving active rules.
---

# SQL Guardrail Designer

## Goal

Create reviewable SQL guardrail document assets for PuddingClaw's database QA system.

Guardrails are stored as Markdown files with YAML frontmatter:

```text
/sql-guardrails/
  drafts/{id}/guardrail.md
  rules/{id}/guardrail.md
```

The frontmatter is compiled by the backend into executable SQL guardrail rules. The Markdown body explains the business constraint, forbidden SQL shape, recommended SQL shape, scope, and risks.

## Workflow

1. Understand the user's constraint.
2. Choose the closest guardrail type from `references/rule-types.md`.
3. Draft one `guardrail.md` using `references/guardrail-template.md`.
4. Show the draft to the user for review.
5. If the user has not explicitly confirmed creation, write only to `/sql-guardrails/drafts/{id}/guardrail.md` or just show the draft.
6. Only after the user explicitly says "确认", "创建", "保存", "没问题", or equivalent, write the final file to `/sql-guardrails/rules/{id}/guardrail.md`.
7. Remind the user when a matching measure/dimension/grain Markdown asset should also be updated.

## Confirmation Rules

- Do not create or overwrite `/sql-guardrails/rules/**/guardrail.md` on the first user request.
- Always show the proposed guardrail before creating an active rule.
- Drafts can be written to `/sql-guardrails/drafts/**/guardrail.md`.
- Active rules require explicit confirmation.
- If the action is `block`, call out the operational risk before saving.

## Output Format

When drafting, respond with:

```markdown
## Guardrail Draft

<guardrail.md content>

## Why This Rule

- ...

## Semantic Asset Sync

- ...

## Risks

- ...
```

When saving, write the file and respond with:

```markdown
已创建 SQL 守卫：<name>

路径：/sql-guardrails/rules/<id>/guardrail.md
生效条件：...
下一步：刷新 SQL 守卫或重新发起数据库问数验证。
```

## Constraints

- Use `formatter: sql-guardrail`.
- One `guardrail.md` contains exactly one rule.
- Keep executable fields in YAML frontmatter. Do not rely on Markdown body for execution.
- Use stable snake_case IDs.
- Use table scope only for routed table matching. Do not put data-source filters in the guardrail.
- Prefer `rewrite` over `block` unless the user explicitly wants hard blocking.
- If the guardrail depends on a measure/dimension/grain, include its semantic asset id in `scope.semantic_assets`.

## Resources

- `references/rule-types.md`: available backend rule types and parameters.
- `references/guardrail-template.md`: canonical `guardrail.md` template.

