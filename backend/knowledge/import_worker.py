"""Background worker for knowledge import jobs."""

from __future__ import annotations

import asyncio
import logging
import os
import sys
from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from db import get_sessionmaker
from knowledge.import_jobs import (
    LLM_WIKI_INGEST_KIND,
    READ_LATER_CAPTURE_KIND,
    VANNA_ENTITY_IMPORT_KIND,
    claim_next_job,
    cleanup_succeeded_task_sources,
    job_kind,
    mark_job_failed,
    process_import_job,
)
from knowledge.models import KnowledgeImportJob
from knowledge.queue_repository import (
    LeaseLostError,
    bind_lease_owner,
    heartbeat,
    heartbeat_loop,
    new_worker_id,
    reset_lease_owner,
)

logger = logging.getLogger(__name__)


class KnowledgeImportWorkerManager:
    def __init__(self) -> None:
        self._task: asyncio.Task | None = None
        self._stop_event: asyncio.Event | None = None
        self._base_dir: Path | None = None
        self._worker_id: str | None = None
        self._prefer_feishu = False

    def start(self, base_dir: Path) -> None:
        if os.getenv("PUDDINGCLAW_DISABLE_KNOWLEDGE_WORKER", "").strip().lower() in {"1", "true", "yes", "on"}:
            logger.info("[knowledge-worker] disabled by PUDDINGCLAW_DISABLE_KNOWLEDGE_WORKER")
            return
        if self._task is not None and not self._task.done():
            return
        self._base_dir = base_dir
        self._worker_id = new_worker_id("knowledge-import")
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
        try:
            async with sessionmaker() as session:
                cleaned = await cleanup_succeeded_task_sources(session, base_dir=self._base_dir)
            if cleaned:
                logger.info("[knowledge-worker] recovered %s completed task source cleanups", cleaned)
        except Exception:
            # Cleanup is storage hygiene.  It must never prevent the import
            # worker from serving queued or retryable tasks.
            logger.warning("[knowledge-worker] completed task source cleanup sweep failed", exc_info=True)
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
        # Alternate queue preference so a continuous upload stream cannot
        # indefinitely starve scheduled connector runs.
        if self._prefer_feishu:
            self._prefer_feishu = False
            if await self._run_feishu_sync_once(sessionmaker, base_dir):
                return True
        async with sessionmaker() as session:
            job = await claim_next_job(session, worker_id=self._worker_id)
            if job is not None:
                self._prefer_feishu = True
                job_id = job.id
                kind = job_kind(job)
                worker_id = job.lease_owner or self._worker_id or ""
                logger.info("[knowledge-worker] claimed job_id=%s kind=%s file=%s", job.id, kind, job.file_name)
        if job is None:
            # Run the connector queue only after the claim session is closed;
            # nesting it inside the ``async with`` above would keep the import
            # claim transaction (and its SQLite write lock) open for the whole
            # sync run.
            return await self._run_feishu_sync_once(sessionmaker, base_dir)

        if kind == VANNA_ENTITY_IMPORT_KIND:
            await self._run_vanna_entity_job_subprocess(sessionmaker, base_dir, job_id, worker_id=worker_id)
            return True

        stop_hb = asyncio.Event()
        lost = asyncio.Event()
        hb_task = asyncio.create_task(
            heartbeat_loop(
                sessionmaker,
                KnowledgeImportJob,
                job_id,
                worker_id,
                stop_event=stop_hb,
                lost_event=lost,
            ),
            name=f"knowledge-import-heartbeat-{job_id}",
        )
        token = bind_lease_owner(worker_id)
        terminal_written = False
        lease_intercepted = False
        try:
            async with sessionmaker() as session:
                job = await session.get(KnowledgeImportJob, job_id)
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
                    terminal_written = True
                    logger.info("[knowledge-worker] completed job_id=%s", job.id)
                except LeaseLostError:
                    # The lease guards blocked the terminal write; the
                    # reclaiming worker owns the job's final state now.
                    lease_intercepted = True
                except Exception as exc:
                    logger.exception("[knowledge-worker] failed job_id=%s", job.id)
                    if kind == READ_LATER_CAPTURE_KIND:
                        from knowledge.models import ReadLaterItem

                        item_id = str((job.job_metadata or {}).get("read_later_item_id") or "")
                        item = await session.get(ReadLaterItem, item_id) if item_id else None
                        if item is not None:
                            item.parse_status = "failed"
                            item.error_message = str(exc)
                    try:
                        await mark_job_failed(session, job, exc)
                        terminal_written = True
                    except LeaseLostError:
                        lease_intercepted = True
                if (lost.is_set() or lease_intercepted) and not terminal_written:
                    logger.warning(
                        "[knowledge-worker] 租约已丢失，终态写入被租约守卫拦截，任务将由回收方重新执行 job_id=%s",
                        job_id,
                    )
            return True
        finally:
            reset_lease_owner(token)
            stop_hb.set()
            hb_task.cancel()
            try:
                await hb_task
            except asyncio.CancelledError:
                pass

    async def _run_feishu_sync_once(
        self,
        sessionmaker: async_sessionmaker[AsyncSession],
        base_dir: Path,
    ) -> bool:
        from knowledge.connectors.feishu_sync import (
            claim_next_feishu_sync_run,
            mark_feishu_sync_failed,
            process_feishu_sync_run,
        )
        from knowledge.models import KnowledgeSyncRun

        async with sessionmaker() as session:
            from knowledge.sources import enqueue_due_feishu_sync_runs

            await enqueue_due_feishu_sync_runs(session)
            await session.commit()
            run = await claim_next_feishu_sync_run(session, worker_id=self._worker_id)
            if run is None:
                return False
            run_id = run.id
            worker_id = run.lease_owner or self._worker_id or ""
            logger.info("[knowledge-worker] claimed Feishu sync run_id=%s source=%s", run.id, run.source_connection_id)

        stop_hb = asyncio.Event()
        lost = asyncio.Event()
        hb_task = asyncio.create_task(
            heartbeat_loop(
                sessionmaker,
                KnowledgeSyncRun,
                run_id,
                worker_id,
                stop_event=stop_hb,
                lost_event=lost,
            ),
            name=f"feishu-sync-heartbeat-{run_id}",
        )
        token = bind_lease_owner(worker_id)
        try:
            async with sessionmaker() as session:
                run = await session.get(KnowledgeSyncRun, run_id)
                if run is None:
                    return True
                try:
                    await process_feishu_sync_run(session, base_dir=base_dir, run=run)
                    logger.info("[knowledge-worker] completed Feishu sync run_id=%s", run_id)
                except LeaseLostError:
                    logger.warning("[knowledge-worker] Feishu sync lease lost run_id=%s", run_id)
                except Exception as exc:
                    logger.exception("[knowledge-worker] failed Feishu sync run_id=%s", run_id)
                    try:
                        await mark_feishu_sync_failed(session, run=run, error=exc)
                    except LeaseLostError:
                        logger.warning("[knowledge-worker] Feishu sync failure write fenced run_id=%s", run_id)
            return True
        finally:
            reset_lease_owner(token)
            stop_hb.set()
            hb_task.cancel()
            try:
                await hb_task
            except asyncio.CancelledError:
                pass

    async def _run_vanna_entity_job_subprocess(
        self,
        sessionmaker: async_sessionmaker[AsyncSession],
        base_dir: Path,
        job_id: str,
        *,
        worker_id: str,
    ) -> None:
        package_dir = Path(__file__).resolve().parents[1]
        if base_dir.expanduser().resolve().name == "backend":
            from runtime_identity.paths import PuddingClawPaths

            base_dir = PuddingClawPaths.from_environment().root
        log_dir = base_dir / "logs" / "vanna-entity-jobs"
        log_dir.mkdir(parents=True, exist_ok=True)
        log_path = log_dir / f"{job_id}.log"

        logger.info("[knowledge-worker] starting vanna entity subprocess job_id=%s log=%s", job_id, log_path)
        lost = False
        with log_path.open("ab") as log_file:
            process = await asyncio.create_subprocess_exec(
                sys.executable,
                "-m",
                "knowledge.vanna_entity_job_runner",
                job_id,
                "--base-dir",
                str(base_dir),
                cwd=str(package_dir),
                stdout=log_file,
                stderr=log_file,
                env={**os.environ, "PUDDINGCLAW_JOB_LEASE_OWNER": worker_id},
            )
            wait_task = asyncio.create_task(process.wait())
            lease_seconds = max(5, int(os.getenv("PUDDINGCLAW_QUEUE_LEASE_SECONDS", "120") or "120"))
            interval = max(1.0, lease_seconds / 3)
            while not wait_task.done():
                try:
                    async with sessionmaker() as session:
                        renewed = await heartbeat(session, KnowledgeImportJob, job_id, worker_id)
                        await session.commit()
                except Exception:
                    logger.exception("[knowledge-worker] vanna entity heartbeat error job_id=%s", job_id)
                    renewed = True
                if not renewed:
                    lost = True
                    logger.error("[knowledge-worker] 租约已丢失 job_id=%s worker=%s，终止子进程", job_id, worker_id)
                    # The reclaiming worker will start its own subprocess for
                    # this job; letting this one run to completion would
                    # double-write the external side effects (Milvus entities).
                    try:
                        process.terminate()
                    except ProcessLookupError:
                        pass
                    try:
                        await asyncio.wait_for(asyncio.shield(wait_task), timeout=5)
                    except asyncio.TimeoutError:
                        try:
                            process.kill()
                        except ProcessLookupError:
                            pass
                    break
                await asyncio.wait({wait_task}, timeout=interval)
            return_code = await wait_task

        if lost:
            logger.warning(
                "[knowledge-worker] 租约已丢失，子进程已终止（return_code=%s），状态由回收方接管 job_id=%s",
                return_code,
                job_id,
            )
            return
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
            job = await session.get(KnowledgeImportJob, job_id)
            if job is not None and job.status in {"queued", "running"}:
                try:
                    await mark_job_failed(session, job, f"实体导入子进程失败，详见日志：{log_path}", lease_owner=worker_id)
                except LeaseLostError:
                    logger.warning("[knowledge-worker] 租约已丢失，失败状态写入被租约守卫拦截 job_id=%s", job_id)


knowledge_import_worker_manager = KnowledgeImportWorkerManager()
