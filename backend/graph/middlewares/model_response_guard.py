"""Bounded recovery for terminal model responses without a deliverable answer."""

from __future__ import annotations

from typing import Annotated, Any, NotRequired

from langchain.agents.middleware import AgentMiddleware
from langchain.agents.middleware.types import AgentState, PrivateStateAttr, hook_config
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

MODEL_RESPONSE_RECOVERY_SOURCE = "puddingclaw_model_response_recovery"
_TRUNCATED_FINISH_REASONS = frozenset({"length", "max_tokens", "max_output_tokens"})
_FILTERED_FINISH_REASONS = frozenset({"content_filter", "safety", "blocked"})


class TerminalModelResponseGuardState(AgentState):
    _model_response_recovery_count: NotRequired[Annotated[int, PrivateStateAttr]]
    _model_response_termination: NotRequired[Annotated[dict[str, Any], PrivateStateAttr]]
    _model_response_incomplete: NotRequired[Annotated[dict[str, Any] | None, PrivateStateAttr]]


def _content_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if not isinstance(value, list):
        return ""
    parts: list[str] = []
    for block in value:
        if isinstance(block, str):
            parts.append(block)
            continue
        if not isinstance(block, dict):
            continue
        block_type = str(block.get("type") or "")
        if block_type in {"reasoning", "reasoning_content", "thinking"}:
            continue
        if block_type in {"text", "output_text"}:
            text = block.get("text")
            if isinstance(text, str):
                parts.append(text)
    return "".join(parts)


def _reasoning_text(message: Any) -> str:
    direct = getattr(message, "reasoning_content", None)
    if isinstance(direct, str):
        return direct
    additional = getattr(message, "additional_kwargs", None)
    if isinstance(additional, dict):
        value = additional.get("reasoning_content")
        if isinstance(value, str):
            return value
        if isinstance(value, list):
            return "".join(str(item) for item in value if item is not None)
    content = getattr(message, "content", None)
    if isinstance(content, list):
        return "".join(
            str(block.get("text") or block.get("reasoning") or "")
            for block in content
            if isinstance(block, dict)
            and str(block.get("type") or "") in {"reasoning", "reasoning_content", "thinking"}
        )
    return ""


def _finish_reason(message: Any) -> str | None:
    for raw in (
        getattr(message, "response_metadata", None),
        getattr(message, "additional_kwargs", None),
    ):
        if not isinstance(raw, dict):
            continue
        for key in ("finish_reason", "stop_reason", "finishReason"):
            value = raw.get(key)
            if value is not None and str(value).strip():
                return str(value).strip().lower()
    return None


def _last_ai_message(state: Any) -> AIMessage | Any | None:
    messages = state.get("messages") if isinstance(state, dict) else getattr(state, "messages", None)
    for message in reversed(list(messages or [])):
        if isinstance(message, AIMessage) or getattr(message, "type", None) == "ai":
            return message
    return None


def _pending_terminal_tool_calls(state: Any, message: Any) -> list[str]:
    """Return tool calls from the terminal AI turn that lack a ToolMessage.

    A return_direct or structured-output tool may legitimately be the terminal
    graph turn.  In that case its ToolMessage is already present and the guard
    must not force an extra model call.
    """

    tool_calls = list(getattr(message, "tool_calls", None) or [])
    pending = {
        str(call.get("id") or f"<missing-id:{index}>")
        for index, call in enumerate(tool_calls)
        if isinstance(call, dict)
    }
    if not pending:
        return []
    messages = list(state.get("messages") or []) if isinstance(state, dict) else []
    try:
        message_index = max(index for index, item in enumerate(messages) if item is message)
    except ValueError:
        return sorted(pending)
    for item in messages[message_index + 1 :]:
        if isinstance(item, ToolMessage) or getattr(item, "type", None) == "tool":
            call_id = str(getattr(item, "tool_call_id", "") or "")
            pending.discard(call_id)
    return sorted(pending)


def terminal_model_response_summary(state: Any) -> tuple[dict[str, Any], str | None, bool]:
    """Return the minimal termination summary, invalid reason and recoverability."""

    message = _last_ai_message(state)
    if message is None:
        summary = {
            "finish_reason": None,
            "content_chars": 0,
            "reasoning_chars": 0,
            "tool_call_count": 0,
            "invalid_reason": "missing_ai_message",
        }
        return summary, "missing_ai_message", True

    content = _content_text(getattr(message, "content", None)).strip()
    reasoning = _reasoning_text(message).strip()
    tool_calls = list(getattr(message, "tool_calls", None) or [])
    pending_tool_call_ids = _pending_terminal_tool_calls(state, message)
    invalid_tool_calls = list(getattr(message, "invalid_tool_calls", None) or [])
    finish_reason = _finish_reason(message)
    invalid_reason: str | None = None
    recoverable = True
    if finish_reason in _FILTERED_FINISH_REASONS:
        invalid_reason = "provider_content_filtered"
        recoverable = False
    elif finish_reason in _TRUNCATED_FINISH_REASONS:
        invalid_reason = "provider_output_truncated"
    elif invalid_tool_calls:
        invalid_reason = "invalid_tool_calls"
    elif finish_reason in {"tool_calls", "function_call"} and not tool_calls:
        invalid_reason = "missing_tool_calls"
    elif pending_tool_call_ids:
        # Only an unconsumed call is incomplete. return_direct and structured
        # output tools legitimately terminate with a matching ToolMessage.
        invalid_reason = "terminal_tool_turn_without_final_response"
    elif not content and not tool_calls:
        invalid_reason = "reasoning_without_final_content" if reasoning else "empty_terminal_response"

    summary = {
        "finish_reason": finish_reason,
        "content_chars": len(content),
        "reasoning_chars": len(reasoning),
        "tool_call_count": len(tool_calls),
        "pending_tool_call_count": len(pending_tool_call_ids),
        "invalid_tool_call_count": len(invalid_tool_calls),
        "invalid_reason": invalid_reason,
    }
    return summary, invalid_reason, recoverable


class TerminalModelResponseGuardMiddleware(AgentMiddleware[Any, Any, Any]):
    """Require a deliverable terminal Assistant response and recover once."""

    state_schema = TerminalModelResponseGuardState

    def __init__(self, *, max_recovery_attempts: int = 1) -> None:
        self.max_recovery_attempts = max(0, min(3, int(max_recovery_attempts)))

    @staticmethod
    def _emit(runtime: Any, payload: dict[str, Any]) -> None:
        writer = getattr(runtime, "stream_writer", None)
        if writer is not None:
            writer(payload)

    def _update(self, state: Any, runtime: Any) -> dict[str, Any] | None:
        summary, invalid_reason, recoverable = terminal_model_response_summary(dict(state))
        if invalid_reason is None:
            attempts = int(state.get("_model_response_recovery_count") or 0)
            return {
                "_model_response_termination": {
                    **summary,
                    "recovery_attempted": attempts > 0,
                    "recovery_attempts": attempts,
                },
                "_model_response_incomplete": None,
            }

        attempts = int(state.get("_model_response_recovery_count") or 0)
        context = getattr(runtime, "context", None)
        context = context if isinstance(context, dict) else {}
        event_base = {
            "session_id": str(context.get("session_id") or ""),
            "query_id": str(context.get("query_id") or ""),
            "run_id": str(context.get("run_id") or ""),
            "reason": invalid_reason,
            "termination": summary,
        }
        if recoverable and attempts < self.max_recovery_attempts:
            next_attempt = attempts + 1
            self._emit(
                runtime,
                {
                    "type": "model_response_recovery_started",
                    "status": "running",
                    "attempt": next_attempt,
                    "max_attempts": self.max_recovery_attempts,
                    **event_base,
                },
            )
            return {
                "_model_response_recovery_count": next_attempt,
                "_model_response_termination": {
                    **summary,
                    "recovery_attempted": True,
                    "recovery_attempt": next_attempt,
                },
                "messages": [
                    HumanMessage(
                        name=MODEL_RESPONSE_RECOVERY_SOURCE,
                        additional_kwargs={"lc_source": MODEL_RESPONSE_RECOVERY_SOURCE},
                        content=(
                            "上一轮模型回合没有形成可交付的最终 Assistant 内容"
                            f"（原因：{invalid_reason}）。请从当前 Run 的已有 ToolMessage、Todo、证据和产物继续，"
                            "不要重复已经成功的工具调用。若仍有工作则继续调用必要工具；若工作已经完成，"
                            "请直接给出完整、正式的最终回答，不要只输出思考过程或进度说明。"
                        ),
                    )
                ],
                "jump_to": "model",
            }

        failure = {
            "code": "model_response_incomplete",
            "message": "模型未返回完整的最终回答或可执行工具调用。",
            "recoverable": recoverable,
            "reason": invalid_reason,
            "recovery_attempts": attempts,
        }
        self._emit(
            runtime,
            {
                "type": "model_response_incomplete",
                "status": "failed",
                "failure": failure,
                **event_base,
            },
        )
        return {
            "_model_response_termination": {
                **summary,
                "recovery_attempted": attempts > 0,
                "recovery_attempts": attempts,
            },
            "_model_response_incomplete": failure,
        }

    @hook_config(can_jump_to=["model"])
    def after_agent(self, state: Any, runtime: Any) -> dict[str, Any] | None:
        return self._update(state, runtime)

    @hook_config(can_jump_to=["model"])
    async def aafter_agent(self, state: Any, runtime: Any) -> dict[str, Any] | None:
        return self._update(state, runtime)
