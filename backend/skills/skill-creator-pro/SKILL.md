---
name: skill-creator-pro
description: Design, create, review, and iteratively improve high-quality AI skills with strong trigger definitions, progressive disclosure, reusable scripts/references/assets planning, validation rules, and anti-pattern avoidance. Use when asked to create a new skill, upgrade an existing skill, turn a repeated workflow into a reusable skill, review skill quality, or define skill design best practices.
---

# Skill Creator Pro

## Goal
Design and refine production-grade skills that trigger correctly, stay lean in context, execute predictably, and improve after real usage. Automatically recommend the best design paradigm for each new skill.

## Operating Modes
- Create: recommend paradigm → define boundary → plan resources → generate structure → validate.
- Review: inspect an existing skill for structural defects, content-quality issues, routing weaknesses, and missing validation.
- Upgrade: review first, then apply the smallest changes that materially improve trigger quality, clarity, reuse, and verification.

## Create Workflow (Five Phases)

**Phase 0 — Paradigm Selection**
Run `scripts/paradigm_recommender.py --description "user's description"` to get a paradigm recommendation.
If confidence ≥ 0.7, present the recommendation and ask user to confirm.
If confidence < 0.7, ask clarifying questions before recommending.
Read `references/paradigms/{paradigm}.md` for the confirmed paradigm's best practices.

**Phase 1 — Boundary Definition**
Answer four questions before writing anything:
- What repeated problem does this skill solve?
- What user requests should trigger it?
- What nearby requests should not trigger it?
- What counts as success?

**Phase 2 — Resource Planning**
Based on paradigm, decide what goes into scripts/, references/, and assets/.
Use the paradigm-specific resource heuristics from `references/paradigms/{paradigm}.md`.

**Phase 3 — Generate Skill**
Scaffold with `scripts/init_skill_pro.py <name> --path <dir> --paradigm <paradigm>`.
Then replace all template wording with real content from Phase 1 and 2.

**Phase 4 — Validate**
Run `scripts/review_skill.py <path>`. Fix any high-severity findings.
Test at least one realistic usage path end to end.

## Review and Upgrade Workflow
1. Run `scripts/review_skill.py <path/to/skill>`.
2. Separate findings: hard structural failures → routing weaknesses → content issues → polish.
3. Run `scripts/paradigm_recommender.py --skill-path <path>` to check paradigm alignment.
4. Fix trigger and routing before expanding scope.
5. Hand off to `skill-benchmark` when user wants effectiveness evidence.

## Paradigm Quick Reference

| Paradigm | Core Job | Main Risk | Key Directory | Share |
|----------|----------|-----------|---------------|-------|
| Operator | Execute toolchain reliably | Execution failure | scripts/ | 28% |
| Navigator | Route to right information | Wrong guidance | references/ | 19% |
| Architect | Produce reusable systems | Non-reusable output | assets/ | 17% |
| Partner | Structure collaboration | Misunderstood intent | references/ | 14% |
| Orchestrator | Coordinate multi-tool/agent | Coordination chaos | references/ | 13% |
| Scout | Inspect before acting | Wrong assumptions | references/ | 6% |
| Philosopher | Establish governing principles | Inconsistent behavior | references/ | 3% |

## Paradigm Decision Tree
Identify the main risk, then match:
- Execution fails or results inconsistent → **Operator**
- User gets wrong information → **Navigator**
- Output is not reusable or systematic → **Architect**
- User intent misunderstood → **Partner**
- Multi-tool coordination breaks → **Orchestrator**
- Acting on wrong assumptions → **Scout**
- Behavior lacks principled consistency → **Philosopher**

When unclear, ask: Does it need scripts for determinism? (Operator) Large knowledge base? (Navigator) User confirmations? (Partner) Environment recon? (Scout) Reusable templates? (Architect) Multi-agent handoff? (Orchestrator) Constitutional rules? (Philosopher)

## Boundary First
Before creating or revising a skill, answer:
- What repeated problem does this skill solve?
- What user requests should trigger it?
- What nearby requests should not trigger it?
- What counts as success for the skill user?
If answers are fuzzy, use `references/templates/request-templates.md` to collect missing info.

## Design Rules
- Prefer narrow and strong over broad and vague.
- Choose the paradigm before choosing the structure.
- Frontmatter = Routing Layer. `SKILL.md` = Control Layer. `scripts/ references/ assets/` = Execution Support.
- Keep `SKILL.md` focused on workflow, decision points, constraints, validation, and resource routing.
- Do not duplicate information across `SKILL.md` and `references/`.
- Do not explain basics the model already knows.
- Do not create empty directories.

## Trigger Quality
- `name`: lowercase hyphen-case.
- `description`: capability + invocation context + representative tasks/objects.
- Include "Use when..." phrasing.
- Keep trigger logic in frontmatter, not body.

## Recommended `SKILL.md` Shape
```md
---
name: my-skill
description: [capability + "Use when..." + trigger contexts]
---
# My Skill
## Goal
## Workflow
## Decision Tree
## Constraints
## Validation
## Resources
```

## Validation Standard
- frontmatter exists and is valid YAML
- `name` and `description` are present and correct
- directory structure matches paradigm needs
- `agents/openai.yaml` reflects the skill
- at least one realistic usage path works end to end

## Anti-Patterns
- Writing a tutorial instead of an execution guide
- Making the skill broad before making it strong
- Mixing trigger rules into body instead of frontmatter
- Copying large reference content into `SKILL.md`
- Creating empty or decorative directories
- Declaring success without a validation path
- Choosing wrong paradigm (e.g., treating Operator as Navigator)

## Resources
- `scripts/paradigm_recommender.py`: recommend paradigm from description or existing skill
- `scripts/init_skill_pro.py`: scaffold skill structure with paradigm-specific templates
- `scripts/review_skill.py`: review an existing skill for structural and content issues
- `references/paradigms/`: seven paradigm best-practice files (operator, navigator, architect, partner, orchestrator, scout, philosopher)
- `references/paradigms/hybrid-patterns.md`: when and how to combine paradigms
- `references/core/design-playbook.md`: boundary, scope, and build order
- `references/core/not-to-do-red-lines.md`: anti-patterns and failure classes
- `references/core/examples.md`: strong and weak skill patterns
- `references/advanced/constitution.md`: 10 constitutional rules
- `references/advanced/content-review.md`: content quality and routing strength rubric
- `references/advanced/remediation-playbook.md`: findings to fixes mapping
- `references/advanced/evaluation-handoff.md`: when to hand off to skill-benchmark
- `references/templates/request-templates.md`: collect missing info for new skills
- `references/validation/checklists.md`: pre-release checklists
- `references/validation/skill-review-scorecard.md`: five-dimension scoring
- `assets/skill-templates/`: paradigm-specific SKILL.md starter templates
