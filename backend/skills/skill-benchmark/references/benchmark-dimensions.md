# Benchmark Dimensions

## Core Dimensions (used in verdict calculation)
- `trigger_accuracy`: whether the skill activates when it should and stays silent when it should not.
- `routing_clarity`: whether the correct branch, script, or resource path was chosen.
- `output_signal_retention`: whether required output signals (keywords, concepts) appear in the with-skill output. Scored per-signal with synonym support, not all-or-nothing per scenario.
- `outcome_quality`: whether the task was completed, output is structured, and required signals are present. Composite score: completion (0.5) + structured output (0.25) + signal coverage (0.25).

## Task Completion Dimension (reported separately)
- `task_completion.baseline`: fraction of scenarios where the baseline run finished without timeout or failure.
- `task_completion.with_skill`: fraction of scenarios where the with-skill run finished without timeout or failure.
- `task_completion.gain`: with_skill minus baseline. Positive means the skill helped tasks finish.

A task is considered completed when: not timed out, not failed, and output is at least 50 characters.

## Supplementary Dimensions (not used in verdict, tracked for context)
- `model_robustness`: whether the gain persists across multiple models rather than one lucky run. Requires multi-model benchmark runs.
- `consistency_over_time`: whether repeated runs or later versions show stable or improving behavior. Requires historical trend data.

## Verdict Formula
The final verdict is based on the average of the four core dimensions:
```
core_avg = (trigger_accuracy + routing_clarity + output_signal_retention + outcome_quality) / 4
```

## Usage Rule
A skill should not be called broadly effective unless at least trigger accuracy, routing clarity, and outcome quality show repeatable gains. Task completion gain provides supporting evidence but does not override the core dimensions.
