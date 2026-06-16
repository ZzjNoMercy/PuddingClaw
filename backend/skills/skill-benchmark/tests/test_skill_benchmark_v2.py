import json
import subprocess
import sys
from pathlib import Path

BASE = Path('/Users/mac/.claude/skills/skill-benchmark')


def test_skill_references_v2_resources():
    text = (BASE / 'SKILL.md').read_text()
    assert 'scripts/run_benchmark.py' in text
    assert 'scripts/score_benchmark.py' in text
    assert 'scripts/write_trend_summary.py' in text
    assert 'references/benchmark-workflow.md' in text
    assert 'references/history-schema.md' in text


def test_run_benchmark_writes_raw_result(tmp_path):
    prompts = tmp_path / 'prompts.json'
    prompts.write_text(json.dumps({
        'prompts': [
            {'id': 'p1', 'text': 'use this skill', 'should_trigger': True},
            {'id': 'p2', 'text': 'do not use this skill', 'should_trigger': False},
        ]
    }))
    out_dir = tmp_path / 'benchmarks'
    result = subprocess.run(
        [
            sys.executable,
            str(BASE / 'scripts' / 'run_benchmark.py'),
            '--skill', '/tmp/demo-skill',
            '--mode', 'benchmark-run',
            '--prompts', str(prompts),
            '--model', 'claude-sonnet',
            '--output-dir', str(out_dir),
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    data = json.loads(result.stdout)
    raw_file = Path(data['raw_file'])
    assert raw_file.exists()
    payload = json.loads(raw_file.read_text())
    assert payload['mode'] == 'benchmark-run'
    assert payload['skill_targets'] == ['/tmp/demo-skill']
    assert len(payload['results']) == 2


def test_score_benchmark_emits_summary_with_verdict(tmp_path):
    raw_file = tmp_path / 'raw.json'
    raw_file.write_text(json.dumps({
        'run_id': 'run-1',
        'mode': 'benchmark-run',
        'skill_targets': ['/tmp/demo-skill'],
        'models': ['claude-sonnet'],
        'results': [
            {'prompt_id': 'p1', 'baseline_triggered': False, 'with_skill_triggered': True, 'expected_trigger': True, 'routing_ok': True, 'outcome_ok': True},
            {'prompt_id': 'p2', 'baseline_triggered': True, 'with_skill_triggered': False, 'expected_trigger': False, 'routing_ok': True, 'outcome_ok': True},
        ],
        'timestamp': '2026-03-07T00:00:00Z'
    }))
    result = subprocess.run(
        [sys.executable, str(BASE / 'scripts' / 'score_benchmark.py'), str(raw_file)],
        capture_output=True,
        text=True,
        check=True,
    )
    data = json.loads(result.stdout)
    assert data['final_verdict'] == 'effective'
    assert data['aggregate_scores']['trigger_accuracy'] == 1.0
    assert data['aggregate_scores']['routing_clarity'] == 1.0
    assert data['aggregate_scores']['outcome_quality'] == 1.0


def test_write_trend_summary_aggregates_runs(tmp_path):
    summaries_dir = tmp_path / 'summaries'
    summaries_dir.mkdir()
    (summaries_dir / 'one.json').write_text(json.dumps({
        'skill_targets': ['/tmp/demo-skill'],
        'final_verdict': 'partially effective',
        'aggregate_scores': {'trigger_accuracy': 0.5},
        'models': ['claude-sonnet'],
        'timestamp': '2026-03-06T00:00:00Z'
    }))
    (summaries_dir / 'two.json').write_text(json.dumps({
        'skill_targets': ['/tmp/demo-skill'],
        'final_verdict': 'effective',
        'aggregate_scores': {'trigger_accuracy': 0.9},
        'models': ['claude-sonnet', 'gpt-5'],
        'timestamp': '2026-03-07T00:00:00Z'
    }))
    result = subprocess.run(
        [
            sys.executable,
            str(BASE / 'scripts' / 'write_trend_summary.py'),
            '--skill-name', 'demo-skill',
            '--summaries-dir', str(summaries_dir),
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    data = json.loads(result.stdout)
    assert data['skill_name'] == 'demo-skill'
    assert data['run_count'] == 2
    assert data['latest_verdict'] == 'effective'
    assert data['trend_signal'] in {'improving', 'stable'}
    assert 'gpt-5' in data['models_seen']
