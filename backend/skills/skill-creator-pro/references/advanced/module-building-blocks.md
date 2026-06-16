# Module Building Blocks

Use the whitepaper building blocks to choose what belongs in the control layer.

## Whitepaper Building Blocks

### Identity
- Purpose: define who the skill is and when it should trigger.
- Main carrier: frontmatter.
- Rule: frontmatter is a router, not decoration.
- Failure mode: capability-only descriptions with no invocation context.

### Interaction
- Purpose: define how the skill starts when the task is collaborative or staged.
- Main carrier: opening steps in `Workflow`.
- Rule: define the initial interaction protocol when the skill needs clarification or approvals.
- Failure mode: every run starts differently because the agent improvises.

### Decision
- Purpose: define explicit branch rules.
- Main carrier: `Decision Tree`.
- Rule: if multiple paths exist, make the path selection visible.
- Failure mode: branches are buried in paragraphs.

### Doctrine
- Purpose: keep only principles that materially change behavior.
- Main carrier: `Constraints` and a small number of workflow rules.
- Rule: remove manifesto-style prose that does not change execution.
- Failure mode: value statements that add noise but do not alter decisions.

## Three-Layer Mapping

### Routing Layer
- Carrier: `name + description`
- Job: get recalled for the right task.

### Control Layer
- Carrier: `SKILL.md`
- Job: decide what the agent does next.

### Execution Support
- Carrier: `scripts/`, `references/`, `assets/`
- Job: make execution lean, reusable, and deterministic.

## Core Modules
- `Goal`: repeated problem being stabilized.
- `Workflow`: the main execution path.
- `Decision Tree`: explicit branch rules.
- `Constraints`: non-negotiable behavioral rules.
- `Validation`: required checks, success criteria, first failure checks.
- `Resources`: when to run, read, or use support files.

## Optional Modules
Use only when the paradigm or task actually needs them.
- `Interaction` opening protocol
- `Resume` or workspace state
- compatibility override notes
- archive or upgrade hooks
- review hooks and scorecards

## Module Selection Rules
- Start from paradigm, not from a template checklist.
- Keep the control layer short enough to scan.
- Move long detail into `references/`.
- Move repeated fragile actions into `scripts/`.
- Do not add optional modules just to look complete.

## Bad Module Patterns
- Huge `SKILL.md` plus huge `references/` that repeat the same content.
- Decision logic hidden inside `Workflow` prose.
- Validation that says `verify` but gives no checks.
- Compatibility or historical notes placed before frontmatter.
