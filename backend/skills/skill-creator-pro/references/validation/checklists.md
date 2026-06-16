# Checklists

## Creation Checklist
- name is valid hyphen-case
- description includes both capability and when-to-use context
- main workflow is explicit
- decision points are explicit when branches exist
- repeated fragile actions are pushed into scripts when justified
- long or optional detail is moved into references
- templates and output materials are placed in assets
- validation steps are defined
- openai.yaml matches the skill

## Review Checklist
- Is the trigger too broad?
- Is the trigger too narrow?
- Is `SKILL.md` doing routing or trying to be a textbook?
- Is any content duplicated?
- Is anything repeatedly done by hand that should be scripted?
- Can a new agent follow one real path end to end?
- Is there a concrete completion check?

## Release Checklist
- Validate frontmatter
- Read the skill once from top to bottom for scanability
- Test one realistic request
- Fix any ambiguity discovered during testing
