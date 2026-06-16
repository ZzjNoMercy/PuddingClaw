---
name: skill-benchmark
description: >
  Use when asked to evaluate whether a local or external skill is actually effective across representative prompts, multiple models, and baseline-vs-with-skill comparisons. Supports quick checks, formal benchmark runs, skill-vs-skill comparisons, and long-term trend reviews.
---

# Skill Benchmark

## Goal
Measure whether a skill produces stable, repeatable gains in trigger accuracy, routing clarity, and task outcome quality across representative prompts, multiple models, and baseline comparisons.

## Workflow
1. Confirm the candidate skill or skills to benchmark.
2. Run `scripts/candidate_check.py` to verify the benchmark target is usable.
3. Choose the benchmark mode with `scripts/benchmark_level.py`.
4. Select or build evaluation scenarios that cover should-trigger, should-not-trigger, edge, and comparison prompts.
5. Run baseline and with-skill comparisons with `scripts/run_real_benchmark.py`, extract trace signals with `scripts/extract_trace_signals.py`, and score the raw run with `scripts/judge_real_results.py`.
6. Aggregate summaries with `scripts/aggregate_results.py` and update long-term trends with `scripts/write_trend_summary.py`.

## Decision Tree
- If the user wants a fast confidence signal, run `quick-check`.
- If the user wants a formal evaluation of one skill, run `benchmark-run`.
- If the user wants to compare two skills or two versions, run `compare-skills`.
- If the user wants to inspect accumulated historical results, run `trend-review`.
- If the candidate is incomplete or invalid, stop and report the eligibility failure before benchmarking.

## Constraints
- Do not claim a skill is universally effective based on one model or one prompt set.
- Do not confuse structural compliance with empirical effectiveness.
- Do not benchmark an incomplete candidate as if it were production-ready.
- Keep baseline and with-skill outputs clearly separated.
- Treat trend review as meaningful only when the result schema and scoring rules stayed consistent.

## Validation
- Candidate eligibility must be checked before any formal run.
- Every benchmark run must record candidate identity, model, prompts, baseline results, with-skill results, scores, verdicts, and timestamp.
- Final conclusions must distinguish `effective`, `partially effective`, `not proven`, and `ineffective`.
- Aggregated output must summarize verdict counts and preserve the source skill name.

## Resources
- `scripts/candidate_check.py`: verify whether a local path, remote URL, or external candidate can enter benchmarking.
- `scripts/benchmark_level.py`: classify the request into quick-check, benchmark-run, compare-skills, or trend-review.
- `scripts/run_benchmark.py`: write simulated raw benchmark runs for quick checks and schema testing.
- `scripts/run_real_benchmark.py`: execute baseline and with-skill runs through a runner adapter and write real raw benchmark runs; defaults to the built-in `claude-cli` adapter when no custom executor path is provided.
- `scripts/extract_trace_signals.py`: verify trigger and route evidence from real raw runs.
- `scripts/score_benchmark.py`: turn a simulated raw run into a scored summary and verdict.
- `scripts/judge_real_results.py`: judge real raw runs for trigger accuracy, routing clarity, output signal retention (per-signal with synonyms), task completion rate, and outcome quality.
- `scripts/aggregate_results.py`: summarize groups of run or summary files into a stable benchmark result.
- `scripts/write_trend_summary.py`: roll up summary files into a per-skill trend signal.
- `scripts/sync_benchmark_outputs.py`: rebuild summary and trend outputs from a raw benchmark directory in one pass.
- `references/benchmark-dimensions.md`: scoring dimensions for trigger, routing, outcome, robustness, and long-term consistency.
- `references/eval-scenarios.md`: scenario design rules for should-trigger, should-not-trigger, edge, and comparison prompts.
- `references/scoring-rules.md`: scoring and verdict rules.
- `references/result-schema.md`: required result fields for raw runs, summaries, and trend analysis.
- `references/benchmark-workflow.md`: execution order and storage rules for benchmark runs.
- `references/history-schema.md`: long-term summary and trend record schema.
- `references/trace-verification.md`: trace-based rules for validating real trigger and route evidence.
- `references/output-assertions.md`: starter output assertion rules for claim decomposition, disagreement, and arbitration.
- `references/real-benchmark-scenarios.md`: scenario patterns for route-sensitive and output-sensitive real benchmarks.
- `references/signal-synonyms.json`: extensible synonym mapping for output signal matching (add new synonyms here without changing code).
- `assets/benchmark-prompts-template.json`: starter benchmark prompt set for should-trigger, should-not-trigger, edge, comparison, route, and output-signal cases.
- `assets/report-template.md`: optional report template for benchmark summaries.
