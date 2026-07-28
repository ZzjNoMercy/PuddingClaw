from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from analytics.table_catalog import TableAssetCatalog, TableCatalogError
from knowledge.models import Base, KnowledgeBase, KnowledgeTableAsset
from knowledge.service import DEFAULT_KNOWLEDGE_BASE_ID


def test_remove_table_asset_detaches_entire_workbook_and_keeps_source_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    async def run() -> None:
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'catalog.db'}")
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        session_factory = async_sessionmaker(engine, expire_on_commit=False)
        knowledge_root = tmp_path / "knowledge"
        monkeypatch.setenv("PUDDINGCLAW_KNOWLEDGE_DIR", str(knowledge_root))
        catalog = TableAssetCatalog(tmp_path / "backend")
        workbook = catalog.knowledge_root / "imported" / "sales.xlsx"
        workbook.parent.mkdir(parents=True, exist_ok=True)
        workbook.write_bytes(b"source workbook remains in the knowledge library")

        async with session_factory() as session:
            session.add(KnowledgeBase(id=DEFAULT_KNOWLEDGE_BASE_ID, name="Default", description=""))
            first_profile = catalog.profile_path("tbl_sales_1")
            second_profile = catalog.profile_path("tbl_sales_2")
            first_profile.parent.mkdir(parents=True, exist_ok=True)
            first_profile.write_text("{}", encoding="utf-8")
            second_profile.write_text("{}", encoding="utf-8")
            for asset_id, sheet_name, profile_path in [
                ("tbl_sales_1", "Sheet 1", first_profile),
                ("tbl_sales_2", "Sheet 2", second_profile),
            ]:
                session.add(
                    KnowledgeTableAsset(
                        asset_id=asset_id,
                        knowledge_base_id=DEFAULT_KNOWLEDGE_BASE_ID,
                        source_type="xlsx",
                        file_name="sales.xlsx",
                        storage_path=str(workbook),
                        virtual_path=f"/knowledge/imported/sales.xlsx#{sheet_name}",
                        sheet_name=sheet_name,
                        size_bytes=workbook.stat().st_size,
                        content_sha256="test",
                        profile_status="ready",
                        profile_path=str(profile_path),
                        rows=10,
                        columns_count=2,
                        columns=["brand", "series"],
                        reference_status="ready",
                    )
                )
            await session.commit()

            result = await catalog.remove_asset(session, "tbl_sales_1")
            assert result["removed_asset_ids"] == ["tbl_sales_1", "tbl_sales_2"]
            assert result["source_file_preserved"] is True
            assert workbook.is_file()
            assert not first_profile.exists()
            assert not second_profile.exists()
            assert await catalog.list_assets(session, ensure_scanned=False) == []
            with pytest.raises(TableCatalogError):
                await catalog.get_asset(session, "tbl_sales_1")

            assets = list((await session.execute(select(KnowledgeTableAsset))).scalars())
            assert {asset.reference_status for asset in assets} == {"removed"}
            assert {asset.profile_status for asset in assets} == {"missing"}

        await engine.dispose()

    asyncio.run(run())
