# Evaluation Handoff

## When to Hand Off
Hand off to `skill-benchmark` after create or upgrade work when the user needs evidence that the skill is effective, not just structurally sound.

## Use `skill-benchmark` For
- baseline-vs-with-skill checks
- quick confidence checks after a description or routing change
- formal benchmark runs across representative prompts
- comparison between two skill versions or a local skill and an external candidate
- trend review after repeated upgrades

Start with `skill-benchmark/assets/benchmark-prompts-template.json` when you need a minimal prompt set after create or upgrade work. Replace placeholders with realistic should-trigger, should-not-trigger, edge, and comparison prompts for the target skill.

## Boundary Rule
`skill-creator-pro` designs and upgrades skills. `skill-benchmark` measures whether those changes produce stable gains in triggering, routing, and task outcomes.
