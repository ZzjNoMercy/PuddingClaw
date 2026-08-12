"""读取本地 Higress 配置，提取 AI 路由模型列表。

Higress all-in-one 将 K8s 资源以 YAML 形式持久化在用户 Home 的
``infrastructure/higress``（或显式 ``HIGRESS_DATA_DIR``）。backend 直接读取这些文件，无需访问
Higress apiserver 的 18443 端口。
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

from runtime_identity.paths import PuddingClawPaths

logger = logging.getLogger(__name__)

def _default_higress_data_dir() -> Path:
    """Return the default Higress data directory.

    Priority:
    1. HIGRESS_DATA_DIR environment variable
    2. PUDDINGCLAW_HOME/infrastructure/higress
    """
    if env_dir := os.getenv("HIGRESS_DATA_DIR"):
        return Path(env_dir).expanduser().resolve(strict=False)
    return PuddingClawPaths.from_environment().infrastructure() / "higress"


DEFAULT_HIGRESS_DATA_DIR = _default_higress_data_dir()
INGRESSES_DIR = DEFAULT_HIGRESS_DATA_DIR / "ingresses"


def _safe_load_yaml(path: Path) -> dict[str, Any] | None:
    """安全加载 YAML 文件，失败时返回 None。"""
    try:
        import yaml

        return yaml.safe_load(path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        logger.debug("[higress_config_reader] failed to load %s: %s", path, exc)
        return None


def _extract_models_from_ingress(ingress: dict[str, Any]) -> list[str]:
    """从单个 Ingress 资源中提取 AI 路由匹配的模型名。"""
    models: list[str] = []
    metadata = ingress.get("metadata", {}) or {}
    annotations = metadata.get("annotations", {}) or {}

    # Higress AI Route 可用 exact-match-header 或 prefix-match-header 注解匹配 model header
    header_keys = (
        "higress.io/exact-match-header-x-higress-llm-model",
        "higress.io/prefix-match-header-x-higress-llm-model",
    )
    for header_key in header_keys:
        raw = annotations.get(header_key, "")
        if raw:
            # 逗号分隔表示多个模型名
            for model in str(raw).split(","):
                model = model.strip()
                if model:
                    models.append(model)

    # 兜底：也尝试从 ConfigMap 的 ai-route 数据里读取
    return models


def get_higress_routed_models(data_dir: Path | str | None = None, *, include_embeddings: bool = False) -> list[str]:
    """返回 Higress 当前配置中所有 AI 路由模型名。

    Args:
        data_dir: Higress 数据目录，默认 PUDDINGCLAW_HOME/infrastructure/higress
        include_embeddings: 是否包含 embeddings 路由（path=/v1/embeddings）的模型

    Returns:
        模型名列表，按发现顺序去重。
    """
    base = Path(data_dir) if data_dir else DEFAULT_HIGRESS_DATA_DIR
    ingresses_dir = base / "ingresses"

    if not ingresses_dir.exists():
        logger.warning("[higress_config_reader] ingresses dir not found: %s", ingresses_dir)
        return []

    embedding_models: set[str] = set()
    if not include_embeddings:
        for route in get_higress_routes(base):
            if route.get("path", "/").startswith("/v1/embeddings"):
                embedding_models.add(route["model"])

    models: list[str] = []
    seen: set[str] = set()

    for path in ingresses_dir.glob("*.yaml"):
        ingress = _safe_load_yaml(path)
        if not ingress or ingress.get("kind") != "Ingress":
            continue

        for model in _extract_models_from_ingress(ingress):
            if model in seen:
                continue
            if not include_embeddings and model in embedding_models:
                continue
            seen.add(model)
            models.append(model)

    return models


def get_higress_routes(data_dir: Path | str | None = None) -> list[dict[str, str]]:
    """返回 Higress AI 路由的详细信息。

    Returns:
        每条路由包含 name、model、destination、path
    """
    base = Path(data_dir) if data_dir else DEFAULT_HIGRESS_DATA_DIR
    ingresses_dir = base / "ingresses"

    if not ingresses_dir.exists():
        return []

    routes: list[dict[str, str]] = []

    for path in ingresses_dir.glob("*.yaml"):
        ingress = _safe_load_yaml(path)
        if not ingress or ingress.get("kind") != "Ingress":
            continue

        metadata = ingress.get("metadata", {}) or {}
        annotations = metadata.get("annotations", {}) or {}
        spec = ingress.get("spec", {}) or {}
        rules = spec.get("rules", []) or []
        paths = rules[0].get("http", {}).get("paths", []) if rules else []
        route_path = paths[0].get("path", "/") if paths else "/"

        raw_models = ""
        for header_key in (
            "higress.io/exact-match-header-x-higress-llm-model",
            "higress.io/prefix-match-header-x-higress-llm-model",
        ):
            raw_models = annotations.get(header_key, "")
            if raw_models:
                break
        if not raw_models:
            continue

        for model in str(raw_models).split(","):
            model = model.strip()
            if not model:
                continue
            routes.append({
                "name": metadata.get("name", ""),
                "model": model,
                "destination": annotations.get("higress.io/destination", ""),
                "path": route_path,
            })

    return routes


def _as_token_list(raw_tokens: Any) -> list[str]:
    """Normalize Higress ai-proxy apiTokens into a flat list.

    Higress may persist tokens as a plain string, comma-separated string, list
    of strings, or list of token objects depending on console/version. This
    helper intentionally never logs token values.
    """

    if raw_tokens is None:
        return []
    if isinstance(raw_tokens, str):
        return [item.strip() for item in raw_tokens.split(",") if item.strip()]
    if isinstance(raw_tokens, list):
        tokens: list[str] = []
        for item in raw_tokens:
            if isinstance(item, str) and item.strip():
                tokens.append(item.strip())
            elif isinstance(item, dict):
                for key in ("token", "value", "apiKey", "api_key", "key"):
                    value = item.get(key)
                    if isinstance(value, str) and value.strip():
                        tokens.append(value.strip())
                        break
        return tokens
    if isinstance(raw_tokens, dict):
        for key in ("token", "value", "apiKey", "api_key", "key"):
            value = raw_tokens.get(key)
            if isinstance(value, str) and value.strip():
                return [value.strip()]
    return []


def get_higress_dashscope_api_key(data_dir: Path | str | None = None) -> str:
    """Return the DashScope/Qwen provider token from local Higress ai-proxy config.

    This is a local-development convenience fallback for backend components that
    need to call DashScope directly (for example Qwen-VL multimodal embedding).
    It reads only the project-local mounted Higress YAML and returns an empty
    string when the provider/token cannot be found.
    """

    base = Path(data_dir) if data_dir else DEFAULT_HIGRESS_DATA_DIR
    plugin_path = base / "wasmplugins" / "ai-proxy.internal.yaml"
    plugin = _safe_load_yaml(plugin_path)
    if not plugin:
        return ""

    spec = plugin.get("spec", {}) or {}
    default_config = spec.get("defaultConfig", {}) or {}
    providers = default_config.get("providers", []) or []
    for provider in providers:
        if not isinstance(provider, dict):
            continue
        provider_type = str(provider.get("type", "")).lower()
        provider_id = str(provider.get("id", "")).lower()
        if provider_type not in {"qwen", "dashscope"} and "qwen" not in provider_id and "multi" not in provider_id:
            continue
        tokens = _as_token_list(provider.get("apiTokens"))
        if tokens:
            return tokens[0]

    return ""
