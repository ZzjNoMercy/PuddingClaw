import json
import subprocess
import sys
from pathlib import Path

BASE = Path('/Users/mac/.claude/skills/skill-benchmark')


def test_skill_routes_to_real_benchmark_resources():
    text = (BASE / 'SKILL.md').read_text()
    assert 'scripts/run_real_benchmark.py' in text
    assert 'scripts/extract_trace_signals.py' in text
    assert 'scripts/judge_real_results.py' in text
    assert 'references/trace-verification.md' in text
    assert 'references/output-assertions.md' in text
    assert 'references/real-benchmark-scenarios.md' in text


def test_run_real_benchmark_executes_baseline_and_with_skill(tmp_path):
    executor = tmp_path / 'executor.py'
    executor.write_text(
        """
import argparse, json
from pathlib import Path
parser = argparse.ArgumentParser()
parser.add_argument('--prompt-file', required=True)
parser.add_argument('--model', required=True)
parser.add_argument('--run-kind', required=True)
parser.add_argument('--skill', default='')
args = parser.parse_args()
prompt = json.loads(Path(args.prompt_file).read_text())
triggered = bool(args.skill)
team_mode = triggered and prompt.get('expected_route') == 'switch-model-team'
output = {
    'output_text': 'claims: yes\\ndisagreement: yes\\narbitration: yes' if triggered else 'plain baseline output',
    'trace_signals': {
        'skill_triggered': triggered,
        'team_mode_used': team_mode,
    },
    'meta': {'model': args.model, 'run_kind': args.run_kind}
}
print(json.dumps(output))
"""
    )
    prompts = tmp_path / 'prompts.json'
    prompts.write_text(json.dumps({
        'prompts': [
            {
                'id': 'p1',
                'text': 'verify claims with cross-model-verifier',
                'should_trigger': True,
                'expected_skill': 'cross-model-verifier',
                'expected_route': 'switch-model-team',
                'required_output_signals': ['claim_decomposition', 'disagreement', 'arbitration'],
            }
        ]
    }))
    out_dir = tmp_path / 'benchmarks'
    result = subprocess.run(
        [
            sys.executable,
            str(BASE / 'scripts' / 'run_real_benchmark.py'),
            '--skill', '/tmp/cross-model-verifier',
            '--mode', 'benchmark-run',
            '--prompts', str(prompts),
            '--model', 'gpt-5',
            '--output-dir', str(out_dir),
            '--executor', str(executor),
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    data = json.loads(result.stdout)
    raw = json.loads(Path(data['raw_file']).read_text())
    run = raw['runs'][0]
    assert run['baseline']['trace_signals']['skill_triggered'] is False
    assert run['with_skill']['trace_signals']['skill_triggered'] is True
    assert run['with_skill']['trace_signals']['team_mode_used'] is True
    assert Path(run['artifacts_dir']).exists()


def test_extract_trace_signals_and_judge_real_results(tmp_path):
    raw_file = tmp_path / 'raw.json'
    raw_file.write_text(json.dumps({
        'run_id': 'run-1',
        'mode': 'benchmark-run',
        'skill_targets': ['/tmp/cross-model-verifier'],
        'models': ['gpt-5'],
        'runs': [
            {
                'prompt_id': 'p1',
                'prompt_text': 'verify claims',
                'expected_trigger': True,
                'expected_skill': 'cross-model-verifier',
                'expected_route': 'switch-model-team',
                'required_output_signals': ['claim_decomposition', 'disagreement', 'arbitration'],
                'baseline': {
                    'output_text': 'plain baseline output',
                    'trace_signals': {'skill_triggered': False, 'team_mode_used': False}
                },
                'with_skill': {
                    'output_text': 'claims: yes\ndisagreement: yes\narbitration: yes',
                    'trace_signals': {'skill_triggered': True, 'team_mode_used': True}
                }
            }
        ],
        'timestamp': '2026-03-07T00:00:00Z'
    }))
    extract = subprocess.run(
        [sys.executable, str(BASE / 'scripts' / 'extract_trace_signals.py'), str(raw_file)],
        capture_output=True,
        text=True,
        check=True,
    )
    extracted = json.loads(extract.stdout)
    assert extracted['runs'][0]['trace_verification']['trigger_correct'] is True
    assert extracted['runs'][0]['trace_verification']['route_correct'] is True

    judged = subprocess.run(
        [sys.executable, str(BASE / 'scripts' / 'judge_real_results.py'), str(raw_file)],
        capture_output=True,
        text=True,
        check=True,
    )
    summary = json.loads(judged.stdout)
    assert summary['aggregate_scores']['trigger_accuracy'] == 1.0
    assert summary['aggregate_scores']['routing_clarity'] == 1.0
    assert summary['aggregate_scores']['output_signal_retention'] == 1.0
    assert summary['final_verdict'] == 'effective'


def test_judge_real_results_accepts_partial_output_text(tmp_path):
    raw_file = tmp_path / 'raw_partial.json'
    raw_file.write_text(json.dumps({
        'run_id': 'run-2',
        'mode': 'benchmark-run',
        'skill_targets': ['/tmp/switch-model'],
        'models': ['sonnet'],
        'runs': [
            {
                'prompt_id': 'p2',
                'prompt_text': 'team mode check',
                'expected_trigger': True,
                'expected_skill': 'switch-model',
                'expected_route': 'switch-model-team',
                'required_output_signals': ['claim_decomposition', 'disagreement', 'arbitration'],
                'baseline': {
                    'output_text': '',
                    'trace_signals': {'skill_triggered': False, 'team_mode_used': False}
                },
                'with_skill': {
                    'output_text': 'Claim decomposition\\nDisagreement\\nArbitration',
                    'trace_signals': {'skill_triggered': True, 'team_mode_used': True},
                    'meta': {'timed_out': True}
                }
            }
        ],
        'timestamp': '2026-03-07T00:00:00Z'
    }))
    judged = subprocess.run(
        [sys.executable, str(BASE / 'scripts' / 'judge_real_results.py'), str(raw_file)],
        capture_output=True,
        text=True,
        check=True,
    )
    summary = json.loads(judged.stdout)
    assert summary['aggregate_scores']['output_signal_retention'] == 1.0
    assert summary['final_verdict'] == 'effective'


def test_extract_and_judge_accept_switch_model_bash_fallback_route(tmp_path):
    raw_file = tmp_path / 'raw_fallback.json'
    raw_file.write_text(json.dumps({
        'run_id': 'run-3',
        'mode': 'benchmark-run',
        'skill_targets': ['/tmp/switch-model'],
        'models': ['sonnet'],
        'runs': [
            {
                'prompt_id': 'p3',
                'prompt_text': 'team mode fallback',
                'expected_trigger': True,
                'expected_skill': 'switch-model',
                'expected_route': 'switch-model-team',
                'required_output_signals': ['claim_decomposition', 'disagreement', 'arbitration'],
                'baseline': {
                    'output_text': '',
                    'trace_signals': {'skill_triggered': False, 'team_mode_used': False}
                },
                'with_skill': {
                    'output_text': (
                        'I will fall back to the Bash script approach as specified in the skill workflow.\\n'
                        'Team Mode Assessment Complete\\n'
                        'Claim Decomposition\\n'
                        'Disagreement\\n'
                        'Arbitration\\n'
                        'Models used: OpenAI GPT-4o, DeepSeek-chat'
                    ),
                    'trace_signals': {'skill_triggered': True, 'team_mode_used': False}
                }
            }
        ],
        'timestamp': '2026-03-07T00:00:00Z'
    }))
    extract = subprocess.run(
        [sys.executable, str(BASE / 'scripts' / 'extract_trace_signals.py'), str(raw_file)],
        capture_output=True,
        text=True,
        check=True,
    )
    extracted = json.loads(extract.stdout)
    assert extracted['runs'][0]['trace_verification']['route_correct'] is True

    judged = subprocess.run(
        [sys.executable, str(BASE / 'scripts' / 'judge_real_results.py'), str(raw_file)],
        capture_output=True,
        text=True,
        check=True,
    )
    summary = json.loads(judged.stdout)
    assert summary['aggregate_scores']['routing_clarity'] == 1.0
    assert summary['aggregate_scores']['output_signal_retention'] == 1.0
    assert summary['final_verdict'] == 'effective'


def test_run_real_benchmark_auto_syncs_summary_and_trend(tmp_path):
    executor = tmp_path / 'executor.py'
    executor.write_text(
        """
import argparse, json
from pathlib import Path
parser = argparse.ArgumentParser()
parser.add_argument('--prompt-file', required=True)
parser.add_argument('--model', required=True)
parser.add_argument('--run-kind', required=True)
parser.add_argument('--skill', default='')
args = parser.parse_args()
prompt = json.loads(Path(args.prompt_file).read_text())
triggered = bool(args.skill)
team_mode = triggered and prompt.get('expected_route') == 'switch-model-team'
output = {
    'output_text': 'Claim Decomposition\\nDisagreement\\nArbitration\\nBash script approach\\nTeam mode assessment\\nModels used: sonnet' if triggered else 'plain baseline output',
    'trace_signals': {
        'skill_triggered': triggered,
        'team_mode_used': team_mode,
    },
    'meta': {'model': args.model, 'run_kind': args.run_kind}
}
print(json.dumps(output))
"""
    )
    prompts = tmp_path / 'prompts.json'
    prompts.write_text(json.dumps({
        'prompts': [
            {
                'id': 'p1',
                'text': 'verify claims with switch-model team mode',
                'should_trigger': True,
                'expected_skill': 'switch-model',
                'expected_route': 'switch-model-team',
                'required_output_signals': ['claim_decomposition', 'disagreement', 'arbitration'],
            }
        ]
    }))
    out_dir = tmp_path / 'benchmarks'
    result = subprocess.run(
        [
            sys.executable,
            str(BASE / 'scripts' / 'run_real_benchmark.py'),
            '--skill', '/tmp/switch-model',
            '--mode', 'benchmark-run',
            '--prompts', str(prompts),
            '--model', 'sonnet',
            '--output-dir', str(out_dir),
            '--executor', str(executor),
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    data = json.loads(result.stdout)
    summary_file = Path(data['summary_file'])
    trend_file = Path(data['trend_files']['switch-model'])
    assert summary_file.exists()
    assert trend_file.exists()
    summary = json.loads(summary_file.read_text())
    trend = json.loads(trend_file.read_text())
    assert summary['final_verdict'] == 'effective'
    assert trend['latest_verdict'] == 'effective'
