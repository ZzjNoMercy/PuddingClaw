import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path('/Users/mac/.claude/skills/skill-benchmark/scripts')))
import claude_cli_executor as mod


def test_parse_stream_detects_switch_model_attempt_only_without_tool_result():
    lines = [
        json.dumps({
            'type': 'assistant',
            'message': {
                'content': [
                    {'type': 'tool_use', 'name': 'Read', 'input': {'file_path': '/Users/mac/.claude/skills/switch-model/SKILL.md'}},
                    {'type': 'tool_use', 'name': 'mcp__llm-router__get_team_config', 'input': {'team_name': 'review-team'}},
                ]
            }
        }),
        json.dumps({'type': 'result', 'result': 'final answer with claim decomposition, disagreement, and arbitration'})
    ]
    parsed = mod.parse_stream_lines(lines, '/Users/mac/.claude/skills/switch-model')
    assert parsed['trace_signals']['skill_attempted'] is True
    assert parsed['trace_signals']['team_mode_attempted'] is True
    assert parsed['trace_signals']['skill_triggered'] is False
    assert parsed['trace_signals']['team_mode_used'] is False
    assert 'claim decomposition' in parsed['output_text']


def test_parse_stream_without_skill_keeps_flags_false():
    lines = [
        json.dumps({'type': 'assistant', 'message': {'content': []}}),
        json.dumps({'type': 'result', 'result': 'plain baseline output'})
    ]
    parsed = mod.parse_stream_lines(lines, '/Users/mac/.claude/skills/switch-model')
    assert parsed['trace_signals']['skill_triggered'] is False
    assert parsed['trace_signals']['team_mode_used'] is False


def test_executor_timeout_returns_structured_failure():
    original_collect = mod._collect_stream
    mod._collect_stream = lambda cmd, env, timeout_seconds, run_kind: {
        'stdout_lines': [],
        'stderr_lines': [],
        'stdout_tail': [],
        'stderr_tail': [],
        'timed_out': True,
        'returncode': None,
    }
    try:
        prompt_file = Path('/tmp/nonexistent-prompt.json')
        original_read = Path.read_text
        Path.read_text = lambda self: '{"text":"hello"}'
        try:
            data = mod.run(prompt_file, 'sonnet', 'baseline', None)
        finally:
            Path.read_text = original_read
    finally:
        mod._collect_stream = original_collect
    assert data['meta']['timed_out'] is True
    assert data['trace_signals']['skill_triggered'] is False


def test_timeout_preserves_partial_trace_signals():
    partial_line = json.dumps({
        'type': 'assistant',
        'message': {
            'content': [
                {'type': 'tool_use', 'name': 'Read', 'input': {'file_path': '/Users/mac/.claude/skills/switch-model/SKILL.md'}},
                {'type': 'tool_use', 'name': 'mcp__llm-router__get_team_config', 'input': {'team_name': 'review-team'}},
                {'type': 'text', 'text': 'Claim decomposition\\nDisagreement\\nArbitration'},
            ]
        }
    })

    original_collect = mod._collect_stream
    mod._collect_stream = lambda cmd, env, timeout_seconds, run_kind: {
        'stdout_lines': [partial_line],
        'stderr_lines': [],
        'stdout_tail': [partial_line],
        'stderr_tail': [],
        'timed_out': True,
        'returncode': None,
    }
    try:
        prompt_file = Path('/tmp/nonexistent-prompt.json')
        original_read = Path.read_text
        Path.read_text = lambda self: '{"text":"hello"}'
        try:
            data = mod.run(prompt_file, 'sonnet', 'with-skill', '/Users/mac/.claude/skills/switch-model')
        finally:
            Path.read_text = original_read
    finally:
        mod._collect_stream = original_collect
    assert data['meta']['timed_out'] is True
    assert data['trace_signals']['skill_attempted'] is True
    assert data['trace_signals']['team_mode_attempted'] is True
    assert data['trace_signals']['skill_triggered'] is False
    assert data['trace_signals']['team_mode_used'] is False
    assert 'Claim decomposition' in data['output_text']


def test_baseline_command_disallows_llm_router_tools():
    cmd = mod.build_command('hello', 'sonnet', 'baseline')
    rendered = ' '.join(cmd)
    assert '--disallowedTools' in cmd or '--disallowed-tools' in cmd
    assert 'Skill' in rendered
    assert 'Read' in rendered
    assert 'mcp__llm-router__call_llm' in rendered
    assert 'mcp__llm-router__compare_models' in rendered
    assert 'mcp__llm-router__get_team_config' in rendered


def test_with_skill_timeout_budget_is_larger_than_baseline():
    assert mod.timeout_for_run_kind('with-skill') > mod.timeout_for_run_kind('baseline')


def test_with_skill_timeout_budget_is_180_seconds():
    assert mod.timeout_for_run_kind('with-skill') == 180


def test_parse_stream_collects_assistant_text_without_result_event():
    lines = [
        json.dumps({
            'type': 'assistant',
            'message': {
                'content': [
                    {'type': 'text', 'text': 'Claim decomposition\\n'},
                    {'type': 'text', 'text': 'Disagreement\\nArbitration'},
                ]
            }
        })
    ]
    parsed = mod.parse_stream_lines(lines, '/Users/mac/.claude/skills/switch-model')
    assert 'Claim decomposition' in parsed['output_text']
    assert 'Disagreement' in parsed['output_text']
    assert 'Arbitration' in parsed['output_text']
