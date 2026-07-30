"""GET/POST /api/tokens — Token counting for sessions and files."""

from pathlib import Path
from typing import Any, Literal

import tiktoken
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from graph.session_manager import session_manager
from graph.prompt_builder import build_system_prompt
from config import (
    get_compaction_trigger_tokens,
    get_deepagents_summarization_config,
    get_deepagents_tool_context_config,
    get_rag_mode,
)

router = APIRouter()

BASE_DIR = Path(__file__).resolve().parent.parent

# Cache the encoder instance with a fallback for offline / slow networks.
try:
    _encoder = tiktoken.get_encoding("cl100k_base")
except Exception as _tiktoken_exc:  # pragma: no cover - offline fallback
    import warnings

    warnings.warn(
        f"Failed to load tiktoken cl100k_base ({_tiktoken_exc}); "
        "using rough character-based token estimate."
    )

    class _FallbackEncoder:
        """Rough token estimator when tiktoken encodings cannot be downloaded."""

        def encode(self, text: str) -> list[int]:
            return [0] * max(1, len(text) // 4)

    _encoder = _FallbackEncoder()


def _count_tokens(text: str) -> int:
    """Count tokens using cl100k_base encoding (or a rough fallback)."""
    return len(_encoder.encode(text))


@router.get("/tokens/session/{session_id}")
async def get_session_token_count(
    session_id: str,
    runtime_mode: Literal["chat", "agent"] | None = None,
) -> dict[str, Any]:
    """Count tokens in a session: system prompt + all messages.

    若存在 context_usage_peak 且大于静态统计，则优先使用峰值，
    因为 session 中的 tool output 可能已被摘要或截断，峰值更能反映 LLM 实际消耗。
    返回的 total_tokens 分母按运行时选择：Chat 使用原 compact 阈值，
    DeepAgents 使用其独立的全局 summarize 阈值（默认 200K）。
    """
    system_prompt = build_system_prompt(BASE_DIR, rag_mode=get_rag_mode())
    system_tokens = _count_tokens(system_prompt)

    messages = session_manager.load_session(session_id)
    message_tokens = 0
    tool_output_tokens = 0
    for msg in messages:
        message_tokens += _count_tokens(msg.get("content", ""))
        for tc in msg.get("tool_calls", []):
            tool_output_tokens += _count_tokens(tc.get("output", ""))

    message_tokens += tool_output_tokens

    metadata = session_manager.get_metadata(session_id)
    # The new-conversation workbench uses the non-persisted ``default``
    # placeholder until the first message creates its real Session.  There is
    # therefore no authoritative runtime_mode to read yet.  Let the caller
    # describe that placeholder, while always trusting persisted Session
    # metadata once it exists.  Without this distinction an Agent workbench
    # briefly reports the legacy Chat compaction limit (500K), then jumps to
    # the DeepAgents summarization limit after the first query.
    effective_runtime_mode = (
        metadata.get("runtime_mode")
        if session_manager.session_exists(session_id)
        else runtime_mode or metadata.get("runtime_mode")
    )
    is_agent = effective_runtime_mode == "agent"
    if is_agent:
        # Agent keeps the complete transcript for the UI, but DeepAgents may
        # send a much smaller summarized context to the model. Never replace
        # that current value with the historical peak/full transcript size.
        tool_context_enabled = bool(
            get_deepagents_tool_context_config().get("enabled", True)
        )
        current_usage = session_manager.get_effective_agent_context_usage(
            session_id,
            use_tool_context=tool_context_enabled,
        )
        total_tokens = current_usage or (system_tokens + message_tokens)
        message_tokens = max(0, total_tokens - system_tokens)
        compaction_trigger = int(
            get_deepagents_summarization_config().get("trigger_tokens", 200000)
        )
    else:
        context_usage_peak = session_manager.get_context_usage_peak(session_id)
        if context_usage_peak > system_tokens + message_tokens:
            message_tokens = context_usage_peak - system_tokens
        compaction_trigger = get_compaction_trigger_tokens()
        total_tokens = system_tokens + message_tokens
    return {
        "system_tokens": system_tokens,
        "message_tokens": message_tokens,
        "total_tokens": total_tokens,
        "compaction_trigger": compaction_trigger,
        "percentage": round(total_tokens / compaction_trigger * 100, 1),
    }


class FileTokenRequest(BaseModel):
    paths: list[str]


@router.post("/tokens/files")
async def get_file_token_counts(request: FileTokenRequest) -> dict[str, Any]:
    """Count tokens for a list of files."""
    results: list[dict[str, Any]] = []
    for rel_path in request.paths:
        normalized = rel_path.replace("\\", "/").lstrip("./")
        full_path = (BASE_DIR / normalized).resolve()
        if not str(full_path).startswith(str(BASE_DIR)):
            results.append({"path": rel_path, "tokens": 0})
            continue
        if not full_path.exists():
            results.append({"path": rel_path, "tokens": 0})
            continue
        try:
            content = full_path.read_text(encoding="utf-8")
            tokens = _count_tokens(content)
            results.append({"path": rel_path, "tokens": tokens})
        except Exception:
            results.append({"path": rel_path, "tokens": 0})

    return {"files": results}
