"""Adapter base classes and provider-response normalization."""

from __future__ import annotations

import re
from abc import ABC, abstractmethod
from typing import Any
from urllib.parse import unquote, urlsplit

from web_search.models import AdapterResponse, SearchRequest, SearchResult

_MARKDOWN_URL_RE = re.compile(r"\[([^\]]+)\]\((https?://[^)\s]+)\)")
_BARE_URL_RE = re.compile(r"https?://[^\s<>\)\]\}\[`'\"，。；：、（）【】]+")
_NUMERIC_CITATION_TITLE_RE = re.compile(r"^\[?\d+\]?$")


class WebSearchAdapter(ABC):
    id: str

    @abstractmethod
    def search(self, request: SearchRequest, credential: str, options: dict[str, Any]) -> AdapterResponse:
        raise NotImplementedError

    def probe(self, credential: str, options: dict[str, Any]) -> AdapterResponse:
        return self.search(
            SearchRequest(
                query="Find the official provider API documentation homepage and cite its URL.",
                scope="global",
                source="web",
                provider=self.id,
                max_results=1,
            ),
            credential,
            options,
        )


def response_payload(response: Any) -> dict[str, Any]:
    if isinstance(response, dict):
        return response
    dumper = getattr(response, "model_dump", None)
    if callable(dumper):
        value = dumper(exclude_none=True)
        return value if isinstance(value, dict) else {}
    return {}


def response_text(response: Any, payload: dict[str, Any]) -> str:
    value = str(getattr(response, "output_text", "") or "").strip()
    if value:
        return value
    texts: list[str] = []

    def visit(node: Any) -> None:
        if isinstance(node, dict):
            if node.get("type") in {"output_text", "text"} and isinstance(node.get("text"), str):
                texts.append(node["text"])
            for child in node.values():
                if isinstance(child, (dict, list)):
                    visit(child)
        elif isinstance(node, list):
            for child in node:
                visit(child)

    visit(payload.get("output", []))
    return "\n".join(dict.fromkeys(item.strip() for item in texts if item.strip()))


def normalized_response_sources(
    response: Any,
    payload: dict[str, Any],
    text: str,
    *,
    provider: str,
    query: str,
) -> list[SearchResult]:
    candidates: list[dict[str, Any]] = []

    def append_citations(raw_citations: Any) -> None:
        if isinstance(raw_citations, list):
            for item in raw_citations:
                if isinstance(item, str):
                    candidates.append({"url": item})
                elif isinstance(item, dict):
                    candidates.append(item)
                else:
                    dumped = getattr(item, "model_dump", None)
                    if callable(dumped):
                        candidates.append(dumped(exclude_none=True))

    append_citations(getattr(response, "citations", None))
    # xAI Responses returns the complete encountered-source list at the top
    # level. The OpenAI SDK may retain it only in model_dump(), so inspect both
    # representations instead of relying on a generated SDK attribute.
    append_citations(payload.get("citations"))

    def visit(node: Any) -> None:
        if isinstance(node, dict):
            node_type = str(node.get("type") or "").lower()
            url = str(node.get("url") or node.get("uri") or "").strip()
            if url.startswith(("http://", "https://")) and (
                "citation" in node_type or "source" in node_type or node.get("title") or node.get("snippet")
            ):
                candidates.append(node)
            for child in node.values():
                if isinstance(child, (dict, list)):
                    visit(child)
        elif isinstance(node, list):
            for child in node:
                visit(child)

    visit(payload)
    for title, url in _MARKDOWN_URL_RE.findall(text):
        candidates.append({"title": title, "url": url})
    for url in _BARE_URL_RE.findall(text):
        candidates.append({"url": url.rstrip(".,;，。；")})

    def derived_title(url: str, host: str) -> str:
        parsed = urlsplit(url)
        parts = [part for part in parsed.path.split("/") if part]
        if host in {"x.com", "twitter.com"} or host.endswith(".x.com"):
            if parts and parts[0].lower() != "i":
                handle = parts[0]
                return f"@{handle} 的 X 帖子" if "status" in parts[1:] else f"@{handle} 的 X 主页"
            return "X 帖子"
        slug = unquote(parts[-1]).rsplit(".", 1)[0] if parts else ""
        label = re.sub(r"[-_]+", " ", slug).strip()
        return f"{label} · {host}" if label else host or "网页来源"

    def contextual_quote(url: str) -> str:
        position = text.find(url)
        if position < 0:
            return ""
        prefix = text[max(0, position - 420):position]
        # Prefer the current paragraph/list item and remove markdown decoration.
        segment = re.split(r"\n\s*\n|\n(?=\s*(?:[-*#]|\d+[.)]))", prefix)[-1]
        segment = re.sub(r"\[\[?\d+\]?\]\([^)]*\)", "", segment)
        segment = re.sub(r"[*_`#]+", "", segment).strip(" \n-:：")
        return segment[-360:].strip()

    results: list[SearchResult] = []
    seen: set[str] = set()
    for item in candidates:
        url = str(item.get("url") or item.get("uri") or "").strip().rstrip(".,;，。；")
        if not url.startswith(("http://", "https://")) or url in seen:
            continue
        try:
            parsed = urlsplit(url)
            host = (parsed.hostname or "").removeprefix("www.")
        except ValueError:
            # Provider text can contain two adjacent markdown URLs separated
            # by Unicode punctuation. A malformed citation must not fail the
            # entire search response.
            continue
        if not host:
            continue
        seen.add(url)
        source_type = "x" if host in {"x.com", "twitter.com"} or host.endswith(".x.com") else "web"
        raw_title = str(item.get("title") or item.get("name") or "").strip()
        title = (
            derived_title(url, host)
            if not raw_title
            or _NUMERIC_CITATION_TITLE_RE.fullmatch(raw_title)
            or raw_title.lower() in {host.lower(), "x.com", "twitter.com", "网页来源"}
            else raw_title
        )
        quote = str(item.get("snippet") or item.get("quote") or item.get("text") or "").strip()
        if not quote:
            quote = contextual_quote(url)
        published_at = item.get("published_at") or item.get("publishedAt") or item.get("date")
        results.append(
            SearchResult(
                title=title,
                uri=url,
                quote=quote,
                source_type=source_type,
                published_at=str(published_at) if published_at else None,
                metadata={"provider": provider, "query": query, "adapter": f"{provider}_responses"},
            )
        )
    return results


def response_usage(payload: dict[str, Any]) -> dict[str, Any]:
    usage = payload.get("usage")
    return usage if isinstance(usage, dict) else {}
