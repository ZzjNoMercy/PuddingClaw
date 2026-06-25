"""读取本地 Higress 配置，提取 AI 路由模型列表。

Higress all-in-one 将 K8s 资源以 YAML 形式持久化在 /app/data/higress
（通过 docker-compose 挂载）。backend 直接读取这些文件，无需访问
Higress apiserver 的 18443 端口。
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

DEFAULT_HIGRESS_DATA_DIR = Path("/app/data/higress")
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

    # Higress AI Route 通常用 exact-match-header 注解匹配 model header
    header_key = "higress.io/exact-match-header-x-higress-llm-model"
    raw = annotations.get(header_key, "")
    if raw:
        # 逗号分隔表示多个模型名
        for model in str(raw).split(","):
            model = model.strip()
            if model:
                models.append(model)

    # 兜底：也尝试从 ConfigMap 的 ai-route 数据里读取
    return models


def get_higress_routed_models(data_dir: Path | str | None = None) -> list[str]:
    """返回 Higress 当前配置中所有 AI 路由模型名。

    Args:
        data_dir: Higress 数据目录，默认 /app/data/higress

    Returns:
        模型名列表，按发现顺序去重。
    """
    base = Path(data_dir) if data_dir else DEFAULT_HIGRESS_DATA_DIR
    ingresses_dir = base / "ingresses"

    if not ingresses_dir.exists():
        logger.warning("[higress_config_reader] ingresses dir not found: %s", ingresses_dir)
        return []

    models: list[str] = []
    seen: set[str] = set()

    for path in ingresses_dir.glob("*.yaml"):
        ingress = _safe_load_yaml(path)
        if not ingress or ingress.get("kind") != "Ingress":
            continue

        for model in _extract_models_from_ingress(ingress):
            if model not in seen:
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

        header_key = "higress.io/exact-match-header-x-higress-llm-model"
        raw_models = annotations.get(header_key, "")
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
