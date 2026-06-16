from pathlib import Path

BENCH = Path('/Users/mac/.claude/skills/skill-benchmark')
CREATOR = Path('/Users/mac/.claude/skills/skill-creator-pro')


def test_benchmark_has_prompt_template_asset():
    template = BENCH / 'assets' / 'benchmark-prompts-template.json'
    assert template.exists()
    text = template.read_text()
    assert 'should-trigger' in text
    assert 'should-not-trigger' in text
    assert 'comparison' in text


def test_skill_benchmark_routes_to_prompt_template():
    text = (BENCH / 'SKILL.md').read_text()
    assert 'assets/benchmark-prompts-template.json' in text


def test_skill_creator_handoff_mentions_prompt_template():
    text = (CREATOR / 'references' / 'evaluation-handoff.md').read_text()
    assert 'benchmark-prompts-template.json' in text


def test_switch_model_benchmark_prompt_set_exists_and_is_route_sensitive():
    prompt_set = BENCH / 'assets' / 'switch-model-team-prompts.json'
    assert prompt_set.exists()
    text = prompt_set.read_text()
    assert 'switch-model-team' in text
    assert 'required_output_signals' in text
    assert 'claim_decomposition' in text
    assert 'The final answer must explicitly say that team mode was used.' in text


def test_switch_model_concise_prompt_set_exists():
    prompt_set = BENCH / 'assets' / 'switch-model-team-concise.json'
    assert prompt_set.exists()
    text = prompt_set.read_text()
    assert 'switch-model-team' in text
    assert 'Keep the answer under 180 words' in text
    assert 'claim_decomposition' in text


def test_switch_model_intensive_prompt_set_exists_and_mentions_cross_verification():
    prompt_set = BENCH / 'assets' / 'switch-model-team-intensive.json'
    assert prompt_set.exists()
    text = prompt_set.read_text()
    assert 'cross_verification' in text
    assert 'switch-model-team' in text
    assert 'Do not provide a direct single-model answer.' in text
    assert 'Arbitration must name the stronger side and the main uncertainty.' in text
