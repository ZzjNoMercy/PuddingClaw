"""API-level E2E coverage for vertical concat logical datasets."""

from __future__ import annotations

import asyncio
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from api import analytics as analytics_api
from knowledge.models import Base, KnowledgeBase, KnowledgeTableAsset
from knowledge.service import DEFAULT_KNOWLEDGE_BASE_ID


async def _add_asset(session, asset_id: str, path: Path) -> None:
    session.add(
        KnowledgeTableAsset(
            asset_id=asset_id,
            knowledge_base_id=DEFAULT_KNOWLEDGE_BASE_ID,
            source_type="csv",
            file_name=path.name,
            storage_path=str(path),
            virtual_path=f"/knowledge/imported/{path.name}",
            sheet_name=None,
            size_bytes=path.stat().st_size,
            content_sha256="test",
            profile_status="missing",
            profile_path="",
            rows=1,
            columns_count=None,
            columns=[],
            reference_status="ready",
        )
    )


def test_concat_api_e2e_schema_strategies_and_later_append(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    async def run() -> None:
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'catalog.db'}")
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        session_factory = async_sessionmaker(engine, expire_on_commit=False)
        monkeypatch.setenv("PUDDINGCLAW_KNOWLEDGE_DIR", str(tmp_path / "knowledge"))
        monkeypatch.setattr(analytics_api, "BASE_DIR", tmp_path / "backend")

        files = {
            "jan": "品牌,车系,销量\n比亚迪,秦PLUS,10\n",
            "feb": "销量,车系,品牌\n20,宋PLUS,比亚迪\n",
            "mar": "品牌,车系,上险量\n比亚迪,海豹,30\n",
        }
        paths: dict[str, Path] = {}
        for month, content in files.items():
            path = tmp_path / f"{month}.csv"
            path.write_text(content, encoding="utf-8")
            paths[month] = path
        async with session_factory() as session:
            session.add(KnowledgeBase(id=DEFAULT_KNOWLEDGE_BASE_ID, name="Default", description=""))
            for month, path in paths.items():
                await _add_asset(session, f"tbl_{month}", path)
            await session.commit()

        async def session_override():
            async with session_factory() as session:
                yield session

        app = FastAPI()
        app.include_router(analytics_api.router, prefix="/api")
        app.dependency_overrides[analytics_api.get_db_session] = session_override
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            # Same fields, even in a different order: direct strict creation succeeds.
            preview = await client.post("/api/analytics/table-assets/concat-datasets/preview", json={"source_asset_ids": ["tbl_jan", "tbl_feb"]})
            assert preview.status_code == 200
            assert preview.json()["has_schema_drift"] is False
            created = await client.post("/api/analytics/table-assets/concat-datasets", json={"name": "上险量", "source_asset_ids": ["tbl_jan", "tbl_feb"]})
            assert created.status_code == 200
            dataset = created.json()["asset"]
            assert dataset["rows"] == 2

            # Later source has one missing baseline field and one extra field.
            drift = await client.post("/api/analytics/table-assets/concat-datasets/preview", json={"source_asset_ids": ["tbl_jan", "tbl_feb", "tbl_mar"]})
            assert drift.status_code == 200
            assert drift.json()["has_schema_drift"] is True
            assert drift.json()["sources"][2]["missing_from_baseline"] == ["销量"]
            assert drift.json()["sources"][2]["extra_vs_baseline"] == ["上险量"]

            strict_append = await client.post(
                f"/api/analytics/table-assets/{dataset['asset_id']}/concat-sources",
                json={"source_asset_ids": ["tbl_mar"], "schema_mode": "strict"},
            )
            assert strict_append.status_code == 400

            keep_extra = await client.post(
                f"/api/analytics/table-assets/{dataset['asset_id']}/concat-sources",
                json={"source_asset_ids": ["tbl_mar"], "schema_mode": "union_fill_missing"},
            )
            assert keep_extra.status_code == 200
            kept_asset = keep_extra.json()["asset"]
            assert kept_asset["rows"] == 3
            assert "上险量" in kept_asset["columns"]
            assert kept_asset["logical_dataset"]["source_asset_ids"] == ["tbl_jan", "tbl_feb", "tbl_mar"]

            drop_extra = await client.post(
                "/api/analytics/table-assets/concat-datasets",
                json={"name": "上险量-基准字段", "source_asset_ids": ["tbl_jan", "tbl_mar"], "schema_mode": "baseline_fill_missing"},
            )
            assert drop_extra.status_code == 200
            dropped_asset = drop_extra.json()["asset"]
            assert "上险量" not in dropped_asset["columns"]
            assert dropped_asset["logical_dataset"]["schema_mode"] == "baseline_fill_missing"

        await engine.dispose()

    asyncio.run(run())
