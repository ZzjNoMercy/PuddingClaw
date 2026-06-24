"""AI Gateway 与 Provider 设置边界测试。"""

import json

import config


def test_gateway_has_no_key_and_provider_key_is_masked(tmp_path, monkeypatch):
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps({
        "ai_gateway": {
            "enabled": True,
            "base_url": "http://gateway:8080/v1",
            "health_path": "/ready",
            "fallback_to_direct": True,
        },
        "llm": {"api_key": "provider-secret-1234"},
    }), encoding="utf-8")
    monkeypatch.setattr(config, "CONFIG_FILE", config_path)

    displayed = config.get_settings_for_display()
    assert "api_key" not in displayed["ai_gateway"]
    assert "api_key_masked" not in displayed["ai_gateway"]
    assert displayed["llm"]["api_key_masked"].endswith("1234")

    config.update_settings({"ai_gateway": {"enabled": False}})
    saved = json.loads(config_path.read_text(encoding="utf-8"))
    assert saved["ai_gateway"]["enabled"] is False
    assert "api_key" not in saved["ai_gateway"]
