"""Background worker for knowledge import jobs."""

from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path

from sqlalchemy.ext.asyncio import async_sessionmaker, AsyncSession

from db import get_sessionmaker
from knowledge.import_jobs import claim_next_job, mark_job_failed, process_import_job

logger = logging.getLogger(__name__)


class KnowledgeImportWorkerManager:
    def __init__(self) -> None:
        self._task: asyncio.Task | None = None
        self._stop_event: asyncio.Event | None = None
        self._base_dir: Path | None = None

    def start(self, base_dir: Path) -> None:
        if os.getenv("PUDDINGCLAW_DISABLE_KNOWLEDGE_WORKER", "").strip().lower() in {"1", "true", "yes", "on"}:
            logger.info("[knowledge-worker] disabled by PUDDINGCLAW_DISABLE_KNOWLEDGE_WORKER")
            return
        if self._task is not None and not self._task.done():
            return
        self._base_dir = base_dir
        self._stop_event = asyncio.Event()
        self._task = asyncio.create_task(self._run_loop(), name="knowledge-import-worker")
        logger.info("[knowledge-worker] started")

    async def stop(self) -> None:
        if self._task is None:
            return
        if self._stop_event is not None:
            self._stop_event.set()
        self._task.cancel()
        try:
            await self._task
        except asyncio.CancelledError:
            pass
        finally:
            self._task = None
            logger.info("[knowledge-worker] stopped")

    async def _run_loop(self) -> None:
        assert self._base_dir is not None
        sessionmaker = get_sessionmaker()
        idle_sleep = float(os.getenv("PUDDINGCLAW_KNOWLEDGE_WORKER_POLL_SECONDS", "2") or "2")
        while True:
            try:
                did_work = await self._run_once(sessionmaker, self._base_dir)
                if not did_work:
                    await asyncio.sleep(idle_sleep)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("[knowledge-worker] loop error")
                await asyncio.sleep(idle_sleep)

    async def _run_once(self, sessionmaker: async_sessionmaker[AsyncSession], base_dir: Path) -> bool:
        async with sessionmaker() as session:
            job = await claim_next_job(session)
            if job is None:
                return False
            logger.info("[knowledge-worker] claimed job_id=%s file=%s", job.id, job.file_name)

        async with sessionmaker() as session:
            job = await session.get(type(job), job.id)
            if job is None:
                return True
            try:
                await process_import_job(session, base_dir=base_dir, job=job)
                logger.info("[knowledge-worker] completed job_id=%s", job.id)
            except Exception as exc:
                logger.exception("[knowledge-worker] failed job_id=%s", job.id)
                await mark_job_failed(session, job, exc)
            return True


knowledge_import_worker_manager = KnowledgeImportWorkerManager()

