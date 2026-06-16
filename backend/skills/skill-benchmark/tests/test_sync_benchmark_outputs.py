import json
import subprocess
import sys
from pathlib import Path

BASE = Path('/Users/mac/.claude/skills/skill-benchmark')


def test_sync_benchmark_outputs_generates_summary_and_trend(tmp_path):
    raw_dir = tmp_path / 'raw'
    summaries_dir = tmp_path / 'summaries'
    trends_dir = tmp_path / 'trends'
    raw_dir.mkdir()
    summaries_dir.mkdir()
    trends_dir.mkdir()

    raw_file = raw_dir / 'real-demo.json'
    raw_file.write_text(json.dumps({
        'run_id': 'real-demo',
        'mode': 'benchmark-run',
        'skill_targets': ['/tmp/switch-model'],
        'models': ['sonnet'],
        'runs': [
            {
                'prompt_id': 'p1',
                'expected_trigger': True,
                'expected_route': 'switch-model-team',
                'required_output_signals': ['claim_decomposition', 'disagreement', 'arbitration'],
                'baseline': {
                    'output_text': 'plain answer',
                    'trace_signals': {
                        'skill_triggered': False,
                        'team_mode_used': False,
                    },
                },
                'with_skill': {
                    'output_text': 'Claim Decomposition\\nDisagreement\\nArbitration\\nBash script approach\\nTeam mode assessment\\nModels used: sonnet',
                    'trace_signals': {
                        'skill_triggered': True,
                        'team_mode_used': True,
                    },
                },
            }
        ],
        'timestamp': '2026-03-08T00:00:00Z',
    }))

    result = subprocess.run(
        [
            sys.executable,
            str(BASE / 'scripts' / 'sync_benchmark_outputs.py'),
            '--raw-dir', str(raw_dir),
            '--summaries-dir', str(summaries_dir),
            '--trends-dir', str(trends_dir),
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    payload = json.loads(result.stdout)
    assert payload['processed_runs'] == 1
    assert payload['generated_summaries'] == 1
    assert payload['summary_changed'] == 1
    assert payload['trend_changed'] == 1
    summary_file = summaries_dir / 'real-demo.json'
    assert summary_file.exists()
    summary = json.loads(summary_file.read_text())
    assert summary['final_verdict'] == 'effective'

    trend_file = trends_dir / 'switch-model.json'
    assert trend_file.exists()
    trend = json.loads(trend_file.read_text())
    assert trend['skill_name'] == 'switch-model'
    assert trend['run_count'] == 1
    assert trend['latest_verdict'] == 'effective'


def test_sync_benchmark_outputs_reports_no_change_on_second_run(tmp_path):
    raw_dir = tmp_path / 'raw'
    summaries_dir = tmp_path / 'summaries'
    trends_dir = tmp_path / 'trends'
    raw_dir.mkdir()
    summaries_dir.mkdir()
    trends_dir.mkdir()

    raw_file = raw_dir / 'real-demo.json'
    raw_file.write_text(json.dumps({
        'run_id': 'real-demo',
        'mode': 'benchmark-run',
        'skill_targets': ['/tmp/switch-model'],
        'models': ['sonnet'],
        'runs': [
            {
                'prompt_id': 'p1',
                'expected_trigger': True,
                'expected_route': 'switch-model-team',
                'required_output_signals': ['claim_decomposition', 'disagreement', 'arbitration'],
                'baseline': {
                    'output_text': 'plain answer',
                    'trace_signals': {
                        'skill_triggered': False,
                        'team_mode_used': False,
                    },
                },
                'with_skill': {
                    'output_text': 'Claim Decomposition\\nDisagreement\\nArbitration\\nBash script approach\\nTeam mode assessment\\nModels used: sonnet',
                    'trace_signals': {
                        'skill_triggered': True,
                        'team_mode_used': True,
                    },
                },
            }
        ],
        'timestamp': '2026-03-08T00:00:00Z',
    }))

    cmd = [
        sys.executable,
        str(BASE / 'scripts' / 'sync_benchmark_outputs.py'),
        '--raw-dir', str(raw_dir),
        '--summaries-dir', str(summaries_dir),
        '--trends-dir', str(trends_dir),
    ]
    subprocess.run(cmd, capture_output=True, text=True, check=True)
    result = subprocess.run(cmd, capture_output=True, text=True, check=True)
    payload = json.loads(result.stdout)
    assert payload['processed_runs'] == 1
    assert payload['generated_summaries'] == 1
    assert payload['summary_changed'] == 0
    assert payload['trend_changed'] == 0
