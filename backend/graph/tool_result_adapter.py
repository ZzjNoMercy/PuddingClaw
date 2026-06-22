"""Framework-neutral adapters that normalize tool output into citation sources.

This module deliberately sits at the ToolMessage boundary instead of using a
LangChain AgentMiddleware. It can therefore normalize local tools, MCP tools,
Skill scripts, and terminal-based web calls through one stable contract.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

from graph.citations import normalize_source, parse_tool_result


_MARKDOWN_LINK_RE = re.compile(r"(?<!!)\[([^\]\n]{1,240})\]\((https?://[^\s)]+)\)")
_BARE_URL_RE = re.compile(r"(?<![\w\"'=])(https?://[^\s<>\]\[)\"']+)")
_MAX_SOURCES = 30


@dataclass(frozen=True)
class AdaptedToolResult:
    answer_context: str
    sources: list[dict[str, Any]]
    adapter: str


class ToolResultAdapter:
    """Apply deterministic source adapters in trust order."""

    def adapt(
        self,
        raw_output: str,
        *,
        tool_name: str = "",
        tool_input: str = "",
        tool_call_id: str = "",
    ) -> AdaptedToolResult:
        # 1. Explicit PuddingClaw envelope: highest-trust contract.
        answer_context, sources = parse_tool_result(raw_output, tool_call_id)
        if sources or answer_context != raw_output:
            return AdaptedToolResult(answer_context, sources, "standard")

        # 2. Common JSON search/news schemas (Tavily, AI HOT, generic APIs).
        payload = self._parse_json(raw_output)
        if payload is not None:
            json_sources = self._sources_from_json(payload, tool_call_id)
            if json_sources:
                return AdaptedToolResult(raw_output, json_sources, "common_json")

        # 3. fetch_url has one authoritative requested page. Do not mistake all
        # links in the returned page body for evidence sources.
        requested_url = self._url_from_tool_input(tool_input)
        if tool_name == "fetch_url" and requested_url:
            source = normalize_source({
                "title": self._title_from_markdown(raw_output) or self._host_title(requested_url),
                "uri": requested_url,
                "document_id": requested_url,
                "chunk_id": "fetched-page",
                "source_type": "web",
                "quote": self._plain_preview(raw_output),
                "metadata": {"adapter": "fetch_url"},
            }, tool_call_id)
            return AdaptedToolResult(raw_output, [source], "fetch_url")

        # 4. Markdown/bare-link fallback. These are real URLs returned by the
        # tool, but remain retrieval-only until the model explicitly cites them.
        markdown_sources = self._sources_from_markdown(raw_output, tool_call_id)
        if markdown_sources:
            return AdaptedToolResult(raw_output, markdown_sources, "markdown_links")

        return AdaptedToolResult(raw_output, [], "plain_text")

    @staticmethod
    def _parse_json(raw_output: str) -> Any | None:
        text = (raw_output or "").strip()
        if not text:
            return None
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            # Terminal tools sometimes prefix a command label before JSON.
            starts = [index for index in (text.find("{"), text.find("[")) if index >= 0]
            if not starts:
                return None
            start = min(starts)
            for end_char in ("}", "]"):
                end = text.rfind(end_char)
                if end <= start:
                    continue
                try:
                    return json.loads(text[start:end + 1])
                except json.JSONDecodeError:
                    continue
        return None

    def _sources_from_json(self, payload: Any, tool_call_id: str) -> list[dict[str, Any]]:
        candidates: list[dict[str, Any]] = []

        def visit(value: Any) -> None:
            if len(candidates) >= _MAX_SOURCES:
                return
            if isinstance(value, list):
                for item in value:
                    visit(item)
                return
            if not isinstance(value, dict):
                return

            url = self._first(value, "url", "sourceUrl", "source_url", "link", "href")
            title = self._first(value, "title", "name", "leadTitle", "headline")
            if self._is_web_url(url) and title:
                quote = self._first(
                    value, "snippet", "content", "summary", "description",
                    "leadParagraph", "text",
                )
                metadata = {
                    "adapter": "common_json",
                    "source_name": self._first(value, "source", "sourceName", "publisher"),
                    "published_at": self._first(
                        value, "publishedAt", "published_at", "date", "createdAt"
                    ),
                    "category": self._first(value, "category", "type"),
                }
                candidates.append(normalize_source({
                    "title": title,
                    "uri": url,
                    "document_id": self._first(value, "id", "document_id") or url,
                    "chunk_id": self._first(value, "chunk_id") or "web-result",
                    "source_type": "web",
                    "quote": quote,
                    "score": self._first(value, "score", "relevance_score"),
                    "metadata": {key: item for key, item in metadata.items() if item not in (None, "")},
                }, tool_call_id))

            # AI HOT daily responses nest items under sections/flashes; generic
            # recursive traversal also covers Tavily results and MCP web tools.
            for nested in value.values():
                if isinstance(nested, (dict, list)):
                    visit(nested)

        visit(payload)
        return self._dedupe(candidates)

    def _sources_from_markdown(self, text: str, tool_call_id: str) -> list[dict[str, Any]]:
        sources: list[dict[str, Any]] = []
        seen_urls: set[str] = set()
        for title, url in _MARKDOWN_LINK_RE.findall(text or ""):
            clean_url = url.rstrip(".,;，。；")
            if clean_url in seen_urls:
                continue
            seen_urls.add(clean_url)
            sources.append(normalize_source({
                "title": self._plain_text(title) or self._host_title(clean_url),
                "uri": clean_url,
                "document_id": clean_url,
                "chunk_id": "markdown-link",
                "source_type": "web",
                "quote": self._context_around(text, clean_url),
                "metadata": {"adapter": "markdown_links"},
            }, tool_call_id))
            if len(sources) >= _MAX_SOURCES:
                return sources

        for match in _BARE_URL_RE.finditer(text or ""):
            clean_url = match.group(1).rstrip(".,;，。；")
            if clean_url in seen_urls:
                continue
            seen_urls.add(clean_url)
            context = self._context_around(text, clean_url)
            sources.append(normalize_source({
                "title": self._nearest_markdown_title(text, match.start()) or self._host_title(clean_url),
                "uri": clean_url,
                "document_id": clean_url,
                "chunk_id": "bare-url",
                "source_type": "web",
                "quote": context,
                "metadata": {"adapter": "markdown_links"},
            }, tool_call_id))
            if len(sources) >= _MAX_SOURCES:
                break
        return self._dedupe(sources)

    @staticmethod
    def _first(value: dict[str, Any], *keys: str) -> Any:
        for key in keys:
            item = value.get(key)
            if item not in (None, ""):
                return item
        return None

    @staticmethod
    def _is_web_url(value: Any) -> bool:
        return isinstance(value, str) and value.startswith(("http://", "https://"))

    @staticmethod
    def _url_from_tool_input(tool_input: str) -> str:
        match = re.search(r"https?://[^\s'\"},]+", tool_input or "")
        return match.group(0).rstrip("'\"},") if match else ""

    @staticmethod
    def _host_title(url: str) -> str:
        return urlparse(url).netloc or url

    @staticmethod
    def _plain_text(text: str) -> str:
        return re.sub(r"[*_`#]", "", str(text or "")).strip()

    def _plain_preview(self, text: str) -> str:
        return self._plain_text(re.sub(r"\s+", " ", text or ""))[:600]

    def _title_from_markdown(self, text: str) -> str:
        match = re.search(r"^#{1,3}\s+(.+)$", text or "", re.MULTILINE)
        return self._plain_text(match.group(1)) if match else ""

    def _nearest_markdown_title(self, text: str, position: int) -> str:
        before = (text or "")[:position]
        lines = [line.strip() for line in before.splitlines() if line.strip()]
        for line in reversed(lines[-4:]):
            cleaned = self._plain_text(re.sub(r"^\d+[.)]\s*", "", line))
            if cleaned and not cleaned.startswith("http") and len(cleaned) <= 240:
                return cleaned
        return ""

    def _context_around(self, text: str, needle: str) -> str:
        index = (text or "").find(needle)
        if index < 0:
            return ""
        return self._plain_text((text[max(0, index - 240):index] + text[index + len(needle):index + len(needle) + 240]))[:600]

    @staticmethod
    def _dedupe(sources: list[dict[str, Any]]) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        seen: set[str] = set()
        for source in sources:
            key = source.get("uri") or source.get("source_id")
            if not key or key in seen:
                continue
            seen.add(key)
            result.append(source)
        return result


tool_result_adapter = ToolResultAdapter()
