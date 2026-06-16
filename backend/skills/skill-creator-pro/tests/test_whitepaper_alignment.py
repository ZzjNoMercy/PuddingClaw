from pathlib import Path

SKILL_DIR = Path('/Users/mac/.claude/skills/skill-creator-pro')


def test_skill_routes_paradigm_selection_before_restructure():
    text = (SKILL_DIR / 'SKILL.md').read_text()
    assert 'Choose the primary paradigm before restructuring the skill.' in text


def test_skill_routes_whitepaper_references():
    text = (SKILL_DIR / 'SKILL.md').read_text()
    for ref in [
        'references/skill-paradigms.md',
        'references/module-building-blocks.md',
        'references/to-do-constitution.md',
        'references/not-to-do-red-lines.md',
    ]:
        assert ref in text


def test_whitepaper_reference_files_exist():
    for rel in [
        'references/skill-paradigms.md',
        'references/module-building-blocks.md',
        'references/to-do-constitution.md',
        'references/not-to-do-red-lines.md',
    ]:
        assert (SKILL_DIR / rel).exists()


def test_paradigm_reference_contains_whitepaper_paradigms():
    text = (SKILL_DIR / 'references' / 'skill-paradigms.md').read_text()
    for paradigm in ['Navigator', 'Operator', 'Partner', 'Scout', 'Architect', 'Orchestrator']:
        assert paradigm in text


def test_module_reference_contains_whitepaper_blocks():
    text = (SKILL_DIR / 'references' / 'module-building-blocks.md').read_text()
    for block in ['Identity', 'Interaction', 'Decision', 'Doctrine', 'Routing Layer', 'Control Layer', 'Execution Support']:
        assert block in text
