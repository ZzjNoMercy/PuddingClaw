# Benchmark Workflow

## Sequence
1. Run `scripts/candidate_check.py` before any formal benchmark.
2. Classify the request with `scripts/benchmark_level.py`.
3. Run `scripts/run_benchmark.py` to write a raw run record.
4. Score the raw run with `scripts/score_benchmark.py`.
5. Aggregate summaries with `scripts/aggregate_results.py` when multiple run files need a combined summary.
6. Update long-term signals with `scripts/write_trend_summary.py`.

## Output Directories
- `benchmarks/raw/`: run-level records.
- `benchmarks/summaries/`: scored summaries.
- `benchmarks/trends/`: per-skill trend rollups.

## Guardrails
- Keep baseline and with-skill results separate.
- Use the same scoring rules across trend runs.
- Treat quick checks as weak evidence unless they are repeated.
