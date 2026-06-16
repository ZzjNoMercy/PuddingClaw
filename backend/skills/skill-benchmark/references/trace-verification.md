# Trace Verification

## Principle
Prefer execution trace signals over output imitation when deciding whether a skill or route actually fired. Output structure alone is not enough to prove that the intended skill path was used.

## Signal Priority
1. Trace signals
2. Artifact or metadata signals
3. Output text signals

Use lower-priority signals only as supporting evidence when stronger signals are missing.

## `cross-model-verifier` Trigger Verification

### Strong Evidence
Treat the trigger as proven when one or more of these is true:
- the execution trace shows the skill was loaded or read
- the execution trace shows a dedicated `cross-model-verifier` script or reference was accessed
- the runner metadata explicitly records `skill_triggered: true` for `cross-model-verifier`

### Weak Evidence
Treat these as suggestive only, not sufficient on their own:
- output contains a claim-by-claim verification structure
- output contains disagreement or adjudication sections that resemble the skill's expected output

### Verdict Rule
- `trigger_correct = true` only when strong evidence shows the expected skill path was used
- if only weak evidence exists, record the run as `suggestive-only` in analysis notes rather than trigger-correct

## `switch-model-team` Route Verification

### Strong Evidence
Treat the route as proven when one or more of these is true:
- the trace shows `switch-model` was invoked and classified to `team` mode
- runner metadata records `team_mode_used: true`
- a route artifact explicitly records a team-mode execution path

### Supporting Evidence
Useful for explanation, but not enough on its own:
- output names a team, team preset, or multi-model orchestration path
- output includes provider/model splits that are consistent with team mode

### Verdict Rule
When `expected_route` is `switch-model-team`:
- `route_correct = true` only when strong evidence shows team mode was used
- supporting evidence alone does not make the route correct

## Fallback Rule
If neither strong trigger nor strong route evidence is available, do not infer success from output style alone. Mark the benchmark as not proven on those dimensions.
