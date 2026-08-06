from __future__ import annotations

import json
import os
import stat

import httpx
import pytest

from provider_registry import DASHSCOPE_NATIVE_MODEL_CATALOG, ProviderRegistry


def _legacy_config() -> dict:
    return {
        "fallback_llm": {
            "provider": "deepseek",
            "model": "deepseek-chat",
            "base_url": "https://api.deepseek.com",
            "api_key": "deepseek-test-secret",
            "temperature": 0.2,
        },
        "fallback_embedding": {
            "provider": "qwen",
            "model": "text-embedding-v4",
            "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
            "api_key": "dashscope-text-secret",
            "dimension": 1024,
            "batch_size": 20,
        },
        "multimodal_embedding": {
            "model": "qwen2.5-vl-embedding",
            "api_key": "dashscope-mm-secret",
            "dimension": 1024,
            "batch_size": 10,
        },
    }


def test_migrates_direct_llm_text_and_multimodal_bindings(tmp_path):
    registry = ProviderRegistry(tmp_path)
    legacy = _legacy_config()

    registry.ensure_migrated(legacy)

    assert registry.resolve_binding("agent", legacy_config={})["api_key"] == "deepseek-test-secret"
    assert registry.resolve_binding("agent", legacy_config={})["categories"] == ["llm"]
    assert registry.resolve_binding("image_analyzer", legacy_config={})["id"] == registry.resolve_binding(
        "agent", legacy_config={}
    )["id"]
    assert registry.resolve_binding("text_embedding", legacy_config={})["dimension"] == 1024
    multimodal = registry.resolve_binding("multimodal_embedding", legacy_config={})
    assert multimodal["api_key"] == "dashscope-text-secret"
    assert multimodal["protocol"] == "dashscope_multimodal_embedding"


def test_display_never_contains_plaintext_credentials(tmp_path):
    registry = ProviderRegistry(tmp_path)
    registry.ensure_migrated(_legacy_config())

    rendered = json.dumps(registry.display(legacy_config={}), ensure_ascii=False)

    assert "deepseek-test-secret" not in rendered
    assert "dashscope-text-secret" not in rendered
    assert "dashscope-mm-secret" not in rendered


def test_explicit_conversation_model_uses_configured_endpoint_not_legacy_provider_switch(tmp_path):
    registry = ProviderRegistry(tmp_path)
    registry.ensure_migrated(_legacy_config())
    registry.update_provider(
        "kimi",
        {
            "enabled": False,
            "endpoints": [{"id": "kimi-openai", "api_key": "kimi-test-secret"}],
        },
    )
    model = registry.upsert_model(
        "kimi",
        {
            "endpoint_id": "kimi-openai",
            "capability": "llm",
            "name": "kimi-k3",
            "categories": ["llm"],
        },
    )

    resolved = registry.resolve_model(model["id"], legacy_config={})

    assert resolved["provider_id"] == "kimi"
    assert resolved["api_key"] == "kimi-test-secret"
    assert resolved["thinking_profile"]["levels"] == ["low", "high", "max"]


def test_environment_reference_is_not_copied_to_credential_file(tmp_path, monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "env-only-secret")
    registry = ProviderRegistry(tmp_path)
    legacy = _legacy_config()
    legacy["fallback_llm"]["api_key"] = ""

    registry.ensure_migrated(legacy)

    payload = json.loads((tmp_path / "credentials.json").read_text())
    assert "env-only-secret" not in json.dumps(payload)
    assert registry.resolve_binding("agent", legacy_config={})["api_key"] == "env-only-secret"


def test_endpoint_connectivity_probe_discards_model_list(tmp_path, monkeypatch):
    registry = ProviderRegistry(tmp_path)
    registry.ensure_migrated(_legacy_config())
    captured: dict[str, object] = {}

    class FakeResponse:
        status_code = 200

        def raise_for_status(self):
            return None

    class FakeClient:
        def __init__(self, **kwargs):
            captured["client_kwargs"] = kwargs

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def get(self, url, headers):
            captured["url"] = url
            captured["headers"] = headers
            return FakeResponse()

    monkeypatch.setattr(httpx, "Client", FakeClient)

    result = registry.test_endpoint("deepseek", "deepseek-openai")

    assert result == {"reachable": True, "status_code": 200}
    assert captured["url"] == "https://api.deepseek.com/models"
    assert captured["headers"] == {"Authorization": "Bearer deepseek-test-secret"}
    assert captured["client_kwargs"] == {"timeout": 10.0, "trust_env": False}


def test_endpoint_probe_accepts_unsaved_local_alias_with_explicit_key(tmp_path, monkeypatch):
    registry = ProviderRegistry(tmp_path)
    registry.ensure_migrated(_legacy_config())
    captured: dict[str, object] = {}

    class FakeResponse:
        status_code = 200

        def raise_for_status(self):
            return None

    class FakeClient:
        def __init__(self, **_kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def get(self, _url, headers):
            captured["headers"] = headers
            return FakeResponse()

    monkeypatch.setattr(httpx, "Client", FakeClient)

    registry.test_endpoint(
        "deepseek",
        "deepseek-openai",
        api_key="unsaved-evaluation-secret",
        credential_name="evaluate",
    )

    assert captured["headers"] == {
        "Authorization": "Bearer unsaved-evaluation-secret"
    }


def test_dashscope_native_discovery_merges_official_and_remote_models(tmp_path, monkeypatch):
    registry = ProviderRegistry(tmp_path)
    registry.ensure_migrated(_legacy_config())
    captured: dict[str, object] = {}

    class FakeResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {
                "output": {
                    "page_no": 1,
                    "page_size": 100,
                    "total": 3,
                    "models": [
                        {"model_name": "qwen3-vl-embedding-preview"},
                        {"model_name": "gte-rerank-v3"},
                        {"model_name": "qwen3-235b"},
                    ],
                }
            }

    class FakeClient:
        def __init__(self, **kwargs):
            captured["client_kwargs"] = kwargs

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def get(self, url, headers, timeout):
            captured["url"] = url
            captured["headers"] = headers
            captured["request_timeout"] = timeout
            return FakeResponse()

    monkeypatch.setattr(httpx, "Client", FakeClient)

    models = registry.discover_models("dashscope", "dashscope-native-mm")
    names = {model["name"] for model in models}

    assert "qwen2.5-vl-embedding" in names
    assert "qwen3-vl-rerank" in names
    assert "qwen3-vl-embedding-preview" in names
    assert "gte-rerank-v3" in names
    assert "qwen3-235b" not in names
    assert str(captured["url"]).startswith("https://dashscope.aliyuncs.com/api/v1/deployments/models?")
    assert captured["headers"] == {"Authorization": "Bearer dashscope-text-secret"}
    assert captured["client_kwargs"] == {"timeout": 3.0}
    assert 0 < float(captured["request_timeout"]) <= 3.0


def test_dashscope_native_discovery_falls_back_and_caches_on_timeout(tmp_path, monkeypatch):
    registry = ProviderRegistry(tmp_path)
    registry.ensure_migrated(_legacy_config())
    calls = 0

    class FakeClient:
        def __init__(self, **_kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def get(self, _url, headers=None, **_kwargs):
            nonlocal calls
            calls += 1
            raise httpx.ReadTimeout("slow deployment catalog")

    monkeypatch.setattr(httpx, "Client", FakeClient)

    first = registry.discover_models("dashscope", "dashscope-native-mm")
    second = registry.discover_models("dashscope", "dashscope-native-mm")

    assert {model["name"] for model in first} == set(DASHSCOPE_NATIVE_MODEL_CATALOG)
    assert second == first
    assert calls == 1


def test_provider_update_persists_api_key_and_reports_masked_status(tmp_path):
    registry = ProviderRegistry(tmp_path)

    displayed = registry.update_provider(
        "deepseek",
        {
            "endpoints": [
                {
                    "id": "deepseek-openai",
                    "base_url": "https://api.deepseek.com",
                    "api_key": "saved-provider-secret",
                }
            ]
        },
    )

    endpoint = displayed["providers"][0]["endpoints"][0]
    assert endpoint["credential_configured"] is True
    assert endpoint["credential_source"] == "local_file"
    assert endpoint["api_key_masked"].endswith("cret")
    assert "saved-provider-secret" not in json.dumps(displayed)

    reloaded = ProviderRegistry(tmp_path)
    reloaded_endpoint = reloaded._payload()["providers"][0]["endpoints"][0]
    assert reloaded.credentials.get(reloaded_endpoint["credential_ref"]) == "saved-provider-secret"


def test_provider_supports_named_credentials_with_default_fallback(tmp_path):
    registry = ProviderRegistry(tmp_path)
    registry.ensure_migrated(_legacy_config())
    model_id = registry._payload()["bindings"]["agent"]

    displayed = registry.update_provider(
        "deepseek",
        {
            "credentials": [
                {"name": "default", "value": "primary-secret"},
                {"name": "evaluation", "value": "evaluation-secret"},
            ]
        },
        legacy_config={},
    )

    provider = next(item for item in displayed["providers"] if item["id"] == "deepseek")
    assert [item["name"] for item in provider["api_keys"]] == ["default", "evaluation"]
    assert all(item["credential_configured"] for item in provider["api_keys"])
    assert "primary-secret" not in json.dumps(displayed)
    assert "evaluation-secret" not in json.dumps(displayed)
    assert registry.resolve_model(model_id, legacy_config={})["api_key"] == "primary-secret"
    evaluated = registry.resolve_model(
        model_id,
        legacy_config={},
        credential_name="evaluation",
    )
    assert evaluated["api_key"] == "evaluation-secret"
    assert evaluated["credential_name"] == "evaluation"
    assert registry.reveal_credential(
        "deepseek",
        "evaluation",
        legacy_config={},
    ) == "evaluation-secret"


def test_unknown_named_credential_is_rejected(tmp_path):
    registry = ProviderRegistry(tmp_path)
    registry.ensure_migrated(_legacy_config())
    model_id = registry._payload()["bindings"]["agent"]

    with pytest.raises(ValueError, match="本地未保存 DeepSeek 的 API Key：missing"):
        registry.resolve_model(
            model_id,
            legacy_config={},
            credential_name="missing",
        )


def test_dashscope_endpoints_share_one_provider_credential(tmp_path):
    registry = ProviderRegistry(tmp_path)
    registry.ensure_migrated(_legacy_config())

    displayed = registry.update_provider(
        "dashscope",
        {"endpoints": [{"id": "dashscope-native-mm", "api_key": "shared-dashscope-secret"}]},
        legacy_config={},
    )

    dashscope = next(provider for provider in displayed["providers"] if provider["id"] == "dashscope")
    assert dashscope["credential_scope"] == "provider"
    masked_values = {endpoint["api_key_masked"] for endpoint in dashscope["endpoints"]}
    assert len(masked_values) == 1
    assert next(iter(masked_values)).endswith("cret")
    stored_dashscope = next(provider for provider in registry._payload()["providers"] if provider["id"] == "dashscope")
    references = {endpoint["credential_ref"] for endpoint in stored_dashscope["endpoints"]}
    assert references == {"local-file://dashscope-shared"}
    assert registry.resolve_binding("text_embedding", legacy_config={})["api_key"] == "shared-dashscope-secret"
    assert registry.resolve_binding("multimodal_embedding", legacy_config={})["api_key"] == "shared-dashscope-secret"


def test_dashscope_split_credential_migration_prefers_explicit_compatible_key(tmp_path):
    registry = ProviderRegistry(tmp_path)
    registry.ensure_migrated(_legacy_config())
    payload = registry._payload()
    dashscope = next(provider for provider in payload["providers"] if provider["id"] == "dashscope")
    compatible = next(endpoint for endpoint in dashscope["endpoints"] if endpoint["id"] == "dashscope-compatible")
    native = next(endpoint for endpoint in dashscope["endpoints"] if endpoint["id"] == "dashscope-native-mm")
    compatible["credential_ref"] = registry.credentials.put("dashscope-dashscope-compatible", "selected-compatible-secret")
    native["credential_ref"] = "local-file://legacy-dashscope-multimodal"
    registry._save(payload)

    registry.display(legacy_config={})

    migrated = next(provider for provider in registry._payload()["providers"] if provider["id"] == "dashscope")
    references = {endpoint["credential_ref"] for endpoint in migrated["endpoints"]}
    assert references == {"local-file://dashscope-dashscope-compatible"}
    assert registry.resolve_binding("text_embedding", legacy_config={})["api_key"] == "selected-compatible-secret"
    assert registry.resolve_binding("multimodal_embedding", legacy_config={})["api_key"] == "selected-compatible-secret"


def test_legacy_reimport_never_overwrites_user_saved_credential(tmp_path):
    registry = ProviderRegistry(tmp_path)
    registry.ensure_migrated(_legacy_config())
    registry.update_provider(
        "deepseek",
        {"endpoints": [{"id": "deepseek-openai", "base_url": "https://api.deepseek.com", "api_key": "user-provider-secret"}]},
        legacy_config={},
    )

    # config.json regains a legacy api_key (e.g. saved via the old settings
    # UI), so the next load_config() re-runs the legacy importer.  The user's
    # explicit Provider-page credential must survive that re-import, even when
    # the legacy secret belongs to a different endpoint.
    registry.ensure_migrated({"fallback_embedding": {"api_key": "new-legacy-emb-secret"}})

    endpoint = registry._payload()["providers"][0]["endpoints"][0]
    assert endpoint["credential_ref"] == "local-file://deepseek-deepseek-openai"
    assert registry.credentials.get(endpoint["credential_ref"]) == "user-provider-secret"

    # A provider-scoped DashScope credential remains shared even if a stale
    # endpoint-specific legacy key reappears in config.json.
    dashscope = registry._payload()["providers"][1]
    references = {endpoint["credential_ref"] for endpoint in dashscope["endpoints"]}
    assert len(references) == 1
    assert registry.credentials.get(next(iter(references))) == "new-legacy-emb-secret"


def test_binding_rejects_incompatible_model_capability(tmp_path):
    registry = ProviderRegistry(tmp_path)
    registry.ensure_migrated(_legacy_config())
    text_model = registry._payload()["bindings"]["text_embedding"]

    try:
        registry.set_binding("agent", text_model)
    except ValueError as exc:
        assert "requires a llm model" in str(exc)
    else:
        raise AssertionError("incompatible binding must fail before a request")


def test_completed_registry_backfills_image_analyzer_binding(tmp_path):
    registry = ProviderRegistry(tmp_path)
    registry.ensure_migrated(_legacy_config())
    payload = registry._payload()
    agent_model_id = payload["bindings"]["agent"]
    payload["bindings"].pop("image_analyzer")
    registry._save(payload)

    displayed = registry.display(legacy_config={})

    assert displayed["bindings"]["image_analyzer"] == agent_model_id
    assert registry._payload()["bindings"]["image_analyzer"] == agent_model_id


def test_image_analyzer_binding_requires_llm_model(tmp_path):
    registry = ProviderRegistry(tmp_path)
    registry.ensure_migrated(_legacy_config())
    text_model = registry._payload()["bindings"]["text_embedding"]

    with pytest.raises(ValueError, match="requires a llm model"):
        registry.set_binding("image_analyzer", text_model)


def test_llm_model_categories_can_be_added_and_removed_without_name_inference(tmp_path):
    registry = ProviderRegistry(tmp_path)
    registry.ensure_migrated(_legacy_config())

    created = registry.upsert_model(
        "dashscope",
        {
            "endpoint_id": "dashscope-compatible",
            "capability": "llm",
            "name": "qwen3.7",
            "categories": ["llm", "multimodal_llm"],
        },
    )
    updated = registry.upsert_model(
        "dashscope",
        {
            "endpoint_id": "dashscope-compatible",
            "capability": "llm",
            "name": "qwen3.7",
            "categories": ["multimodal_llm"],
        },
    )

    assert created["categories"] == ["llm", "multimodal_llm"]
    assert updated["id"] == created["id"]
    assert updated["categories"] == ["multimodal_llm"]


def test_model_categories_cannot_mix_runtime_capabilities(tmp_path):
    registry = ProviderRegistry(tmp_path)
    registry.ensure_migrated(_legacy_config())

    with pytest.raises(ValueError, match="incompatible with llm"):
        registry.upsert_model(
            "dashscope",
            {
                "endpoint_id": "dashscope-compatible",
                "capability": "llm",
                "name": "mixed-model",
                "categories": ["llm", "text_embedding"],
            },
        )


def test_config_migration_moves_plaintext_key_outside_repository(tmp_path, monkeypatch):
    import config
    import provider_registry

    config_path = tmp_path / "repo" / "config.json"
    config_path.parent.mkdir()
    legacy = _legacy_config()
    config_path.write_text(json.dumps(legacy), encoding="utf-8")
    monkeypatch.setattr(config, "CONFIG_FILE", config_path)
    monkeypatch.setenv("PUDDINGDATA_USER_DATA_DIR", str(tmp_path / "user-data"))
    monkeypatch.setattr(provider_registry, "_default_registry_instance", None)

    loaded = config.load_config()
    saved = json.loads(config_path.read_text(encoding="utf-8"))

    assert loaded["fallback_llm"]["api_key"] == ""
    assert saved["fallback_llm"]["api_key"] == ""
    assert "deepseek-test-secret" not in config_path.read_text(encoding="utf-8")
    credentials = (tmp_path / "user-data" / "credentials.json").read_text(encoding="utf-8")
    assert "deepseek-test-secret" in credentials
    if os.name != "nt":
        assert stat.S_IMODE((tmp_path / "user-data" / "credentials.json").stat().st_mode) == 0o600
