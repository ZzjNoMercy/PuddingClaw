import pytest
from pydantic import ValidationError
from langchain_core.messages import AIMessage, ToolMessage
from langgraph.graph import END, START, StateGraph
from langgraph.prebuilt import ToolNode
from langgraph.prebuilt.tool_node import ToolRuntime
from langgraph.types import Command

from graph.middlewares import harness_todos as harness_todos_module
from graph.middlewares.harness_todos import (
    HarnessTodoMiddleware,
    HarnessTodoState,
    TodoPatchOperation,
    UpdateTodosInput,
    _apply_operations,
    _available_todo_evidence,
)


def test_update_todos_tool_schema_makes_create_completed_unrepresentable():
    schema = UpdateTodosInput.model_json_schema()

    assert "discriminator" in str(schema)
    create_schema = schema["$defs"]["CreateTodoOperation"]
    assert create_schema["properties"]["status"]["enum"] == [
        "pending",
        "in_progress",
    ]
    with pytest.raises(ValidationError):
        UpdateTodosInput.model_validate(
            {
                "operations": [
                    {
                        "action": "create",
                        "content": "已经完成的伪任务",
                        "status": "completed",
                    }
                ]
            }
        )


def test_harness_todo_ledger_remains_in_compiled_agent_input_schema():
    """Persisted Session todos must survive the agent factory schema merge."""

    from langchain.agents.factory import _resolve_schemas

    from graph.deepagents_manager import PuddingClawAgentState

    _state_schema, input_schema, _output_schema = _resolve_schemas(
        [PuddingClawAgentState, HarnessTodoState]
    )

    assert "todos" in input_schema.__annotations__


def test_todo_patch_rename_and_reorder_preserve_stable_identity():
    original = [
        {"id": "todo-a", "content": "验证所有图表", "status": "pending"},
        {"id": "todo-b", "content": "更新摘要", "status": "in_progress"},
    ]

    updated, _ = _apply_operations(
        original,
        [
            TodoPatchOperation(action="update", todo_id="todo-a", content="验证全部 2026 图表"),
            TodoPatchOperation(action="reorder", ordered_ids=["todo-b", "todo-a"]),
        ],
        tool_call_id="call-1",
        run_id="run-2",
        query_id="query-2",
    )

    assert [item["id"] for item in updated] == ["todo-b", "todo-a"]
    assert [item["position"] for item in updated] == [0, 1]
    renamed = next(item for item in updated if item["id"] == "todo-a")
    assert renamed["content"] == "验证全部 2026 图表"
    assert renamed["status"] == "pending"


def test_cross_run_duplicate_todo_create_is_idempotent_by_normalized_content():
    result, applied = _apply_operations(
        [
            {
                "id": "todo-existing",
                "content": "验证  E2E 流程",
                "status": "in_progress",
            }
        ],
        [TodoPatchOperation(action="create", content="  验证 e2e   流程  ")],
        tool_call_id="call-next-run",
        run_id="run-next",
        query_id="query-next",
    )

    assert len(result) == 1
    assert result[0]["id"] == "todo-existing"
    assert applied == [
        {"action": "create", "todo_id": "todo-existing", "deduplicated": True}
    ]


def test_todo_create_is_idempotent_for_replayed_tool_call_and_cannot_replace_pending_item():
    operations = [TodoPatchOperation(action="create", content="生成趋势总结")]
    first, _ = _apply_operations(
        [{"id": "todo-a", "content": "验证所有图表", "status": "pending"}],
        operations,
        tool_call_id="call-stable",
        run_id="run-1",
        query_id="query-1",
    )
    replayed, _ = _apply_operations(
        first,
        operations,
        tool_call_id="call-stable",
        run_id="run-1",
        query_id="query-1",
    )

    assert len(replayed) == 2
    assert replayed[0] == first[0]
    assert replayed[1]["id"].startswith("todo_")
    assert replayed[1]["id"] == first[1]["id"]


def test_todo_create_cannot_bypass_lifecycle_as_already_completed():
    with pytest.raises(ValueError, match="create status must be pending or in_progress"):
        _apply_operations(
            [],
            [
                TodoPatchOperation(
                    action="create",
                    content="产出最终报告 - 已完成",
                    status="completed",
                )
            ],
            tool_call_id="call-bypass",
            run_id="run-bypass",
            query_id="query-bypass",
        )


def test_goal_continuation_reuses_prior_run_evidence(monkeypatch):
    monkeypatch.setattr(
        harness_todos_module.session_manager,
        "get_run_state",
        lambda *_: {"verification_activations": []},
    )
    monkeypatch.setattr(
        harness_todos_module.session_manager,
        "get_goal_state",
        lambda *_: {"objective_revision": 2},
    )
    monkeypatch.setattr(
        harness_todos_module.session_manager,
        "resolve_goal_evidence_records",
        lambda *_: [
            {
                "kind": "tool_result",
                "id": "call-query-prior",
                "payload": {"tool_name": "pandas_knowledge_query"},
            },
            {"kind": "artifact", "id": "artifact-prior", "payload": {}},
            {
                "kind": "validation_receipt",
                "id": "validation-prior",
                "payload": {},
            },
            {
                "kind": "tool_result",
                "id": "call-write-prior",
                "payload": {"tool_name": "write_file"},
            },
        ],
    )

    available = _available_todo_evidence(
        session_id="session-1",
        run_id="run-2",
        goal_id="goal-1",
        goal_revision=2,
    )

    assert available == {
        "query_result": {"call-query-prior"},
        "artifact_receipt": {"artifact-prior"},
        "validation_receipt": {"validation-prior"},
    }


def test_todo_cancel_is_tombstone_not_deletion():
    updated, _ = _apply_operations(
        [{"id": "todo-a", "content": "旧任务", "status": "pending"}],
        [TodoPatchOperation(action="cancel", todo_id="todo-a")],
        tool_call_id="call-2",
        run_id="run-2",
        query_id="query-2",
    )

    assert updated == [
        {
            "id": "todo-a",
            "content": "旧任务",
            "status": "cancelled",
            "updated_at": updated[0]["updated_at"],
            "last_changed_run_id": "run-2",
            "last_changed_query_id": "query-2",
            "position": 0,
        }
    ]


def test_validation_todo_cannot_complete_without_registered_receipt():
    created, _ = _apply_operations(
        [],
        [
            TodoPatchOperation(
                action="create",
                content="验证目标产物",
                completion_contract="validation_receipt",
            )
        ],
        tool_call_id="call-validation",
        run_id="run-validation",
        query_id="query-validation",
    )
    todo_id = created[0]["id"]

    with pytest.raises(ValueError, match="requires validation_receipt evidence"):
        _apply_operations(
            created,
            [TodoPatchOperation(action="complete", todo_id=todo_id)],
            tool_call_id="call-complete-missing",
            run_id="run-validation",
            query_id="query-validation",
            available_evidence={"validation_receipt": set()},
        )

    completed, _ = _apply_operations(
        created,
        [
            TodoPatchOperation(
                action="complete",
                todo_id=todo_id,
                evidence_refs=["validation-receipt-1"],
            )
        ],
        tool_call_id="call-complete-valid",
        run_id="run-validation",
        query_id="query-validation",
        available_evidence={"validation_receipt": {"validation-receipt-1"}},
    )

    assert completed[0]["status"] == "completed"
    assert completed[0]["evidence_refs"] == ["validation-receipt-1"]


def test_delivery_bundle_todo_requires_query_artifact_and_validation_evidence():
    created, _ = _apply_operations(
        [],
        [
            TodoPatchOperation(
                action="create",
                content="刷新并交付热力图",
                completion_contract="delivery_bundle",
            )
        ],
        tool_call_id="call-delivery",
        run_id="run-delivery",
        query_id="query-delivery",
    )
    todo_id = created[0]["id"]
    evidence = {
        "query_result": {"query-result-1"},
        "artifact_receipt": {"artifact-1"},
        "validation_receipt": {"validation-1"},
    }

    with pytest.raises(ValueError, match="validation_receipt"):
        _apply_operations(
            created,
            [
                TodoPatchOperation(
                    action="complete",
                    todo_id=todo_id,
                    evidence_refs=["query-result-1", "artifact-1"],
                )
            ],
            tool_call_id="call-delivery-incomplete",
            run_id="run-delivery",
            query_id="query-delivery",
            available_evidence=evidence,
        )

    completed, _ = _apply_operations(
        created,
        [
            TodoPatchOperation(
                action="complete",
                todo_id=todo_id,
                evidence_refs=[
                    "query-result-1",
                    "artifact-1",
                    "validation-1",
                ],
            )
        ],
        tool_call_id="call-delivery-complete",
        run_id="run-delivery",
        query_id="query-delivery",
        available_evidence=evidence,
    )

    assert completed[0]["status"] == "completed"


@pytest.mark.asyncio
async def test_update_todos_receives_runtime_through_real_async_tool_node():
    tool = HarnessTodoMiddleware().tools[0]
    node = ToolNode([tool])
    runtime = ToolRuntime(
        state={"messages": [], "todos": []},
        context={"run_id": "run-async", "query_id": "query-async"},
        config={},
        stream_writer=lambda _: None,
        tool_call_id="call-async",
        store=None,
        tools=[tool],
    )

    result = await node._arun_one(
        {
            "name": "update_todos",
            "args": {
                "operations": [
                    {
                        "action": "create",
                        "content": "查询最新上市时间",
                        "status": "in_progress",
                    }
                ]
            },
            "id": "call-async",
            "type": "tool_call",
        },
        "dict",
        runtime,
    )

    assert isinstance(result, Command)
    created = result.update["todos"][0]
    assert created["content"] == "查询最新上市时间"
    assert created["created_run_id"] == "run-async"
    assert created["last_changed_query_id"] == "query-async"


@pytest.mark.asyncio
async def test_harness_todo_state_channel_persists_command_update_in_graph():
    middleware = HarnessTodoMiddleware()
    tool = middleware.tools[0]
    builder = StateGraph(HarnessTodoState)
    builder.add_node("tools", ToolNode([tool]))
    builder.add_edge(START, "tools")
    builder.add_edge("tools", END)
    graph = builder.compile()

    result = await graph.ainvoke(
        {
            "messages": [
                AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "name": "update_todos",
                            "args": {
                                "operations": [
                                    {
                                        "action": "create",
                                        "content": "刷新 2026 图表",
                                    }
                                ]
                            },
                            "id": "call-state-channel",
                            "type": "tool_call",
                        }
                    ],
                )
            ],
            "todos": [],
        },
        context={"run_id": "run-state", "query_id": "query-state"},
    )

    assert result["todos"][0]["content"] == "刷新 2026 图表"
    assert result["todos"][0]["id"].startswith("todo_")


@pytest.mark.asyncio
async def test_persisted_todo_can_be_completed_after_cross_run_restore():
    middleware = HarnessTodoMiddleware()
    tool = middleware.tools[0]
    builder = StateGraph(HarnessTodoState)
    builder.add_node("tools", ToolNode([tool]))
    builder.add_edge(START, "tools")
    builder.add_edge("tools", END)
    graph = builder.compile()
    persisted = {
        "id": "todo-persisted",
        "content": "更新目标 HTML",
        "status": "in_progress",
    }

    result = await graph.ainvoke(
        {
            "messages": [
                AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "name": "update_todos",
                            "args": {
                                "operations": [
                                    {
                                        "action": "complete",
                                        "todo_id": "todo-persisted",
                                    }
                                ]
                            },
                            "id": "call-cross-run",
                            "type": "tool_call",
                        }
                    ],
                )
            ],
            "todos": [persisted],
        },
        context={"run_id": "run-next", "query_id": "query-next"},
    )

    assert result["todos"][0]["id"] == "todo-persisted"
    assert result["todos"][0]["status"] == "completed"
    assert result["todos"][0]["last_changed_run_id"] == "run-next"


@pytest.mark.asyncio
async def test_unknown_todo_id_returns_recoverable_tool_error():
    tool = HarnessTodoMiddleware().tools[0]
    node = ToolNode([tool])
    runtime = ToolRuntime(
        state={"messages": [], "todos": []},
        context={"run_id": "run-stale", "query_id": "query-stale"},
        config={},
        stream_writer=lambda _: None,
        tool_call_id="call-stale",
        store=None,
        tools=[tool],
    )

    result = await node._arun_one(
        {
            "name": "update_todos",
            "args": {
                "operations": [
                    {"action": "complete", "todo_id": "todo_missing"}
                ]
            },
            "id": "call-stale",
            "type": "tool_call",
        },
        "dict",
        runtime,
    )

    assert isinstance(result, ToolMessage)
    assert result.status == "error"
    assert "Unknown todo_id: todo_missing" in str(result.content)
