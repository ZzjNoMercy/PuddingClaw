#!/usr/bin/env python3
import argparse
import json
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from judge_real_results import judge  # noqa: E402
from score_benchmark import score  # noqa: E402
from write_trend_summary import build_trend  # noqa: E402


def _is_real_run(data: dict) -> bool:
    runs = data.get('runs', [])
    if not runs:
        return False
    first = runs[0]
    return 'baseline' in first and 'with_skill' in first


def _skill_name_from_target(target: str) -> str:
    path = Path(target)
    return path.name or target.rstrip('/').split('/')[-1]


def _write_if_changed(path: Path, payload: dict) -> bool:
    new_text = json.dumps(payload, indent=2, ensure_ascii=False)
    if path.exists() and path.read_text() == new_text:
        return False
    path.write_text(new_text)
    return True


def sync_outputs(raw_dir: Path, summaries_dir: Path, trends_dir: Path) -> dict:
    summaries_dir.mkdir(parents=True, exist_ok=True)
    trends_dir.mkdir(parents=True, exist_ok=True)

    processed_runs = 0
    generated_summaries = 0
    summary_changed = 0
    touched_skills: set[str] = set()

    for raw_file in sorted(raw_dir.glob('*.json')):
        data = json.loads(raw_file.read_text())
        run_id = data.get('run_id') or raw_file.stem
        summary_file = summaries_dir / f'{run_id}.json'
        if _is_real_run(data):
            summary = judge(raw_file)
        else:
            summary = score(raw_file)
        if _write_if_changed(summary_file, summary):
            summary_changed += 1
        processed_runs += 1
        generated_summaries += 1
        for target in summary.get('skill_targets', []):
            touched_skills.add(_skill_name_from_target(target))

    generated_trends = 0
    trend_changed = 0
    for skill_name in sorted(touched_skills):
        trend = build_trend(skill_name, summaries_dir)
        trend_file = trends_dir / f'{skill_name}.json'
        if _write_if_changed(trend_file, trend):
            trend_changed += 1
        generated_trends += 1

    return {
        'processed_runs': processed_runs,
        'generated_summaries': generated_summaries,
        'summary_changed': summary_changed,
        'generated_trends': generated_trends,
        'trend_changed': trend_changed,
        'skills': sorted(touched_skills),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description='Generate summaries and trends from raw benchmark runs.')
    parser.add_argument('--raw-dir', required=True)
    parser.add_argument('--summaries-dir', required=True)
    parser.add_argument('--trends-dir', required=True)
    args = parser.parse_args()
    result = sync_outputs(Path(args.raw_dir), Path(args.summaries_dir), Path(args.trends_dir))
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
