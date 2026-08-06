import json
from pathlib import Path

import provider_registry
from evaluation.settings import EvaluationSettingsStore


def test_langsmith_secret_is_stored_outside_evaluation_settings(tmp_path: Path, monkeypatch):
    monkeypatch.delenv("LANGSMITH_API_KEY", raising=False)
    credential_root = tmp_path / "user-data"
    monkeypatch.setattr(provider_registry, "user_data_dir", lambda: credential_root)
    settings_path = tmp_path / "evaluation-settings.json"
    store = EvaluationSettingsStore(settings_path)

    public = store.update({"enabled": True, "api_key": "lsv2_secret_value"})

    serialized = settings_path.read_text(encoding="utf-8")
    assert "lsv2_secret_value" not in serialized
    assert json.loads(serialized)["api_key_ref"] == "local-file://puddingclaw-langsmith"
    assert public["api_key_configured"] is True
    assert store.load().api_key == "lsv2_secret_value"
    assert "lsv2_secret_value" not in str(public)


def test_explicit_evaluation_key_wins_over_process_environment(tmp_path: Path, monkeypatch):
    credential_root = tmp_path / "user-data"
    monkeypatch.setattr(provider_registry, "user_data_dir", lambda: credential_root)
    monkeypatch.setenv("LANGSMITH_API_KEY", "lsv2_old_environment_key")
    store = EvaluationSettingsStore(tmp_path / "evaluation-settings.json")

    store.update({"api_key": "lsv2_new_ui_key", "enabled": True})

    assert store.load().api_key == "lsv2_new_ui_key"
    assert store.public()["api_key_masked"].endswith("_key")


def test_cleared_explicit_key_does_not_fall_back_to_environment(tmp_path: Path, monkeypatch):
    credential_root = tmp_path / "user-data"
    monkeypatch.setattr(provider_registry, "user_data_dir", lambda: credential_root)
    monkeypatch.setenv("LANGSMITH_API_KEY", "lsv2_environment_key")
    store = EvaluationSettingsStore(tmp_path / "evaluation-settings.json")
    store.update({"api_key": "lsv2_ui_key"})

    store.update({}, clear_api_key=True)

    assert store.load().api_key == ""
    assert store.public()["api_key_configured"] is False
