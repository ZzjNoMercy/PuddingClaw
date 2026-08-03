"""Filesystem-backed analytics model registry."""

from __future__ import annotations

import hashlib
import io
import json
import re
import zipfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from threading import RLock
from typing import Any, BinaryIO

import yaml

from knowledge.paths import get_knowledge_root


class AnalyticsModelError(ValueError):
    """Raised when analytics model input or filesystem state is invalid."""


SAFE_EXTRA_SUFFIXES = {
    ".md",
    ".txt",
    ".json",
    ".yaml",
    ".yml",
    ".html",
    ".css",
    ".js",
    ".csv",
    ".tsv",
}
SLUG_RE = re.compile(r"[^0-9A-Za-z_\-\u4e00-\u9fff]+")
IGNORED_MODEL_FILE_NAMES = {".DS_Store", "Thumbs.db", "desktop.ini"}
IGNORED_MODEL_PATH_PARTS = {"__MACOSX", ".git", ".svn"}


def _is_ignored_model_file(path: Path) -> bool:
    return (
        path.name in IGNORED_MODEL_FILE_NAMES
        or path.name.startswith("._")
        or any(part in IGNORED_MODEL_PATH_PARTS for part in path.parts)
    )


def _normalize_table_aliases(data_assets: dict[str, Any]) -> dict[str, list[str]]:
    """Validate and normalize model-owned aliases without broadening table scope."""

    table_refs = [str(item or "").strip() for item in data_assets.get("tables") or [] if str(item or "").strip()]
    declared_database_refs = {ref for ref in table_refs if not ref.startswith("table_asset:") and "." in ref}
    raw_aliases = data_assets.get("table_aliases") or {}
    if not isinstance(raw_aliases, dict):
        raise AnalyticsModelError("data_assets.table_aliases must be a mapping")

    physical_identifiers: dict[str, dict[str, str]] = {}
    for ref in declared_database_refs:
        source_id, table_name = ref.split(".", 1)
        clean_table = table_name.strip().strip('"').lower()
        source_identifiers = physical_identifiers.setdefault(source_id, {})
        for identifier in {clean_table, clean_table.split(".")[-1]}:
            source_identifiers[identifier] = table_name

    aliases_by_source: dict[str, dict[str, str]] = {}
    normalized: dict[str, list[str]] = {}
    for raw_ref, raw_values in raw_aliases.items():
        ref = str(raw_ref or "").strip()
        if ref not in declared_database_refs:
            raise AnalyticsModelError(f"table alias references undeclared database table: {ref}")
        if not isinstance(raw_values, list):
            raise AnalyticsModelError(f"table aliases must be a list: {ref}")
        source_id, table_name = ref.split(".", 1)
        source_aliases = aliases_by_source.setdefault(source_id, {})
        values: list[str] = []
        for raw_alias in raw_values:
            alias = str(raw_alias or "").strip().lower()
            if not alias or alias in values:
                continue
            physical_target = physical_identifiers.get(source_id, {}).get(alias)
            if physical_target and physical_target != table_name:
                raise AnalyticsModelError(f"table alias conflicts with physical table identifier: {alias}")
            existing = source_aliases.get(alias)
            if existing and existing != table_name:
                raise AnalyticsModelError(f"table alias maps to multiple tables in one source: {alias}")
            source_aliases[alias] = table_name
            values.append(alias)
        if values:
            normalized[ref] = values
    return normalized


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


def canonical_model_resource_path(raw_path: object, *, root: str) -> str:
    """Return one model-relative resource path with a single declared root.

    Model metadata paths are always relative to the directory containing
    ``model.md``.  Older template declarations omitted the leading
    ``templates/`` segment; keep that one compatibility rule here so runtime
    loading and project export cannot drift apart.
    """

    value = str(raw_path or "").strip()
    if not value:
        raise AnalyticsModelError(f"{root} resource path is required")
    if "\x00" in value or "\\" in value or value.startswith("/") or "://" in value or re.match(r"^[A-Za-z]:/", value):
        raise AnalyticsModelError(f"Invalid {root} resource path: {value}")
    while value.startswith("./"):
        value = value[2:]
    path = PurePosixPath(value)
    if not path.parts or any(part in {"", ".", ".."} for part in path.parts):
        raise AnalyticsModelError(f"Invalid {root} resource path: {value}")
    reserved_roots = {"templates", "references", "examples"}
    if path.parts[0] in reserved_roots and path.parts[0] != root:
        raise AnalyticsModelError(f"Invalid {root} resource path: {value}")
    if path.parts[0] != root:
        path = PurePosixPath(root) / path
    return path.as_posix()


def model_resource_virtual_path(model_path: object, model_relative_path: object) -> str:
    """Resolve a validated model-relative path into the managed namespace."""

    model_file = PurePosixPath(str(model_path or "").strip())
    resource = PurePosixPath(str(model_relative_path or "").strip())
    if not model_file.parts or model_file.name != "model.md":
        raise AnalyticsModelError(f"Invalid analytics model path: {model_path}")
    return "/" + (model_file.parent / resource).as_posix()


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

    def get_model_context(self, model_id: str, *, query: str = "") -> dict[str, Any]:
        model = self.get_model(model_id)
        missing_references: list[str] = []
        relation_context: list[dict[str, Any]] = []
        semantic_asset_context: list[dict[str, Any]] = []
        binding_assets_by_dimension: dict[str, list[str]] = {}
        semantic_assets_meta = (model.get("frontmatter") or {}).get("semantic_assets") or {}
        if isinstance(semantic_assets_meta, dict):
            from analytics.semantic_assets import get_semantic_asset_registry

            assets = get_semantic_asset_registry(self.base_dir)
            for asset_group in ("measures", "dimensions", "grains"):
                for raw_asset_id in semantic_assets_meta.get(asset_group) or []:
                    asset_id = str(raw_asset_id).strip()
                    if not asset_id:
                        continue
                    try:
                        asset = assets.get_asset(asset_id)
                    except Exception:
                        missing_references.append(asset_id)
                        continue
                    frontmatter = json.loads(
                        json.dumps(asset.get("frontmatter") or {}, ensure_ascii=False, default=str)
                    )
                    semantic_asset_context.append(
                        {
                            "id": asset_id,
                            "name": asset.get("name"),
                            "type": asset.get("type"),
                            "description": asset.get("description") or "",
                            "path": asset.get("path"),
                            "frontmatter": frontmatter,
                        }
                    )
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
        data_asset_context: list[dict[str, Any]] = []
        logical_dataset_context: list[dict[str, Any]] = []
        missing_data_assets: list[str] = []
        table_refs = [
            str(item).strip()
            for item in ((model.get("frontmatter") or {}).get("data_assets") or {}).get("tables") or []
        ]
        for table_ref in table_refs:
            if not table_ref.startswith("table_asset:"):
                database_source_id, separator, table_name = table_ref.partition(".")
                data_asset_context.append(
                    {
                        "ref": table_ref,
                        "asset_type": "database_table",
                        "name": table_name if separator else table_ref,
                        "database_source_id": database_source_id if separator else "",
                        "table_name": table_name if separator else table_ref,
                    }
                )
                continue
            asset_id = table_ref.removeprefix("table_asset:").strip()
            if not asset_id or "/" in asset_id or "\\" in asset_id:
                missing_data_assets.append(table_ref)
                continue
            definition_path = self.base_dir / "data" / "analytics-concat-datasets" / asset_id / "dataset.json"
            try:
                definition = json.loads(definition_path.read_text(encoding="utf-8"))
            except Exception:
                definition = None
            if isinstance(definition, dict) and definition.get("formatter") == "logical-data-asset":
                logical_summary = {
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
                        {
                            "asset_id": source.get("asset_id"),
                            "name": source.get("name"),
                            "sheet_name": source.get("sheet_name"),
                        }
                        for source in definition.get("sources") or []
                        if isinstance(source, dict)
                    ],
                }
                logical_dataset_context.append(logical_summary)
                data_asset_context.append({"ref": table_ref, "asset_type": "logical_dataset", **logical_summary})
                continue

            profile_path = (
                get_knowledge_root(self.base_dir) / ".puddingclaw" / "table_profiles" / f"{asset_id}.profile.json"
            )
            try:
                profile = json.loads(profile_path.read_text(encoding="utf-8"))
            except Exception:
                profile = None
            if not isinstance(profile, dict):
                missing_data_assets.append(table_ref)
                data_asset_context.append(
                    {
                        "ref": table_ref,
                        "asset_id": asset_id,
                        "asset_type": "table_asset",
                        "status": "metadata_missing",
                    }
                )
                continue

            columns = [item for item in profile.get("columns") or [] if isinstance(item, dict)]
            year_column = next((item for item in columns if str(item.get("name") or "") == "年份"), None)
            year_values = [str(item) for item in (year_column or {}).get("sample_values") or []]
            month_fields = [
                str(item.get("name"))
                for item in columns
                if str(item.get("name") or "").isdigit() and 1 <= int(str(item.get("name"))) <= 12
            ]
            data_asset_context.append(
                {
                    "ref": table_ref,
                    "asset_id": asset_id,
                    "asset_type": "raw_table",
                    "name": profile.get("file_name") or asset_id,
                    "source_type": profile.get("source_type"),
                    "virtual_path": profile.get("virtual_path"),
                    "sheet_name": profile.get("sheet_name"),
                    "schema": {
                        "fields": [str(item.get("name") or "") for item in columns],
                        "field_types": {str(item.get("name") or ""): str(item.get("dtype") or "") for item in columns},
                    },
                    "coverage": {
                        "years": year_values,
                        "month_fields": month_fields,
                    },
                    "statistics": {
                        "shape": profile.get("shape"),
                        "size_bytes": profile.get("size_bytes"),
                    },
                }
            )
        model_file_paths = {
            str(item.get("relative_path") or "").strip()
            for item in model.get("files") or []
            if str(item.get("relative_path") or "").strip()
        }
        resolved_references: dict[str, dict[str, Any]] = {}
        raw_references = (model.get("frontmatter") or {}).get("references")
        if isinstance(raw_references, dict):
            for reference_id, raw_definition in raw_references.items():
                definition = raw_definition if isinstance(raw_definition, dict) else {"path": raw_definition}
                declared_path = str(definition.get("path") or "").strip()
                if not declared_path:
                    continue
                relative_path = canonical_model_resource_path(declared_path, root="references")
                if relative_path not in model_file_paths:
                    missing_references.append(f"reference:{reference_id}:{relative_path}")
                resolved_references[str(reference_id)] = {
                    **{key: value for key, value in definition.items() if key != "path"},
                    "declared_path": declared_path,
                    "model_relative_path": relative_path,
                    "virtual_path": model_resource_virtual_path(model["path"], relative_path),
                    "available": relative_path in model_file_paths,
                }

        semantic_by_id = {
            str(item.get("id") or ""): item for item in semantic_asset_context if str(item.get("id") or "")
        }
        resolved_templates: dict[str, dict[str, Any]] = {}
        raw_templates = model.get("templates") if isinstance(model.get("templates"), dict) else {}
        for template_id, raw_definition in raw_templates.items():
            definition = raw_definition if isinstance(raw_definition, dict) else {"path": raw_definition}
            declared_path = str(definition.get("path") or "").strip()
            if not declared_path:
                continue
            relative_path = canonical_model_resource_path(declared_path, root="templates")
            missing_template_paths: list[str] = []
            if relative_path not in model_file_paths:
                missing_references.append(f"template:{template_id}:{relative_path}")
                missing_template_paths.append(relative_path)
            resolved: dict[str, Any] = {
                **{key: value for key, value in definition.items() if key not in {"path", "guide", "assets"}},
                "declared_path": declared_path,
                "model_relative_path": relative_path,
                "virtual_path": model_resource_virtual_path(model["path"], relative_path),
            }
            declared_guide = str(definition.get("guide") or "").strip()
            guide_frontmatter: dict[str, Any] = {}
            if declared_guide:
                guide_relative_path = canonical_model_resource_path(declared_guide, root="templates")
                if guide_relative_path not in model_file_paths:
                    missing_references.append(f"template_guide:{template_id}:{guide_relative_path}")
                    missing_template_paths.append(guide_relative_path)
                resolved.update(
                    {
                        "declared_guide": declared_guide,
                        "guide_model_relative_path": guide_relative_path,
                        "guide_virtual_path": model_resource_virtual_path(model["path"], guide_relative_path),
                    }
                )
                if guide_relative_path in model_file_paths:
                    model_main_path = _ensure_under_root(self.root_dir, self.base_dir / str(model["path"]))
                    guide_path = _ensure_under_root(
                        self.root_dir,
                        model_main_path.parent / guide_relative_path,
                    )
                    guide_bytes = guide_path.read_bytes()
                    try:
                        guide_text = guide_bytes.decode("utf-8")
                    except UnicodeDecodeError as exc:
                        raise AnalyticsModelError(
                            f"Template {template_id} guide must be UTF-8: {guide_relative_path}"
                        ) from exc
                    guide_frontmatter, _guide_body = _parse_frontmatter(guide_text)
                    if guide_frontmatter:
                        if str(guide_frontmatter.get("formatter") or "") != "analytics-template":
                            raise AnalyticsModelError(
                                f"Template {template_id} guide frontmatter formatter must be analytics-template"
                            )
                        manifest_id = str(guide_frontmatter.get("id") or "").strip()
                        if manifest_id != str(template_id):
                            raise AnalyticsModelError(
                                f"Template {template_id} guide frontmatter id must match its model registration"
                            )
                    resolved["guide_content_sha256"] = "sha256:" + hashlib.sha256(guide_bytes).hexdigest()
                    resolved["guide_frontmatter"] = guide_frontmatter
            asset_relative_paths: list[str] = []
            asset_virtual_paths: list[str] = []
            raw_assets = definition.get("assets") or []
            if not isinstance(raw_assets, list):
                raise AnalyticsModelError(f"Template {template_id} assets must be a list")
            for raw_asset_path in raw_assets:
                asset_relative_path = canonical_model_resource_path(raw_asset_path, root="templates")
                if asset_relative_path not in model_file_paths:
                    missing_references.append(f"template_asset:{template_id}:{asset_relative_path}")
                    missing_template_paths.append(asset_relative_path)
                asset_relative_paths.append(asset_relative_path)
                asset_virtual_paths.append(model_resource_virtual_path(model["path"], asset_relative_path))
            resolved["asset_model_relative_paths"] = list(dict.fromkeys(asset_relative_paths))
            resolved["asset_virtual_paths"] = list(dict.fromkeys(asset_virtual_paths))

            if "semantic_scope" in definition:
                raise AnalyticsModelError(
                    f"Template {template_id} semantic_scope must be declared in its guide frontmatter"
                )
            semantic_scope = guide_frontmatter.get("semantic_scope")
            compiled_filters: dict[str, dict[str, list[str]]] = {}
            if isinstance(semantic_scope, dict):
                unknown_scope_keys = sorted(set(semantic_scope) - {"enum_filters"})
                if unknown_scope_keys:
                    raise AnalyticsModelError(
                        f"Template {template_id} semantic_scope has unknown keys: {', '.join(unknown_scope_keys)}"
                    )
                enum_filters = semantic_scope.get("enum_filters")
                if enum_filters is not None and not isinstance(enum_filters, dict):
                    raise AnalyticsModelError(f"Template {template_id} semantic_scope.enum_filters must be a mapping")
                for asset_id, raw_filter in (enum_filters or {}).items():
                    asset_id = str(asset_id).strip()
                    asset = semantic_by_id.get(asset_id)
                    if not asset or str(asset.get("type") or "") != "dimension":
                        raise AnalyticsModelError(
                            f"Template {template_id} enum filter references an unselected dimension: {asset_id}"
                        )
                    if not isinstance(raw_filter, dict):
                        raise AnalyticsModelError(f"Template {template_id} enum filter {asset_id} must be a mapping")
                    unknown_filter_keys = sorted(set(raw_filter) - {"members", "classifications"})
                    if unknown_filter_keys:
                        raise AnalyticsModelError(
                            f"Template {template_id} enum filter {asset_id} has unknown keys: "
                            + ", ".join(unknown_filter_keys)
                        )
                    frontmatter = asset.get("frontmatter") if isinstance(asset.get("frontmatter"), dict) else {}
                    enum_universe = {str(value).strip() for value in frontmatter.get("enum_universe") or []}
                    classifications = (
                        frontmatter.get("classifications")
                        if isinstance(frontmatter.get("classifications"), dict)
                        else {}
                    )
                    members = [str(value).strip() for value in raw_filter.get("members") or [] if str(value).strip()]
                    labels = [
                        str(value).strip() for value in raw_filter.get("classifications") or [] if str(value).strip()
                    ]
                    unknown_members = sorted(set(members) - enum_universe)
                    unknown_labels = sorted(set(labels) - {str(key) for key in classifications})
                    if unknown_members or unknown_labels:
                        invalid = ", ".join([*unknown_members, *unknown_labels])
                        raise AnalyticsModelError(
                            f"Template {template_id} enum filter {asset_id} contains undeclared values: {invalid}"
                        )
                    compiled_filters[asset_id] = {
                        "members": list(dict.fromkeys(members)),
                        "classifications": list(dict.fromkeys(labels)),
                    }
            resolved["compiled_semantic_scope"] = {"enum_filters": compiled_filters}
            resolved["available"] = not missing_template_paths
            resolved["missing_paths"] = missing_template_paths
            resolved_templates[str(template_id)] = resolved

        return {
            "id": model["id"],
            "name": model["name"],
            "version": model.get("version") or "0.1.0",
            "path": model["path"],
            "description": model.get("description") or "",
            "frontmatter": model.get("frontmatter") or {},
            "body": model.get("body") or "",
            "files": model.get("files") or [],
            "missing_references": list(dict.fromkeys(missing_references)),
            "semantic_assets": semantic_asset_context,
            "asset_relations": relation_context,
            "derived_dimension_paths": derived_dimension_paths,
            "data_assets": data_asset_context,
            "missing_data_assets": list(dict.fromkeys(missing_data_assets)),
            "logical_datasets": logical_dataset_context,
            "resolved_references": resolved_references,
            "resolved_templates": resolved_templates,
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
            relative_path = path.relative_to(model_dir)
            if path.is_symlink() or not path.is_file() or _is_ignored_model_file(relative_path):
                continue
            stat = path.stat()
            files.append(
                {
                    "name": path.name,
                    "path": path.relative_to(self.base_dir).as_posix(),
                    "relative_path": relative_path.as_posix(),
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
            "data_assets": {
                "tables": data_assets.get("tables") or [],
                "table_aliases": _normalize_table_aliases(data_assets),
            },
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
        _normalize_table_aliases(data_assets)
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
