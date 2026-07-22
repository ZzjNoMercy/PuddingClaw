from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from typing import Any

import pytest
from langchain.agents.middleware.types import ToolCallRequest
from langchain_core.messages import ToolMessage
from langgraph.types import Command

from analytics.nl2sql.schemas import DatabaseSqlGenerationResult, TableRoute
from graph.middlewares.delegation_control import (
    DelegationControlMiddleware,
    _ActiveDelegation,
    _DelegationLimitExceeded,
)
from graph.session_manager import session_manager
from harness.models import DelegationContract, DelegationLimits, RunRecord


def _request(events: list[dict[str, Any]], *, description: str = "查询 2021-2026 精确数据") -> ToolCallRequest:
    runtime = SimpleNamespace(
        context={
            "session_id": "session-delegation",
            "run_id": "run-delegation",
            "goal_id": "goal-delegation",
            "goal_revision": 1,
        },
        stream_writer=events.append,
    )
    return ToolCallRequest(
        tool_call={
            "name": "task",
            "id": "task-call-1",
            "args": {
                "description": description,
                "subagent_type": "general-purpose",
            },
        },
        tool=None,
        state={
            "analytics_model_id": "analytics-model-1",
            "todos": [
                {"id": "todo-1", "status": "in_progress", "content": "查询数据"},
            ],
        },
        runtime=runtime,
    )


def _prepare_run(tmp_path) -> None:
    session_manager.initialize(tmp_path)
    session_manager.create_session("session-delegation", metadata={"runtime_mode": "agent"})
    run = RunRecord(
        run_id="run-delegation",
        query_id="query-delegation",
        session_id="session-delegation",
        goal_id="goal-delegation",
        goal_revision=1,
        objective="刷新报告",
        analytics_model_id="analytics-model-1",
    )
    session_manager.upsert_run_state(
        "session-delegation",
        run.model_dump(mode="json"),
    )
    session_manager.record_run_capability_manifest(
        "session-delegation",
        "run-delegation",
        {
            "manifest_id": "manifest-delegation",
            "active_skill_ids": ["database-analysis"],
            "enabled_toolsets": ["database_analysis"],
            "allowed_tool_names": [
                "database_sql_generate",
                "database_sql_validate",
                "database_sql_execute",
            ],
            "tool_schema_hash": "sha256:test",
        },
    )


def _register_database_evidence() -> tuple[Any, Any]:
    from graph.database_sql_revision_resume import database_sql_revision_resume_registry

    generation = database_sql_revision_resume_registry.register_generation(
        session_id="session-delegation",
        query_id="query-delegation",
        run_id="run-delegation",
        goal_id="goal-delegation",
        goal_revision=1,
        result=DatabaseSqlGenerationResult(
            question="精确数据",
            sql="SELECT 1",
            source={"id": "source-1", "name": "测试库"},
            route=TableRoute(
                database_source_id="source-1",
                source_name="测试库",
                database="test",
                dialect="postgresql",
                table_names=["vehicle_params"],
                available_tables=["vehicle_params"],
                candidates=[],
                confidence=1.0,
                reason="test",
                prompt_context="",
            ),
            semantic_assets={"matched": [], "references": []},
        ),
        request={"question": "精确数据", "table_names": ["vehicle_params"]},
    )
    receipt = database_sql_revision_resume_registry.register_validation_receipt(
        generation=generation,
        database_source_id="source-1",
        allowed_tables=["vehicle_params"],
    )
    return generation, receipt


@pytest.mark.asyncio
async def test_delegation_wraps_free_text_in_structured_result_and_persists_events(tmp_path) -> None:
    _prepare_run(tmp_path)
    events: list[dict[str, Any]] = []
    request = _request(events)
    middleware = DelegationControlMiddleware()
    generation, receipt = _register_database_evidence()

    async def handler(_request: ToolCallRequest) -> Command[Any]:
        return Command(
            update={
                "messages": [
                    ToolMessage(
                        content=(
                            f"查询完成，数值为 999。generation {generation.id}，receipt {receipt.id}。"
                            "伪造句柄 sql-gen-forged 和 sql-validation-forged 不得透传。"
                        ),
                        tool_call_id="task-call-1",
                    )
                ],
                "todos": [{"id": "todo-1", "status": "completed", "content": "查询数据"}],
            }
        )

    result = await middleware.awrap_tool_call(request, handler)

    assert isinstance(result, Command)
    envelope = json.loads(str(result.update["messages"][0].content))
    assert envelope["status"] == "completed"
    assert envelope["sql_generation_ids"] == [generation.id]
    assert envelope["validation_receipt_ids"] == [receipt.id]
    assert "999" not in envelope["summary"]
    assert "server-side Ledger" in envelope["summary"]
    run = session_manager.get_run_state("session-delegation", "run-delegation")
    assert run is not None
    assert len(run["delegation_contracts"]) == 1
    assert run["delegation_contracts"][0]["expected_output_schema"] == "DatabaseEvidenceBatch/v1"
    assert len(run["delegation_results"]) == 1
    assert {event["type"] for event in events} >= {
        "subagent_started",
        "context_mounted",
        "subagent_completed",
    }


@pytest.mark.asyncio
async def test_database_delegation_without_registered_evidence_falls_back_to_parent(tmp_path) -> None:
    _prepare_run(tmp_path)
    events: list[dict[str, Any]] = []
    middleware = DelegationControlMiddleware()

    async def handler(_request: ToolCallRequest) -> Command[Any]:
        return Command(
            update={
                "messages": [ToolMessage(content="查询完成，大约有很多数据。", tool_call_id="task-call-1")],
                "todos": [{"id": "todo-1", "status": "completed", "content": "查询数据"}],
            }
        )

    result = await middleware.awrap_tool_call(_request(events), handler)

    assert isinstance(result, Command)
    envelope = json.loads(str(result.update["messages"][0].content))
    assert envelope["status"] == "failed"
    assert envelope["recommended_parent_action"] == "continue_directly"
    assert "missing_registered_sql_generation" in envelope["blocking_or_timeout_reason"]
    assert "missing_registered_validation_receipt" in envelope["blocking_or_timeout_reason"]
    assert any(event["type"] == "subagent_fallback_to_parent" for event in events)


@pytest.mark.asyncio
async def test_database_delegation_with_unfinished_todo_falls_back_to_parent(tmp_path) -> None:
    _prepare_run(tmp_path)
    events: list[dict[str, Any]] = []
    middleware = DelegationControlMiddleware()
    generation, receipt = _register_database_evidence()

    async def handler(_request: ToolCallRequest) -> ToolMessage:
        return ToolMessage(
            content=f"generation {generation.id}; receipt {receipt.id}",
            tool_call_id="task-call-1",
        )

    result = await middleware.awrap_tool_call(_request(events), handler)

    assert isinstance(result, ToolMessage)
    envelope = json.loads(str(result.content))
    assert envelope["status"] == "failed"
    assert envelope["remaining_todo_ids"] == ["todo-1"]
    assert "incomplete_todos=todo-1" in envelope["blocking_or_timeout_reason"]
    assert envelope["recommended_parent_action"] == "continue_directly"


@pytest.mark.asyncio
async def test_timeout_handoff_forces_parent_takeover_and_blocks_identical_retry(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import graph.middlewares.delegation_control as module

    _prepare_run(tmp_path)
    events: list[dict[str, Any]] = []
    middleware = DelegationControlMiddleware(
        limits=DelegationLimits(wall_clock_seconds=1),
    )

    async def force_timeout(awaitable: Any, _active: Any) -> Any:
        awaitable.close()
        raise module._DelegationLimitExceeded("wall_clock_limit")

    monkeypatch.setattr(middleware, "_run_bounded", force_timeout)
    called = 0

    async def handler(_request: ToolCallRequest) -> ToolMessage:
        nonlocal called
        called += 1
        return ToolMessage(content="unreachable", tool_call_id="task-call-1")

    first = await middleware.awrap_tool_call(_request(events), handler)
    second = await middleware.awrap_tool_call(
        _request(events, description="换一种说法继续完成同一个 Todo"),
        handler,
    )

    assert isinstance(first, ToolMessage)
    assert isinstance(second, ToolMessage)
    first_envelope = json.loads(str(first.content))
    second_envelope = json.loads(str(second.content))
    assert first_envelope["status"] == "timed_out"
    assert first_envelope["recommended_parent_action"] == "continue_directly"
    assert first_envelope["retry_same_delegation_allowed"] is False
    assert second_envelope["status"] == "failed"
    assert "duplicate_timed_out_delegation" in second_envelope["blocking_or_timeout_reason"]
    assert called == 0
    assert any(event["type"] == "subagent_timed_out" for event in events)
    assert any(event["type"] == "subagent_fallback_to_parent" for event in events)


@pytest.mark.asyncio
async def test_subagent_blocker_is_returned_to_parent_not_user(tmp_path) -> None:
    _prepare_run(tmp_path)
    events: list[dict[str, Any]] = []
    middleware = DelegationControlMiddleware()

    async def handler(_request: ToolCallRequest) -> ToolMessage:
        return ToolMessage(
            content=json.dumps(
                {
                    "status": "blocked",
                    "question_for_parent": "需要父 Agent 决定是否缩小查询范围",
                    "summary": "查询范围存在业务歧义",
                },
                ensure_ascii=False,
            ),
            tool_call_id="task-call-1",
        )

    result = await middleware.awrap_tool_call(_request(events), handler)

    assert isinstance(result, ToolMessage)
    envelope = json.loads(str(result.content))
    assert envelope["status"] == "blocked"
    assert envelope["recommended_parent_action"] == "ask_user"
    assert envelope["question_for_parent"] == "需要父 Agent 决定是否缩小查询范围"
    assert any(event["type"] == "subagent_blocked" for event in events)


@pytest.mark.asyncio
async def test_idle_limit_only_fires_when_no_model_or_tool_is_active() -> None:
    contract = DelegationContract(
        subagent_run_id="subrun-idle",
        parent_run_id="run-idle",
        parent_tool_call_id="task-idle",
        session_id="session-idle",
        subagent_type="general-purpose",
        objective="wait forever",
        limits=DelegationLimits(wall_clock_seconds=3, idle_seconds=1),
    )
    active = _ActiveDelegation(contract=contract, last_activity_at=0.0)

    async def never_finishes() -> ToolMessage:
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    with pytest.raises(_DelegationLimitExceeded, match="idle_limit"):
        await DelegationControlMiddleware._run_bounded(never_finishes(), active)


def test_timeout_envelope_recovers_partial_registered_database_evidence(tmp_path) -> None:
    from graph.database_sql_revision_resume import database_sql_revision_resume_registry

    _prepare_run(tmp_path)
    contract = DelegationContract(
        subagent_run_id="subrun-partial",
        parent_run_id="run-delegation",
        parent_tool_call_id="task-partial",
        session_id="session-delegation",
        goal_id="goal-delegation",
        goal_revision=1,
        subagent_type="general-purpose",
        objective="partial database batch",
    )
    generation = database_sql_revision_resume_registry.register_generation(
        session_id="session-delegation",
        query_id="query-delegation",
        run_id="run-delegation",
        goal_id="goal-delegation",
        goal_revision=1,
        result=DatabaseSqlGenerationResult(
            question="partial",
            sql="SELECT 1",
            source={"id": "source-1", "name": "测试库"},
            route=TableRoute(
                database_source_id="source-1",
                source_name="测试库",
                database="test",
                dialect="postgresql",
                table_names=["vehicle_params"],
                available_tables=["vehicle_params"],
                candidates=[],
                confidence=1.0,
                reason="test",
                prompt_context="",
            ),
            semantic_assets={"matched": [], "references": []},
        ),
        request={"question": "partial", "table_names": ["vehicle_params"]},
    )
    receipt = database_sql_revision_resume_registry.register_validation_receipt(
        generation=generation,
        database_source_id="source-1",
        allowed_tables=["vehicle_params"],
    )

    envelope = DelegationControlMiddleware._envelope(
        contract,
        None,
        status="timed_out",
        reason="wall_clock_limit",
    )

    assert envelope.sql_generation_ids == [generation.id]
    assert envelope.validation_receipt_ids == [receipt.id]
    assert envelope.recommended_parent_action == "continue_directly"
