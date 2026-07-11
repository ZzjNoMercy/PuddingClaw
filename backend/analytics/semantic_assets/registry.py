"""Filesystem-backed semantic asset registry.

Semantic assets are Skill-like Markdown references used by database QA. The
registry keeps a process-local snapshot so tools can consume definitions without
rescanning files on every question.
"""

from __future__ import annotations

import io
import re
import zipfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from threading import RLock
from typing import Any, BinaryIO

import yaml


class SemanticAssetError(ValueError):
    """Raised when semantic asset input or filesystem state is invalid."""


ASSET_TYPES = {
    "measure": ("measures", "measure.md"),
    "dimension": ("dimensions", "dimension.md"),
    "grain": ("grains", "grain.md"),
}
DIMENSION_RESOLUTION_MODES = {
    "source_field": "直接字段",
    "derived": "推导规则",
    "entity_lookup": "实体匹配",
    "calendar_lookup": "日历映射",
}
SAFE_EXTRA_SUFFIXES = {".md", ".txt", ".json", ".yaml", ".yml", ".csv", ".tsv"}
SLUG_RE = re.compile(r"[^0-9A-Za-z_\-\u4e00-\u9fff]+")


@dataclass(frozen=True)
class SemanticAsset:
    id: str
    name: str
    type: str
    path: str
    description: str = ""
    aliases: tuple[str, ...] = ()
    tags: tuple[str, ...] = ()
    formatter: str = "semantic-asset"
    resolution_mode: str = ""
    resolution_label: str = ""
    mtime: float = 0.0
    size_bytes: int = 0
    body: str = ""
    frontmatter: dict | None = None

    def to_summary(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "type": self.type,
            "path": self.path,
            "description": self.description,
            "aliases": list(self.aliases),
            "tags": list(self.tags),
            "formatter": self.formatter,
            "resolution_mode": self.resolution_mode,
            "resolution_label": self.resolution_label,
            "mtime": self.mtime,
            "size_bytes": self.size_bytes,
        }

    def to_detail(self) -> dict:
        data = self.to_summary()
        data.update({"body": self.body, "frontmatter": self.frontmatter or {}})
        return data


def _base_dir_from_here() -> Path:
    return Path(__file__).resolve().parents[2]


def _parse_frontmatter(text: str) -> tuple[dict, str]:
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


def _slugify(value: str) -> str:
    slug = SLUG_RE.sub("-", value.strip()).strip("-_").lower()
    return slug or "semantic_asset"


def _plain_string(value: object) -> str:
    return str(value or "").strip()


def _string_list(value: object) -> list[str]:
    if isinstance(value, str):
        return [item.strip() for item in value.replace("\n", ",").split(",") if item.strip()]
    if isinstance(value, list):
        return [_plain_string(item) for item in value if _plain_string(item)]
    return []


def _normalize_dimension_definition(value: object) -> tuple[str, dict[str, Any]]:
    """Return a portable, intentionally small contract for dimension resolution.

    The definition is declarative. It records how a value is obtained, but does
    not make a handwritten SQL expression executable by itself.
    """
    raw = value if isinstance(value, dict) else {}
    mode = _plain_string(raw.get("mode") or raw.get("resolution_mode") or "source_field").lower()
    if mode not in DIMENSION_RESOLUTION_MODES:
        raise SemanticAssetError(
            "dimension resolution_mode must be source_field, derived, entity_lookup or calendar_lookup"
        )

    bindings: list[dict[str, Any]] = []
    for item in raw.get("bindings") or raw.get("source_bindings") or []:
        if not isinstance(item, dict):
            continue
        fields_raw = item.get("fields")
        fields = {str(key).strip(): _plain_string(field) for key, field in fields_raw.items()} if isinstance(fields_raw, dict) else {}
        bindings.append(
            {
                "asset_ref": _plain_string(item.get("asset_ref")),
                "display_name": _plain_string(item.get("display_name")),
                "fields": {key: field for key, field in fields.items() if key and field},
            }
        )

    definition: dict[str, Any] = {"mode": mode}
    if mode == "source_field":
        definition["bindings"] = bindings
    elif mode == "derived":
        definition.update(
            {
                "bindings": bindings,
                "source_fields": _string_list(raw.get("source_fields")),
                "expression": _plain_string(raw.get("expression")),
            }
        )
    elif mode == "entity_lookup":
        canonical_raw = raw.get("canonical") if isinstance(raw.get("canonical"), dict) else {}
        definition.update(
            {
                "canonical": {
                    "key": _plain_string(canonical_raw.get("key") or "entity_key"),
                    "fields": _string_list(canonical_raw.get("fields")),
                },
                "bindings": bindings,
                "reference_path": _plain_string(raw.get("reference_path")),
            }
        )
    else:
        definition.update(
            {
                "bindings": bindings,
                "date_field": _plain_string(raw.get("date_field")),
                "week_start_day": _plain_string(raw.get("week_start_day") or "monday").lower(),
                "timezone": _plain_string(raw.get("timezone") or "Asia/Shanghai"),
            }
        )
    return mode, definition


def _dimension_resolution_summary(meta: dict[str, Any]) -> tuple[str, str]:
    mode = _plain_string(meta.get("resolution_mode"))
    resolution = meta.get("resolution") if isinstance(meta.get("resolution"), dict) else {}
    if not mode:
        mode = _plain_string(resolution.get("mode"))
    label = DIMENSION_RESOLUTION_MODES.get(mode, "未配置")
    return mode or "unconfigured", label


def _ensure_under_root(root: Path, target: Path) -> Path:
    resolved = target.resolve()
    if not str(resolved).startswith(str(root.resolve())):
        raise SemanticAssetError("Path traversal detected")
    return resolved


def _safe_parts(path: str) -> list[str]:
    normalized = path.replace("\\", "/").strip("/")
    parts = [part for part in normalized.split("/") if part and part != "."]
    if not parts or any(part == ".." for part in parts):
        raise SemanticAssetError(f"Invalid import path: {path}")
    return parts


def _semantic_relative_path(original_path: str) -> Path | None:
    parts = _safe_parts(original_path)
    if "semantic-assets" in parts:
        parts = parts[parts.index("semantic-assets") + 1 :]
    if not parts:
        return None

    if parts[0] in {"measures", "dimensions", "grains"}:
        return Path(*parts)

    stripped = parts[1:] if len(parts) > 2 else parts
    if not stripped:
        return None
    filename = stripped[-1]
    if filename == "measure.md":
        return Path("measures", *stripped)
    if filename == "dimension.md":
        return Path("dimensions", *stripped)
    if filename == "grain.md":
        return Path("grains", *stripped)

    return None


class SemanticAssetRegistry:
    def __init__(self, base_dir: Path | None = None):
        self.base_dir = base_dir or _base_dir_from_here()
        self.root_dir = self.base_dir / "semantic-assets"
        self._lock = RLock()
        self._assets: dict[str, SemanticAsset] = {}
        self._last_scanned_at: str | None = None

    def refresh(self) -> dict:
        self.root_dir.mkdir(parents=True, exist_ok=True)
        next_assets: dict[str, SemanticAsset] = {}
        for asset_type, (subdir, filename) in ASSET_TYPES.items():
            for path in sorted((self.root_dir / subdir).glob(f"**/{filename}")):
                asset = self._read_asset(path, asset_type)
                next_assets[asset.id] = asset
        with self._lock:
            self._assets = next_assets
            self._last_scanned_at = datetime.now(tz=timezone.utc).isoformat()
            return self.snapshot()

    def snapshot(self) -> dict:
        with self._lock:
            assets = [asset.to_summary() for asset in self._assets.values()]
            assets.sort(key=lambda item: (item["type"], item["name"], item["path"]))
            type_counts = {
                asset_type: sum(1 for item in assets if item["type"] == asset_type)
                for asset_type in ASSET_TYPES
            }
            return {
                "assets": assets,
                "count": len(assets),
                "type_counts": type_counts,
                "root_dir": str(self.root_dir),
                "last_scanned_at": self._last_scanned_at,
            }

    def list_assets(self) -> dict:
        if self._last_scanned_at is None:
            return self.refresh()
        return self.snapshot()

    def get_asset(self, asset_id: str) -> dict:
        with self._lock:
            asset = self._assets.get(asset_id)
        if not asset:
            self.refresh()
            with self._lock:
                asset = self._assets.get(asset_id)
        if not asset:
            raise SemanticAssetError(f"Semantic asset not found: {asset_id}")
        detail = asset.to_detail()
        detail["files"] = self._asset_files(asset.path)
        return detail

    def create_asset(
        self,
        *,
        name: str,
        asset_type: str,
        description: str = "",
        aliases: list[str] | None = None,
        tags: list[str] | None = None,
        version: str = "0.1.0",
        slug: str | None = None,
        dimension_definition: dict[str, Any] | None = None,
    ) -> dict:
        asset_type = asset_type.strip().lower()
        if asset_type not in ASSET_TYPES:
            raise SemanticAssetError("type must be measure, dimension or grain")
        clean_name = name.strip()
        if not clean_name:
            raise SemanticAssetError("name is required")

        subdir, filename = ASSET_TYPES[asset_type]
        asset_slug = _slugify(slug or clean_name)
        asset_dir = _ensure_under_root(self.root_dir, self.root_dir / subdir / asset_slug)
        target = _ensure_under_root(self.root_dir, asset_dir / filename)
        if target.exists():
            raise SemanticAssetError(f"Semantic asset already exists: {asset_slug}")
        asset_dir.mkdir(parents=True, exist_ok=True)
        normalized_definition: dict[str, Any] | None = None
        if asset_type == "dimension":
            _mode, normalized_definition = _normalize_dimension_definition(dimension_definition)
        target.write_text(
            self._template(
                name=clean_name,
                asset_type=asset_type,
                description=description.strip(),
                aliases=aliases or [],
                tags=tags or [],
                version=version.strip() or "0.1.0",
                dimension_definition=normalized_definition,
            ),
            encoding="utf-8",
        )
        self.refresh()
        return self.get_asset(f"{asset_type}:{asset_dir.relative_to(self.root_dir / subdir).as_posix()}")

    def update_dimension_definition(
        self,
        asset_id: str,
        definition: dict[str, Any],
        *,
        name: str | None = None,
        description: str | None = None,
        aliases: list[str] | None = None,
        tags: list[str] | None = None,
        version: str | None = None,
    ) -> dict:
        """Update the structured fields of a dimension.

        Preserve the authored Markdown body and any advanced metadata such as a
        build-skill declaration. This keeps structured editing and direct file
        editing as equal maintenance paths.
        """
        with self._lock:
            asset = self._assets.get(asset_id)
        if asset is None:
            self.refresh()
            with self._lock:
                asset = self._assets.get(asset_id)
        if asset is None:
            raise SemanticAssetError(f"Semantic asset not found: {asset_id}")
        if asset.type != "dimension":
            raise SemanticAssetError("Only dimensions have a resolution definition")

        path = _ensure_under_root(self.root_dir, self.base_dir / asset.path)
        metadata, body = _parse_frontmatter(path.read_text(encoding="utf-8"))
        mode, normalized = _normalize_dimension_definition(definition)
        if name is not None:
            clean_name = name.strip()
            if not clean_name:
                raise SemanticAssetError("name is required")
            metadata["name"] = clean_name
        if description is not None:
            metadata["description"] = description.strip()
        if aliases is not None:
            metadata["aliases"] = [item.strip() for item in aliases if item and item.strip()]
        if tags is not None:
            metadata["tags"] = [item.strip() for item in tags if item and item.strip()]
        if version is not None:
            metadata["version"] = version.strip() or "0.1.0"
        metadata["resolution_mode"] = mode
        metadata["resolution"] = normalized
        metadata["updated_at"] = datetime.now(tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        frontmatter = yaml.safe_dump(metadata, allow_unicode=True, sort_keys=False).strip()
        path.write_text(f"---\n{frontmatter}\n---\n\n{body}", encoding="utf-8")
        self.refresh()
        return self.get_asset(asset_id)

    def import_zip(self, fileobj: BinaryIO) -> dict:
        data = fileobj.read()
        try:
            archive = zipfile.ZipFile(io.BytesIO(data))
        except zipfile.BadZipFile as exc:
            raise SemanticAssetError("Invalid ZIP file") from exc

        imported: list[str] = []
        with archive:
            for info in archive.infolist():
                if info.is_dir():
                    continue
                rel = _semantic_relative_path(info.filename)
                if rel is None:
                    continue
                if Path(rel.name).suffix.lower() not in SAFE_EXTRA_SUFFIXES:
                    continue
                target = _ensure_under_root(self.root_dir, self.root_dir / rel)
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(archive.read(info))
                imported.append(rel.as_posix())

        return self._finish_import(imported)

    def import_files(self, files: list[tuple[str, bytes]]) -> dict:
        imported: list[str] = []
        for name, content in files:
            rel = _semantic_relative_path(name)
            if rel is None:
                continue
            if Path(rel.name).suffix.lower() not in SAFE_EXTRA_SUFFIXES:
                continue
            target = _ensure_under_root(self.root_dir, self.root_dir / rel)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content)
            imported.append(rel.as_posix())
        return self._finish_import(imported)

    def _finish_import(self, imported: list[str]) -> dict:
        if not any(path.endswith("measure.md") or path.endswith("dimension.md") or path.endswith("grain.md") for path in imported):
            raise SemanticAssetError("Import must contain at least one measure.md, dimension.md or grain.md")
        snapshot = self.refresh()
        return {"imported": sorted(set(imported)), "imported_count": len(set(imported)), **snapshot}

    def _read_asset(self, path: Path, expected_type: str) -> SemanticAsset:
        text = path.read_text(encoding="utf-8")
        meta, body = _parse_frontmatter(text)
        asset_type = str(meta.get("type") or expected_type).strip().lower()
        if asset_type not in ASSET_TYPES:
            asset_type = expected_type
        subdir, _ = ASSET_TYPES[asset_type]
        relative_dir = path.parent.relative_to(self.root_dir / subdir).as_posix()
        stat = path.stat()
        resolution_mode, resolution_label = _dimension_resolution_summary(meta) if asset_type == "dimension" else ("", "")
        return SemanticAsset(
            id=f"{asset_type}:{relative_dir}",
            name=str(meta.get("name") or path.parent.name),
            type=asset_type,
            path=path.relative_to(self.base_dir).as_posix(),
            description=str(meta.get("description") or "").strip(),
            aliases=_list_from_meta(meta.get("aliases")),
            tags=_list_from_meta(meta.get("tags")),
            formatter=str(meta.get("formatter") or "semantic-asset"),
            resolution_mode=resolution_mode,
            resolution_label=resolution_label,
            mtime=stat.st_mtime,
            size_bytes=stat.st_size,
            body=body,
            frontmatter=meta,
        )

    def _asset_files(self, asset_path: str) -> list[dict[str, Any]]:
        main_path = _ensure_under_root(self.root_dir, self.base_dir / asset_path)
        asset_dir = _ensure_under_root(self.root_dir, main_path.parent)
        files: list[dict[str, Any]] = []
        for path in sorted(asset_dir.rglob("*")):
            if not path.is_file():
                continue
            stat = path.stat()
            files.append(
                {
                    "name": path.name,
                    "path": path.relative_to(self.base_dir).as_posix(),
                    "relative_path": path.relative_to(asset_dir).as_posix(),
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
        name: str,
        asset_type: str,
        description: str,
        aliases: list[str],
        tags: list[str],
        version: str,
        dimension_definition: dict[str, Any] | None = None,
    ) -> str:
        now_text = datetime.now(tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        metadata = {
            "formatter": "semantic-asset",
            "name": name,
            "type": asset_type,
            "description": description,
            "aliases": aliases,
            "tags": tags,
            "version": version,
            "created": now_text,
            "updated_at": now_text,
        }
        if asset_type == "dimension":
            mode, definition = _normalize_dimension_definition(dimension_definition)
            metadata["resolution_mode"] = mode
            metadata["resolution"] = definition
        frontmatter = yaml.safe_dump(metadata, allow_unicode=True, sort_keys=False).strip()
        type_titles = {"measure": "度量值", "dimension": "维度", "grain": "颗粒度"}
        title = type_titles.get(asset_type, asset_type)
        references_section = ""
        if asset_type == "measure":
            references_section = (
                "## References\n\n"
                "如某些业务对象存在专用识别规则，请放在本度量值目录的 `references/` 下。\n\n"
                "分析过程中，命中本度量值后必须继续检索 `references/`，并优先遵守匹配 reference。\n\n"
            )
        dimension_section = ""
        if asset_type == "dimension":
            mode = str(metadata.get("resolution_mode") or "")
            label = DIMENSION_RESOLUTION_MODES.get(mode, mode)
            dimension_section = (
                f"## 创建方式\n\n{label}（`{mode}`）\n\n"
                "前置的 `resolution` 是机器可读定义；下方补充业务解释、适用边界与禁止规则。\n\n"
            )
        return (
            f"---\n{frontmatter}\n---\n\n"
            f"# {name}\n\n"
            f"## 类型\n\n{title}\n\n"
            f"## 业务口径\n\n{description or '在这里描述自然语言口径、适用数据资产、字段要求和计算规则。'}\n\n"
            f"{dimension_section}"
            f"{references_section}"
            "## 查询规则\n\n"
            "- 明确需要使用的字段或 type_name 口径。\n"
            "- 明确禁止从名称猜测字段含义。\n"
            "- 如需分组、筛选或去重，在这里写清楚。\n\n"
            "## SQL Hint\n\n"
            "```sql\n"
            "-- 可选：写入 SQL 片段或字段映射提示。\n"
            "```\n"
        )


_REGISTRIES: dict[Path, SemanticAssetRegistry] = {}
_REGISTRIES_LOCK = RLock()


def get_semantic_asset_registry(base_dir: Path | None = None) -> SemanticAssetRegistry:
    resolved = (base_dir or _base_dir_from_here()).resolve()
    with _REGISTRIES_LOCK:
        registry = _REGISTRIES.get(resolved)
        if registry is None:
            registry = SemanticAssetRegistry(resolved)
            _REGISTRIES[resolved] = registry
        return registry
