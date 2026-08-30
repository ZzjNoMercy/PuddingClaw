"""Safe transcript projection for nested verification agents."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from langchain_core.messages import (
    AIMessage,
    BaseMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
    messages_from_dict,
)

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


def materialize_grader_messages(messages: Iterable[Any]) -> list[BaseMessage]:
    """Convert a JSON-safe transcript projection into SDK-native messages."""

    materialized: list[BaseMessage] = []
    for message_index, message in enumerate(messages):
        if isinstance(message, BaseMessage):
            materialized.append(message)
            continue
        if not isinstance(message, dict):
            raise TypeError(f"Unsupported grader message type: {type(message).__name__}")
        if isinstance(message.get("data"), dict) and message.get("type"):
            materialized.extend(messages_from_dict([message]))
            continue

        role, name, additional = message_metadata(message)
        content = message.get("content", "")
        query_id = str(message.get("query_id") or "")
        if query_id:
            additional.setdefault("puddingclaw_query_id", query_id)
        message_name = name or None
        if role in {"human", "user"}:
            materialized.append(
                HumanMessage(content=content, name=message_name, additional_kwargs=additional)
            )
            continue
        if role == "system":
            materialized.append(
                SystemMessage(content=content, name=message_name, additional_kwargs=additional)
            )
            continue
        if role in {"ai", "assistant"}:
            raw_calls = [item for item in message.get("tool_calls") or [] if isinstance(item, dict)]
            tool_calls: list[dict[str, Any]] = []
            tool_results: list[ToolMessage] = []
            for call_index, raw_call in enumerate(raw_calls):
                call_id = str(
                    raw_call.get("id")
                    or f"grader_projection_{message_index}_{call_index}"
                )
                raw_args = raw_call.get("args", raw_call.get("input", {}))
                args = dict(raw_args) if isinstance(raw_args, dict) else {"input": raw_args}
                tool_name = str(raw_call.get("name") or raw_call.get("tool") or "unknown_tool")
                tool_calls.append({"id": call_id, "name": tool_name, "args": args})
                raw_output = raw_call.get("output", raw_call.get("raw_output", ""))
                output = (
                    raw_output
                    if isinstance(raw_output, (str, list))
                    else str(raw_output or "")
                )
                tool_results.append(
                    ToolMessage(
                        content=output,
                        tool_call_id=call_id,
                        name=tool_name,
                        status=(
                            "error"
                            if raw_call.get("is_error")
                            or raw_call.get("status") in {"error", "failed", "interrupted"}
                            else "success"
                        ),
                    )
                )
            if tool_calls:
                materialized.append(
                    AIMessage(
                        content="",
                        tool_calls=tool_calls,
                        name=message_name,
                        additional_kwargs=additional,
                    )
                )
                materialized.extend(tool_results)
            if content or not tool_calls:
                materialized.append(
                    AIMessage(content=content, name=message_name, additional_kwargs=additional)
                )
            continue
        if role == "tool":
            materialized.append(
                ToolMessage(
                    content=content,
                    tool_call_id=str(message.get("tool_call_id") or message.get("id") or "unknown"),
                    name=message_name,
                    additional_kwargs=additional,
                )
            )
            continue
        raise ValueError(f"Unsupported grader message role: {role or '<empty>'}")
    return materialized


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
