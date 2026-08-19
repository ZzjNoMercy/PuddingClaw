"""Tests for the database-level drain/maintenance runtime control protocol.

Runs against a real SQLite file database migrated to the latest schema, the same way
``db.init_database()`` brings up the Core catalog.
"""

from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import inspect, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import runtime_control
import schema_migrations
from knowledge.import_jobs import claim_next_job
from knowledge.models import Base, KnowledgeBase, KnowledgeImportJob, SemanticDimensionBuildJob
from knowledge.semantic_dimension_jobs import (
    claim_next_semantic_dimension_build_job,
    create_semantic_dimension_build_job,
)
from runtime_control import MaintenanceConflictError, MaintenanceModeError

JOB_TABLES = ("knowledge_import_jobs", "semantic_dimension_build_jobs")


async def _make_sessionmaker(tmp_path, *, migrate: bool = True):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'catalog.db'}")
    async with engine.begin() as connection:
        if migrate:
            await connection.run_sync(schema_migrations.migrate_to_latest)
        else:
            # Pre-v3 legacy database shape: ORM tables, no migration DDL.
            await connection.run_sync(Base.metadata.create_all)
    return engine, async_sessionmaker(engine, expire_on_commit=False)


async def _seed_queued_jobs(session) -> None:
    session.add(KnowledgeBase(id="kb-1", name="测试知识库"))
    session.add(
        KnowledgeImportJob(
            id="job-import-1",
            knowledge_base_id="kb-1",
            status="queued",
            file_name="demo.md",
            source_path="/tmp/demo.md",
        )
    )
    session.add(
        SemanticDimensionBuildJob(
            id="job-sdb-1",
            dimension_id="dim-1",
            adapter="mock",
            status="queued",
        )
    )
    await session.commit()


async def _seed_running_sdb_job(session, *, lease_sql: str, job_id: str = "job-running-1") -> None:
    await session.execute(
        text(
            "INSERT INTO semantic_dimension_build_jobs ("
            "id, session_id, query_id, dimension_id, adapter, requested_scope, input_snapshot, "
            "status, current_step, progress, staging_path, published_reference_path, result_summary, "
            "retry_count, created_at, updated_at, lease_owner, lease_expires_at, attempt"
            ") VALUES ("
            f":job_id, '', '', 'dim-run', 'mock', '{{}}', '{{}}', "
            f"'running', 'load_source_profiles', 40, '', '', '{{}}', "
            f"0, datetime('now'), datetime('now'), 'worker-x', {lease_sql}, 1)"
        ),
        {"job_id": job_id},
    )
    await session.commit()


async def _expire_runtime_control_lease(session) -> None:
    await session.execute(text("UPDATE core_runtime_control SET lease_expires_at = '2000-01-01 00:00:00' WHERE id = 1"))
    await session.commit()


async def _seed_running_import_job(session, *, lease_sql: str, job_id: str = "job-import-running") -> None:
    await session.execute(
        text(
            "INSERT INTO knowledge_import_jobs ("
            "id, knowledge_base_id, status, file_name, file_type, file_size, source_path, "
            "source_sha256, publish_targets, current_step, progress, retry_count, job_metadata, "
            "created_at, updated_at, lease_owner, lease_expires_at, attempt"
            ") VALUES ("
            f":job_id, 'kb-1', 'running', 'demo.md', 'markdown', 0, '/tmp/demo.md', "
            f"'', '[]', 'parsing', 40, 0, '{{}}', "
            f"datetime('now'), datetime('now'), 'worker-x', {lease_sql}, 1)"
        ),
        {"job_id": job_id},
    )
    await session.commit()


def test_get_state_lazily_creates_default_row(tmp_path) -> None:
    async def run() -> None:
        engine, sessionmaker = await _make_sessionmaker(tmp_path)
        async with sessionmaker() as session:
            state = await runtime_control.get_state(session)
            assert state["write_mode"] == "normal"
            assert state["generation"] == 0
            assert state["maintenance_owner"] is None
            assert state["lease_expires_at"] is None
            assert state["reason"] == ""

            # Idempotent: the second call must not insert again or change state.
            again = await runtime_control.get_state(session)
            assert again == state
            count = await session.scalar(text("SELECT COUNT(*) FROM core_runtime_control"))
            assert count == 1
        await engine.dispose()

    asyncio.run(run())


def test_acquire_maintenance_cas_and_conflict(tmp_path) -> None:
    async def run() -> None:
        engine, sessionmaker = await _make_sessionmaker(tmp_path)
        async with sessionmaker() as session:
            state = await runtime_control.acquire_maintenance(session, owner="owner-a", reason="迁移到 PG")
            assert state["write_mode"] == "draining"
            assert state["generation"] == 1
            assert state["maintenance_owner"] == "owner-a"
            assert state["lease_expires_at"] is not None
            assert state["reason"] == "迁移到 PG"

            with pytest.raises(MaintenanceConflictError) as exc_info:
                await runtime_control.acquire_maintenance(session, owner="owner-b")
            assert exc_info.value.state["maintenance_owner"] == "owner-a"

            # Same owner may re-acquire (续租), generation increments again.
            state = await runtime_control.acquire_maintenance(session, owner="owner-a")
            assert state["write_mode"] == "draining"
            assert state["generation"] == 2
        await engine.dispose()

    asyncio.run(run())


def test_expired_lease_can_be_taken_over(tmp_path) -> None:
    async def run() -> None:
        engine, sessionmaker = await _make_sessionmaker(tmp_path)
        async with sessionmaker() as session:
            await runtime_control.acquire_maintenance(session, owner="owner-a")
            await _expire_runtime_control_lease(session)

            state = await runtime_control.acquire_maintenance(session, owner="owner-b")
            assert state["maintenance_owner"] == "owner-b"
            assert state["write_mode"] == "draining"
            assert state["generation"] == 2
        await engine.dispose()

    asyncio.run(run())


def test_renew_maintenance_requires_matching_owner(tmp_path) -> None:
    async def run() -> None:
        engine, sessionmaker = await _make_sessionmaker(tmp_path)
        async with sessionmaker() as session:
            await runtime_control.acquire_maintenance(session, owner="owner-a")

            state = await runtime_control.renew_maintenance(session, owner="owner-a", lease_seconds=600)
            assert state["write_mode"] == "draining"
            assert state["generation"] == 1  # renew never bumps generation

            with pytest.raises(MaintenanceConflictError):
                await runtime_control.renew_maintenance(session, owner="owner-b")
        await engine.dispose()

    asyncio.run(run())


def test_enter_maintenance_blocked_by_running_jobs_then_succeeds(tmp_path) -> None:
    async def run() -> None:
        engine, sessionmaker = await _make_sessionmaker(tmp_path)
        async with sessionmaker() as session:
            await runtime_control.acquire_maintenance(session, owner="owner-a")
            await _seed_running_sdb_job(session, lease_sql="datetime('now', '+1 hour')")

            with pytest.raises(MaintenanceConflictError, match="1 个运行中任务"):
                await runtime_control.enter_maintenance(session, owner="owner-a")
            state = await runtime_control.get_state(session)
            assert state["write_mode"] == "draining"

            # A running job whose lease already expired is safe to take over.
            await session.execute(
                text("UPDATE semantic_dimension_build_jobs SET lease_expires_at = '2000-01-01 00:00:00'")
            )
            await session.commit()

            state = await runtime_control.enter_maintenance(session, owner="owner-a")
            assert state["write_mode"] == "maintenance"
            assert state["generation"] == 2
            assert state["maintenance_owner"] == "owner-a"

            # Entering requires draining state and the matching owner.
            with pytest.raises(MaintenanceConflictError):
                await runtime_control.enter_maintenance(session, owner="owner-a")
        await engine.dispose()

    asyncio.run(run())


def test_enter_maintenance_blocked_by_active_import_job_lease(tmp_path) -> None:
    """The quiet-queue precondition covers knowledge_import_jobs atomically."""

    async def run() -> None:
        engine, sessionmaker = await _make_sessionmaker(tmp_path)
        async with sessionmaker() as session:
            session.add(KnowledgeBase(id="kb-1", name="测试知识库"))
            await session.commit()
            await runtime_control.acquire_maintenance(session, owner="owner-a")
            await _seed_running_import_job(session, lease_sql="datetime('now', '+1 hour')")

            with pytest.raises(MaintenanceConflictError, match="1 个运行中任务"):
                await runtime_control.enter_maintenance(session, owner="owner-a")
            state = await runtime_control.get_state(session)
            assert state["write_mode"] == "draining"

            # Expired lease: the single-statement CAS must now succeed.
            await session.execute(
                text("UPDATE knowledge_import_jobs SET lease_expires_at = '2000-01-01 00:00:00'")
            )
            await session.commit()
            state = await runtime_control.enter_maintenance(session, owner="owner-a")
            assert state["write_mode"] == "maintenance"
            assert state["maintenance_owner"] == "owner-a"
        await engine.dispose()

    asyncio.run(run())


def test_release_maintenance_restores_normal(tmp_path) -> None:
    async def run() -> None:
        engine, sessionmaker = await _make_sessionmaker(tmp_path)
        async with sessionmaker() as session:
            await runtime_control.acquire_maintenance(session, owner="owner-a")

            with pytest.raises(MaintenanceConflictError):
                await runtime_control.release_maintenance(session, owner="owner-b")

            state = await runtime_control.release_maintenance(session, owner="owner-a", reason="done")
            assert state["write_mode"] == "normal"
            assert state["generation"] == 2
            assert state["maintenance_owner"] is None
            assert state["lease_expires_at"] is None
            assert state["reason"] == "done"
        await engine.dispose()

    asyncio.run(run())


def test_claim_gating_stops_workers_during_drain(tmp_path) -> None:
    async def run() -> None:
        engine, sessionmaker = await _make_sessionmaker(tmp_path)
        async with sessionmaker() as session:
            await _seed_queued_jobs(session)
            await runtime_control.acquire_maintenance(session, owner="owner-a")

            assert await claim_next_job(session, worker_id="worker-1") is None
            assert await claim_next_semantic_dimension_build_job(session, worker_id="worker-1") is None

            await runtime_control.release_maintenance(session, owner="owner-a")

            claimed_import = await claim_next_job(session, worker_id="worker-1")
            assert claimed_import is not None and claimed_import.id == "job-import-1"
            claimed_sdb = await claim_next_semantic_dimension_build_job(session, worker_id="worker-1")
            assert claimed_sdb is not None and claimed_sdb.id == "job-sdb-1"
        await engine.dispose()

    asyncio.run(run())


def test_assert_writes_allowed_blocks_enqueue_during_drain(tmp_path) -> None:
    async def run() -> None:
        engine, sessionmaker = await _make_sessionmaker(tmp_path)
        async with sessionmaker() as session:
            await runtime_control.assert_writes_allowed(session)  # normal: no-op

            await runtime_control.acquire_maintenance(session, owner="owner-a")
            with pytest.raises(MaintenanceModeError) as exc_info:
                await runtime_control.assert_writes_allowed(session)
            assert exc_info.value.write_mode == "draining"
            assert exc_info.value.retry_after > 0

            with pytest.raises(MaintenanceModeError):
                await create_semantic_dimension_build_job(session, dimension_id="dim-1", adapter="mock")
            count = await session.scalar(text("SELECT COUNT(*) FROM semantic_dimension_build_jobs"))
            assert count == 0

            await runtime_control.release_maintenance(session, owner="owner-a")
            job, created = await create_semantic_dimension_build_job(session, dimension_id="dim-1", adapter="mock")
            assert created and job.status == "queued"
        await engine.dispose()

    asyncio.run(run())


def test_pre_v3_database_is_treated_as_normal(tmp_path) -> None:
    """Legacy catalogs without core_runtime_control fail open."""

    async def run() -> None:
        engine, sessionmaker = await _make_sessionmaker(tmp_path, migrate=False)
        async with sessionmaker() as session:
            assert await runtime_control.writes_allowed(session) is True
            await runtime_control.assert_writes_allowed(session)
            await _seed_queued_jobs(session)
            claimed = await claim_next_job(session, worker_id="worker-1")
            assert claimed is not None and claimed.id == "job-import-1"
        await engine.dispose()

    asyncio.run(run())


def test_migration_latest_on_legacy_database_with_data(tmp_path) -> None:
    """A legacy (pre-version-table) catalog with data upgrades to the latest schema intact."""

    async def run() -> None:
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'catalog.db'}")
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
            for table in JOB_TABLES:
                for column in ("lease_owner", "lease_expires_at", "heartbeat_at", "attempt"):
                    await connection.exec_driver_sql(f"ALTER TABLE {table} DROP COLUMN {column}")
            await connection.exec_driver_sql(
                "INSERT INTO knowledge_bases (id, name, description, created_at, updated_at) "
                "VALUES ('kb-1', 'legacy', '', '2026-01-01 00:00:00', '2026-01-01 00:00:00')"
            )

        sessionmaker = async_sessionmaker(engine, expire_on_commit=False)
        async with sessionmaker() as session:
            # Pre-migration the runtime control protocol fails open.
            assert await runtime_control.writes_allowed(session) is True

        async with engine.begin() as connection:
            applied = await connection.run_sync(schema_migrations.migrate_to_latest)
        assert applied == [1, 2, 3, 4]

        async with engine.connect() as connection:
            tables = await connection.run_sync(lambda conn: set(inspect(conn).get_table_names()))
            assert "core_runtime_control" in tables
            version = await connection.run_sync(schema_migrations.current_schema_version)
            assert version == 4
            name = await connection.scalar(text("SELECT name FROM knowledge_bases WHERE id = 'kb-1'"))
            assert name == "legacy"

        # Idempotent re-run, and the protocol now persists state.
        async with engine.begin() as connection:
            assert await connection.run_sync(schema_migrations.migrate_to_latest) == []
        async with sessionmaker() as session:
            state = await runtime_control.get_state(session)
            assert state["write_mode"] == "normal"
            assert state["generation"] == 0
        await engine.dispose()

    asyncio.run(run())


def test_get_state_read_only_when_row_missing(tmp_path) -> None:
    """create_if_missing=False：读不到行时不落库，返回 not_initialized 默认视图。"""

    async def run() -> None:
        engine, sessionmaker = await _make_sessionmaker(tmp_path)
        async with sessionmaker() as session:
            state = await runtime_control.get_state(session, create_if_missing=False)
            assert state["write_mode"] == "normal"
            assert state["generation"] == 0
            assert state["maintenance_owner"] is None
            assert state["note"] == "not_initialized"
            count = await session.scalar(text("SELECT COUNT(*) FROM core_runtime_control"))
            assert count == 0

            # 已有行时按原样读出，不附加 note
            await runtime_control.acquire_maintenance(session, owner="owner-a")
            state = await runtime_control.get_state(session, create_if_missing=False)
            assert state["write_mode"] == "draining"
            assert "note" not in state
        await engine.dispose()

    asyncio.run(run())
