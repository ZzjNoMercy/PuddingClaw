import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path('/Users/mac/.claude/skills/skill-benchmark/scripts')))
import claude_cli_executor as exec_mod
import judge_real_results as judge_mod


def test_parse_stream_marks_attempted_but_not_confirmed_on_tool_use_only():
    lines = [
        json.dumps({
            'type': 'assistant',
            'message': {
                'content': [
                    {'type': 'tool_use', 'name': 'Read', 'input': {'file_path': '/Users/mac/.claude/skills/switch-model/SKILL.md'}},
                    {'type': 'tool_use', 'name': 'mcp__llm-router__get_team_config', 'input': {'team_name': 'review-team'}},
                ]
            }
        })
    ]
    parsed = exec_mod.parse_stream_lines(lines, '/Users/mac/.claude/skills/switch-model')
    assert parsed['trace_signals']['skill_attempted'] is True
    assert parsed['trace_signals']['team_mode_attempted'] is True
    assert parsed['trace_signals']['skill_triggered'] is False
    assert parsed['trace_signals']['team_mode_used'] is False


def test_parse_stream_confirms_when_tool_result_follows():
    lines = [
        json.dumps({
            'type': 'assistant',
            'message': {
                'content': [
                    {'type': 'tool_use', 'id': 'toolu_1', 'name': 'Read', 'input': {'file_path': '/Users/mac/.claude/skills/switch-model/SKILL.md'}},
                    {'type': 'tool_use', 'id': 'toolu_2', 'name': 'mcp__llm-router__get_team_config', 'input': {'team_name': 'review-team'}},
                ]
            }
        }),
        json.dumps({
            'type': 'user',
            'message': {
                'content': [
                    {'type': 'tool_result', 'tool_use_id': 'toolu_1', 'content': 'read ok'},
                    {'type': 'tool_result', 'tool_use_id': 'toolu_2', 'content': 'team config ok'},
                ]
            }
        }),
        json.dumps({'type': 'result', 'result': 'final answer'})
    ]
    parsed = exec_mod.parse_stream_lines(lines, '/Users/mac/.claude/skills/switch-model')
    assert parsed['trace_signals']['skill_triggered'] is True
    assert parsed['trace_signals']['team_mode_used'] is True


def test_judge_real_results_uses_confirmed_not_attempted_signals(tmp_path):
    raw = tmp_path / 'raw.json'
    raw.write_text(json.dumps({
        'run_id': 'run-1',
        'skill_targets': ['/Users/mac/.claude/skills/switch-model'],
        'models': ['sonnet'],
        'runs': [
            {
                'prompt_id': 'p1',
                'expected_trigger': True,
                'expected_route': 'switch-model-team',
                'required_output_signals': ['claim_decomposition'],
                'baseline': {
                    'output_text': '',
                    'trace_signals': {
                        'skill_attempted': True,
                        'team_mode_attempted': True,
                        'skill_triggered': False,
                        'team_mode_used': False,
                    }
                },
                'with_skill': {
                    'output_text': '',
                    'trace_signals': {
                        'skill_attempted': True,
                        'team_mode_attempted': True,
                        'skill_triggered': False,
                        'team_mode_used': False,
                    }
                }
            }
        ],
        'timestamp': '2026-03-07T00:00:00Z'
    }))
    summary = judge_mod.judge(raw)
    assert summary['aggregate_scores']['trigger_accuracy'] == 0.0
    assert summary['aggregate_scores']['routing_clarity'] == 0.0
