"""Tests for the knowledge import worker's lease-lost subprocess handling."""

from __future__ import annotations

import asyncio

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import knowledge.import_worker as import_worker
from knowledge.import_worker import KnowledgeImportWorkerManager
from knowledge.models import Base, KnowledgeBase, KnowledgeImportJob


class _FakeProcess:
    """Subprocess double that runs until terminated or killed."""

    def __init__(self) -> None:
        self.terminated = False
        self.killed = False
        self._exited = asyncio.Event()

    def terminate(self) -> None:
        self.terminated = True
        self._exited.set()

    def kill(self) -> None:
        self.killed = True
        self._exited.set()

    async def wait(self) -> int:
        await self._exited.wait()
        return -9 if self.killed else -15


def test_vanna_subprocess_is_terminated_when_lease_is_lost(tmp_path, monkeypatch) -> None:
    """On lease loss the worker must kill the subprocess, not wait for it."""

    async def run() -> None:
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'jobs.db'}")
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        sessionmaker = async_sessionmaker(engine, expire_on_commit=False)
        async with sessionmaker() as session:
            session.add(KnowledgeBase(id="kb-1", name="测试知识库"))
            session.add(
                KnowledgeImportJob(
                    id="job-vanna-1",
                    knowledge_base_id="kb-1",
                    status="running",
                    file_name="entities",
                    source_path="database://src/t.c",
                    lease_owner="worker-1",
                )
            )
            await session.commit()

        process = _FakeProcess()

        async def fake_subprocess_exec(*args, **kwargs):
            return process

        async def fake_heartbeat(*args, **kwargs) -> bool:
            return False  # lease already reclaimed by another worker

        monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_subprocess_exec)
        monkeypatch.setattr(import_worker, "heartbeat", fake_heartbeat)

        manager = KnowledgeImportWorkerManager()
        await manager._run_vanna_entity_job_subprocess(sessionmaker, tmp_path, "job-vanna-1", worker_id="worker-1")

        assert process.terminated or process.killed
        # The lost worker must not write any terminal state.
        async with sessionmaker() as session:
            stored = await session.get(KnowledgeImportJob, "job-vanna-1")
            assert stored is not None
            assert stored.status == "running"
            assert stored.lease_owner == "worker-1"
        await engine.dispose()

    asyncio.run(run())
