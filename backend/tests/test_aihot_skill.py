import importlib.util
import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace


SCRIPT = Path(__file__).parents[1] / "skills" / "aihot" / "scripts" / "aihot_query.py"
SPEC = importlib.util.spec_from_file_location("aihot_query", SCRIPT)
assert SPEC and SPEC.loader
aihot = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = aihot
SPEC.loader.exec_module(aihot)


def args(**overrides):
    values = {
        "user_query": "",
        "kind": None,
        "mode": None,
        "category": None,
        "search_query": None,
        "since": None,
        "hours": None,
        "days": None,
        "date": None,
        "take": None,
        "timeout": 1,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_route_defaults_to_selected_items_with_time_window():
    request = aihot.infer_request(args(user_query="今天 AI 圈有什么"))
    assert request["kind"] == "items"
    assert request["mode"] == "selected"
    assert request["take"] == 30
    assert request["since"].endswith("Z")


def test_route_explicit_daily_and_date():
    request = aihot.infer_request(args(user_query="看 2026-06-20 的 AI 日报"))
    assert request == {"kind": "daily", "date": "2026-06-20"}


def test_route_all_papers_and_keyword():
    request = aihot.infer_request(args(user_query="最近一周 OpenAI 的全部论文"))
    assert request["kind"] == "items"
    assert request["mode"] == "all"
    assert request["category"] == "paper"
    assert request["q"] == "OpenAI"


def test_items_formatter_keeps_urls_as_structured_sources():
    context, sources = aihot.format_items({
        "items": [{
            "id": "item-1",
            "title": "测试新闻",
            "url": "https://example.com/news",
            "source": "Example",
            "summary": "这是派生摘要",
            "publishedAt": "2026-06-22T00:00:00Z",
            "selected": True,
        }],
        "hasNext": False,
    }, {"mode": "selected"})
    assert "测试新闻" in context
    assert len(sources) == 1
    assert sources[0]["uri"] == "https://example.com/news"
    assert sources[0]["metadata"]["evidence_kind"] == "derived_summary"
    assert f"[^{sources[0]['source_id']}]" in context


def test_daily_formatter_keeps_source_url():
    _, sources = aihot.format_daily({
        "date": "2026-06-22",
        "sections": [{
            "label": "行业动态",
            "items": [{
                "title": "日报新闻",
                "summary": "摘要",
                "sourceUrl": "https://example.com/daily",
                "sourceName": "Example",
            }],
        }],
    })
    assert [source["uri"] for source in sources] == ["https://example.com/daily"]


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
