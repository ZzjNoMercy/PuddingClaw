# Archive Index

## Active Files
- `SKILL.md`
- `agents/openai.yaml`
- `references/benchmark-dimensions.md`
- `references/eval-scenarios.md`
- `references/scoring-rules.md`
- `references/result-schema.md`
- `references/benchmark-workflow.md`
- `references/history-schema.md`
- `references/trace-verification.md`
- `references/output-assertions.md`
- `references/real-benchmark-scenarios.md`
- `scripts/candidate_check.py`
- `scripts/benchmark_level.py`
- `scripts/run_benchmark.py`
- `scripts/score_benchmark.py`
- `scripts/aggregate_results.py`
- `scripts/run_real_benchmark.py`
- `scripts/extract_trace_signals.py`
- `scripts/judge_real_results.py`
- `scripts/write_trend_summary.py`
- `assets/benchmark-prompts-template.json`
- `assets/report-template.md`
- `tests/test_skill_benchmark.py`
- `tests/test_skill_benchmark_v2.py`

## Governance Log
- `2026-03-07 Initial Creation`: created `skill-benchmark` as a benchmark controller skeleton with candidate checking, mode routing, aggregation, tests, and validator/review pass.
- `2026-03-07 V2 Execution Upgrade`: added raw benchmark run writing, scoring summaries, trend rollups, workflow/history references, and v2 tests.
- `2026-03-07 Prompt Template Addition`: added a starter benchmark prompt asset for post-upgrade evaluation handoff from `skill-creator-pro`.
- `2026-03-07 V3.1 Real Runner Upgrade`: added executor-driven real benchmark runs, trace extraction, real-result judgment, and route/output-sensitive benchmark guidance.
