"""xAI Grok server-side Web Search and X Search adapter."""

from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta
from typing import Any

from web_search.adapters.base import (
    WebSearchAdapter,
    normalized_response_sources,
    response_payload,
    response_text,
    response_usage,
)
from web_search.models import AdapterResponse, SearchRequest, WebSearchError


class GrokSearchAdapter(WebSearchAdapter):
    id = "grok"

    @staticmethod
    def _relative_date_range(time_range: str | None) -> tuple[str | None, str | None]:
        days = {"day": 1, "week": 7, "month": 30, "year": 365}.get(str(time_range or ""))
        if days is None:
            return None, None
        today = datetime.now(UTC).date()
        return (today - timedelta(days=days)).isoformat(), today.isoformat()

    def probe(self, credential: str, options: dict[str, Any]) -> AdapterResponse:
        source = "both" if options.get("web_search_enabled", True) and options.get("x_search_enabled", True) else (
            "x" if options.get("x_search_enabled", True) else "web"
        )
        return self.search(
            SearchRequest(
                query="Find one current xAI official documentation page and one recent post from the @xai account.",
                scope="global",
                source=source,
                provider="grok",
                max_results=2,
            ),
            credential,
            options,
        )

    @classmethod
    def _build_tools(cls, request: SearchRequest, options: dict[str, Any]) -> list[dict[str, Any]]:
        source = request.source if request.source != "auto" else "web"
        web_enabled = bool(options.get("web_search_enabled", True))
        x_enabled = bool(options.get("x_search_enabled", True))
        if source in {"web", "both"} and not web_enabled:
            raise WebSearchError("Grok Web Search 未启用", category="capability_disabled", retryable=False)
        if source in {"x", "both"} and not x_enabled:
            raise WebSearchError("Grok X Search 未启用", category="capability_disabled", retryable=False)

        tools: list[dict[str, Any]] = []
        if source in {"web", "both"}:
            web_tool: dict[str, Any] = {"type": "web_search"}
            # xAI's OpenAI Responses-compatible API nests domain constraints
            # under filters; the native xAI SDK exposes the same names directly.
            if request.include_domains:
                web_tool["filters"] = {"allowed_domains": request.include_domains}
            elif request.exclude_domains:
                web_tool["filters"] = {"excluded_domains": request.exclude_domains}
            if request.enable_image_understanding:
                web_tool["enable_image_understanding"] = True
            if request.enable_image_search:
                web_tool["enable_image_search"] = True
            tools.append(web_tool)
        if source in {"x", "both"}:
            x_tool: dict[str, Any] = {"type": "x_search"}
            if request.allowed_x_handles:
                x_tool["allowed_x_handles"] = request.allowed_x_handles
            elif request.excluded_x_handles:
                x_tool["excluded_x_handles"] = request.excluded_x_handles
            relative_from, relative_to = cls._relative_date_range(request.time_range)
            if request.from_date or relative_from:
                x_tool["from_date"] = request.from_date or relative_from
            if request.to_date or relative_to:
                x_tool["to_date"] = request.to_date or relative_to
            if request.enable_image_understanding:
                x_tool["enable_image_understanding"] = True
            if request.enable_video_understanding:
                x_tool["enable_video_understanding"] = True
            tools.append(x_tool)
        return tools

    def search(self, request: SearchRequest, credential: str, options: dict[str, Any]) -> AdapterResponse:
        from openai import APIConnectionError, APIStatusError, OpenAI

        tools = self._build_tools(request, options)

        started = time.monotonic()
        client = OpenAI(
            api_key=credential,
            base_url="https://api.x.ai/v1",
            # grok-4.5 may spend more than 30 seconds reasoning and searching X;
            # the previous limit produced false connection failures in normal use.
            timeout=60.0,
            max_retries=0,
        )
        prompt = (
            "Use the enabled server-side search tools to research the request. "
            "Return concise evidence with clickable source URLs; preserve X post URLs for X evidence.\n\n"
            + request.query
        )
        try:
            response = client.responses.create(
                model="grok-4.5",
                input=prompt,
                tools=tools,
                max_output_tokens=1400,
            )
        except APIStatusError as exc:
            category = "authentication" if exc.status_code in {401, 403} else "rate_limit" if exc.status_code == 429 else "provider_http"
            raise WebSearchError(
                f"Grok HTTP {exc.status_code}", category=category, retryable=exc.status_code not in {400, 401, 403}
            ) from exc
        except APIConnectionError as exc:
            raise WebSearchError(f"Grok 连接失败：{exc}", category="network", retryable=True) from exc
        except Exception as exc:
            raise WebSearchError(f"Grok 搜索失败：{exc}", category="provider_error", retryable=True) from exc

        payload = response_payload(response)
        text = response_text(response, payload)
        sources = normalized_response_sources(response, payload, text, provider="grok", query=request.query)
        return AdapterResponse(
            provider="grok",
            answer_context=text,
            sources=sources,
            latency_ms=int((time.monotonic() - started) * 1000),
            usage=response_usage(payload),
            server_tools=[str(item["type"]) for item in tools],
        )
