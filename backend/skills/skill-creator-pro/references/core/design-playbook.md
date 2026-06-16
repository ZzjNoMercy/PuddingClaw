# Design Playbook

## Top-Down Build Order
1. Define the repeated problem.
2. List trigger examples.
3. List non-trigger neighbors.
4. Define success criteria.
5. Identify reusable scripts, references, and assets.
6. Build the narrowest strong version first.
7. Add validation before adding breadth.
8. Iterate after real usage.

## Skill Worthiness Test
A task is usually worth turning into a skill when it is:
- repeated
- error-prone
- context-heavy
- organization-specific
- scriptable or template-friendly

## Strong Description Formula
Use:
- capability
- typical user request patterns
- relevant objects or file types
- task boundary

Example:

`Create and maintain reusable local automation skills for repeated engineering workflows. Use when asked to turn a recurring process, tool usage pattern, or domain workflow into a reusable skill with SKILL.md, scripts, references, or assets.`

## Main File Rule
`SKILL.md` should act as a routing layer.
It should not try to hold every detail.

Put in `SKILL.md`:
- goal
- workflow
- decision tree
- constraints
- validation
- resource navigation

Move out of `SKILL.md`:
- long domain detail
- deep examples
- platform-specific edge behavior
- repeated boilerplate content

## Decision-First Skills
For multi-branch skills, always help the agent answer:
- What kind of request is this?
- Which path applies?
- What resource should be loaded next?
- What proof is required before completion?
