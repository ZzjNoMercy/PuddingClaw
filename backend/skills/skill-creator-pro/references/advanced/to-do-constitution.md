# To-Do Constitution

Convert the whitepaper To-Do constitution into action.

## Core Whitepaper Rules
- Write YAML descriptions for routing quality, not style.
- Prefer surgical edits over wholesale rewrites when improving an existing skill.
- Move heavy reference material into `references/`.
- Recon the current skill before editing it.
- Require an automated or explicit validation loop before declaring success.
- Define the initial interaction protocol when the skill is collaborative.
- Define phase exits or completion gates when the workflow is staged.
- Use few-shot anchors only when they materially improve output quality.
- For large reference sets, prefer targeted lookup over bulk loading.

## Execution Checklist: Upgrade an Existing Skill
1. Review the skill before editing.
2. Fix invalid frontmatter and structural failures first.
3. Choose the primary paradigm.
4. Decide whether the skill is light, medium, or heavy.
5. Archive loose backups before heavy restructuring.
6. Slim `SKILL.md` before adding new detail.
7. Add or update `agents/openai.yaml`.
8. Add scripts only for repeated, fragile, deterministic actions.
9. Run review and validator again.
10. Stop when high/medium issues are gone and readability is still intact.

## Execution Checklist: Create a New Skill
1. Define trigger and non-trigger examples.
2. Choose the primary paradigm.
3. Choose only the required modules.
4. Decide what belongs in `references/`, `scripts/`, and `assets/`.
5. Write the shortest control layer that still routes correctly.
6. Add validation before calling the skill done.

## Stop Conditions
- Stop adding detail when the control layer is clear and the resources are discoverable.
- Stop refactoring when remaining findings are low-value polish and further changes would reduce readability.
