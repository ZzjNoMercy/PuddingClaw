from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import pytest
from langchain_core.messages import HumanMessage

from analytics.nl2sql.service import _bounded_profile_type_names
from analytics.nl2sql.sql_runner import SqlRunnerError, validate_readonly_sql
from graph.database_evidence import DatabaseEvidenceRegistry
from tools.database.sql_validate_tool import (
    _eav_bindings,
    _validate_postgres_plan,
    _validate_real_columns,
)
from tools.toolsets import TOOL_CONTROL_DESCRIPTORS, TOOLSETS


def test_evidence_profile_selection_preserves_entity_relevance_order() -> None:
    from analytics.nl2sql.evidence_service import _prompt_entity_type_names

    entities = [
        {
            "canonical_name": "激光雷达线数",
            "table_column": "public.vehicle_params.type_name",
        },
        {
            "canonical_name": "激光雷达数量",
            "table_column": "public.vehicle_params.type_name",
        },
        {
            "canonical_name": "激光雷达型号",
            "table_column": "public.vehicle_params.type_name",
        },
        {
            "canonical_name": "前方感知摄像头类型",
            "table_column": "public.vehicle_params.type_name",
        },
    ]

    recalled = _prompt_entity_type_names(entities)

    assert _bounded_profile_type_names(recalled) == [
        "激光雷达线数",
        "激光雷达数量",
        "激光雷达型号",
    ]
    assert _bounded_profile_type_names(recalled, limit=None) == [
        "激光雷达线数",
        "激光雷达数量",
        "激光雷达型号",
        "前方感知摄像头类型",
    ]


def test_evidence_profile_selection_deduplicates_repeated_entity_types() -> None:
    from analytics.nl2sql.evidence_service import _prompt_entity_type_names

    entities = [
        {
            "canonical_name": "激光雷达线数",
            "table_column": "public.vehicle_params.type_name",
        },
        {
            "canonical_name": "激光雷达线数",
            "table_column": "public.vehicle_params.type_name",
        },
        {
            "canonical_name": "无关品牌",
            "table_column": "public.vehicle_params.brand",
        },
    ]

    assert _prompt_entity_type_names(entities) == ["激光雷达线数"]


def test_agent_validator_binds_eav_value_to_one_type_name() -> None:
    bindings, unprovable = _eav_bindings(
        "SELECT vp.type_value FROM public.vehicle_params vp "
        "WHERE vp.type_name = '激光雷达线数' AND vp.type_value = '896线'"
    )

    assert unprovable == []
    assert bindings == [{"alias": "vp", "type_name": "激光雷达线数", "type_value": "896线"}]


def test_agent_validator_rejects_dynamic_eav_value_binding() -> None:
    bindings, unprovable = _eav_bindings(
        "SELECT vp.type_value FROM public.vehicle_params vp "
        "WHERE vp.type_name = '激光雷达线数' AND vp.type_value LIKE '%线'"
    )

    assert bindings == []
    assert unprovable


def test_agent_validator_ignores_negated_eav_exclusion_literals() -> None:
    bindings, unprovable = _eav_bindings(
        "SELECT vp.type_value FROM public.vehicle_params vp "
        "WHERE vp.type_name = '激光雷达线数' "
        "AND vp.type_value NOT IN ('', '-', '无', '未配备', '不配备')"
    )

    assert bindings == []
    assert unprovable == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "database_error",
    [
        "cannot cast type text[] to integer",
        "function count(character varying, character varying, character varying) does not exist",
    ],
)
async def test_postgres_plan_validation_rejects_dialect_and_type_errors(
    monkeypatch: pytest.MonkeyPatch,
    database_error: str,
) -> None:
    import tools.database.sql_validate_tool as module

    statements: list[str] = []

    class Connection:
        async def execute(self, statement: Any) -> None:
            rendered = str(statement)
            statements.append(rendered)
            if rendered.startswith("EXPLAIN"):
                raise RuntimeError(database_error)

    class ConnectionContext:
        async def __aenter__(self) -> Connection:
            return Connection()

        async def __aexit__(self, *_args: object) -> None:
            return None

    class Engine:
        def connect(self) -> ConnectionContext:
            return ConnectionContext()

        async def dispose(self) -> None:
            return None

    monkeypatch.setattr(module, "create_async_engine", lambda *_args, **_kwargs: Engine())
    monkeypatch.setattr(module, "database_source_url", lambda _source: "postgresql://test")

    with pytest.raises(SqlRunnerError) as captured:
        await _validate_postgres_plan("SELECT 1", {})

    assert captured.value.error_code == "sql_dialect_validation_failed"
    assert database_error in str(captured.value)
    assert any(item.startswith("EXPLAIN (FORMAT JSON) SELECT 1") for item in statements)


def test_strict_schema_scope_does_not_authorize_private_table_by_bare_name() -> None:
    with pytest.raises(SqlRunnerError):
        validate_readonly_sql(
            "SELECT * FROM audit_log",
            allowed_tables=["private.audit_log"],
            require_schema_qualified=True,
        )


def test_agent_parser_defers_unregistered_scalar_function_while_legacy_remains_strict() -> None:
    sql = "SELECT regexp_split_to_table(type_value, ',') FROM vehicle_params"

    assert validate_readonly_sql(
        sql,
        allowed_tables=["vehicle_params"],
        allow_unregistered_functions=True,
    ) == sql
    with pytest.raises(SqlRunnerError, match="registry"):
        validate_readonly_sql(sql, allowed_tables=["vehicle_params"])
    with pytest.raises(SqlRunnerError, match="未授权"):
        validate_readonly_sql(
            "SELECT pg_read_file('/etc/passwd') FROM vehicle_params",
            allowed_tables=["vehicle_params"],
            allow_unregistered_functions=True,
        )


def test_evidence_registry_is_run_and_table_scoped() -> None:
    registry = DatabaseEvidenceRegistry(ttl_seconds=60)
    item = registry.register(
        session_id="session-1",
        query_id="query-1",
        run_id="run-1",
        goal_id="",
        goal_revision=None,
        database_source_id="db-1",
        allowed_tables=["public.vehicle_params"],
        payload={"observations": []},
    )

    assert registry.get(
        item["id"],
        session_id="session-1",
        query_id="query-1",
        run_id="run-1",
        goal_id="",
        goal_revision=None,
        database_source_id="db-1",
        allowed_tables=["public.vehicle_params"],
    ) is not None
    assert registry.get(
        item["id"],
        session_id="session-1",
        query_id="query-1",
        run_id="run-2",
        goal_id="",
        goal_revision=None,
        database_source_id="db-1",
        allowed_tables=["public.vehicle_params"],
    ) is None


def test_evidence_registry_allows_table_subset_but_not_scope_expansion() -> None:
    registry = DatabaseEvidenceRegistry(ttl_seconds=60)
    item = registry.register(
        session_id="session-subset",
        query_id="query-subset",
        run_id="run-subset",
        goal_id="",
        goal_revision=None,
        database_source_id="db-1",
        allowed_tables=["public.vehicle_params", "public.vehicle_model_base"],
        payload={"observations": []},
    )
    common = {
        "session_id": "session-subset",
        "query_id": "query-subset",
        "run_id": "run-subset",
        "goal_id": "",
        "goal_revision": None,
        "database_source_id": "db-1",
    }

    assert registry.get(item["id"], allowed_tables=["public.vehicle_params"], **common) is not None
    assert registry.get(
        item["id"],
        allowed_tables=["public.vehicle_params", "public.unauthorized"],
        **common,
    ) is None


@pytest.mark.asyncio
async def test_real_column_validation_accepts_order_by_output_alias(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import tools.database.sql_validate_tool as module

    monkeypatch.setattr(
        module,
        "_load_columns",
        lambda *_args: _async_value({"vehicle_params": {"type_value"}}),
    )

    await _validate_real_columns(
        "SELECT type_value, COUNT(*) AS row_count "
        "FROM vehicle_params GROUP BY type_value ORDER BY row_count DESC",
        {},
        ["vehicle_params"],
    )


@pytest.mark.asyncio
async def test_real_column_validation_does_not_allow_output_alias_in_where(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import tools.database.sql_validate_tool as module

    monkeypatch.setattr(
        module,
        "_load_columns",
        lambda *_args: _async_value({"vehicle_params": {"type_value"}}),
    )

    with pytest.raises(SqlRunnerError, match="row_count"):
        await _validate_real_columns(
            "SELECT COUNT(*) AS row_count FROM vehicle_params WHERE row_count > 0",
            {},
            ["vehicle_params"],
        )


def test_agent_database_tools_are_registered_with_explicit_controls() -> None:
    assert {"database_evidence_search", "database_sql_validate"} <= TOOLSETS["database_analysis"]
    assert TOOL_CONTROL_DESCRIPTORS["database_evidence_search"].side_effect == "none"
    assert TOOL_CONTROL_DESCRIPTORS["database_sql_validate"].side_effect == "internal_mutation"


def test_database_agent_route_uses_evidence_and_validator() -> None:
    import graph.middlewares.tool_intent_router as module

    decision = module.ToolIntentRouterMiddleware()._classify_intent("从数据库查询空气悬架配置率")

    assert decision["preferred_tools"] == [
        "database_evidence_search",
        "database_sql_validate",
        "database_sql_execute",
    ]
    assert "database_sql_generate" not in decision["preferred_tools"]
    assert "必须优先调用 database_sql_generate" not in decision["routing_prompt"]


def test_fallback_policy_fails_closed_for_every_error() -> None:
    from analytics.nl2sql.agent_path_policy import fallback_policy

    for error_code in (
        "database_unavailable",
        "evidence_search_failed",
        "eav_evidence_required",
        "sql_guardrail_conflict",
    ):
        policy = fallback_policy(error_code)
        assert policy["eligible"] is False
        assert policy["enabled"] is False
        assert policy["target_path"] == ""

    assert fallback_policy(
        "database_unavailable",
        config={"database_agent_sql_fallback_enabled": True},
    )["eligible"] is False


def test_database_path_events_are_append_only_and_scope_filterable(tmp_path: object) -> None:
    from graph.session_manager import SessionManager

    manager = SessionManager()
    manager.initialize(tmp_path)  # type: ignore[arg-type]
    manager.create_session("session-path")
    manager.record_database_path_event(
        "session-path",
        "event-1",
        {
            "event_id": "event-1",
            "event_type": "fallback_offered",
            "query_id": "query-1",
            "run_id": "run-1",
            "goal_id": "",
            "goal_revision": None,
            "target_path": "legacy_generation",
            "created_at": 1,
        },
    )
    manager.record_database_path_event(
        "session-path",
        "event-2",
        {
            "event_id": "event-2",
            "event_type": "fallback_used",
            "query_id": "query-2",
            "run_id": "run-2",
            "goal_id": "",
            "goal_revision": None,
            "target_path": "legacy_generation",
            "created_at": 2,
        },
    )

    events = manager.list_database_path_events("session-path", query_id="query-1", run_id="run-1")
    assert [item["event_id"] for item in events] == ["event-1"]


def test_historical_replay_reports_unsupported_and_false_rejection_metrics(tmp_path: object) -> None:
    from analytics.nl2sql.agent_sql_replay import load_replay_cases, replay_static_contract

    path = tmp_path / "replay.jsonl"  # type: ignore[union-attr]
    path.write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "case_id": "cte-1",
                        "question": "统计车型数",
                        "sql": "WITH base AS (SELECT 1 AS n) SELECT n FROM base",
                        "database_source_id": "db-1",
                        "allowed_tables": ["public.vehicle_params"],
                    },
                    ensure_ascii=False,
                ),
                json.dumps(
                    {
                        "case_id": "write-1",
                        "question": "恶意写入",
                        "sql": "DELETE FROM public.vehicle_params",
                        "database_source_id": "db-1",
                        "allowed_tables": ["public.vehicle_params"],
                        "expected_status": "rejected",
                    },
                    ensure_ascii=False,
                ),
            ]
        ),
        encoding="utf-8",
    )

    summary = replay_static_contract(load_replay_cases(path))
    assert summary.total == 2
    assert summary.passed == 1
    assert summary.expected_failures == 1
    assert summary.false_rejection_rate == 0


@pytest.mark.asyncio
async def test_evidence_infrastructure_failure_does_not_expose_legacy_tool(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import tools.database.evidence_search_tool as module

    async def unavailable(**_kwargs: object) -> dict[str, object]:
        raise ConnectionError("database unavailable")

    monkeypatch.setattr(module, "search_database_evidence", unavailable)
    result = await module.DatabaseEvidenceSearchTool()._arun(
        question="查询车型数量",
        runtime=SimpleNamespace(context={"run_id": "run-fallback", "goal_id": ""}),
    )
    payload = json.loads(result)

    assert payload["status"] == "rejected"
    assert payload["code"] == "database_unavailable"
    assert payload["recoverable"] is False
    assert payload["fallback"]["available"] is False
    assert payload["fallback"]["tool"] is None
    assert payload["fallback"]["blocked_reason"] == "legacy_tool_not_exposed"


@pytest.mark.asyncio
async def test_legacy_generator_admission_guard_blocks_agent_run_without_state_marker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import tools.database.sql_generate_tool as module

    monkeypatch.setattr(module, "record_legacy_fallback_used_if_offered", lambda **_kwargs: None)
    result = await module.DatabaseSqlGenerateTool(
        session_id="session-agent",
        query_id="query-agent",
    )._arun(
        question="查询产品配置",
        runtime=SimpleNamespace(
            context={"session_id": "session-agent", "run_id": "run-agent"},
            state={"analytics_model_id": "产品配置分析"},
            tool_call_id="call-legacy",
        ),
    )

    assert "兼容路径未获准" in result


@pytest.mark.asyncio
async def test_validate_fails_closed_without_trusted_scope() -> None:
    from tools.database.sql_validate_tool import DatabaseSqlValidateTool

    result = await DatabaseSqlValidateTool(session_id="session-1")._arun(
        sql="SELECT 1",
        runtime=SimpleNamespace(state={"messages": [HumanMessage(content="查询数据")]}, context={}),
    )

    payload = json.loads(result)
    assert payload["status"] == "rejected"
    assert payload["code"] == "semantic_context_unavailable"


@pytest.mark.asyncio
async def test_validate_uses_evidence_as_advisory_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import tools.database.sql_validate_tool as module

    class SessionContext:
        async def __aenter__(self) -> object:
            return object()

        async def __aexit__(self, *_args: object) -> None:
            return None

    route = SimpleNamespace(
        database_source_id="db-1",
        source_name="Test DB",
        database="test",
        dialect="PostgreSQL",
        table_names=["public.vehicle_params"],
        available_tables=["public.vehicle_params"],
        candidates=[],
        confidence=1.0,
        reason="test",
        prompt_context="",
        alias_resolutions=[],
    )
    semantic = SimpleNamespace(
        semantic_hash="sha256:semantic",
        model_version="1",
        trace={"matched": [], "references": []},
        semantic_asset_ids=("grain:car_model", "dimension:vehicle_series"),
    )
    monkeypatch.setattr(module, "get_sessionmaker", lambda: lambda: SessionContext())
    monkeypatch.setattr(module, "route_database_tables", lambda *_args: _async_value(route))
    monkeypatch.setattr(module, "get_database_source", lambda *_args: _async_value({"id": "db-1"}))
    monkeypatch.setattr(module, "_validate_real_columns", lambda *_args: _async_value(None))
    monkeypatch.setattr(module, "_validate_postgres_plan", lambda *_args: _async_value(None))
    monkeypatch.setattr(module, "compile_semantic_query_context", lambda **_kwargs: semantic)
    monkeypatch.setattr(
        module,
        "session_manager",
        SimpleNamespace(is_initialized=True, get_permission_policy=lambda _sid: {"policy_epoch": 4}),
    )

    from graph.database_evidence import database_evidence_registry

    evidence = database_evidence_registry.register(
        session_id="session-v2",
        query_id="query-v2",
        run_id="run-v2",
        goal_id="",
        goal_revision=None,
        database_source_id="db-1",
        allowed_tables=route.table_names,
        payload={
            "observations": [
                {
                    "table": "public.vehicle_params",
                    "type_name": "激光雷达线数",
                    "values": ["896线"],
                    "complete": True,
                    "profile_revision": "sha256:profile",
                }
            ]
        },
        trusted_question_sha256=module._hash("哪些车搭载了896线激光雷达？"),
        analytics_model_id="",
        analytics_model_revision="1",
        semantic_context_hash="sha256:semantic",
        selected_semantic_asset_ids=["grain:car_model", "dimension:vehicle_series"],
    )
    runtime = SimpleNamespace(
        context={"run_id": "run-v2", "goal_id": "", "goal_revision": None},
        state={
            "_run_objective": "哪些车搭载了896线激光雷达？",
            "analytics_model_id": "",
            "allowed_semantic_asset_ids": ["grain:car_model", "dimension:vehicle_series"],
            "messages": [],
        },
    )
    result = await module.DatabaseSqlValidateTool(session_id="session-v2", query_id="query-v2")._arun(
        sql=(
            "SELECT vp.type_value FROM public.vehicle_params vp "
            "WHERE vp.type_name = '激光雷达线数' AND vp.type_value = '896线'"
        ),
        evidence_search_id=evidence["id"],
        selected_semantic_asset_ids=["grain:car_model"],
        runtime=runtime,
    )

    payload = json.loads(result)
    assert payload["status"] == "passed"
    assert payload["provenance"] == "agent_authored"
    assert payload["sql_submission_id"].startswith("sql-submission-")
    submission = module.database_sql_revision_resume_registry.get_submission(
        payload["sql_submission_id"],
        session_id="session-v2",
        query_id="query-v2",
        run_id="run-v2",
        goal_id="",
        goal_revision=None,
    )
    assert submission is not None
    assert submission.request["selected_semantic_asset_ids"] == ["grain:car_model"]

    advisory_result = await module.DatabaseSqlValidateTool(
        session_id="session-v2",
        query_id="query-v2",
    )._arun(
        sql=(
            "SELECT vp.type_value FROM public.vehicle_params vp "
            "WHERE vp.type_name = '激光雷达线数' AND vp.type_value LIKE '%线'"
        ),
        evidence_search_id="expired-evidence",
        selected_semantic_asset_ids=["grain:car_model"],
        runtime=runtime,
    )
    advisory_payload = json.loads(advisory_result)
    assert advisory_payload["status"] == "passed"
    assert advisory_payload["execution_allowed"] is True
    assert all(item["blocking"] is False for item in advisory_payload["warnings"])
    assert {item["code"] for item in advisory_payload["warnings"]} >= {
        "evidence_unavailable",
        "eav_binding_unproven",
    }


@pytest.mark.asyncio
async def test_agent_execution_uses_only_registered_submission_and_scope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import tools.database.sql_execute_tool as module
    from graph.database_sql_revision_resume import database_sql_revision_resume_registry

    route = SimpleNamespace(
        database_source_id="db-exec",
        table_names=["public.vehicle_params"],
        source_name="Execution DB",
        database="test",
        dialect="PostgreSQL",
        available_tables=["public.vehicle_params"],
        candidates=[],
        confidence=1.0,
        reason="test",
        prompt_context="",
        alias_resolutions=[],
    )
    submission = database_sql_revision_resume_registry.register_submission(
        session_id="session-exec",
        query_id="query-exec",
        run_id="run-exec",
        goal_id="",
        goal_revision=None,
        sql="SELECT 1",
        request={
            "trusted_question": "查询车型数量",
            "database_source_id": "db-exec",
            "table_names": list(route.table_names),
            "analytics_model_id": "",
            "selected_semantic_asset_ids": [],
        },
    )
    receipt = database_sql_revision_resume_registry.register_agent_validation_receipt(
        submission=submission,
        database_source_id="db-exec",
        allowed_tables=route.table_names,
        metadata={
            "trusted_question_sha256": "sha256:trusted-question",
            "analytics_model_id": "",
            "analytics_model_revision": "",
            "semantic_context_hash": "sha256:semantic-exec",
            "route_hash": "sha256:route",
            "evidence_search_id": "expired-evidence",
            "permission_epoch": 1,
        },
    )

    async def fake_resolve(*_args: object, **kwargs: object) -> tuple[dict[str, str], dict[str, object], list[str]]:
        assert kwargs["enforce_selected_tables"] is True
        return {"id": "db-exec"}, {"id": "db-exec", "name": "Execution DB"}, list(route.table_names)

    async def fake_run(*_args: object, **_kwargs: object) -> SimpleNamespace:
        return SimpleNamespace(
            rows=[],
            columns=[],
            row_count=0,
            total_row_count=0,
            preview_count=0,
            omitted_count=0,
            is_complete=True,
            limited=False,
            estimated_tokens=0,
            profile={},
            result_id=None,
            result_store={},
            actions=[],
        )

    monkeypatch.setattr(module, "resolve_database_source_scope", fake_resolve)
    monkeypatch.setattr(module, "run_readonly_sql", fake_run)
    monkeypatch.setattr(
        module,
        "session_manager",
        SimpleNamespace(is_initialized=True, get_permission_policy=lambda _sid: {"policy_epoch": 1}),
    )
    result = await module.DatabaseSqlExecuteTool(session_id="session-exec", query_id="query-exec")._arun(
        sql_submission_id=submission.id,
        agent_validation_receipt_id=receipt.id,
        runtime=SimpleNamespace(
            context={"run_id": "run-exec", "goal_id": "", "goal_revision": None},
            state={"_run_objective": "查询车型数量", "analytics_model_id": "", "messages": []},
        ),
    )

    assert "Agent submission 登记结果" in result
    assert submission.id in result


async def _async_value(value: object) -> object:
    return value
