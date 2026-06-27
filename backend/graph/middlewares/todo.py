"""DeepAgents-native todo middleware.

Provides a `write_todos` tool and maintains `state["todos"]` using the standard
AgentMiddleware hooks (`state_schema` + `awrap_tool_call`).
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from langchain.agents.middleware.types import AgentMiddleware, ToolCallRequest
from langchain_core.messages import ToolMessage
from langchain_core.tools import tool
from langgraph.types import Command
from typing_extensions import TypedDict


class TodoState(TypedDict, total=False):
    """State field injected by TodoListMiddleware."""

    todos: list[dict[str, Any]]


@tool
def write_todos(todos: list[dict[str, str]]) -> str:
    """把任务拆解为待办清单，并记录到当前会话状态。

    Args:
        todos: 待办列表。每项至少包含 content；可选 status，值为
               pending / in_progress / completed，缺省为 pending。

    Example:
        write_todos(todos=[
            {"content": "创建项目目录", "status": "pending"},
            {"content": "初始化 Flask 应用", "status": "pending"},
        ])
    """
    normalized = []
    for item in todos:
        content = str(item.get("content", "")).strip()
        if not content:
            continue
        status = str(item.get("status") or "pending").strip().lower()
        if status not in {"pending", "in_progress", "completed"}:
            status = "pending"
        normalized.append(
            {
                "content": content,
                "status": status,
                "created_at": datetime.now(timezone.utc).isoformat(),
            }
        )

    return json.dumps(
        {
            "puddingclaw_tool_result": 1,
            "answer_context": f"已创建 {len(normalized)} 项待办清单。",
            "todos": normalized,
        },
        ensure_ascii=False,
    )


class TodoListMiddleware(AgentMiddleware[TodoState, Any, Any]):
    """Maintain a todo list in graph state via native AgentMiddleware hooks.

    - Injects `write_todos` schema into the model's tool list at runtime by
      overriding `request.tools` in `awrap_model_call`.
    - Intercepts `write_todos` tool calls in `awrap_tool_call` and writes the
      resulting todos into `state["todos"]` using a LangGraph `Command`.
    """

    state_schema = TodoState

    def __init__(self) -> None:
        self._write_todos_tool = write_todos

    async def awrap_model_call(self, request, handler):
        """Inject write_todos into the available tools for the model call."""
        existing_names = {
            getattr(t, "name", None) or (t.get("name") if isinstance(t, dict) else None)
            for t in request.tools
        }
        if self._write_todos_tool.name not in existing_names:
            request = request.override(tools=[*request.tools, self._write_todos_tool])
        return await handler(request)

    async def awrap_tool_call(
        self, request: ToolCallRequest, handler
    ) -> ToolMessage | Command:
        """Run the tool, then update state["todos"] if it was write_todos."""
        result = await handler(request)

        if request.tool_call.get("name") != "write_todos":
            return result

        try:
            args = request.tool_call.get("args") or {}
            raw_todos = args.get("todos") if isinstance(args, dict) else []
            normalized: list[dict[str, Any]] = []
            for item in raw_todos or []:
                content = str(item.get("content", "")).strip()
                if not content:
                    continue
                status = str(item.get("status") or "pending").strip().lower()
                if status not in {"pending", "in_progress", "completed"}:
                    status = "pending"
                normalized.append(
                    {
                        "content": content,
                        "status": status,
                        "created_at": datetime.now(timezone.utc).isoformat(),
                    }
                )

            if normalized:
                # Update graph state without interfering with the normal ToolMessage
                # flow. The ToolMessage is returned by `handler` and will be appended
                # to messages by the graph reducer; we only add the todos field.
                return Command(update={"todos": normalized})
        except Exception:
            # If parsing fails, fall back to normal tool result.
            pass

        return result
