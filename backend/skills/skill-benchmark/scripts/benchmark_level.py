#!/usr/bin/env python3
import json
import sys


def classify(text: str) -> str:
    lowered = text.lower()
    if 'trend' in lowered or 'history' in lowered:
        return 'trend-review'
    if 'compare' in lowered or 'versus' in lowered or 'vs' in lowered:
        return 'compare-skills'
    if 'quick' in lowered or 'fast' in lowered:
        return 'quick-check'
    return 'benchmark-run'


def main() -> int:
    if len(sys.argv) != 2:
        print('usage: benchmark_level.py <request-text>', file=sys.stderr)
        return 2
    mode = classify(sys.argv[1])
    print(json.dumps({'mode': mode, 'input': sys.argv[1]}, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
