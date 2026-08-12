"""Structured source and citation helpers for Agent tool results."""

from __future__ import annotations

import hashlib
import html
import json
import re
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit


STRUCTURED_TOOL_RESULT_KEY = "puddingclaw_tool_result"
STRUCTURED_TOOL_RESULT_VERSION = 1
_CITATION_MARKER_RE = re.compile(r"\[\^(src_[A-Za-z0-9_-]+)\]")
_FOOTNOTE_DEFINITION_RE = re.compile(
    r"^[ \t]*\[\^[^\]\n]+\]:[^\n]*(?:\n(?:(?: {2,}|\t)[^\n]*))*\n?",
    re.MULTILINE,
)
_UNSUPPORTED_FOOTNOTE_REFERENCE_RE = re.compile(
    r"\[\^(?!src_[A-Za-z0-9_-]+\])[^\]\n]+\]"
)
_GENERATED_CITATIONS_RE = re.compile(
    r"\n*<!-- puddingclaw-citations:start -->.*?"
    r"<!-- puddingclaw-citations:end -->\n*",
    re.DOTALL,
)
_GENERATED_CITATION_MAP_RE = re.compile(
    r"<!-- puddingclaw-citation-map: (\{.*?\}) -->"
)
_NUMERIC_FOOTNOTE_DEFINITION_RE = re.compile(
    r"^[ \t]*\[\^(\d+)\]:[ \t]*(.*?)[ \t]*$",
    re.MULTILINE,
)
_GENERATED_HTML_CITATIONS_RE = re.compile(
    r"\s*<!-- puddingclaw-citations:start -->.*?"
    r"<!-- puddingclaw-citations:end -->\s*",
    re.DOTALL,
)
_GENERATED_HTML_MARKER_RE = re.compile(
    r'<sup\s+class="citation"\s+data-source-id="(src_[A-Za-z0-9_-]+)"[^>]*>'
    r'.*?</sup>',
    re.DOTALL,
)


def sanitize_citation_markdown(content: str) -> str:
    """Allow only structured ``src_*`` citation markers in assistant Markdown.

    SQL ``generation_id`` values and other runtime identifiers are execution
    handles, not citation sources.  Models may still show those ids as normal
    code or table cells, but GFM footnote syntax must not turn them into a
    synthetic Footnotes section with localhost back-links.
    """

    sanitized = _FOOTNOTE_DEFINITION_RE.sub("", str(content or ""))
    sanitized = _UNSUPPORTED_FOOTNOTE_REFERENCE_RE.sub("", sanitized)
    return re.sub(r"\n{3,}", "\n\n", sanitized).strip()


def _clean_text(value: Any, limit: int | None = None) -> str:
    text = str(value or "").strip()
    if limit is not None and len(text) > limit:
        return text[:limit] + "…"
    return text


def make_source_id(source: dict[str, Any]) -> str:
    """Create a deterministic source id without exposing local paths."""
    uri = _clean_text(source.get("uri"))
    source_type = _clean_text(source.get("source_type"))
    if source_type in {"web", "x"} and urlsplit(uri).scheme in {"http", "https"}:
        # Search providers can describe the same URL with different numeric
        # citation labels or excerpts across calls. Its identity must remain
        # stable so the model cannot see duplicate source ids for one page.
        identity = f"{source_type}|{uri}"
    else:
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


def _find_structured_payload(raw_output: str) -> dict[str, Any] | None:
    """Find a standard tool-result envelope, even when wrapped by tool logs.

    Some tool runners prepend human-readable execution text before the actual
    script output, for example:

        技能：aihot
        执行结果：
        [scripts/aihot_query.py] {"puddingclaw_tool_result": 1, ...}

    The citation protocol should treat the JSON envelope as authoritative even
    when it is embedded in such wrappers. We deliberately only accept objects
    carrying STRUCTURED_TOOL_RESULT_KEY, so example JSON in docs/read_file does
    not become a source.
    """
    text = str(raw_output or "").strip()
    if not text:
        return None
    decoder = json.JSONDecoder()
    for match in re.finditer(r"\{", text):
        try:
            payload, _end = decoder.raw_decode(text[match.start():])
        except json.JSONDecodeError:
            continue
        if (
            isinstance(payload, dict)
            and payload.get(STRUCTURED_TOOL_RESULT_KEY) == STRUCTURED_TOOL_RESULT_VERSION
        ):
            return payload
    return None


def parse_tool_result(raw_output: str, tool_call_id: str = "") -> tuple[str, list[dict[str, Any]]]:
    """Return display/model context and sources, preserving legacy plain text tools."""
    try:
        payload = json.loads(raw_output)
    except (TypeError, json.JSONDecodeError):
        payload = _find_structured_payload(raw_output)
    if not isinstance(payload, dict) or payload.get(STRUCTURED_TOOL_RESULT_KEY) != STRUCTURED_TOOL_RESULT_VERSION:
        return raw_output, []
    sources = [
        normalize_source(source, tool_call_id)
        for source in payload.get("sources", [])
        if isinstance(source, dict)
    ]
    return _clean_text(payload.get("answer_context")), dedupe_sources(sources)


def format_sources_for_model(
    answer_context: str,
    sources: list[dict[str, Any]],
    *,
    include_evidence: bool = True,
) -> str:
    """Keep stable source ids visible to the model after extracting the envelope.

    The catalog serves two distinct needs:

    1. Identity continuity — the ``source_id`` ↔ title mapping lets the model
       keep citing the same ids across turns. This is cheap and always needed.
    2. Evidence text — the per-source quote lets the model verify what it is
       citing *while composing* a new answer.

    Historical projections only need the first: the reply that cited these
    sources is already written and frozen, and the raw evidence remains
    recoverable through read_evidence. Pass ``include_evidence=False`` on the
    historical path so old search results are not re-billed at full price on
    every new Run. In-run callers keep the default so citation accuracy is
    preserved while the model is actually writing.
    """
    if not sources:
        return answer_context
    catalog = []
    for source in sources:
        location = f"，第 {source['page']} 页" if source.get("page") not in (None, "") else ""
        uri = _clean_text(source.get("uri"))
        link = f"\n  链接：{uri}" if urlsplit(uri).scheme in {"http", "https"} else ""
        if include_evidence:
            catalog.append(
                f"- {source['source_id']}: {source['title']}{location}\n"
                f"  证据：{source.get('quote') or '见工具返回内容'}"
                f"{link}"
            )
        else:
            catalog.append(f"- {source['source_id']}: {source['title']}{location}{link}")
    omitted_note = ""
    if not include_evidence:
        # Tell the model the omission is deliberate and recoverable, otherwise
        # it may treat the missing quotes as lost data and re-run the search.
        omitted_note = (
            "\n(证据摘录已在历史投影中省略；原文保留在会话证据存储中，"
            "可按需用 read_evidence 读取。)"
        )
    return (
        f"{answer_context}\n\n[可引用来源]\n"
        + "\n".join(catalog)
        + omitted_note
        + "\n\n回答中使用某来源支持具体论述时，请在该论述后添加 [^source_id]，"
          "例如 [^src_abc123]。只能使用上方列出的 source_id。"
    )


def finalize_citations(content: str, sources: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Validate citation markers and assign stable display indexes by first use."""
    content = sanitize_citation_markdown(content)
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


def resolve_message_citations(
    content: str,
    turn_sources: list[dict[str, Any]],
    session_sources: list[dict[str, Any]] | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Resolve one answer against new sources plus reusable session history.

    Every source discovered in the current turn remains visible as a retrieval
    result. Historical sources are copied onto the new message only when its
    final text actually cites them, so source reuse works without duplicating
    the complete session catalog into every answer.
    """

    current = dedupe_sources(turn_sources)
    available = dedupe_sources(current + list(session_sources or []))
    citations = finalize_citations(content, available)
    cited_ids = {citation["source_id"] for citation in citations}
    current_ids = {source["source_id"] for source in current}
    reused = [
        source
        for source in available
        if source["source_id"] in cited_ids and source["source_id"] not in current_ids
    ]
    return dedupe_sources(current + reused), citations


def _source_link_target(source: dict[str, Any]) -> str:
    """Return a portable web URL or an exact local ``file://`` target."""

    uri = _clean_text(source.get("uri"))
    if urlsplit(uri).scheme in {"http", "https"}:
        return uri

    metadata = source.get("metadata") if isinstance(source.get("metadata"), dict) else {}
    local_candidates = []
    if str(source.get("source_type") or "") == "knowledge_image":
        local_candidates.append(metadata.get("linked_markdown"))
    local_candidates.extend((metadata.get("file_path"), metadata.get("linked_markdown")))
    if uri.startswith("/") and not uri.startswith(("/knowledge/", "/workspace/", "/scratch/")):
        local_candidates.append(uri)
    for candidate in local_candidates:
        raw = _clean_text(candidate)
        if not raw:
            continue
        try:
            return Path(raw).expanduser().resolve(strict=False).as_uri()
        except (OSError, ValueError):
            continue

    # Keep non-HTTP schemes such as wiki:// or database:// usable when the
    # source does not have a host-file counterpart.
    if urlsplit(uri).scheme:
        return uri
    return ""


def _source_locator(source: dict[str, Any]) -> str:
    metadata = source.get("metadata") if isinstance(source.get("metadata"), dict) else {}
    parts: list[str] = []
    if source.get("page") not in (None, ""):
        parts.append(f"第 {source['page']} 页")
    heading = _clean_text(metadata.get("chunk_title") or metadata.get("header_path"))
    if heading and heading != "/":
        parts.append(f"章节：{heading.strip('/')}")
    chunk_id = _clean_text(source.get("chunk_id"))
    if chunk_id and chunk_id not in {"tavily-result", "web-result"}:
        parts.append(f"片段：{chunk_id}")
    return "；".join(parts)


def _markdown_label(value: str) -> str:
    return value.replace("\\", "\\\\").replace("[", "\\[").replace("]", "\\]")


def _markdown_source_definition(source: dict[str, Any], source_id: str) -> str:
    title = _markdown_label(_clean_text(source.get("title")) or source_id)
    target = _source_link_target(source)
    label = f"[{title}](<{target.replace('>', '%3E')}>)" if target else title
    locator = _source_locator(source)
    return f"{label}{f' — {locator}' if locator else ''}"


def materialize_artifact_citations(
    content: str,
    sources: list[dict[str, Any]],
    *,
    file_path: str,
) -> tuple[str, dict[str, Any]]:
    """Make ``src_*`` markers self-contained in Markdown or HTML artifacts.

    Chat keeps structured source objects for interactive citation cards. Files
    cannot depend on that UI state, so this function derives stable footnote
    definitions (Markdown) or numbered anchors (HTML) from the same source
    records at the write boundary. The generated block is tagged and replaced
    idempotently on later writes or patches.
    """

    suffix = Path(str(file_path or "")).suffix.lower()
    if suffix not in {".md", ".markdown", ".html", ".htm"}:
        return content, {"materialized": 0, "unresolved_source_ids": []}

    source_map = {
        str(source.get("source_id") or ""): source
        for source in dedupe_sources(sources)
        if str(source.get("source_id") or "")
    }
    previous_markdown_map: dict[str, str] = {}
    if suffix in {".html", ".htm"}:
        cleaned = _GENERATED_HTML_CITATIONS_RE.sub("", str(content or ""))
        cleaned = _GENERATED_HTML_MARKER_RE.sub(lambda match: f"[^{match.group(1)}]", cleaned)
    else:
        raw_content = str(content or "")
        generated_block = _GENERATED_CITATIONS_RE.search(raw_content)
        if generated_block:
            map_match = _GENERATED_CITATION_MAP_RE.search(generated_block.group(0))
            if map_match:
                try:
                    parsed_map = json.loads(map_match.group(1))
                    if isinstance(parsed_map, dict):
                        previous_markdown_map = {
                            str(index): str(source_id)
                            for index, source_id in parsed_map.items()
                            if str(index).isdigit() and _CITATION_MARKER_RE.fullmatch(f"[^{source_id}]")
                        }
                except json.JSONDecodeError:
                    previous_markdown_map = {}
        if previous_markdown_map:
            # Migrate the short-lived comment-map format without leaving any
            # machine metadata visible in Markdown renderers that escape HTML.
            cleaned = _GENERATED_CITATIONS_RE.sub("\n", raw_content)
            for index, source_id in previous_markdown_map.items():
                cleaned = re.sub(
                    rf"\[\^{re.escape(index)}\]",
                    f"[^{source_id}]",
                    cleaned,
                )
        else:
            cleaned = raw_content
    ordered_ids = list(dict.fromkeys(match.group(1) for match in _CITATION_MARKER_RE.finditer(cleaned)))
    if not ordered_ids:
        return cleaned, {"materialized": 0, "unresolved_source_ids": []}

    unresolved = [source_id for source_id in ordered_ids if source_id not in source_map]
    if suffix in {".md", ".markdown"}:
        existing_definitions = {
            int(match.group(1)): match.group(2).strip()
            for match in _NUMERIC_FOOTNOTE_DEFINITION_RE.finditer(cleaned)
        }
        definition_indexes = {
            definition: index for index, definition in existing_definitions.items()
        }
        next_index = max(existing_definitions, default=0) + 1
        indexes: dict[str, int] = {}
        new_definitions: list[tuple[int, str]] = []
        for source_id in ordered_ids:
            source = source_map.get(source_id)
            definition = (
                _markdown_source_definition(source, source_id)
                if source is not None
                else f"⚠️ 未解析来源 `{source_id}`"
            )
            index = definition_indexes.get(definition)
            if index is None:
                index = next_index
                next_index += 1
                definition_indexes[definition] = index
                new_definitions.append((index, definition))
            indexes[source_id] = index
        rendered_body = _CITATION_MARKER_RE.sub(
            lambda match: f"[^{indexes[match.group(1)]}]",
            cleaned,
        )
        if new_definitions:
            rendered_body = (
                rendered_body.rstrip()
                + "\n\n"
                + "\n".join(
                    f"[^{index}]: {definition}"
                    for index, definition in new_definitions
                )
                + "\n"
            )
        return rendered_body, {
            "materialized": len(ordered_ids) - len(unresolved),
            "unresolved_source_ids": unresolved,
        }

    indexes = {source_id: index for index, source_id in enumerate(ordered_ids, start=1)}
    occurrence_counts: dict[str, int] = {}

    def replace_marker(match: re.Match[str]) -> str:
        source_id = match.group(1)
        occurrence_counts[source_id] = occurrence_counts.get(source_id, 0) + 1
        index = indexes[source_id]
        return (
            f'<sup class="citation" data-source-id="{html.escape(source_id)}" '
            f'id="cite-ref-{html.escape(source_id)}-{occurrence_counts[source_id]}">'
            f'<a href="#cite-source-{html.escape(source_id)}" aria-label="引用 {index}">[{index}]</a>'
            "</sup>"
        )

    rendered = _CITATION_MARKER_RE.sub(replace_marker, cleaned)
    items: list[str] = []
    for source_id in ordered_ids:
        source = source_map.get(source_id)
        index = indexes[source_id]
        if source is None:
            description = f"⚠️ 未解析来源 {html.escape(source_id)}"
        else:
            title = html.escape(_clean_text(source.get("title")) or source_id)
            target = _source_link_target(source)
            description = (
                f'<a href="{html.escape(target, quote=True)}">{title}</a>' if target else title
            )
            locator = _source_locator(source)
            if locator:
                description += f" — {html.escape(locator)}"
        items.append(
            f'<li id="cite-source-{html.escape(source_id)}" value="{index}">{description} '
            f'<a href="#cite-ref-{html.escape(source_id)}-1" aria-label="返回正文">↩</a></li>'
        )
    block = (
        '<!-- puddingclaw-citations:start -->\n'
        '<section class="citation-references" aria-label="引用">\n'
        '<h2>引用</h2>\n<ol>\n'
        + "\n".join(items)
        + "\n</ol>\n</section>\n"
        '<!-- puddingclaw-citations:end -->'
    )
    body_close = re.search(r"</body\s*>", rendered, re.IGNORECASE)
    if body_close:
        rendered = rendered[:body_close.start()] + block + "\n" + rendered[body_close.start():]
    else:
        rendered = rendered.rstrip() + "\n" + block + "\n"
    return rendered, {
        "materialized": len(ordered_ids) - len(unresolved),
        "unresolved_source_ids": unresolved,
    }
