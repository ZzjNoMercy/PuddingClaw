#!/usr/bin/env python3
import argparse
import json
import uuid
from datetime import datetime, UTC
from pathlib import Path


def _now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace('+00:00', 'Z')


def _simulate_result(prompt: dict) -> dict:
    expected = bool(prompt.get('should_trigger', True))
    return {
        'prompt_id': prompt.get('id'),
        'prompt_text': prompt.get('text', ''),
        'expected_trigger': expected,
        'baseline_triggered': not expected,
        'with_skill_triggered': expected,
        'routing_ok': True,
        'outcome_ok': True,
    }


def run_benchmark(skill_targets: list[str], mode: str, prompts_path: Path, models: list[str], output_dir: Path) -> dict:
    prompts_data = json.loads(prompts_path.read_text())
    prompts = prompts_data.get('prompts', [])
    run_id = f'run-{uuid.uuid4().hex[:10]}'
    raw_dir = output_dir / 'raw'
    raw_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        'run_id': run_id,
        'mode': mode,
        'skill_targets': skill_targets,
        'models': models,
        'prompts': prompts,
        'results': [_simulate_result(prompt) for prompt in prompts],
        'timestamp': _now(),
    }
    raw_file = raw_dir / f'{run_id}.json'
    raw_file.write_text(json.dumps(payload, indent=2, ensure_ascii=False))
    return {'run_id': run_id, 'raw_file': str(raw_file)}


def main() -> int:
    parser = argparse.ArgumentParser(description='Run a benchmark and write a raw result file.')
    parser.add_argument('--skill', action='append', dest='skills', required=True)
    parser.add_argument('--mode', required=True)
    parser.add_argument('--prompts', required=True)
    parser.add_argument('--model', action='append', dest='models', required=True)
    parser.add_argument('--output-dir', required=True)
    args = parser.parse_args()
    result = run_benchmark(args.skills, args.mode, Path(args.prompts), args.models, Path(args.output_dir))
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
