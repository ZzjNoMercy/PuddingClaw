"""Lifecycle worker for persisted NL2SQL result artifacts."""

from __future__ import annotations

import asyncio
import logging
import os

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from db import get_sessionmaker

from .result_store import (
    cleanup_expired_query_results,
    scavenge_orphaned_query_result_files,
)

logger = logging.getLogger(__name__)


class QueryResultCleanupManager:
    """Periodically reconcile result rows, artifacts, and sidecar catalogs."""

    def __init__(self) -> None:
        self._task: asyncio.Task[None] | None = None

    def start(self) -> None:
        if os.getenv(
            "PUDDINGCLAW_DISABLE_QUERY_RESULT_CLEANUP",
            "",
        ).strip().lower() in {"1", "true", "yes", "on"}:
            logger.info("[query-result-cleanup] disabled by environment")
            return
        if self._task is not None and not self._task.done():
            return
        self._task = asyncio.create_task(
            self._run_loop(),
            name="query-result-cleanup",
        )
        logger.info("[query-result-cleanup] started")

    async def stop(self) -> None:
        if self._task is None:
            return
        self._task.cancel()
        try:
            await self._task
        except asyncio.CancelledError:
            pass
        finally:
            self._task = None
            logger.info("[query-result-cleanup] stopped")

    async def _run_loop(self) -> None:
        interval = max(
            60.0,
            float(
                os.getenv(
                    "PUDDINGCLAW_QUERY_RESULT_CLEANUP_INTERVAL_SECONDS",
                    "900",
                )
                or "900"
            ),
        )
        sessionmaker = get_sessionmaker()
        while True:
            try:
                expired, orphaned = await self._run_once(sessionmaker)
                if expired or orphaned:
                    logger.info(
                        "[query-result-cleanup] expired=%d orphaned=%d",
                        expired,
                        orphaned,
                    )
                await asyncio.sleep(interval)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("[query-result-cleanup] loop error")
                await asyncio.sleep(interval)

    @staticmethod
    async def _run_once(
        sessionmaker: async_sessionmaker[AsyncSession],
    ) -> tuple[int, int]:
        async with sessionmaker() as session:
            expired = await cleanup_expired_query_results(session)
            orphaned = await scavenge_orphaned_query_result_files(session)
        return expired, orphaned


query_result_cleanup_manager = QueryResultCleanupManager()


__all__ = ["QueryResultCleanupManager", "query_result_cleanup_manager"]
