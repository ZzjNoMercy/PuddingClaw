import json
import subprocess
import sys
from pathlib import Path

BASE = Path('/Users/mac/.claude/skills/skill-benchmark')


def test_skill_has_standard_sections():
    skill = (BASE / 'SKILL.md')
    assert skill.exists()
    text = skill.read_text()
    for section in ['## Goal', '## Workflow', '## Decision Tree', '## Constraints', '## Validation', '## Resources']:
        assert section in text


def test_openai_yaml_exists():
    assert (BASE / 'agents' / 'openai.yaml').exists()


def test_candidate_check_accepts_local_skill(tmp_path):
    skill_dir = tmp_path / 'demo-skill'
    skill_dir.mkdir()
    (skill_dir / 'SKILL.md').write_text('---\nname: demo-skill\ndescription: demo\n---\n')
    result = subprocess.run(
        [sys.executable, str(BASE / 'scripts' / 'candidate_check.py'), str(skill_dir)],
        capture_output=True,
        text=True,
        check=True,
    )
    data = json.loads(result.stdout)
    assert data['eligible'] is True
    assert data['candidate_type'] == 'local-skill'


def test_benchmark_level_classifies_compare_mode():
    result = subprocess.run(
        [sys.executable, str(BASE / 'scripts' / 'benchmark_level.py'), 'compare two skills across models'],
        capture_output=True,
        text=True,
        check=True,
    )
    data = json.loads(result.stdout)
    assert data['mode'] == 'compare-skills'


def test_aggregate_results_summarizes_effective_runs(tmp_path):
    result_file = tmp_path / 'run.json'
    result_file.write_text(json.dumps({
        'skill_name': 'demo-skill',
        'runs': [
            {'verdict': 'effective', 'model': 'gpt-x'},
            {'verdict': 'partially effective', 'model': 'claude-y'},
            {'verdict': 'effective', 'model': 'gpt-x'},
        ]
    }))
    result = subprocess.run(
        [sys.executable, str(BASE / 'scripts' / 'aggregate_results.py'), str(result_file)],
        capture_output=True,
        text=True,
        check=True,
    )
    data = json.loads(result.stdout)
    assert data['skill_name'] == 'demo-skill'
    assert data['verdict_counts']['effective'] == 2
    assert data['verdict_counts']['partially effective'] == 1
