from __future__ import annotations

import json

import pytest

import config


@pytest.mark.parametrize(
    "legacy_key, value",
    [
        ("sandbox_mode", "auto"),
        ("docker_enabled", True),
        ("on_unavailable", "fallback"),
    ],
)
def test_removed_terminal_execution_fields_fail_closed_at_config_load(
    tmp_path,
    monkeypatch,
    legacy_key,
    value,
):
    config_path = tmp_path / "config.json"
    config_path.write_text(
        json.dumps({"harness": {"terminal": {legacy_key: value}}}),
        encoding="utf-8",
    )
    monkeypatch.setattr(config, "CONFIG_FILE", config_path)

    with pytest.raises(config.UnsupportedTerminalExecutionConfig):
        config.load_config()


def test_new_terminal_execution_mode_loads_without_legacy_translation(tmp_path, monkeypatch):
    config_path = tmp_path / "config.json"
    config_path.write_text(
        json.dumps({"harness": {"terminal": {"execution_mode": "kernel"}}}),
        encoding="utf-8",
    )
    monkeypatch.setattr(config, "CONFIG_FILE", config_path)

    loaded = config.load_config()

    assert loaded["harness"]["terminal"]["execution_mode"] == "kernel"
