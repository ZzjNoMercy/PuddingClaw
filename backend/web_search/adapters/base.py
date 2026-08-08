"""Adapter base classes and provider-response normalization."""

from __future__ import annotations

import re
from abc import ABC, abstractmethod
from typing import Any
from urllib.parse import urlsplit

from web_search.models import AdapterResponse, SearchRequest, SearchResult

_MARKDOWN_URL_RE = re.compile(r"\[([^\]]+)\]\((https?://[^)\s]+)\)")
_BARE_URL_RE = re.compile(r"https?://[^\s<>\)\]\}\[`'\"，。；：、（）【】]+")


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
    raw_citations = getattr(response, "citations", None)
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
        title = str(item.get("title") or item.get("name") or host or "网页来源").strip()
        quote = str(item.get("snippet") or item.get("quote") or item.get("text") or "").strip()
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
