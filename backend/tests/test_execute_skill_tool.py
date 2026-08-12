import json

from deepagents.backends.protocol import ExecuteResponse


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

    def runner(skill_id, skill_version, script_relative, user_query):
        assert skill_id == "demo"
        assert skill_version.startswith("sha256-")
        assert script_relative == "scripts/query.py"
        assert user_query == "本轮问题"
        return ExecuteResponse(
            output=json.dumps(
                {
                    "puddingclaw_tool_result": 1,
                    "answer_context": user_query + "x" * 13000,
                    "sources": [{"title": "source", "uri": "https://example.com"}],
                }
            ),
            exit_code=0,
        )

    tool = ExecuteSkillTool(skills_dir=str(tmp_path / "skills"), runner=runner)
    output = json.loads(tool._run("demo", "本轮问题"))
    assert "本轮问题" in output["answer_context"]
    assert "上下文已截断，来源仍完整保留" in output["answer_context"]
    assert output["sources"][0]["uri"] == "https://example.com"
