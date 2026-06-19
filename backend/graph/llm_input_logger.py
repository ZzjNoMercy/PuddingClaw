"""Structured logging for payloads sent to the LLM.

The log is intentionally JSONL so a problematic phrase can be grepped and the
exact pre-agent/model-request context can be inspected without reproducing the UI.
"""

from __future__ import annotations

import contextvars
import hashlib
import json
import os
import time
from datetime import datetime
from pathlib import Path
from typing import Any


current_session_id: contextvars.ContextVar[str] = contextvars.ContextVar(
    "llm_input_session_id", default=""
)
current_user_id: contextvars.ContextVar[str] = contextvars.ContextVar(
    "llm_input_user_id", default=""
)


def _enabled() -> bool:
    return os.getenv("LLM_INPUT_LOG_ENABLED", "1").lower() not in {"0", "false", "no"}


def _max_chars() -> int:
    try:
        return int(os.getenv("LLM_INPUT_LOG_MAX_CHARS_PER_MESSAGE", "200000"))
    except ValueError:
        return 200000


def _log_dir() -> Path:
    default_dir = Path(__file__).resolve().parent.parent / "logs" / "llm-input"
    return Path(os.getenv("LLM_INPUT_LOG_DIR", str(default_dir)))


def _content_to_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(
            block.get("text", "") if isinstance(block, dict) else str(block)
            for block in content
        )
    return "" if content is None else str(content)


def _maybe_truncate(text: str) -> tuple[str, bool]:
    limit = _max_chars()
    if limit <= 0 or len(text) <= limit:
        return text, False
    return text[:limit], True


def _serialize_tool_calls(tool_calls: Any) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for tc in tool_calls or []:
        if isinstance(tc, dict):
            args = tc.get("args", tc.get("input", ""))
            args_text, args_truncated = _maybe_truncate(_content_to_text(args))
            result.append({
                "id": tc.get("id", ""),
                "name": tc.get("name") or tc.get("tool", ""),
                "args": args_text,
                "args_len": len(_content_to_text(args)),
                "args_truncated": args_truncated,
            })
        else:
            args = getattr(tc, "args", "")
            args_text, args_truncated = _maybe_truncate(_content_to_text(args))
            result.append({
                "id": getattr(tc, "id", ""),
                "name": getattr(tc, "name", ""),
                "args": args_text,
                "args_len": len(_content_to_text(args)),
                "args_truncated": args_truncated,
            })
    return result


def _serialize_message(message: Any, index: int) -> dict[str, Any]:
    content = _content_to_text(getattr(message, "content", ""))
    content_logged, content_truncated = _maybe_truncate(content)
    return {
        "index": index,
        "type": getattr(message, "type", type(message).__name__),
        "role": getattr(message, "role", ""),
        "name": getattr(message, "name", ""),
        "id": getattr(message, "id", ""),
        "tool_call_id": getattr(message, "tool_call_id", ""),
        "content_len": len(content),
        "content_sha256": hashlib.sha256(content.encode("utf-8", errors="replace")).hexdigest(),
        "content_truncated": content_truncated,
        "content": content_logged,
        "tool_calls": _serialize_tool_calls(getattr(message, "tool_calls", None)),
        "additional_kwargs_keys": sorted(
            list((getattr(message, "additional_kwargs", {}) or {}).keys())
        ),
    }


def log_llm_input(
    *,
    source: str,
    messages: list[Any],
    system_message: Any = None,
    session_id: str | None = None,
    user_id: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> None:
    """Append one JSONL entry describing the payload about to enter the LLM."""
    if not _enabled():
        return

    sid = session_id if session_id is not None else current_session_id.get()
    uid = user_id if user_id is not None else current_user_id.get()
    system_content = _content_to_text(getattr(system_message, "content", system_message or ""))
    system_logged, system_truncated = _maybe_truncate(system_content)

    entry = {
        "ts": time.time(),
        "iso": datetime.now().isoformat(timespec="seconds"),
        "source": source,
        "session_id": sid,
        "user_id": uid,
        "message_count": len(messages),
        "metadata": metadata or {},
        "system": {
            "content_len": len(system_content),
            "content_sha256": hashlib.sha256(system_content.encode("utf-8", errors="replace")).hexdigest(),
            "content_truncated": system_truncated,
            "content": system_logged,
        },
        "messages": [
            _serialize_message(message, idx)
            for idx, message in enumerate(messages)
        ],
    }

    log_dir = _log_dir()
    log_dir.mkdir(parents=True, exist_ok=True)
    path = log_dir / f"{datetime.now().strftime('%Y-%m-%d')}.jsonl"
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")
