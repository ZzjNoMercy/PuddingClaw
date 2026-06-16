from pathlib import Path

BASE = Path('/Users/mac/.claude/skills/skill-creator-pro')


def test_skill_md_mentions_skill_benchmark_handoff():
    text = (BASE / 'SKILL.md').read_text()
    assert 'skill-benchmark' in text
    assert 'baseline-vs-with-skill' in text or 'baseline vs with-skill' in text


def test_resources_include_benchmark_handoff_reference():
    text = (BASE / 'SKILL.md').read_text()
    assert 'references/evaluation-handoff.md' in text
    assert (BASE / 'references' / 'evaluation-handoff.md').exists()
