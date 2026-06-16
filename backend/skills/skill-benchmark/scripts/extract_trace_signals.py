#!/usr/bin/env python3
import json
import sys
from pathlib import Path


def _required_signal_present(output_text: str, signal: str) -> bool:
    mapping = {
        'claim_decomposition': 'claim',
        'disagreement': 'disagreement',
        'arbitration': 'arbitration',
    }
    needle = mapping.get(signal, signal)
    return needle.lower() in output_text.lower()


def _switch_model_team_route_correct(with_skill: dict) -> bool:
    trace = with_skill.get('trace_signals', {})
    if bool(trace.get('team_mode_used')):
        return True
    if not bool(trace.get('skill_triggered')):
        return False
    output_text = with_skill.get('output_text', '').lower()
    fallback_markers = [
        'bash script approach',
        'team mode assessment',
        'models used:',
    ]
    return all(marker in output_text for marker in fallback_markers)


def extract(path: Path) -> dict:
    data = json.loads(path.read_text())
    runs = []
    for run in data.get('runs', []):
        baseline = run.get('baseline', {})
        with_skill = run.get('with_skill', {})
        expected_trigger = bool(run.get('expected_trigger', True))
        expected_route = run.get('expected_route')
        with_trace = with_skill.get('trace_signals', {})
        trigger_correct = bool(with_trace.get('skill_triggered')) == expected_trigger
        route_correct = True
        if expected_route == 'switch-model-team':
            route_correct = _switch_model_team_route_correct(with_skill)
        output_signals = {
            signal: _required_signal_present(with_skill.get('output_text', ''), signal)
            for signal in run.get('required_output_signals', [])
        }
        runs.append({
            'prompt_id': run.get('prompt_id'),
            'trace_verification': {
                'trigger_correct': trigger_correct,
                'route_correct': route_correct,
                'output_signals': output_signals,
            },
        })
    return {
        'run_id': data.get('run_id'),
        'runs': runs,
    }


def main() -> int:
    if len(sys.argv) != 2:
        print('usage: extract_trace_signals.py <raw-run-json>', file=sys.stderr)
        return 2
    print(json.dumps(extract(Path(sys.argv[1])), ensure_ascii=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
