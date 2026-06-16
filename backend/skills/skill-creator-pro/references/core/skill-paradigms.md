# Skill Paradigms

Use the whitepaper paradigm system before restructuring a skill. Pick the primary paradigm first, then choose any secondary traits only if they materially change the structure.

## Navigator
- Main job: route the agent to the right information.
- Use for: large knowledge domains, branching documentation, policy or schema lookup.
- Keep in `SKILL.md`: trigger boundary, routing logic, decision tree, minimal validation.
- Push down: domain detail into `references/`.
- Watch for: encyclopedic `SKILL.md` files and hidden branches.

## Operator
- Main job: execute a toolchain reliably.
- Use for: repeated file operations, data transforms, batch tools, deterministic pipelines.
- Keep in `SKILL.md`: operation boundary, input requirements, failure checks, tool routing.
- Push down: repeated or fragile actions into `scripts/`.
- Watch for: retyping the same command logic in prose.

## Partner
- Main job: structure human-agent collaboration.
- Use for: co-design, staged approval, iterative drafting, guided clarification.
- Keep in `SKILL.md`: opening protocol, confirmation points, stage exits.
- Push down: detailed prompts, examples, optional branches into `references/`.
- Watch for: missing confirmation checkpoints and improvised starts.

## Scout
- Main job: inspect the environment before acting.
- Use for: unknown repos, risky edits, investigation-heavy work, context-sensitive systems.
- Keep in `SKILL.md`: recon-first workflow, safety rules, first inspection commands, stop conditions.
- Push down: environment-specific detail into `references/`.
- Watch for: guessing structure, editing before inspection, skipping validation.

## Architect
- Main job: produce reusable systems, frameworks, or standards.
- Use for: starter templates, governance systems, specification builders, courseware systems.
- Keep in `SKILL.md`: top-down workflow, phase structure, design gates, validation standard.
- Push down: templates, scorecards, detailed methodology, examples.
- Watch for: writing a loose tutorial instead of a system-design control layer.

## Orchestrator
- Main job: coordinate multiple tools, agents, or phases.
- Use for: multi-agent workflows, multi-stage pipelines, mode-based systems, handoff-heavy skills.
- Keep in `SKILL.md`: mode selection, orchestration route, handoff logic, resume rules, synthesis checkpoints.
- Push down: role detail, phase-specific deep logic, long prompts, file protocols.
- Watch for: giant `SKILL.md` files that carry every agent detail inline.

## Hybrid Patterns
- Many strong skills are hybrid, but only one paradigm should dominate the control layer.
- Choose the paradigm that decides the structure of `SKILL.md`.
- Secondary paradigms may affect specific modules:
  - `Scout` adds recon-first rules.
  - `Partner` adds confirmation checkpoints.
  - `Operator` adds scripts.
  - `Orchestrator` adds modes and handoffs.

## Paradigm Selection Checklist
1. What is the main repeated problem this skill stabilizes?
2. Is the main risk wrong information, wrong execution, wrong collaboration, wrong assumptions, weak system design, or weak coordination?
3. Which paradigm determines the structure of the control layer?
4. Which details should move to `references/`, `scripts/`, or `assets/` because of that paradigm?
5. What anti-pattern appears if the skill is treated as the wrong paradigm?
