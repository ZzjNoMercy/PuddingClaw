"""Structured source and citation helpers for Agent tool results."""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any


STRUCTURED_TOOL_RESULT_KEY = "puddingclaw_tool_result"
STRUCTURED_TOOL_RESULT_VERSION = 1
_CITATION_MARKER_RE = re.compile(r"\[\^(src_[A-Za-z0-9_-]+)\]")


def _clean_text(value: Any, limit: int | None = None) -> str:
    text = str(value or "").strip()
    if limit is not None and len(text) > limit:
        return text[:limit] + "…"
    return text


def make_source_id(source: dict[str, Any]) -> str:
    """Create a deterministic source id without exposing local paths."""
    identity = "|".join(
        _clean_text(source.get(key))
        for key in ("document_id", "chunk_id", "uri", "title", "page", "quote")
    )
    digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:16]
    return f"src_{digest}"


def normalize_source(source: dict[str, Any], tool_call_id: str = "") -> dict[str, Any]:
    """Normalize a tool-provided source into the public SSE/session schema."""
    metadata = source.get("metadata") if isinstance(source.get("metadata"), dict) else {}
    normalized: dict[str, Any] = {
        "source_id": _clean_text(source.get("source_id")),
        "title": _clean_text(source.get("title")) or "未命名来源",
        "uri": _clean_text(source.get("uri")),
        "document_id": _clean_text(source.get("document_id")),
        "chunk_id": _clean_text(source.get("chunk_id")),
        "source_type": _clean_text(source.get("source_type")) or "knowledge_base",
        "quote": _clean_text(source.get("quote"), 1200),
        "tool_call_id": tool_call_id or _clean_text(source.get("tool_call_id")),
        "metadata": metadata,
    }
    page = source.get("page")
    if page not in (None, ""):
        normalized["page"] = page
    score = source.get("score")
    if score not in (None, ""):
        try:
            normalized["score"] = float(score)
        except (TypeError, ValueError):
            pass
    if not normalized["source_id"]:
        normalized["source_id"] = make_source_id(normalized)
    return normalized


def dedupe_sources(sources: list[dict[str, Any]]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for source in sources:
        normalized = normalize_source(source, source.get("tool_call_id", ""))
        source_id = normalized["source_id"]
        if source_id in seen:
            continue
        seen.add(source_id)
        result.append(normalized)
    return result


def encode_tool_result(answer_context: str, sources: list[dict[str, Any]]) -> str:
    """Encode model-readable context and machine-readable sources in one ToolMessage."""
    payload = {
        STRUCTURED_TOOL_RESULT_KEY: STRUCTURED_TOOL_RESULT_VERSION,
        "answer_context": answer_context,
        "sources": dedupe_sources(sources),
    }
    return json.dumps(payload, ensure_ascii=False)


def parse_tool_result(raw_output: str, tool_call_id: str = "") -> tuple[str, list[dict[str, Any]]]:
    """Return display/model context and sources, preserving legacy plain text tools."""
    try:
        payload = json.loads(raw_output)
    except (TypeError, json.JSONDecodeError):
        return raw_output, []
    if not isinstance(payload, dict) or payload.get(STRUCTURED_TOOL_RESULT_KEY) != STRUCTURED_TOOL_RESULT_VERSION:
        return raw_output, []
    sources = [
        normalize_source(source, tool_call_id)
        for source in payload.get("sources", [])
        if isinstance(source, dict)
    ]
    return _clean_text(payload.get("answer_context")), dedupe_sources(sources)


def format_sources_for_model(answer_context: str, sources: list[dict[str, Any]]) -> str:
    """Keep stable source ids visible to the model after extracting the envelope."""
    if not sources:
        return answer_context
    catalog = []
    for source in sources:
        location = f"，第 {source['page']} 页" if source.get("page") not in (None, "") else ""
        catalog.append(
            f"- {source['source_id']}: {source['title']}{location}\n"
            f"  证据：{source.get('quote') or '见工具返回内容'}"
        )
    return (
        f"{answer_context}\n\n[可引用来源]\n"
        + "\n".join(catalog)
        + "\n\n回答中使用某来源支持具体论述时，请在该论述后添加 [^source_id]，"
          "例如 [^src_abc123]。只能使用上方列出的 source_id。"
    )


def finalize_citations(content: str, sources: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Validate citation markers and assign stable display indexes by first use."""
    source_ids = {source.get("source_id") for source in sources}
    display_indexes: dict[str, int] = {}
    citations: list[dict[str, Any]] = []
    for match in _CITATION_MARKER_RE.finditer(content or ""):
        source_id = match.group(1)
        if source_id not in source_ids:
            continue
        if source_id not in display_indexes:
            display_indexes[source_id] = len(display_indexes) + 1
        citations.append({
            "citation_id": f"cite_{hashlib.sha256(f'{source_id}:{match.start()}'.encode()).hexdigest()[:16]}",
            "source_id": source_id,
            "display_index": display_indexes[source_id],
            "start": match.start(),
            "end": match.end(),
            "status": "verified",
        })
    return citations
