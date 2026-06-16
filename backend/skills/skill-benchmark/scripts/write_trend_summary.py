#!/usr/bin/env python3
import argparse
import json
from pathlib import Path


ORDER = {
    'ineffective': 0,
    'not proven': 1,
    'partially effective': 2,
    'effective': 3,
}


def build_trend(skill_name: str, summaries_dir: Path) -> dict:
    entries = []
    for file in sorted(summaries_dir.glob('*.json')):
        data = json.loads(file.read_text())
        targets = data.get('skill_targets', [])
        if skill_name in targets or any(Path(t).name == skill_name for t in targets):
            entries.append(data)
    entries.sort(key=lambda item: item.get('timestamp', ''))
    models = sorted({model for item in entries for model in item.get('models', [])})
    latest_verdict = entries[-1].get('final_verdict') if entries else 'not proven'
    trend_signal = 'insufficient-data'
    if len(entries) >= 2:
        first = ORDER.get(entries[0].get('final_verdict', 'not proven'), 1)
        last = ORDER.get(entries[-1].get('final_verdict', 'not proven'), 1)
        if last > first:
            trend_signal = 'improving'
        elif last < first:
            trend_signal = 'regressing'
        else:
            trend_signal = 'stable'
    elif len(entries) == 1:
        trend_signal = 'stable'
    return {
        'skill_name': skill_name,
        'run_count': len(entries),
        'latest_verdict': latest_verdict,
        'trend_signal': trend_signal,
        'models_seen': models,
        'last_updated': entries[-1].get('timestamp') if entries else None,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description='Aggregate summaries into a trend summary.')
    parser.add_argument('--skill-name', required=True)
    parser.add_argument('--summaries-dir', required=True)
    args = parser.parse_args()
    print(json.dumps(build_trend(args.skill_name, Path(args.summaries_dir)), ensure_ascii=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
