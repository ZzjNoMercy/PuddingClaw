"""Filesystem-backed analytics model registry."""

from __future__ import annotations

import io
import json
import re
import zipfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from threading import RLock
from typing import Any, BinaryIO

import yaml


class AnalyticsModelError(ValueError):
    """Raised when analytics model input or filesystem state is invalid."""


SAFE_EXTRA_SUFFIXES = {".md", ".txt", ".json", ".yaml", ".yml", ".html", ".css", ".csv", ".tsv"}
SLUG_RE = re.compile(r"[^0-9A-Za-z_\-\u4e00-\u9fff]+")


@dataclass(frozen=True)
class AnalyticsModel:
    id: str
    name: str
    path: str
    description: str = ""
    version: str = "0.1.0"
    tags: tuple[str, ...] = ()
    data_assets: dict[str, Any] | None = None
    semantic_assets: dict[str, Any] | None = None
    asset_relations: tuple[str, ...] = ()
    guardrails: tuple[str, ...] = ()
    templates: dict[str, Any] | None = None
    default_template: str = ""
    formatter: str = "analytics-model"
    mtime: float = 0.0
    size_bytes: int = 0
    body: str = ""
    frontmatter: dict[str, Any] | None = None

    def to_summary(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "path": self.path,
            "description": self.description,
            "version": self.version,
            "tags": list(self.tags),
            "formatter": self.formatter,
            "data_assets": self.data_assets or {},
            "semantic_assets": self.semantic_assets or {},
            "asset_relations": list(self.asset_relations),
            "guardrails": list(self.guardrails),
            "templates": self.templates or {},
            "default_template": self.default_template,
            "mtime": self.mtime,
            "size_bytes": self.size_bytes,
        }

    def to_detail(self) -> dict[str, Any]:
        data = self.to_summary()
        data.update({"body": self.body, "frontmatter": self.frontmatter or {}})
        return data


def _base_dir_from_here() -> Path:
    return Path(__file__).resolve().parents[2]


def _parse_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    if not text.startswith("---"):
        return {}, text
    parts = text.split("---", 2)
    if len(parts) < 3:
        return {}, text
    raw_meta = yaml.safe_load(parts[1]) or {}
    if not isinstance(raw_meta, dict):
        raw_meta = {}
    return raw_meta, parts[2].lstrip("\n")


def _list_from_meta(value: object) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        return tuple(item.strip() for item in value.split(",") if item.strip())
    if isinstance(value, list):
        return tuple(str(item).strip() for item in value if str(item).strip())
    return ()


def _dict_from_meta(value: object) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _canonical_semantic_ref(value: object, asset_type: str) -> str:
    """Keep model semantic references aligned with registry asset IDs.

    Registry IDs already include their type prefix (for example,
    ``dimension:vehicle_series``). Older UI payloads added that prefix a second
    time, so normalize repeated prefixes here at the persistence boundary.
    """
    ref = str(value).strip()
    prefix = f"{asset_type}:"
    while ref.startswith(prefix + prefix):
        ref = ref[len(prefix) :]
    if ref and not ref.startswith(prefix):
        ref = prefix + ref
    return ref


def _normalize_semantic_assets(value: dict[str, Any] | None) -> dict[str, list[str]]:
    raw = value or {}
    normalized: dict[str, list[str]] = {}
    for field, asset_type in (("measures", "measure"), ("dimensions", "dimension"), ("grains", "grain")):
        seen: set[str] = set()
        items: list[str] = []
        for item in raw.get(field) or []:
            ref = _canonical_semantic_ref(item, asset_type)
            if ref and ref not in seen:
                seen.add(ref)
                items.append(ref)
        normalized[field] = items
    return normalized


def _slugify(value: str) -> str:
    slug = SLUG_RE.sub("-", value.strip()).strip("-_").lower()
    return slug or "analytics_model"


def _ensure_under_root(root: Path, target: Path) -> Path:
    resolved = target.resolve()
    resolved.relative_to(root.resolve())
    return resolved


def _safe_parts(path: str) -> list[str]:
    normalized = path.replace("\\", "/").strip("/")
    parts = [part for part in normalized.split("/") if part and part != "."]
    if not parts or any(part == ".." for part in parts):
        raise AnalyticsModelError(f"Invalid import path: {path}")
    return parts


def _model_relative_path(original_path: str) -> Path | None:
    parts = _safe_parts(original_path)
    if "analytics-models" in parts:
        parts = parts[parts.index("analytics-models") + 1 :]
    if not parts:
        return None
    if len(parts) >= 2 and parts[-1] == "model.md":
        return Path(*parts)
    if len(parts) >= 2 and Path(parts[-1]).suffix.lower() in SAFE_EXTRA_SUFFIXES:
        return Path(*parts)
    return None


class AnalyticsModelRegistry:
    def __init__(self, base_dir: Path | None = None):
        self.base_dir = base_dir or _base_dir_from_here()
        self.root_dir = self.base_dir / "analytics-models"
        self._lock = RLock()
        self._models: dict[str, AnalyticsModel] = {}
        self._last_scanned_at: str | None = None

    def refresh(self) -> dict[str, Any]:
        self.root_dir.mkdir(parents=True, exist_ok=True)
        next_models: dict[str, AnalyticsModel] = {}
        for path in sorted(self.root_dir.glob("**/model.md")):
            model = self._read_model(path)
            next_models[model.id] = model
        with self._lock:
            self._models = next_models
            self._last_scanned_at = datetime.now(tz=timezone.utc).isoformat()
            return self.snapshot()

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            models = [model.to_summary() for model in self._models.values()]
            models.sort(key=lambda item: (item["name"], item["path"]))
            return {
                "models": models,
                "count": len(models),
                "root_dir": str(self.root_dir),
                "last_scanned_at": self._last_scanned_at,
            }

    def list_models(self) -> dict[str, Any]:
        if self._last_scanned_at is None:
            return self.refresh()
        return self.snapshot()

    def get_model(self, model_id: str) -> dict[str, Any]:
        with self._lock:
            model = self._models.get(model_id)
        if not model:
            self.refresh()
            with self._lock:
                model = self._models.get(model_id)
        if not model:
            raise AnalyticsModelError(f"Analytics model not found: {model_id}")
        detail = model.to_detail()
        detail["files"] = self._model_files(model.path)
        return detail

    def get_model_context(self, model_id: str) -> dict[str, Any]:
        model = self.get_model(model_id)
        missing_references: list[str] = []
        relation_context: list[dict[str, Any]] = []
        binding_assets_by_dimension: dict[str, list[str]] = {}
        relation_ids = [str(item).strip() for item in (model.get("asset_relations") or []) if str(item).strip()]
        if relation_ids:
            from analytics.semantic_assets import get_semantic_asset_registry

            assets = get_semantic_asset_registry(self.base_dir)
            for relation_id in relation_ids:
                try:
                    relation = assets.get_asset(relation_id)
                    metadata = relation.get("frontmatter") or {}
                    definition = metadata.get("relation") or {}
                    relation_type = metadata.get("relation_type")
                    relation_context.append(
                        {
                            "id": relation_id,
                            "name": relation.get("name"),
                            "type": relation_type,
                            "definition": definition,
                        }
                    )
                    if relation_type == "dimension_binding" and isinstance(definition, dict):
                        dimension_ref = str((definition.get("dimension") or {}).get("ref") or "").strip()
                        asset_ref = str((definition.get("asset") or {}).get("ref") or "").strip()
                        if dimension_ref and asset_ref:
                            binding_assets_by_dimension.setdefault(dimension_ref, []).append(asset_ref)
                except Exception:
                    missing_references.append(relation_id)
        derived_dimension_paths = [
            {
                "dimension": dimension_ref,
                "assets": sorted(set(asset_refs)),
                "rule": "这些资产通过同一已选维度关联；联合分析必须经由该维度的规范键。",
            }
            for dimension_ref, asset_refs in sorted(binding_assets_by_dimension.items())
            if len(set(asset_refs)) >= 2
        ]
        logical_dataset_context: list[dict[str, Any]] = []
        table_refs = [str(item).strip() for item in ((model.get("frontmatter") or {}).get("data_assets") or {}).get("tables") or []]
        for table_ref in table_refs:
            asset_id = table_ref.removeprefix("table_asset:").strip()
            if not asset_id or "/" in asset_id or "\\" in asset_id:
                continue
            definition_path = self.base_dir / "data" / "analytics-concat-datasets" / asset_id / "dataset.json"
            try:
                definition = json.loads(definition_path.read_text(encoding="utf-8"))
            except Exception:
                continue
            if not isinstance(definition, dict) or definition.get("formatter") != "logical-data-asset":
                continue
            logical_dataset_context.append(
                {
                    "asset_id": asset_id,
                    "name": definition.get("name"),
                    "description": definition.get("description") or "",
                    "tags": definition.get("tags") or [],
                    "kind": definition.get("kind"),
                    "materialization": definition.get("materialization"),
                    "schema": definition.get("schema"),
                    "coverage": definition.get("coverage"),
                    "statistics": definition.get("statistics"),
                    "routing": definition.get("routing"),
                    "sources": [
                        {"asset_id": source.get("asset_id"), "name": source.get("name"), "sheet_name": source.get("sheet_name")}
                        for source in definition.get("sources") or []
                        if isinstance(source, dict)
                    ],
                }
            )
        return {
            "id": model["id"],
            "name": model["name"],
            "version": model.get("version") or "0.1.0",
            "path": model["path"],
            "description": model.get("description") or "",
            "frontmatter": model.get("frontmatter") or {},
            "body": model.get("body") or "",
            "files": model.get("files") or [],
            "missing_references": missing_references,
            "asset_relations": relation_context,
            "derived_dimension_paths": derived_dimension_paths,
            "logical_datasets": logical_dataset_context,
        }

    def create_model(
        self,
        *,
        name: str,
        description: str = "",
        version: str = "0.1.0",
        slug: str | None = None,
        tags: list[str] | None = None,
        data_assets: dict[str, Any] | None = None,
        semantic_assets: dict[str, Any] | None = None,
        asset_relations: list[str] | None = None,
        guardrails: list[str] | None = None,
        templates: dict[str, Any] | None = None,
        default_template: str | None = None,
    ) -> dict[str, Any]:
        clean_name = name.strip()
        if not clean_name:
            raise AnalyticsModelError("name is required")
        model_slug = _slugify(slug or clean_name)
        model_dir = _ensure_under_root(self.root_dir, self.root_dir / model_slug)
        target = _ensure_under_root(self.root_dir, model_dir / "model.md")
        if target.exists():
            raise AnalyticsModelError(f"Analytics model already exists: {model_slug}")
        model_dir.mkdir(parents=True, exist_ok=True)
        (model_dir / "templates").mkdir(exist_ok=True)
        (model_dir / "examples").mkdir(exist_ok=True)
        normalized_semantic_assets = _normalize_semantic_assets(semantic_assets)
        self._validate_asset_graph(
            data_assets=data_assets or {},
            semantic_assets=normalized_semantic_assets,
            asset_relations=asset_relations or [],
        )
        target.write_text(
            self._template(
                model_id=model_slug,
                name=clean_name,
                description=description.strip(),
                version=version.strip() or "0.1.0",
                tags=tags or [],
                data_assets=data_assets or {},
                semantic_assets=normalized_semantic_assets,
                asset_relations=asset_relations or [],
                guardrails=guardrails or [],
                templates=templates or {},
                default_template=default_template or "",
            ),
            encoding="utf-8",
        )
        self.refresh()
        return self.get_model(model_slug)

    def import_zip(self, fileobj: BinaryIO) -> dict[str, Any]:
        data = fileobj.read()
        try:
            archive = zipfile.ZipFile(io.BytesIO(data))
        except zipfile.BadZipFile as exc:
            raise AnalyticsModelError("Invalid ZIP file") from exc

        imported: list[str] = []
        with archive:
            for info in archive.infolist():
                if info.is_dir():
                    continue
                rel = _model_relative_path(info.filename)
                if rel is None:
                    continue
                if Path(rel.name).suffix.lower() not in SAFE_EXTRA_SUFFIXES:
                    continue
                target = _ensure_under_root(self.root_dir, self.root_dir / rel)
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(archive.read(info))
                imported.append(rel.as_posix())
        return self._finish_import(imported)

    def import_files(self, files: list[tuple[str, bytes]]) -> dict[str, Any]:
        imported: list[str] = []
        for name, content in files:
            rel = _model_relative_path(name)
            if rel is None:
                continue
            if Path(rel.name).suffix.lower() not in SAFE_EXTRA_SUFFIXES:
                continue
            target = _ensure_under_root(self.root_dir, self.root_dir / rel)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content)
            imported.append(rel.as_posix())
        return self._finish_import(imported)

    def _finish_import(self, imported: list[str]) -> dict[str, Any]:
        if not any(path.endswith("model.md") for path in imported):
            raise AnalyticsModelError("Import must contain at least one model.md")
        snapshot = self.refresh()
        return {"imported": sorted(set(imported)), "imported_count": len(set(imported)), **snapshot}

    def _read_model(self, path: Path) -> AnalyticsModel:
        text = path.read_text(encoding="utf-8")
        meta, body = _parse_frontmatter(text)
        relative_dir = path.parent.relative_to(self.root_dir).as_posix()
        model_id = str(meta.get("id") or relative_dir).strip() or relative_dir
        stat = path.stat()
        return AnalyticsModel(
            id=model_id,
            name=str(meta.get("name") or path.parent.name),
            path=path.relative_to(self.base_dir).as_posix(),
            description=str(meta.get("description") or "").strip(),
            version=str(meta.get("version") or "0.1.0").strip(),
            tags=_list_from_meta(meta.get("tags")),
            data_assets=_dict_from_meta(meta.get("data_assets")),
            semantic_assets=_dict_from_meta(meta.get("semantic_assets")),
            asset_relations=_list_from_meta(meta.get("asset_relations")),
            guardrails=_list_from_meta(meta.get("guardrails")),
            templates=_dict_from_meta(meta.get("templates")),
            default_template=str(meta.get("default_template") or "").strip(),
            formatter=str(meta.get("formatter") or "analytics-model"),
            mtime=stat.st_mtime,
            size_bytes=stat.st_size,
            body=body,
            frontmatter=meta,
        )

    def _model_files(self, model_path: str) -> list[dict[str, Any]]:
        main_path = _ensure_under_root(self.root_dir, self.base_dir / model_path)
        model_dir = _ensure_under_root(self.root_dir, main_path.parent)
        files: list[dict[str, Any]] = []
        for path in sorted(model_dir.rglob("*")):
            if not path.is_file():
                continue
            stat = path.stat()
            files.append(
                {
                    "name": path.name,
                    "path": path.relative_to(self.base_dir).as_posix(),
                    "relative_path": path.relative_to(model_dir).as_posix(),
                    "size_bytes": stat.st_size,
                    "mtime": stat.st_mtime,
                    "editable": path.suffix.lower() in SAFE_EXTRA_SUFFIXES,
                    "main": path == main_path,
                }
            )
        return files

    def _template(
        self,
        *,
        model_id: str,
        name: str,
        description: str,
        version: str,
        tags: list[str],
        data_assets: dict[str, Any],
        semantic_assets: dict[str, Any],
        asset_relations: list[str],
        guardrails: list[str],
        templates: dict[str, Any],
        default_template: str,
    ) -> str:
        now_text = datetime.now(tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        metadata = {
            "formatter": "analytics-model",
            "id": model_id,
            "name": name,
            "type": "analysis_model",
            "version": version,
            "description": description,
            "tags": tags,
            "data_assets": {"tables": data_assets.get("tables") or []},
            "semantic_assets": {
                "measures": semantic_assets.get("measures") or [],
                "dimensions": semantic_assets.get("dimensions") or [],
                "grains": semantic_assets.get("grains") or [],
            },
            "asset_relations": asset_relations,
            "guardrails": guardrails,
            "templates": templates,
            "default_template": default_template,
            "created": now_text,
            "updated_at": now_text,
        }
        frontmatter = yaml.safe_dump(metadata, allow_unicode=True, sort_keys=False).strip()
        return (
            f"---\n{frontmatter}\n---\n\n"
            f"# {name}\n\n"
            "## 模型目标\n\n"
            f"{description or '描述这个分析模型解决的业务问题。'}\n\n"
            "## 适用问题\n\n"
            "- 写出用户可能提出的问题。\n\n"
            "## 分析原则\n\n"
            "- 明确优先使用的数据资产和语义资产。\n"
            "- 明确必须说明的口径、分母、分子和排除规则。\n"
            "- 明确缺少关键参数时是否追问。\n\n"
            "## 输出要求\n\n"
            "- 输出核心结论、数据证据和异常说明。\n"
            "- 如果引用模板，按模板组织最终结果。\n"
        )

    def _validate_asset_graph(
        self,
        *,
        data_assets: dict[str, Any],
        semantic_assets: dict[str, Any],
        asset_relations: list[str],
    ) -> None:
        tables = {str(item).strip() for item in data_assets.get("tables") or [] if str(item).strip()}
        dimensions = {str(item).strip() for item in semantic_assets.get("dimensions") or [] if str(item).strip()}
        if len(tables) <= 1:
            return
        if not asset_relations:
            raise AnalyticsModelError("多数据资产模型必须选择资产关联，或缩小为单一数据资产")

        from analytics.semantic_assets import get_semantic_asset_registry

        registry = get_semantic_asset_registry(self.base_dir)
        adjacency: dict[str, set[str]] = {ref: set() for ref in [*tables, *dimensions]}
        for relation_id in asset_relations:
            try:
                relation = registry.get_asset(str(relation_id))
            except Exception as exc:
                raise AnalyticsModelError(f"资产关联不存在: {relation_id}") from exc
            if relation.get("type") != "relation":
                raise AnalyticsModelError(f"不是资产关联: {relation_id}")
            metadata = relation.get("frontmatter") or {}
            definition = metadata.get("relation") if isinstance(metadata.get("relation"), dict) else {}
            relation_type = str(metadata.get("relation_type") or "").strip()
            if relation_type == "dimension_binding":
                asset_ref = str((definition.get("asset") or {}).get("ref") or "").strip()
                dimension_ref = str((definition.get("dimension") or {}).get("ref") or "").strip()
                if asset_ref not in tables or dimension_ref not in dimensions:
                    raise AnalyticsModelError(f"关联 {relation_id} 的资产和维度必须均已被模型选择")
                adjacency.setdefault(asset_ref, set()).add(dimension_ref)
                adjacency.setdefault(dimension_ref, set()).add(asset_ref)
            elif relation_type == "direct_join":
                left_ref = str((definition.get("left") or {}).get("ref") or "").strip()
                right_ref = str((definition.get("right") or {}).get("ref") or "").strip()
                if left_ref not in tables or right_ref not in tables:
                    raise AnalyticsModelError(f"关联 {relation_id} 的两端资产必须均已被模型选择")
                adjacency.setdefault(left_ref, set()).add(right_ref)
                adjacency.setdefault(right_ref, set()).add(left_ref)
            else:
                raise AnalyticsModelError(f"资产关联类型无效: {relation_id}")

        reachable: set[str] = set()
        stack = [next(iter(tables))]
        while stack:
            node = stack.pop()
            if node in reachable:
                continue
            reachable.add(node)
            stack.extend(adjacency.get(node, set()) - reachable)
        disconnected = sorted(tables - reachable)
        if disconnected:
            raise AnalyticsModelError(f"模型数据资产未形成连通语义图: {', '.join(disconnected)}")


_REGISTRIES: dict[Path, AnalyticsModelRegistry] = {}
_REGISTRIES_LOCK = RLock()


def get_analytics_model_registry(base_dir: Path | None = None) -> AnalyticsModelRegistry:
    resolved = (base_dir or _base_dir_from_here()).resolve()
    with _REGISTRIES_LOCK:
        registry = _REGISTRIES.get(resolved)
        if registry is None:
            registry = AnalyticsModelRegistry(resolved)
            _REGISTRIES[resolved] = registry
        return registry
