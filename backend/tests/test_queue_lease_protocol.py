"""Tests for the cross-database queue lease protocol on a real SQLite file."""

from __future__ import annotations

import asyncio
import sqlite3

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import knowledge.import_jobs as import_jobs_module
from knowledge.import_jobs import (
    claim_next_job,
    create_import_job,
    mark_job_failed,
    process_import_job,
    retry_import_job,
    task_source_path,
)
from knowledge.models import Base, KnowledgeBase, KnowledgeImportJob, SemanticDimensionBuildJob
from knowledge.queue_repository import (
    LeaseLostError,
    bind_lease_owner,
    claim_next,
    heartbeat,
    heartbeat_loop,
    lease_valid,
    release_lease,
    require_current_lease,
    require_lease,
    reset_lease_owner,
)
from knowledge.semantic_dimension_jobs import (
    cancel_semantic_dimension_build_job,
    claim_next_semantic_dimension_build_job,
    create_semantic_dimension_build_job,
    list_semantic_dimension_build_events,
)


async def _make_sessionmaker(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'jobs.db'}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    return engine, async_sessionmaker(engine, expire_on_commit=False)


async def _create_import_job(session, *, kb_id: str = "kb-1") -> KnowledgeImportJob:
    if await session.get(KnowledgeBase, kb_id) is None:
        session.add(KnowledgeBase(id=kb_id, name="测试知识库"))
        await session.commit()
    job = KnowledgeImportJob(
        knowledge_base_id=kb_id,
        status="queued",
        file_name="demo.md",
        source_path="/tmp/demo.md",
    )
    session.add(job)
    await session.commit()
    await session.refresh(job)
    return job


def test_concurrent_claim_grants_job_to_exactly_one_worker(tmp_path) -> None:
    async def run() -> None:
        engine, sessionmaker = await _make_sessionmaker(tmp_path)
        async with sessionmaker() as session:
            job = await _create_import_job(session)

        async def claim_as(worker_id: str):
            async with sessionmaker() as session:
                claimed = await claim_next(session, KnowledgeImportJob, worker_id=worker_id)
                await session.commit()
                return claimed

        first, second = await asyncio.gather(claim_as("worker-1"), claim_as("worker-2"))
        winners = [claimed for claimed in (first, second) if claimed is not None]
        assert len(winners) == 1
        winner = winners[0]
        assert winner.id == job.id
        assert winner.status == "running"
        assert winner.lease_owner in {"worker-1", "worker-2"}
        assert winner.attempt == 1
        assert winner.started_at is not None
        assert winner.lease_expires_at is not None
        assert winner.heartbeat_at is not None
        await engine.dispose()

    asyncio.run(run())


def test_active_lease_cannot_be_stolen(tmp_path) -> None:
    async def run() -> None:
        engine, sessionmaker = await _make_sessionmaker(tmp_path)
        async with sessionmaker() as session:
            job = await _create_import_job(session)
        async with sessionmaker() as session:
            claimed = await claim_next(session, KnowledgeImportJob, worker_id="worker-1", lease_seconds=120)
            await session.commit()
            assert claimed is not None and claimed.id == job.id
        async with sessionmaker() as session:
            stolen = await claim_next(session, KnowledgeImportJob, worker_id="worker-2")
            assert stolen is None
        await engine.dispose()

    asyncio.run(run())


def test_expired_lease_is_reclaimed_and_attempt_increments(tmp_path) -> None:
    async def run() -> None:
        engine, sessionmaker = await _make_sessionmaker(tmp_path)
        async with sessionmaker() as session:
            job = await _create_import_job(session)
        async with sessionmaker() as session:
            claimed = await claim_next(session, KnowledgeImportJob, worker_id="worker-1", lease_seconds=30)
            await session.commit()
            assert claimed is not None and claimed.attempt == 1
        async with sessionmaker() as session:
            await session.execute(
                text("UPDATE knowledge_import_jobs SET lease_expires_at = datetime('now', '-1 seconds') WHERE id = :job_id"),
                {"job_id": job.id},
            )
            await session.commit()
        async with sessionmaker() as session:
            reclaimed = await claim_next(session, KnowledgeImportJob, worker_id="worker-2", lease_seconds=30)
            await session.commit()
            assert reclaimed is not None
            assert reclaimed.id == job.id
            assert reclaimed.lease_owner == "worker-2"
            assert reclaimed.attempt == 2
            assert reclaimed.started_at is not None
        await engine.dispose()

    asyncio.run(run())


def test_legacy_running_row_without_lease_is_recovered(tmp_path) -> None:
    async def run() -> None:
        engine, sessionmaker = await _make_sessionmaker(tmp_path)
        async with sessionmaker() as session:
            job = await _create_import_job(session)
        # Simulate a pre-lease crash leftover: running with all lease columns NULL.
        async with sessionmaker() as session:
            await session.execute(
                text(
                    "UPDATE knowledge_import_jobs SET status = 'running', lease_owner = NULL, "
                    "lease_expires_at = NULL, heartbeat_at = NULL, attempt = 0 WHERE id = :job_id"
                ),
                {"job_id": job.id},
            )
            await session.commit()
        async with sessionmaker() as session:
            claimed = await claim_next(session, KnowledgeImportJob, worker_id="worker-1", lease_seconds=30)
            await session.commit()
            assert claimed is not None
            assert claimed.id == job.id
            assert claimed.lease_owner == "worker-1"
            assert claimed.attempt == 1
        await engine.dispose()

    asyncio.run(run())


def test_heartbeat_renews_only_for_lease_owner_of_running_job(tmp_path) -> None:
    async def run() -> None:
        engine, sessionmaker = await _make_sessionmaker(tmp_path)
        async with sessionmaker() as session:
            job = await _create_import_job(session)
        # Claim from a fresh session: claim_next returns session.get() results,
        # which would be the stale identity-map instance in the creator session.
        async with sessionmaker() as session:
            claimed = await claim_next(session, KnowledgeImportJob, worker_id="worker-1", lease_seconds=30)
            await session.commit()
            assert claimed is not None
            initial_expiry = claimed.lease_expires_at
            assert initial_expiry is not None

        async with sessionmaker() as session:
            renewed = await heartbeat(session, KnowledgeImportJob, job.id, "worker-1", lease_seconds=3600)
            await session.commit()
            assert renewed is True
            refreshed = await session.get(KnowledgeImportJob, job.id)
            assert refreshed is not None
            assert refreshed.lease_expires_at is not None
            assert refreshed.lease_expires_at > initial_expiry

        async with sessionmaker() as session:
            wrong_owner = await heartbeat(session, KnowledgeImportJob, job.id, "worker-2", lease_seconds=3600)
            await session.commit()
            assert wrong_owner is False

        async with sessionmaker() as session:
            await session.execute(
                text("UPDATE knowledge_import_jobs SET status = 'failed' WHERE id = :job_id"),
                {"job_id": job.id},
            )
            await session.commit()
        async with sessionmaker() as session:
            not_running = await heartbeat(session, KnowledgeImportJob, job.id, "worker-1", lease_seconds=3600)
            await session.commit()
            assert not_running is False
        await engine.dispose()

    asyncio.run(run())


def test_expired_owner_cannot_heartbeat_or_fence_before_reclaim(tmp_path) -> None:
    """Lease expiry itself revokes the old worker; reclaim need not happen first."""

    async def run() -> None:
        engine, sessionmaker = await _make_sessionmaker(tmp_path)
        async with sessionmaker() as session:
            job = await _create_import_job(session)
            claimed = await claim_next(session, KnowledgeImportJob, worker_id="worker-1", lease_seconds=120)
            await session.commit()
            assert claimed is not None

        async with sessionmaker() as session:
            await session.execute(
                text(
                    "UPDATE knowledge_import_jobs "
                    "SET lease_expires_at = datetime('now', '-1 second') WHERE id = :job_id"
                ),
                {"job_id": job.id},
            )
            await session.commit()

        async with sessionmaker() as session:
            assert await heartbeat(session, KnowledgeImportJob, job.id, "worker-1") is False
            assert await lease_valid(session, KnowledgeImportJob, job.id, "worker-1") is False
            with pytest.raises(LeaseLostError):
                await require_lease(session, KnowledgeImportJob, job.id, "worker-1")
        await engine.dispose()

    asyncio.run(run())


def test_require_lease_raises_lease_lost_for_non_owner(tmp_path) -> None:
    async def run() -> None:
        engine, sessionmaker = await _make_sessionmaker(tmp_path)
        async with sessionmaker() as session:
            job = await _create_import_job(session)
            claimed = await claim_next(session, KnowledgeImportJob, worker_id="worker-1", lease_seconds=120)
            await session.commit()
            assert claimed is not None

        async with sessionmaker() as session:
            assert await lease_valid(session, KnowledgeImportJob, job.id, "worker-1") is True
            assert await lease_valid(session, KnowledgeImportJob, job.id, "worker-2") is False
            await require_lease(session, KnowledgeImportJob, job.id, "worker-1")
            with pytest.raises(LeaseLostError):
                await require_lease(session, KnowledgeImportJob, job.id, "worker-2")
        await engine.dispose()

    asyncio.run(run())


def test_fenced_terminal_write_blocks_reclaim_until_commit(tmp_path) -> None:
    """A claimant cannot slip between lease validation and the terminal commit."""

    async def run() -> None:
        engine, sessionmaker = await _make_sessionmaker(tmp_path)
        async with sessionmaker() as session:
            job = await _create_import_job(session)
        async with sessionmaker() as session:
            claimed = await claim_next(session, KnowledgeImportJob, worker_id="worker-1", lease_seconds=120)
            await session.commit()
            assert claimed is not None

        async def expire_and_reclaim():
            async with sessionmaker() as session:
                await session.execute(
                    text(
                        "UPDATE knowledge_import_jobs "
                        "SET lease_expires_at = datetime('now', '-1 second') WHERE id = :job_id"
                    ),
                    {"job_id": job.id},
                )
                await session.commit()
            async with sessionmaker() as session:
                reclaimed = await claim_next(
                    session, KnowledgeImportJob, worker_id="worker-2", lease_seconds=120
                )
                await session.commit()
                return reclaimed

        async with sessionmaker() as session:
            owned = await session.get(KnowledgeImportJob, job.id)
            assert owned is not None
            await require_lease(session, KnowledgeImportJob, job.id, "worker-1")
            contender = asyncio.create_task(expire_and_reclaim())
            await asyncio.sleep(0.1)
            assert not contender.done()

            owned.status = "succeeded"
            owned.current_step = "done"
            owned.progress = 100
            owned.lease_owner = None
            owned.lease_expires_at = None
            owned.heartbeat_at = None
            await session.commit()

        assert await contender is None
        async with sessionmaker() as session:
            stored = await session.get(KnowledgeImportJob, job.id)
            assert stored is not None
            assert stored.status == "succeeded"
            assert stored.lease_owner is None
        await engine.dispose()

    asyncio.run(run())


def test_release_lease_clears_fields_only_for_owner(tmp_path) -> None:
    async def run() -> None:
        engine, sessionmaker = await _make_sessionmaker(tmp_path)
        async with sessionmaker() as session:
            job = await _create_import_job(session)
            claimed = await claim_next(session, KnowledgeImportJob, worker_id="worker-1", lease_seconds=120)
            await session.commit()
            assert claimed is not None

        async with sessionmaker() as session:
            await release_lease(session, KnowledgeImportJob, job.id, "worker-2")
            await session.commit()
        async with sessionmaker() as session:
            stored = await session.get(KnowledgeImportJob, job.id)
            assert stored is not None
            assert stored.lease_owner == "worker-1"
            assert stored.lease_expires_at is not None
            assert stored.heartbeat_at is not None

        async with sessionmaker() as session:
            await release_lease(session, KnowledgeImportJob, job.id, "worker-1")
            await session.commit()
        async with sessionmaker() as session:
            stored = await session.get(KnowledgeImportJob, job.id)
            assert stored is not None
            assert stored.lease_owner is None
            assert stored.lease_expires_at is None
            assert stored.heartbeat_at is None
        await engine.dispose()

    asyncio.run(run())


def test_running_semantic_dimension_job_cannot_be_cancelled(tmp_path) -> None:
    async def run() -> None:
        engine, sessionmaker = await _make_sessionmaker(tmp_path)
        async with sessionmaker() as session:
            job, queued = await create_semantic_dimension_build_job(
                session, dimension_id="vehicle_series", adapter="entity_crosswalk_v1"
            )
            assert queued is True
            job_id = job.id
            claimed = await claim_next_semantic_dimension_build_job(session, worker_id="worker-1")
            assert claimed is not None and claimed.status == "running"
            with pytest.raises(ValueError, match="Only queued"):
                await cancel_semantic_dimension_build_job(session, job_id)
            await session.rollback()

            stored = await session.get(SemanticDimensionBuildJob, job_id)
            assert stored is not None and stored.status == "running"

            other, _ = await create_semantic_dimension_build_job(
                session, dimension_id="brand", adapter="entity_crosswalk_v1"
            )
            other_id = other.id
            cancelled = await cancel_semantic_dimension_build_job(session, other_id)
            assert cancelled.status == "cancelled"
            assert cancelled.lease_owner is None
            assert cancelled.lease_expires_at is None
            assert cancelled.heartbeat_at is None
            events = await list_semantic_dimension_build_events(session, other_id)
            assert any(event.message == "任务已取消" for event in events)
        await engine.dispose()

    asyncio.run(run())


def test_retry_import_job_clears_lease_and_requeues(tmp_path) -> None:
    async def run() -> None:
        engine, sessionmaker = await _make_sessionmaker(tmp_path)
        async with sessionmaker() as session:
            job = await _create_import_job(session)
            claimed = await claim_next_job(session, worker_id="worker-1", lease_seconds=120)
            assert claimed is not None and claimed.id == job.id
            assert claimed.lease_owner == "worker-1"
            assert claimed.current_step == "starting"

            with pytest.raises(LeaseLostError):
                await mark_job_failed(session, claimed, "boom", lease_owner="worker-2")

            await mark_job_failed(session, claimed, "boom", lease_owner="worker-1")
            assert claimed.status == "failed"
            assert claimed.lease_owner is None
            assert claimed.lease_expires_at is None
            assert claimed.heartbeat_at is None

            retried = await retry_import_job(session, job.id)
            assert retried.status == "queued"
            assert retried.current_step == "queued"
            assert retried.retry_count == 1
            assert retried.started_at is None
            assert retried.lease_owner is None
            assert retried.lease_expires_at is None
            assert retried.heartbeat_at is None

            reclaimed = await claim_next_job(session, worker_id="worker-2", lease_seconds=120)
            assert reclaimed is not None
            assert reclaimed.id == job.id
            assert reclaimed.lease_owner == "worker-2"
        await engine.dispose()

    asyncio.run(run())


def test_heartbeat_loop_renews_until_stopped(tmp_path) -> None:
    async def run() -> None:
        engine, sessionmaker = await _make_sessionmaker(tmp_path)
        async with sessionmaker() as session:
            job = await _create_import_job(session)
        # Claim from a fresh session so lease fields come from the database.
        async with sessionmaker() as session:
            claimed = await claim_next(session, KnowledgeImportJob, worker_id="worker-1", lease_seconds=5)
            await session.commit()
            assert claimed is not None
            initial_expiry = claimed.lease_expires_at
            assert initial_expiry is not None

        stop_event = asyncio.Event()
        lost_event = asyncio.Event()
        loop_task = asyncio.create_task(
            heartbeat_loop(
                sessionmaker,
                KnowledgeImportJob,
                job.id,
                "worker-1",
                lease_seconds=5,
                stop_event=stop_event,
                lost_event=lost_event,
            )
        )
        await asyncio.sleep(2.5)
        stop_event.set()
        await asyncio.wait_for(loop_task, timeout=10)

        assert lost_event.is_set() is False
        async with sessionmaker() as session:
            stored = await session.get(KnowledgeImportJob, job.id)
            assert stored is not None
            assert stored.lease_owner == "worker-1"
            assert stored.lease_expires_at is not None
            assert stored.lease_expires_at > initial_expiry
        await engine.dispose()

    asyncio.run(run())


def test_heartbeat_loop_sets_lost_event_when_lease_is_taken(tmp_path) -> None:
    async def run() -> None:
        engine, sessionmaker = await _make_sessionmaker(tmp_path)
        async with sessionmaker() as session:
            job = await _create_import_job(session)
            claimed = await claim_next(session, KnowledgeImportJob, worker_id="worker-1", lease_seconds=120)
            await session.commit()
            assert claimed is not None

        # Simulate another worker taking over the lease.
        async with sessionmaker() as session:
            await session.execute(
                text("UPDATE knowledge_import_jobs SET lease_owner = 'intruder' WHERE id = :job_id"),
                {"job_id": job.id},
            )
            await session.commit()

        stop_event = asyncio.Event()
        lost_event = asyncio.Event()
        await asyncio.wait_for(
            heartbeat_loop(
                sessionmaker,
                KnowledgeImportJob,
                job.id,
                "worker-1",
                lease_seconds=5,
                stop_event=stop_event,
                lost_event=lost_event,
            ),
            timeout=10,
        )
        assert lost_event.is_set() is True
        assert stop_event.is_set() is False
        await engine.dispose()

    asyncio.run(run())


def test_claim_next_rejects_unknown_extra_sets_columns(tmp_path) -> None:
    async def run() -> None:
        engine, sessionmaker = await _make_sessionmaker(tmp_path)
        async with sessionmaker() as session:
            with pytest.raises(ValueError, match="unknown columns"):
                await claim_next(
                    session,
                    KnowledgeImportJob,
                    worker_id="worker-1",
                    extra_sets={"no_such_column": 1},
                )
        await engine.dispose()

    asyncio.run(run())


async def _expire_and_reclaim(sessionmaker, job_id: str, *, new_owner: str) -> None:
    """Expire worker-1's lease via SQL and let ``new_owner`` reclaim the job."""

    async with sessionmaker() as session:
        await session.execute(
            text("UPDATE knowledge_import_jobs SET lease_expires_at = datetime('now', '-1 seconds') WHERE id = :job_id"),
            {"job_id": job_id},
        )
        await session.commit()
    async with sessionmaker() as session:
        reclaimed = await claim_next_job(session, worker_id=new_owner, lease_seconds=120)
        assert reclaimed is not None
        assert reclaimed.id == job_id
        assert reclaimed.lease_owner == new_owner


def test_terminal_write_guard_blocks_stale_worker(tmp_path) -> None:
    """w1 claims -> lease expires -> w2 reclaims -> w1's terminal guard refuses."""

    async def run() -> None:
        engine, sessionmaker = await _make_sessionmaker(tmp_path)
        async with sessionmaker() as session:
            job = await _create_import_job(session)
        async with sessionmaker() as session:
            claimed = await claim_next_job(session, worker_id="worker-1", lease_seconds=120)
            assert claimed is not None and claimed.id == job.id

        await _expire_and_reclaim(sessionmaker, job.id, new_owner="worker-2")

        async with sessionmaker() as session:
            token = bind_lease_owner("worker-1")
            try:
                with pytest.raises(LeaseLostError):
                    await require_current_lease(session, KnowledgeImportJob, job.id)
            finally:
                reset_lease_owner(token)
            # Without a bound owner (direct API/test call) the guard is a no-op.
            await require_current_lease(session, KnowledgeImportJob, job.id)
            # worker-2's running state is untouched.
            stored = await session.get(KnowledgeImportJob, job.id)
            assert stored is not None
            assert stored.status == "running"
            assert stored.lease_owner == "worker-2"
        await engine.dispose()

    asyncio.run(run())


def test_process_import_job_terminal_write_requires_live_lease(tmp_path, monkeypatch) -> None:
    """The succeeded write at the end of process_import_job is lease-guarded."""

    async def run() -> None:
        engine, sessionmaker = await _make_sessionmaker(tmp_path)
        backend_dir = tmp_path / "backend"
        job_id = "job-lease-guard"
        source_path = task_source_path(backend_dir, job_id=job_id, filename="guard.md")
        source_path.parent.mkdir(parents=True, exist_ok=True)
        content = b"# Guard\n\nterminal write lease guard\n"
        source_path.write_bytes(content)

        async with sessionmaker() as session:
            job = await create_import_job(
                session,
                base_dir=backend_dir,
                filename="guard.md",
                source_path=source_path,
                file_size=len(content),
                source_sha256="",
            )
            assert job.id == job_id
        async with sessionmaker() as session:
            claimed = await claim_next_job(session, worker_id="worker-1", lease_seconds=120)
            assert claimed is not None and claimed.id == job_id

        await _expire_and_reclaim(sessionmaker, job_id, new_owner="worker-2")

        # Skip the (already guarded) progress writes so the flow reaches the
        # terminal write itself.
        async def _noop_progress(*args, **kwargs) -> None:
            return None

        monkeypatch.setattr(import_jobs_module, "update_job_progress", _noop_progress)

        async with sessionmaker() as session:
            stale_job = await session.get(KnowledgeImportJob, job_id)
            assert stale_job is not None
            token = bind_lease_owner("worker-1")
            try:
                with pytest.raises(LeaseLostError):
                    await process_import_job(session, base_dir=backend_dir, job=stale_job)
            finally:
                reset_lease_owner(token)
            await session.rollback()

        async with sessionmaker() as session:
            stored = await session.get(KnowledgeImportJob, job_id)
            assert stored is not None
            assert stored.status == "running"
            assert stored.lease_owner == "worker-2"
        await engine.dispose()

    asyncio.run(run())


def test_claim_survives_stale_wal_read_snapshot(tmp_path) -> None:
    """A claim after a stale WAL read snapshot must not die with SQLITE_BUSY_SNAPSHOT.

    The writes_allowed probe opens a read transaction; under WAL a concurrent
    commit invalidates that snapshot, and upgrading it to the claim UPDATE
    fails immediately (busy_timeout does not apply). The claim path must
    close the read transaction first.
    """

    async def run() -> None:
        db_path = tmp_path / "jobs-wal.db"
        engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}")
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        await engine.dispose()
        sync_conn = sqlite3.connect(str(db_path))
        try:
            assert sync_conn.execute("PRAGMA journal_mode=WAL").fetchone()[0].lower() == "wal"
        finally:
            sync_conn.close()

        engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}")
        sessionmaker = async_sessionmaker(engine, expire_on_commit=False)
        async with sessionmaker() as session:
            job = await _create_import_job(session)

        # session1 opens a WAL read snapshot (what the writes_allowed probe does).
        session1 = sessionmaker()
        await session1.execute(text("SELECT COUNT(*) FROM knowledge_import_jobs"))
        # Another connection commits, invalidating session1's snapshot.
        async with sessionmaker() as session2:
            session2.add(KnowledgeBase(id="kb-2", name="并发知识库"))
            await session2.commit()

        claimed = await claim_next_job(session1, worker_id="worker-1", lease_seconds=30)
        assert claimed is not None
        assert claimed.id == job.id
        assert claimed.lease_owner == "worker-1"
        assert claimed.status == "running"
        await session1.close()
        await engine.dispose()

    asyncio.run(run())
