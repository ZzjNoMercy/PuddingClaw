"""Tavily REST Search adapter."""

from __future__ import annotations

import time
from typing import Any

import requests

from web_search.adapters.base import WebSearchAdapter
from web_search.models import AdapterResponse, SearchRequest, SearchResult, WebSearchError


class TavilySearchAdapter(WebSearchAdapter):
    id = "tavily"

    def search(self, request: SearchRequest, credential: str, options: dict[str, Any]) -> AdapterResponse:
        if request.source in {"x", "both"}:
            raise WebSearchError("Tavily 不支持 X Search", category="unsupported_source", retryable=False)
        started = time.monotonic()
        body: dict[str, Any] = {
            "api_key": credential,
            "query": request.query,
            "max_results": request.max_results,
            "search_depth": options.get("search_depth", "basic"),
            "include_answer": False,
            "include_images": False,
            "include_raw_content": False,
            "include_usage": True,
        }
        if request.include_domains:
            body["include_domains"] = request.include_domains
        if request.exclude_domains:
            body["exclude_domains"] = request.exclude_domains
        if request.time_range:
            body["time_range"] = request.time_range
        if request.scope == "domestic":
            body["country"] = "china"
        try:
            response = requests.post(
                "https://api.tavily.com/search",
                json=body,
                headers={"Accept": "application/json"},
                timeout=18,
            )
            response.raise_for_status()
            payload = response.json()
        except requests.HTTPError as exc:
            status = exc.response.status_code if exc.response is not None else 0
            category = "authentication" if status in {401, 403} else "rate_limit" if status == 429 else "provider_http"
            raise WebSearchError(f"Tavily HTTP {status}", category=category, retryable=status not in {400, 401, 403}) from exc
        except (requests.RequestException, ValueError) as exc:
            raise WebSearchError(f"Tavily 请求失败：{exc}", category="network", retryable=True) from exc

        sources: list[SearchResult] = []
        for item in payload.get("results") or []:
            if not isinstance(item, dict):
                continue
            url = str(item.get("url") or "").strip()
            title = str(item.get("title") or "").strip()
            if not url.startswith(("http://", "https://")) or not title:
                continue
            sources.append(
                SearchResult(
                    title=title,
                    uri=url,
                    quote=str(item.get("content") or "").strip(),
                    score=item.get("score"),
                    source_type="web",
                    metadata={"provider": "tavily", "query": request.query, "adapter": "tavily_search"},
                )
            )
        lines = [f"Tavily 为“{request.query}”返回 {len(sources)} 条网页结果。"]
        lines.extend(f"{index}. {item.title}\n{item.quote}" for index, item in enumerate(sources, 1))
        return AdapterResponse(
            provider="tavily",
            answer_context="\n".join(lines),
            sources=sources,
            latency_ms=int((time.monotonic() - started) * 1000),
            usage=payload.get("usage") if isinstance(payload.get("usage"), dict) else {},
            server_tools=["web_search"],
        )
