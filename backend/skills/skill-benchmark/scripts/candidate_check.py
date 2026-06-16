#!/usr/bin/env python3
import json
import sys
from pathlib import Path


def check(target: str) -> dict:
    if target.startswith('http://') or target.startswith('https://'):
        return {
            'candidate_type': 'remote-candidate',
            'eligible': True,
            'target': target,
        }

    path = Path(target)
    if path.exists() and path.is_dir():
        has_skill_md = (path / 'SKILL.md').exists()
        return {
            'candidate_type': 'local-skill',
            'eligible': has_skill_md,
            'target': str(path),
            'has_skill_md': has_skill_md,
        }

    return {
        'candidate_type': 'unknown',
        'eligible': False,
        'target': target,
        'reason': 'candidate path or URL not recognized',
    }


def main() -> int:
    if len(sys.argv) != 2:
        print('usage: candidate_check.py <skill-path-or-url>', file=sys.stderr)
        return 2
    print(json.dumps(check(sys.argv[1]), ensure_ascii=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
