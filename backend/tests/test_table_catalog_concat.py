"""Regression coverage for materialized vertical-concat logical datasets."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from analytics.table_catalog import DERIVED_CONCAT_LINEAGE_COLUMNS, TableAssetCatalog, TableCatalogError
from knowledge.models import Base, KnowledgeBase, KnowledgeTableAsset
from knowledge.service import DEFAULT_KNOWLEDGE_BASE_ID


async def _add_csv_asset(session, *, asset_id: str, path: Path, columns: list[str]) -> None:
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
            columns_count=len(columns),
            columns=columns,
            reference_status="ready",
        )
    )


def test_virtual_concat_registers_strict_schema_and_reads_with_lineage(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    async def run() -> None:
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'catalog.db'}")
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        session_factory = async_sessionmaker(engine, expire_on_commit=False)
        monkeypatch.setenv("PUDDINGCLAW_KNOWLEDGE_DIR", str(tmp_path / "knowledge"))
        first = tmp_path / "jan.csv"
        second = tmp_path / "feb.csv"
        first.write_text("品牌,车系,销量\n比亚迪,秦PLUS,10\n", encoding="utf-8")
        # Column order may differ, but the strict field set is the same.
        second.write_text("销量,车系,品牌\n20,宋PLUS,比亚迪\n", encoding="utf-8")

        catalog = TableAssetCatalog(tmp_path / "backend")
        async with session_factory() as session:
            session.add(KnowledgeBase(id=DEFAULT_KNOWLEDGE_BASE_ID, name="Default", description=""))
            await _add_csv_asset(session, asset_id="tbl_jan", path=first, columns=["品牌", "车系", "销量"])
            await _add_csv_asset(session, asset_id="tbl_feb", path=second, columns=["销量", "车系", "品牌"])
            await session.commit()

            dataset = await catalog.create_concat_dataset(session, name="2023年上险量", source_asset_ids=["tbl_jan", "tbl_feb"])
            assert dataset["source_type"] == "logical_concat"
            assert dataset["logical_dataset"]["materialization"] == "virtual"
            assert dataset["rows"] == 2
            assert dataset["logical_dataset"]["source_asset_ids"] == ["tbl_jan", "tbl_feb"]
            assert set(DERIVED_CONCAT_LINEAGE_COLUMNS).issubset(dataset["columns"])

            _asset, frame = await catalog.load_dataframe_for_asset(session, dataset["asset_id"])
            assert frame["车系"].tolist() == ["秦PLUS", "宋PLUS"]
            assert frame["_pc_source_asset_id"].tolist() == ["tbl_jan", "tbl_feb"]

            first.write_text("品牌,车系,销量\n比亚迪,秦PLUS,10\n比亚迪,汉,30\n", encoding="utf-8")
            refreshed = await catalog.refresh_concat_dataset(session, dataset["asset_id"])
            assert refreshed["asset_id"] == dataset["asset_id"]
            # Virtual definitions do not scan source files at refresh time; row metadata follows source profiles.
            assert refreshed["rows"] == 2
            _asset, refreshed_frame = await catalog.load_dataframe_for_asset(session, dataset["asset_id"])
            assert len(refreshed_frame) == 3

            march = tmp_path / "mar.csv"
            march.write_text("品牌,车系,销量\n比亚迪,海豹,40\n", encoding="utf-8")
            await _add_csv_asset(session, asset_id="tbl_mar", path=march, columns=["品牌", "车系", "销量"])
            await session.commit()
            appended = await catalog.append_concat_dataset_sources(
                session,
                asset_id=dataset["asset_id"],
                source_asset_ids=["tbl_mar"],
                schema_mode="strict",
            )
            assert appended["asset_id"] == dataset["asset_id"]
            assert appended["logical_dataset"]["source_asset_ids"] == ["tbl_jan", "tbl_feb", "tbl_mar"]
            assert appended["rows"] == 3
            _asset, appended_frame = await catalog.load_dataframe_for_asset(session, dataset["asset_id"])
            assert len(appended_frame) == 4
        await engine.dispose()

    asyncio.run(run())


def test_virtual_concat_profile_and_definition_edit_do_not_materialize_sources(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    async def run() -> None:
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'catalog.db'}")
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        session_factory = async_sessionmaker(engine, expire_on_commit=False)
        monkeypatch.setenv("PUDDINGCLAW_KNOWLEDGE_DIR", str(tmp_path / "knowledge"))
        jan = tmp_path / "jan.csv"
        feb = tmp_path / "feb.csv"
        jan.write_text("月份,品牌,销量\n2023-01,比亚迪,10\n", encoding="utf-8")
        feb.write_text("月份,品牌,销量\n2023-02,比亚迪,20\n", encoding="utf-8")
        catalog = TableAssetCatalog(tmp_path / "backend")
        async with session_factory() as session:
            session.add(KnowledgeBase(id=DEFAULT_KNOWLEDGE_BASE_ID, name="Default", description=""))
            await _add_csv_asset(session, asset_id="tbl_jan", path=jan, columns=["月份", "品牌", "销量"])
            await _add_csv_asset(session, asset_id="tbl_feb", path=feb, columns=["月份", "品牌", "销量"])
            await session.commit()
            dataset = await catalog.create_concat_dataset(
                session,
                name="月度销量",
                description="月度销量统一视图",
                tags=["销量"],
                source_asset_ids=["tbl_jan", "tbl_feb"],
            )
            assert dataset["profile_status"] == "missing"

            profiled = await catalog.generate_profile(session, dataset["asset_id"], include_profile=True)
            assert profiled["profile_status"] == "ready"
            assert profiled["profile"]["kind"] == "logical_dataset_profile"
            assert profiled["profile"]["summary"]["fresh_source_count"] == 2
            coverage = profiled["logical_dataset"]["coverage"]
            assert len(coverage) == 2
            assert coverage[0]["dimensions"][0]["field"] == "月份"
            assert coverage[0]["dimensions"][0]["min"].startswith("2023-01")

            # A later source-file change must be refreshed by the logical Profile action,
            # not left as a misleading successful-but-partial aggregate.
            jan.write_text("月份,品牌,销量\n2023-01,比亚迪,10\n2023-03,比亚迪,30\n", encoding="utf-8")
            refreshed_profile = await catalog.generate_profile(session, dataset["asset_id"], include_profile=True)
            assert refreshed_profile["profile_status"] == "ready"
            assert refreshed_profile["profile"]["summary"]["fresh_source_count"] == 2

            updated = await catalog.update_logical_dataset_definition(
                session,
                asset_id=dataset["asset_id"],
                name="2023 月度销量",
                description="仅用于跨月趋势和环比分析",
                tags=["销量", "月度"],
                preferred_intents=["trend", "period_comparison"],
                direct_source_allowed=False,
            )
            assert updated["file_name"] == "2023 月度销量"
            assert updated["logical_dataset"]["source_asset_ids"] == ["tbl_jan", "tbl_feb"]
            assert updated["logical_dataset"]["description"] == "仅用于跨月趋势和环比分析"
            assert updated["logical_dataset"]["routing"]["direct_source_allowed"] is False
            assert updated["logical_dataset"]["profile"]["fresh_source_count"] == 2
        await engine.dispose()

    asyncio.run(run())


def test_vertical_concat_rejects_schema_drift(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    async def run() -> None:
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'catalog.db'}")
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        session_factory = async_sessionmaker(engine, expire_on_commit=False)
        monkeypatch.setenv("PUDDINGCLAW_KNOWLEDGE_DIR", str(tmp_path / "knowledge"))
        first = tmp_path / "jan.csv"
        second = tmp_path / "bad.csv"
        first.write_text("品牌,车系,销量\n比亚迪,秦PLUS,10\n", encoding="utf-8")
        second.write_text("品牌,车系,上险量\n比亚迪,宋PLUS,20\n", encoding="utf-8")
        catalog = TableAssetCatalog(tmp_path / "backend")
        async with session_factory() as session:
            session.add(KnowledgeBase(id=DEFAULT_KNOWLEDGE_BASE_ID, name="Default", description=""))
            await _add_csv_asset(session, asset_id="tbl_jan", path=first, columns=["品牌", "车系", "销量"])
            await _add_csv_asset(session, asset_id="tbl_bad", path=second, columns=["品牌", "车系", "上险量"])
            await session.commit()
            with pytest.raises(TableCatalogError, match="字段不完全一致"):
                await catalog.create_concat_dataset(session, name="不一致测试", source_asset_ids=["tbl_jan", "tbl_bad"])
        await engine.dispose()

    asyncio.run(run())


def test_vertical_concat_union_fills_missing_fields_after_preview(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    async def run() -> None:
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'catalog.db'}")
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        session_factory = async_sessionmaker(engine, expire_on_commit=False)
        monkeypatch.setenv("PUDDINGCLAW_KNOWLEDGE_DIR", str(tmp_path / "knowledge"))
        first = tmp_path / "jan.csv"
        second = tmp_path / "feb.csv"
        first.write_text("品牌,车系,销量\n比亚迪,秦PLUS,10\n", encoding="utf-8")
        second.write_text("品牌,车系,上险量\n比亚迪,宋PLUS,20\n", encoding="utf-8")
        catalog = TableAssetCatalog(tmp_path / "backend")
        async with session_factory() as session:
            session.add(KnowledgeBase(id=DEFAULT_KNOWLEDGE_BASE_ID, name="Default", description=""))
            await _add_csv_asset(session, asset_id="tbl_jan", path=first, columns=["品牌", "车系", "销量"])
            await _add_csv_asset(session, asset_id="tbl_feb", path=second, columns=["品牌", "车系", "上险量"])
            await session.commit()

            preview = await catalog.preview_concat_dataset(session, source_asset_ids=["tbl_jan", "tbl_feb"])
            assert preview["has_schema_drift"] is True
            assert preview["canonical_columns"] == ["品牌", "车系", "销量", "上险量"]
            assert preview["sources"][1]["missing_from_baseline"] == ["销量"]
            assert preview["sources"][1]["extra_vs_baseline"] == ["上险量"]

            dataset = await catalog.create_concat_dataset(
                session,
                name="允许差异测试",
                source_asset_ids=["tbl_jan", "tbl_feb"],
                schema_mode="union_fill_missing",
            )
            assert dataset["logical_dataset"]["schema_mode"] == "union_fill_missing"
            _asset, frame = await catalog.load_dataframe_for_asset(session, dataset["asset_id"])
            assert frame["销量"].tolist()[0] == 10
            assert frame["销量"].isna().tolist()[1] is True
            assert frame["上险量"].isna().tolist()[0] is True
            assert frame["上险量"].tolist()[1] == 20

            baseline_only = await catalog.create_concat_dataset(
                session,
                name="丢弃额外字段测试",
                source_asset_ids=["tbl_jan", "tbl_feb"],
                schema_mode="baseline_fill_missing",
            )
            assert baseline_only["logical_dataset"]["schema_mode"] == "baseline_fill_missing"
            assert baseline_only["logical_dataset"]["canonical_columns"] == ["品牌", "车系", "销量"]
            _asset, baseline_frame = await catalog.load_dataframe_for_asset(session, baseline_only["asset_id"])
            assert "上险量" not in baseline_frame.columns
            assert baseline_frame["销量"].isna().tolist()[1] is True
        await engine.dispose()

    asyncio.run(run())
