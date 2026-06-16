# Real Benchmark Scenarios

## Scenario Types
- `should-trigger`: the target skill should trigger.
- `should-not-trigger`: the target skill should not trigger.
- `route-sensitive`: the benchmark expects a specific route such as `switch-model-team`.
- `output-sensitive`: the benchmark expects structured output signals such as claim decomposition, disagreement, and arbitration.

## Prompt Authoring Rule
Include expected route and required output signals whenever the benchmark is meant to validate more than simple triggering.

## Route-Sensitive Prompt Rule
When the benchmark is intended to prove a route such as `switch-model-team`, the prompt must make direct answering non-compliant.

Use prompts that explicitly require:
- the named skill or route
- an acknowledgement that the route was used
- structured output signals that are difficult to fake accidentally

Do not use broad prompts that a strong baseline can satisfy with a direct answer.
