# Evaluation Scenarios

## Scenario Classes
- `should-trigger`: prompts where the skill should activate.
- `should-not-trigger`: nearby prompts where the skill should stay inactive.
- `edge-case`: ambiguous or mixed prompts that stress the trigger boundary.
- `compare-case`: prompts used to compare two skills or two versions.

## Scenario Quality Rules
- Include both clear positives and nearby non-trigger negatives.
- Avoid writing prompts that merely restate the skill description.
- Include at least one ambiguous prompt in formal runs.
- Use the same prompt set for baseline and with-skill comparisons.
