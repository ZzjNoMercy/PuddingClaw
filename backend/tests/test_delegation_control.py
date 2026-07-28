from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from typing import Any

import pytest
from deepagents import CompiledSubAgent, create_deep_agent
from langchain.agents import create_agent
from langchain.agents.middleware.types import ToolCallRequest
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.runnables import RunnableLambda
from langchain_core.tools import tool
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.errors import GraphInterrupt
from langgraph.types import Command, Interrupt, interrupt
from pydantic import PrivateAttr

from analytics.nl2sql.schemas import DatabaseSqlGenerationResult, TableRoute
from graph.middlewares.delegation_control import (
    DelegationControlMiddleware,
    SubagentProgressMiddleware,
    _ActiveDelegation,
    _DelegationLimitExceeded,
)
from graph.session_manager import session_manager
from harness.models import DelegationContract, DelegationLimits, RunRecord


class _ScriptedDelegationModel(BaseChatModel):
    _responses: list[AIMessage] = PrivateAttr()
    _calls: int = PrivateAttr(default=0)

    def __init__(self, responses: list[AIMessage]) -> None:
        super().__init__()
        self._responses = responses

    @property
    def _llm_type(self) -> str:
        return "delegation_interrupt_scripted"

    def bind_tools(self, _tools: list[Any], **_kwargs: Any):
        return self

    def _generate(
        self,
        _messages: list[Any],
        stop: list[str] | None = None,
        run_manager: Any = None,
        **_kwargs: Any,
    ) -> ChatResult:
        del stop, run_manager
        response = self._responses[self._calls]
        self._calls += 1
        return ChatResult(generations=[ChatGeneration(message=response)])


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
    assert not any(event["type"] == "subagent_fallback_to_parent" for event in events)


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


def test_sync_parent_tool_announces_takeover_only_after_handler_returns(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _prepare_run(tmp_path)
    delegated = _request([])
    request = ToolCallRequest(
        tool_call={
            "name": "read_file",
            "id": "parent-sync-call",
            "args": {"file_path": "/workspace/report.js"},
        },
        tool=None,
        state=delegated.state,
        runtime=delegated.runtime,
    )
    middleware = DelegationControlMiddleware()
    observed: list[ToolMessage | Command[Any]] = []
    monkeypatch.setattr(
        middleware,
        "_emit_parent_takeover_if_needed",
        lambda _request, result: observed.append(result),
    )

    result = middleware.wrap_tool_call(
        request,
        lambda _request: ToolMessage(
            content="parent continued",
            tool_call_id="parent-sync-call",
        ),
    )

    assert observed == [result]


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
    assert not any(event["type"] == "subagent_fallback_to_parent" for event in events)

    async def failed_parent_handler(_request: ToolCallRequest) -> ToolMessage:
        return ToolMessage(
            content=json.dumps({"status": "permission_required"}),
            tool_call_id="parent-call",
            status="error",
        )

    delegated_request = _request(events)
    parent_request = ToolCallRequest(
        tool_call={
            "name": "read_file",
            "id": "parent-call",
            "args": {"file_path": "/workspace/report.js"},
        },
        tool=None,
        state=delegated_request.state,
        runtime=delegated_request.runtime,
    )
    await middleware.awrap_tool_call(parent_request, failed_parent_handler)
    assert not any(
        event["type"] == "subagent_fallback_to_parent"
        for event in events
    )

    async def parent_handler(_request: ToolCallRequest) -> ToolMessage:
        return ToolMessage(content="parent continued", tool_call_id="parent-call")

    await middleware.awrap_tool_call(parent_request, parent_handler)
    assert not any(
        event["type"] == "subagent_fallback_to_parent"
        for event in events
    )

    takeover_request = ToolCallRequest(
        tool_call={
            "name": "update_todos",
            "id": "parent-todo-takeover",
            "args": {
                "operations": [
                    {"action": "start", "todo_id": "todo-1"}
                ]
            },
        },
        tool=None,
        state=delegated_request.state,
        runtime=delegated_request.runtime,
    )

    async def takeover_handler(_request: ToolCallRequest) -> ToolMessage:
        return ToolMessage(
            content=json.dumps({"status": "completed"}),
            tool_call_id="parent-todo-takeover",
        )

    await middleware.awrap_tool_call(takeover_request, takeover_handler)
    await middleware.awrap_tool_call(takeover_request, takeover_handler)

    fallback_events = [
        event
        for event in events
        if event["type"] == "subagent_fallback_to_parent"
    ]
    assert len(fallback_events) == 1
    assert fallback_events[0]["remaining_todo_ids"] == ["todo-1"]
    assert fallback_events[0]["parent_tool"] == "update_todos"


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
async def test_subagent_permission_interrupt_bubbles_and_resumes_same_contract(
    tmp_path,
) -> None:
    _prepare_run(tmp_path)
    events: list[dict[str, Any]] = []
    middleware = DelegationControlMiddleware()
    request = _request(events)

    async def interrupted(_request: ToolCallRequest) -> ToolMessage:
        raise GraphInterrupt(
            [
                Interrupt(
                    value={
                        "type": "permission_request",
                        "request": {"id": "permission-subagent"},
                    },
                    id="interrupt-subagent",
                )
            ]
        )

    with pytest.raises(GraphInterrupt):
        await middleware.awrap_tool_call(request, interrupted)

    run_after_interrupt = session_manager.get_run_state(
        "session-delegation",
        "run-delegation",
    )
    assert run_after_interrupt is not None
    assert len(run_after_interrupt["delegation_contracts"]) == 1
    contract = run_after_interrupt["delegation_contracts"][0]
    assert contract["permission_context"]["policy_version"]
    assert any(
        event["type"] == "subagent_waiting_for_permission"
        for event in events
    )
    assert run_after_interrupt["delegation_results"] == []

    async def denied(_request: ToolCallRequest) -> ToolMessage:
        return ToolMessage(
            content=json.dumps(
                {
                    "status": "permission_denied",
                    "error": "user rejected",
                }
            ),
            tool_call_id="task-call-1",
        )

    resumed = await middleware.awrap_tool_call(request, denied)
    envelope = json.loads(str(resumed.content))
    run_after_resume = session_manager.get_run_state(
        "session-delegation",
        "run-delegation",
    )
    assert envelope["status"] == "blocked"
    assert envelope["blocking_or_timeout_reason"] == "permission_denied"
    assert envelope["recommended_parent_action"] == "continue_directly"
    assert run_after_resume is not None
    assert len(run_after_resume["delegation_contracts"]) == 1
    assert (
        run_after_resume["delegation_contracts"][0]["subagent_run_id"]
        == contract["subagent_run_id"]
    )


@pytest.mark.asyncio
async def test_real_graph_resumes_subagent_permission_from_same_checkpoint(
    tmp_path,
) -> None:
    """Exercise the actual LangGraph interrupt/resume protocol, not a mock."""

    _prepare_run(tmp_path)
    session_manager.record_run_capability_manifest(
        "session-delegation",
        "run-delegation",
        {
            "manifest_id": "manifest-delegation-no-database-contract",
            "active_skill_ids": [],
            "enabled_toolsets": [],
            "allowed_tool_names": ["task"],
            "tool_schema_hash": "sha256:test-task-only",
        },
    )

    @tool("task")
    def permissioned_task(description: str, subagent_type: str) -> str:
        """Run one delegated operation that requires parent-approved access."""

        decision = interrupt(
            {
                "type": "permission_request",
                "request": {"id": "permission-real-subagent"},
            }
        )
        return json.dumps(
            {
                "status": "completed",
                "description": description,
                "subagent_type": subagent_type,
                "decision": decision,
            }
        )

    task_call = {
        "name": "task",
        "id": "task-call-real-checkpoint",
        "args": {
            "description": "复制已授权模板",
            "subagent_type": "general-purpose",
        },
        "type": "tool_call",
    }
    model = _ScriptedDelegationModel(
        [
            AIMessage(content="", tool_calls=[task_call]),
            AIMessage(content="子代理授权后已返回。"),
        ]
    )
    agent = create_agent(
        model=model,
        tools=[permissioned_task],
        middleware=[DelegationControlMiddleware()],
        checkpointer=InMemorySaver(),
    )
    config = {"configurable": {"thread_id": "subagent-real-checkpoint"}}
    context = {
        "session_id": "session-delegation",
        "run_id": "run-delegation",
        "query_id": "query-delegation",
        "goal_id": "goal-delegation",
        "goal_revision": 1,
    }

    interrupted_result = await agent.ainvoke(
        {"messages": [("user", "请交给子代理执行")]},
        config=config,
        context=context,
    )
    interrupts = interrupted_result.get("__interrupt__") or ()
    assert len(interrupts) == 1
    assert interrupts[0].value["request"]["id"] == "permission-real-subagent"

    run_waiting = session_manager.get_run_state(
        "session-delegation",
        "run-delegation",
    )
    assert run_waiting is not None
    assert len(run_waiting["delegation_contracts"]) == 1
    waiting_contract = run_waiting["delegation_contracts"][0]
    assert run_waiting["delegation_results"] == []
    assert any(
        event["type"] == "subagent_waiting_for_permission"
        for event in run_waiting["delegation_events"]
    )

    resumed_result = await agent.ainvoke(
        Command(resume={"decision": "approve"}),
        config=config,
        context=context,
    )
    assert resumed_result["messages"][-1].content == "子代理授权后已返回。"

    run_completed = session_manager.get_run_state(
        "session-delegation",
        "run-delegation",
    )
    assert run_completed is not None
    assert len(run_completed["delegation_contracts"]) == 1
    assert (
        run_completed["delegation_contracts"][0]["subagent_run_id"]
        == waiting_contract["subagent_run_id"]
    )
    assert len(run_completed["delegation_results"]) == 1
    assert run_completed["delegation_results"][0]["status"] == "completed"


@pytest.mark.asyncio
async def test_native_deepagent_subagent_resumes_permission_checkpoint(
    tmp_path,
) -> None:
    """Prove resume through DeepAgents' native SubAgentMiddleware task path."""

    _prepare_run(tmp_path)
    session_manager.record_run_capability_manifest(
        "session-delegation",
        "run-delegation",
        {
            "manifest_id": "manifest-native-subagent",
            "active_skill_ids": [],
            "enabled_toolsets": [],
            "allowed_tool_names": ["task"],
            "tool_schema_hash": "sha256:native-subagent",
        },
    )

    def native_subagent_runnable(_state: dict[str, Any]) -> dict[str, Any]:
        decision = interrupt(
            {
                "type": "permission_request",
                "request": {"id": "permission-native-subagent"},
            }
        )
        return {
            "messages": [
                AIMessage(
                    content=json.dumps(
                        {
                            "status": "completed",
                            "decision": decision,
                        }
                    )
                )
            ]
        }

    task_call = {
        "name": "task",
        "id": "task-call-native-subagent",
        "args": {
            "description": "通过原生子代理执行需授权操作",
            "subagent_type": "reviewer",
        },
        "type": "tool_call",
    }
    model = _ScriptedDelegationModel(
        [
            AIMessage(content="", tool_calls=[task_call]),
            AIMessage(content="原生子代理已在授权后恢复。"),
        ]
    )
    agent = create_deep_agent(
        model=model,
        tools=[],
        subagents=[
            CompiledSubAgent(
                name="reviewer",
                description="permission checkpoint reviewer",
                runnable=RunnableLambda(native_subagent_runnable),
            )
        ],
        middleware=[DelegationControlMiddleware()],
        checkpointer=InMemorySaver(),
    )
    config = {"configurable": {"thread_id": "native-subagent-checkpoint"}}
    context = {
        "session_id": "session-delegation",
        "run_id": "run-delegation",
        "query_id": "query-delegation",
        "goal_id": "goal-delegation",
        "goal_revision": 1,
    }

    interrupted_result = await agent.ainvoke(
        {"messages": [("user", "委托给 reviewer")]},
        config=config,
        context=context,
    )
    interrupts = interrupted_result.get("__interrupt__") or ()
    assert len(interrupts) == 1
    assert (
        interrupts[0].value["request"]["id"]
        == "permission-native-subagent"
    )
    waiting = session_manager.get_run_state(
        "session-delegation",
        "run-delegation",
    )
    assert waiting is not None
    assert len(waiting["delegation_contracts"]) == 1
    subagent_run_id = waiting["delegation_contracts"][0]["subagent_run_id"]
    assert any(
        event["type"] == "subagent_waiting_for_permission"
        for event in waiting["delegation_events"]
    )

    resumed_result = await agent.ainvoke(
        Command(resume={"decision": "approve"}),
        config=config,
        context=context,
    )
    assert (
        resumed_result["messages"][-1].content
        == "原生子代理已在授权后恢复。"
    )
    completed = session_manager.get_run_state(
        "session-delegation",
        "run-delegation",
    )
    assert completed is not None
    assert len(completed["delegation_contracts"]) == 1
    assert (
        completed["delegation_contracts"][0]["subagent_run_id"]
        == subagent_run_id
    )
    assert len(completed["delegation_results"]) == 1
    assert completed["delegation_results"][0]["status"] == "completed"


def test_delegation_budget_scales_with_observable_complexity(tmp_path) -> None:
    _prepare_run(tmp_path)
    middleware = DelegationControlMiddleware()
    contract = middleware._contract(
        _request(
            [],
            description="查询数据库 337 行配置并用 source_ref 填充模板 slot",
        )
    )

    assert contract.limits.model_calls >= 20
    assert contract.limits.tool_calls >= 53
    assert contract.declared_artifact_targets == []


@pytest.mark.asyncio
async def test_subagent_progress_enforces_model_and_tool_call_limits() -> None:
    from graph.middlewares import delegation_control as module

    contract = DelegationContract(
        subagent_run_id="subrun-budget",
        parent_run_id="run-budget",
        parent_tool_call_id="task-budget",
        session_id="session-budget",
        subagent_type="general-purpose",
        objective="bounded",
        limits=DelegationLimits(model_calls=1, tool_calls=1),
    )
    active = _ActiveDelegation(
        contract=contract,
        last_activity_at=0.0,
    )
    token = module._ACTIVE_DELEGATION.set(active)
    progress = SubagentProgressMiddleware()
    try:
        model_request = SimpleNamespace(
            runtime=SimpleNamespace(stream_writer=None)
        )

        async def model_handler(_request):
            return SimpleNamespace()

        await progress.awrap_model_call(model_request, model_handler)
        with pytest.raises(_DelegationLimitExceeded, match="model_call_limit"):
            await progress.awrap_model_call(model_request, model_handler)

        tool_request = ToolCallRequest(
            tool_call={"name": "read_file", "id": "tool-budget", "args": {}},
            tool=None,
            state={},
            runtime=SimpleNamespace(context={}, stream_writer=None),
        )

        async def tool_handler(_request):
            return ToolMessage(content="ok", tool_call_id="tool-budget")

        await progress.awrap_tool_call(tool_request, tool_handler)
        with pytest.raises(_DelegationLimitExceeded, match="tool_call_limit"):
            await progress.awrap_tool_call(tool_request, tool_handler)
    finally:
        module._ACTIVE_DELEGATION.reset(token)


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
