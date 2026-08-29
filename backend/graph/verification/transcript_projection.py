"""Safe transcript projection for nested verification agents."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from langchain_core.messages import AIMessage, HumanMessage

INTERNAL_GRADER_MESSAGE_SOURCES = frozenset(
    {
        "rubric_grader",
        "puddingclaw_completion_gate",
        "puddingclaw_goal_completion_protocol",
        "puddingclaw_goal_continuation",
        "puddingclaw_model_response_recovery",
    }
)


def message_metadata(message: Any) -> tuple[str, str, dict[str, Any]]:
    if isinstance(message, dict):
        data = message.get("data") if isinstance(message.get("data"), dict) else message
        role = str(data.get("role") or data.get("type") or message.get("type") or "")
        name = str(data.get("name") or "")
        additional = data.get("additional_kwargs")
        return role, name, dict(additional) if isinstance(additional, dict) else {}
    role = str(getattr(message, "type", None) or getattr(message, "role", None) or "")
    name = str(getattr(message, "name", None) or "")
    additional = getattr(message, "additional_kwargs", None)
    return role, name, dict(additional) if isinstance(additional, dict) else {}


def is_internal_control_message(message: Any, *, extra_sources: Iterable[str] = ()) -> bool:
    _, name, metadata = message_metadata(message)
    source = str(metadata.get("lc_source") or name or "")
    return source in INTERNAL_GRADER_MESSAGE_SOURCES | frozenset(str(item) for item in extra_sources)


def is_external_user_message(message: Any) -> bool:
    role, _, metadata = message_metadata(message)
    return role in {"human", "user"} and not metadata.get("lc_source") and not is_internal_control_message(message)


def project_messages_for_grader(
    messages: Iterable[Any],
    *,
    run_query_id: str | None = None,
    objective: str | None = None,
    extra_internal_sources: Iterable[str] = (),
) -> list[Any]:
    """Return a current-Run, control-message-free copy for grading."""

    source = list(messages)
    start: int | None = None
    if run_query_id:
        for index in range(len(source) - 1, -1, -1):
            role, _, metadata = message_metadata(source[index])
            if role in {"human", "user"} and metadata.get("puddingclaw_query_id") == run_query_id:
                start = index
                break
    if start is None:
        for index in range(len(source) - 1, -1, -1):
            if is_external_user_message(source[index]):
                start = index
                break

    if start is None:
        tail = next(([item] for item in reversed(source) if isinstance(item, AIMessage)), [])
        scoped = [HumanMessage(content=objective, name="puddingclaw_run_objective")] + tail if objective else tail
    else:
        scoped = source[start:]

    filtered = [
        item
        for item in scoped
        if not is_internal_control_message(item, extra_sources=extra_internal_sources)
    ]
    first_name = message_metadata(filtered[0])[1] if filtered else ""
    if objective and (
        not filtered
        or (
            not is_external_user_message(filtered[0])
            and first_name != "puddingclaw_run_objective"
        )
    ):
        filtered.insert(0, HumanMessage(content=objective, name="puddingclaw_run_objective"))
    return filtered


def serialize_projected_messages(messages: Iterable[Any]) -> list[dict[str, Any]]:
    """Serialize the bounded projection into a replayable, JSON-safe form."""

    serialized: list[dict[str, Any]] = []
    for message in messages:
        if isinstance(message, dict):
            data = message.get("data") if isinstance(message.get("data"), dict) else message
            serialized.append(
                {
                    "role": str(data.get("role") or data.get("type") or message.get("type") or ""),
                    "name": str(data.get("name") or "") or None,
                    "content": data.get("content"),
                    "additional_kwargs": (
                        dict(data.get("additional_kwargs"))
                        if isinstance(data.get("additional_kwargs"), dict)
                        else {}
                    ),
                    "tool_calls": [
                        dict(item)
                        for item in data.get("tool_calls") or []
                        if isinstance(item, dict)
                    ],
                }
            )
            continue
        serialized.append(
            {
                "role": str(getattr(message, "type", None) or getattr(message, "role", None) or ""),
                "name": str(getattr(message, "name", None) or "") or None,
                "content": getattr(message, "content", None),
                "additional_kwargs": dict(getattr(message, "additional_kwargs", None) or {}),
                "tool_calls": [
                    dict(item)
                    for item in getattr(message, "tool_calls", None) or []
                    if isinstance(item, dict)
                ],
            }
        )
    return serialized


def candidate_from_projected_messages(
    messages: Iterable[Any],
) -> tuple[str, list[dict[str, Any]]]:
    """Extract the verifier-owned candidate independently of UI presentation."""

    source = list(messages)
    candidate_content = ""
    for message in reversed(source):
        role, _, _ = message_metadata(message)
        if role not in {"ai", "assistant"}:
            continue
        if isinstance(message, dict):
            data = message.get("data") if isinstance(message.get("data"), dict) else message
            content = data.get("content")
        else:
            content = getattr(message, "content", "")
        candidate_content = content if isinstance(content, str) else str(content or "")
        break

    candidate_tool_calls: list[dict[str, Any]] = []
    for message in source:
        if isinstance(message, dict):
            data = message.get("data") if isinstance(message.get("data"), dict) else message
            calls = data.get("tool_calls")
        else:
            calls = getattr(message, "tool_calls", None)
        if isinstance(calls, list):
            candidate_tool_calls.extend(dict(item) for item in calls if isinstance(item, dict))
    return candidate_content, candidate_tool_calls
