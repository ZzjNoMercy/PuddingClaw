from __future__ import annotations

import json

import httpx
import pytest

from provider_registry import (
    DASHSCOPE_NATIVE_MODEL_CATALOG,
    DEFAULT_AGENT_MODEL,
    ProviderRegistry,
    _default_registry,
)


def _configured_registry(tmp_path) -> ProviderRegistry:
    registry = ProviderRegistry(tmp_path)
    registry.update_provider(
        "deepseek",
        {"credentials": [{"name": "default", "value": "deepseek-test-secret"}]},
    )
    registry.update_provider(
        "dashscope",
        {"credentials": [{"name": "default", "value": "dashscope-text-secret"}]},
    )
    return registry


def test_product_default_agent_uses_flash():
    assert DEFAULT_AGENT_MODEL["model"] == "deepseek-v4-flash"
    assert DEFAULT_AGENT_MODEL["thinking"]["model"] == "deepseek-v4-flash"


def test_fresh_registry_has_canonical_models_and_bindings(tmp_path):
    registry = _configured_registry(tmp_path)

    assert "migration" not in registry._payload()
    assert registry.resolve_binding("agent")["id"] == "deepseek:deepseek-openai:deepseek-v4-flash:llm"
    assert registry.resolve_binding("agent")["categories"] == ["llm"]
    assert registry.resolve_binding("image_analyzer")["id"] == "dashscope:dashscope-compatible:qwen3-7-plus:llm"
    assert registry.resolve_binding("text_embedding")["dimension"] == 1024
    multimodal = registry.resolve_binding("multimodal_embedding")
    assert multimodal["api_key"] == "dashscope-text-secret"
    assert multimodal["protocol"] == "dashscope_multimodal_embedding"


def test_legacy_init_provider_is_merged_into_builtin_provider(tmp_path, monkeypatch):
    monkeypatch.delenv("PUDDINGCLAW_INITIAL_PROVIDER", raising=False)
    payload = _default_registry()
    payload["providers"].insert(
        0,
        {
            "id": "initial-deepseek",
            "name": "DeepSeek",
            "enabled": True,
            "credentials": {"default": "env://PUDDINGCLAW_INITIAL_PROVIDER_API_KEY"},
            "endpoints": [
                {
                    "id": "initial-deepseek-initial",
                    "protocol": "deepseek",
                    "base_url": "https://api.deepseek.com",
                    "credential_ref": "env://PUDDINGCLAW_INITIAL_PROVIDER_API_KEY",
                    "capabilities": ["llm"],
                }
            ],
            "models": [
                {
                    "id": "initial-deepseek:initial-deepseek-initial:deepseek-chat:llm",
                    "name": "deepseek-chat",
                    "endpoint_id": "initial-deepseek-initial",
                    "capability": "llm",
                    "categories": ["llm"],
                }
            ],
        },
    )
    payload["bindings"]["agent"] = "initial-deepseek:initial-deepseek-initial:deepseek-chat:llm"
    (tmp_path / "providers.json").write_text(json.dumps(payload), encoding="utf-8")

    migrated = ProviderRegistry(tmp_path)._payload()

    assert not any(provider["id"] == "initial-deepseek" for provider in migrated["providers"])
    assert [provider["id"] for provider in migrated["providers"]].count("deepseek") == 1
    assert migrated["bindings"]["agent"] == "deepseek:deepseek-openai:deepseek-chat:llm"
    assert json.loads((tmp_path / "providers.json").read_text(encoding="utf-8")) == migrated


def test_cli_init_bootstrap_updates_existing_registry_once_per_generation(tmp_path, monkeypatch):
    monkeypatch.delenv("PUDDINGCLAW_INITIAL_PROVIDER", raising=False)
    monkeypatch.delenv("PUDDINGCLAW_INITIAL_MULTIMODAL_PROVIDER", raising=False)
    payload = _default_registry()
    (tmp_path / "providers.json").write_text(json.dumps(payload), encoding="utf-8")
    monkeypatch.setenv("PUDDINGCLAW_INITIAL_PROVIDER_BOOTSTRAP_ID", "init-generation-1")
    monkeypatch.setenv(
        "PUDDINGCLAW_INITIAL_PROVIDER",
        json.dumps(
            {
                "status": "configured",
                "id": "deepseek",
                "name": "DeepSeek",
                "protocol": "deepseek",
                "base_url": "https://api.deepseek.com",
                "model": "deepseek-chat",
            }
        ),
    )
    monkeypatch.setenv(
        "PUDDINGCLAW_INITIAL_MULTIMODAL_PROVIDER",
        json.dumps(
            {
                "status": "configured",
                "id": "vision-provider",
                "name": "Vision Provider",
                "protocol": "openai_compatible",
                "base_url": "https://vision.example.com/v1",
                "model": "vision-model",
                "reuse_primary_credential": False,
            }
        ),
    )
    registry = ProviderRegistry(tmp_path)

    first = registry._payload()

    assert first["bindings"]["agent"] == "deepseek:deepseek-openai:deepseek-chat:llm"
    assert first["bindings"]["image_analyzer"] == (
        "vision-provider:vision-provider-openai:vision-model:llm"
    )
    assert json.loads((tmp_path / ".cli-provider-bootstrap.json").read_text())["bootstrap_id"] == (
        "init-generation-1"
    )

    first["bindings"]["image_analyzer"] = "dashscope:dashscope-compatible:qwen3-7-plus:llm"
    (tmp_path / "providers.json").write_text(json.dumps(first), encoding="utf-8")
    unchanged = registry._payload()
    assert unchanged["bindings"]["image_analyzer"] == (
        "dashscope:dashscope-compatible:qwen3-7-plus:llm"
    )

    monkeypatch.setenv("PUDDINGCLAW_INITIAL_PROVIDER_BOOTSTRAP_ID", "init-generation-2")
    reapplied = registry._payload()
    assert reapplied["bindings"]["image_analyzer"] == (
        "vision-provider:vision-provider-openai:vision-model:llm"
    )


def test_display_never_contains_plaintext_credentials(tmp_path):
    registry = _configured_registry(tmp_path)

    rendered = json.dumps(registry.display(), ensure_ascii=False)

    assert "deepseek-test-secret" not in rendered
    assert "dashscope-text-secret" not in rendered


def test_explicit_conversation_model_uses_configured_endpoint(tmp_path):
    registry = _configured_registry(tmp_path)
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

    resolved = registry.resolve_model(model["id"])

    assert resolved["provider_id"] == "kimi"
    assert resolved["api_key"] == "kimi-test-secret"
    assert resolved["thinking_profile"]["levels"] == ["low", "high", "max"]


def test_environment_reference_is_not_copied_to_credential_file(tmp_path, monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "env-only-secret")
    registry = ProviderRegistry(tmp_path)

    assert not registry.credentials.path.exists()
    assert registry.resolve_binding("agent")["api_key"] == "env-only-secret"


def test_endpoint_connectivity_probe_discards_model_list(tmp_path, monkeypatch):
    registry = _configured_registry(tmp_path)
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
    registry = _configured_registry(tmp_path)
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
    registry = _configured_registry(tmp_path)
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
    registry = _configured_registry(tmp_path)
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
    assert endpoint["credential_source"] == "vault"
    assert endpoint["api_key_masked"].endswith("cret")
    assert "saved-provider-secret" not in json.dumps(displayed)

    reloaded = ProviderRegistry(tmp_path)
    reloaded_endpoint = reloaded._payload()["providers"][0]["endpoints"][0]
    assert reloaded.credentials.get(reloaded_endpoint["credential_ref"]) == "saved-provider-secret"


def test_provider_supports_named_credentials_with_default_fallback(tmp_path):
    registry = _configured_registry(tmp_path)
    model_id = registry._payload()["bindings"]["agent"]

    displayed = registry.update_provider(
        "deepseek",
        {
            "credentials": [
                {"name": "default", "value": "primary-secret"},
                {"name": "evaluation", "value": "evaluation-secret"},
            ]
        },
    )

    provider = next(item for item in displayed["providers"] if item["id"] == "deepseek")
    assert [item["name"] for item in provider["api_keys"]] == ["default", "evaluation"]
    assert all(item["credential_configured"] for item in provider["api_keys"])
    assert "primary-secret" not in json.dumps(displayed)
    assert "evaluation-secret" not in json.dumps(displayed)
    assert registry.resolve_model(model_id)["api_key"] == "primary-secret"
    evaluated = registry.resolve_model(
        model_id,
        credential_name="evaluation",
    )
    assert evaluated["api_key"] == "evaluation-secret"
    assert evaluated["credential_name"] == "evaluation"
    assert registry.resolve_credential_for_runtime("deepseek", "evaluation") == "evaluation-secret"


def test_unknown_named_credential_is_rejected(tmp_path):
    registry = _configured_registry(tmp_path)
    model_id = registry._payload()["bindings"]["agent"]

    with pytest.raises(ValueError, match="本地未保存 DeepSeek 的 API Key：missing"):
        registry.resolve_model(
            model_id,
            credential_name="missing",
        )


def test_dashscope_endpoints_share_one_provider_credential(tmp_path):
    registry = _configured_registry(tmp_path)

    displayed = registry.update_provider(
        "dashscope",
        {"endpoints": [{"id": "dashscope-native-mm", "api_key": "shared-dashscope-secret"}]},
    )

    dashscope = next(provider for provider in displayed["providers"] if provider["id"] == "dashscope")
    assert dashscope["credential_scope"] == "provider"
    masked_values = {endpoint["api_key_masked"] for endpoint in dashscope["endpoints"]}
    assert len(masked_values) == 1
    assert next(iter(masked_values)).endswith("cret")
    stored_dashscope = next(provider for provider in registry._payload()["providers"] if provider["id"] == "dashscope")
    references = {endpoint["credential_ref"] for endpoint in stored_dashscope["endpoints"]}
    assert references == {"vault://users/local/credentials/dashscope-shared"}
    assert registry.resolve_binding("text_embedding")["api_key"] == "shared-dashscope-secret"
    assert registry.resolve_binding("multimodal_embedding")["api_key"] == "shared-dashscope-secret"


def test_registry_rejects_retired_migration_metadata(tmp_path):
    payload = _default_registry()
    payload["migration"] = {"state": "complete"}
    (tmp_path / "providers.json").write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ValueError, match="Unsupported Provider Registry fields: migration"):
        ProviderRegistry(tmp_path).display()


def test_registry_rejects_unsupported_version(tmp_path):
    payload = _default_registry()
    payload["version"] = 1
    (tmp_path / "providers.json").write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ValueError, match="Unsupported Provider Registry version: 1"):
        ProviderRegistry(tmp_path).display()


def test_binding_rejects_incompatible_model_capability(tmp_path):
    registry = _configured_registry(tmp_path)
    text_model = registry._payload()["bindings"]["text_embedding"]

    try:
        registry.set_binding("agent", text_model)
    except ValueError as exc:
        assert "requires a llm model" in str(exc)
    else:
        raise AssertionError("incompatible binding must fail before a request")


def test_registry_rejects_missing_required_binding(tmp_path):
    payload = _default_registry()
    payload["bindings"].pop("image_analyzer")
    (tmp_path / "providers.json").write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ValueError, match="Provider Registry is missing bindings: image_analyzer"):
        ProviderRegistry(tmp_path).display()


def test_image_analyzer_binding_requires_llm_model(tmp_path):
    registry = _configured_registry(tmp_path)
    text_model = registry._payload()["bindings"]["text_embedding"]

    with pytest.raises(ValueError, match="requires a llm model"):
        registry.set_binding("image_analyzer", text_model)


def test_llm_model_categories_can_be_added_and_removed_without_name_inference(tmp_path):
    registry = _configured_registry(tmp_path)

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
    registry = _configured_registry(tmp_path)

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


def test_invalid_registry_json_fails_fast(tmp_path):
    (tmp_path / "providers.json").write_text("{", encoding="utf-8")

    with pytest.raises(ValueError, match="Invalid Provider Registry JSON"):
        ProviderRegistry(tmp_path).display()
