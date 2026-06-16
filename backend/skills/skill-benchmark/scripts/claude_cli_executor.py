#!/usr/bin/env python3
import argparse
import json
import os
import selectors
import subprocess
import sys
import time
from pathlib import Path


BASELINE_DISALLOWED_TOOLS = ','.join([
    'Skill',
    'Read',
    'mcp__llm-router__call_llm',
    'mcp__llm-router__compare_models',
    'mcp__llm-router__get_team_config',
    'mcp__llm-router__switch_default',
    'mcp__llm-router__check_health',
    'mcp__llm-router__list_providers',
])

DEFAULT_BASELINE_TIMEOUT_SECONDS = 180
DEFAULT_WITH_SKILL_TIMEOUT_SECONDS = 180
DEFAULT_STREAM_TAIL_LINES = 120


def _bool_env(name: str, default: bool = False) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {'1', 'true', 'yes', 'on'}


def _int_env(name: str, default: int) -> int:
    value = os.environ.get(name)
    if value is None:
        return default
    try:
        parsed = int(value)
    except ValueError:
        return default
    return parsed if parsed > 0 else default


def build_command(prompt_text: str, model: str, run_kind: str) -> list[str]:
    cmd = [
        'claude',
        '-p', prompt_text,
        '--output-format', 'stream-json',
        '--verbose',
    ]
    if _bool_env('SKILL_BENCHMARK_BYPASS_PERMISSIONS', False):
        cmd.extend(['--permission-mode', 'bypassPermissions', '--dangerously-skip-permissions'])
    if model:
        cmd.extend(['--model', model])
    if run_kind == 'baseline':
        cmd.append('--disable-slash-commands')
        cmd.extend(['--disallowedTools', BASELINE_DISALLOWED_TOOLS])
    return cmd


def timeout_for_run_kind(run_kind: str) -> int:
    if run_kind == 'with-skill':
        return _int_env('SKILL_BENCHMARK_WITH_SKILL_TIMEOUT_SECONDS', DEFAULT_WITH_SKILL_TIMEOUT_SECONDS)
    return _int_env('SKILL_BENCHMARK_BASELINE_TIMEOUT_SECONDS', DEFAULT_BASELINE_TIMEOUT_SECONDS)


def parse_stream_lines(lines: list[str], skill_path: str) -> dict:
    skill_attempted = False
    team_mode_attempted = False
    skill_triggered = False
    team_mode_used = False
    output_parts = []
    skill_root = str(Path(skill_path))
    skill_name = Path(skill_path).name if skill_path else ''
    pending = {}
    for raw in lines:
        raw = raw.strip()
        if not raw:
            continue
        try:
            event = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if event.get('type') == 'assistant':
            message = event.get('message', {})
            for item in message.get('content', []):
                if item.get('type') == 'text':
                    text = item.get('text', '')
                    if text:
                        output_parts.append(text)
                    continue
                if item.get('type') != 'tool_use':
                    continue
                tool_id = item.get('id')
                name = item.get('name', '')
                tool_input = item.get('input', {})
                if name in {'Skill', 'Read'}:
                    file_path = tool_input.get('file_path', '') or ''
                    skill_ref = tool_input.get('skill', '') or ''
                    skill_match = False
                    if skill_root and skill_root in file_path:
                        skill_match = True
                    elif skill_name and skill_ref == skill_name:
                        skill_match = True
                    elif 'switch-model' in file_path or skill_ref == 'switch-model':
                        skill_match = True
                    if skill_match:
                        skill_attempted = True
                        if tool_id:
                            pending[tool_id] = 'skill'
                if name == 'mcp__llm-router__get_team_config':
                    team_mode_attempted = True
                    if tool_id:
                        pending[tool_id] = 'team'
        elif event.get('type') == 'user':
            message = event.get('message', {})
            for item in message.get('content', []):
                if item.get('type') != 'tool_result':
                    continue
                tool_id = item.get('tool_use_id')
                if pending.get(tool_id) == 'skill':
                    skill_triggered = True
                if pending.get(tool_id) == 'team':
                    team_mode_used = True
        elif event.get('type') == 'result':
            text = event.get('result', '') or event.get('content', '') or ''
            if text:
                output_parts.append(text)
    return {
        'output_text': ''.join(output_parts),
        'trace_signals': {
            'skill_attempted': skill_attempted,
            'team_mode_attempted': team_mode_attempted,
            'skill_triggered': skill_triggered,
            'team_mode_used': team_mode_used,
        },
    }


def _trim_tail(lines: list[str], limit: int) -> list[str]:
    if len(lines) <= limit:
        return lines
    return lines[-limit:]


def _dump_trace_file(lines: list[str], stderr_lines: list[str], run_kind: str) -> None:
    dump_file = os.environ.get('SKILL_BENCHMARK_TRACE_DUMP_FILE')
    if not dump_file:
        return
    payload = {
        'run_kind': run_kind,
        'stdout_lines': lines,
        'stderr_lines': stderr_lines,
    }
    Path(dump_file).write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding='utf-8')


def _collect_stream(cmd: list[str], env: dict[str, str], timeout_seconds: int, run_kind: str) -> dict:
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
        env=env,
    )
    selector = selectors.DefaultSelector()
    assert proc.stdout is not None
    assert proc.stderr is not None
    selector.register(proc.stdout, selectors.EVENT_READ, data='stdout')
    selector.register(proc.stderr, selectors.EVENT_READ, data='stderr')
    stdout_lines: list[str] = []
    stderr_lines: list[str] = []
    timed_out = False
    deadline = time.monotonic() + timeout_seconds
    while selector.get_map():
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            timed_out = True
            proc.kill()
            break
        events = selector.select(timeout=min(0.25, remaining))
        if not events:
            if proc.poll() is not None:
                break
            continue
        for key, _ in events:
            chunk = key.fileobj.readline()
            if chunk == '':
                selector.unregister(key.fileobj)
                continue
            if key.data == 'stdout':
                stdout_lines.append(chunk.rstrip('\n'))
            else:
                stderr_lines.append(chunk.rstrip('\n'))
    if timed_out:
        try:
            proc.wait(timeout=1)
        except subprocess.TimeoutExpired:
            proc.terminate()
            proc.wait(timeout=1)
    else:
        proc.wait()
    for stream_name, stream in [('stdout', proc.stdout), ('stderr', proc.stderr)]:
        if stream is None:
            continue
        for chunk in stream.readlines():
            if stream_name == 'stdout':
                stdout_lines.append(chunk.rstrip('\n'))
            else:
                stderr_lines.append(chunk.rstrip('\n'))
    tail_limit = _int_env('SKILL_BENCHMARK_STREAM_TAIL_LINES', DEFAULT_STREAM_TAIL_LINES)
    stdout_tail = _trim_tail(stdout_lines, tail_limit)
    stderr_tail = _trim_tail(stderr_lines, tail_limit)
    _dump_trace_file(stdout_tail, stderr_tail, run_kind)
    return {
        'stdout_lines': stdout_lines,
        'stderr_lines': stderr_lines,
        'stdout_tail': stdout_tail,
        'stderr_tail': stderr_tail,
        'timed_out': timed_out,
        'returncode': proc.returncode,
    }


def _finalize(parsed: dict, *, model: str, run_kind: str, cmd: list[str], timeout_seconds: int, timed_out: bool, failed: bool = False, returncode: int | None = None, stderr: str = '', stdout_tail: list[str] | None = None, stderr_tail_lines: list[str] | None = None) -> dict:
    parsed['meta'] = {
        'model': model,
        'run_kind': run_kind,
        'command': cmd,
        'timed_out': timed_out,
        'timeout_seconds': timeout_seconds,
        'failed': failed,
        'returncode': returncode,
        'stderr_tail': stderr[-4000:] if stderr else '',
        'stream_stdout_tail': stdout_tail or [],
        'stream_stderr_tail': stderr_tail_lines or [],
    }
    return parsed


def run(prompt_file: Path, model: str, run_kind: str, skill: str | None) -> dict:
    prompt = json.loads(prompt_file.read_text())
    cmd = build_command(prompt.get('text', ''), model, run_kind)
    env = {k: v for k, v in os.environ.items() if k != 'CLAUDECODE'}
    timeout_seconds = timeout_for_run_kind(run_kind)
    collected = _collect_stream(cmd, env, timeout_seconds, run_kind)
    stdout_lines = collected['stdout_lines']
    stderr_lines = collected['stderr_lines']
    parsed = parse_stream_lines(stdout_lines, skill or '')
    if not parsed.get('output_text') and collected['timed_out']:
        parsed['output_text'] = '[benchmark timeout: no final output captured]'
    elif not parsed.get('output_text') and collected['returncode'] not in (0, None):
        parsed['output_text'] = '[benchmark command failed before final output]'
    failed = bool(collected['timed_out']) or collected['returncode'] not in (0, None)
    return _finalize(
        parsed,
        model=model,
        run_kind=run_kind,
        cmd=cmd,
        timeout_seconds=timeout_seconds,
        timed_out=bool(collected['timed_out']),
        failed=failed,
        returncode=collected['returncode'],
        stderr='\n'.join(stderr_lines),
        stdout_tail=collected['stdout_tail'],
        stderr_tail_lines=collected['stderr_tail'],
    )


def main() -> int:
    parser = argparse.ArgumentParser(description='Claude CLI executor adapter for real benchmarks.')
    parser.add_argument('--prompt-file', required=True)
    parser.add_argument('--model', required=True)
    parser.add_argument('--run-kind', required=True)
    parser.add_argument('--skill', default='')
    args = parser.parse_args()
    data = run(Path(args.prompt_file), args.model, args.run_kind, args.skill or None)
    print(json.dumps(data, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
