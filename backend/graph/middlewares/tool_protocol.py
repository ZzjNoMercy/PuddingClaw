"""Keep OpenAI tool-call message protocol valid across compaction and resume."""

from __future__ import annotations

import logging
import re
from collections.abc import Awaitable, Callable
from typing import Any

from langchain.agents.middleware import AgentMiddleware
from langchain.agents.middleware.types import ModelRequest, ModelResponse
from langchain_core.messages import AIMessage, ToolMessage
from langchain_core.messages.utils import count_tokens_approximately
from langgraph.config import get_stream_writer

logger = logging.getLogger(__name__)

_MISSING_TOOL_OUTPUT = (
    "Tool execution result was unavailable in the compacted Agent context. "
    "Treat this call as unresolved and retry the tool if its result is required."
)

_DROPPED_TOOL_CALLS_KEY = "puddingclaw_dropped_unpairable_tool_calls"

_TOOL_CALL_ID_PATTERN = re.compile(r"^\S{1,256}$")
_TOOL_NAME_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


def _raw_tool_call_id(tool_call: Any) -> str:
    if not isinstance(tool_call, dict):
        return ""
    call_id = tool_call.get("id")
    return call_id if isinstance(call_id, str) else ""


def _raw_tool_call_name(tool_call: Any) -> str:
    if not isinstance(tool_call, dict):
        return ""
    tool_name = tool_call.get("name")
    return tool_name if isinstance(tool_name, str) else ""


def _raw_provider_tool_call_fields(tool_call: Any) -> tuple[str, str, str | None]:
    """Read only fields that ChatOpenAI actually serializes for a raw call."""

    if not isinstance(tool_call, dict):
        return "", "", None
    call_id = tool_call.get("id")
    function = tool_call.get("function")
    if not isinstance(call_id, str) or not isinstance(function, dict):
        return "", "", None
    tool_name = function.get("name")
    arguments = function.get("arguments")
    return (
        call_id,
        tool_name if isinstance(tool_name, str) else "",
        arguments if isinstance(arguments, str) else None,
    )


def _normalize_ai_tool_calls(message: AIMessage) -> tuple[AIMessage, list[tuple[str, str, str]], dict[str, list[str]]]:
    """Mirror the OpenAI serializer and remove calls that cannot be paired.

    LangChain serializes parsed calls first, then invalid calls, and only falls
    back to ``additional_kwargs['tool_calls']`` when neither list is populated.
    The trace/UI historically inspected only parsed calls, which allowed a
    malformed provider call to look like an empty AI message while still being
    replayed to the API on a Rubric jump.
    """

    additional = dict(message.additional_kwargs or {})
    raw_key_present = "tool_calls" in additional
    raw_additional = additional.get("tool_calls")
    raw_calls = list(raw_additional) if isinstance(raw_additional, list) else []
    parsed_calls: list[dict[str, Any]] = []
    invalid_calls: list[dict[str, Any]] = []
    serializable_calls: list[tuple[str, str, str]] = []
    seen_ids: set[str] = set()
    dropped: list[str] = []
    duplicates: list[str] = []
    invalid_ids: list[str] = []
    raw_ids: list[str] = []
    canonicalized_shadow_ids: list[str] = []
    canonicalized_ids: list[str] = []
    dropped_legacy_calls: list[str] = []

    if "function_call" in additional:
        legacy_call = additional.pop("function_call", None)
        legacy_name = legacy_call.get("name") if isinstance(legacy_call, dict) else None
        dropped_legacy_calls.append(legacy_name if isinstance(legacy_name, str) and legacy_name else "<legacy>")

    def accept(tool_call: dict[str, Any], *, source: str) -> dict[str, Any] | None:
        if source == "raw":
            call_id, tool_name, raw_arguments = _raw_provider_tool_call_fields(tool_call)
        else:
            call_id = _raw_tool_call_id(tool_call)
            tool_name = _raw_tool_call_name(tool_call)
            raw_arguments = None
        if not call_id or not _TOOL_CALL_ID_PATTERN.fullmatch(call_id):
            dropped.append(f"{source}:<invalid-id>" if call_id else f"{source}:<missing-id>")
            return None

        if not _TOOL_NAME_PATTERN.fullmatch(tool_name):
            dropped.append(f"{source}:{call_id}:<invalid-name>")
            return None

        if source == "parsed" and not isinstance(tool_call.get("args"), dict):
            dropped.append(f"{source}:{call_id}:<invalid-args>")
            return None
        if source == "invalid" and not isinstance(tool_call.get("args"), str):
            dropped.append(f"{source}:{call_id}:<invalid-args>")
            return None
        if source == "raw":
            function = tool_call.get("function")
            if tool_call.get("type") != "function" or not isinstance(function, dict):
                dropped.append(f"{source}:{call_id}:<invalid-shape>")
                return None
            if raw_arguments is None:
                dropped.append(f"{source}:{call_id}:<invalid-arguments>")
                return None

        if call_id in seen_ids:
            duplicates.append(call_id)
            return None
        seen_ids.add(call_id)
        serializable_calls.append((call_id, tool_name, source))

        if source == "parsed":
            canonical = {
                "id": call_id,
                "name": tool_name,
                "args": tool_call["args"],
                "type": "tool_call",
            }
        elif source == "invalid":
            canonical = {
                "id": call_id,
                "name": tool_name,
                "args": tool_call["args"],
                "error": tool_call.get("error"),
                "type": "invalid_tool_call",
            }
        else:
            canonical = {
                "id": call_id,
                "type": "function",
                "function": {"name": tool_name, "arguments": raw_arguments},
            }
        if canonical != tool_call:
            canonicalized_ids.append(f"{source}:{call_id}")
        return canonical

    structured_calls_present = bool(message.tool_calls or message.invalid_tool_calls)

    for tool_call in message.tool_calls or []:
        candidate = dict(tool_call)
        if canonical := accept(candidate, source="parsed"):
            parsed_calls.append(canonical)

    for tool_call in message.invalid_tool_calls or []:
        candidate = dict(tool_call)
        if canonical := accept(candidate, source="invalid"):
            invalid_calls.append(canonical)
            invalid_ids.append(canonical["id"])

    # Match langchain_openai._convert_message_to_dict precedence exactly.
    if structured_calls_present:
        if raw_key_present:
            shadow_ids = [
                call_id for tool_call in raw_calls if (call_id := _raw_provider_tool_call_fields(tool_call)[0])
            ]
            canonicalized_shadow_ids.extend(shadow_ids or ["<empty-or-invalid>"])
            additional.pop("tool_calls", None)
    else:
        cleaned_raw: list[dict[str, Any]] = []
        if raw_key_present and not isinstance(raw_additional, list):
            dropped.append("raw:<invalid-container>")
        else:
            for tool_call in raw_calls:
                if not isinstance(tool_call, dict):
                    dropped.append("raw:<non-object>")
                    continue
                candidate = dict(tool_call)
                if canonical := accept(candidate, source="raw"):
                    cleaned_raw.append(canonical)
                    raw_ids.append(canonical["id"])
        if cleaned_raw:
            additional["tool_calls"] = cleaned_raw
        else:
            if raw_key_present and not dropped:
                canonicalized_ids.append("raw:<empty>")
            additional.pop("tool_calls", None)

    if dropped:
        existing = additional.get(_DROPPED_TOOL_CALLS_KEY)
        prior = list(existing) if isinstance(existing, list) else []
        additional[_DROPPED_TOOL_CALLS_KEY] = [*prior, *dropped]

    normalized = message.model_copy(
        update={
            "tool_calls": parsed_calls,
            "invalid_tool_calls": invalid_calls,
            "additional_kwargs": additional,
        }
    )
    return (
        normalized,
        serializable_calls,
        {
            "invalid_tool_call_ids": invalid_ids,
            "raw_tool_call_ids": raw_ids,
            "dropped_unpairable_tool_calls": dropped,
            "duplicate_tool_call_ids": duplicates,
            "canonicalized_shadow_tool_call_ids": canonicalized_shadow_ids,
            "canonicalized_tool_call_ids": canonicalized_ids,
            "dropped_legacy_function_calls": dropped_legacy_calls,
        },
    )


def pending_executable_tool_call_ids(messages: list[Any]) -> list[str]:
    """Return parsed tool calls still awaiting a real ToolMessage at the tail.

    A pending parsed call can still be executing in LangGraph's tools node, so
    compact-context persistence must wait instead of freezing a synthetic error
    response. Invalid/raw provider calls are not executable and are repaired at
    the model boundary instead.
    """

    pending: dict[str, None] = {}
    for message in messages:
        if isinstance(message, ToolMessage):
            call_id = message.tool_call_id if isinstance(message.tool_call_id, str) else ""
            pending.pop(call_id, None)
            continue
        if pending:
            pending.clear()
        if isinstance(message, AIMessage):
            _normalized, serializable_calls, _report = _normalize_ai_tool_calls(message)
            pending = {call_id: None for call_id, _tool_name, source in serializable_calls if source == "parsed"}
    return list(pending)


def repair_tool_message_protocol(messages: list[Any]) -> tuple[list[Any], dict[str, list[str]]]:
    """Return an OpenAI-compatible transcript and a deterministic repair report.

    Every AI tool call must be followed by one ToolMessage for each call id
    before any human/assistant message. Missing responses are represented as
    explicit error ToolMessages; orphan responses are removed.
    """

    repaired: list[Any] = []
    pending: dict[str, str] = {}
    missing_ids: list[str] = []
    orphan_ids: list[str] = []
    invalid_ids: list[str] = []
    raw_ids: list[str] = []
    dropped_calls: list[str] = []
    duplicate_ids: list[str] = []
    canonicalized_shadow_ids: list[str] = []
    canonicalized_ids: list[str] = []
    dropped_legacy_calls: list[str] = []

    def close_pending() -> None:
        for call_id, tool_name in pending.items():
            repaired.append(
                ToolMessage(
                    content=_MISSING_TOOL_OUTPUT,
                    tool_call_id=call_id,
                    name=tool_name or None,
                    status="error",
                    additional_kwargs={"lc_source": "puddingclaw_tool_protocol_repair"},
                )
            )
            missing_ids.append(call_id)
        pending.clear()

    for message in messages:
        if isinstance(message, ToolMessage):
            call_id = message.tool_call_id if isinstance(message.tool_call_id, str) else ""
            if call_id and call_id in pending:
                repaired.append(message)
                pending.pop(call_id, None)
            else:
                orphan_ids.append(call_id or "<empty>")
            continue

        if pending:
            close_pending()
        if isinstance(message, AIMessage):
            normalized, serializable_calls, normalization_report = _normalize_ai_tool_calls(message)
            repaired.append(normalized)
            invalid_ids.extend(normalization_report["invalid_tool_call_ids"])
            raw_ids.extend(normalization_report["raw_tool_call_ids"])
            dropped_calls.extend(normalization_report["dropped_unpairable_tool_calls"])
            duplicate_ids.extend(normalization_report["duplicate_tool_call_ids"])
            canonicalized_shadow_ids.extend(normalization_report["canonicalized_shadow_tool_call_ids"])
            canonicalized_ids.extend(normalization_report["canonicalized_tool_call_ids"])
            dropped_legacy_calls.extend(normalization_report["dropped_legacy_function_calls"])
            for call_id, tool_name, _source in serializable_calls:
                pending[call_id] = tool_name
        else:
            repaired.append(message)

    if pending:
        close_pending()
    return repaired, {
        "missing_tool_call_ids": missing_ids,
        "orphan_tool_call_ids": orphan_ids,
        "invalid_tool_call_ids": invalid_ids,
        "raw_tool_call_ids": raw_ids,
        "dropped_unpairable_tool_calls": dropped_calls,
        "duplicate_tool_call_ids": duplicate_ids,
        "canonicalized_shadow_tool_call_ids": canonicalized_shadow_ids,
        "canonicalized_tool_call_ids": canonicalized_ids,
        "dropped_legacy_function_calls": dropped_legacy_calls,
    }


def _report_has_changes(report: dict[str, list[str]]) -> bool:
    return any(report.values())


class ToolProtocolIntegrityMiddleware(AgentMiddleware):
    """Repair protocol gaps at the last boundary before every model request."""

    def __init__(
        self,
        *,
        context_trigger_tokens: int = 160000,
        emit_context_usage: bool = True,
    ) -> None:
        super().__init__()
        self.context_trigger_tokens = max(1, int(context_trigger_tokens))
        self.emit_context_usage = emit_context_usage

    @property
    def name(self) -> str:
        return "ToolProtocolIntegrityMiddleware"

    @staticmethod
    def _prepare(request: ModelRequest) -> ModelRequest:
        repaired, report = repair_tool_message_protocol(list(request.messages))
        if _report_has_changes(report):
            logger.warning("Repaired Agent tool-message protocol before model call: %s", report)
            return request.override(messages=repaired)
        return request

    def _emit_context_usage(self, request: ModelRequest, response: ModelResponse) -> None:
        if not self.emit_context_usage:
            return
        messages = [
            *([request.system_message] if request.system_message is not None else []),
            *request.messages,
            *response.result,
        ]
        try:
            used_tokens = int(count_tokens_approximately(messages, tools=request.tools))
        except TypeError:
            used_tokens = int(count_tokens_approximately(messages))
        try:
            writer = get_stream_writer()
            writer(
                {
                    "type": "context_usage",
                    "used_tokens": used_tokens,
                    "total_tokens": self.context_trigger_tokens,
                    "percentage": round(used_tokens / self.context_trigger_tokens * 100, 1),
                    "includes_tool_schemas": True,
                }
            )
        except RuntimeError:
            return

    def wrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], ModelResponse],
    ) -> ModelResponse:
        prepared = self._prepare(request)
        response = handler(prepared)
        self._emit_context_usage(prepared, response)
        return response

    async def awrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], Awaitable[ModelResponse]],
    ) -> ModelResponse:
        prepared = self._prepare(request)
        response = await handler(prepared)
        self._emit_context_usage(prepared, response)
        return response
