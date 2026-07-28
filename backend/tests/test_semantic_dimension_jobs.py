"""Regression tests for the independent semantic-dimension build queue."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import api.analytics as analytics_api
from api.analytics import SemanticDimensionBaselineChangeDecisionRequest
from knowledge.models import Base, SemanticDimensionBuildJob
from knowledge.semantic_dimension_jobs import (
    claim_next_semantic_dimension_build_job,
    create_semantic_dimension_build_job,
    list_task_notifications,
    mark_semantic_dimension_build_waiting_baseline_change,
    mark_semantic_dimension_build_waiting_publish,
    resolve_semantic_dimension_baseline_change,
    semantic_dimension_job_to_dict,
)
from knowledge.semantic_dimension_publisher import publish_semantic_dimension_build
from knowledge.semantic_dimension_crosswalk import get_matching_overview, publish_generated_crosswalk


def _crosswalk(*, include_han: bool = True) -> dict:
    records = [
        {
            "entity": {"entity_key": "byd::qinplus", "canonical_brand": "比亚迪", "canonical_serial_name": "秦PLUS"},
            "bindings": [{"source_kind": "database_table", "source_ref": "database:insight:vehicle_model_base", "key_fields": {"brand": "比亚迪", "serial_name": "秦PLUS"}}],
            "resolution": {"status": "auto_matched", "join_eligible": True},
        }
    ]
    if include_han:
        records.append({
            "entity": {"entity_key": "byd::han", "canonical_brand": "比亚迪", "canonical_serial_name": "汉"},
            "bindings": [{"source_kind": "database_table", "source_ref": "database:insight:vehicle_model_base", "key_fields": {"brand": "比亚迪", "serial_name": "汉"}}],
            "resolution": {"status": "auto_matched", "join_eligible": True},
        })
    return {
        "formatter": "entity-resolution-crosswalk",
        "schema_version": "entity-resolution-crosswalk/v1",
        "entity_type": "vehicle_series",
        "records": records,
        "source_diagnostics": [],
    }


def _write_publishable_dimension(base_dir: Path) -> None:
    dimension = base_dir / "semantic-assets" / "dimensions" / "vehicle_series"
    dimension.mkdir(parents=True)
    (dimension / "dimension.md").write_text(
        "---\n"
        "id: vehicle_series\n"
        "resolution:\n"
        "  reference_path: references/active_crosswalk.json\n"
        "build_skill:\n"
        "  adapter: entity_crosswalk_v1\n"
        "updated_at: 2026-07-11 00:00:00\n"
        "---\n",
        encoding="utf-8",
    )


def test_semantic_dimension_build_job_is_deduplicated_and_never_auto_published(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'jobs.db'}")
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        sessionmaker = async_sessionmaker(engine, expire_on_commit=False)
        async with sessionmaker() as session:
            first, queued = await create_semantic_dimension_build_job(
                session,
                dimension_id="vehicle_series",
                adapter="vehicle_series_full",
                requested_scope={"brands": "all"},
                input_snapshot={"sales_file_name": "sales.xlsx"},
                session_id="session_demo",
            )
            duplicate, queued_again = await create_semantic_dimension_build_job(
                session,
                dimension_id="vehicle_series",
                adapter="vehicle_series_full",
                requested_scope={"brands": "all"},
                input_snapshot={"sales_file_name": "sales.xlsx"},
                session_id="session_demo",
            )
            assert queued is True
            assert queued_again is False
            assert duplicate.id == first.id

            claimed = await claim_next_semantic_dimension_build_job(session)
            assert claimed is not None
            await mark_semantic_dimension_build_waiting_publish(
                session,
                claimed,
                staging_path="/tmp/staging",
                published_reference_path="references/full_crosswalk.json",
                result_summary={"summary": {"sales_series_count": 12}},
            )

            stored = await session.get(SemanticDimensionBuildJob, first.id)
            assert stored is not None
            assert stored.status == "waiting_for_publish_confirmation"
            assert stored.published_reference_path == "references/full_crosswalk.json"
            assert stored.result_summary["summary"]["sales_series_count"] == 12
            serialized = semantic_dimension_job_to_dict(stored)
            assert serialized["finished_at"].endswith("+00:00")
            assert serialized["finished_at_display"].endswith("（北京时间）")
            notifications = await list_task_notifications(session, unread_only=True)
            assert len(notifications) == 1
            assert notifications[0].subject_id == first.id
        await engine.dispose()

    asyncio.run(run())


def test_publish_semantic_dimension_build_activates_crosswalk_and_backwrites_job(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'jobs.db'}")
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        sessionmaker = async_sessionmaker(engine, expire_on_commit=False)

        base_dir = tmp_path / "backend"
        dimension_dir = base_dir / "semantic-assets" / "dimensions" / "vehicle_series"
        dimension_dir.mkdir(parents=True)
        (dimension_dir / "dimension.md").write_text(
            "---\nid: vehicle_series\nresolution:\n  reference_path: references/byd_chery_demo.json\nbuild_skill:\n  adapter: vehicle_series_demo\nupdated_at: 2026-07-10 00:00:00\n---\n",
            encoding="utf-8",
        )
        staging_crosswalk = tmp_path / "staging" / "full_crosswalk.json"
        staging_crosswalk.parent.mkdir(parents=True)
        staging_crosswalk.write_text(
            json.dumps({"formatter": "entity-resolution-crosswalk", "entity_type": "vehicle_series", "records": []}),
            encoding="utf-8",
        )

        async with sessionmaker() as session:
            job, _ = await create_semantic_dimension_build_job(
                session,
                dimension_id="vehicle_series",
                adapter="vehicle_series_full",
                requested_scope={"brands": "all"},
            )
            claimed = await claim_next_semantic_dimension_build_job(session)
            assert claimed is not None
            await mark_semantic_dimension_build_waiting_publish(
                session,
                claimed,
                staging_path=str(staging_crosswalk.parent),
                published_reference_path="references/full_crosswalk.json",
                result_summary={"artifact_paths": {"crosswalk": str(staging_crosswalk)}},
            )
            result = await publish_semantic_dimension_build(session, base_dir=base_dir, job_id=job.id)

            assert result["already_published"] is False
            stored = await session.get(SemanticDimensionBuildJob, job.id)
            assert stored is not None
            assert stored.status == "published"
            generated_crosswalk = dimension_dir / "references" / "generated_crosswalk.json"
            active_crosswalk = dimension_dir / "references" / "active_crosswalk.json"
            assert json.loads(generated_crosswalk.read_text(encoding="utf-8")) == json.loads(staging_crosswalk.read_text(encoding="utf-8"))
            assert json.loads(active_crosswalk.read_text(encoding="utf-8"))["formatter"] == "entity-resolution-crosswalk"
            dimension_text = (dimension_dir / "dimension.md").read_text(encoding="utf-8")
            assert "reference_path: references/active_crosswalk.json" in dimension_text
            assert "adapter: vehicle_series_full" in dimension_text
            assert "updated_at: 2026-07-10 00:00:00" not in dimension_text
            notifications = await list_task_notifications(session, unread_only=True)
            assert any(item.title == "vehicle_series 维度已发布" for item in notifications)
        await engine.dispose()

    asyncio.run(run())


def test_baseline_change_waits_for_matching_management_decision(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'jobs.db'}")
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        sessionmaker = async_sessionmaker(engine, expire_on_commit=False)
        async with sessionmaker() as session:
            job, _ = await create_semantic_dimension_build_job(
                session, dimension_id="vehicle_series", adapter="entity_crosswalk_v1", session_id="session_demo"
            )
            claimed = await claim_next_semantic_dimension_build_job(session)
            assert claimed is not None
            await mark_semantic_dimension_build_waiting_baseline_change(
                session, claimed, staging_path="/tmp/staging", published_reference_path="references/active_crosswalk.json",
                result_summary={"baseline_delta": {"removed": [{"entity_key": "brand::series"}]}},
            )
            assert claimed.status == "waiting_for_baseline_change_confirmation"
            await resolve_semantic_dimension_baseline_change(session, claimed, action="inactive")
            assert claimed.status == "waiting_for_publish_confirmation"
            notifications = await list_task_notifications(session, unread_only=True)
            assert any("规范基准发生变化" in item.title for item in notifications)
        await engine.dispose()

    asyncio.run(run())


def test_baseline_shrink_e2e_keeps_active_unchanged_until_publish_then_inactivates(tmp_path, monkeypatch) -> None:
    """Exercise the real review boundary for a genuine canonical removal.

    This guards against both dangerous outcomes: silently changing active data
    before user review, and dropping historical entities after the user chooses
    to keep them as inactive.
    """

    async def run() -> None:
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'jobs.db'}")
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        sessionmaker = async_sessionmaker(engine, expire_on_commit=False)
        base_dir = tmp_path / "backend"
        _write_publishable_dimension(base_dir)
        publish_generated_crosswalk(base_dir, "vehicle_series", _crosswalk())
        references = base_dir / "semantic-assets" / "dimensions" / "vehicle_series" / "references"
        active_path = references / "active_crosswalk.json"
        active_before = json.loads(active_path.read_text(encoding="utf-8"))

        staging_path = tmp_path / "staging" / "full_crosswalk.json"
        staging_path.parent.mkdir(parents=True)
        staging_path.write_text(json.dumps(_crosswalk(include_han=False), ensure_ascii=False), encoding="utf-8")
        monkeypatch.setattr(analytics_api, "BASE_DIR", base_dir)

        async with sessionmaker() as session:
            job, _ = await create_semantic_dimension_build_job(
                session, dimension_id="vehicle_series", adapter="entity_crosswalk_v1", session_id="e2e-session"
            )
            claimed = await claim_next_semantic_dimension_build_job(session)
            assert claimed is not None
            await mark_semantic_dimension_build_waiting_baseline_change(
                session,
                claimed,
                staging_path=str(staging_path.parent),
                published_reference_path="references/active_crosswalk.json",
                result_summary={
                    "artifact_paths": {"crosswalk": str(staging_path)},
                    "baseline_delta": {"removed": [{"entity_key": "byd::han", "label": "比亚迪 / 汉"}]},
                },
            )

            pending = await analytics_api.get_semantic_dimension_baseline_change("vehicle_series", session=session)
            assert pending["change"]["job"]["id"] == job.id
            assert pending["change"]["baseline_delta"]["removed"][0]["entity_key"] == "byd::han"

            # The decision only mutates staging and draft overrides. Active is untouched.
            resolved = await analytics_api.resolve_semantic_dimension_baseline_change_request(
                job.id,
                SemanticDimensionBaselineChangeDecisionRequest(action="inactive"),
                session=session,
            )
            assert resolved["status"] == "resolved"
            assert json.loads(active_path.read_text(encoding="utf-8")) == active_before
            staged_after_decision = json.loads(staging_path.read_text(encoding="utf-8"))
            retained_han = next(item for item in staged_after_decision["records"] if item["entity"]["entity_key"] == "byd::han")
            assert retained_han["resolution"]["status"] == "inactive"
            assert retained_han["resolution"]["join_eligible"] is False

            publication = await publish_semantic_dimension_build(session, base_dir=base_dir, job_id=job.id)
            assert publication["already_published"] is False
            stored = await session.get(SemanticDimensionBuildJob, job.id)
            assert stored is not None and stored.status == "published"

        active_after = json.loads(active_path.read_text(encoding="utf-8"))
        han_after = next(item for item in active_after["records"] if item["entity"]["entity_key"] == "byd::han")
        assert han_after["resolution"]["status"] == "inactive"
        assert han_after["resolution"]["join_eligible"] is False
        assert (references / "versions" / f"{active_after['version']}.json").is_file()
        overview = get_matching_overview(base_dir, "vehicle_series", query="比亚迪汉")
        assert overview["rows"][0]["status"] == "inactive"
        await engine.dispose()

    asyncio.run(run())


def test_baseline_shrink_e2e_remove_only_takes_effect_after_publish(tmp_path, monkeypatch) -> None:
    async def run() -> None:
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'jobs.db'}")
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        sessionmaker = async_sessionmaker(engine, expire_on_commit=False)
        base_dir = tmp_path / "backend"
        _write_publishable_dimension(base_dir)
        publish_generated_crosswalk(base_dir, "vehicle_series", _crosswalk())
        references = base_dir / "semantic-assets" / "dimensions" / "vehicle_series" / "references"
        active_path = references / "active_crosswalk.json"
        active_before = json.loads(active_path.read_text(encoding="utf-8"))
        staging_path = tmp_path / "staging" / "full_crosswalk.json"
        staging_path.parent.mkdir(parents=True)
        staging_path.write_text(json.dumps(_crosswalk(include_han=False), ensure_ascii=False), encoding="utf-8")
        monkeypatch.setattr(analytics_api, "BASE_DIR", base_dir)

        async with sessionmaker() as session:
            job, _ = await create_semantic_dimension_build_job(session, dimension_id="vehicle_series", adapter="entity_crosswalk_v1")
            claimed = await claim_next_semantic_dimension_build_job(session)
            assert claimed is not None
            await mark_semantic_dimension_build_waiting_baseline_change(
                session,
                claimed,
                staging_path=str(staging_path.parent),
                published_reference_path="references/active_crosswalk.json",
                result_summary={
                    "artifact_paths": {"crosswalk": str(staging_path)},
                    "baseline_delta": {"removed": [{"entity_key": "byd::han", "label": "比亚迪 / 汉"}]},
                },
            )
            await analytics_api.resolve_semantic_dimension_baseline_change_request(
                job.id,
                SemanticDimensionBaselineChangeDecisionRequest(action="remove"),
                session=session,
            )
            assert json.loads(active_path.read_text(encoding="utf-8")) == active_before
            await publish_semantic_dimension_build(session, base_dir=base_dir, job_id=job.id)

        active_after = json.loads(active_path.read_text(encoding="utf-8"))
        assert {record["entity"]["entity_key"] for record in active_after["records"]} == {"byd::qinplus"}
        assert (references / "versions" / f"{active_after['version']}.json").is_file()
        await engine.dispose()

    asyncio.run(run())
