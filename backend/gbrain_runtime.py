"""Resolve the user-installed gbrain CLI outside an interactive shell PATH."""

from __future__ import annotations

import os
import shutil
from collections.abc import Mapping
from pathlib import Path
from typing import Any

_GBRAIN_PROVIDER_ALIASES = {
    "dashscope": "dashscope",
    "qwen": "dashscope",
    "aliyun": "dashscope",
    "deepseek": "deepseek",
    "openai": "openai",
    "anthropic": "anthropic",
    "kimi": "moonshot",
    "moonshot": "moonshot",
    "zhipu": "zhipu",
    "zai": "zhipu",
    "zai-org": "zhipu",
    "openrouter": "openrouter",
    "google": "google",
    "gemini": "google",
    "ollama": "ollama",
}

_GBRAIN_PROVIDER_KEYS = {
    "dashscope": "DASHSCOPE_API_KEY",
    "deepseek": "DEEPSEEK_API_KEY",
    "openai": "OPENAI_API_KEY",
    "anthropic": "ANTHROPIC_API_KEY",
    "moonshot": "MOONSHOT_API_KEY",
    "zhipu": "ZHIPUAI_API_KEY",
    "openrouter": "OPENROUTER_API_KEY",
    "google": "GOOGLE_GENERATIVE_AI_API_KEY",
}


def _executable(candidate: Path) -> str | None:
    expanded = candidate.expanduser()
    if expanded.is_file() and os.access(expanded, os.X_OK):
        # Keep the executable entry path instead of resolving symlinks. Bun's
        # global gbrain command is commonly ~/.bun/bin/gbrain -> src/cli.ts;
        # its parent directory must remain available so /usr/bin/env finds bun.
        return str(expanded.absolute())
    return None


def resolve_gbrain_binary(*, home: Path | None = None) -> str | None:
    """Return the configured or discovered gbrain executable.

    macOS GUI applications commonly start without the user's interactive shell
    PATH. Bun's default global bin directory is therefore checked explicitly,
    while an explicit PUDDINGCLAW_GBRAIN_BIN remains authoritative.
    """

    configured = os.getenv("PUDDINGCLAW_GBRAIN_BIN", "").strip()
    if configured:
        configured_path = Path(configured).expanduser()
        if configured_path.is_absolute() or "/" in configured:
            return _executable(configured_path)
        return shutil.which(configured)

    discovered = shutil.which("gbrain")
    if discovered:
        return str(Path(discovered).absolute())

    user_home = (home or Path.home()).expanduser()
    candidates: list[Path] = []
    bun_install = os.getenv("BUN_INSTALL", "").strip()
    if bun_install:
        candidates.append(Path(bun_install).expanduser() / "bin" / "gbrain")
    candidates.extend(
        [
            user_home / ".bun" / "bin" / "gbrain",
            user_home / ".local" / "bin" / "gbrain",
            Path("/opt/homebrew/bin/gbrain"),
            Path("/usr/local/bin/gbrain"),
        ]
    )
    for candidate in candidates:
        executable = _executable(candidate)
        if executable:
            return executable
    return None


def gbrain_subprocess_environment(
    binary: str,
    base: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Return an environment that can execute a Bun-backed gbrain command."""

    environment = dict(base or os.environ)
    binary_dir = str(Path(binary).expanduser().absolute().parent)
    path_entries = [entry for entry in environment.get("PATH", "").split(os.pathsep) if entry]
    if binary_dir not in path_entries:
        path_entries.insert(0, binary_dir)
    environment["PATH"] = os.pathsep.join(path_entries)
    return environment


def _gbrain_provider(model: Mapping[str, Any]) -> str:
    provider_id = str(model.get("provider_id") or "").strip().lower()
    if provider_id in _GBRAIN_PROVIDER_ALIASES:
        return _GBRAIN_PROVIDER_ALIASES[provider_id]
    for alias, provider in _GBRAIN_PROVIDER_ALIASES.items():
        if alias and alias in provider_id:
            return provider
    protocol = str(model.get("protocol") or "").strip().lower()
    if protocol in {"openai", "openai_compatible", "deepseek"}:
        return "openai"
    raise ValueError(
        f"模型服务 {provider_id or 'unknown'} 暂不能映射到 gbrain Provider Recipe"
    )


def resolve_gbrain_ai_runtime() -> dict[str, Any]:
    """Resolve gbrain model ids, credentials and endpoint overrides.

    Credentials stay in the subprocess environment; the returned public model
    summaries deliberately omit them when exposed by callers.
    """

    from config import get_llm_wiki_gbrain_config

    resolved = get_llm_wiki_gbrain_config()
    embedding = resolved["embedding"]
    think = resolved["think"]
    embedding_provider = _gbrain_provider(embedding)
    think_provider = _gbrain_provider(think)
    environment: dict[str, str] = {}
    base_urls: dict[str, str] = {}

    for provider, model in ((embedding_provider, embedding), (think_provider, think)):
        api_key = str(model.get("api_key") or "")
        env_key = _GBRAIN_PROVIDER_KEYS.get(provider)
        if env_key:
            existing_key = environment.get(env_key)
            if existing_key and existing_key != api_key:
                raise ValueError(f"gbrain Provider {provider} 收到了冲突的 API Key")
            if not api_key:
                raise ValueError(f"gbrain Provider {provider} 尚未配置 API Key")
            environment[env_key] = api_key
        base_url = str(model.get("base_url") or "").strip().rstrip("/")
        if base_url:
            existing_url = base_urls.get(provider)
            if existing_url and existing_url != base_url:
                raise ValueError(f"gbrain Provider {provider} 收到了冲突的 Base URL")
            base_urls[provider] = base_url

    return {
        "embedding_model": f"{embedding_provider}:{embedding['name']}",
        "embedding_dimensions": int(embedding["dimension"]),
        "chat_model": f"{think_provider}:{think['name']}",
        "environment": environment,
        "provider_base_urls": base_urls,
        "embedding": {
            "model_id": str(embedding.get("id") or ""),
            "name": str(embedding.get("name") or ""),
            "provider": embedding_provider,
            "dimension": int(embedding["dimension"]),
            "uses_default_binding": bool(embedding.get("uses_default_binding")),
        },
        "think": {
            "model_id": str(think.get("id") or ""),
            "name": str(think.get("name") or ""),
            "provider": think_provider,
            "uses_default_binding": bool(think.get("uses_default_binding")),
        },
    }


def apply_gbrain_ai_environment(
    base: Mapping[str, str] | None = None,
) -> tuple[dict[str, str], dict[str, Any]]:
    runtime = resolve_gbrain_ai_runtime()
    environment = dict(base or os.environ)
    environment.update(runtime["environment"])
    return environment, runtime
