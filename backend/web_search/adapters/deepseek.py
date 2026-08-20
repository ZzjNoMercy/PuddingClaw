"""DeepSeek v4 Flash server-side Web Search adapter."""

from __future__ import annotations

import time
from typing import Any

from web_search.adapters.base import (
    WebSearchAdapter,
    normalized_response_sources,
    openai_compatible_http_client,
    response_payload,
    response_text,
    response_usage,
)
from web_search.models import AdapterResponse, SearchRequest, WebSearchError


class DeepSeekSearchAdapter(WebSearchAdapter):
    id = "deepseek"

    def probe(self, credential: str, options: dict[str, Any]) -> AdapterResponse:
        return self.search(
            SearchRequest(
                query=(
                    "Use Web Search now. Find the official DeepSeek Responses API documentation "
                    "page on api-docs.deepseek.com and return its exact HTTPS URL with a citation. "
                    "Do not ask for clarification."
                ),
                scope="global",
                source="web",
                provider="deepseek",
                max_results=1,
            ),
            credential,
            options,
        )

    def search(self, request: SearchRequest, credential: str, options: dict[str, Any]) -> AdapterResponse:
        if request.source in {"x", "both"}:
            raise WebSearchError("DeepSeek 不支持 X Search", category="unsupported_source", retryable=False)
        from openai import APIConnectionError, APIStatusError, OpenAI

        started = time.monotonic()
        client = OpenAI(
            api_key=credential,
            base_url="https://api.deepseek.com",
            timeout=30.0,
            max_retries=0,
            http_client=openai_compatible_http_client(timeout=30.0),
        )
        domain_note = ""
        if request.include_domains:
            domain_note = f" Only use these domains when possible: {', '.join(request.include_domains)}."
        elif request.exclude_domains:
            domain_note = f" Do not use these domains: {', '.join(request.exclude_domains)}."
        prompt = (
            f"Search the public web for the following request and return concise evidence with clickable source URLs."
            f" Prefer Chinese mainland public sources when the request is domestic.{domain_note}\n\n{request.query}"
        )
        try:
            response = client.responses.create(
                model="deepseek-v4-flash",
                input=prompt,
                tools=[{"type": "web_search"}],
                tool_choice={"type": "web_search"},
                max_output_tokens=800 if request.max_results == 1 else 1200,
            )
        except APIStatusError as exc:
            category = "authentication" if exc.status_code in {401, 403} else "rate_limit" if exc.status_code == 429 else "provider_http"
            raise WebSearchError(
                f"DeepSeek HTTP {exc.status_code}", category=category, retryable=exc.status_code not in {400, 401, 403}
            ) from exc
        except APIConnectionError as exc:
            raise WebSearchError(f"DeepSeek 连接失败：{exc}", category="network", retryable=True) from exc
        except Exception as exc:
            raise WebSearchError(f"DeepSeek 搜索失败：{exc}", category="provider_error", retryable=True) from exc
        payload = response_payload(response)
        text = response_text(response, payload)
        sources = normalized_response_sources(response, payload, text, provider="deepseek", query=request.query)
        return AdapterResponse(
            provider="deepseek",
            answer_context=text,
            sources=sources,
            latency_ms=int((time.monotonic() - started) * 1000),
            usage=response_usage(payload),
            server_tools=["web_search"],
        )
