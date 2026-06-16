#!/usr/bin/env python3
import json
import sys
from pathlib import Path


VERDICTS = (
    ('effective', 0.85),
    ('partially effective', 0.60),
    ('not proven', 0.35),
    ('ineffective', 0.0),
)


def _ratio(flags: list[bool]) -> float:
    if not flags:
        return 0.0
    return round(sum(1 for flag in flags if flag) / len(flags), 4)


def score(path: Path) -> dict:
    data = json.loads(path.read_text())
    results = data.get('results', [])
    trigger_flags = [r.get('with_skill_triggered') == r.get('expected_trigger') for r in results]
    routing_flags = [bool(r.get('routing_ok')) for r in results]
    outcome_flags = [bool(r.get('outcome_ok')) for r in results]
    trigger_accuracy = _ratio(trigger_flags)
    routing_clarity = _ratio(routing_flags)
    outcome_quality = _ratio(outcome_flags)
    score_value = round((trigger_accuracy + routing_clarity + outcome_quality) / 3, 4)
    final_verdict = 'ineffective'
    for verdict, threshold in VERDICTS:
        if score_value >= threshold:
            final_verdict = verdict
            break
    summary = {
        'run_id': data.get('run_id'),
        'skill_targets': data.get('skill_targets', []),
        'models': data.get('models', []),
        'aggregate_scores': {
            'trigger_accuracy': trigger_accuracy,
            'routing_clarity': routing_clarity,
            'outcome_quality': outcome_quality,
        },
        'final_verdict': final_verdict,
        'timestamp': data.get('timestamp'),
    }
    return summary


def main() -> int:
    if len(sys.argv) != 2:
        print('usage: score_benchmark.py <raw-run-json>', file=sys.stderr)
        return 2
    print(json.dumps(score(Path(sys.argv[1])), ensure_ascii=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
