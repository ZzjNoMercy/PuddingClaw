"""Tests for TodoListMiddleware."""

from __future__ import annotations

import json
from typing import Any

import pytest
from langchain.agents.middleware.types import ModelRequest, Runtime, ToolCallRequest
from langchain_core.messages import AIMessage, ToolMessage
from langgraph.types import Command

from graph.middlewares.todo import TodoListMiddleware, write_todos


class FakeModelRequest(ModelRequest[Any]):
    """Minimal override to avoid dataclass init quirks in tests."""


@pytest.fixture
def middleware() -> TodoListMiddleware:
    return TodoListMiddleware()


def test_write_todos_tool_returns_structured_result():
    result = write_todos.invoke(
        {"todos": [
            {"content": "Step 1", "status": "pending"},
            {"content": "Step 2"},
        ]}
    )
    payload = json.loads(result)
    assert payload["puddingclaw_tool_result"] == 1
    assert len(payload["todos"]) == 2
    assert payload["todos"][0]["status"] == "pending"
    assert payload["todos"][1]["status"] == "pending"
    assert "created_at" in payload["todos"][0]


@pytest.mark.anyio
async def test_awrap_model_call_injects_write_todos_tool(middleware: TodoListMiddleware):
    request = FakeModelRequest(
        model=None,  # type: ignore[arg-type]
        messages=[],
        tools=[],
        runtime=Runtime(),
    )

    async def handler(req: ModelRequest[Any]):
        return req

    new_request = await middleware.awrap_model_call(request, handler)
    tool_names = {t.name for t in new_request.tools}
    assert "write_todos" in tool_names


@pytest.mark.anyio
async def test_awrap_tool_call_updates_state_for_write_todos(
    middleware: TodoListMiddleware,
):
    tool_call = {
        "id": "tc-1",
        "name": "write_todos",
        "args": {
            "todos": [
                {"content": "Do X", "status": "in_progress"},
                {"content": "Do Y"},
            ]
        },
    }
    request = ToolCallRequest(
        tool_call=tool_call,
        tool=write_todos,
        state={},
        runtime=Runtime(),  # type: ignore[arg-type]
    )

    async def handler(_req: ToolCallRequest) -> ToolMessage:
        return ToolMessage(content="ok", tool_call_id="tc-1")

    result = await middleware.awrap_tool_call(request, handler)
    assert isinstance(result, Command)
    assert result.update is not None
    assert "todos" in result.update
    assert len(result.update["todos"]) == 2
    assert result.update["todos"][0]["content"] == "Do X"
    assert result.update["todos"][0]["status"] == "in_progress"


@pytest.mark.anyio
async def test_awrap_tool_call_passthrough_for_other_tools(
    middleware: TodoListMiddleware,
):
    request = ToolCallRequest(
        tool_call={"id": "tc-2", "name": "read_file", "args": {"path": "/tmp"}},
        tool=None,
        state={},
        runtime=Runtime(),  # type: ignore[arg-type]
    )

    async def handler(_req: ToolCallRequest) -> ToolMessage:
        return ToolMessage(content="file content", tool_call_id="tc-2")

    result = await middleware.awrap_tool_call(request, handler)
    assert isinstance(result, ToolMessage)
    assert result.content == "file content"
