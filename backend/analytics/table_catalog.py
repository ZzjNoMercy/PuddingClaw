"""Table asset catalog for the analytics workbench.

The upload/import entry remains the existing knowledge import flow. This module
only catalogs spreadsheet-like files already stored under the configured
knowledge root and generates cached profile JSON for table analysis.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from knowledge.paths import get_knowledge_root
from utils.table_engine.profiler import profile_dataframe

TABLE_SUFFIXES = {".xlsx", ".xls", ".csv", ".tsv"}
IGNORED_TOP_LEVEL_DIRS = {".puddingclaw", ".tasks", "tasks", "originals", "assets"}


def _utc_iso(timestamp: float) -> str:
    return datetime.fromtimestamp(timestamp, tz=timezone.utc).isoformat()


def _virtual_path(root: Path, path: Path) -> str:
    return f"/knowledge/{path.relative_to(root).as_posix()}"


def _profile_dir(root: Path) -> Path:
    return root / ".puddingclaw" / "table_profiles"


def _asset_id(virtual_path: str, sheet_name: str | None) -> str:
    payload = f"{virtual_path}#{sheet_name or ''}"
    return "tbl_" + hashlib.sha1(payload.encode("utf-8")).hexdigest()[:24]


def _json_default(value: Any) -> Any:
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value)


@dataclass(frozen=True)
class TableAssetRef:
    asset_id: str
    path: Path
    virtual_path: str
    file_name: str
    source_type: str
    sheet_name: str | None
    size_bytes: int
    modified_at: str


class TableCatalogError(RuntimeError):
    """Raised when table catalog operations fail."""


class TableAssetCatalog:
    """Scan imported table files and generate cached profiles."""

    def __init__(self, base_dir: Path):
        self.base_dir = base_dir
        self.knowledge_root = get_knowledge_root(base_dir).expanduser().resolve()

    def profile_path(self, asset_id: str) -> Path:
        return _profile_dir(self.knowledge_root) / f"{asset_id}.profile.json"

    def list_assets(self, *, include_profile: bool = False, limit: int = 500) -> list[dict[str, Any]]:
        assets = self._scan_assets(limit=limit)
        return [self._asset_to_dict(asset, include_profile=include_profile) for asset in assets]

    def get_asset(self, asset_id: str, *, include_profile: bool = True) -> dict[str, Any]:
        asset = self._find_asset(asset_id)
        if not asset:
            raise TableCatalogError("Table asset not found.")
        return self._asset_to_dict(asset, include_profile=include_profile)

    def generate_profile(self, asset_id: str) -> dict[str, Any]:
        asset = self._find_asset(asset_id)
        if not asset:
            raise TableCatalogError("Table asset not found.")
        df = self._load_dataframe(asset)
        base_profile = profile_dataframe(df, preview_rows=8)
        profile = {
            "asset_id": asset.asset_id,
            "kind": "table_asset_profile",
            "source_type": asset.source_type,
            "file_name": asset.file_name,
            "virtual_path": asset.virtual_path,
            "sheet_name": asset.sheet_name,
            "size_bytes": asset.size_bytes,
            "modified_at": asset.modified_at,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "shape": base_profile.get("shape"),
            "columns": self._column_profiles(df, base_profile),
            "dtypes": base_profile.get("dtypes", {}),
            "preview": base_profile.get("preview", []),
        }
        path = self.profile_path(asset.asset_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(profile, ensure_ascii=False, indent=2, default=_json_default), encoding="utf-8")
        return self._asset_to_dict(asset, include_profile=True)

    def refresh_profiles(self, *, limit: int = 200) -> dict[str, Any]:
        assets = self._scan_assets(limit=limit)
        generated: list[dict[str, Any]] = []
        errors: list[dict[str, str]] = []
        for asset in assets:
            try:
                generated.append(self.generate_profile(asset.asset_id))
            except Exception as exc:  # pragma: no cover - error payload path
                errors.append({"asset_id": asset.asset_id, "file_name": asset.file_name, "error": str(exc)})
        return {"generated": generated, "errors": errors, "total": len(assets)}

    def _scan_assets(self, *, limit: int) -> list[TableAssetRef]:
        if not self.knowledge_root.exists():
            return []
        assets: list[TableAssetRef] = []
        for path in sorted(self.knowledge_root.rglob("*")):
            if len(assets) >= limit:
                break
            if not path.is_file() or path.name.startswith("."):
                continue
            suffix = path.suffix.lower()
            if suffix not in TABLE_SUFFIXES:
                continue
            try:
                relative_path = path.relative_to(self.knowledge_root)
            except ValueError:
                continue
            if not self._is_catalog_asset_path(relative_path):
                continue
            try:
                assets.extend(self._asset_refs_for_path(path))
            except Exception:
                continue
        return sorted(assets, key=lambda asset: (asset.modified_at, asset.file_name, asset.sheet_name or ""), reverse=True)[:limit]

    @staticmethod
    def _is_catalog_asset_path(relative_path: Path) -> bool:
        """Return whether a table path is a final user-facing asset.

        Knowledge import keeps transient copies under `/knowledge/tasks/...`
        and may keep originals under `/knowledge/originals/...`. Those are not
        separate analytics assets; showing them duplicates the final
        `/knowledge/imported/...` table.
        """

        parts = relative_path.parts
        if not parts:
            return False
        if parts[0] in IGNORED_TOP_LEVEL_DIRS:
            return False
        return True

    def _asset_refs_for_path(self, path: Path) -> list[TableAssetRef]:
        suffix = path.suffix.lower()
        stat = path.stat()
        virtual_path = _virtual_path(self.knowledge_root, path)
        source_type = "excel" if suffix in {".xlsx", ".xls"} else ("tsv" if suffix == ".tsv" else "csv")
        sheets: list[str | None]
        if source_type == "excel":
            import pandas as pd

            sheets = [str(sheet) for sheet in pd.ExcelFile(path).sheet_names]
        else:
            sheets = [None]
        return [
            TableAssetRef(
                asset_id=_asset_id(virtual_path, sheet),
                path=path,
                virtual_path=virtual_path,
                file_name=path.name,
                source_type=source_type,
                sheet_name=sheet,
                size_bytes=stat.st_size,
                modified_at=_utc_iso(stat.st_mtime),
            )
            for sheet in sheets
        ]

    def _find_asset(self, asset_id: str) -> TableAssetRef | None:
        for asset in self._scan_assets(limit=2000):
            if asset.asset_id == asset_id:
                return asset
        return None

    def _asset_to_dict(self, asset: TableAssetRef, *, include_profile: bool) -> dict[str, Any]:
        profile_path = self.profile_path(asset.asset_id)
        profile = self._read_profile(profile_path) if profile_path.exists() else None
        result: dict[str, Any] = {
            "asset_id": asset.asset_id,
            "file_name": asset.file_name,
            "source_type": asset.source_type,
            "virtual_path": asset.virtual_path,
            "sheet_name": asset.sheet_name,
            "size_bytes": asset.size_bytes,
            "modified_at": asset.modified_at,
            "profile_status": "ready" if profile else "missing",
            "profile_path": str(profile_path),
            "rows": profile.get("shape", [None, None])[0] if profile else None,
            "columns_count": profile.get("shape", [None, None])[1] if profile else None,
            "columns": [column.get("name") for column in profile.get("columns", [])] if profile else [],
            "reference_status": "pending",
        }
        if include_profile and profile:
            result["profile"] = profile
        return result

    @staticmethod
    def _read_profile(path: Path) -> dict[str, Any] | None:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return None
        return payload if isinstance(payload, dict) else None

    @staticmethod
    def _load_dataframe(asset: TableAssetRef):
        import pandas as pd

        if asset.source_type == "excel":
            return pd.read_excel(asset.path, sheet_name=asset.sheet_name)
        sep = "\t" if asset.source_type == "tsv" else ","
        try:
            return pd.read_csv(asset.path, sep=sep)
        except UnicodeDecodeError:
            return pd.read_csv(asset.path, sep=sep, encoding="gb18030")

    @staticmethod
    def _column_profiles(df: Any, base_profile: dict[str, Any]) -> list[dict[str, Any]]:
        columns: list[dict[str, Any]] = []
        dtypes = base_profile.get("dtypes", {}) if isinstance(base_profile.get("dtypes"), dict) else {}
        for column in df.columns:
            name = str(column)
            series = df[column]
            non_null = int(series.notna().sum())
            sample_values = [
                str(value)
                for value in series.dropna().astype(str).drop_duplicates().head(5).tolist()
            ]
            columns.append(
                {
                    "name": name,
                    "dtype": str(dtypes.get(name, series.dtype)),
                    "non_null": non_null,
                    "null_count": int(len(series) - non_null),
                    "sample_values": sample_values,
                    "semantic_role_hint": "measure_candidate" if str(series.dtype).startswith(("int", "float")) else "dimension_candidate",
                }
            )
        return columns
