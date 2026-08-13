from __future__ import annotations

import json

from extensions import (
    disabled_extension_for_api_path,
    extension_disabled_payload,
    extension_enabled,
    extension_states,
    runtime_profile,
)
from tools.toolsets import agent_custom_tool_names
from provider_registry import _default_registry


def test_source_checkout_defaults_to_all_extensions() -> None:
    assert extension_states({}) == {
        "knowledge": True,
        "analytics": True,
        "headless_worker": True,
    }


def test_explicit_extension_environment_overrides_json_contract() -> None:
    environ = {
        "PUDDINGCLAW_EXTENSIONS": json.dumps(
            {"knowledge": True, "analytics": True, "headless_worker": False}
        ),
        "PUDDINGCLAW_EXTENSION_KNOWLEDGE": "0",
    }
    assert extension_enabled("knowledge", environ) is False
    assert extension_enabled("analytics", environ) is True
    assert extension_enabled("headless_worker", environ) is False


def test_runtime_profile_is_non_secret() -> None:
    profile = runtime_profile(
        {
            "PUDDINGCLAW_PROFILE": "harness",
            "PUDDINGCLAW_EXTENSION_KNOWLEDGE": "0",
            "PUDDINGCLAW_EXTENSION_ANALYTICS": "0",
            "PUDDINGCLAW_EXTENSION_HEADLESS_WORKER": "0",
            "PUDDINGCLAW_INITIAL_PROVIDER_API_KEY": "must-not-leak",
        }
    )
    assert profile == {
        "schema_version": 1,
        "profile": "harness",
        "extensions": {"knowledge": False, "analytics": False, "headless_worker": False},
    }


def test_disabled_extension_paths_have_stable_boundary() -> None:
    environ = {
        "PUDDINGCLAW_EXTENSION_KNOWLEDGE": "0",
        "PUDDINGCLAW_EXTENSION_ANALYTICS": "0",
        "PUDDINGCLAW_EXTENSION_HEADLESS_WORKER": "0",
    }
    assert disabled_extension_for_api_path("/api/knowledge", environ) == "knowledge"
    assert disabled_extension_for_api_path("/api/read-later/items", environ) == "knowledge"
    assert disabled_extension_for_api_path("/api/analytics/query", environ) == "analytics"
    assert disabled_extension_for_api_path("/api/headless/runs", environ) == "headless_worker"
    assert disabled_extension_for_api_path("/api/agent", environ) is None


def test_disabled_extension_payload_is_actionable_and_compatible() -> None:
    assert extension_disabled_payload("knowledge") == {
        "code": "extension_disabled",
        "error_code": "extension_disabled",
        "extension": "knowledge",
        "message": "知识库功能尚未启用，请运行 puddingclaw init 进行配置",
    }
    assert extension_disabled_payload("analytics")["message"].startswith("智能问数功能尚未启用")


def test_harness_only_removes_business_extension_tools(monkeypatch) -> None:
    monkeypatch.setenv("PUDDINGCLAW_EXTENSION_KNOWLEDGE", "0")
    monkeypatch.setenv("PUDDINGCLAW_EXTENSION_ANALYTICS", "0")
    names = agent_custom_tool_names()
    assert "llamaindex_knowledge_query" not in names
    assert "llm_wiki_query" not in names
    assert "database_sql_execute" not in names
    assert "read_later_save_url" not in names
    assert "inspect_skill" in names
    assert "read_resource" in names


def test_init_provider_bootstrap_keeps_secret_in_environment_reference(monkeypatch) -> None:
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
    monkeypatch.setenv("PUDDINGCLAW_INITIAL_PROVIDER_API_KEY", "secret-value")
    payload = _default_registry()
    assert payload["bindings"]["agent"].startswith("initial-deepseek:")
    provider = next(item for item in payload["providers"] if item["id"] == "initial-deepseek")
    assert provider["credentials"]["default"] == "env://PUDDINGCLAW_INITIAL_PROVIDER_API_KEY"
    assert "secret-value" not in json.dumps(payload)
