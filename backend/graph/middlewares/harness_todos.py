"""Stable-ID, incremental Todo control for Harness Runs and Goals."""

import hashlib
import time
from typing import Any, Literal, NotRequired

from langchain.agents.middleware.types import AgentMiddleware, AgentState
from langchain.tools import ToolRuntime
from langchain_core.messages import ToolMessage
from langchain_core.tools import StructuredTool
from langgraph.types import Command
from pydantic import BaseModel, Field, model_validator

from graph.session_manager import session_manager


class HarnessTodoState(AgentState):
    """Graph state owned by the Harness Todo control plane."""

    # Session JSON is the cross-Run authority for the Todo ledger. The manager
    # restores that trusted value through the graph input on every Run, so this
    # field must remain part of the compiled input schema. Public clients never
    # invoke the graph directly and cannot supply this internal state field.
    todos: NotRequired[list[dict[str, Any]]]


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
    completion_contract: Literal[
        "validation_receipt",
        "artifact_receipt",
        "query_result",
        "delivery_bundle",
    ] | None = None
    evidence_refs: list[str] = Field(default_factory=list)

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
    expected_revision: int | None = Field(default=None, ge=0)


TODO_PATCH_PROMPT = """## `update_todos`

For complex work, maintain the Todo ledger with incremental operations.
Each Todo has a stable `id`; always reference that ID when changing an item.
Never replace the whole list and never reuse an ID for a different task.

- `create`: add a new task (Harness assigns the stable ID).
- `update`: rename or edit one existing task without changing its ID.
- `start`, `complete`, `cancel`, `reopen`: change lifecycle explicitly.
- `reorder`: change display order only; it never changes identity or status.

For validation, artifact delivery, or query-result work, set a
`completion_contract` when creating the Todo. Completing such a Todo requires
the corresponding structured IDs in `evidence_refs`; prose claims are not
evidence. Use validation_receipt, artifact_receipt, or query_result.
For a report/dashboard/chart Todo that combines refreshed source data, a
written artifact, and rendered or executable validation, use delivery_bundle.
It requires at least one query_result, artifact_receipt, and validation_receipt
ID before completion. Do not create such delivery Todos without this contract.

Do not mark a Todo complete until its result is actually produced and verified.
Do not delete unfinished work; use `cancel` with an explicit lifecycle record.
If an update is rejected because a todo_id is unknown, do not retry that stale
ID. Recreate the missing work from the returned current ledger instead.
"""


TODO_PATCH_DESCRIPTION = """Incrementally update the current Harness Todo ledger.
Use stable todo_id values returned by earlier calls. This tool never replaces the
entire list, so renaming, reordering, splitting, or adding work cannot silently
erase an unfinished Todo."""


def _stable_created_id(tool_call_id: str, operation_index: int) -> str:
    digest = hashlib.sha256(f"{tool_call_id}:create:{operation_index}".encode()).hexdigest()[:16]
    return f"todo_{digest}"


def _normalized_todo_content(value: str) -> str:
    """Canonical identity used to suppress cross-Run duplicate creates."""

    return " ".join(str(value or "").strip().casefold().split())


def _completion_evidence_error(
    *,
    todo_id: str,
    contract: str,
    refs: list[str],
    available_evidence: dict[str, set[str]] | None,
) -> str | None:
    available = available_evidence or {}
    if contract == "delivery_bundle":
        missing_kinds = [
            kind
            for kind in ("query_result", "artifact_receipt", "validation_receipt")
            if not any(ref in available.get(kind, set()) for ref in refs)
        ]
        if missing_kinds:
            return (
                f"Todo {todo_id} requires delivery_bundle evidence for "
                + ", ".join(missing_kinds)
            )
        return None
    accepted = available.get(contract, set())
    if not refs:
        return f"Todo {todo_id} requires {contract} evidence before completion"
    unknown = [ref for ref in refs if ref not in accepted]
    if unknown:
        return (
            f"Todo {todo_id} references unknown {contract} evidence: "
            + ", ".join(unknown)
        )
    return None


def _apply_operations(
    todos: list[dict[str, Any]],
    operations: list[TodoPatchOperation],
    *,
    tool_call_id: str,
    run_id: str,
    query_id: str,
    available_evidence: dict[str, set[str]] | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    now = time.time()
    result = [dict(item) for item in todos if isinstance(item, dict)]
    by_id = {str(item.get("id") or ""): item for item in result if item.get("id")}
    by_content = {
        _normalized_todo_content(str(item.get("content") or "")): item
        for item in result
        if _normalized_todo_content(str(item.get("content") or ""))
        and str(item.get("status") or "") != "cancelled"
    }
    applied: list[dict[str, Any]] = []

    for index, operation in enumerate(operations):
        if operation.action == "create":
            normalized_content = _normalized_todo_content(str(operation.content or ""))
            duplicate = by_content.get(normalized_content)
            if duplicate is not None:
                applied.append(
                    {
                        "action": "create",
                        "todo_id": str(duplicate.get("id") or ""),
                        "deduplicated": True,
                    }
                )
                continue
            todo_id = _stable_created_id(tool_call_id, index)
            existing = by_id.get(todo_id)
            if existing is None:
                if operation.status == "completed" and operation.completion_contract:
                    evidence_error = _completion_evidence_error(
                        todo_id=todo_id,
                        contract=operation.completion_contract,
                        refs=list(operation.evidence_refs),
                        available_evidence=available_evidence,
                    )
                    if evidence_error:
                        raise ValueError(evidence_error)
                item = {
                    "id": todo_id,
                    "content": str(operation.content or "").strip(),
                    "status": operation.status or "pending",
                    "created_at": now,
                    "updated_at": now,
                    "created_run_id": run_id or None,
                    "last_changed_run_id": run_id or None,
                    "last_changed_query_id": query_id or None,
                    "completion_contract": operation.completion_contract,
                    "evidence_refs": list(dict.fromkeys(operation.evidence_refs)),
                }
                result.append(item)
                by_id[todo_id] = item
                by_content[normalized_content] = item
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
        prior_normalized_content = _normalized_todo_content(str(item.get("content") or ""))
        if operation.action == "update":
            if operation.content is not None:
                item["content"] = operation.content.strip()
            if operation.status is not None:
                item["status"] = operation.status
            if operation.completion_contract is not None:
                item["completion_contract"] = operation.completion_contract
            if operation.evidence_refs:
                item["evidence_refs"] = list(
                    dict.fromkeys([*(item.get("evidence_refs") or []), *operation.evidence_refs])
                )
            if operation.status == "completed" and item.get("completion_contract"):
                contract = str(item["completion_contract"])
                refs = list(item.get("evidence_refs") or [])
                evidence_error = _completion_evidence_error(
                    todo_id=todo_id,
                    contract=contract,
                    refs=refs,
                    available_evidence=available_evidence,
                )
                if evidence_error:
                    raise ValueError(evidence_error)
        else:
            if operation.action == "complete":
                contract = str(item.get("completion_contract") or "")
                refs = list(dict.fromkeys([*(item.get("evidence_refs") or []), *operation.evidence_refs]))
                if contract:
                    evidence_error = _completion_evidence_error(
                        todo_id=todo_id,
                        contract=contract,
                        refs=refs,
                        available_evidence=available_evidence,
                    )
                    if evidence_error:
                        raise ValueError(evidence_error)
                    item["evidence_refs"] = refs
            item["status"] = {
                "start": "in_progress",
                "complete": "completed",
                "cancel": "cancelled",
                "reopen": "pending",
            }[operation.action]
        if prior_normalized_content and by_content.get(prior_normalized_content) is item:
            by_content.pop(prior_normalized_content, None)
        next_normalized_content = _normalized_todo_content(str(item.get("content") or ""))
        if next_normalized_content and str(item.get("status") or "") != "cancelled":
            by_content[next_normalized_content] = item
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
    expected_revision: int | None = None,
) -> ToolMessage | Command[Any]:
    context = runtime.context if isinstance(runtime.context, dict) else {}
    if str(context.get("run_kind") or "") == "goal_inspection":
        return ToolMessage(
            content=(
                "Todo update rejected: this is a read-only Goal inspection Run. "
                "The user must explicitly request Goal continuation before Todo mutation."
            ),
            tool_call_id=runtime.tool_call_id,
            name="update_todos",
            status="error",
        )
    available_evidence = _available_todo_evidence(
        session_id=str(context.get("session_id") or ""),
        run_id=str(context.get("run_id") or ""),
    )
    goal_id = str(context.get("goal_id") or "") or None
    goal_revision = context.get("goal_revision")

    def mutate_authoritative(
        authoritative_todos: list[dict[str, Any]],
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        next_items, applied_items = _apply_operations(
            authoritative_todos,
            operations,
            tool_call_id=runtime.tool_call_id,
            run_id=str(context.get("run_id") or ""),
            query_id=str(context.get("query_id") or ""),
            available_evidence=available_evidence,
        )
        for item in next_items:
            item["goal_id"] = goal_id
            item["goal_revision"] = goal_revision
        return next_items, applied_items

    session_id = str(context.get("session_id") or "")
    # Isolated ToolNode tests and third-party graph embeddings may not mount a
    # Session authority. Preserve local Command semantics there; production
    # Agent Runs always provide session_id and therefore use the durable path.
    if not session_id or not session_manager.is_initialized:
        try:
            next_todos, applied = mutate_authoritative(
                list(runtime.state.get("todos") or [])
            )
        except ValueError as exc:
            return ToolMessage(
                content=(
                    f"Todo update rejected: {exc}. "
                    "Do not retry unknown IDs; recreate missing work with create operations."
                ),
                tool_call_id=runtime.tool_call_id,
                name="update_todos",
                status="error",
            )
        receipt = {"ledger_revision": 0, "durably_persisted": False}
    else:
        try:
            receipt = session_manager.apply_todo_patch(
                session_id,
                goal_id=goal_id,
                goal_revision=goal_revision,
                run_id=(
                    None
                    if str(context.get("goal_id") or "")
                    else str(context.get("run_id") or "") or None
                ),
                operation_id=runtime.tool_call_id,
                expected_revision=expected_revision,
                mutator=mutate_authoritative,
            )
            next_todos = list(receipt["todos"])
            applied = list(receipt["applied"])
            receipt["durably_persisted"] = True
        except (FileNotFoundError, ValueError) as exc:
            # A stale model-visible ID must not abort the entire Run. Returning a
            # tool error lets the model reconcile against the authoritative ledger
            # and recreate genuinely missing work with a fresh stable ID.
            return ToolMessage(
                content=(
                    f"Todo update rejected: {exc}. "
                    "Do not retry unknown IDs; recreate missing work with create operations."
                ),
                tool_call_id=runtime.tool_call_id,
                name="update_todos",
                status="error",
            )
    return Command(
        update={
            "todos": next_todos,
            "messages": [
                ToolMessage(
                    content=(
                        f"Applied Todo operations: {applied}. "
                        f"Ledger revision: {receipt['ledger_revision']}. "
                        "Durably persisted: "
                        f"{str(bool(receipt.get('durably_persisted'))).lower()}. "
                        f"Current ledger: {next_todos}"
                    ),
                    tool_call_id=runtime.tool_call_id,
                    name="update_todos",
                )
            ],
        }
    )


def _available_todo_evidence(*, session_id: str, run_id: str) -> dict[str, set[str]]:
    available = {
        "validation_receipt": set(),
        "artifact_receipt": set(),
        "query_result": set(),
    }
    run = session_manager.get_run_state(session_id, run_id) if session_id and run_id else None
    activations = run.get("verification_activations") if isinstance(run, dict) else None
    for activation in activations if isinstance(activations, list) else []:
        if not isinstance(activation, dict):
            continue
        for ref in activation.get("evidence_refs") or []:
            if not isinstance(ref, dict) or ref.get("material") is not True:
                continue
            if ref.get("kind") == "validation_receipt" and ref.get("validation_receipt_id"):
                available["validation_receipt"].add(str(ref["validation_receipt_id"]))
            if ref.get("kind") == "artifact_write" and ref.get("artifact_id"):
                available["artifact_receipt"].add(str(ref["artifact_id"]))
            if ref.get("kind") in {"analytics_result", "tool_result"}:
                result_id = ref.get("result_id") or ref.get("ref") or ref.get("output_digest")
                if result_id:
                    available["query_result"].add(str(result_id))
    return available


async def _aupdate_todos(
    runtime: ToolRuntime[Any, Any],
    operations: list[TodoPatchOperation],
    expected_revision: int | None = None,
) -> ToolMessage | Command[Any]:
    return _update_todos(runtime, operations, expected_revision)


class HarnessTodoMiddleware(AgentMiddleware[HarnessTodoState, Any, Any]):
    """Expose patch-style Todo updates instead of whole-list replacement."""

    state_schema = HarnessTodoState

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
