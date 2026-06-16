---
name: skill-creator-pro
description: Design, create, review, and iteratively improve high-quality AI skills with strong trigger definitions, progressive disclosure, reusable scripts/references/assets planning, validation rules, and anti-pattern avoidance. Use when asked to create a new skill, upgrade an existing skill, turn a repeated workflow into a reusable skill, review skill quality, or define skill design best practices.
---

# Skill Creator Pro

## Goal
Design and refine production-grade skills that trigger correctly, stay lean in context, execute predictably, and improve after real usage.

## Operating Modes
Use one of these paths based on the request:
- Create: define a new skill boundary, choose resources, generate a strong starter structure, then customize it.
- Review: inspect an existing skill for structural defects, content-quality issues, routing weaknesses, and missing validation.
- Upgrade: review first, then apply the smallest changes that materially improve trigger quality, clarity, reuse, and verification.

## Core Workflow
1. Define the skill boundary before writing or changing files.
2. Collect trigger examples and nearby non-trigger examples.
3. Identify reusable resources: scripts, references, and assets.
4. Create or update the skill structure.
5. Keep `SKILL.md` short and route-oriented.
6. Generate or update `agents/openai.yaml` to match the skill.
7. Validate the structure and frontmatter.
8. Test at least one realistic usage path and iterate.

## Boundary First
Before creating or revising a skill, answer these questions:
- What repeated problem does this skill solve?
- What user requests should trigger it?
- What nearby requests should not trigger it?
- What counts as success for the skill user?

If those answers are fuzzy, tighten the scope before writing anything.

## Review First For Existing Skills
When the request is about upgrading or improving an existing skill, review before editing.

Run:

```bash
python3 /Users/mac/.claude/skills/skill-creator-pro/scripts/review_skill.py <path/to/skill>
```

Use the review output to separate:
- hard structural failures
- routing or trigger weaknesses
- content-quality issues
- low-priority polish

Do not expand scope until the trigger and main workflow are coherent.

## Design Rules
- Prefer narrow and strong over broad and vague.
- Keep `SKILL.md` focused on workflow, decision points, constraints, validation, and resource routing.
- Put detailed knowledge into `references/`.
- Put deterministic or repeated operations into `scripts/`.
- Put templates and output materials into `assets/`.
- Do not duplicate the same information across `SKILL.md` and `references/`.
- Do not explain basics the model likely already knows.

## Trigger Quality
Write frontmatter for routing, not for style.
- `name` must be lowercase hyphen-case.
- `description` must explain both capability and invocation context.
- Include representative tasks, objects, or file types when relevant.
- Keep trigger logic in frontmatter, not in a separate "When to use" section.
- Prefer wording that implies the repeated workflow, not every adjacent workflow.

## Recommended `SKILL.md` Shape
Use this structure unless the skill clearly benefits from a different shape:

```md
---
name: my-skill
description: [what it does + invocation context + common trigger contexts]
---

# My Skill

## Goal
[1-2 sentence mission]

## Workflow
1. Confirm inputs.
2. Classify the request.
3. Route to scripts, references, or assets.
4. Execute the smallest viable path.
5. Validate output.
6. Report results and risks.

## Decision Tree
- If A, run `scripts/a.py`
- If B, read `references/b.md`
- If C, use `assets/template-c/`

## Constraints
- Non-negotiable rules
- Preservation requirements
- Cases that require confirmation

## Validation
- Required checks
- Success criteria
- First failure checks

## Resources
- `scripts/...`: when to run
- `references/...`: when to read
- `assets/...`: when to use
```

## Resource Selection Heuristics
Use `scripts/` when the task is repeated, fragile, or should be deterministic.
Use `references/` when the detail is too long for `SKILL.md` or only relevant in some branches.
Use `assets/` when files are used in outputs rather than as reading material.

If a skill has no real need for one of these directories, do not create it just to look complete.

## Progressive Disclosure
Keep metadata precise and compact.
Keep `SKILL.md` under control and easy to scan.
Load detailed references only when needed.

For skills with multiple variants, keep the selection logic in `SKILL.md` and move variant details into separate reference files.

## Validation Standard
At minimum, validate:
- frontmatter exists and is valid YAML
- `name` and `description` are present and correct
- the directory structure matches actual needs
- `agents/openai.yaml` still reflects the skill
- at least one realistic usage path can be followed without ambiguity

Use the official validator when available:

```bash
python3 -B /Users/mac/.codex/skills/.system/skill-creator/scripts/quick_validate.py <path/to/skill>
```

## Action Paths
- For new skills, scaffold first with:

```bash
python3 /Users/mac/.claude/skills/skill-creator-pro/scripts/init_skill_pro.py <skill-name> --path <parent-dir> --resources references
```

  Then replace all template wording before use.
- For existing skills, run `scripts/review_skill.py` first and fix trigger quality, routing, and validation before polishing details.

## Anti-Patterns
Avoid these mistakes:
- Writing a tutorial instead of an execution guide
- Making the skill broad before making it strong
- Mixing trigger rules into the body instead of frontmatter
- Copying large reference content into `SKILL.md`
- Creating empty or decorative directories
- Declaring success without a validation path

## Resources
- `scripts/review_skill.py`: review an existing skill before changing it
- `scripts/init_skill_pro.py`: scaffold a stronger starter layout for a new skill
- `references/design-playbook.md`: read when defining boundary, scope, and build order
- `references/checklists.md`: read before declaring a skill ready
- `references/examples.md`: read when comparing strong and weak skill patterns
- `references/content-review.md`: read when judging content quality and routing strength
- `references/remediation-playbook.md`: read when mapping findings to concrete fixes
- `assets/skill-template/`: use when you need a review-ready starter template
