---
name: example-skill
description: [State what this skill does, when to use it, and the kinds of requests or files that should trigger it.]
---

# Example Skill

## Goal
[State the repeated problem this skill stabilizes.]

## Workflow
1. Confirm the task boundary and the required inputs.
2. Classify the request into the correct branch.
3. Route to the right script, reference, or asset.
4. Execute the smallest viable path.
5. Validate the result.
6. Report outcome, risks, and next steps.

## Decision Tree
- If the request is [type A], run `scripts/...`
- If the request is [type B], read `references/...`
- If the request is [type C], use `assets/...`

## Constraints
- Preserve critical state or formatting.
- Do not guess missing critical input.
- Ask for confirmation before risky actions.

## Validation
- Required checks:
- Success criteria:
- First failure checks:

## Resources
- `scripts/...`: when to run
- `references/...`: when to read
- `assets/...`: when to use
