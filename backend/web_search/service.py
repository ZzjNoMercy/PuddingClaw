"""Managed web-search orchestration, probing, routing, and fallback."""

from __future__ import annotations

from typing import Any

from web_search.adapters import DeepSeekSearchAdapter, GrokSearchAdapter, TavilySearchAdapter
from web_search.adapters.base import WebSearchAdapter
from web_search.models import AdapterResponse, SearchRequest, SearchResponse, WebSearchError
from web_search.registry import PROVIDER_IDS, WebSearchRegistry, get_web_search_registry

_DOMESTIC_TERMS = (
    "中国大陆", "国内", "工信部", "政务", "国务院", "微信", "公众号", "知乎", "微博", "小红书", "哔哩哔哩", "b站",
)
_GLOBAL_TERMS = ("全球", "海外", "国外", "英文资料", "international", "global")
_X_TERMS = ("x.com", "twitter", "推特", "推文", "x 上", "x上", "thread", "tweet", "账号动态")
_BOTH_TERMS = ("网页和 x", "网页与 x", "全网及社交", "web and x", "web + x")


class WebSearchService:
    def __init__(
        self,
        registry: WebSearchRegistry | None = None,
        adapters: dict[str, WebSearchAdapter] | None = None,
    ) -> None:
        self.registry = registry or get_web_search_registry()
        self.adapters = adapters or {
            "tavily": TavilySearchAdapter(),
            "deepseek": DeepSeekSearchAdapter(),
            "grok": GrokSearchAdapter(),
        }

    @staticmethod
    def _resolved_scope(request: SearchRequest, default_scope: str) -> str:
        if request.scope != "auto":
            return request.scope
        lowered = request.query.lower()
        if any(term in lowered for term in _DOMESTIC_TERMS):
            return "domestic"
        if any(term in lowered for term in _GLOBAL_TERMS) or any(term in lowered for term in _X_TERMS):
            return "global"
        return default_scope if default_scope in {"domestic", "global"} else "global"

    @staticmethod
    def _resolved_source(request: SearchRequest) -> str:
        if request.source != "auto":
            return request.source
        lowered = request.query.lower()
        if any(term in lowered for term in _BOTH_TERMS):
            return "both"
        if any(term in lowered for term in _X_TERMS):
            return "x"
        return "web"

    def _candidate_providers(
        self,
        request: SearchRequest,
        *,
        resolved_scope: str,
        resolved_source: str,
        payload: dict[str, Any],
    ) -> list[str]:
        requires_grok_media = bool(
            request.enable_image_understanding
            or request.enable_image_search
            or request.enable_video_understanding
        )
        if request.provider != "auto":
            if resolved_source in {"x", "both"} and request.provider != "grok":
                raise WebSearchError("X Search 只能由 Grok 执行", category="invalid_route", retryable=False)
            order = [request.provider]
        elif resolved_source in {"x", "both"} or requires_grok_media:
            order = ["grok"]
        else:
            order = list(payload["routing"][resolved_scope])
        providers = payload["providers"]
        return [
            provider_id
            for provider_id in order
            if provider_id in PROVIDER_IDS
            and providers[provider_id].get("enabled")
            and providers[provider_id].get("state") == "ready"
        ]

    def search(self, request: SearchRequest) -> SearchResponse:
        payload = self.registry.raw()
        resolved_scope = self._resolved_scope(request, str(payload.get("default_scope") or "global"))
        resolved_source = self._resolved_source(request)
        candidates = self._candidate_providers(
            request,
            resolved_scope=resolved_scope,
            resolved_source=resolved_source,
            payload=payload,
        )
        if not candidates:
            if request.provider != "auto":
                message = f"{request.provider} 未启用或未通过连接测试"
            else:
                if resolved_source in {"x", "both"}:
                    message = "Grok X Search 未配置或未启用"
                elif request.enable_image_understanding or request.enable_image_search:
                    message = "Grok 图像搜索能力未配置或未启用"
                else:
                    message = "没有已启用且通过测试的联网搜索供应商"
            raise WebSearchError(message, category="unavailable", retryable=False)

        max_attempts = 1
        if request.provider == "auto" and resolved_source == "web" and payload["routing"].get("fallback_enabled", True):
            max_attempts = int(payload["routing"].get("max_provider_attempts") or 2)
        cross_check = bool(
            request.cross_check
            and request.provider == "auto"
            and resolved_source == "web"
            and payload["routing"].get("cross_check_enabled", False)
        )
        if cross_check:
            max_attempts = max(max_attempts, 2)
        attempts: list[dict[str, Any]] = []
        last_error: WebSearchError | None = None
        successes: list[AdapterResponse] = []
        effective_request = request.model_copy(
            update={"scope": resolved_scope, "source": resolved_source}
        )
        for provider_id in candidates[:max_attempts]:
            credential, _source = self.registry.credential(provider_id)
            if not credential:
                error = WebSearchError(f"{provider_id} API Key 未配置", category="authentication", retryable=True)
                attempts.append({"provider": provider_id, "status": "error", "category": error.category, "error": str(error)})
                last_error = error
                continue
            adapter = self.adapters[provider_id]
            try:
                response = adapter.search(
                    effective_request.model_copy(update={"provider": provider_id}),
                    credential,
                    payload["providers"][provider_id].get("options") or {},
                )
            except WebSearchError as exc:
                attempts.append({"provider": provider_id, "status": "error", "category": exc.category, "error": str(exc)})
                last_error = exc
                if exc.category == "authentication":
                    self.registry.mark_auth_failure(provider_id, str(exc))
                    if request.provider == "auto" and resolved_source == "web":
                        continue
                if not exc.retryable:
                    break
                continue
            attempts.append(
                {
                    "provider": provider_id,
                    "status": "success",
                    "latency_ms": response.latency_ms,
                    "source_count": len(response.sources),
                    "server_tools": response.server_tools,
                }
            )
            if not response.sources:
                last_error = WebSearchError(
                    f"{provider_id} 未返回可引用来源", category="empty_sources", retryable=True
                )
                continue
            successes.append(response)
            if not cross_check or len(successes) >= 2:
                return self._merge_successes(
                    request,
                    resolved_scope=resolved_scope,
                    resolved_source=resolved_source,
                    successes=successes,
                    attempts=attempts,
                )
        if successes:
            return self._merge_successes(
                request,
                resolved_scope=resolved_scope,
                resolved_source=resolved_source,
                successes=successes,
                attempts=attempts,
            )
        if last_error is not None:
            raise WebSearchError(
                f"联网搜索失败：{last_error}", category=last_error.category, retryable=False
            ) from last_error
        raise WebSearchError("联网搜索没有可用结果", category="empty_sources", retryable=False)

    @staticmethod
    def _merge_successes(
        request: SearchRequest,
        *,
        resolved_scope: str,
        resolved_source: str,
        successes: list[AdapterResponse],
        attempts: list[dict[str, Any]],
    ) -> SearchResponse:
        sources = []
        seen: set[str] = set()
        for response in successes:
            for source in response.sources:
                if source.uri in seen:
                    continue
                seen.add(source.uri)
                sources.append(source)
        context = "\n\n".join(
            f"[{response.provider}]\n{response.answer_context}" for response in successes
        )
        usage = {
            response.provider: response.usage
            for response in successes
            if response.usage
        }
        return SearchResponse(
            answer_context=context,
            sources=sources[: request.max_results],
            requested_scope=request.scope,
            resolved_scope=resolved_scope,
            requested_source=request.source,
            resolved_source=resolved_source,
            selected_provider=successes[0].provider,
            attempts=attempts,
            usage=usage,
        )

    def test_provider(self, provider_id: str) -> dict[str, Any]:
        if provider_id not in self.adapters:
            raise ValueError(f"Unknown web-search provider: {provider_id}")
        credential, credential_source = self.registry.credential(provider_id)
        if not credential:
            raise ValueError("请先配置 API Key")
        payload = self.registry.raw()
        try:
            response = self.adapters[provider_id].probe(
                credential,
                payload["providers"][provider_id].get("options") or {},
            )
            if not response.sources:
                raise WebSearchError("连接成功，但没有返回可引用来源", category="empty_sources", retryable=False)
        except Exception as exc:
            latency = int(getattr(exc, "latency_ms", 0) or 0)
            self.registry.mark_test(provider_id, success=False, latency_ms=latency, error=str(exc))
            raise
        self.registry.mark_test(provider_id, success=True, latency_ms=response.latency_ms)
        return {
            "success": True,
            "provider_id": provider_id,
            "credential_source": credential_source,
            "latency_ms": response.latency_ms,
            "source_count": len(response.sources),
            "server_tools": response.server_tools,
        }

    def enable_provider(self, provider_id: str) -> dict[str, Any]:
        self.registry.prepare(provider_id)
        return self.registry.set_enabled(provider_id, True)


_default_service: WebSearchService | None = None


def get_web_search_service() -> WebSearchService:
    global _default_service
    if _default_service is None:
        _default_service = WebSearchService()
    return _default_service
