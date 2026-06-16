# Remediation Playbook

Use this mapping to convert review findings into fixes.

## Description Too Broad
Symptoms:
- trigger overlaps many adjacent tasks
- description contains words like "general", "anything", or "various tasks"

Fix:
- rewrite around repeated workflow shape
- add typical request patterns
- add object or file type cues
- remove adjacent tasks that belong to other skills

## Main File Too Long
Symptoms:
- `SKILL.md` is hard to scan
- examples and detail dominate workflow guidance

Fix:
- keep only goal, workflow, routing, constraints, validation, and resource navigation
- move deep examples and optional detail into `references/`
- shorten repeated wording

## Weak Routing
Symptoms:
- no decision tree
- multiple branches exist but the skill does not classify them

Fix:
- add explicit branch rules
- map each branch to a script, reference, or asset
- make the first action after classification obvious

## Missing Scripts
Symptoms:
- repeated fragile steps are always done manually
- the skill claims determinism but has no executable helper

Fix:
- add a small script for the repeated path
- keep parameters minimal
- document when to run it

## Missing Validation
Symptoms:
- skill says "done" without proof
- no checks, outputs, or acceptance rules are defined

Fix:
- add required checks
- define success criteria
- add first failure checks
- if possible, add a smoke-test command
