import asyncio
import json
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from langgraph.types import Interrupt
from pydantic import ValidationError

from graph.deepagents_manager import DeepAgentsAgentManager
from graph.middlewares.user_input_boundary import UserInputBoundaryMiddleware
from graph.session_manager import SessionManager
from graph.user_input_resume import UserInputResumeRegistry
from graph.trace_collector import TraceCollector
from harness.coordinators import HarnessRunCoordinator
from harness.models import RunStatus
from tools.request_user_input_tool import RequestUserInputArgs


def _question() -> dict:
    return {
        "id": "style",
        "prompt": "选择报告风格",
        "type": "single_select",
        "options": [
            {"id": "report", "label": "数据报告"},
            {"id": "brand", "label": "品牌展示"},
        ],
        "required": True,
        "allow_other": False,
        "min_selections": 0,
        "max_selections": 1,
        "max_length": 1000,
    }


def test_user_input_schema_rejects_secrets_and_malformed_choices():
    with pytest.raises(ValidationError, match="不得索取"):
        RequestUserInputArgs.model_validate(
            {
                "title": "提供密码",
                "reason": "需要继续",
                "questions": [{**_question(), "prompt": "请输入 API key"}],
                "tool_call_id": "call-schema-1",
            }
        )

    with pytest.raises(ValidationError, match="option id 必须唯一"):
        RequestUserInputArgs.model_validate(
            {
                "reason": "需要一个不可推导的偏好",
                "questions": [
                    {
                        **_question(),
                        "options": [
                            {"id": "same", "label": "A"},
                            {"id": "same", "label": "B"},
                        ],
                    }
                ],
                "tool_call_id": "call-schema-3",
            }
        )

    with pytest.raises(ValidationError, match="至少需要两个选项"):
        RequestUserInputArgs.model_validate(
            {
                "reason": "需要一个不可推导的偏好",
                "questions": [{**_question(), "options": [{"id": "only", "label": "唯一"}]}],
                "tool_call_id": "call-schema-2",
            }
        )

    with pytest.raises(ValidationError, match="推荐项数量"):
        RequestUserInputArgs.model_validate(
            {
                "reason": "需要多选偏好",
                "questions": [
                    {
                        **_question(),
                        "type": "multi_select",
                        "max_selections": 1,
                        "options": [
                            {"id": "a", "label": "A", "recommended": True},
                            {"id": "b", "label": "B", "recommended": True},
                        ],
                    }
                ],
                "tool_call_id": "call-schema-4",
            }
        )


def test_user_input_registry_replays_once_and_resolution_is_idempotent():
    async def run() -> None:
        registry = UserInputResumeRegistry()
        kwargs = {
            "session_id": "session-1",
            "query_id": "query-1",
            "run_id": "run-1",
            "goal_id": "goal-1",
            "goal_revision": 1,
            "tool_call_id": "call-1",
            "payload": {
                "title": "选择风格",
                "reason": "两种方案都会显著改变交付物",
                "questions": [_question()],
                "allow_agent_decide": True,
            },
        }
        first = registry.create(**kwargs)
        replay = registry.create(**kwargs)
        assert replay["id"] == first["id"]

        decision = {
            "action": "submit",
            "answers": [
                {"question_id": "style", "option_ids": ["report"], "text": ""}
            ],
        }
        resolved, resumed = registry.resolve(first["id"], decision)
        assert resumed is True
        assert await registry.wait(first["id"]) == resolved
        assert registry.resolve(first["id"], decision) == (resolved, False)
        with pytest.raises(RuntimeError, match="不同答案"):
            registry.resolve(
                first["id"],
                {
                    "action": "submit",
                    "answers": [
                        {"question_id": "style", "option_ids": ["brand"], "text": ""}
                    ],
                },
            )

    asyncio.run(run())


def test_real_tool_call_injects_tool_call_id_and_creates_request(monkeypatch):
    from tools import request_user_input_tool as tool_module

    async def run() -> None:
        registry = UserInputResumeRegistry()
        monkeypatch.setattr(tool_module, "user_input_resume_registry", registry)
        monkeypatch.setattr(
            tool_module,
            "interrupt",
            lambda _payload: {
                "action": "submit",
                "answers": [
                    {"question_id": "style", "option_ids": ["report"], "text": ""}
                ],
            },
        )
        tool = tool_module.create_request_user_input_tool(
            session_id="session-1",
            query_id="query-1",
            run_id="run-1",
        )
        result = await tool.ainvoke(
            {
                "type": "tool_call",
                "name": "request_user_input",
                "id": "call-real",
                "args": {
                    "title": "选择风格",
                    "reason": "会改变交付物",
                    "questions": [_question()],
                },
            }
        )
        assert "用户已提交结构化答案" in str(result.content)
        request = next(iter(registry._requests.values()))
        assert request["tool_call_id"] == "call-real"

    asyncio.run(run())


def test_user_input_boundary_rejects_every_sibling_tool_call():
    state = {
        "messages": [
            SimpleNamespace(
                tool_calls=[
                    {"id": "ask", "name": "request_user_input", "args": {}},
                    {"id": "write", "name": "write_file", "args": {}},
                ]
            )
        ]
    }
    for call_id, name in (("ask", "request_user_input"), ("write", "write_file")):
        request = SimpleNamespace(
            tool_call={"id": call_id, "name": name, "args": {}},
            state=state,
        )
        rejection = UserInputBoundaryMiddleware._rejection(request)
        assert rejection is not None
        assert rejection.status == "error"


def test_unknown_hitl_interrupt_fails_closed():
    item = {
        "__interrupt__": (
            Interrupt(
                value={"type": "future_unknown_request", "request": {"id": "req-1"}},
                id="interrupt-1",
            ),
        )
    }
    with pytest.raises(RuntimeError, match="Unsupported HITL interrupt type"):
        DeepAgentsAgentManager._extract_hitl_interrupts(item)


def test_user_input_answer_validation_is_server_authoritative():
    async def run() -> None:
        registry = UserInputResumeRegistry()
        request = registry.create(
            session_id="session-1",
            query_id="query-1",
            run_id="run-1",
            goal_id="",
            goal_revision=None,
            tool_call_id="call-2",
            payload={
                "title": "选择风格",
                "reason": "需要偏好",
                "questions": [_question()],
                "allow_agent_decide": False,
            },
        )
        with pytest.raises(ValueError, match="无效选项"):
            registry.resolve(
                request["id"],
                {
                    "action": "submit",
                    "answers": [
                        {"question_id": "style", "option_ids": ["invalid"], "text": ""}
                    ],
                },
            )
        with pytest.raises(ValueError, match="不允许由 Agent 决定"):
            registry.resolve(request["id"], {"action": "agent_decide"})

    asyncio.run(run())


def test_cancelled_wait_can_still_mark_request_cancelled():
    async def run() -> None:
        registry = UserInputResumeRegistry()
        request = registry.create(
            session_id="session-1",
            query_id="query-1",
            run_id="run-1",
            goal_id="",
            goal_revision=None,
            tool_call_id="call-cancel",
            payload={
                "title": "选择风格",
                "reason": "需要偏好",
                "questions": [_question()],
                "allow_agent_decide": True,
            },
        )
        waiter = asyncio.create_task(registry.wait(request["id"]))
        await asyncio.sleep(0)
        waiter.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiter
        assert registry.reject_run("session-1", "run-1", "cancelled") == 1
        assert registry.get(request["id"])["status"] == "cancelled"

    asyncio.run(run())


def test_resolve_api_is_idempotent_after_goal_detaches(monkeypatch):
    from api import user_input_requests as api_module

    async def run() -> None:
        registry = UserInputResumeRegistry()
        monkeypatch.setattr(api_module, "user_input_resume_registry", registry)
        state = {"run_status": "waiting_hitl", "current_run_id": "run-1"}
        monkeypatch.setattr(
            api_module,
            "session_manager",
            SimpleNamespace(
                get_run_state=lambda _session, _run: {"status": state["run_status"]},
                get_goal_state=lambda _session, _goal: {
                    "status": "active" if state["current_run_id"] else "completed",
                    "requested_status": None,
                    "current_run_id": state["current_run_id"],
                    "objective_revision": 1,
                },
            ),
        )
        request = registry.create(
            session_id="session-1",
            query_id="query-1",
            run_id="run-1",
            goal_id="goal-1",
            goal_revision=1,
            tool_call_id="call-api",
            payload={
                "title": "选择风格",
                "reason": "需要偏好",
                "questions": [_question()],
                "allow_agent_decide": True,
            },
        )
        body = api_module.ResolveUserInputRequest.model_validate(
            {
                "request_version": 1,
                "action": "submit",
                "answers": [
                    {"question_id": "style", "option_ids": ["report"], "text": ""}
                ],
            }
        )
        first = await api_module.resolve_user_input_request("session-1", request["id"], body)
        assert first["resumed"] is True
        state.update(run_status="completed", current_run_id="")
        retry = await api_module.resolve_user_input_request("session-1", request["id"], body)
        assert retry["resumed"] is False

        conflicting = body.model_copy(
            update={
                "answers": [
                    api_module.UserInputAnswer(
                        question_id="style", option_ids=["brand"], text=""
                    )
                ]
            }
        )
        with pytest.raises(HTTPException) as caught:
            await api_module.resolve_user_input_request(
                "session-1", request["id"], conflicting
            )
        assert caught.value.status_code == 409

    asyncio.run(run())


def test_resolve_api_rejects_goal_control_race(monkeypatch):
    from api import user_input_requests as api_module

    async def run() -> None:
        registry = UserInputResumeRegistry()
        monkeypatch.setattr(api_module, "user_input_resume_registry", registry)
        monkeypatch.setattr(
            api_module,
            "session_manager",
            SimpleNamespace(
                get_run_state=lambda _session, _run: {"status": "waiting_hitl"},
                get_goal_state=lambda _session, _goal: {
                    "status": "active",
                    "requested_status": "cancelled",
                    "current_run_id": "run-1",
                    "objective_revision": 1,
                },
            ),
        )
        request = registry.create(
            session_id="session-1",
            query_id="query-1",
            run_id="run-1",
            goal_id="goal-1",
            goal_revision=1,
            tool_call_id="call-race",
            payload={
                "title": "选择风格",
                "reason": "需要偏好",
                "questions": [_question()],
                "allow_agent_decide": True,
            },
        )
        body = api_module.ResolveUserInputRequest(
            request_version=1,
            action="agent_decide",
        )
        with pytest.raises(HTTPException) as caught:
            await api_module.resolve_user_input_request("session-1", request["id"], body)
        assert caught.value.status_code == 409
        assert registry.get(request["id"])["status"] == "pending"

    asyncio.run(run())


def test_user_input_interrupt_pauses_and_resumes_the_same_run(tmp_path, monkeypatch):
    from graph import deepagents_manager as manager_module

    sessions = SessionManager()
    sessions.initialize(tmp_path)
    sessions.create_session("hitl-session")
    coordinator = HarnessRunCoordinator(sessions)
    run_record, _ = coordinator.start_run(
        session_id="hitl-session",
        query_id="hitl-query",
        objective="重新设计报告",
        goal_mode=False,
    )
    coordinator.transition(run_record, RunStatus.RUNNING)
    monkeypatch.setattr(manager_module, "session_manager", sessions)

    registry = UserInputResumeRegistry()
    monkeypatch.setattr(manager_module, "user_input_resume_registry", registry)

    async def run() -> None:
        request = registry.create(
            session_id="hitl-session",
            query_id="hitl-query",
            run_id=run_record.run_id,
            goal_id="",
            goal_revision=None,
            tool_call_id="call-hitl",
            payload={
                "title": "选择风格",
                "reason": "会显著改变交付物",
                "questions": [_question()],
                "allow_agent_decide": True,
            },
        )

        class FakeAgent:
            def __init__(self) -> None:
                self.calls = 0

            async def astream(self, *_args, **_kwargs):
                self.calls += 1
                if self.calls == 1:
                    yield {
                        "__interrupt__": (
                            Interrupt(
                                value={"type": "user_input_request", "request": request},
                                id="interrupt-hitl",
                            ),
                        )
                    }
                    return
                yield ("messages", ("resumed", {"langgraph_node": "model"}))

        runtime = DeepAgentsAgentManager()
        runtime.initialize(tmp_path)
        seen_statuses: list[str] = []
        with TraceCollector(session_id="hitl-session", query_id="hitl-query") as trace:
            async for item in runtime._astream_with_hitl_resume(
                FakeAgent(),
                {"messages": []},
                stream_mode=["messages", "updates", "values"],
                config={"configurable": {"thread_id": "hitl-session:hitl-query"}},
                context={
                    "session_id": "hitl-session",
                    "query_id": "hitl-query",
                    "run_id": run_record.run_id,
                },
                trace_collector=trace,
            ):
                if isinstance(item, dict) and item.get("event") == "run_status_changed":
                    seen_statuses.append(json.loads(item["data"])["status"])
                if isinstance(item, dict) and item.get("event") == "user_input_required":
                    assert sessions.get_run_state("hitl-session", run_record.run_id)["status"] == "waiting_hitl"
                    registry.resolve(
                        request["id"],
                        {
                            "action": "submit",
                            "answers": [
                                {
                                    "question_id": "style",
                                    "option_ids": ["report"],
                                    "text": "",
                                }
                            ],
                        },
                    )

        assert seen_statuses == ["waiting_hitl", "running"]
        assert sessions.get_run_state("hitl-session", run_record.run_id)["status"] == "running"

    asyncio.run(run())


def test_goal_control_and_hitl_resume_are_one_atomic_transition(tmp_path):
    sessions = SessionManager()
    sessions.initialize(tmp_path)
    sessions.create_session("goal-hitl-session")
    coordinator = HarnessRunCoordinator(sessions)
    run_record, goal = coordinator.start_run(
        session_id="goal-hitl-session",
        query_id="goal-hitl-query",
        objective="完成报告",
        goal_mode=True,
    )
    assert goal is not None
    coordinator.transition(run_record, RunStatus.RUNNING)
    sessions.transition_run_status(
        run_record.session_id,
        run_record.run_id,
        "waiting_hitl",
        expected_statuses={"running"},
    )
    sessions.request_goal_control(
        run_record.session_id,
        goal.goal_id,
        "cancelled",
    )

    with pytest.raises(ValueError, match="Goal control changed"):
        sessions.resume_run_from_hitl(
            run_record.session_id,
            run_record.run_id,
            goal_id=goal.goal_id,
            goal_revision=goal.objective_revision,
        )
    assert sessions.get_run_state(run_record.session_id, run_record.run_id)["status"] == "waiting_hitl"
