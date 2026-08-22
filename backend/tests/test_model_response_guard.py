from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from langchain.agents import create_agent
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from pydantic import PrivateAttr

from graph.middlewares.model_response_guard import (
    MODEL_RESPONSE_RECOVERY_SOURCE,
    TerminalModelResponseGuardMiddleware,
    terminal_model_response_summary,
)


class _ScriptedModel(BaseChatModel):
    _responses: list[AIMessage] = PrivateAttr()
    _calls: int = PrivateAttr(default=0)
    _inputs: list[list[Any]] = PrivateAttr(default_factory=list)

    def __init__(self, responses: list[AIMessage]) -> None:
        super().__init__()
        self._responses = responses

    @property
    def _llm_type(self) -> str:
        return "terminal_response_guard_test"

    def bind_tools(self, _tools: list[Any], **_kwargs: Any):
        return self

    def _generate(
        self,
        messages: list[Any],
        stop: list[str] | None = None,
        run_manager: Any = None,
        **_kwargs: Any,
    ) -> ChatResult:
        del stop, run_manager
        self._inputs.append(list(messages))
        response = self._responses[self._calls]
        self._calls += 1
        return ChatResult(generations=[ChatGeneration(message=response)])


def _runtime(events: list[dict[str, Any]]) -> SimpleNamespace:
    return SimpleNamespace(
        context={"session_id": "session-1", "query_id": "query-1", "run_id": "run-1"},
        stream_writer=events.append,
    )


def test_reasoning_only_response_requests_one_in_place_recovery() -> None:
    events: list[dict[str, Any]] = []
    middleware = TerminalModelResponseGuardMiddleware(max_recovery_attempts=1)
    state = {
        "messages": [
            AIMessage(content="", additional_kwargs={"reasoning_content": "Q1: 2021 = 289"}),
        ]
    }

    update = middleware.after_agent(state, _runtime(events))

    assert update is not None
    assert update["jump_to"] == "model"
    assert update["_model_response_recovery_count"] == 1
    assert update["messages"][0].name == MODEL_RESPONSE_RECOVERY_SOURCE
    assert "不要重复已经成功的工具调用" in update["messages"][0].content
    assert events[-1]["type"] == "model_response_recovery_started"
    assert events[-1]["reason"] == "reasoning_without_final_content"


def test_second_incomplete_response_becomes_structured_failure() -> None:
    events: list[dict[str, Any]] = []
    middleware = TerminalModelResponseGuardMiddleware(max_recovery_attempts=1)
    state = {
        "_model_response_recovery_count": 1,
        "messages": [AIMessage(content="", additional_kwargs={"reasoning_content": "still working"})],
    }

    update = middleware.after_agent(state, _runtime(events))

    assert update is not None and "jump_to" not in update
    assert update["_model_response_incomplete"]["code"] == "model_response_incomplete"
    assert update["_model_response_incomplete"]["recoverable"] is True
    assert update["_model_response_termination"]["recovery_attempts"] == 1
    assert events[-1]["type"] == "model_response_incomplete"


def test_length_finish_reason_is_not_accepted_as_final_content() -> None:
    summary, reason, recoverable = terminal_model_response_summary(
        {
            "messages": [
                AIMessage(content="partial answer", response_metadata={"finish_reason": "length"}),
            ]
        }
    )

    assert reason == "provider_output_truncated"
    assert recoverable is True
    assert summary["content_chars"] == len("partial answer")
    assert summary["finish_reason"] == "length"


def test_consumed_terminal_tool_call_is_valid_for_return_direct() -> None:
    tool_call = {
        "name": "direct_answer",
        "args": {},
        "id": "call-direct-1",
        "type": "tool_call",
    }
    summary, reason, recoverable = terminal_model_response_summary(
        {
            "messages": [
                AIMessage(content="", tool_calls=[tool_call]),
                ToolMessage(content="direct result", tool_call_id="call-direct-1"),
            ]
        }
    )

    assert reason is None
    assert recoverable is True
    assert summary["tool_call_count"] == 1
    assert summary["pending_tool_call_count"] == 0


def test_unconsumed_terminal_tool_call_requests_recovery() -> None:
    summary, reason, _recoverable = terminal_model_response_summary(
        {
            "messages": [
                AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "name": "unfinished",
                            "args": {},
                            "id": "call-pending-1",
                            "type": "tool_call",
                        }
                    ],
                )
            ]
        }
    )

    assert reason == "terminal_tool_turn_without_final_response"
    assert summary["pending_tool_call_count"] == 1


@pytest.mark.asyncio
async def test_graph_reenters_model_and_returns_second_final_content() -> None:
    model = _ScriptedModel(
        [
            AIMessage(content="", additional_kwargs={"reasoning_content": "Q1: 2021 = 289"}),
            AIMessage(content="最终完整结果：Q1-Q6 均已完成。", response_metadata={"finish_reason": "stop"}),
        ]
    )
    agent = create_agent(
        model=model,
        tools=[],
        middleware=[TerminalModelResponseGuardMiddleware(max_recovery_attempts=1)],
    )

    result = await agent.ainvoke(
        {"messages": [("user", "完成 Q1-Q6")]},
        context={"session_id": "session-1", "query_id": "query-1", "run_id": "run-1"},
    )

    assert model._calls == 2
    assert result["messages"][-1].content == "最终完整结果：Q1-Q6 均已完成。"
    assert any(
        getattr(message, "name", None) == MODEL_RESPONSE_RECOVERY_SOURCE
        for message in model._inputs[-1]
    )
