"""GET/POST /api/tokens — Token counting for sessions and files."""

from pathlib import Path
from typing import Any

import tiktoken
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from graph.session_manager import session_manager
from graph.prompt_builder import build_system_prompt
from config import get_rag_mode, get_compaction_trigger_tokens

router = APIRouter()

BASE_DIR = Path(__file__).resolve().parent.parent

# Cache the encoder instance
_encoder = tiktoken.get_encoding("cl100k_base")


def _count_tokens(text: str) -> int:
    """Count tokens using cl100k_base encoding."""
    return len(_encoder.encode(text))


@router.get("/tokens/session/{session_id}")
async def get_session_token_count(session_id: str) -> dict[str, Any]:
    """Count tokens in a session: system prompt + all messages.

    若存在 context_usage_peak 且大于静态统计，则优先使用峰值，
    因为 session 中的 tool output 可能已被摘要或截断，峰值更能反映 LLM 实际消耗。
    返回的 total_tokens 分母使用 compaction_trigger（默认 500K），便于前端显示压缩进度。
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
