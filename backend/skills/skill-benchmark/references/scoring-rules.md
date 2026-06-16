# Scoring Rules

## Verdicts
- `effective`: core_avg >= 0.85. Clear and repeatable gain across the key dimensions.
- `partially effective`: core_avg >= 0.60. Some gain is present, but not stable across all dimensions.
- `not proven`: core_avg >= 0.35. The evidence is insufficient or mixed.
- `ineffective`: core_avg < 0.35. No meaningful gain or clear regression versus baseline.

## Verdict Calculation
```
core_avg = (trigger_accuracy + routing_clarity + output_signal_retention + outcome_quality) / 4
```

## Output Signal Retention Scoring
Signals are scored **per-signal**, not per-scenario:
- Each required_output_signal is checked independently against the with-skill output.
- Synonym matching is supported via `references/signal-synonyms.json`.
- Retention = total_signals_hit / total_signals_required.

This replaces the previous all-or-nothing approach where a single missing signal would fail the entire scenario.

## Outcome Quality Scoring
Each scenario scores 0.0 to 1.0:
- +0.50 if the task completed (not timed out, not failed, output >= 50 chars).
- +0.25 if the output is structured (contains headers, lists, tables, or code blocks).
- +0.25 if all required output signals are present (or no signals were required and task completed).

A scenario passes (True) if outcome_score >= 0.5.

## Task Completion
Reported as a separate metric, not part of the verdict formula:
- `baseline`: completion rate without the skill.
- `with_skill`: completion rate with the skill.
- `gain`: the difference. Positive = skill helps tasks finish.

Baseline and with-skill use the same timeout (180s) for fair comparison.

## Interpretation Rules
- One strong anecdotal run is not enough for `effective`.
- If trigger behavior improves but outcome quality does not, the result is at most `partially effective`.
- If results conflict strongly across models, the verdict should fall to `not proven` unless the evaluated scope is model-specific.
- Task completion gain is supporting evidence; a high gain with low trigger accuracy is still `not proven`.
