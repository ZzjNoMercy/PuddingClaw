"""Background worker for knowledge import jobs."""

from __future__ import annotations

import asyncio
import logging
import os
import sys
from pathlib import Path

from sqlalchemy.ext.asyncio import async_sessionmaker, AsyncSession

from db import get_sessionmaker
from knowledge.import_jobs import (
    LLM_WIKI_INGEST_KIND,
    READ_LATER_CAPTURE_KIND,
    VANNA_ENTITY_IMPORT_KIND,
    claim_next_job,
    job_kind,
    mark_job_failed,
    process_import_job,
)

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
            job_id = job.id
            kind = job_kind(job)
            logger.info("[knowledge-worker] claimed job_id=%s kind=%s file=%s", job.id, kind, job.file_name)

        if kind == VANNA_ENTITY_IMPORT_KIND:
            await self._run_vanna_entity_job_subprocess(sessionmaker, base_dir, job_id)
            return True

        async with sessionmaker() as session:
            job = await session.get(type(job), job_id)
            if job is None:
                return True
            try:
                if kind == LLM_WIKI_INGEST_KIND:
                    from knowledge.llm_wiki_job_runner import process_llm_wiki_ingest_job

                    await process_llm_wiki_ingest_job(session, base_dir=base_dir, job=job)
                elif kind == READ_LATER_CAPTURE_KIND:
                    from knowledge.read_later import process_read_later_capture_job

                    await process_read_later_capture_job(session, base_dir=base_dir, job=job)
                else:
                    await process_import_job(session, base_dir=base_dir, job=job)
                logger.info("[knowledge-worker] completed job_id=%s", job.id)
            except Exception as exc:
                logger.exception("[knowledge-worker] failed job_id=%s", job.id)
                if kind == READ_LATER_CAPTURE_KIND:
                    from knowledge.models import ReadLaterItem

                    item_id = str((job.job_metadata or {}).get("read_later_item_id") or "")
                    item = await session.get(ReadLaterItem, item_id) if item_id else None
                    if item is not None:
                        item.parse_status = "failed"
                        item.error_message = str(exc)
                await mark_job_failed(session, job, exc)
            return True

    async def _run_vanna_entity_job_subprocess(
        self,
        sessionmaker: async_sessionmaker[AsyncSession],
        base_dir: Path,
        job_id: str,
    ) -> None:
        backend_dir = Path(__file__).resolve().parents[1]
        log_dir = backend_dir / "logs" / "vanna-entity-jobs"
        log_dir.mkdir(parents=True, exist_ok=True)
        log_path = log_dir / f"{job_id}.log"

        logger.info("[knowledge-worker] starting vanna entity subprocess job_id=%s log=%s", job_id, log_path)
        with log_path.open("ab") as log_file:
            process = await asyncio.create_subprocess_exec(
                sys.executable,
                "-m",
                "knowledge.vanna_entity_job_runner",
                job_id,
                "--base-dir",
                str(base_dir),
                cwd=str(backend_dir),
                stdout=log_file,
                stderr=log_file,
            )
            return_code = await process.wait()

        if return_code == 0:
            logger.info("[knowledge-worker] vanna entity subprocess completed job_id=%s", job_id)
            return

        logger.error(
            "[knowledge-worker] vanna entity subprocess failed job_id=%s return_code=%s log=%s",
            job_id,
            return_code,
            log_path,
        )
        async with sessionmaker() as session:
            from knowledge.models import KnowledgeImportJob

            job = await session.get(KnowledgeImportJob, job_id)
            if job is not None and job.status in {"queued", "running"}:
                await mark_job_failed(session, job, f"实体导入子进程失败，详见日志：{log_path}")


knowledge_import_worker_manager = KnowledgeImportWorkerManager()
