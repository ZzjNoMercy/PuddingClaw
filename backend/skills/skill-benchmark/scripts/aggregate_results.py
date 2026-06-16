#!/usr/bin/env python3
import json
import sys
from pathlib import Path


def aggregate(path: Path) -> dict:
    data = json.loads(path.read_text())
    verdict_counts = {}
    models = set()
    for run in data.get('runs', []):
        verdict = run.get('verdict', 'unknown')
        verdict_counts[verdict] = verdict_counts.get(verdict, 0) + 1
        model = run.get('model')
        if model:
            models.add(model)
    return {
        'skill_name': data.get('skill_name'),
        'run_count': len(data.get('runs', [])),
        'verdict_counts': verdict_counts,
        'models': sorted(models),
    }


def main() -> int:
    if len(sys.argv) != 2:
        print('usage: aggregate_results.py <result-json>', file=sys.stderr)
        return 2
    print(json.dumps(aggregate(Path(sys.argv[1])), ensure_ascii=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
