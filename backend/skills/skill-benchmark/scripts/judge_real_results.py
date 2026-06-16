#!/usr/bin/env python3
"""Judge real benchmark runs with task-completion metrics instead of output length."""

import json
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
SYNONYMS_FILE = SCRIPT_DIR.parent / 'references' / 'signal-synonyms.json'

_synonyms_cache = None


def _load_synonyms() -> dict:
    """Load synonym mapping from external JSON file. Cached after first call."""
    global _synonyms_cache
    if _synonyms_cache is not None:
        return _synonyms_cache
    if SYNONYMS_FILE.exists():
        try:
            data = json.loads(SYNONYMS_FILE.read_text(encoding='utf-8'))
            # Strip the _comment key
            _synonyms_cache = {k: v for k, v in data.items() if not k.startswith('_')}
        except (json.JSONDecodeError, OSError):
            _synonyms_cache = {}
    else:
        _synonyms_cache = {}
    return _synonyms_cache


def _ratio(flags):
    flags = list(flags)
    if not flags:
        return 0.0
    return round(sum(1 for flag in flags if flag) / len(flags), 4)


def _signal_present(output_text: str, signal: str) -> bool:
    """Check if a signal keyword is present in output, with synonym support from external file."""
    lowered = output_text.lower()
    needle = signal.lower()

    # Direct match
    if needle in lowered:
        return True

    # Synonym match from references/signal-synonyms.json
    synonyms = _load_synonyms()
    alts = synonyms.get(needle, [])
    if alts:
        return any(alt.lower() in lowered for alt in alts)

    return False


def _task_completed(run_result: dict) -> bool:
    """Determine if the task was actually completed (not just started or timed out)."""
    meta = run_result.get('meta', {})

    # Timed out = not completed
    if meta.get('timed_out', False):
        return False

    # Failed with non-zero exit = not completed
    if meta.get('failed', False):
        return False

    # Has meaningful output (more than just a stub)
    output = run_result.get('output_text', '')
    if len(output) < 50:
        return False

    return True


def _output_is_structured(output_text: str) -> bool:
    """Check if output shows structured response (headers, lists, tables, code blocks)."""
    markers = ['##', '- ', '| ', '```', '1.', '2.', '**']
    return sum(1 for m in markers if m in output_text) >= 2


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


def judge(path: Path) -> dict:
    data = json.loads(path.read_text())
    trigger_flags = []
    route_flags = []

    # Per-signal tracking instead of all-or-nothing
    signal_hits = 0
    signal_total = 0

    # Task completion tracking
    baseline_completion_flags = []
    withskill_completion_flags = []

    # Outcome quality tracking
    outcome_flags = []

    per_run_details = []

    for run in data.get('runs', []):
        baseline = run.get('baseline', {})
        with_skill = run.get('with_skill', {})
        with_trace = with_skill.get('trace_signals', {})
        expected_trigger = bool(run.get('expected_trigger', True))

        # --- Dimension 1: Trigger Accuracy ---
        trigger_flags.append(bool(with_trace.get('skill_triggered')) == expected_trigger)

        # --- Dimension 2: Routing Clarity ---
        expected_route = run.get('expected_route')
        if expected_route == 'switch-model-team':
            route_flags.append(_switch_model_team_route_correct(with_skill))
        else:
            route_flags.append(True)

        # --- Dimension 3: Output Signal Retention (per-signal) ---
        output_text = with_skill.get('output_text', '')
        needed = run.get('required_output_signals', [])
        run_signal_hits = []
        for signal in needed:
            hit = _signal_present(output_text, signal)
            run_signal_hits.append({'signal': signal, 'hit': hit})
            signal_total += 1
            if hit:
                signal_hits += 1

        # --- Dimension 4: Task Completion Rate ---
        b_completed = _task_completed(baseline)
        w_completed = _task_completed(with_skill)
        baseline_completion_flags.append(b_completed)
        withskill_completion_flags.append(w_completed)

        # --- Dimension 5: Outcome Quality (structured + complete + correct) ---
        outcome_score = 0.0
        if w_completed:
            outcome_score += 0.5  # Task finished
        if _output_is_structured(output_text):
            outcome_score += 0.25  # Output is structured
        if needed and all(s['hit'] for s in run_signal_hits):
            outcome_score += 0.25  # All required signals present
        elif not needed and w_completed:
            outcome_score += 0.25  # No signals required, completion is enough
        outcome_flags.append(outcome_score >= 0.5)

        per_run_details.append({
            'prompt_id': run.get('prompt_id'),
            'trigger_correct': trigger_flags[-1],
            'route_correct': route_flags[-1],
            'signals': run_signal_hits,
            'baseline_completed': b_completed,
            'withskill_completed': w_completed,
            'outcome_score': outcome_score,
        })

    trigger_accuracy = _ratio(trigger_flags)
    routing_clarity = _ratio(route_flags)
    signal_retention = round(signal_hits / signal_total, 4) if signal_total > 0 else 1.0
    baseline_completion = _ratio(baseline_completion_flags)
    withskill_completion = _ratio(withskill_completion_flags)
    outcome_quality = _ratio(outcome_flags)

    # Completion improvement: how much better with-skill is vs baseline
    completion_gain = round(withskill_completion - baseline_completion, 4)

    # Final verdict based on 4 real dimensions (exclude model_robustness and consistency)
    core_avg = round((trigger_accuracy + routing_clarity + signal_retention + outcome_quality) / 4, 4)
    if core_avg >= 0.85:
        verdict = 'effective'
    elif core_avg >= 0.60:
        verdict = 'partially effective'
    elif core_avg >= 0.35:
        verdict = 'not proven'
    else:
        verdict = 'ineffective'

    return {
        'run_id': data.get('run_id'),
        'skill_targets': data.get('skill_targets', []),
        'models': data.get('models', []),
        'aggregate_scores': {
            'trigger_accuracy': trigger_accuracy,
            'routing_clarity': routing_clarity,
            'output_signal_retention': signal_retention,
            'task_completion': {
                'baseline': baseline_completion,
                'with_skill': withskill_completion,
                'gain': completion_gain,
            },
            'outcome_quality': outcome_quality,
        },
        'final_verdict': verdict,
        'per_run_details': per_run_details,
        'timestamp': data.get('timestamp'),
    }


def main() -> int:
    if len(sys.argv) != 2:
        print('usage: judge_real_results.py <raw-run-json>', file=sys.stderr)
        return 2
    result = judge(Path(sys.argv[1]))
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
