"""E2E acceptance for Goal inspection routing and durable Todo reconciliation."""

from __future__ import annotations

import asyncio
import copy
import json
from pathlib import Path
from typing import Any

from fastapi import FastAPI
from fastapi.testclient import TestClient
from deepagents import create_deep_agent
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, AIMessageChunk
from langchain_core.outputs import ChatGeneration, ChatResult
from langgraph.prebuilt import ToolNode
from langgraph.prebuilt.tool_node import ToolRuntime
from langgraph.types import Command
from pydantic import PrivateAttr


class _ScriptedGoalModel(BaseChatModel):
    _responses: list[AIMessage] = PrivateAttr()
    _calls: int = PrivateAttr(default=0)

    def __init__(self, responses: list[AIMessage]) -> None:
        super().__init__()
        self._responses = responses

    @property
    def _llm_type(self) -> str:
        return "goal_continuation_scripted"

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


def test_progress_question_is_a_read_only_run_and_preserves_goal_todos(
    tmp_path: Path,
    monkeypatch,
) -> None:
    from graph import deepagents_manager as manager_module
    from graph.session_manager import session_manager
    from harness.models import GoalRecord
    from projects.registry import project_registry

    session_manager.initialize(tmp_path)
    project_registry.initialize(tmp_path)
    session_manager.create_session("goal-inspection-e2e")
    goal = GoalRecord(
        goal_id="goal-inspection-e2e",
        session_id="goal-inspection-e2e",
        objective="完成 V2 报告并执行 E2E",
    )
    session_manager.upsert_goal_state(
        "goal-inspection-e2e",
        goal.model_dump(mode="json"),
    )
    goal_todos = [
        {"id": "todo-done", "content": "完成后端修复", "status": "completed"},
        {"id": "todo-left", "content": "执行 E2E", "status": "in_progress"},
    ]
    session_manager.update_todos(
        "goal-inspection-e2e",
        goal_todos,
        goal_id=goal.goal_id,
        goal_revision=1,
    )

    class FakeInspectionAgent:
        async def astream(self, graph_input, **_kwargs):
            # The inspection prompt must include bounded Goal/Todo context, but
            # the original Goal objective must not replace the current request.
            human_text = "\n".join(
                str(message.content)
                for message in graph_input["messages"]
                if hasattr(message, "content")
            )
            assert "总结进度" in human_text
            assert "执行 E2E" in human_text
            yield (
                "messages",
                (AIMessageChunk(content="已完成后端修复，剩余 E2E。"), {"langgraph_node": "model"}),
            )
            yield ("values", {"messages": [AIMessage(content="已完成后端修复，剩余 E2E。")]})

    monkeypatch.setattr(
        manager_module,
        "create_deep_agent",
        lambda **_kwargs: FakeInspectionAgent(),
    )

    async def no_title(_session_id: str):
        return None

    monkeypatch.setattr(manager_module, "_generate_title", no_title)
    runtime = manager_module.DeepAgentsAgentManager()
    runtime.initialize(tmp_path)

    async def collect():
        return [
            event
            async for event in runtime.astream(
                message="总结进度，现在做到哪了？",
                session_id="goal-inspection-e2e",
                context_goal_id=goal.goal_id,
                goal_mode=False,
                user_id="e2e-user",
            )
        ]

    events = asyncio.run(collect())
    routed = json.loads(next(event["data"] for event in events if event["event"] == "goal_turn_routed"))
    assert routed["intent"] == "inspect_goal"
    assert not any(event["event"] in {"goal_run_continued", "verification_report"} for event in events)

    harness = session_manager.get_harness_state("goal-inspection-e2e")
    inspection = harness["runs"][harness["latest_run_id"]]
    assert inspection["run_kind"] == "goal_inspection"
    assert inspection["goal_id"] is None
    assert inspection["context_goal_id"] == goal.goal_id
    assert harness["goals"][goal.goal_id]["run_ids"] == []
    assert harness["goals"][goal.goal_id]["round"] == 0

    snapshot = session_manager.get_todo_snapshot("goal-inspection-e2e")
    assert snapshot["todos"] == goal_todos
    assert snapshot["authority"] == {
        "kind": "goal",
        "goal_id": goal.goal_id,
        "goal_revision": 1,
    }


def test_explicit_continue_creates_real_goal_run_and_keeps_authoritative_todos(
    tmp_path: Path,
    monkeypatch,
) -> None:
    from graph import deepagents_manager as manager_module
    from graph.session_manager import session_manager
    from harness.models import GoalRecord
    from projects.registry import project_registry

    session_manager.initialize(tmp_path)
    project_registry.initialize(tmp_path)
    session_manager.create_session("goal-continue-e2e")
    goal = GoalRecord(
        goal_id="goal-continue-e2e",
        session_id="goal-continue-e2e",
        objective="完成控制面修复并执行 E2E",
    )
    session_manager.upsert_goal_state(
        "goal-continue-e2e",
        goal.model_dump(mode="json"),
    )
    goal_todos = [
        {"id": "todo-done", "content": "完成实现", "status": "completed"},
        {"id": "todo-left", "content": "执行 E2E", "status": "in_progress"},
    ]
    session_manager.update_todos(
        "goal-continue-e2e",
        goal_todos,
        goal_id=goal.goal_id,
        goal_revision=goal.objective_revision,
    )

    scripted_model = _ScriptedGoalModel(
        [AIMessage(content="已按现有 Goal 上下文继续执行。")]
    )

    def create_real_scripted_agent(**kwargs: Any):
        kwargs["model"] = scripted_model
        return create_deep_agent(**kwargs)

    monkeypatch.setattr(
        manager_module,
        "create_deep_agent",
        create_real_scripted_agent,
    )
    original_config = copy.deepcopy(manager_module.config.load_config())
    original_config.setdefault("harness", {}).setdefault(
        "completion", {}
    ).setdefault("rubric", {})["enabled"] = False
    monkeypatch.setattr(
        manager_module.config,
        "load_config",
        lambda: copy.deepcopy(original_config),
    )

    async def no_title(_session_id: str):
        return None

    monkeypatch.setattr(manager_module, "_generate_title", no_title)
    runtime = manager_module.DeepAgentsAgentManager()
    runtime.initialize(tmp_path)

    async def collect():
        return [
            event
            async for event in runtime.astream(
                message="继续推进，把剩余做完",
                session_id="goal-continue-e2e",
                context_goal_id=goal.goal_id,
                goal_mode=False,
                user_id="e2e-user",
            )
        ]

    events = asyncio.run(collect())
    routed = json.loads(
        next(
            event["data"]
            for event in events
            if event["event"] == "goal_turn_routed"
        )
    )
    assert routed["intent"] == "continue_goal"

    harness = session_manager.get_harness_state("goal-continue-e2e")
    execution_runs = [
        run
        for run in harness["runs"].values()
        if run.get("run_kind") == "goal_execution"
    ]
    assert len(execution_runs) == 1
    run = execution_runs[0]
    assert run["goal_id"] == goal.goal_id
    assert run["context_goal_id"] is None
    assert run["objective"] == goal.objective
    assert run["goal_turn_intent"] == "continue_goal"

    updated_goal = harness["goals"][goal.goal_id]
    assert updated_goal["run_ids"] == [run["run_id"]]
    assert updated_goal["round"] == 1
    snapshot = session_manager.get_todo_snapshot(
        "goal-continue-e2e",
        goal_id=goal.goal_id,
        goal_revision=goal.objective_revision,
    )
    assert snapshot["todos"] == goal_todos
    assert snapshot["authority"]["goal_id"] == goal.goal_id


def test_todo_patch_survives_immediate_run_cancellation_and_api_reload(
    tmp_path: Path,
) -> None:
    from api import sessions as sessions_api
    from graph.middlewares.harness_todos import HarnessTodoMiddleware
    from graph.session_manager import session_manager
    from harness.coordinators import HarnessRunCoordinator
    from harness.models import RunOutcome

    session_manager.initialize(tmp_path)
    session_manager.create_session("todo-cancel-e2e")
    coordinator = HarnessRunCoordinator(session_manager)
    run, goal = coordinator.start_run(
        session_id="todo-cancel-e2e",
        query_id="query-todo-cancel",
        objective="完成全部开发",
        goal_mode=True,
    )
    assert goal is not None

    tool = HarnessTodoMiddleware().tools[0]
    runtime = ToolRuntime(
        state={"messages": [], "todos": []},
        context={
            "session_id": "todo-cancel-e2e",
            "run_id": run.run_id,
            "query_id": run.query_id,
            "goal_id": goal.goal_id,
            "goal_revision": goal.objective_revision,
            "run_kind": "goal_execution",
        },
        config={},
        stream_writer=lambda _: None,
        tool_call_id="call-persist-before-cancel",
        store=None,
        tools=[tool],
    )

    result = asyncio.run(
        ToolNode([tool])._arun_one(
            {
                "name": "update_todos",
                "args": {
                    "expected_revision": 0,
                    "operations": [
                        {
                            "action": "create",
                            "content": "取消前即时落盘",
                            "status": "in_progress",
                        }
                    ],
                },
                "id": "call-persist-before-cancel",
                "type": "tool_call",
            },
            "dict",
            runtime,
        )
    )
    assert isinstance(result, Command)
    coordinator.fail(run, outcome=RunOutcome.CANCELLED, error="manual stop")

    app = FastAPI()
    app.include_router(sessions_api.router, prefix="/api")
    payload = TestClient(app).get("/api/sessions/todo-cancel-e2e/todos/current").json()
    assert payload["ledger_revision"] == 1
    assert payload["authority"] == {
        "kind": "goal",
        "goal_id": goal.goal_id,
        "goal_revision": goal.objective_revision,
    }
    assert payload["todos"][0]["content"] == "取消前即时落盘"
    assert payload["todos"][0]["status"] == "in_progress"

    # Retried delivery of the same tool call is idempotent after cancellation.
    replay = session_manager.apply_todo_patch(
        "todo-cancel-e2e",
        goal_id=goal.goal_id,
        goal_revision=goal.objective_revision,
        operation_id="call-persist-before-cancel",
        expected_revision=0,
        mutator=lambda current: (current, []),
    )
    assert replay["replayed"] is True
    assert replay["ledger_revision"] == 1
    assert len(replay["todos"]) == 1
