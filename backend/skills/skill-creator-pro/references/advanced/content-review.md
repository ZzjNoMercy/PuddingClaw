# Content Review Rubric

Use this rubric when reviewing skill quality beyond syntax and file presence.

## 1. Trigger Quality
Strong skill:
- states both capability and invocation context in frontmatter
- includes concrete task shapes or request patterns
- avoids broad claims like "any task" or "various tasks"

Weak skill:
- only states capability
- uses generic language that overlaps many other skills
- leaves boundary decisions to the body

## 2. Routing Quality
Strong skill:
- makes the next step obvious
- helps the agent classify the request
- tells the agent which resource to load next

Weak skill:
- reads like a tutorial or essay
- hides branching logic in long paragraphs
- mixes unrelated paths without explicit selection rules

## 3. Context Efficiency
Strong skill:
- keeps `SKILL.md` compact
- moves deep detail into `references/`
- avoids repeating the same guidance in multiple places

Weak skill:
- has a bloated main file
- duplicates reference material
- explains basics the model likely already knows

## 4. Reuse and Determinism
Strong skill:
- scripts repeated fragile actions when justified
- uses templates or assets for repeatable outputs
- avoids re-deriving the same structure every time

Weak skill:
- repeats manual steps for common tasks
- leaves stable operations to free-form reasoning
- has no reusable scaffolding despite repeated use

## 5. Validation Quality
Strong skill:
- says what must be checked
- defines success criteria
- gives first failure checks

Weak skill:
- only says "verify" without telling how
- confuses completion with execution
- omits evidence requirements
