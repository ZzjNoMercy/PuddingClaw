from __future__ import annotations

import json
from typing import Any

import pytest

from graph.citations import format_sources_for_model, make_source_id, parse_tool_result
from web_search.adapters.base import normalized_response_sources
from web_search.adapters.grok import GrokSearchAdapter
from web_search.models import AdapterResponse, SearchRequest, SearchResult, WebSearchError
from web_search.registry import WebSearchRegistry
from web_search.service import WebSearchService


class FakeAdapter:
    def __init__(
        self,
        provider_id: str,
        *,
        fail: bool = False,
        error_category: str = "network",
        source_type: str = "web",
    ) -> None:
        self.id = provider_id
        self.fail = fail
        self.error_category = error_category
        self.source_type = source_type
        self.calls: list[SearchRequest] = []

    def search(self, request: SearchRequest, credential: str, options: dict[str, Any]) -> AdapterResponse:
        self.calls.append(request)
        if self.fail:
            raise WebSearchError(
                f"{self.id} unavailable",
                category=self.error_category,
                retryable=self.error_category != "authentication",
            )
        uri = "https://x.com/xai/status/1" if request.source in {"x", "both"} else f"https://{self.id}.example/result"
        return AdapterResponse(
            provider=self.id,
            answer_context=f"result from {self.id}",
            sources=[
                SearchResult(
                    title=f"{self.id} result",
                    uri=uri,
                    quote="evidence",
                    source_type="x" if "x.com" in uri else "web",
                )
            ],
            latency_ms=10,
            usage={},
            server_tools=["x_search" if request.source == "x" else "web_search"],
        )

    def probe(self, credential: str, options: dict[str, Any]) -> AdapterResponse:
        return self.search(SearchRequest(query="probe", provider=self.id), credential, options)


def _ready_registry(tmp_path, *provider_ids: str) -> WebSearchRegistry:
    registry = WebSearchRegistry(tmp_path)
    for provider_id in provider_ids:
        registry.save_credential(provider_id, f"{provider_id}-secret-key")
        registry.mark_test(provider_id, success=True, latency_ms=12)
        registry.set_enabled(provider_id, True)
    return registry


def test_registry_masks_credentials_and_uses_confirmed_default_routes(tmp_path) -> None:
    registry = _ready_registry(tmp_path, "tavily", "grok")

    displayed = registry.display()
    rendered = json.dumps(displayed, ensure_ascii=False)

    assert "tavily-secret-key" not in rendered
    assert "grok-secret-key" not in rendered
    assert displayed["routing"]["domestic"] == ["deepseek", "tavily", "grok"]
    assert displayed["routing"]["global"] == ["grok", "tavily", "deepseek"]
    assert displayed["ready_providers"] == ["tavily", "grok"]
    assert all(item["dependencies"]["status"] == "already_satisfied" for item in displayed["providers"])
    assert registry.available() is True


def test_legacy_global_switch_is_ignored_in_favor_of_provider_readiness(tmp_path) -> None:
    registry = _ready_registry(tmp_path, "grok")
    payload = json.loads(registry.path.read_text(encoding="utf-8"))
    payload["enabled"] = False
    registry.path.write_text(json.dumps(payload), encoding="utf-8")

    assert registry.available() is True
    assert "enabled" not in registry.raw()


def test_disable_preserves_ready_state_and_reenable_does_not_probe(tmp_path) -> None:
    registry = _ready_registry(tmp_path, "grok")
    adapter = FakeAdapter("grok")
    service = WebSearchService(
        registry,
        {"grok": adapter, "tavily": FakeAdapter("tavily"), "deepseek": FakeAdapter("deepseek")},
    )

    disabled = registry.set_enabled("grok", False)
    grok = next(item for item in disabled["providers"] if item["id"] == "grok")
    assert grok["enabled"] is False
    assert grok["state"] == "ready"
    assert registry.available() is False

    enabled = service.enable_provider("grok")
    grok = next(item for item in enabled["providers"] if item["id"] == "grok")
    assert grok["enabled"] is True
    assert adapter.calls == []


def test_global_web_search_prefers_grok(tmp_path) -> None:
    registry = _ready_registry(tmp_path, "tavily", "grok")
    grok = FakeAdapter("grok")
    tavily = FakeAdapter("tavily")
    service = WebSearchService(registry, {"grok": grok, "tavily": tavily, "deepseek": FakeAdapter("deepseek")})

    response = service.search(SearchRequest(query="latest AI API news", scope="global", source="web"))

    assert response.selected_provider == "grok"
    assert len(grok.calls) == 1
    assert tavily.calls == []


def test_cross_check_uses_two_providers_only_when_both_request_and_setting_allow_it(tmp_path) -> None:
    registry = _ready_registry(tmp_path, "tavily", "grok")
    registry.update_routing({"cross_check_enabled": True})
    grok = FakeAdapter("grok")
    tavily = FakeAdapter("tavily")
    service = WebSearchService(
        registry,
        {"grok": grok, "tavily": tavily, "deepseek": FakeAdapter("deepseek")},
    )

    response = service.search(
        SearchRequest(query="verify the latest AI API news", scope="global", cross_check=True)
    )

    assert [attempt["provider"] for attempt in response.attempts] == ["grok", "tavily"]
    assert {source.uri for source in response.sources} == {
        "https://grok.example/result",
        "https://tavily.example/result",
    }


def test_web_search_falls_back_but_x_search_never_impersonates_x(tmp_path) -> None:
    registry = _ready_registry(tmp_path, "tavily", "grok")
    grok = FakeAdapter("grok", fail=True)
    tavily = FakeAdapter("tavily")
    service = WebSearchService(registry, {"grok": grok, "tavily": tavily, "deepseek": FakeAdapter("deepseek")})

    web_response = service.search(SearchRequest(query="latest AI API news", scope="global", source="web"))
    assert web_response.selected_provider == "tavily"

    with pytest.raises(WebSearchError, match="联网搜索失败"):
        service.search(SearchRequest(query="what is xAI saying on X", source="x"))
    assert len(tavily.calls) == 1


def test_auth_failure_disables_rejected_provider_and_falls_back(tmp_path) -> None:
    registry = _ready_registry(tmp_path, "tavily", "grok")
    grok = FakeAdapter("grok", fail=True, error_category="authentication")
    service = WebSearchService(
        registry,
        {"grok": grok, "tavily": FakeAdapter("tavily"), "deepseek": FakeAdapter("deepseek")},
    )

    response = service.search(SearchRequest(query="latest AI API news", scope="global"))

    assert response.selected_provider == "tavily"
    grok_config = next(item for item in registry.display()["providers"] if item["id"] == "grok")
    assert grok_config["enabled"] is False
    assert grok_config["state"] == "needs_test"


def test_explicit_disabled_provider_does_not_fallback(tmp_path) -> None:
    registry = _ready_registry(tmp_path, "tavily")
    service = WebSearchService(
        registry,
        {"grok": FakeAdapter("grok"), "tavily": FakeAdapter("tavily"), "deepseek": FakeAdapter("deepseek")},
    )

    with pytest.raises(WebSearchError, match="grok 未启用"):
        service.search(SearchRequest(query="latest AI API news", provider="grok"))


def test_auto_source_routes_x_intent_to_grok_x_search(tmp_path) -> None:
    registry = _ready_registry(tmp_path, "tavily", "grok")
    grok = FakeAdapter("grok")
    service = WebSearchService(registry, {"grok": grok, "tavily": FakeAdapter("tavily"), "deepseek": FakeAdapter("deepseek")})

    response = service.search(SearchRequest(query="X 上大家怎么评价 Grok 4.5？"))

    assert response.resolved_source == "x"
    assert response.selected_provider == "grok"
    assert response.sources[0].source_type == "x"
    assert grok.calls[0].source == "x"


def test_search_request_rejects_conflicting_filters() -> None:
    with pytest.raises(ValueError, match="不能同时设置"):
        SearchRequest(query="test", include_domains=["a.com"], exclude_domains=["b.com"])
    with pytest.raises(ValueError, match="不能同时设置"):
        SearchRequest(query="test", source="x", allowed_x_handles=["xai"], excluded_x_handles=["spam"])


def test_grok_web_search_builds_domain_and_image_parameters() -> None:
    tools = GrokSearchAdapter._build_tools(
        SearchRequest(
            query="show official launch images",
            source="web",
            include_domains=["x.ai", "spacex.com"],
            enable_image_understanding=True,
            enable_image_search=True,
        ),
        {"web_search_enabled": True, "x_search_enabled": True},
    )

    assert tools == [
        {
            "type": "web_search",
            "filters": {"allowed_domains": ["x.ai", "spacex.com"]},
            "enable_image_understanding": True,
            "enable_image_search": True,
        }
    ]


def test_grok_x_search_builds_media_understanding_parameters() -> None:
    tools = GrokSearchAdapter._build_tools(
        SearchRequest(
            query="analyze recent media posts",
            source="x",
            allowed_x_handles=["xai"],
            enable_image_understanding=True,
            enable_video_understanding=True,
        ),
        {"web_search_enabled": True, "x_search_enabled": True},
    )

    assert tools == [
        {
            "type": "x_search",
            "allowed_x_handles": ["xai"],
            "enable_image_understanding": True,
            "enable_video_understanding": True,
        }
    ]


def test_openai_compatible_http_client_follows_shared_proxy(monkeypatch) -> None:
    import httpx

    from web_search.adapters import base

    monkeypatch.setattr(base, "resolved_https_proxy", lambda: "http://127.0.0.1:27890")
    client = base.openai_compatible_http_client(timeout=1.0)
    assert isinstance(client, httpx.Client)
    client.close()

    monkeypatch.setattr(base, "resolved_https_proxy", lambda: "")
    assert base.openai_compatible_http_client(timeout=1.0) is None


def test_grok_search_routes_openai_client_through_shared_proxy(monkeypatch) -> None:
    import httpx
    from openai import APIConnectionError

    from web_search.adapters import base

    captured: dict[str, Any] = {}

    class _FakeResponses:
        @staticmethod
        def create(**_kwargs: Any) -> Any:
            raise APIConnectionError(request=httpx.Request("POST", "https://api.x.ai/v1/responses"))

    class _FakeOpenAI:
        def __init__(self, **kwargs: Any) -> None:
            captured.update(kwargs)

        @property
        def responses(self) -> _FakeResponses:
            return _FakeResponses()

    monkeypatch.setattr("openai.OpenAI", _FakeOpenAI)
    monkeypatch.setattr(base, "resolved_https_proxy", lambda: "http://127.0.0.1:27890")

    with pytest.raises(WebSearchError):
        GrokSearchAdapter().search(
            SearchRequest(query="ping", source="web", provider="grok"),
            "xai-test-key",
            {"web_search_enabled": True, "x_search_enabled": True},
        )

    http_client = captured.get("http_client")
    assert isinstance(http_client, httpx.Client)
    http_client.close()


def test_grok_media_capabilities_do_not_fall_back_to_other_providers(tmp_path) -> None:
    registry = _ready_registry(tmp_path, "tavily", "grok")
    grok = FakeAdapter("grok")
    tavily = FakeAdapter("tavily")
    service = WebSearchService(
        registry,
        {"grok": grok, "tavily": tavily, "deepseek": FakeAdapter("deepseek")},
    )

    response = service.search(
        SearchRequest(query="show launch images", scope="domestic", enable_image_search=True)
    )

    assert response.selected_provider == "grok"
    assert grok.calls[0].enable_image_search is True
    assert tavily.calls == []


def test_unicode_punctuation_between_urls_does_not_corrupt_netloc() -> None:
    text = (
        "官方入口（`https://api.deepseek.com`）："
        "https://api-docs.deepseek.com/zh-cn/guides/responses_api/"
    )

    sources = normalized_response_sources(
        {},
        {},
        text,
        provider="deepseek",
        query="connectivity check",
    )

    assert [source.uri for source in sources] == [
        "https://api.deepseek.com",
        "https://api-docs.deepseek.com/zh-cn/guides/responses_api/",
    ]


def test_xai_top_level_citations_are_kept_without_inline_links() -> None:
    sources = normalized_response_sources(
        {},
        {"citations": ["https://x.com/thsottiaux/status/2086972933566857393"]},
        "Grok found a recent Codex usage reset announcement.",
        provider="grok",
        query="codex reset",
    )

    assert len(sources) == 1
    assert sources[0].uri == "https://x.com/thsottiaux/status/2086972933566857393"
    assert sources[0].title == "@thsottiaux 的 X 帖子"


def test_numeric_xai_inline_citation_title_is_replaced_and_quote_is_extracted() -> None:
    text = (
        "Latest official reset: @thsottiaux reset paid Codex usage limits."
        "[[1]](https://x.com/thsottiaux/status/2086972933566857393)"
    )
    sources = normalized_response_sources({}, {}, text, provider="grok", query="codex reset")

    assert sources[0].title == "@thsottiaux 的 X 帖子"
    assert "reset paid Codex usage limits" in sources[0].quote


def test_numeric_xai_web_citation_title_uses_page_slug_and_host() -> None:
    text = "See the current API guide.[[1]](https://docs.x.ai/developers/tools/web-search)"
    sources = normalized_response_sources({}, {}, text, provider="grok", query="xAI web search")

    assert sources[0].source_type == "web"
    assert sources[0].title == "web search · docs.x.ai"


def test_web_source_id_is_stable_across_provider_labels_and_catalog_shows_uri() -> None:
    first = {
        "title": "1",
        "uri": "https://x.com/thsottiaux",
        "source_type": "x",
        "quote": "first excerpt",
    }
    second = {
        "title": "@thsottiaux 的 X 主页",
        "uri": "https://x.com/thsottiaux",
        "source_type": "x",
        "quote": "another excerpt",
    }

    assert make_source_id(first) == make_source_id(second)
    rendered = format_sources_for_model("answer", [{**second, "source_id": make_source_id(second)}])
    assert "链接：https://x.com/thsottiaux" in rendered


def test_managed_tool_returns_structured_citations(tmp_path, monkeypatch) -> None:
    from tools.web_search_tool import ManagedWebSearchTool

    registry = _ready_registry(tmp_path, "grok")
    service = WebSearchService(registry, {"grok": FakeAdapter("grok"), "tavily": FakeAdapter("tavily"), "deepseek": FakeAdapter("deepseek")})
    monkeypatch.setattr("tools.web_search_tool.get_web_search_service", lambda: service)

    raw = ManagedWebSearchTool().invoke({"query": "latest AI API news", "scope": "global"})
    context, sources = parse_tool_result(raw)

    assert "搜索路由：grok" in context
    assert sources[0]["uri"] == "https://grok.example/result"
    assert sources[0]["metadata"]["provider"] == "grok"


def test_web_search_schema_declares_internal_knowledge_first_boundary() -> None:
    from tools.web_search_tool import ManagedWebSearchTool

    description = ManagedWebSearchTool().description

    assert "Public-internet search fallback" in description
    assert "first query the internal Markdown LLM Wiki" in description
    assert "open-source, projects, or recommend" in description
    assert "explicitly requests current/latest information" in description
    assert "clear knowledge gap" in description
