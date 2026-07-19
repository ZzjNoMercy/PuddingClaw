"""Stable-ID, incremental Todo control for Harness Runs and Goals."""

from __future__ import annotations

import hashlib
import time
from typing import Any, Literal

from langchain.agents.middleware.types import AgentMiddleware
from langchain.tools import ToolRuntime
from langchain_core.messages import ToolMessage
from langchain_core.tools import StructuredTool
from langgraph.types import Command
from pydantic import BaseModel, Field, model_validator


class TodoPatchOperation(BaseModel):
    action: Literal[
        "create",
        "update",
        "start",
        "complete",
        "cancel",
        "reopen",
        "reorder",
    ]
    todo_id: str | None = None
    content: str | None = None
    status: Literal["pending", "in_progress", "completed", "cancelled"] | None = None
    ordered_ids: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_action_fields(self) -> "TodoPatchOperation":
        if self.action == "create" and not str(self.content or "").strip():
            raise ValueError("create requires non-empty content")
        if self.action in {"update", "start", "complete", "cancel", "reopen"} and not self.todo_id:
            raise ValueError(f"{self.action} requires todo_id")
        if self.action == "update" and self.content is None and self.status is None:
            raise ValueError("update requires content or status")
        if self.action == "reorder" and not self.ordered_ids:
            raise ValueError("reorder requires ordered_ids")
        return self


class UpdateTodosInput(BaseModel):
    operations: list[TodoPatchOperation] = Field(min_length=1, max_length=50)


TODO_PATCH_PROMPT = """## `update_todos`

For complex work, maintain the Todo ledger with incremental operations.
Each Todo has a stable `id`; always reference that ID when changing an item.
Never replace the whole list and never reuse an ID for a different task.

- `create`: add a new task (Harness assigns the stable ID).
- `update`: rename or edit one existing task without changing its ID.
- `start`, `complete`, `cancel`, `reopen`: change lifecycle explicitly.
- `reorder`: change display order only; it never changes identity or status.

Do not mark a Todo complete until its result is actually produced and verified.
Do not delete unfinished work; use `cancel` with an explicit lifecycle record.
"""


TODO_PATCH_DESCRIPTION = """Incrementally update the current Harness Todo ledger.
Use stable todo_id values returned by earlier calls. This tool never replaces the
entire list, so renaming, reordering, splitting, or adding work cannot silently
erase an unfinished Todo."""


def _stable_created_id(tool_call_id: str, operation_index: int) -> str:
    digest = hashlib.sha256(
        f"{tool_call_id}:create:{operation_index}".encode("utf-8")
    ).hexdigest()[:16]
    return f"todo_{digest}"


def _apply_operations(
    todos: list[dict[str, Any]],
    operations: list[TodoPatchOperation],
    *,
    tool_call_id: str,
    run_id: str,
    query_id: str,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    now = time.time()
    result = [dict(item) for item in todos if isinstance(item, dict)]
    by_id = {str(item.get("id") or ""): item for item in result if item.get("id")}
    applied: list[dict[str, Any]] = []

    for index, operation in enumerate(operations):
        if operation.action == "create":
            todo_id = _stable_created_id(tool_call_id, index)
            existing = by_id.get(todo_id)
            if existing is None:
                item = {
                    "id": todo_id,
                    "content": str(operation.content or "").strip(),
                    "status": operation.status or "pending",
                    "created_at": now,
                    "updated_at": now,
                    "created_run_id": run_id or None,
                    "last_changed_run_id": run_id or None,
                    "last_changed_query_id": query_id or None,
                }
                result.append(item)
                by_id[todo_id] = item
            applied.append({"action": "create", "todo_id": todo_id})
            continue

        if operation.action == "reorder":
            ordered_ids = list(dict.fromkeys(operation.ordered_ids))
            unknown = [todo_id for todo_id in ordered_ids if todo_id not in by_id]
            if unknown:
                raise ValueError(f"Unknown todo_id(s) in reorder: {', '.join(unknown)}")
            rank = {todo_id: index for index, todo_id in enumerate(ordered_ids)}
            original_rank = {str(item.get("id")): index for index, item in enumerate(result)}
            result.sort(
                key=lambda item: (
                    rank.get(str(item.get("id")), len(rank) + original_rank[str(item.get("id"))])
                )
            )
            applied.append({"action": "reorder", "ordered_ids": ordered_ids})
            continue

        todo_id = str(operation.todo_id or "")
        item = by_id.get(todo_id)
        if item is None:
            raise ValueError(f"Unknown todo_id: {todo_id}")
        if operation.action == "update":
            if operation.content is not None:
                item["content"] = operation.content.strip()
            if operation.status is not None:
                item["status"] = operation.status
        else:
            item["status"] = {
                "start": "in_progress",
                "complete": "completed",
                "cancel": "cancelled",
                "reopen": "pending",
            }[operation.action]
        item["updated_at"] = now
        item["last_changed_run_id"] = run_id or None
        item["last_changed_query_id"] = query_id or None
        applied.append({"action": operation.action, "todo_id": todo_id})

    # Position is explicit control-plane state.  UI and summaries must not
    # infer order from SSE arrival timing or from rewritten natural language.
    for position, item in enumerate(result):
        item["position"] = position

    return result, applied


def _update_todos(
    runtime: ToolRuntime[Any, Any],
    operations: list[TodoPatchOperation],
) -> Command[Any]:
    context = runtime.context if isinstance(runtime.context, dict) else {}
    next_todos, applied = _apply_operations(
        list(runtime.state.get("todos") or []),
        operations,
        tool_call_id=runtime.tool_call_id,
        run_id=str(context.get("run_id") or ""),
        query_id=str(context.get("query_id") or ""),
    )
    return Command(
        update={
            "todos": next_todos,
            "messages": [
                ToolMessage(
                    content=f"Applied Todo operations: {applied}. Current ledger: {next_todos}",
                    tool_call_id=runtime.tool_call_id,
                )
            ],
        }
    )


async def _aupdate_todos(
    runtime: ToolRuntime[Any, Any],
    operations: list[TodoPatchOperation],
) -> Command[Any]:
    return _update_todos(runtime, operations)


class HarnessTodoMiddleware(AgentMiddleware[Any, Any, Any]):
    """Expose patch-style Todo updates instead of whole-list replacement."""

    @property
    def name(self) -> str:
        return "HarnessTodoMiddleware"

    def __init__(self) -> None:
        super().__init__()
        self.tools = [
            StructuredTool.from_function(
                name="update_todos",
                description=TODO_PATCH_DESCRIPTION,
                func=_update_todos,
                coroutine=_aupdate_todos,
                args_schema=UpdateTodosInput,
                infer_schema=False,
            )
        ]

    def wrap_model_call(self, request: Any, handler: Any) -> Any:
        system_message = request.system_message
        if system_message is None:
            from langchain_core.messages import SystemMessage

            system_message = SystemMessage(content=TODO_PATCH_PROMPT)
        else:
            system_message = system_message.model_copy(
                update={"content": f"{system_message.content}\n\n{TODO_PATCH_PROMPT}"}
            )
        return handler(request.override(system_message=system_message))

    async def awrap_model_call(self, request: Any, handler: Any) -> Any:
        system_message = request.system_message
        if system_message is None:
            from langchain_core.messages import SystemMessage

            system_message = SystemMessage(content=TODO_PATCH_PROMPT)
        else:
            system_message = system_message.model_copy(
                update={"content": f"{system_message.content}\n\n{TODO_PATCH_PROMPT}"}
            )
        return await handler(request.override(system_message=system_message))
