import json
from pathlib import Path

SKILL = Path(__file__).parents[1] / "skills" / "aihot" / "SKILL.md"


def test_aihot_skill_uses_public_readonly_api_without_legacy_script():
    content = SKILL.read_text(encoding="utf-8")

    assert "https://aihot.virxact.com/api/v1/*" in content
    assert "匿名只读请求" in content
    assert "不需要、也不得索要用户的 API Key" in content
    assert "aihot-skill/1.0.0" in content
    assert "普通查询不做版本检查" in content
    assert "不得因为无法设置而拒绝查询或伪装浏览器" in content
    assert "scripts/aihot_query.py" not in content
    assert not SKILL.parent.joinpath("scripts", "aihot_query.py").exists()


def test_aihot_skill_keeps_selected_daily_hot_and_keyword_routes_explicit():
    content = SKILL.read_text(encoding="utf-8")

    assert "/api/v1/items?mode=selected&window=24h" in content
    assert "/api/v1/hot-topics" in content
    assert "/api/v1/dailies/{YYYY-MM-DD}" in content
    assert "/api/v1/items?mode=selected&q=<关键词>&window=<24h|7d>" in content
    assert "只有用户明确说“日报”才用 dailies" in content
    assert "只有用户明确要全部公开动态时才用 `mode=all`" in content


def test_execute_skill_passes_query_and_preserves_sources_before_truncation(tmp_path, monkeypatch):
    # Import lazily because this module depends on LangChain in the backend runtime.
    from tools.execute_skill_tool import ExecuteSkillTool

    skill = tmp_path / "skills" / "demo"
    script = skill / "scripts" / "query.py"
    script.parent.mkdir(parents=True)
    skill.joinpath("SKILL.md").write_text(
        "---\nname: demo\ndescription: demo\n---\n## Resources\n- `scripts/query.py`\n",
        encoding="utf-8",
    )
    script.write_text(
        "import json, os\n"
        "print(json.dumps({'puddingclaw_tool_result': 1, "
        "'answer_context': os.environ.get('SKILL_USER_QUERY', '') + 'x' * 13000, "
        "'sources': [{'title': 'source', 'uri': 'https://example.com'}]}))\n",
        encoding="utf-8",
    )
    tool = ExecuteSkillTool(skills_dir=str(tmp_path / "skills"))
    output = json.loads(tool._run("demo", "本轮问题"))
    assert "本轮问题" in output["answer_context"]
    assert "上下文已截断，来源仍完整保留" in output["answer_context"]
    assert output["sources"][0]["uri"] == "https://example.com"
