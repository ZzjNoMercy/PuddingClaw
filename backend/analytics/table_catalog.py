"""Persistent table asset catalog for the analytics workbench.

The upload/import entry remains the existing knowledge import flow. This module
keeps spreadsheet-like assets in the catalog database so the analytics page and
Pandas tool do not repeatedly scan `/knowledge`.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from knowledge.models import KnowledgeDocument, KnowledgeTableAsset
from knowledge.paths import get_knowledge_root
from knowledge.service import DEFAULT_KNOWLEDGE_BASE_ID, KnowledgeService
from utils.table_engine.profiler import profile_dataframe

TABLE_SUFFIXES = {".xlsx", ".xls", ".csv", ".tsv"}
IGNORED_TOP_LEVEL_DIRS = {".puddingclaw", ".tasks", "tasks", "originals", "assets"}
PROFILE_SAMPLE_ROWS = 20000


def _utc_datetime(timestamp: float) -> datetime:
    return datetime.fromtimestamp(timestamp, tz=timezone.utc)


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def _virtual_path(root: Path, path: Path) -> str:
    return f"/knowledge/{path.relative_to(root).as_posix()}"


def _profile_dir(root: Path) -> Path:
    return root / ".puddingclaw" / "table_profiles"


def _asset_id(virtual_path: str, sheet_name: str | None) -> str:
    payload = f"{virtual_path}#{sheet_name or ''}"
    return "tbl_" + hashlib.sha1(payload.encode("utf-8")).hexdigest()[:24]


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


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
    modified_at: datetime
    content_sha256: str = ""
    document_id: str | None = None


class TableCatalogError(RuntimeError):
    """Raised when table catalog operations fail."""


class TableAssetCatalog:
    """Catalog imported table files and generate cached profiles."""

    def __init__(self, base_dir: Path):
        self.base_dir = base_dir
        self.knowledge_root = get_knowledge_root(base_dir).expanduser().resolve()

    def profile_path(self, asset_id: str) -> Path:
        return _profile_dir(self.knowledge_root) / f"{asset_id}.profile.json"

    async def list_assets(
        self,
        session: AsyncSession,
        *,
        include_profile: bool = False,
        limit: int = 500,
        ensure_scanned: bool = True,
    ) -> list[dict[str, Any]]:
        if ensure_scanned:
            await self.ensure_catalog_populated(session, limit=limit)
        stmt = (
            select(KnowledgeTableAsset)
            .where(
                KnowledgeTableAsset.knowledge_base_id == DEFAULT_KNOWLEDGE_BASE_ID,
                KnowledgeTableAsset.reference_status != "removed",
            )
            .order_by(KnowledgeTableAsset.updated_at.desc(), KnowledgeTableAsset.file_name.asc())
            .limit(max(1, min(limit, 2000)))
        )
        result = await session.execute(stmt)
        return [self._asset_to_dict(asset, include_profile=include_profile) for asset in result.scalars()]

    async def get_asset(self, session: AsyncSession, asset_id: str, *, include_profile: bool = True) -> dict[str, Any]:
        asset = await session.get(KnowledgeTableAsset, asset_id)
        if asset is None:
            await self.ensure_catalog_populated(session, limit=2000)
            asset = await session.get(KnowledgeTableAsset, asset_id)
        if asset is None or asset.reference_status == "removed":
            raise TableCatalogError("Table asset not found.")
        return self._asset_to_dict(asset, include_profile=include_profile)

    async def remove_asset(self, session: AsyncSession, asset_id: str) -> dict[str, Any]:
        """Detach one uploaded workbook/flat file from analytics without deleting its KB file."""
        asset = await session.get(KnowledgeTableAsset, asset_id)
        if asset is None or asset.reference_status == "removed":
            raise TableCatalogError("Table asset not found.")

        siblings = list(
            (
                await session.execute(
                    select(KnowledgeTableAsset).where(
                        KnowledgeTableAsset.knowledge_base_id == asset.knowledge_base_id,
                        KnowledgeTableAsset.storage_path == asset.storage_path,
                        KnowledgeTableAsset.reference_status != "removed",
                    )
                )
            ).scalars()
        )
        removed_ids: list[str] = []
        for sibling in siblings:
            profile_path = Path(sibling.profile_path) if sibling.profile_path else self.profile_path(sibling.asset_id)
            if profile_path.is_file() and self.profile_path(sibling.asset_id).parent in profile_path.parents:
                await asyncio.to_thread(profile_path.unlink, missing_ok=True)
            sibling.reference_status = "removed"
            sibling.profile_status = "missing"
            sibling.profile_path = ""
            sibling.rows = None
            sibling.columns_count = None
            sibling.columns = []
            removed_ids.append(sibling.asset_id)
        await session.commit()
        return {
            "asset_id": asset_id,
            "removed_asset_ids": removed_ids,
            "file_name": asset.file_name,
            "storage_path": asset.storage_path,
            "source_file_preserved": True,
        }

    async def load_dataframe_for_asset(self, session: AsyncSession, asset_id: str):
        asset = await session.get(KnowledgeTableAsset, asset_id)
        if asset is None:
            await self.ensure_catalog_populated(session, limit=2000)
            asset = await session.get(KnowledgeTableAsset, asset_id)
        if asset is None or asset.reference_status == "removed":
            raise TableCatalogError("Table asset not found.")
        return asset, self._load_dataframe(self._ref_from_model(asset))

    async def generate_profile(
        self,
        session: AsyncSession,
        asset_id: str,
        *,
        include_profile: bool = True,
    ) -> dict[str, Any]:
        asset = await session.get(KnowledgeTableAsset, asset_id)
        if asset is None:
            await self.ensure_catalog_populated(session, limit=2000)
            asset = await session.get(KnowledgeTableAsset, asset_id)
        if asset is None or asset.reference_status == "removed":
            raise TableCatalogError("Table asset not found.")

        ref = self._ref_from_model(asset)
        profile = await asyncio.to_thread(self._build_profile, asset, ref)
        profile_path = self.profile_path(asset.asset_id)
        profile_path.parent.mkdir(parents=True, exist_ok=True)
        profile_json = json.dumps(profile, ensure_ascii=False, indent=2, default=_json_default)
        await asyncio.to_thread(profile_path.write_text, profile_json, encoding="utf-8")

        shape = profile.get("shape") if isinstance(profile.get("shape"), list) else []
        columns = [column.get("name") for column in profile.get("columns", []) if isinstance(column, dict)]
        asset.profile_status = "ready"
        asset.profile_path = str(profile_path)
        asset.rows = int(shape[0]) if len(shape) > 0 and shape[0] is not None else None
        asset.columns_count = int(shape[1]) if len(shape) > 1 and shape[1] is not None else None
        asset.columns = [str(column) for column in columns if column]
        asset.asset_metadata = {
            **(asset.asset_metadata or {}),
            "profile_generated_at": profile["generated_at"],
        }
        await session.commit()
        await session.refresh(asset)
        return self._asset_to_dict(asset, include_profile=include_profile)

    def _build_profile(self, asset: KnowledgeTableAsset, ref: TableAssetRef) -> dict[str, Any]:
        """Build a sampled table profile off the event loop."""

        df = self._load_dataframe(ref, sample_rows=PROFILE_SAMPLE_ROWS)
        base_profile = profile_dataframe(df, preview_rows=8)
        actual_shape = self._table_shape(ref, sample_df=df)
        sampled_rows = int(df.shape[0])
        total_rows = int(actual_shape[0]) if actual_shape and actual_shape[0] is not None else sampled_rows
        return {
            "asset_id": asset.asset_id,
            "kind": "table_asset_profile",
            "sampled": sampled_rows < total_rows,
            "sample_rows": sampled_rows,
            "profile_sample_limit": PROFILE_SAMPLE_ROWS,
            "source_type": asset.source_type,
            "file_name": asset.file_name,
            "virtual_path": asset.virtual_path,
            "sheet_name": asset.sheet_name,
            "size_bytes": asset.size_bytes,
            "modified_at": _iso(asset.modified_at),
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "shape": actual_shape or base_profile.get("shape"),
            "columns": self._column_profiles(df, base_profile),
            "dtypes": base_profile.get("dtypes", {}),
            "preview": base_profile.get("preview", []),
        }

    async def refresh_profiles(self, session: AsyncSession, *, limit: int = 200) -> dict[str, Any]:
        await self.ensure_catalog_populated(session, limit=limit)
        stmt = (
            select(KnowledgeTableAsset.asset_id)
            .where(KnowledgeTableAsset.knowledge_base_id == DEFAULT_KNOWLEDGE_BASE_ID)
            .order_by(KnowledgeTableAsset.updated_at.desc())
            .limit(max(1, min(limit, 1000)))
        )
        result = await session.execute(stmt)
        asset_ids = [str(asset_id) for asset_id in result.scalars()]
        generated: list[dict[str, Any]] = []
        errors: list[dict[str, str]] = []
        for asset_id in asset_ids:
            try:
                generated.append(await self.generate_profile(session, asset_id))
            except Exception as exc:  # pragma: no cover - error payload path
                errors.append({"asset_id": asset_id, "error": str(exc)})
        return {"generated": generated, "errors": errors, "total": len(asset_ids)}

    async def register_path(
        self,
        session: AsyncSession,
        path: Path,
        *,
        virtual_path: str | None = None,
        knowledge_base_id: str = DEFAULT_KNOWLEDGE_BASE_ID,
        document_id: str | None = None,
    ) -> list[KnowledgeTableAsset]:
        await KnowledgeService(self.base_dir).ensure_default_knowledge_base(session)
        if not path.exists() or not path.is_file() or path.suffix.lower() not in TABLE_SUFFIXES:
            return []
        refs = self._dedupe_refs(self._asset_refs_for_path(
            path,
            virtual_path=virtual_path,
            document_id=document_id,
            content_sha256=_sha256(path),
        ))
        return await self._commit_asset_refs(session, refs, knowledge_base_id=knowledge_base_id)

    async def ensure_catalog_populated(self, session: AsyncSession, *, limit: int = 500) -> int:
        await KnowledgeService(self.base_dir).ensure_default_knowledge_base(session)
        count_stmt = select(KnowledgeTableAsset.asset_id).where(KnowledgeTableAsset.knowledge_base_id == DEFAULT_KNOWLEDGE_BASE_ID).limit(1)
        existing = (await session.execute(count_stmt)).scalar_one_or_none()
        if existing:
            return 0
        refs = self._dedupe_refs(self._scan_assets(limit=limit))
        await self._commit_asset_refs(session, refs, knowledge_base_id=DEFAULT_KNOWLEDGE_BASE_ID)
        return len(refs)

    async def sync_from_documents(self, session: AsyncSession, *, limit: int = 500) -> int:
        stmt = (
            select(KnowledgeDocument)
            .where(
                KnowledgeDocument.knowledge_base_id == DEFAULT_KNOWLEDGE_BASE_ID,
                KnowledgeDocument.source_type == "file_upload",
            )
            .order_by(KnowledgeDocument.updated_at.desc())
            .limit(max(1, min(limit, 2000)))
        )
        result = await session.execute(stmt)
        count = 0
        for document in result.scalars():
            path = Path(document.storage_path)
            if path.suffix.lower() not in TABLE_SUFFIXES:
                continue
            assets = await self.register_path(
                session,
                path,
                virtual_path=document.virtual_path,
                knowledge_base_id=document.knowledge_base_id,
                document_id=document.id,
            )
            count += len(assets)
        return count

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
                assets.extend(self._asset_refs_for_path(path, content_sha256=_sha256(path)))
            except Exception:
                continue
        return sorted(assets, key=lambda asset: (asset.modified_at, asset.file_name, asset.sheet_name or ""), reverse=True)[:limit]

    @staticmethod
    def _dedupe_refs(refs: list[TableAssetRef]) -> list[TableAssetRef]:
        deduped: dict[str, TableAssetRef] = {}
        for ref in refs:
            deduped[ref.asset_id] = ref
        return list(deduped.values())

    async def _commit_asset_refs(
        self,
        session: AsyncSession,
        refs: list[TableAssetRef],
        *,
        knowledge_base_id: str,
    ) -> list[KnowledgeTableAsset]:
        if not refs:
            return []
        last_error: IntegrityError | None = None
        for _attempt in range(3):
            assets = [
                await self._upsert_asset_ref(session, ref, knowledge_base_id=knowledge_base_id)
                for ref in refs
            ]
            try:
                await session.commit()
                refreshed: list[KnowledgeTableAsset] = []
                for asset in assets:
                    current = await session.get(KnowledgeTableAsset, asset.asset_id)
                    if current is not None:
                        refreshed.append(current)
                return refreshed
            except IntegrityError as exc:
                last_error = exc
                await session.rollback()
        raise TableCatalogError(f"Failed to update table asset catalog: {last_error}") from last_error

    @staticmethod
    def _is_catalog_asset_path(relative_path: Path) -> bool:
        parts = relative_path.parts
        if not parts:
            return False
        if parts[0] in IGNORED_TOP_LEVEL_DIRS:
            return False
        return True

    def _asset_refs_for_path(
        self,
        path: Path,
        *,
        virtual_path: str | None = None,
        document_id: str | None = None,
        content_sha256: str = "",
    ) -> list[TableAssetRef]:
        suffix = path.suffix.lower()
        stat = path.stat()
        if virtual_path is None:
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
                modified_at=_utc_datetime(stat.st_mtime),
                content_sha256=content_sha256,
                document_id=document_id,
            )
            for sheet in sheets
        ]

    async def _upsert_asset_ref(
        self,
        session: AsyncSession,
        ref: TableAssetRef,
        *,
        knowledge_base_id: str,
    ) -> KnowledgeTableAsset:
        with session.no_autoflush:
            asset = await session.get(KnowledgeTableAsset, ref.asset_id)
            if asset is None:
                stmt = select(KnowledgeTableAsset).where(
                    KnowledgeTableAsset.knowledge_base_id == knowledge_base_id,
                    KnowledgeTableAsset.virtual_path == ref.virtual_path,
                    KnowledgeTableAsset.sheet_name == ref.sheet_name,
                )
                asset = (await session.execute(stmt)).scalars().first()
        profile_path = self.profile_path(ref.asset_id)
        profile = self._read_profile(profile_path) if profile_path.exists() else None
        rows = None
        columns_count = None
        columns: list[str] = []
        if profile:
            shape = profile.get("shape") if isinstance(profile.get("shape"), list) else []
            rows = int(shape[0]) if len(shape) > 0 and shape[0] is not None else None
            columns_count = int(shape[1]) if len(shape) > 1 and shape[1] is not None else None
            columns = [str(column.get("name")) for column in profile.get("columns", []) if isinstance(column, dict) and column.get("name")]

        if asset is None:
            asset = KnowledgeTableAsset(asset_id=ref.asset_id, knowledge_base_id=knowledge_base_id)
            session.add(asset)
        asset.document_id = ref.document_id or asset.document_id
        asset.source_type = ref.source_type
        asset.file_name = ref.file_name
        asset.storage_path = str(ref.path)
        asset.virtual_path = ref.virtual_path
        asset.sheet_name = ref.sheet_name
        asset.size_bytes = ref.size_bytes
        asset.modified_at = ref.modified_at
        asset.content_sha256 = ref.content_sha256 or asset.content_sha256 or ""
        asset.profile_status = "ready" if profile else (asset.profile_status or "missing")
        asset.profile_path = str(profile_path)
        asset.rows = rows if rows is not None else asset.rows
        asset.columns_count = columns_count if columns_count is not None else asset.columns_count
        asset.columns = columns or asset.columns or []
        asset.reference_status = asset.reference_status or "pending"
        asset.asset_metadata = {
            **(asset.asset_metadata or {}),
            "catalog_source": "scan_or_import",
        }
        return asset

    def _asset_to_dict(self, asset: KnowledgeTableAsset, *, include_profile: bool) -> dict[str, Any]:
        profile = self._read_profile(Path(asset.profile_path)) if asset.profile_path and Path(asset.profile_path).exists() else None
        profile_status = "ready" if profile else "missing"
        result: dict[str, Any] = {
            "asset_id": asset.asset_id,
            "file_name": asset.file_name,
            "source_type": asset.source_type,
            "virtual_path": asset.virtual_path,
            "sheet_name": asset.sheet_name,
            "size_bytes": asset.size_bytes,
            "modified_at": _iso(asset.modified_at) or _iso(asset.updated_at) or "",
            "profile_status": profile_status,
            "profile_path": asset.profile_path,
            "rows": asset.rows,
            "columns_count": asset.columns_count,
            "columns": asset.columns or [],
            "reference_status": asset.reference_status,
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
    def _ref_from_model(asset: KnowledgeTableAsset) -> TableAssetRef:
        path = Path(asset.storage_path)
        if not path.exists():
            raise TableCatalogError(f"Table asset file not found: {path}")
        return TableAssetRef(
            asset_id=asset.asset_id,
            path=path,
            virtual_path=asset.virtual_path,
            file_name=asset.file_name,
            source_type=asset.source_type,
            sheet_name=asset.sheet_name,
            size_bytes=asset.size_bytes,
            modified_at=asset.modified_at or datetime.now(timezone.utc),
            content_sha256=asset.content_sha256,
            document_id=asset.document_id,
        )

    @staticmethod
    @staticmethod
    def _load_dataframe(asset: TableAssetRef, *, sample_rows: int | None = None):
        import pandas as pd

        nrows = max(1, sample_rows) if sample_rows else None
        if asset.source_type == "excel":
            return pd.read_excel(asset.path, sheet_name=asset.sheet_name, nrows=nrows)
        sep = "\t" if asset.source_type == "tsv" else ","
        try:
            return pd.read_csv(asset.path, sep=sep, nrows=nrows)
        except UnicodeDecodeError:
            return pd.read_csv(asset.path, sep=sep, encoding="gb18030", nrows=nrows)

    @staticmethod
    def _table_shape(asset: TableAssetRef, *, sample_df: Any) -> list[int] | None:
        """Return actual table shape without fully materializing large files when possible."""

        if asset.source_type == "excel" and asset.path.suffix.lower() == ".xlsx":
            try:
                from openpyxl import load_workbook

                workbook = load_workbook(asset.path, read_only=True, data_only=True)
                try:
                    sheet = workbook[asset.sheet_name] if asset.sheet_name else workbook.active
                    rows = max(int(sheet.max_row or 0) - 1, 0)
                    columns = int(sheet.max_column or sample_df.shape[1])
                    return [rows, columns]
                finally:
                    workbook.close()
            except Exception:
                return [int(sample_df.shape[0]), int(sample_df.shape[1])]

        if asset.source_type in {"csv", "tsv"}:
            try:
                with asset.path.open("rb") as handle:
                    rows = sum(1 for _ in handle)
                return [max(rows - 1, 0), int(sample_df.shape[1])]
            except Exception:
                return [int(sample_df.shape[0]), int(sample_df.shape[1])]

        return [int(sample_df.shape[0]), int(sample_df.shape[1])]

    @staticmethod
    def _column_profiles(df: Any, base_profile: dict[str, Any]) -> list[dict[str, Any]]:
        columns: list[dict[str, Any]] = []
        dtypes = base_profile.get("dtypes", {}) if isinstance(base_profile.get("dtypes"), dict) else {}
        for column in df.columns:
            name = str(column)
            series = df[column]
            non_null = int(series.notna().sum())
            distinct_count = int(series.dropna().nunique())
            distinct_ratio = distinct_count / non_null if non_null else 0
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
                    "distinct_count": distinct_count,
                    "distinct_ratio": round(distinct_ratio, 6),
                    "sample_values": sample_values,
                    "semantic_role_hint": "measure_candidate" if str(series.dtype).startswith(("int", "float")) else "dimension_candidate",
                }
            )
        return columns
