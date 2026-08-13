import hashlib
from pathlib import Path
from types import SimpleNamespace

import pytest
from langchain.agents.middleware.types import ModelRequest, ModelResponse, ToolCallRequest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage

from graph.middlewares.skill_intent_router import (
    RequiredSkillBoundaryMiddleware,
    SkillIntentRouterMiddleware,
)
from graph.middlewares.toolset import (
    ToolsetMiddleware,
    discover_skill_catalog,
    discover_skill_toolsets,
)
from harness.models import RunRecord, RunTaskProfile, SkillCacheEntry, SkillCandidate
from harness.task_profiles import TaskProfileClassifier
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


def _install_test_skill(
    root: Path,
    skill_id: str,
    toolsets: set[str],
) -> None:
    skill_dir = root / skill_id
    skill_dir.mkdir(parents=True, exist_ok=True)
    declared = "".join(f"  - {item}\n" for item in sorted(toolsets))
    (skill_dir / "SKILL.md").write_text(
        f"---\nname: {skill_id}\ndescription: test skill\ntoolsets:\n{declared}---\n# Test\n",
        encoding="utf-8",
    )


def _active_skill_state(
    middleware: ToolsetMiddleware,
    skill_id: str,
) -> dict[str, object]:
    activation = middleware._activation_for_skill(
        skill_id,
        run_id="run-1",
        goal_id=None,
        goal_revision=None,
        tool_call_id="read-skill",
    )
    assert activation is not None
    assert activation is not None
    return {
        "messages": [],
        "active_skill_ids": [skill_id],
        "skill_activations": [activation.model_dump(mode="json")],
    }


def _cache_test_skill(
    middleware: ToolsetMiddleware,
    *,
    session_id: str,
    run_id: str,
    policy_epoch: int = 1,
    skill_id: str = "database-analysis",
    created_at: float | None = None,
) -> SkillCacheEntry:
    from graph.session_manager import session_manager

    digest = middleware._skill_digest(skill_id)
    content = middleware._skill_content(skill_id)
    assert digest is not None and content is not None
    entry = SkillCacheEntry(
        skill_id=skill_id,
        skill_content_sha256=digest,
        content=content,
        toolsets=["database_analysis"],
        policy_epoch=policy_epoch,
        source_run_id=run_id,
        **({"created_at": created_at} if created_at is not None else {}),
    )
    session_manager.record_skill_cache_entry(
        session_id,
        entry.model_dump(mode="json"),
    )
    return entry


def test_project_skill_frontmatter_declares_known_toolsets() -> None:
    skills = discover_skill_toolsets(Path(__file__).resolve().parents[1] / "skills")

    assert skills["build-semantic-dimension"] == {"semantic_dimension_build", "semantic_lookup"}
    assert skills["semantic-steward"] == {"database_analysis", "semantic_lookup", "semantic_steward"}
    assert skills["build-logical-dataset"] == {"logical_dataset"}
    assert skills["database-analysis"] == {"database_analysis", "semantic_lookup"}
    assert skills["skill-management"] == {"skill_management"}
    assert all(name in TOOLSETS for values in skills.values() for name in values)


def test_agent_sql_path_hides_legacy_database_tools_from_manifest(
    tmp_path,
) -> None:
    _install_test_skill(tmp_path, "database-analysis", {"database_analysis"})
    middleware = ToolsetMiddleware(
        skills_dir=tmp_path,
        toolsets_by_skill={"database-analysis": {"database_analysis"}},
    )
    state = _active_skill_state(middleware, "database-analysis")
    request = ModelRequest(
        model=None,
        messages=[HumanMessage(content="查询产品配置")],
        system_message=SystemMessage(content="base"),
        tools=[
            {"name": "database_evidence_search"},
            {"name": "database_sql_generate"},
            {"name": "database_sql_validate_legacy"},
            {"name": "database_sql_validate"},
            {"name": "database_sql_execute"},
        ],
        state=state,
        runtime=SimpleNamespace(context={"run_id": "run-agent"}),
    )

    visible = middleware._visible_tools(request)
    manifest = middleware._capability_manifest(request, visible)

    assert [item["name"] for item in visible] == [
        "database_evidence_search",
        "database_sql_validate",
        "database_sql_execute",
    ]
    assert "database_sql_generate" not in manifest.allowed_tool_names
    assert "database_sql_validate_legacy" not in manifest.allowed_tool_names
    assert all(
        item["tool"] not in {"database_sql_generate", "database_sql_validate_legacy"}
        for item in manifest.unavailable_tools
    )


def test_skill_catalog_is_discovered_from_installed_frontmatter() -> None:
    catalog = discover_skill_catalog(Path(__file__).resolve().parents[1] / "skills")
    by_id = {item["skill_id"]: item for item in catalog}

    assert by_id["database-analysis"]["name"] == "database-analysis"
    assert "relational data" in by_id["database-analysis"]["description"]
    assert by_id["database-analysis"]["path"] == ("/skills/database-analysis/SKILL.md")
    assert {"tavily-search", "web-tools-guide"}.isdisjoint(by_id)


def test_every_registered_agent_custom_tool_has_an_explicit_policy() -> None:
    assert agent_custom_tool_names() == business_tool_names() | DEFAULT_CUSTOM_TOOL_NAMES
    assert {
        "llm_wiki_create_raw",
        "llm_wiki_start_ingest",
        "llm_wiki_retire_pages",
    }.issubset(BUSINESS_TOOLSETS["llm_wiki"])
    assert "llm_wiki_publish" not in BUSINESS_TOOLSETS["llm_wiki"]
    assert {
        "prepare_skill_install",
        "install_skill",
        "prepare_skill_update",
        "update_skill",
        "inspect_skill",
    }.issubset(BUSINESS_TOOLSETS["skill_management"])
    assert "edit_file" not in UNCONDITIONAL_TOOL_NAMES
    assert "read_later_save_url" in UNCONDITIONAL_TOOL_NAMES
    assert "update_memory" in UNCONDITIONAL_TOOL_NAMES
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
    assert (harness_file_tools - {"execute_external_directory"}) <= ToolExecutionPipeline.DECLARED_ALLOW_TOOLS
    assert "execute_external_directory" not in ToolExecutionPipeline.DECLARED_ALLOW_TOOLS


def test_every_registered_tool_declares_a_control_descriptor() -> None:
    assert validate_tool_control_descriptors() == []
    assert TOOL_CONTROL_DESCRIPTORS["execute"].policy == "dynamic"
    assert TOOL_CONTROL_DESCRIPTORS["task"].policy == "inherit_parent"
    assert TOOL_CONTROL_DESCRIPTORS["update_memory"].side_effect == "internal_mutation"
    assert TOOL_CONTROL_DESCRIPTORS["commit_external_artifact"].side_effect == "external_mutation"
    semantic_publish = TOOL_CONTROL_DESCRIPTORS["publish_semantic_markdown"]
    assert semantic_publish.approval_scope == "call"
    assert semantic_publish.policy == "digest_bound_user_confirmation"
    assert "publish_semantic_markdown" in ToolExecutionPipeline.SEMANTIC_COMMIT_TOOLS


def test_toolset_activates_only_after_successfully_reading_skill_file(tmp_path) -> None:
    middleware = ToolsetMiddleware(
        skills_dir=tmp_path,
        toolsets_by_skill={"database-analysis": {"database_analysis", "semantic_lookup"}},
    )
    messages = [
        AIMessage(
            content="",
            tool_calls=[
                {
                    "name": "read_file",
                    "args": {"file_path": "/skills/database-analysis/SKILL.md"},
                    "id": "call_skill",
                    "type": "tool_call",
                }
            ],
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


def test_loaded_mcp_tools_are_default_visible_without_skill_activation(tmp_path) -> None:
    middleware = ToolsetMiddleware(
        skills_dir=tmp_path,
        toolsets_by_skill={"database-analysis": {"database_analysis"}},
        mcp_tool_names={"zhihuiya_patents_search"},
    )
    request = ModelRequest(
        model=None,
        messages=[],
        tools=[
            {"name": "read_file"},
            {"name": "database_evidence_search"},
            {"name": "zhihuiya_patents_search"},
        ],
        state={"messages": []},
    )

    assert [tool["name"] for tool in middleware._visible_tools(request)] == [
        "read_file",
        "zhihuiya_patents_search",
    ]


def test_capability_manifest_drives_prompt_and_visible_schema_from_same_state(tmp_path) -> None:
    _install_test_skill(tmp_path, "database-analysis", {"database_analysis"})
    middleware = ToolsetMiddleware(
        skills_dir=tmp_path,
        toolsets_by_skill={"database-analysis": {"database_analysis"}},
    )
    request = ModelRequest(
        model=None,
        messages=[HumanMessage(content="查询销量")],
        system_message=SystemMessage(content="base"),
        tools=[
            {"name": "read_file"},
            {"name": "execute"},
            {"name": "database_evidence_search"},
        ],
        state=_active_skill_state(middleware, "database-analysis"),
    )

    updated = middleware._request_with_capability_manifest(request)

    visible = [tool["name"] for tool in updated.tools]
    assert visible == ["read_file", "execute", "database_evidence_search"]
    prompt = str(updated.system_message.content)
    assert "Current Capability Manifest" in prompt
    assert '"active_skill_ids": ["database-analysis"]' in prompt
    assert '"database_evidence_search"' in prompt


def test_capability_manifest_prompt_is_stable_when_only_audit_time_changes(
    tmp_path,
    monkeypatch,
) -> None:
    middleware = ToolsetMiddleware(skills_dir=tmp_path, toolsets_by_skill={})
    request = ModelRequest(
        model=None,
        messages=[HumanMessage(content="hello")],
        system_message=SystemMessage(content="base"),
        tools=[{"name": "read_file"}, {"name": "execute"}],
        state={"messages": [], "active_skill_ids": [], "skill_activations": []},
        runtime=SimpleNamespace(context={"run_id": "run-cache-stability"}),
    )

    monkeypatch.setattr("graph.middlewares.toolset.time.time", lambda: 1.0)
    first = middleware._request_with_capability_manifest(request)
    monkeypatch.setattr("graph.middlewares.toolset.time.time", lambda: 2.0)
    second = middleware._request_with_capability_manifest(request)

    first_prompt = str(first.system_message.content)
    second_prompt = str(second.system_message.content)
    assert first_prompt == second_prompt
    assert '"created_at"' not in first_prompt


def test_capability_and_permission_prompts_are_stable_across_run_identity(
    tmp_path,
) -> None:
    middleware = ToolsetMiddleware(skills_dir=tmp_path, toolsets_by_skill={})

    def request_for(run_id: str) -> ModelRequest:
        return ModelRequest(
            model=None,
            messages=[HumanMessage(content="same turn")],
            system_message=SystemMessage(content="base"),
            tools=[{"name": "read_file"}, {"name": "execute"}],
            state={"messages": [], "active_skill_ids": [], "skill_activations": []},
            runtime=SimpleNamespace(context={"run_id": run_id}),
        )

    first_request = request_for("run-cache-a")
    second_request = request_for("run-cache-b")
    first_manifest = middleware._capability_manifest(
        first_request,
        middleware._visible_tools(first_request),
    )
    second_manifest = middleware._capability_manifest(
        second_request,
        middleware._visible_tools(second_request),
    )
    first_permission = middleware._permission_manifest(
        first_request,
        middleware._visible_tools(first_request),
    )
    second_permission = middleware._permission_manifest(
        second_request,
        middleware._visible_tools(second_request),
    )
    first = middleware._request_with_capability_manifest(first_request)
    second = middleware._request_with_capability_manifest(second_request)

    assert first_manifest.run_id != second_manifest.run_id
    assert first_manifest.manifest_id == second_manifest.manifest_id
    assert first_permission.run_id != second_permission.run_id
    assert first_permission.manifest_id == second_permission.manifest_id
    assert str(first.system_message.content) == str(second.system_message.content)
    assert '"run_id"' not in str(first.system_message.content)


def test_permission_boundary_change_still_changes_model_visible_manifest(
    tmp_path,
    monkeypatch,
) -> None:
    from graph.session_manager import session_manager

    state_dir = tmp_path / "state"
    state_dir.mkdir()
    session_manager.initialize(state_dir)
    session_manager.create_session("session-policy")
    middleware = ToolsetMiddleware(skills_dir=tmp_path, toolsets_by_skill={})
    snapshots = {
        "run-strict": {
            "config_snapshot": {
                "permissions": {
                    "approval_mode": "strict",
                    "policy_epoch": 1,
                    "policy_version": "policy-a",
                }
            }
        },
        "run-smart": {
            "config_snapshot": {
                "permissions": {
                    "approval_mode": "smart",
                    "policy_epoch": 2,
                    "policy_version": "policy-b",
                }
            }
        },
    }
    monkeypatch.setattr(
        "graph.middlewares.toolset.session_manager.get_run_state",
        lambda _session_id, run_id: snapshots[run_id],
    )
    monkeypatch.setattr(
        "graph.middlewares.toolset.session_manager.list_permission_grants",
        lambda _session_id: [],
    )

    def request_for(run_id: str) -> ModelRequest:
        return ModelRequest(
            model=None,
            messages=[HumanMessage(content="same turn")],
            system_message=SystemMessage(content="base"),
            tools=[{"name": "read_file"}],
            state={"messages": [], "active_skill_ids": [], "skill_activations": []},
            runtime=SimpleNamespace(context={"session_id": "session-policy", "run_id": run_id}),
        )

    strict_request = request_for("run-strict")
    smart_request = request_for("run-smart")
    strict_manifest = middleware._permission_manifest(
        strict_request,
        middleware._visible_tools(strict_request),
    )
    smart_manifest = middleware._permission_manifest(
        smart_request,
        middleware._visible_tools(smart_request),
    )

    assert strict_manifest.manifest_id != smart_manifest.manifest_id
    assert str(middleware._request_with_capability_manifest(strict_request).system_message.content) != str(
        middleware._request_with_capability_manifest(smart_request).system_message.content
    )


def test_run_scoped_grant_still_changes_permission_manifest_without_run_id(
    tmp_path,
    monkeypatch,
) -> None:
    from graph.session_manager import session_manager

    state_dir = tmp_path / "state"
    state_dir.mkdir()
    session_manager.initialize(state_dir)
    session_manager.create_session("session-run-grant")
    monkeypatch.setattr(
        "graph.middlewares.toolset.session_manager.get_run_state",
        lambda _session_id, _run_id: {"config_snapshot": {}},
    )
    monkeypatch.setattr(
        "graph.middlewares.toolset.session_manager.permission_grants_snapshot",
        lambda _session_id: (
            [
                {
                    "type": "external_directory_read",
                    "scope": "run",
                    "target_kind": "exact_directory",
                    "target": "/approved",
                    "capabilities": ["read", "recursive", "external_path"],
                    "metadata": {"run_id": "run-with-grant"},
                }
            ],
            1,
        ),
    )
    middleware = ToolsetMiddleware(skills_dir=tmp_path, toolsets_by_skill={})

    def permission_for(run_id: str):
        request = ModelRequest(
            model=None,
            messages=[HumanMessage(content="same turn")],
            tools=[{"name": "read_file"}],
            state={"messages": [], "active_skill_ids": [], "skill_activations": []},
            runtime=SimpleNamespace(context={"session_id": "session-run-grant", "run_id": run_id}),
        )
        return middleware._permission_manifest(
            request,
            middleware._visible_tools(request),
        )

    granted = permission_for("run-with-grant")
    ungranted = permission_for("run-without-grant")

    assert granted.manifest_id != ungranted.manifest_id
    assert any(item.get("scope") == "run" for item in granted.allowed)
    assert not any(item.get("scope") == "run" for item in ungranted.allowed)


def test_soft_recommendation_changes_user_hint_not_capability_identity(
    tmp_path,
) -> None:
    _install_test_skill(tmp_path, "database-analysis", {"database_analysis"})
    middleware = ToolsetMiddleware(
        skills_dir=tmp_path,
        toolsets_by_skill={"database-analysis": {"database_analysis"}},
    )
    base_state = {
        "messages": [],
        "active_skill_ids": [],
        "skill_activations": [],
    }
    recommended_state = {
        **base_state,
        "task_profile": RunTaskProfile(
            skill_candidates=[
                SkillCandidate(
                    skill_id="database-analysis",
                    confidence=0.9,
                    evidence="database task",
                )
            ]
        ).model_dump(mode="json"),
    }

    def request_for(state: dict) -> ModelRequest:
        return ModelRequest(
            model=None,
            messages=[HumanMessage(content="same turn")],
            system_message=SystemMessage(content="base"),
            tools=[{"name": "read_file"}, {"name": "database_sql_generate"}],
            state=state,
            runtime=SimpleNamespace(context={"run_id": "run-recommendation"}),
        )

    plain_request = request_for(base_state)
    recommended_request = request_for(recommended_state)
    plain_manifest = middleware._capability_manifest(
        plain_request,
        middleware._visible_tools(plain_request),
    )
    recommended_manifest = middleware._capability_manifest(
        recommended_request,
        middleware._visible_tools(recommended_request),
    )
    plain = middleware._request_with_capability_manifest(plain_request)
    recommended = middleware._request_with_capability_manifest(recommended_request)

    assert plain_manifest.manifest_id == recommended_manifest.manifest_id
    assert str(plain.system_message.content) == str(recommended.system_message.content)
    assert "/skills/database-analysis/SKILL.md" not in str(plain.messages[-1].content)
    assert "/skills/database-analysis/SKILL.md" in str(recommended.messages[-1].content)


def test_capability_schema_hash_changes_with_description_or_parameters(tmp_path) -> None:
    middleware = ToolsetMiddleware(skills_dir=tmp_path, toolsets_by_skill={})
    base = ModelRequest(
        model=None,
        messages=[],
        tools=[
            {
                "name": "read_file",
                "description": "read one file",
                "parameters": {"type": "object", "properties": {"path": {"type": "string"}}},
            }
        ],
        state={"messages": []},
        runtime=SimpleNamespace(context={"run_id": "run-schema"}),
    )
    changed = base.override(
        tools=[
            {
                "name": "read_file",
                "description": "read one file safely",
                "parameters": {"type": "object", "properties": {"path": {"type": "string"}}},
            }
        ]
    )

    first = middleware._capability_manifest(base, middleware._visible_tools(base))
    second = middleware._capability_manifest(changed, middleware._visible_tools(changed))

    assert first.allowed_tool_names == second.allowed_tool_names
    assert first.tool_schema_hash != second.tool_schema_hash
    assert first.manifest_id != second.manifest_id


def test_permission_manifest_marks_path_tools_argument_dependent(tmp_path) -> None:
    middleware = ToolsetMiddleware(skills_dir=tmp_path, toolsets_by_skill={})
    request = ModelRequest(
        model=None,
        messages=[],
        tools=[{"name": "read_file"}, {"name": "update_todos"}],
        state={"messages": []},
        runtime=SimpleNamespace(context={"run_id": "run-path-policy"}),
    )

    visible = middleware._visible_tools(request)
    manifest = middleware._permission_manifest(request, visible)

    read_policy = next(item for item in manifest.hitl_required if item["tool"] == "read_file")
    assert read_policy["approval_scope"] == "argument_dependent"
    assert any(item["tool"] == "update_todos" for item in manifest.allowed)


@pytest.mark.parametrize("backend_mode", ["spawn", "kernel"])
def test_smart_permission_manifest_marks_host_reads_runtime_evaluated(
    tmp_path,
    monkeypatch,
    backend_mode,
) -> None:
    from graph.session_manager import session_manager

    session_manager.initialize(tmp_path)
    session_manager.create_session("spawn-manifest-session")
    middleware = ToolsetMiddleware(skills_dir=tmp_path, toolsets_by_skill={})
    monkeypatch.setattr(
        "graph.middlewares.toolset.session_manager.get_run_state",
        lambda _session_id, _run_id: {
            "config_snapshot": {
                "permissions": {"approval_mode": "smart", "policy_epoch": 1},
                "execution": {"backend_mode": backend_mode},
            }
        },
    )
    monkeypatch.setattr(
        "graph.middlewares.toolset.session_manager.permission_grants_snapshot",
        lambda _session_id: ([], 0),
    )
    monkeypatch.setattr(
        "graph.middlewares.toolset.session_manager.get_permission_policy",
        lambda _session_id: {"policy_epoch": 1},
    )
    request = ModelRequest(
        model=None,
        messages=[],
        tools=[{"name": "read_file"}, {"name": "write_file"}, {"name": "execute"}],
        state={"messages": []},
        runtime=SimpleNamespace(
            context={"session_id": "spawn-manifest-session", "run_id": "spawn-manifest-run"}
        ),
    )

    manifest = middleware._permission_manifest(request, middleware._visible_tools(request))

    assert not any(item.get("tool") == "read_file" for item in manifest.allowed)
    assert any(
        item.get("tool") == "read_file"
        and item.get("reason")
        == "ordinary_local_reads_auto_allow_while_sensitive_or_expansive_reads_use_the_gate"
        for item in manifest.runtime_evaluated
    )
    assert not any(item.get("tool") == "read_file" for item in manifest.hitl_required)
    assert any(item.get("tool") == "write_file" for item in manifest.hitl_required)
    assert not any(item.get("tool") == "execute" for item in manifest.hitl_required)
    assert any(item.get("tool") == "execute" for item in manifest.runtime_evaluated)


def test_goal_inspection_exposes_only_read_only_tools(tmp_path) -> None:
    middleware = ToolsetMiddleware(skills_dir=tmp_path, toolsets_by_skill={})
    request = ModelRequest(
        model=None,
        messages=[HumanMessage(content="总结当前进度")],
        tools=[
            {"name": "read_file"},
            {"name": "read_evidence"},
            {"name": "update_todos"},
            {"name": "write_file"},
            {"name": "execute"},
            {"name": "task"},
            {"name": "database_sql_generate"},
            {"name": "database_sql_validate"},
            {"name": "database_sql_execute"},
            {"name": "database_query_result_source"},
            {"name": "database_query_trace_inspect"},
            {"name": "database_query_result_page"},
            {"name": "database_schema_inspect"},
        ],
        state={"messages": []},
        runtime=SimpleNamespace(context={"run_id": "run-inspect", "run_kind": "goal_inspection"}),
    )

    visible = [tool["name"] for tool in middleware._visible_tools(request)]
    manifest = middleware._capability_manifest(request, middleware._visible_tools(request))

    assert visible == ["read_file", "read_evidence"]
    assert middleware._inspection_tool_allowed("database_query_trace_inspect")
    assert middleware._inspection_tool_allowed("database_query_result_page")
    assert "update_todos" not in manifest.allowed_tool_names
    assert "database_sql_execute" not in manifest.allowed_tool_names
    assert "database_query_result_source" not in manifest.allowed_tool_names
    assert "database_schema_inspect" not in manifest.allowed_tool_names
    assert any(
        item["tool"] == "update_todos" and item["reason"] == "inspection_run_read_only"
        for item in manifest.unavailable_tools
    )


def test_capability_manifest_recommends_concrete_inactive_skill_without_expanding_tools(
    tmp_path,
) -> None:
    _install_test_skill(tmp_path, "database-analysis", {"database_analysis"})
    middleware = ToolsetMiddleware(
        skills_dir=tmp_path,
        toolsets_by_skill={"database-analysis": {"database_analysis"}},
    )
    request = ModelRequest(
        model=None,
        messages=[HumanMessage(content="补算产品配置热力图")],
        system_message=SystemMessage(content="base"),
        tools=[
            {"name": "read_file"},
            {"name": "database_evidence_search"},
        ],
        state={
            "messages": [],
            "active_skill_ids": [],
            "skill_activations": [],
            "task_profile": RunTaskProfile(
                skill_candidates=[
                    SkillCandidate(
                        skill_id="database-analysis",
                        confidence=0.91,
                        evidence="需要重算数据库图表",
                    )
                ],
                classifier="llm_router",
            ).model_dump(mode="json"),
        },
    )

    routed = SkillIntentRouterMiddleware()._request_with_routing_prompt(request)
    updated = middleware._request_with_capability_manifest(routed)

    assert [tool["name"] for tool in updated.tools] == ["read_file"]
    prompt = str(updated.system_message.content)
    assert '"recommended_inactive_skills"' not in prompt
    assert '"skill_id": "database-analysis"' not in prompt
    assert "/skills/database-analysis/SKILL.md" not in prompt
    assert "/skills/database-analysis/SKILL.md" in str(updated.messages[-1].content)
    assert '"allowed_tool_names": ["read_file"]' in prompt
    assert '"reason": "skill_not_activated"' in prompt
    assert '"tool": "database_evidence_search"' in prompt
    assert '"activation_skill_ids": ["database-analysis"]' in prompt
    assert "recoverable capability dependency" in prompt
    audit_manifest = middleware._capability_manifest(
        request,
        middleware._visible_tools(request),
    )
    assert [item.skill_id for item in audit_manifest.recommended_inactive_skills] == ["database-analysis"]
    unavailable = next(item for item in audit_manifest.unavailable_tools if item["tool"] == "database_evidence_search")
    assert unavailable["activation_skill_ids"] == ["database-analysis"]


def test_artifact_contract_validator_is_unconditionally_visible(tmp_path) -> None:
    middleware = ToolsetMiddleware(skills_dir=tmp_path, toolsets_by_skill={})
    request = ModelRequest(
        model=None,
        messages=[HumanMessage(content="修复热力图下拉")],
        system_message=SystemMessage(content="base"),
        tools=[
            {"name": "read_file"},
            {"name": "validate_artifact_contract"},
            {"name": "database_sql_generate"},
        ],
        state={"messages": [], "active_skill_ids": [], "skill_activations": []},
    )

    updated = middleware._request_with_capability_manifest(request)

    assert [tool["name"] for tool in updated.tools] == [
        "read_file",
        "validate_artifact_contract",
    ]


def test_capability_manifest_recommends_skill_from_follow_up_artifact_without_inheriting_it(
    tmp_path,
) -> None:
    from graph.session_manager import session_manager
    from harness.models import RunRecord, RunStatus

    skills = tmp_path / "skills"
    _install_test_skill(skills, "database-analysis", {"database_analysis"})
    middleware = ToolsetMiddleware(
        skills_dir=skills,
        toolsets_by_skill={"database-analysis": {"database_analysis"}},
    )
    state = tmp_path / "state"
    state.mkdir()
    session_manager.initialize(state)
    session_manager.create_session("artifact-skill-session")
    activation = middleware._activation_for_skill(
        "database-analysis",
        run_id="run-old",
        goal_id="goal-old",
        goal_revision=1,
        tool_call_id="read-skill",
    )
    old = RunRecord(
        run_id="run-old",
        query_id="query-old",
        session_id="artifact-skill-session",
        objective="build report",
        goal_id="goal-old",
        goal_revision=1,
        status=RunStatus.PREPARING,
        skill_activations=[activation],
    )
    session_manager.start_harness_run("artifact-skill-session", old.model_dump(mode="json"))
    report_target = tmp_path / "report.html"
    report_target.write_text("report", encoding="utf-8")
    delivered = session_manager.register_delivered_artifact(
        "artifact-skill-session",
        target_path=str(report_target),
        content_sha256="sha256:" + hashlib.sha256(report_target.read_bytes()).hexdigest(),
        source_run_id="run-old",
        source_query_id="query-old",
        source_goal_id="goal-old",
        source_goal_revision=1,
    )
    for status in (RunStatus.RUNNING, RunStatus.EVALUATING, RunStatus.COMPLETED):
        session_manager.transition_run_status("artifact-skill-session", "run-old", status.value)
    current = RunRecord(
        run_id="run-new",
        query_id="query-new",
        session_id="artifact-skill-session",
        objective="这个报告还没更新",
        follow_up_of_artifact_ids=[delivered["artifact_id"]],
        execution_mode="delta_repair",
        status=RunStatus.PREPARING,
    )
    session_manager.start_harness_run("artifact-skill-session", current.model_dump(mode="json"))
    request = ModelRequest(
        model=None,
        messages=[HumanMessage(content="这个报告还没更新")],
        system_message=SystemMessage(content="base"),
        tools=[{"name": "read_file"}, {"name": "database_sql_generate"}],
        state={"messages": [], "active_skill_ids": [], "skill_activations": []},
        runtime=SimpleNamespace(
            context={
                "session_id": "artifact-skill-session",
                "run_id": "run-new",
            }
        ),
    )

    updated = middleware._request_with_capability_manifest(request)

    assert [tool["name"] for tool in updated.tools] == ["read_file"]
    prompt = str(updated.system_message.content)
    assert '"source": "durable_artifact"' not in prompt
    assert "/skills/database-analysis/SKILL.md" not in prompt
    assert "/skills/database-analysis/SKILL.md" in str(updated.messages[-1].content)
    assert "durable_artifact" in str(updated.messages[-1].content)
    audit_manifest = middleware._capability_manifest(
        request,
        middleware._visible_tools(request),
    )
    assert audit_manifest.recommended_inactive_skills[0].source == "durable_artifact"


def test_skill_management_tools_are_visible_only_after_skill_activation(tmp_path) -> None:
    _install_test_skill(tmp_path, "skill-management", {"skill_management"})
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
        state=_active_skill_state(middleware, "skill-management"),
    )

    assert middleware._visible_tools(inactive) == []
    assert [tool["name"] for tool in middleware._visible_tools(active)] == [
        "inspect_skill",
        "prepare_skill_install",
        "install_skill",
        "prepare_skill_update",
        "update_skill",
    ]


def test_current_run_skill_reads_activate_tools(tmp_path) -> None:
    _install_test_skill(tmp_path, "database-analysis", {"database_analysis"})
    middleware = ToolsetMiddleware(
        skills_dir=tmp_path,
        toolsets_by_skill={"database-analysis": {"database_analysis"}},
    )
    history = [
        AIMessage(
            content="",
            tool_calls=[
                {
                    "name": "read_file",
                    "args": {"file_path": "/skills/database-analysis/SKILL.md"},
                    "id": "old",
                    "type": "tool_call",
                }
            ],
        ),
        ToolMessage(content="# old", name="read_file", tool_call_id="old", status="success"),
    ]
    assert middleware._loaded_skill_ids(history) == ["database-analysis"]
    update = middleware.before_agent({"messages": history}, runtime=None)
    assert update is not None
    assert update["active_skill_ids"] == ["database-analysis"]
    assert len(update["skill_activations"]) == 1


def test_historical_skill_reads_do_not_activate_tools(tmp_path) -> None:
    _install_test_skill(tmp_path, "database-analysis", {"database_analysis"})
    middleware = ToolsetMiddleware(
        skills_dir=tmp_path,
        toolsets_by_skill={"database-analysis": {"database_analysis"}},
    )
    historical = {"puddingclaw_historical": True}
    history = [
        AIMessage(
            content="",
            tool_calls=[
                {
                    "name": "read_file",
                    "args": {"file_path": "/skills/database-analysis/SKILL.md"},
                    "id": "old",
                    "type": "tool_call",
                }
            ],
            additional_kwargs=historical,
        ),
        ToolMessage(
            content="# old",
            name="read_file",
            tool_call_id="old",
            status="success",
            additional_kwargs=historical,
        ),
    ]

    assert middleware._loaded_skill_ids(history) == []
    update = middleware.before_agent(
        {"messages": history},
        runtime=SimpleNamespace(context={"run_id": "run-new"}),
    )
    assert update == {"active_skill_ids": [], "skill_activations": []}
    assert "database_sql_execute" not in middleware._allowed_tool_names(update)


def test_execute_cannot_use_skill_entrypoint_before_current_skill_activation(tmp_path) -> None:
    _install_test_skill(tmp_path, "aihot", set())
    middleware = ToolsetMiddleware(
        skills_dir=tmp_path,
        toolsets_by_skill={"aihot": set()},
    )
    executed: list[str] = []
    request = ToolCallRequest(
        tool_call={
            "name": "execute",
            "args": {
                "command": "python3 /skills/aihot/scripts/aihot_query.py --user-query latest",
            },
            "id": "call-stale-entrypoint",
            "type": "tool_call",
        },
        tool=None,
        state={"messages": [], "active_skill_ids": [], "skill_activations": []},
        runtime=SimpleNamespace(context={"run_id": "run-new"}),
    )

    result = middleware.wrap_tool_call(
        request,
        lambda _request: executed.append("executed") or ToolMessage(
            content="unexpected",
            tool_call_id="call-stale-entrypoint",
            name="execute",
        ),
    )

    assert isinstance(result, ToolMessage)
    assert result.status == "error"
    assert "read the current authoritative" in str(result.content).lower()
    assert result.additional_kwargs["puddingclaw_control_plane"] == {
        "type": "skill_context_required",
        "skill_id": "aihot",
        "original_tool_executed": False,
    }
    assert executed == []


def test_session_cached_skill_does_not_activate_a_new_run(
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

    assert update == {"active_skill_ids": [], "skill_activations": []}
    assert "database_sql_generate" not in middleware._allowed_tool_names({"messages": [], **update})


def test_hash_bound_session_cache_fast_activates_only_a_routed_skill(
    tmp_path,
) -> None:
    from graph.session_manager import session_manager

    skills = tmp_path / "skills"
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    _install_test_skill(skills, "database-analysis", {"database_analysis"})
    middleware = ToolsetMiddleware(
        skills_dir=skills,
        toolsets_by_skill={"database-analysis": {"database_analysis"}},
    )
    session_manager.initialize(state_dir)
    session_manager.create_session("session-cache-route")
    run = RunRecord(
        run_id="run-cache-route",
        query_id="query-cache-route",
        session_id="session-cache-route",
        objective="继续分析数据库",
        task_profile=RunTaskProfile(
            skill_candidates=[
                SkillCandidate(
                    skill_id="database-analysis",
                    confidence=0.95,
                    evidence="数据库连续追问",
                )
            ]
        ),
    )
    session_manager.start_harness_run(
        run.session_id,
        run.model_dump(mode="json"),
    )
    _cache_test_skill(
        middleware,
        session_id=run.session_id,
        run_id="run-previous",
    )
    policy_before = session_manager.get_permission_policy(run.session_id)
    runtime = SimpleNamespace(
        context={
            "session_id": run.session_id,
            "run_id": run.run_id,
        }
    )

    update = middleware.before_agent(
        {"messages": [], "task_profile": run.task_profile.model_dump(mode="json")},
        runtime=runtime,
    )

    assert update["active_skill_ids"] == ["database-analysis"]
    assert update["skill_activations"][0]["source_tool_call_id"].startswith("skill-cache:task-profile:")
    assert "database_evidence_search" in middleware._allowed_tool_names(
        update,
        policy_epoch=policy_before["policy_epoch"],
    )
    request = ModelRequest(
        model=None,
        messages=[HumanMessage(content="继续")],
        system_message=SystemMessage(content="base"),
        tools=[{"name": "read_file"}, {"name": "database_evidence_search"}],
        state={"messages": [], **update},
        runtime=runtime,
    )
    visible = middleware._request_with_capability_manifest(request)
    assert [item["name"] for item in visible.tools] == [
        "read_file",
        "database_evidence_search",
    ]
    assert "# Test" in str(visible.system_message.content)
    assert "grants no permissions by itself" in str(visible.system_message.content)
    assert session_manager.get_permission_policy(run.session_id) == policy_before


def test_hash_bound_session_cache_stays_inactive_without_a_skill_hit(
    tmp_path,
) -> None:
    from graph.session_manager import session_manager

    skills = tmp_path / "skills"
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    _install_test_skill(skills, "database-analysis", {"database_analysis"})
    middleware = ToolsetMiddleware(
        skills_dir=skills,
        toolsets_by_skill={"database-analysis": {"database_analysis"}},
    )
    session_manager.initialize(state_dir)
    session_manager.create_session("session-cache-no-route")
    run = RunRecord(
        run_id="run-cache-no-route",
        query_id="query-cache-no-route",
        session_id="session-cache-no-route",
        objective="解释一段普通代码",
        task_profile=RunTaskProfile(),
    )
    session_manager.start_harness_run(
        run.session_id,
        run.model_dump(mode="json"),
    )
    _cache_test_skill(
        middleware,
        session_id=run.session_id,
        run_id="run-previous",
    )

    update = middleware.before_agent(
        {"messages": [], "task_profile": run.task_profile.model_dump(mode="json")},
        runtime=SimpleNamespace(
            context={
                "session_id": run.session_id,
                "run_id": run.run_id,
            }
        ),
    )

    assert update == {"active_skill_ids": [], "skill_activations": []}
    assert "database_sql_generate" not in middleware._allowed_tool_names(update)


def test_standard_conversation_restores_session_tools_but_only_injects_stub(
    tmp_path,
) -> None:
    from graph.session_manager import session_manager

    skills = tmp_path / "skills"
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    _install_test_skill(skills, "database-analysis", {"database_analysis"})
    middleware = ToolsetMiddleware(
        skills_dir=skills,
        toolsets_by_skill={"database-analysis": {"database_analysis"}},
        restore_session_skills=True,
    )
    session_manager.initialize(state_dir)
    session_manager.create_session("session-cache-standard")
    run = RunRecord(
        run_id="run-cache-standard",
        query_id="query-cache-standard",
        session_id="session-cache-standard",
        objective="现在解释一段普通 Python 代码",
        task_profile=RunTaskProfile(),
    )
    session_manager.start_harness_run(run.session_id, run.model_dump(mode="json"))
    _cache_test_skill(middleware, session_id=run.session_id, run_id="run-previous")
    runtime = SimpleNamespace(
        context={
            "session_id": run.session_id,
            "run_id": run.run_id,
            "run_kind": "standalone",
        }
    )

    update = middleware.before_agent(
        {"messages": [], "task_profile": run.task_profile.model_dump(mode="json")},
        runtime=runtime,
    )

    assert update is not None
    assert update["active_skill_ids"] == ["database-analysis"]
    persisted_profile = session_manager.get_run_state(run.session_id, run.run_id)["task_profile"]
    assert persisted_profile["skill_candidates"] == []
    visible = middleware._request_with_capability_manifest(
        ModelRequest(
            model=None,
            messages=[HumanMessage(content="解释这段 Python")],
            system_message=SystemMessage(content="base"),
            tools=[{"name": "read_file"}, {"name": "database_evidence_search"}],
            state={"messages": [], "task_profile": run.task_profile.model_dump(mode="json"), **update},
            runtime=runtime,
        )
    )
    assert [item["name"] for item in visible.tools] == [
        "read_file",
        "database_evidence_search",
    ]
    prompt = str(visible.system_message.content)
    assert "Session capability stubs" in prompt
    assert "工具 schema 已恢复" in prompt
    assert "# Test" not in prompt


def test_standard_stable_schema_exposes_mounted_skill_tool_without_granting_it(
    tmp_path,
) -> None:
    from graph.session_manager import session_manager

    skills = tmp_path / "skills"
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    _install_test_skill(skills, "database-analysis", {"database_analysis"})
    middleware = ToolsetMiddleware(
        skills_dir=skills,
        toolsets_by_skill={"database-analysis": {"database_analysis"}},
        restore_session_skills=True,
    )
    session_manager.initialize(state_dir)
    session_manager.create_session("session-stable-schema-no-cache")
    run = RunRecord(
        run_id="run-stable-schema-no-cache",
        query_id="query-stable-schema-no-cache",
        session_id="session-stable-schema-no-cache",
        objective="解释一段普通 Python 代码",
        task_profile=RunTaskProfile(),
    )
    session_manager.start_harness_run(run.session_id, run.model_dump(mode="json"))
    runtime = SimpleNamespace(
        context={
            "session_id": run.session_id,
            "run_id": run.run_id,
            "run_kind": "standalone",
        }
    )
    state = {"messages": [], "task_profile": run.task_profile.model_dump(mode="json")}

    prepared = middleware._request_with_capability_manifest(
        ModelRequest(
            model=None,
            messages=[HumanMessage(content="解释这段 Python")],
            system_message=SystemMessage(content="base"),
            tools=[{"name": "read_file"}, {"name": "database_evidence_search"}],
            state=state,
            runtime=runtime,
        )
    )

    assert [item["name"] for item in prepared.tools] == [
        "read_file",
        "database_evidence_search",
    ]
    assert "database_evidence_search" not in middleware._allowed_tool_names(state)
    denied = middleware.wrap_tool_call(
        ToolCallRequest(
            tool_call={
                "name": "database_evidence_search",
                "args": {"question": "配置率"},
                "id": "stable-schema-denied",
                "type": "tool_call",
            },
            tool=None,
            state=state,
            runtime=runtime,
        ),
        lambda _request: pytest.fail("unactivated stable-schema tool must not execute"),
    )
    assert isinstance(denied, ToolMessage)
    assert denied.status == "error"
    assert "not enabled" in str(denied.content)


def test_session_skill_restore_is_disabled_outside_standalone_runs(tmp_path) -> None:
    from graph.session_manager import session_manager

    skills = tmp_path / "skills"
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    _install_test_skill(skills, "database-analysis", {"database_analysis"})
    middleware = ToolsetMiddleware(
        skills_dir=skills,
        toolsets_by_skill={"database-analysis": {"database_analysis"}},
        restore_session_skills=True,
    )
    session_manager.initialize(state_dir)
    session_manager.create_session("session-cache-goal-run")
    run = RunRecord(
        run_id="run-cache-goal-run",
        query_id="query-cache-goal-run",
        session_id="session-cache-goal-run",
        objective="执行一个隔离 Goal",
        run_kind="goal_execution",
        task_profile=RunTaskProfile(),
    )
    session_manager.start_harness_run(run.session_id, run.model_dump(mode="json"))
    _cache_test_skill(middleware, session_id=run.session_id, run_id="run-previous")

    update = middleware.before_agent(
        {"messages": [], "task_profile": run.task_profile.model_dump(mode="json")},
        runtime=SimpleNamespace(
            context={
                "session_id": run.session_id,
                "run_id": run.run_id,
                "run_kind": "goal_execution",
            }
        ),
    )

    assert update == {"active_skill_ids": [], "skill_activations": []}


def test_restored_session_tool_loads_instructions_on_first_use_then_rechecks(
    tmp_path,
) -> None:
    from graph.session_manager import session_manager

    skills = tmp_path / "skills"
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    _install_test_skill(skills, "database-analysis", {"database_analysis"})
    middleware = ToolsetMiddleware(
        skills_dir=skills,
        toolsets_by_skill={"database-analysis": {"database_analysis"}},
        restore_session_skills=True,
    )
    session_manager.initialize(state_dir)
    session_manager.create_session("session-cache-jit")
    run = RunRecord(
        run_id="run-cache-jit",
        query_id="query-cache-jit",
        session_id="session-cache-jit",
        objective="写代码",
        task_profile=RunTaskProfile(),
    )
    session_manager.start_harness_run(run.session_id, run.model_dump(mode="json"))
    _cache_test_skill(middleware, session_id=run.session_id, run_id="run-previous")
    runtime = SimpleNamespace(
        context={
            "session_id": run.session_id,
            "run_id": run.run_id,
            "run_kind": "standalone",
        }
    )
    update = middleware.before_agent(
        {"messages": [], "task_profile": run.task_profile.model_dump(mode="json")},
        runtime=runtime,
    )
    assert update is not None
    executed: list[str] = []
    result = middleware.wrap_tool_call(
        ToolCallRequest(
            tool_call={
                "name": "database_evidence_search",
                "args": {"question": "配置率"},
                "id": "first-restored-call",
                "type": "tool_call",
            },
            tool=None,
            state={"messages": [], "task_profile": run.task_profile.model_dump(mode="json"), **update},
            runtime=runtime,
        ),
        lambda _request: executed.append("executed"),
    )

    assert executed == []
    assert isinstance(result, ToolMessage)
    assert result.name == "load_skill_context"
    assert result.additional_kwargs["puddingclaw_control_plane"]["type"] == (
        "skill_context_loaded_on_demand"
    )
    assert "重新判断它是否仍与当前任务相关" in str(result.content)
    refreshed = middleware.before_model(
        {"messages": [], "task_profile": run.task_profile.model_dump(mode="json"), **update},
        runtime=runtime,
    )
    assert refreshed is not None
    prompt_request = middleware._request_with_capability_manifest(
        ModelRequest(
            model=None,
            messages=[HumanMessage(content="继续")],
            system_message=SystemMessage(content="base"),
            tools=[{"name": "read_file"}, {"name": "database_evidence_search"}],
            state={"messages": [], **refreshed},
            runtime=runtime,
        )
    )
    assert "# Test" in str(prompt_request.system_message.content)


def test_shared_tool_provider_uses_first_activation_and_reports_reason(tmp_path) -> None:
    from graph.session_manager import session_manager

    skills = tmp_path / "skills"
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    _install_test_skill(skills, "database-analysis", {"database_analysis"})
    _install_test_skill(skills, "database-alt", {"database_analysis"})
    middleware = ToolsetMiddleware(
        skills_dir=skills,
        toolsets_by_skill={
            "database-analysis": {"database_analysis"},
            "database-alt": {"database_analysis"},
        },
        restore_session_skills=True,
    )
    session_manager.initialize(state_dir)
    session_manager.create_session("session-provider-order")
    run = RunRecord(
        run_id="run-provider-order",
        query_id="query-provider-order",
        session_id="session-provider-order",
        objective="普通问答",
        task_profile=RunTaskProfile(),
    )
    session_manager.start_harness_run(run.session_id, run.model_dump(mode="json"))
    _cache_test_skill(
        middleware,
        session_id=run.session_id,
        run_id="run-first",
        skill_id="database-analysis",
        created_at=1.0,
    )
    _cache_test_skill(
        middleware,
        session_id=run.session_id,
        run_id="run-second",
        skill_id="database-alt",
        created_at=2.0,
    )
    runtime = SimpleNamespace(
        context={
            "session_id": run.session_id,
            "run_id": run.run_id,
            "run_kind": "standalone",
        }
    )
    update = middleware.before_agent(
        {"messages": [], "task_profile": run.task_profile.model_dump(mode="json")},
        runtime=runtime,
    )
    assert update is not None
    result = middleware.wrap_tool_call(
        ToolCallRequest(
            tool_call={
                "name": "database_evidence_search",
                "args": {"question": "配置率"},
                "id": "shared-provider-call",
                "type": "tool_call",
            },
            tool=None,
            state={"messages": [], "task_profile": run.task_profile.model_dump(mode="json"), **update},
            runtime=runtime,
        ),
        lambda _request: None,
    )

    assert isinstance(result, ToolMessage)
    control = result.additional_kwargs["puddingclaw_control_plane"]
    assert control["skill_id"] == "database-analysis"
    assert control["provider_candidates"] == ["database-alt", "database-analysis"]
    assert control["provider_reason"] == "first_activation_then_skill_id"


def test_skill_cache_invalidates_on_hash_or_policy_epoch_change(tmp_path) -> None:
    from graph.session_manager import session_manager

    skills = tmp_path / "skills"
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    _install_test_skill(skills, "database-analysis", {"database_analysis"})
    middleware = ToolsetMiddleware(
        skills_dir=skills,
        toolsets_by_skill={"database-analysis": {"database_analysis"}},
    )
    session_manager.initialize(state_dir)
    session_manager.create_session("session-cache-invalidation")
    _cache_test_skill(
        middleware,
        session_id="session-cache-invalidation",
        run_id="run-previous",
        policy_epoch=1,
    )

    assert (
        middleware._cached_skill_entry(
            "session-cache-invalidation",
            "database-analysis",
            policy_epoch=2,
        )
        is None
    )
    stale_activation = middleware._activation_for_skill(
        "database-analysis",
        run_id="run-stale",
        goal_id=None,
        goal_revision=None,
        tool_call_id="read-old-policy",
        policy_epoch=1,
    )
    assert stale_activation is not None
    assert "database_sql_generate" not in middleware._allowed_tool_names(
        {"skill_activations": [stale_activation.model_dump(mode="json")]},
        policy_epoch=2,
    )
    skill_path = skills / "database-analysis" / "SKILL.md"
    skill_path.write_text(
        skill_path.read_text(encoding="utf-8") + "\nNew rule.\n",
        encoding="utf-8",
    )
    assert (
        middleware._cached_skill_entry(
            "session-cache-invalidation",
            "database-analysis",
            policy_epoch=1,
        )
        is None
    )


def test_inactive_tool_call_loads_unique_cached_skill_without_execution(
    tmp_path,
) -> None:
    from graph.session_manager import session_manager

    skills = tmp_path / "skills"
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    _install_test_skill(skills, "database-analysis", {"database_analysis"})
    middleware = ToolsetMiddleware(
        skills_dir=skills,
        toolsets_by_skill={"database-analysis": {"database_analysis"}},
    )
    session_manager.initialize(state_dir)
    session_manager.create_session("session-cache-dynamic")
    run = RunRecord(
        run_id="run-cache-dynamic",
        query_id="query-cache-dynamic",
        session_id="session-cache-dynamic",
        objective="解释高阶智驾算法",
    )
    session_manager.start_harness_run(
        run.session_id,
        run.model_dump(mode="json"),
    )
    _cache_test_skill(
        middleware,
        session_id=run.session_id,
        run_id="run-previous",
    )
    runtime = SimpleNamespace(
        context={
            "session_id": run.session_id,
            "run_id": run.run_id,
        }
    )
    executed: list[str] = []
    request = ToolCallRequest(
        tool_call={
            "name": "database_evidence_search",
            "args": {"question": "查看配置项取值"},
            "id": "call-dynamic",
            "type": "tool_call",
        },
        tool=None,
        state={"messages": [], "active_skill_ids": [], "skill_activations": []},
        runtime=runtime,
    )

    result = middleware.wrap_tool_call(
        request,
        lambda _request: (
            executed.append("executed")
            or ToolMessage(
                content="should not execute",
                name="database_evidence_search",
                tool_call_id="call-dynamic",
                status="success",
            )
        ),
    )

    assert executed == []
    assert isinstance(result, ToolMessage)
    assert result.name == "load_skill_context"
    assert result.status == "error"
    assert "原始 `database_evidence_search` 调用没有执行" in str(result.content)
    persisted = session_manager.get_effective_run_skill_activations(
        run.session_id,
        run.run_id,
    )
    assert [item["skill_id"] for item in persisted] == ["database-analysis"]

    model_state = middleware.before_model(
        {
            "messages": [],
            "active_skill_ids": [],
            "skill_activations": [],
        },
        runtime=runtime,
    )
    assert model_state is not None
    visible = middleware._request_with_capability_manifest(
        ModelRequest(
            model=None,
            messages=[HumanMessage(content="继续")],
            system_message=SystemMessage(content="base"),
            tools=[{"name": "read_file"}, {"name": "database_evidence_search"}],
            state={"messages": [], **model_state},
            runtime=runtime,
        )
    )
    assert [item["name"] for item in visible.tools] == [
        "read_file",
        "database_evidence_search",
    ]
    assert "# Test" in str(visible.system_message.content)
    assert session_manager.list_permission_grants(run.session_id) == []


def test_successful_skill_read_is_persisted_to_session_cache(
    tmp_path,
    monkeypatch,
) -> None:
    import graph.middlewares.toolset as module

    _install_test_skill(tmp_path, "database-analysis", {"database_analysis"})
    middleware = ToolsetMiddleware(
        skills_dir=tmp_path,
        toolsets_by_skill={"database-analysis": {"database_analysis"}},
    )
    persisted: list[tuple[str, str, dict[str, object]]] = []
    selected: list[tuple[str, str, str]] = []
    cached: list[tuple[str, dict[str, object]]] = []
    monkeypatch.setattr(
        module.session_manager,
        "record_skill_cache_entry",
        lambda session_id, entry: cached.append((session_id, entry)) or entry,
    )
    monkeypatch.setattr(
        module.session_manager,
        "record_run_skill_activation",
        lambda session_id, run_id, activation: persisted.append((session_id, run_id, activation)) or activation,
    )
    monkeypatch.setattr(
        module.session_manager,
        "record_run_skill_selection",
        lambda session_id, run_id, skill_id: selected.append((session_id, run_id, skill_id)),
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
        runtime=SimpleNamespace(context={"session_id": "session-1", "run_id": "run-1"}),
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

    assert len(persisted) == 1
    assert persisted[0][0:2] == ("session-1", "run-1")
    assert persisted[0][2]["skill_id"] == "database-analysis"
    assert selected == [("session-1", "run-1", "database-analysis")]
    assert cached[0][0] == "session-1"
    assert cached[0][1]["skill_id"] == "database-analysis"
    assert cached[0][1]["content"].endswith("# Test\n")


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
            tool_calls=[
                {
                    "name": "read_file",
                    "args": {"file_path": "/skills/aihot/SKILL.md"},
                    "id": "read-aihot",
                    "type": "tool_call",
                }
            ],
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
            tool_calls=[
                {
                    "name": "read_file",
                    "args": {"file_path": "/skills/late-database/SKILL.md"},
                    "id": "read-late",
                    "type": "tool_call",
                }
            ],
        ),
        ToolMessage(
            content="# Late",
            name="read_file",
            tool_call_id="read-late",
            status="success",
        ),
    ]

    assert middleware._loaded_skill_ids(messages) == ["late-database"]
    update = middleware.before_agent({"messages": messages}, runtime=None)
    assert update is not None
    assert "database_sql_generate" in middleware._allowed_tool_names({"messages": messages, **update})


def test_workspace_shadow_skill_does_not_activate_toolset(tmp_path) -> None:
    middleware = ToolsetMiddleware(
        skills_dir=tmp_path,
        toolsets_by_skill={"database-analysis": {"database_analysis"}},
    )
    messages = [
        AIMessage(
            content="",
            tool_calls=[
                {
                    "name": "read_file",
                    "args": {"file_path": "/workspace/skills/database-analysis/SKILL.md"},
                    "id": "shadow",
                    "type": "tool_call",
                }
            ],
        ),
        ToolMessage(content="# shadow", name="read_file", tool_call_id="shadow", status="success"),
    ]

    assert middleware._loaded_skill_ids(messages) == []


def test_business_tool_execution_is_denied_until_skill_is_active(tmp_path) -> None:
    _install_test_skill(tmp_path, "database-analysis", {"database_analysis"})
    middleware = ToolsetMiddleware(
        skills_dir=tmp_path,
        toolsets_by_skill={"database-analysis": {"database_analysis"}},
    )
    calls: list[str] = []

    def execute(request: ToolCallRequest) -> ToolMessage:
        calls.append(str(request.tool_call["name"]))
        return ToolMessage(
            content="executed",
            name=str(request.tool_call["name"]),
            tool_call_id=str(request.tool_call["id"]),
            status="success",
        )

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
        state=_active_skill_state(middleware, "database-analysis"),
        runtime=None,
    )
    allowed = middleware.wrap_tool_call(allowed_request, execute)
    assert isinstance(allowed, ToolMessage)
    assert allowed.status == "success"
    assert calls == ["database_sql_generate"]


def test_native_and_explicit_base_tools_are_unconditionally_visible_and_executable(tmp_path, monkeypatch) -> None:
    middleware = ToolsetMiddleware(skills_dir=tmp_path, toolsets_by_skill={})
    monkeypatch.setattr(middleware, "_runtime_tool_available", lambda _name: True)
    tools = [
        {"name": "read_file"},
        {"name": "write_file"},
        {"name": "task"},
        {"name": "execute"},
        {"name": "read_resource"},
        {"name": "web_search"},
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
        "web_search",
        "fetch_url",
        "inspect_file_version",
        "patch_file",
        "prepare_attachment_edit",
        "publish_attachment",
    ]

    calls: list[str] = []
    for tool_name in ("execute", "web_search", "fetch_url"):
        tool_request = ToolCallRequest(
            tool_call={"name": tool_name, "args": {}, "id": tool_name, "type": "tool_call"},
            tool=None,
            state={"messages": [], "active_skill_ids": []},
            runtime=None,
        )
        middleware.wrap_tool_call(
            tool_request,
            lambda request: (
                calls.append(str(request.tool_call["name"]))
                or ToolMessage(
                    content="executed",
                    name=str(request.tool_call["name"]),
                    tool_call_id=str(request.tool_call["id"]),
                    status="success",
                )
            ),
        )
    assert calls == ["execute", "web_search", "fetch_url"]

    bypass = ToolCallRequest(
        tool_call={
            "name": "stage_external_artifact",
            "args": {"file_path": "/tmp/guessed"},
            "id": "legacy-bypass",
            "type": "tool_call",
        },
        tool=None,
        state={"messages": [], "active_skill_ids": []},
        runtime=None,
    )
    denied = middleware.wrap_tool_call(
        bypass,
        lambda _request: (_ for _ in ()).throw(AssertionError("hidden legacy tool executed")),
    )
    assert denied.status == "error"
    assert "legacy lease owner" in str(denied.content)


def test_web_search_is_hidden_and_denied_when_no_provider_is_ready(tmp_path, monkeypatch) -> None:
    middleware = ToolsetMiddleware(skills_dir=tmp_path, toolsets_by_skill={})
    monkeypatch.setattr(middleware, "_runtime_tool_available", lambda name: name != "web_search")
    request = ModelRequest(
        model=None,
        messages=[],
        tools=[{"name": "read_file"}, {"name": "web_search"}],
        state={"messages": [], "active_skill_ids": []},
    )

    assert [tool["name"] for tool in middleware._visible_tools(request)] == ["read_file"]
    tool_request = ToolCallRequest(
        tool_call={"name": "web_search", "args": {}, "id": "search", "type": "tool_call"},
        tool=None,
        state={"messages": [], "active_skill_ids": []},
        runtime=None,
    )
    denied = middleware.wrap_tool_call(
        tool_request,
        lambda _request: (_ for _ in ()).throw(AssertionError("unavailable tool executed")),
    )
    assert denied.status == "error"
    assert "Settings > 联网搜索" in str(denied.content)


def test_legacy_lease_tools_are_visible_only_to_active_owner_and_audited(
    tmp_path,
) -> None:
    from graph.session_manager import session_manager
    from harness.models import RunRecord, RunStatus

    state = tmp_path / "state"
    state.mkdir()
    session_manager.initialize(state)
    session_manager.create_session("legacy-tool-session")
    run = RunRecord(
        run_id="run-legacy",
        query_id="query-legacy",
        session_id="legacy-tool-session",
        objective="resume old external draft",
        status=RunStatus.PREPARING,
    )
    session_manager.start_harness_run(
        "legacy-tool-session",
        run.model_dump(mode="json"),
    )
    session_manager.transition_run_status(
        "legacy-tool-session",
        run.run_id,
        RunStatus.RUNNING.value,
    )
    session_manager.upsert_external_artifact_lease(
        "legacy-tool-session",
        {
            "lease_id": "artifact-lease-legacy",
            "run_id": run.run_id,
            "query_id": run.query_id,
            "target_path": str(tmp_path / "external.txt"),
            "status": "staged",
            "expires_at": 4_102_444_800.0,
        },
    )
    middleware = ToolsetMiddleware(skills_dir=tmp_path, toolsets_by_skill={})
    runtime = SimpleNamespace(
        context={
            "session_id": "legacy-tool-session",
            "run_id": run.run_id,
            "goal_id": "",
            "goal_revision": None,
        }
    )
    request = ModelRequest(
        model=None,
        messages=[],
        tools=[{"name": "stage_external_artifact"}],
        state={"messages": [], "active_skill_ids": []},
        runtime=runtime,
    )
    assert [item["name"] for item in middleware._visible_tools(request)] == ["stage_external_artifact"]

    tool_request = ToolCallRequest(
        tool_call={
            "name": "stage_external_artifact",
            "args": {"file_path": str(tmp_path / "external.txt")},
            "id": "legacy-call",
            "type": "tool_call",
        },
        tool=None,
        state={"messages": [], "active_skill_ids": []},
        runtime=runtime,
    )
    result = middleware.wrap_tool_call(
        tool_request,
        lambda request: ToolMessage(
            content="compatibility call completed",
            name=str(request.tool_call["name"]),
            tool_call_id=str(request.tool_call["id"]),
            status="success",
        ),
    )
    assert result.status == "success"
    first = session_manager.audit_legacy_external_leases(
        "legacy-tool-session",
        release_id="release-1",
    )
    assert first["legacy_tool_call_count"] == 1
    assert first["active_lease_count"] == 1
    assert first["retirement_eligible"] is False

    for status in (RunStatus.EVALUATING, RunStatus.COMPLETED):
        session_manager.transition_run_status(
            "legacy-tool-session",
            run.run_id,
            status.value,
        )
    second = session_manager.audit_legacy_external_leases(
        "legacy-tool-session",
        release_id="release-2",
    )
    third = session_manager.audit_legacy_external_leases(
        "legacy-tool-session",
        release_id="release-3",
    )
    assert second["active_lease_count"] == 0
    assert third["zero_call_release_cycles"] == 2
    assert third["retirement_eligible"] is True
    assert middleware._visible_tools(request) == []


def test_active_skill_state_survives_message_compaction(tmp_path) -> None:
    _install_test_skill(tmp_path, "database-analysis", {"database_analysis"})
    middleware = ToolsetMiddleware(
        skills_dir=tmp_path,
        toolsets_by_skill={"database-analysis": {"database_analysis"}},
    )

    assert "database_sql_generate" in middleware._allowed_tool_names(
        {
            **_active_skill_state(middleware, "database-analysis"),
            "messages": [HumanMessage(content="Earlier messages were compacted")],
        }
    )


def _routed_profile(
    *skill_ids: str,
    missing_explicit_skill_ids: list[str] | None = None,
    explicit_skill_ids: set[str] | None = None,
    required_skill_ids: set[str] | None = None,
) -> dict:
    explicit_skill_ids = explicit_skill_ids or set()
    required_skill_ids = required_skill_ids or set()
    return RunTaskProfile(
        skill_candidates=[
            SkillCandidate(
                skill_id=skill_id,
                confidence=0.9,
                evidence="matched by semantic router",
                explicit=skill_id in explicit_skill_ids,
                required=skill_id in required_skill_ids,
            )
            for skill_id in skill_ids
        ],
        missing_explicit_skill_ids=missing_explicit_skill_ids or [],
        execution_route=("skill_first" if skill_ids else "missing_skill" if missing_explicit_skill_ids else "native"),
        native_fallback=not bool(missing_explicit_skill_ids),
    ).model_dump(mode="json")


def test_pdf_file_route_requires_installed_pdf_skill() -> None:
    installed = TaskProfileClassifier.classify(
        message="/Users/pet/Downloads/spec.pdf 说了什么",
        skill_catalog=[{"skill_id": "pdf", "name": "pdf"}],
    )
    candidate = next(item for item in installed.skill_candidates if item.skill_id == "pdf")
    assert candidate.required is True
    assert installed.execution_route == "skill_first"

    missing = TaskProfileClassifier.classify(
        message="/Users/pet/Downloads/spec.pdf 说了什么",
        skill_catalog=[],
    )
    assert missing.execution_route == "missing_skill"
    assert missing.native_fallback is False
    assert "missing_required_skill:pdf" in missing.reasons


def test_required_pdf_skill_barrier_blocks_direct_file_read() -> None:
    middleware = RequiredSkillBoundaryMiddleware()
    request = ToolCallRequest(
        tool_call={
            "name": "read_file",
            "args": {"file_path": "/Users/pet/Downloads/spec.pdf"},
            "id": "read-pdf-directly",
            "type": "tool_call",
        },
        tool=None,
        state={
            "messages": [],
            "active_skill_ids": [],
            "task_profile": _routed_profile("pdf", required_skill_ids={"pdf"}),
        },
        runtime=None,
    )
    executed: list[str] = []

    blocked = middleware.wrap_tool_call(
        request,
        lambda tool_request: executed.append(str(tool_request.tool_call["name"])),
    )

    assert isinstance(blocked, ToolMessage)
    assert blocked.status == "error"
    assert blocked.additional_kwargs["puddingclaw_control_plane"]["pending_skill_ids"] == ["pdf"]
    assert executed == []


def test_missing_pdf_skill_blocks_generic_tool_bypass() -> None:
    middleware = RequiredSkillBoundaryMiddleware()
    profile = RunTaskProfile(
        intents=["pdf_document"],
        execution_route="missing_skill",
        native_fallback=False,
        reasons=["missing_required_skill:pdf"],
    ).model_dump(mode="json")
    request = ToolCallRequest(
        tool_call={
            "name": "execute",
            "args": {"command": "pdftotext /Users/pet/Downloads/spec.pdf -"},
            "id": "bypass-pdf-skill",
            "type": "tool_call",
        },
        tool=None,
        state={"messages": [], "active_skill_ids": [], "task_profile": profile},
        runtime=None,
    )

    blocked = middleware.wrap_tool_call(request, lambda _request: None)

    assert isinstance(blocked, ToolMessage)
    assert blocked.additional_kwargs["puddingclaw_control_plane"] == {
        "type": "required_skill_missing",
        "missing_skill_ids": ["pdf"],
        "original_tool_executed": False,
    }


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
    assert len(routed.messages) == 2
    assert routed.messages[0] is original
    assert routed.messages[0].id == "user-message"
    assert routed.messages[0].content == original.content
    assert routed.messages[-1].additional_kwargs["puddingclaw_prompt_control"] is True
    assert "[PuddingClaw internal control: skill_routing]" in str(routed.messages[-1].content)


def test_explicit_skill_token_is_removed_from_model_task_text() -> None:
    middleware = SkillIntentRouterMiddleware()
    original = HumanMessage(
        content=(
            "/baoyu-design 重新设计byd_sales_launch_correlation.html的样式，并重新补充比亚迪每个月上市车系和款型明细"
        ),
        id="byd-redesign",
    )
    request = ModelRequest(
        model=None,
        messages=[original],
        tools=[],
        state={
            "messages": [original],
            "active_skill_ids": [],
            "task_profile": _routed_profile(
                "baoyu-design",
                explicit_skill_ids={"baoyu-design"},
            ),
        },
    )

    routed = middleware._request_with_routing_prompt(request)
    assert routed.messages[0] is original
    assert routed.messages[0].content == original.content
    routing_hint = str(routed.messages[-1].content)
    assert "规范化任务文本（仅供路由参考）：重新设计byd_sales_launch_correlation.html的样式，并重新补充比亚迪每个月上市车系和款型明细" in routing_hint
    assert "/baoyu-design 重新设计byd_sales_launch_correlation.html" not in routing_hint
    assert "/baoyu-design 是 Skill 调用标记" in routing_hint
    assert "该建议不决定工具可用性" in routing_hint
    assert request.messages == [original]


def test_explicit_skill_advice_does_not_block_parallel_workspace_read() -> None:
    middleware = SkillIntentRouterMiddleware()
    state = {
        "messages": [],
        "active_skill_ids": [],
        "task_profile": _routed_profile(
            "baoyu-design",
            explicit_skill_ids={"baoyu-design"},
        ),
    }
    executed: list[str] = []

    blocked_request = ToolCallRequest(
        tool_call={
            "name": "read_resource",
            "args": {"file_path": "/baoyu-design 重新设计byd_sales_launch_correlation.html"},
            "id": "bad-parallel-read",
            "type": "tool_call",
        },
        tool=None,
        state=state,
        runtime=None,
    )
    result = middleware.wrap_tool_call(
        blocked_request,
        lambda request: executed.append(str(request.tool_call["name"])),
    )

    assert result is None
    assert executed == ["read_resource"]


def test_explicit_skill_advice_does_not_filter_sibling_model_tool_call() -> None:
    middleware = SkillIntentRouterMiddleware()
    request = ModelRequest(
        model=None,
        messages=[HumanMessage(content="/baoyu-design 重新设计byd_sales_launch_correlation.html")],
        tools=[],
        state={
            "messages": [],
            "active_skill_ids": [],
            "task_profile": _routed_profile(
                "baoyu-design",
                explicit_skill_ids={"baoyu-design"},
            ),
        },
    )
    response = ModelResponse(
        result=[
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "read_file",
                        "args": {"file_path": "/skills/baoyu-design/SKILL.md"},
                        "id": "read-skill",
                        "type": "tool_call",
                    },
                    {
                        "name": "read_resource",
                        "args": {"file_path": "/baoyu-design 重新设计byd_sales_launch_correlation.html"},
                        "id": "bad-parallel-read",
                        "type": "tool_call",
                    },
                ],
            )
        ]
    )

    filtered = middleware.wrap_model_call(request, lambda _request: response)

    assert isinstance(filtered.result[0], AIMessage)
    assert [tool_call["id"] for tool_call in filtered.result[0].tool_calls] == [
        "read-skill",
        "bad-parallel-read",
    ]


def test_explicit_skill_advisor_allows_skill_and_workspace_reads() -> None:
    middleware = SkillIntentRouterMiddleware()
    profile = _routed_profile(
        "baoyu-design",
        explicit_skill_ids={"baoyu-design"},
    )
    skill_read_request = ToolCallRequest(
        tool_call={
            "name": "read_file",
            "args": {"file_path": "/skills/baoyu-design/SKILL.md"},
            "id": "read-skill",
            "type": "tool_call",
        },
        tool=None,
        state={"messages": [], "active_skill_ids": [], "task_profile": profile},
        runtime=None,
    )
    skill_read = middleware.wrap_tool_call(
        skill_read_request,
        lambda request: ToolMessage(
            content="skill loaded",
            name=str(request.tool_call["name"]),
            tool_call_id=str(request.tool_call["id"]),
            status="success",
        ),
    )

    workspace_read_request = ToolCallRequest(
        tool_call={
            "name": "read_file",
            "args": {"file_path": "/workspace/byd_sales_launch_correlation.html"},
            "id": "read-workspace",
            "type": "tool_call",
        },
        tool=None,
        state={
            "messages": [],
            "active_skill_ids": ["baoyu-design"],
            "task_profile": profile,
        },
        runtime=None,
    )
    workspace_read = middleware.wrap_tool_call(
        workspace_read_request,
        lambda request: ToolMessage(
            content="workspace file loaded",
            name=str(request.tool_call["name"]),
            tool_call_id=str(request.tool_call["id"]),
            status="success",
        ),
    )

    assert skill_read.status == "success"
    assert workspace_read.status == "success"


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
            "task_profile": _routed_profile(missing_explicit_skill_ids=["missing-demo"]),
        },
    )

    routed = middleware._request_with_routing_prompt(request)

    assert routed.messages[0] is original
    assert routed.messages[0].content == original.content
    control = str(routed.messages[-1].content)
    assert "missing-demo" in control
    assert "当前未安装" in control
