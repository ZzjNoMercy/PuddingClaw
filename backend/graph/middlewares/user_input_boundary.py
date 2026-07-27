"""Trusted execution boundary for generic structured user input."""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from typing import Any

from langchain.agents.middleware import AgentMiddleware
from langchain.agents.middleware.types import ToolCallRequest
from langchain_core.messages import ToolMessage
from langgraph.types import Command


class UserInputBoundaryMiddleware(AgentMiddleware[Any, Any, Any]):
    """Make a user-input request an atomic decision boundary.

    If the model emits request_user_input beside another Tool Call, every call
    in that Assistant message is rejected. This prevents sibling mutations
    from racing ahead of the answer the model said it needed.
    """

    @staticmethod
    def _sibling_names(request: ToolCallRequest) -> list[str]:
        call_id = str(request.tool_call.get("id") or "")
        for message in reversed(list(request.state.get("messages") or [])):
            calls = getattr(message, "tool_calls", None)
            if not isinstance(calls, list):
                continue
            ids = [str(item.get("id") or "") for item in calls if isinstance(item, dict)]
            if call_id in ids:
                return [
                    str(item.get("name") or "")
                    for item in calls
                    if isinstance(item, dict)
                ]
        return []

    @staticmethod
    def _rejection(request: ToolCallRequest) -> ToolMessage | None:
        names = UserInputBoundaryMiddleware._sibling_names(request)
        if "request_user_input" not in names or len(names) == 1:
            return None
        return ToolMessage(
            content=json.dumps(
                {
                    "error": (
                        "request_user_input 必须是该 Assistant Message 中唯一的 Tool Call；"
                        "请先单独请求用户选择，恢复后再执行其他工具。"
                    )
                },
                ensure_ascii=False,
            ),
            tool_call_id=str(request.tool_call.get("id") or ""),
            status="error",
        )

    def wrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], ToolMessage | Command[Any]],
    ) -> ToolMessage | Command[Any]:
        return self._rejection(request) or handler(request)

    async def awrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], Awaitable[ToolMessage | Command[Any]]],
    ) -> ToolMessage | Command[Any]:
        rejection = self._rejection(request)
        return rejection if rejection is not None else await handler(request)
