import json
from pathlib import Path
from types import SimpleNamespace

from langchain.agents.middleware.types import ModelRequest
from langchain_core.messages import HumanMessage, SystemMessage

import config
from graph.deepagents_manager import DeepAgentsAgentManager
from graph.middlewares.skill_intent_router import SkillIntentRouterMiddleware
from graph.middlewares.toolset import ToolsetMiddleware
from graph.prompt_cache import (
    append_control_message,
    build_part_fingerprints,
    compare_part_inputs,
    reorder_system_prompt_sections,
)
from graph.session_manager import session_manager
from harness.models import RunTaskProfile, SkillCandidate


def _enable_prompt_cache(monkeypatch, tmp_path: Path) -> None:
    config_path = tmp_path / "config.json"
    config_path.write_text(
        json.dumps(
            {
                "harness": {
                    "prompt_cache": {
                        "trace_part_diagnostics": True,
                        "ordered_system_sections": True,
                        "tail_routing_message": True,
                        "deterministic_session_projection": True,
                        "stable_tool_schema": True,
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(config, "CONFIG_FILE", config_path)


def test_cache_safe_request_layout_is_enabled_by_default(monkeypatch, tmp_path: Path) -> None:
    config_path = tmp_path / "config.json"
    config_path.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(config, "CONFIG_FILE", config_path)

    prompt_cache = config.load_config()["harness"]["prompt_cache"]
    assert prompt_cache["ordered_system_sections"] is True
    assert prompt_cache["tail_routing_message"] is True
    assert prompt_cache["deterministic_session_projection"] is True
    assert prompt_cache["stable_tool_schema"] is False


def test_part_fingerprints_and_first_diff_are_deterministic() -> None:
    kwargs = {
        "system_prompt": "## Stable Core\ncore\n## Current Capability Manifest\n{}",
        "tool_schemas": [{"name": "read_file", "parameters": {"type": "object"}}],
        "message_previews": [{"role": "user", "content": "same", "chars": 4}],
    }
    first = build_part_fingerprints(**kwargs)
    second = build_part_fingerprints(**kwargs)
    assert first == second

    changed = dict(kwargs)
    changed["system_prompt"] = kwargs["system_prompt"] + "\nchanged"
    current = build_part_fingerprints(**changed)
    diff = compare_part_inputs(
        {"messages_preview": kwargs["message_previews"], "fingerprints": first},
        {"messages_preview": changed["message_previews"], "fingerprints": current},
    )
    assert diff["first_diff_part"] == "system"


def test_system_sections_collect_repeated_run_deltas_and_keep_messages_separate() -> None:
    prompt = (
        "## Stable Core\ncore\n"
        "## Current Run Delta\nfirst\n"
        "## Project Context\nproject\n"
        "## Current Run Delta\nsecond\n"
        "## Active Skill Instructions\nskill\n"
    )
    changed = prompt.replace("second", "changed")
    first = build_part_fingerprints(
        system_prompt=prompt,
        tool_schemas=[],
        message_previews=[
            {"role": "system", "content": prompt, "chars": len(prompt)},
            {"role": "user", "content": "same", "chars": 4},
        ],
    )
    second = build_part_fingerprints(
        system_prompt=changed,
        tool_schemas=[],
        message_previews=[
            {"role": "system", "content": changed, "chars": len(changed)},
            {"role": "user", "content": "same", "chars": 4},
        ],
    )
    assert first["system_volatile_tail_hash"] != second["system_volatile_tail_hash"]
    assert first["messages_history_hash"] == second["messages_history_hash"]
    ordered = reorder_system_prompt_sections(prompt)
    assert ordered.index("## Stable Core") < ordered.index("## Project Context")
    assert ordered.index("## Project Context") < ordered.index("## Active Skill Instructions")
    assert ordered.rindex("## Current Run Delta") > ordered.index("## Active Skill Instructions")


def test_tail_control_preserves_user_message_and_has_one_sorted_tail() -> None:
    original = HumanMessage(content="用户原文")
    messages = append_control_message([original], section="z", content="z")
    messages = append_control_message(messages, section="a", content="a")
    assert messages[0] is original
    assert messages[0].content == "用户原文"
    assert len(messages) == 2
    assert messages[-1].content.index("a") < messages[-1].content.index("z")


def test_tail_routing_does_not_rewrite_human_message(monkeypatch, tmp_path: Path) -> None:
    _enable_prompt_cache(monkeypatch, tmp_path)
    skills = tmp_path / "skills"
    skill = skills / "database-analysis"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text(
        "---\nname: database-analysis\ndescription: test\ntoolsets:\n  - database_analysis\n---\n# Skill",
        encoding="utf-8",
    )
    profile = RunTaskProfile(
        skill_candidates=[SkillCandidate(skill_id="database-analysis", confidence=0.9, evidence="database")]
    )
    request = ModelRequest(
        model=None,
        messages=[HumanMessage(content="查询销量")],
        system_message=SystemMessage(content="base"),
        tools=[{"name": "read_file"}, {"name": "database_sql_generate"}],
        state={"messages": [], "active_skill_ids": [], "task_profile": profile.model_dump(mode="json")},
        runtime=SimpleNamespace(context={"run_id": "run-1"}),
    )
    routed = SkillIntentRouterMiddleware()._request_with_routing_prompt(request)
    updated = ToolsetMiddleware(
        skills_dir=skills,
        toolsets_by_skill={"database-analysis": {"database_analysis"}},
    )._request_with_capability_manifest(routed)
    assert updated.messages[0].content == "查询销量"
    assert updated.messages[-1].additional_kwargs["puddingclaw_prompt_control"] is True


def test_deterministic_projection_does_not_merge_historical_assistants(monkeypatch, tmp_path: Path) -> None:
    _enable_prompt_cache(monkeypatch, tmp_path)
    session_manager.initialize(tmp_path)
    session_manager.create_session("projection")
    session_manager.save_message("projection", "user", "q")
    session_manager.save_message("projection", "assistant", "a1")
    session_manager.save_message("projection", "assistant", "a2")
    history = session_manager.load_session_for_agent("projection", current_run_id="run-1")
    assert [item["content"] for item in history] == ["q", "a1", "a2"]


def test_ready_tool_context_is_selected_without_budget_reinterpretation() -> None:
    history = [
        {
            "role": "assistant",
            "content": "读取完成",
            "query_id": "q1",
            "tool_calls": [
                {
                    "id": "call-1",
                    "tool": "read_file",
                    "input": {"file_path": "/workspace/a.txt"},
                    "output": "raw output",
                    "source_hash": "sha256:raw",
                    "context_output": "stable compact output",
                    "context_compaction": {"status": "ready", "source_hash": "sha256:raw"},
                }
            ],
        }
    ]
    messages = DeepAgentsAgentManager._build_messages(history, "继续", session_id="s", query_id="q2")
    assert any("stable compact output" in str(message.content) for message in messages)
