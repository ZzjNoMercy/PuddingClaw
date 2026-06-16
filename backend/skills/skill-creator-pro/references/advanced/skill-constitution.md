# Skill Constitution

Use this file as the governing checklist for any serious skill.

## Constitutional Rules

1. Route before you explain.
2. Recon before you act when the environment is uncertain.
3. Script repeated fragile work when possible.
4. Keep `SKILL.md` small enough to scan quickly.
5. Move optional or heavy detail into `references/`.
6. Treat `assets/` as output material, not reading material.
7. Define a decision tree when multiple paths exist.
8. Define validation before claiming completion.
9. Avoid broad trigger wording that overlaps many skills.
10. Expand only after real usage proves the need.

## Mandatory Sections

A default skill should include:
- Goal
- Workflow
- Decision Tree
- Constraints
- Validation
- Resources

If you remove one of these, have a reason tied to the skill architecture.

## Trigger Law

The frontmatter description must explain:
- what the skill does
- when to use it
- what kinds of requests should trigger it
- what nearby requests should not be conflated with it

## Main File Law

`SKILL.md` is a routing layer, not a textbook.

Keep in the main file:
- intent
- flow
- branch selection
- constraints
- validation
- resource routing

Move out of the main file:
- long domain background
- large config blocks
- deep examples
- optional branch detail

## Validation Law

Every skill must define:
- required checks
- success criteria
- first failure checks
- evidence required before saying the task is complete

## Anti-Guessing Law

If the environment is not verified, the skill must not guess:
- file state
- UI state
- API state
- system state
- data shape

Inspect first.
