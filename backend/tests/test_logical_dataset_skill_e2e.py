"""End-to-end contract for the logical-dataset Skill's confirmed-rule path."""

from __future__ import annotations

import asyncio
from io import BytesIO
import json
from pathlib import Path

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from graph.logical_dataset_resume import LogicalDatasetResumeRegistry
from graph.attachment_store import attachment_store
from knowledge.models import Base, KnowledgeBase, KnowledgeTableAsset
from knowledge.service import DEFAULT_KNOWLEDGE_BASE_ID
from tools import logical_dataset_tools
from tools.request_logical_dataset_rule_tool import _logical_dataset_business_fields


async def _add_asset(session, *, asset_id: str, path: Path, columns: list[str]) -> None:
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
            content_sha256="e2e",
            profile_status="missing",
            profile_path="",
            rows=1,
            columns_count=len(columns),
            columns=columns,
            reference_status="ready",
        )
    )


def test_logical_dataset_rule_to_virtual_dataset_e2e(tmp_path: Path, monkeypatch) -> None:
    async def run() -> None:
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'catalog.db'}")
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        session_factory = async_sessionmaker(engine, expire_on_commit=False)
        monkeypatch.setenv("PUDDINGCLAW_KNOWLEDGE_DIR", str(tmp_path / "knowledge"))
        monkeypatch.setattr(logical_dataset_tools, "get_sessionmaker", lambda: session_factory)

        files = {
            "tbl_jan": ("jan.csv", "品牌,车系,销量\n比亚迪,秦PLUS,10\n", ["品牌", "车系", "销量"]),
            "tbl_feb": ("feb.csv", "销量,车系,品牌\n20,宋PLUS,比亚迪\n", ["销量", "车系", "品牌"]),
            "tbl_mar": ("mar.csv", "品牌,车系,上险量\n比亚迪,海豹,30\n", ["品牌", "车系", "上险量"]),
        }
        async with session_factory() as session:
            session.add(KnowledgeBase(id=DEFAULT_KNOWLEDGE_BASE_ID, name="Default", description=""))
            for asset_id, (file_name, content, columns) in files.items():
                path = tmp_path / file_name
                path.write_text(content, encoding="utf-8")
                await _add_asset(session, asset_id=asset_id, path=path, columns=columns)
            await session.commit()

        # Agent step 1: inspect reusable candidates.
        candidates_payload = await logical_dataset_tools.ListLogicalDatasetCandidatesTool()._arun(limit=10)
        candidates = json.loads(candidates_payload.split("\n", 1)[1])
        assert {item["asset_id"] for item in candidates} == set(files)

        # Agent step 2 + user HITL: choose Feb as the baseline and a strict create.
        registry = LogicalDatasetResumeRegistry()
        request = registry.create(
            session_id="session-e2e",
            query_id="query-e2e",
            tool_call_id="tool-e2e",
            payload={"operation": "create", "target_asset_id": "", "candidates": candidates},
        )
        create_waiter = asyncio.create_task(registry.wait(request["id"]))
        await asyncio.sleep(0)
        registry.resolve(
            request["id"],
            {
                "action": "confirm",
                "name": "2023年上险量",
                "baseline_asset_id": "tbl_feb",
                "source_asset_ids": ["tbl_jan", "tbl_feb"],
                "schema_mode": "strict",
            },
        )
        create_rule = (await create_waiter)["dataset_rule"]
        assert create_rule["source_asset_ids"] == ["tbl_feb", "tbl_jan"]

        # Agent step 3: execute exactly the user-confirmed rule.
        apply_tool = logical_dataset_tools.ApplyLogicalDatasetRuleTool()
        created = json.loads((await apply_tool._arun(**create_rule)).split("\n", 1)[1])
        assert created["rows"] == 2
        assert created["logical_dataset"]["materialization"] == "virtual"
        assert created["logical_dataset"]["schema_mode"] == "strict"

        # Later append: the existing logical dataset is the baseline; only the new
        # raw source is selected and it must never be passed as a nested source.
        append_request = registry.create(
            session_id="session-e2e",
            query_id="query-e2e-append",
            tool_call_id="tool-e2e-append",
            payload={
                "operation": "append",
                "target_asset_id": created["asset_id"],
                "target": {"asset_id": created["asset_id"], "display_name": "2023年上险量", "fields": created["columns"]},
                "candidates": [next(item for item in candidates if item["asset_id"] == "tbl_mar")],
            },
        )
        append_waiter = asyncio.create_task(registry.wait(append_request["id"]))
        await asyncio.sleep(0)
        registry.resolve(
            append_request["id"],
            {
                "action": "confirm",
                "name": "2023年上险量",
                "baseline_asset_id": created["asset_id"],
                "source_asset_ids": ["tbl_mar"],
                "schema_mode": "union_fill_missing",
            },
        )
        append_rule = (await append_waiter)["dataset_rule"]
        assert append_rule["source_asset_ids"] == ["tbl_mar"]
        assert append_rule["baseline_asset_id"] == created["asset_id"]
        appended = json.loads((await apply_tool._arun(**append_rule)).split("\n", 1)[1])
        assert appended["rows"] == 3
        assert appended["logical_dataset"]["source_asset_ids"] == ["tbl_feb", "tbl_jan", "tbl_mar"]
        assert "上险量" in appended["columns"]

        await engine.dispose()

    asyncio.run(run())


def test_skill_imports_new_spreadsheet_attachment_before_listing_candidates(tmp_path: Path, monkeypatch) -> None:
    async def run() -> None:
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'catalog.db'}")
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        session_factory = async_sessionmaker(engine, expire_on_commit=False)
        monkeypatch.setenv("PUDDINGCLAW_KNOWLEDGE_DIR", str(tmp_path / "knowledge"))
        monkeypatch.setattr(logical_dataset_tools, "get_sessionmaker", lambda: session_factory)
        attachment_store.initialize(tmp_path / "backend")
        async with session_factory() as session:
            session.add(KnowledgeBase(id=DEFAULT_KNOWLEDGE_BASE_ID, name="Default", description=""))
            await session.commit()

        item = attachment_store.save(
            session_id="session-attachment",
            filename="2023年12月上险量.csv",
            mime_type="text/csv",
            source="upload",
            stream=BytesIO("品牌,1-子车型,销量\n比亚迪,秦PLUS,100\n".encode("utf-8")),
        )
        tool = logical_dataset_tools.EnsureAttachmentTableAssetTool(session_id="session-attachment")
        first = json.loads((await tool._arun(attachment_id=item["id"])).split("\n", 1)[1])
        assert first["deduplicated"] is False
        assert len(first["assets"]) == 1
        assert first["assets"][0]["fields"] == ["品牌", "1-子车型", "销量"]

        second = json.loads((await tool._arun(attachment_id=item["id"])).split("\n", 1)[1])
        assert second["deduplicated"] is True
        assert second["assets"][0]["asset_id"] == first["assets"][0]["asset_id"]
        candidates = json.loads((await logical_dataset_tools.ListLogicalDatasetCandidatesTool()._arun(limit=10)).split("\n", 1)[1])
        assert candidates[0]["asset_id"] == first["assets"][0]["asset_id"]
        await engine.dispose()

    asyncio.run(run())


def test_append_target_uses_business_schema_not_runtime_lineage_fields() -> None:
    assert _logical_dataset_business_fields(
        {
            "columns": ["品牌", "车系", "_pc_source_asset_id"],
            "logical_dataset": {"canonical_columns": ["品牌", "车系"]},
        }
    ) == ["品牌", "车系"]
