from __future__ import annotations

from pathlib import Path

from gbrain_runtime import (
    gbrain_subprocess_environment,
    resolve_gbrain_ai_runtime,
    resolve_gbrain_binary,
)


def test_resolver_finds_bun_global_binary_without_shell_path(
    tmp_path: Path,
    monkeypatch,
) -> None:
    binary = tmp_path / ".bun" / "bin" / "gbrain"
    binary.parent.mkdir(parents=True)
    binary.write_text("#!/bin/sh\n", encoding="utf-8")
    binary.chmod(0o755)
    monkeypatch.delenv("PUDDINGCLAW_GBRAIN_BIN", raising=False)
    monkeypatch.delenv("BUN_INSTALL", raising=False)
    monkeypatch.setenv("PATH", str(tmp_path / "empty-path"))

    assert resolve_gbrain_binary(home=tmp_path) == str(binary.resolve())


def test_explicit_binary_path_is_authoritative(tmp_path: Path, monkeypatch) -> None:
    binary = tmp_path / "custom-gbrain"
    binary.write_text("#!/bin/sh\n", encoding="utf-8")
    binary.chmod(0o755)
    monkeypatch.setenv("PUDDINGCLAW_GBRAIN_BIN", str(binary))

    assert resolve_gbrain_binary(home=tmp_path) == str(binary.resolve())


def test_bun_symlink_entry_and_parent_path_are_preserved(tmp_path: Path, monkeypatch) -> None:
    target = tmp_path / "source" / "cli.ts"
    target.parent.mkdir(parents=True)
    target.write_text("#!/usr/bin/env bun\n", encoding="utf-8")
    target.chmod(0o755)
    binary = tmp_path / ".bun" / "bin" / "gbrain"
    binary.parent.mkdir(parents=True)
    binary.symlink_to(target)
    monkeypatch.delenv("PUDDINGCLAW_GBRAIN_BIN", raising=False)
    monkeypatch.delenv("BUN_INSTALL", raising=False)
    monkeypatch.setenv("PATH", "/usr/bin:/bin")

    resolved = resolve_gbrain_binary(home=tmp_path)
    assert resolved == str(binary)
    environment = gbrain_subprocess_environment(resolved, {"PATH": "/usr/bin:/bin"})
    assert environment["PATH"].split(":")[0] == str(binary.parent)


def test_gbrain_ai_runtime_maps_registry_models_without_exposing_credentials(monkeypatch) -> None:
    monkeypatch.setattr(
        "config.get_llm_wiki_gbrain_config",
        lambda: {
            "embedding": {
                "id": "dashscope:compatible:embed",
                "name": "text-embedding-v4",
                "provider_id": "dashscope",
                "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
                "api_key": "embedding-secret",
                "dimension": 1024,
                "uses_default_binding": False,
            },
            "think": {
                "id": "deepseek:openai:think",
                "name": "deepseek-v4-pro",
                "provider_id": "deepseek",
                "base_url": "https://api.deepseek.com",
                "api_key": "think-secret",
                "uses_default_binding": False,
            },
        },
    )
    runtime = resolve_gbrain_ai_runtime()
    assert runtime["embedding_model"] == "dashscope:text-embedding-v4"
    assert runtime["embedding_dimensions"] == 1024
    assert runtime["chat_model"] == "deepseek:deepseek-v4-pro"
    assert runtime["environment"] == {
        "DASHSCOPE_API_KEY": "embedding-secret",
        "DEEPSEEK_API_KEY": "think-secret",
    }
    assert "api_key" not in runtime["embedding"]
    assert "api_key" not in runtime["think"]
