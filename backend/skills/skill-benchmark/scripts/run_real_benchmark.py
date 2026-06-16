#!/usr/bin/env python3
import argparse
import json
import subprocess
import sys
import uuid
from datetime import datetime, UTC
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from sync_benchmark_outputs import sync_outputs  # noqa: E402


BUILTIN_EXECUTOR_ADAPTERS = {
    'claude-cli': SCRIPT_DIR / 'claude_cli_executor.py',
}


def _now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace('+00:00', 'Z')


def resolve_executor(executor: str | None, executor_adapter: str | None) -> tuple[str, Path]:
    if executor and executor_adapter:
        raise ValueError('use either --executor or --executor-adapter, not both')
    if executor:
        return ('custom', Path(executor).resolve())
    adapter_name = executor_adapter or 'claude-cli'
    try:
        return (adapter_name, BUILTIN_EXECUTOR_ADAPTERS[adapter_name].resolve())
    except KeyError as exc:
        raise ValueError(f'unknown executor adapter: {adapter_name}') from exc


def _run_executor(executor: Path, prompt_file: Path, model: str, run_kind: str, skill: str | None = None) -> dict:
    cmd = [
        'python3',
        str(executor),
        '--prompt-file', str(prompt_file),
        '--model', model,
        '--run-kind', run_kind,
    ]
    if skill:
        cmd.extend(['--skill', skill])
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, check=True)
        return json.loads(result.stdout)
    except subprocess.CalledProcessError as exc:
        stdout = exc.stdout or ''
        stderr = exc.stderr or ''
        if stdout:
            try:
                return json.loads(stdout)
            except json.JSONDecodeError:
                pass
        return {
            'output_text': '[benchmark executor failed before producing JSON]',
            'trace_signals': {
                'skill_attempted': False,
                'team_mode_attempted': False,
                'skill_triggered': False,
                'team_mode_used': False,
            },
            'meta': {
                'run_kind': run_kind,
                'failed': True,
                'returncode': exc.returncode,
                'stderr_tail': stderr[-4000:] if stderr else '',
                'command': cmd,
            },
        }


def _limit_prompts(prompts: list[dict], max_prompts: int | None) -> list[dict]:
    if not max_prompts or max_prompts <= 0:
        return prompts
    return prompts[:max_prompts]


def _write_partial(payload: dict, raw_file: Path) -> None:
    raw_file.parent.mkdir(parents=True, exist_ok=True)
    raw_file.write_text(json.dumps(payload, indent=2, ensure_ascii=False))


def run_real_benchmark(skill_targets: list[str], mode: str, prompts_path: Path, models: list[str], output_dir: Path, executor: Path, max_prompts: int | None = None, executor_adapter: str = 'custom') -> dict:
    prompts_data = json.loads(prompts_path.read_text())
    prompts = _limit_prompts(prompts_data.get('prompts', []), max_prompts)
    run_id = f'real-{uuid.uuid4().hex[:10]}'
    raw_dir = output_dir / 'raw'
    artifacts_root = output_dir / 'artifacts' / run_id
    raw_dir.mkdir(parents=True, exist_ok=True)
    artifacts_root.mkdir(parents=True, exist_ok=True)
    raw_file = raw_dir / f'{run_id}.json'
    payload = {
        'run_id': run_id,
        'mode': mode,
        'skill_targets': skill_targets,
        'models': models,
        'runs': [],
        'timestamp': _now(),
        'prompt_count': len(prompts),
    }
    _write_partial(payload, raw_file)
    for prompt in prompts:
        prompt_id = prompt.get('id', uuid.uuid4().hex[:8])
        prompt_dir = artifacts_root / prompt_id
        prompt_dir.mkdir(parents=True, exist_ok=True)
        prompt_file = prompt_dir / 'prompt.json'
        prompt_file.write_text(json.dumps(prompt, indent=2, ensure_ascii=False))
        baseline = _run_executor(executor, prompt_file, models[0], 'baseline')
        with_skill = _run_executor(executor, prompt_file, models[0], 'with-skill', skill_targets[0])
        (prompt_dir / 'baseline.json').write_text(json.dumps(baseline, indent=2, ensure_ascii=False))
        (prompt_dir / 'with_skill.json').write_text(json.dumps(with_skill, indent=2, ensure_ascii=False))
        payload['runs'].append({
            'prompt_id': prompt_id,
            'prompt_text': prompt.get('text', ''),
            'expected_trigger': bool(prompt.get('should_trigger', True)),
            'expected_skill': prompt.get('expected_skill'),
            'expected_route': prompt.get('expected_route'),
            'required_output_signals': prompt.get('required_output_signals', []),
            'baseline': baseline,
            'with_skill': with_skill,
            'artifacts_dir': str(prompt_dir),
        })
        _write_partial(payload, raw_file)
    sync_result = sync_outputs(output_dir / 'raw', output_dir / 'summaries', output_dir / 'trends')
    summary_file = output_dir / 'summaries' / f'{run_id}.json'
    trend_files = {skill_name: str(output_dir / 'trends' / f'{skill_name}.json') for skill_name in sync_result.get('skills', [])}
    return {
        'run_id': run_id,
        'raw_file': str(raw_file),
        'summary_file': str(summary_file),
        'trend_files': trend_files,
        'executor_adapter': executor_adapter,
        'executor_path': str(executor),
        'sync': sync_result,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description='Run a real benchmark via an executor adapter.')
    parser.add_argument('--skill', action='append', dest='skills', required=True)
    parser.add_argument('--mode', required=True)
    parser.add_argument('--prompts', required=True)
    parser.add_argument('--model', action='append', dest='models', required=True)
    parser.add_argument('--output-dir', required=True)
    parser.add_argument('--executor')
    parser.add_argument('--executor-adapter')
    parser.add_argument('--max-prompts', type=int)
    args = parser.parse_args()
    adapter_name, executor_path = resolve_executor(args.executor, args.executor_adapter)
    result = run_real_benchmark(
        args.skills,
        args.mode,
        Path(args.prompts),
        args.models,
        Path(args.output_dir),
        executor_path,
        args.max_prompts,
        executor_adapter=adapter_name,
    )
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
