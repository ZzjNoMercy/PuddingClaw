from pathlib import Path

BASE = Path('/Users/mac/.claude/skills/skill-creator-pro')


def test_request_templates_reference_exists():
    ref = BASE / 'references' / 'request-templates.md'
    assert ref.exists()
    text = ref.read_text()
    assert '通用创建模板' in text
    assert '极简模板' in text
    assert '产品型 skill 模板' in text
    assert '开发型 skill 模板' in text
    assert '内容工作流 skill 模板' in text


def test_skill_md_routes_to_request_templates_and_missing_info_behavior():
    text = (BASE / 'SKILL.md').read_text()
    assert 'references/request-templates.md' in text
    assert '信息不全' in text or 'missing information' in text
