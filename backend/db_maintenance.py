"""Periodic maintenance for the Core catalog database.

Runs a passive WAL checkpoint on a schedule so the write-ahead log does not
grow unbounded, and emits structured logs for lock contention and checkpoint
outcomes. No-ops for non-SQLite providers (PostgreSQL checkpointing is the
database server's own concern).
"""

from __future__ import annotations

import asyncio
import logging
import os

from sqlalchemy import text

from db import get_engine, get_database_url, is_sqlite_url

logger = logging.getLogger(__name__)


def _checkpoint_interval_seconds() -> float:
    return max(10.0, float(os.getenv("PUDDINGCLAW_CATALOG_CHECKPOINT_INTERVAL_SECONDS", "300") or "300"))


class CatalogMaintenanceManager:
    def __init__(self) -> None:
        self._task: asyncio.Task | None = None

    def start(self) -> None:
        if os.getenv("PUDDINGCLAW_DISABLE_CATALOG_MAINTENANCE", "").strip().lower() in {"1", "true", "yes", "on"}:
            logger.info("[catalog-maintenance] disabled by PUDDINGCLAW_DISABLE_CATALOG_MAINTENANCE")
            return
        if not is_sqlite_url(get_database_url()):
            logger.info("[catalog-maintenance] skipped: Core catalog is not SQLite")
            return
        if self._task is not None and not self._task.done():
            return
        self._task = asyncio.create_task(self._run_loop(), name="catalog-maintenance")
        logger.info("[catalog-maintenance] started (interval=%ss)", _checkpoint_interval_seconds())

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
            logger.info("[catalog-maintenance] stopped")

    async def _run_loop(self) -> None:
        while True:
            try:
                await self.checkpoint_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("[catalog-maintenance] checkpoint error")
            await asyncio.sleep(_checkpoint_interval_seconds())

    async def checkpoint_once(self) -> None:
        engine = get_engine()
        async with engine.connect() as conn:
            row = (await conn.execute(text("PRAGMA wal_checkpoint(PASSIVE)"))).first()
            if row is None:
                return
            busy, log_frames, checkpointed_frames = int(row[0]), int(row[1]), int(row[2])
            if busy:
                # Writers held the WAL; the next tick retries. Surfaced as a
                # structured probe rather than a silent stall.
                logger.warning(
                    "[catalog-maintenance] wal_checkpoint deferred: busy=%s log_frames=%s checkpointed=%s",
                    busy,
                    log_frames,
                    checkpointed_frames,
                )
            else:
                logger.info(
                    "[catalog-maintenance] wal_checkpoint ok: log_frames=%s checkpointed=%s",
                    log_frames,
                    checkpointed_frames,
                )


catalog_maintenance_manager = CatalogMaintenanceManager()
