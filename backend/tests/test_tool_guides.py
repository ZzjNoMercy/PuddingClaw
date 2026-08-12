from pathlib import Path
from types import SimpleNamespace

from deepagents.middleware._utils import append_to_system_message
from langchain.agents.middleware.types import ModelRequest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage

from graph.middlewares.tool_guides import ToolGuideMiddleware
from graph.middlewares.toolset import ToolsetMiddleware, discover_skill_toolsets

BASE_DIR = Path(__file__).resolve().parents[1]


def _request(*, skills: list[str] | None = None, tools: list[dict[str, str]] | None = None) -> ModelRequest:
    return ModelRequest(
        model=None,
        messages=[HumanMessage(content="test")],
        system_message=SystemMessage(content="base"),
        tools=tools or [{"name": "read_file"}],
        state={"messages": [], "active_skill_ids": skills or []},
    )


def test_manifest_references_valid_unique_guide_files() -> None:
    middleware = ToolGuideMiddleware(base_dir=BASE_DIR)

    assert (middleware.guide_dir / "core.md").is_file()
    assert set(spec.guide_id for spec in middleware.specs) == {
        "database-analysis",
        "semantic-dimension-builds",
        "table-analysis",
        "knowledge-retrieval",
            "managed-lark-autonomy",
            "web-search",
            "webbridge-browser",
        }
    assert all(spec.path.is_file() for spec in middleware.specs)


def test_core_guide_forbids_narrating_cli_install_without_a_tool_call() -> None:
    core = (BASE_DIR / "prompts" / "tool_guides" / "core.md").read_text(
        encoding="utf-8"
    )

    assert "Invoke that command in the same turn" in core
    assert "HITL card is the confirmation boundary" in core
    assert "A narrated intent" in core


def test_no_request_scoped_guide_is_injected_before_activation() -> None:
    middleware = ToolGuideMiddleware(base_dir=BASE_DIR)

    updated = middleware._request_with_guides(_request())

    assert updated.system_message.content == "base"


def test_cache_reorder_reads_text_blocks_without_python_repr() -> None:
    middleware = ToolGuideMiddleware(base_dir=BASE_DIR)
    system_message = append_to_system_message(
        SystemMessage(content="## Current Run Delta\n\nruntime"),
        "## Stable Core\n\ncore",
    )
    request = ModelRequest(
        model=None,
        messages=[HumanMessage(content="test")],
        system_message=system_message,
        tools=[],
        state={"messages": [], "active_skill_ids": []},
    )

    updated = middleware._request_with_guides(request)
    prompt = updated.system_message.text

    assert prompt.index("## Stable Core") < prompt.index("## Current Run Delta")
    assert "{'type': 'text'" not in prompt


def test_active_database_skill_injects_only_database_guide() -> None:
    middleware = ToolGuideMiddleware(base_dir=BASE_DIR)

    updated = middleware._request_with_guides(_request(skills=["database-analysis"]))
    prompt = str(updated.system_message.content)

    assert "Activated Tool Guides (request-scoped)" in prompt
    assert "## Database Analysis" in prompt
    assert "## Table Analysis" not in prompt
    assert "## Managed Lark personal autonomy" not in prompt


def test_filtered_business_tool_is_a_guide_activation_fallback() -> None:
    middleware = ToolGuideMiddleware(base_dir=BASE_DIR)

    updated = middleware._request_with_guides(
        _request(tools=[{"name": "read_file"}, {"name": "database_sql_generate"}])
    )

    assert "## Database Analysis" in str(updated.system_message.content)


def test_web_search_tool_activates_managed_search_guide() -> None:
    middleware = ToolGuideMiddleware(base_dir=BASE_DIR)

    updated = middleware._request_with_guides(
        _request(tools=[{"name": "read_file"}, {"name": "web_search"}])
    )

    prompt = str(updated.system_message.content)
    assert "## Managed Web Search" in prompt
    assert "source=x" in prompt
    assert "allowed_x_handles" in prompt
    assert "time_range=day|week|month|year" in prompt
    assert "enable_image_understanding=true" in prompt
    assert "enable_video_understanding=true" in prompt
    assert "`enable_image_search` is a Web Search capability" in prompt


def test_browser_guide_routes_screenshot_pixels_conditionally() -> None:
    middleware = ToolGuideMiddleware(base_dir=BASE_DIR)

    updated = middleware._request_with_guides(
        _request(tools=[{"name": "read_file"}, {"name": "browser"}])
    )

    prompt = str(updated.system_message.content)
    assert "puddingclaw_visual_route.status=not_analyzed" in prompt
    assert "subagent_type=image_analyzer" in prompt
    assert "do not invoke `image_analyzer`" in prompt
    assert "Never claim to have seen or verified screenshot contents" in prompt
    assert "Never click a comment, avatar, link" in prompt
    assert "PDF is currently a user-facing output artifact" in prompt
    assert "Reconcile evidence after every navigation" in prompt


def test_successful_skill_read_activates_guide_on_next_model_turn(tmp_path: Path) -> None:
    skills_dir = tmp_path / "skills"
    skill_dir = skills_dir / "database-analysis"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\n"
        "name: database-analysis\n"
        "description: Database analysis test Skill.\n"
        "toolsets:\n"
        "  - database_analysis\n"
        "---\n"
        "# Database Analysis\n",
        encoding="utf-8",
    )
    toolset = ToolsetMiddleware(
        skills_dir=skills_dir,
        toolsets_by_skill=discover_skill_toolsets(skills_dir),
    )
    messages = [
        AIMessage(
            content="",
            tool_calls=[
                {
                    "name": "read_file",
                    "args": {"file_path": "/skills/database-analysis/SKILL.md"},
                    "id": "read-database-skill",
                    "type": "tool_call",
                }
            ],
        ),
        ToolMessage(
            content="# Database Analysis",
            name="read_file",
            tool_call_id="read-database-skill",
            status="success",
        ),
    ]
    state = {"messages": messages, "active_skill_ids": [], "skill_activations": []}
    state.update(toolset.before_model(state, SimpleNamespace(context={})) or {})
    request = ModelRequest(
        model=None,
        messages=messages,
        system_message=SystemMessage(content="base"),
        tools=[{"name": "read_file"}, {"name": "database_evidence_search"}],
        state=state,
    )

    filtered = toolset._request_with_capability_manifest(request)
    updated = ToolGuideMiddleware(base_dir=BASE_DIR)._request_with_guides(filtered)

    assert [tool["name"] for tool in filtered.tools] == ["read_file", "database_evidence_search"]
    assert "## Database Analysis" in str(updated.system_message.content)


def test_lark_skill_prefix_injects_autonomy_guide() -> None:
    middleware = ToolGuideMiddleware(base_dir=BASE_DIR)

    updated = middleware._request_with_guides(_request(skills=["lark-im"]))
    prompt = str(updated.system_message.content)

    assert "## Managed Lark personal autonomy" in prompt
    assert "## Database Analysis" not in prompt
