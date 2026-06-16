# Output Assertions

## Purpose
These rules define the starter output signals checked by real benchmark runs. They are intentionally narrow and deterministic.

## Supported Starter Signals
- `claim_decomposition`
- `disagreement`
- `arbitration`

## Assertion Rules

### `claim_decomposition`
Pass this assertion when the with-skill output clearly breaks the task into multiple claims, checks, or verification items. Stronger evidence includes numbered claims, bullet lists of claims, or explicit `Claim 1`, `Claim 2` style sections.

Do not pass this assertion for a single unstructured paragraph that merely mentions a claim in passing.

### `disagreement`
Pass this assertion when the output explicitly records disagreement, conflict, competing judgments, or inconsistent model findings.

Do not pass this assertion when the output presents only one unified conclusion with no visible tension or comparison.

### `arbitration`
Pass this assertion when the output explicitly includes a final adjudication, arbitration, synthesis, or decision that resolves earlier disagreement or competing claims.

Do not pass this assertion when the output stops at listing disagreements without a final resolution.

## Evaluation Rule
For each prompt, every item in `required_output_signals` must pass for the prompt to count as preserving required output structure.

## Caution
Passing these output assertions does not by itself prove that the expected skill or route was used. Output assertions must be combined with trace verification.
