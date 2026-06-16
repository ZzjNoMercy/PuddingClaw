# Result Schema

## Raw Run Fields
- `run_id`
- `mode`
- `skill_targets`
- `models`
- `runs`
- `timestamp`

## Raw Run Item
- `prompt_id`
- `prompt_text`
- `expected_trigger`
- `expected_skill`
- `expected_route`
- `required_output_signals`
- `baseline`
- `with_skill`
- `artifacts_dir`

## Execution Result
- `output_text`
- `trace_signals`
- `meta`

## Summary Fields
- `run_id`
- `skill_targets`
- `models`
- `aggregate_scores`
- `final_verdict`
- `timestamp`

## Aggregate Score Fields
- `trigger_accuracy`
- `routing_clarity`
- `output_signal_retention`

## Trend Fields
- `skill_name`
- `run_count`
- `latest_verdict`
- `trend_signal`
- `models_seen`
- `last_updated`

## Consistency Requirement
Trend review is only meaningful when the same scenario family and scoring rules were reused.
