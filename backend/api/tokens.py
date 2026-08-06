"""GET/POST /api/tokens — Token counting for sessions and files.

Token counting must never make backend startup depend on the network.  The
``cl100k_base`` vocabulary is loaded only when its verified tiktoken cache file
already exists; otherwise the API uses a local approximation.
"""

import hashlib
import logging
import os
import tempfile
import threading
from math import ceil
from pathlib import Path
from typing import Any, Literal

import tiktoken
from fastapi import APIRouter
from pydantic import BaseModel

from config import (
    get_compaction_trigger_tokens,
    get_deepagents_summarization_config,
    get_deepagents_tool_context_config,
    get_rag_mode,
)
from graph.prompt_builder import build_system_prompt
from graph.session_manager import session_manager

router = APIRouter()
logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent.parent

_CL100K_BLOB_URL = (
    "https://openaipublic.blob.core.windows.net/encodings/cl100k_base.tiktoken"
)
_CL100K_SHA256 = "223921b76ee99bde995b7ff738513eef100fb51d18c93597a113bcffe865b2a7"
_CL100K_CACHE_KEY = hashlib.sha1(_CL100K_BLOB_URL.encode()).hexdigest()
_encoder: Any | None = None
_encoder_lock = threading.Lock()
_fallback_logged = False


def _tiktoken_cache_path() -> Path | None:
    """Return the cache path used internally by tiktoken, if caching is enabled."""
    if "TIKTOKEN_CACHE_DIR" in os.environ:
        cache_dir = os.environ["TIKTOKEN_CACHE_DIR"]
    elif "DATA_GYM_CACHE_DIR" in os.environ:
        cache_dir = os.environ["DATA_GYM_CACHE_DIR"]
    else:
        cache_dir = str(Path(tempfile.gettempdir()) / "data-gym-cache")
    if not cache_dir:
        return None
    return Path(cache_dir) / _CL100K_CACHE_KEY


def _has_verified_tiktoken_cache() -> bool:
    """Check the local vocabulary before calling tiktoken's network-capable loader."""
    cache_path = _tiktoken_cache_path()
    if cache_path is None or not cache_path.is_file():
        return False
    try:
        return hashlib.sha256(cache_path.read_bytes()).hexdigest() == _CL100K_SHA256
    except OSError:
        return False


def _get_cached_encoder() -> Any | None:
    """Load cl100k_base only from a verified local cache; never download it here."""
    global _encoder, _fallback_logged
    if _encoder is not None:
        return _encoder
    if not _has_verified_tiktoken_cache():
        if not _fallback_logged:
            logger.warning(
                "cl100k_base cache is unavailable; using local token estimate "
                "without blocking backend startup"
            )
            _fallback_logged = True
        return None
    with _encoder_lock:
        if _encoder is None:
            try:
                # The verified cache guarantees this call will not fetch the BPE
                # vocabulary over the network.
                _encoder = tiktoken.get_encoding("cl100k_base")
            except Exception:
                logger.exception("Failed to load verified cl100k_base cache")
                return None
    return _encoder


def _estimate_tokens(text: str) -> int:
    """Reasonable offline estimate for mixed Latin and CJK content."""
    if not text:
        return 0
    ascii_count = sum(1 for char in text if ord(char) < 128)
    non_ascii_count = len(text) - ascii_count
    return max(1, ceil(ascii_count / 4 + non_ascii_count * 1.2))


def _count_tokens(text: str) -> int:
    """Count tokens from the local cl100k cache or use an offline estimate."""
    encoder = _get_cached_encoder()
    if encoder is None:
        return _estimate_tokens(text)
    return len(encoder.encode(text))


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
    measured = True
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
        if current_usage > 0:
            total_tokens = current_usage
            message_tokens = max(0, total_tokens - system_tokens)
        else:
            # DeepAgents assembles Skills, middleware prompts, capability
            # manifests, and filtered tool schemas only when a Run is built.
            # The legacy Chat prompt is not a valid Agent baseline, so expose
            # an explicit pending state until the first model request records
            # its effective context.
            measured = False
            system_tokens = 0
            message_tokens = 0
            total_tokens = 0
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
        "measured": measured,
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
