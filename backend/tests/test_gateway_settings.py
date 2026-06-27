"""AI Gateway 与 Provider 设置边界测试。"""

import json

import config
from fastapi.testclient import TestClient
from app import app


def test_gateway_has_no_key_and_provider_key_is_masked(tmp_path, monkeypatch):
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps({
        "ai_gateway": {
            "base_url": "http://gateway:8080/v1",
            "health_path": "/ready",
            "fallback_to_direct": True,
        },
        "fallback_llm": {"api_key": "provider-secret-1234"},
    }), encoding="utf-8")
    monkeypatch.setattr(config, "CONFIG_FILE", config_path)

    displayed = config.get_settings_for_display()
    assert "api_key" not in displayed["ai_gateway"]
    assert "api_key_masked" not in displayed["ai_gateway"]
    assert displayed["fallback_llm"]["api_key_masked"].endswith("1234")

    config.update_settings({"ai_gateway": {"base_url": "http://new-gateway:8080/v1"}})
    saved = json.loads(config_path.read_text(encoding="utf-8"))
    assert saved["ai_gateway"]["base_url"] == "http://new-gateway:8080/v1"
    assert "enabled" not in saved["ai_gateway"]


def test_thinking_mode_switches_to_thinking_model(tmp_path, monkeypatch):
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps({
        "thinking_mode": False,
        "gateway_llm": {
            "model": "deepseek-v4-flash",
            "thinking": {
                "model": "deepseek-v4-pro",
                "reasoning_effort": "high",
                "extra_body": {"thinking": {"type": "enabled"}},
            },
        },
        "fallback_llm": {
            "provider": "deepseek",
            "model": "deepseek-v4-flash",
            "thinking": {
                "model": "deepseek-v4-pro",
                "reasoning_effort": "high",
                "extra_body": {"thinking": {"type": "enabled"}},
            },
        },
    }), encoding="utf-8")
    monkeypatch.setattr(config, "CONFIG_FILE", config_path)

    # Off by default
    gateway = config.get_gateway_llm_config()
    fallback = config.get_fallback_llm_config()
    assert gateway["model"] == "deepseek-v4-flash"
    assert gateway["reasoning_effort"] is None
    assert fallback["model"] == "deepseek-v4-flash"
    assert fallback["reasoning_effort"] is None

    # Enable thinking mode
    config.update_settings({"thinking_mode": True})
    gateway = config.get_gateway_llm_config()
    fallback = config.get_fallback_llm_config()
    assert gateway["model"] == "deepseek-v4-pro"
    assert gateway["reasoning_effort"] == "high"
    assert gateway["extra_body"] == {"thinking": {"type": "enabled"}}
    assert fallback["model"] == "deepseek-v4-pro"
    assert fallback["reasoning_effort"] == "high"
    assert fallback["extra_body"] == {"thinking": {"type": "enabled"}}

    # Displayed settings include the flag
    displayed = config.get_settings_for_display()
    assert displayed["thinking_mode"] is True
    saved = json.loads(config_path.read_text(encoding="utf-8"))
    assert saved["thinking_mode"] is True


def test_settings_api_accepts_thinking_mode(tmp_path, monkeypatch):
    """PUT /api/settings must persist the thinking_mode toggle from the dialog."""
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps({
        "thinking_mode": True,
        "gateway_llm": {"model": "deepseek-v4-flash"},
        "fallback_llm": {"model": "deepseek-v4-flash"},
    }), encoding="utf-8")
    monkeypatch.setattr(config, "CONFIG_FILE", config_path)

    client = TestClient(app)
    response = client.put("/api/settings", json={"thinking_mode": False})
    assert response.status_code == 200, response.text

    saved = json.loads(config_path.read_text(encoding="utf-8"))
    assert saved["thinking_mode"] is False

    gateway = config.get_gateway_llm_config()
    fallback = config.get_fallback_llm_config()
    assert gateway["model"] == "deepseek-v4-flash"
    assert gateway["reasoning_effort"] is None
    assert fallback["model"] == "deepseek-v4-flash"
    assert fallback["reasoning_effort"] is None
