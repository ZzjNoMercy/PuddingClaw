"""Framework-neutral adapters that normalize tool output into citation sources.

This module deliberately sits at the ToolMessage boundary instead of using a
LangChain AgentMiddleware. It can therefore normalize local tools, MCP tools,
Skill scripts, and terminal-based web calls through one stable contract.
"""

from __future__ import annotations

import json
import re
import shlex
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
        is_error: bool = False,
    ) -> AdaptedToolResult:
        # A real browser may contain private, authenticated, or user-entered
        # material. Never promote its output into public citation sources,
        # even if the page happens to contain URLs or a daemon returns an
        # envelope that resembles a retrieval result.
        if tool_name == "browser":
            answer_context, _sources = parse_tool_result(raw_output, tool_call_id)
            return AdaptedToolResult(answer_context or raw_output, [], "browser_private")

        # Provenance describes material actually retrieved by a successful
        # tool call. A rejected/failed network command may still contain a URL
        # in its input, but that URL was not observed and must not become a
        # source merely because the adapter can parse the command text.
        if is_error:
            answer_context, _sources = parse_tool_result(raw_output, tool_call_id)
            return AdaptedToolResult(answer_context or raw_output, [], "error")

        # 1. Explicit PuddingClaw envelope: highest-trust contract.
        answer_context, sources = parse_tool_result(raw_output, tool_call_id)
        if sources or answer_context != raw_output:
            return AdaptedToolResult(answer_context, sources, "standard")

        # 2. A normal fetch_url page is one authoritative source. Search result
        # pages are different: the page is only a container and its outbound
        # result links are the actual evidence sources.
        requested_url = self._url_from_tool_input(tool_input)
        if tool_name == "fetch_url" and requested_url:
            if self._looks_like_fetch_failure(raw_output):
                return AdaptedToolResult(raw_output, [], "fetch_url_rejected")
            payload = self._parse_json(raw_output)
            if payload is not None:
                json_sources = self._sources_from_json(payload, tool_call_id)
                if json_sources:
                    return AdaptedToolResult(raw_output, json_sources, "fetch_url_json")
            if self._is_search_results_url(requested_url):
                search_sources = self._sources_from_search_page(
                    raw_output, requested_url, tool_call_id
                )
                return AdaptedToolResult(
                    raw_output, search_sources, "fetch_url_search_results"
                )
            title = self._title_from_markdown(raw_output)
            source = normalize_source({
                "title": title or self._host_title(requested_url),
                "uri": requested_url,
                "document_id": requested_url,
                "chunk_id": "fetched-page",
                "source_type": "web",
                "quote": self._plain_preview(raw_output),
                "metadata": {"adapter": "fetch_url"},
            }, tool_call_id)
            return AdaptedToolResult(raw_output, [source], "fetch_url")

        # Implicit JSON/Markdown extraction is only valid for tools that
        # actually retrieve external material. Reading SKILL.md, source code,
        # prompts, or config files must never turn documented example URLs into
        # retrieval sources.
        if not self.supports_implicit_sources(tool_name, tool_input):
            return AdaptedToolResult(raw_output, [], "plain_text")

        # 3. Common JSON search/news schemas (Tavily, AI HOT, generic APIs).
        payload = self._parse_json(raw_output)
        if payload is not None:
            json_sources = self._sources_from_json(payload, tool_call_id)
            if json_sources:
                return AdaptedToolResult(raw_output, json_sources, "common_json")

        # 4. Markdown/bare-link fallback. These are real URLs returned by the
        # tool, but remain retrieval-only until the model explicitly cites them.
        markdown_sources = self._sources_from_markdown(raw_output, tool_call_id)
        if markdown_sources:
            return AdaptedToolResult(raw_output, markdown_sources, "markdown_links")

        # A successful observable network command still has authoritative
        # provenance even when its response is plain text or a custom schema:
        # the exact requested endpoint. This keeps arbitrary Skills compatible
        # without trusting them to implement a PuddingClaw-specific envelope.
        request_sources = self._sources_from_network_request(
            tool_input,
            raw_output,
            tool_call_id,
        )
        if request_sources:
            return AdaptedToolResult(raw_output, request_sources, "network_request")

        return AdaptedToolResult(raw_output, [], "plain_text")

    @staticmethod
    def supports_implicit_sources(tool_name: str, tool_input: str = "") -> bool:
        """Whether unstructured output from this tool may represent retrieved sources."""
        name = (tool_name or "").lower().replace("-", "_")
        if name in {"read_file", "write_file", "create_skill_version"}:
            return False
        if name == "execute_skill":
            # execute_skill returns script stdout, not SKILL.md instructions;
            # URLs in that stdout are retrieved results and may be normalized.
            return True
        if name in {"execute", "terminal", "python_repl"}:
            command = ToolResultAdapter._command_from_tool_input(tool_input).lower()
            return bool(
                re.search(r"https?://", command)
                and re.search(r"\b(curl|wget|httpie|requests\.|urllib|fetch)\b", command)
            )
        source_tokens = (
            "search", "fetch", "browse", "browser", "research", "retrieve",
            "lookup", "tavily", "news", "knowledge", "url", "web",
        )
        return any(token in name for token in source_tokens)

    def _sources_from_network_request(
        self,
        tool_input: str,
        raw_output: str,
        tool_call_id: str,
    ) -> list[dict[str, Any]]:
        command = self._command_from_tool_input(tool_input)
        if not command:
            return []
        lowered = command.lower()
        if not re.search(r"\b(curl|wget|httpie|requests\.|urllib|fetch)\b", lowered):
            return []

        urls: list[str] = []
        try:
            tokens = shlex.split(command, posix=True)
        except ValueError:
            tokens = []
        for token in tokens:
            clean = token.rstrip(".,;，。；)}]")
            if self._is_web_url(clean) and clean not in urls:
                urls.append(clean)

        if not urls:
            for match in re.finditer(r"https?://[^\s<>'\"}\])]+", command):
                # Identifiable User-Agent project URLs are metadata, not
                # endpoints contacted by the command.
                if command[max(0, match.start() - 2):match.start()] == "(+":
                    continue
                clean = match.group(0).rstrip(".,;，。；)")
                if clean not in urls:
                    urls.append(clean)

        preview = self._plain_preview(raw_output)
        return self._dedupe([
            normalize_source({
                "title": self._host_title(url),
                "uri": url,
                "document_id": url,
                "chunk_id": "network-response",
                "source_type": "web",
                "quote": preview,
                "metadata": {"adapter": "network_request"},
            }, tool_call_id)
            for url in urls[:_MAX_SOURCES]
        ])

    @staticmethod
    def _command_from_tool_input(tool_input: str) -> str:
        text = str(tool_input or "").strip()
        if not text:
            return ""
        try:
            payload = json.loads(text)
        except (json.JSONDecodeError, TypeError):
            return text
        if isinstance(payload, dict):
            return str(payload.get("command") or "")
        return text

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

            # Aggregators such as AI HOT return both their stable canonical
            # permalink and a third-party article URL. Prefer the permalink:
            # it is the exact item the tool retrieved and remains traceable to
            # the API response used by this Run.
            url = self._first(
                value,
                "permalink",
                "url",
                "sourceUrl",
                "source_url",
                "link",
                "href",
            )
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

    def _sources_from_search_page(
        self, text: str, requested_url: str, tool_call_id: str
    ) -> list[dict[str, Any]]:
        """Extract actual outbound results instead of citing the search shell."""
        search_host = (urlparse(requested_url).hostname or "").lower()
        sources: list[dict[str, Any]] = []
        seen_urls: set[str] = set()
        for raw_title, raw_url in _MARKDOWN_LINK_RE.findall(text or ""):
            title = self._plain_text(raw_title)
            url = raw_url.rstrip(".,;，。；")
            host = (urlparse(url).hostname or "").lower()
            if (
                not title
                or len(title) < 4
                or not host
                or host == search_host
                or host.endswith(f".{search_host}")
                or url in seen_urls
            ):
                continue
            seen_urls.add(url)
            sources.append(normalize_source({
                "title": title,
                "uri": url,
                "document_id": url,
                "chunk_id": "search-result",
                "source_type": "web",
                "quote": self._context_around(text, url),
                "metadata": {
                    "adapter": "fetch_url_search_result",
                    "search_page": requested_url,
                },
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
        title = self._plain_text(match.group(1)) if match else ""
        if not title or title.startswith(("[](", "[ ](")):
            return ""
        return title

    @staticmethod
    def _is_search_results_url(url: str) -> bool:
        parsed = urlparse(url)
        host = (parsed.hostname or "").lower()
        path = parsed.path.lower()
        if host.endswith("google.com") and path == "/search":
            return True
        if host.endswith("bing.com") and path in {"/search", "/news/search"}:
            return True
        if host.endswith("baidu.com") and path in {"/s", "/ns"}:
            return True
        return False

    @staticmethod
    def _looks_like_fetch_failure(text: str) -> bool:
        normalized = re.sub(r"\s+", " ", text or "").strip().lower()
        failure_markers = (
            "request timed out",
            "fetch error:",
            "please click here if you are not redirected",
            "if you're having trouble accessing google search",
            "网络不给力",
            "请稍后重试",
            "enablejs",
        )
        if any(marker in normalized for marker in failure_markers):
            return True
        # Typical UTF-8 decoded as Latin-1/Windows-1252. Such a page is not
        # trustworthy evidence even before the fetch tool's encoding repair.
        mojibake_markers = ("ç½", "è¯", "å", "é¡", "ï¼")
        return sum(marker in normalized for marker in mojibake_markers) >= 2

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
        return self._plain_text(
            text[max(0, index - 240):index]
            + text[index + len(needle):index + len(needle) + 240]
        )[:600]

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
