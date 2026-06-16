import json
import subprocess
import sys
from pathlib import Path

BASE = Path('/Users/mac/.claude/skills/skill-benchmark')


def test_run_real_benchmark_accepts_builtin_claude_cli_adapter(tmp_path):
    prompts = tmp_path / 'prompts.json'
    prompts.write_text(json.dumps({'prompts': []}))
    out_dir = tmp_path / 'benchmarks'
    result = subprocess.run(
        [
            sys.executable,
            str(BASE / 'scripts' / 'run_real_benchmark.py'),
            '--skill', '/tmp/demo-skill',
            '--mode', 'benchmark-run',
            '--prompts', str(prompts),
            '--model', 'sonnet',
            '--output-dir', str(out_dir),
            '--executor-adapter', 'claude-cli',
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    payload = json.loads(result.stdout)
    assert payload['executor_adapter'] == 'claude-cli'
    assert payload['executor_path'].endswith('claude_cli_executor.py')


def test_run_real_benchmark_uses_claude_cli_as_default_adapter(tmp_path):
    prompts = tmp_path / 'prompts.json'
    prompts.write_text(json.dumps({'prompts': []}))
    out_dir = tmp_path / 'benchmarks'
    result = subprocess.run(
        [
            sys.executable,
            str(BASE / 'scripts' / 'run_real_benchmark.py'),
            '--skill', '/tmp/demo-skill',
            '--mode', 'benchmark-run',
            '--prompts', str(prompts),
            '--model', 'sonnet',
            '--output-dir', str(out_dir),
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    payload = json.loads(result.stdout)
    assert payload['executor_adapter'] == 'claude-cli'
    assert payload['executor_path'].endswith('claude_cli_executor.py')
