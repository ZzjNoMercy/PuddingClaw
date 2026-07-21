from pathlib import Path
from types import SimpleNamespace

from langchain.agents.middleware.types import ModelRequest, ToolCallRequest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from graph.middlewares.skill_intent_router import SkillIntentRouterMiddleware
from graph.middlewares.toolset import (
    ToolsetMiddleware,
    discover_skill_catalog,
    discover_skill_toolsets,
)
from harness.models import RunTaskProfile, SkillCandidate
from harness.tool_execution import ToolExecutionPipeline
from tools.toolsets import (
    BUSINESS_TOOLSETS,
    DEFAULT_CUSTOM_TOOL_NAMES,
    TOOL_CONTROL_DESCRIPTORS,
    TOOLSETS,
    UNCONDITIONAL_EXTENSION_TOOLSETS,
    UNCONDITIONAL_TOOL_NAMES,
    agent_custom_tool_names,
    business_tool_names,
    tools_for_toolsets,
    validate_tool_control_descriptors,
)


def test_project_skill_frontmatter_declares_known_toolsets() -> None:
    skills = discover_skill_toolsets(Path(__file__).resolve().parents[1] / "skills")

    assert skills["build-semantic-dimension"] == {"semantic_dimension_build", "semantic_lookup"}
    assert skills["build-logical-dataset"] == {"logical_dataset"}
    assert skills["database-analysis"] == {"database_analysis", "semantic_lookup"}
    assert skills["skill-management"] == {"skill_management"}
    assert all(name in TOOLSETS for values in skills.values() for name in values)


def test_skill_catalog_is_discovered_from_installed_frontmatter() -> None:
    catalog = discover_skill_catalog(Path(__file__).resolve().parents[1] / "skills")
    by_id = {item["skill_id"]: item for item in catalog}

    assert by_id["database-analysis"]["name"] == "database-analysis"
    assert "relational data" in by_id["database-analysis"]["description"]
    assert by_id["database-analysis"]["path"] == (
        "/skills/database-analysis/SKILL.md"
    )


def test_tavily_skill_uses_native_controlled_tool() -> None:
    skill_path = Path(__file__).resolve().parents[1] / "skills" / "tavily-search" / "SKILL.md"
    instructions = skill_path.read_text(encoding="utf-8")

    assert "Use the platform-native `tavily_search` tool" in instructions
    assert "python3 {baseDir}/scripts/tavily_search.py" not in instructions
    assert "Do not ask the user for `TAVILY_API_KEY`" in instructions


def test_every_registered_agent_custom_tool_has_an_explicit_policy() -> None:
    assert agent_custom_tool_names() == business_tool_names() | DEFAULT_CUSTOM_TOOL_NAMES
    assert {
        "prepare_skill_install",
        "install_skill",
        "prepare_skill_update",
        "update_skill",
        "inspect_skill",
    }.issubset(BUSINESS_TOOLSETS["skill_management"])
    assert "edit_file" not in UNCONDITIONAL_TOOL_NAMES
    assert {
        "inspect_file_version",
        "patch_file",
        "stage_external_artifact",
        "commit_external_artifact",
        "prepare_attachment_edit",
        "publish_attachment",
        "stage_external_directory",
        "prepare_external_directory_commit",
        "commit_external_directory",
    }.issubset(UNCONDITIONAL_TOOL_NAMES)

    owners: dict[str, list[str]] = {}
    for toolset, tool_names in BUSINESS_TOOLSETS.items():
        for tool_name in tool_names:
            owners.setdefault(tool_name, []).append(toolset)
    assert {name: values for name, values in owners.items() if len(values) != 1} == {}


def test_default_harness_file_toolset_is_registered_with_execution_pipeline() -> None:
    harness_file_tools = UNCONDITIONAL_EXTENSION_TOOLSETS["harness_files"]

    assert harness_file_tools <= ToolExecutionPipeline.BUILTIN_TOOLS
    assert harness_file_tools <= ToolExecutionPipeline.DECLARED_ALLOW_TOOLS


def test_every_registered_tool_declares_a_control_descriptor() -> None:
    assert validate_tool_control_descriptors() == []
    assert TOOL_CONTROL_DESCRIPTORS["execute"].policy == "dynamic"
    assert TOOL_CONTROL_DESCRIPTORS["task"].policy == "inherit_parent"
    assert TOOL_CONTROL_DESCRIPTORS["commit_external_artifact"].side_effect == "external_mutation"


def test_toolset_activates_only_after_successfully_reading_skill_file(tmp_path) -> None:
    middleware = ToolsetMiddleware(
        skills_dir=tmp_path,
        toolsets_by_skill={"database-analysis": {"database_analysis", "semantic_lookup"}},
    )
    messages = [
        AIMessage(
            content="",
            tool_calls=[{
                "name": "read_file",
                "args": {"file_path": "/skills/database-analysis/SKILL.md"},
                "id": "call_skill",
                "type": "tool_call",
            }],
        ),
        ToolMessage(content="# Database Analysis", name="read_file", tool_call_id="call_skill", status="success"),
    ]

    assert middleware._loaded_skill_ids(messages) == ["database-analysis"]
    assert tools_for_toolsets({"database_analysis", "semantic_lookup"}) | UNCONDITIONAL_TOOL_NAMES >= {
        "database_sql_generate",
        "semantic_entity_lookup",
        "execute",
    }


def test_unloaded_business_tools_are_hidden_from_model_request(tmp_path) -> None:
    middleware = ToolsetMiddleware(
        skills_dir=tmp_path,
        toolsets_by_skill={"database-analysis": {"database_analysis"}},
    )
    tools = [{"name": "read_file"}, {"name": "execute"}, {"name": "database_sql_generate"}]
    request = ModelRequest(model=None, messages=[], tools=tools, state={"messages": []})

    assert [tool["name"] for tool in middleware._visible_tools(request)] == ["read_file", "execute"]


def test_skill_management_tools_are_visible_only_after_skill_activation(tmp_path) -> None:
    middleware = ToolsetMiddleware(
        skills_dir=tmp_path,
        toolsets_by_skill={"skill-management": {"skill_management"}},
    )
    tools = [
        {"name": "inspect_skill"},
        {"name": "prepare_skill_install"},
        {"name": "install_skill"},
        {"name": "prepare_skill_update"},
        {"name": "update_skill"},
    ]

    inactive = ModelRequest(
        model=None,
        messages=[],
        tools=tools,
        state={"messages": [], "active_skill_ids": []},
    )
    active = ModelRequest(
        model=None,
        messages=[],
        tools=tools,
        state={"messages": [], "active_skill_ids": ["skill-management"]},
    )

    assert middleware._visible_tools(inactive) == []
    assert [tool["name"] for tool in middleware._visible_tools(active)] == [
        "inspect_skill",
        "prepare_skill_install",
        "install_skill",
        "prepare_skill_update",
        "update_skill",
    ]


def test_prior_turn_skill_reads_remain_active_in_same_session(tmp_path) -> None:
    middleware = ToolsetMiddleware(
        skills_dir=tmp_path,
        toolsets_by_skill={"database-analysis": {"database_analysis"}},
    )
    history = [
        AIMessage(content="", tool_calls=[{"name": "read_file", "args": {"file_path": "/skills/database-analysis/SKILL.md"}, "id": "old", "type": "tool_call"}]),
        ToolMessage(content="# old", name="read_file", tool_call_id="old", status="success"),
    ]
    assert middleware._loaded_skill_ids(history) == ["database-analysis"]
    assert middleware.before_agent({"messages": history}, runtime=None) == {
        "active_skill_ids": ["database-analysis"]
    }


def test_session_cached_skill_activates_without_reloading(
    tmp_path,
    monkeypatch,
) -> None:
    import graph.middlewares.toolset as module

    middleware = ToolsetMiddleware(
        skills_dir=tmp_path,
        toolsets_by_skill={"database-analysis": {"database_analysis"}},
    )
    monkeypatch.setattr(
        module.session_manager,
        "get_loaded_skill_ids",
        lambda session_id: ["database-analysis"] if session_id == "session-1" else [],
    )

    update = middleware.before_agent(
        {"messages": []},
        runtime=SimpleNamespace(context={"session_id": "session-1"}),
    )

    assert update == {"active_skill_ids": ["database-analysis"]}
    assert "database_sql_generate" in middleware._allowed_tool_names({"messages": [], **update})


def test_successful_skill_read_is_persisted_to_session_cache(
    tmp_path,
    monkeypatch,
) -> None:
    import graph.middlewares.toolset as module

    middleware = ToolsetMiddleware(
        skills_dir=tmp_path,
        toolsets_by_skill={"database-analysis": {"database_analysis"}},
    )
    persisted: list[tuple[str, list[str]]] = []
    selected: list[tuple[str, str, str]] = []
    monkeypatch.setattr(
        module.session_manager,
        "add_loaded_skill_ids",
        lambda session_id, skill_ids: persisted.append((session_id, skill_ids)) or skill_ids,
    )
    monkeypatch.setattr(
        module.session_manager,
        "record_run_skill_selection",
        lambda session_id, run_id, skill_id: selected.append(
            (session_id, run_id, skill_id)
        ),
    )
    request = ToolCallRequest(
        tool_call={
            "name": "read_file",
            "args": {"file_path": "/skills/database-analysis/SKILL.md"},
            "id": "read-skill",
            "type": "tool_call",
        },
        tool=None,
        state={"messages": []},
        runtime=SimpleNamespace(
            context={"session_id": "session-1", "run_id": "run-1"}
        ),
    )

    middleware.wrap_tool_call(
        request,
        lambda _request: ToolMessage(
            content="# Database Analysis",
            name="read_file",
            tool_call_id="read-skill",
            status="success",
        ),
    )

    assert persisted == [("session-1", ["database-analysis"])]
    assert selected == [("session-1", "run-1", "database-analysis")]


def test_skill_without_business_toolset_is_still_an_active_agent_route(tmp_path) -> None:
    skill_dir = tmp_path / "aihot"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text(
        "---\nname: aihot\ndescription: AI news\n---\n# AI HOT\n",
        encoding="utf-8",
    )
    middleware = ToolsetMiddleware(skills_dir=tmp_path, toolsets_by_skill={})
    messages = [
        AIMessage(
            content="",
            tool_calls=[{
                "name": "read_file",
                "args": {"file_path": "/skills/aihot/SKILL.md"},
                "id": "read-aihot",
                "type": "tool_call",
            }],
        ),
        ToolMessage(
            content="# AI HOT",
            name="read_file",
            tool_call_id="read-aihot",
            status="success",
        ),
    ]

    assert middleware._loaded_skill_ids(messages) == ["aihot"]
    assert middleware.toolsets_by_skill["aihot"] == frozenset()


def test_same_run_installed_skill_refreshes_declared_toolsets(tmp_path) -> None:
    middleware = ToolsetMiddleware(skills_dir=tmp_path, toolsets_by_skill={})
    skill_dir = tmp_path / "late-database"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text(
        "---\nname: late-database\ndescription: Late install\ntoolsets:\n  - database_analysis\n---\n",
        encoding="utf-8",
    )
    messages = [
        AIMessage(
            content="",
            tool_calls=[{
                "name": "read_file",
                "args": {"file_path": "/skills/late-database/SKILL.md"},
                "id": "read-late",
                "type": "tool_call",
            }],
        ),
        ToolMessage(
            content="# Late",
            name="read_file",
            tool_call_id="read-late",
            status="success",
        ),
    ]

    assert middleware._loaded_skill_ids(messages) == ["late-database"]
    assert "database_sql_generate" in middleware._allowed_tool_names(
        {"messages": messages}
    )


def test_workspace_shadow_skill_does_not_activate_toolset(tmp_path) -> None:
    middleware = ToolsetMiddleware(
        skills_dir=tmp_path,
        toolsets_by_skill={"database-analysis": {"database_analysis"}},
    )
    messages = [
        AIMessage(content="", tool_calls=[{"name": "read_file", "args": {"file_path": "/workspace/skills/database-analysis/SKILL.md"}, "id": "shadow", "type": "tool_call"}]),
        ToolMessage(content="# shadow", name="read_file", tool_call_id="shadow", status="success"),
    ]

    assert middleware._loaded_skill_ids(messages) == []


def test_business_tool_execution_is_denied_until_skill_is_active(tmp_path) -> None:
    middleware = ToolsetMiddleware(
        skills_dir=tmp_path,
        toolsets_by_skill={"database-analysis": {"database_analysis"}},
    )
    calls: list[str] = []

    def execute(request: ToolCallRequest) -> ToolMessage:
        calls.append(str(request.tool_call["name"]))
        return ToolMessage(content="executed", name=str(request.tool_call["name"]), tool_call_id=str(request.tool_call["id"]), status="success")

    denied_request = ToolCallRequest(
        tool_call={"name": "database_sql_generate", "args": {}, "id": "denied", "type": "tool_call"},
        tool=None,
        state={"messages": [], "active_skill_ids": []},
        runtime=None,
    )
    denied = middleware.wrap_tool_call(denied_request, execute)
    assert isinstance(denied, ToolMessage)
    assert denied.status == "error"
    assert calls == []

    allowed_request = ToolCallRequest(
        tool_call={"name": "database_sql_generate", "args": {}, "id": "allowed", "type": "tool_call"},
        tool=None,
        state={"messages": [], "active_skill_ids": ["database-analysis"]},
        runtime=None,
    )
    allowed = middleware.wrap_tool_call(allowed_request, execute)
    assert isinstance(allowed, ToolMessage)
    assert allowed.status == "success"
    assert calls == ["database_sql_generate"]


def test_native_and_explicit_base_tools_are_unconditionally_visible_and_executable(tmp_path) -> None:
    middleware = ToolsetMiddleware(skills_dir=tmp_path, toolsets_by_skill={})
    tools = [
        {"name": "read_file"},
        {"name": "write_file"},
        {"name": "task"},
        {"name": "execute"},
        {"name": "read_resource"},
        {"name": "tavily_search"},
        {"name": "fetch_url"},
        {"name": "edit_file"},
        {"name": "inspect_file_version"},
        {"name": "patch_file"},
        {"name": "stage_external_artifact"},
        {"name": "commit_external_artifact"},
        {"name": "prepare_attachment_edit"},
        {"name": "publish_attachment"},
        {"name": "prepare_skill_install"},
        {"name": "install_skill"},
        {"name": "prepare_skill_update"},
        {"name": "update_skill"},
        {"name": "unknown_custom_tool"},
    ]
    request = ModelRequest(model=None, messages=[], tools=tools, state={"messages": [], "active_skill_ids": []})

    assert [tool["name"] for tool in middleware._visible_tools(request)] == [
        "read_file",
        "write_file",
        "task",
        "execute",
        "read_resource",
        "tavily_search",
        "fetch_url",
        "inspect_file_version",
        "patch_file",
        "stage_external_artifact",
        "commit_external_artifact",
        "prepare_attachment_edit",
        "publish_attachment",
    ]

    calls: list[str] = []
    for tool_name in ("execute", "tavily_search", "fetch_url"):
        tool_request = ToolCallRequest(
            tool_call={"name": tool_name, "args": {}, "id": tool_name, "type": "tool_call"},
            tool=None,
            state={"messages": [], "active_skill_ids": []},
            runtime=None,
        )
        middleware.wrap_tool_call(
            tool_request,
            lambda request: calls.append(str(request.tool_call["name"]))
            or ToolMessage(
                content="executed",
                name=str(request.tool_call["name"]),
                tool_call_id=str(request.tool_call["id"]),
                status="success",
            ),
        )
    assert calls == ["execute", "tavily_search", "fetch_url"]


def test_active_skill_state_survives_message_compaction(tmp_path) -> None:
    middleware = ToolsetMiddleware(
        skills_dir=tmp_path,
        toolsets_by_skill={"database-analysis": {"database_analysis"}},
    )

    assert "database_sql_generate" in middleware._allowed_tool_names(
        {"messages": [HumanMessage(content="Earlier messages were compacted")], "active_skill_ids": ["database-analysis"]}
    )


def _routed_profile(
    *skill_ids: str,
    missing_explicit_skill_ids: list[str] | None = None,
) -> dict:
    return RunTaskProfile(
        skill_candidates=[
            SkillCandidate(
                skill_id=skill_id,
                confidence=0.9,
                evidence="matched by semantic router",
            )
            for skill_id in skill_ids
        ],
        missing_explicit_skill_ids=missing_explicit_skill_ids or [],
        execution_route=(
            "skill_first"
            if skill_ids
            else "missing_skill"
            if missing_explicit_skill_ids
            else "native"
        ),
        native_fallback=not bool(missing_explicit_skill_ids),
    ).model_dump(mode="json")


def test_skill_router_prompt_is_transient_and_preserves_message_identity() -> None:
    middleware = SkillIntentRouterMiddleware()
    original = HumanMessage(content="用数据库查配置率", id="user-message")
    state = {
        "messages": [original],
        "task_profile": _routed_profile("database-analysis"),
    }
    request = ModelRequest(model=None, messages=[original], tools=[], state=state)

    routed = middleware._request_with_routing_prompt(request)

    assert state["messages"] == [original]
    assert len(routed.messages) == 1
    assert routed.messages[0].id == "user-message"
    assert "[系统 Skill 提示]" in str(routed.messages[0].content)


def test_skill_router_does_not_request_reload_for_active_session_skill() -> None:
    middleware = SkillIntentRouterMiddleware()
    original = HumanMessage(content="继续查数据库配置率", id="follow-up")
    request = ModelRequest(
        model=None,
        messages=[original],
        tools=[],
        state={
            "messages": [original],
            "active_skill_ids": ["database-analysis"],
            "task_profile": _routed_profile("database-analysis"),
        },
    )

    routed = middleware._request_with_routing_prompt(request)

    assert routed is request
    assert routed.messages == [original]


def test_skill_intent_router_consumes_persisted_candidates_not_tool_names() -> None:
    decision = SkillIntentRouterMiddleware()._routing_decision(
        _routed_profile("build-logical-dataset", "table-analysis")
    )

    assert decision["skill_ids"] == ["build-logical-dataset", "table-analysis"]
    assert "database_sql_generate" not in decision["routing_prompt"]
    assert "/skills/build-logical-dataset/SKILL.md" in decision["routing_prompt"]


def test_skill_intent_router_allows_native_fallback_without_candidate() -> None:
    middleware = SkillIntentRouterMiddleware()
    original = HumanMessage(content="解释一个没有专用 Skill 的新任务")
    request = ModelRequest(
        model=None,
        messages=[original],
        tools=[],
        state={"messages": [original], "task_profile": _routed_profile()},
    )

    assert middleware._request_with_routing_prompt(request) is request


def test_skill_intent_router_surfaces_explicit_missing_skill() -> None:
    middleware = SkillIntentRouterMiddleware()
    original = HumanMessage(content="使用 missing-demo Skill 完成任务")
    request = ModelRequest(
        model=None,
        messages=[original],
        tools=[],
        state={
            "messages": [original],
            "task_profile": _routed_profile(
                missing_explicit_skill_ids=["missing-demo"]
            ),
        },
    )

    routed = middleware._request_with_routing_prompt(request)

    assert "missing-demo" in str(routed.messages[0].content)
    assert "当前未安装" in str(routed.messages[0].content)
