"""Database-backed activity log for local Headless CLI Runs."""

from __future__ import annotations

import time
import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from db import get_sessionmaker
from knowledge.models import WorkerAccessLog


class HeadlessActivityLogStore:
    """Persist caller attribution for local CLI Runs without treating it as auth."""

    def __init__(self, sessionmaker: async_sessionmaker[AsyncSession] | None = None) -> None:
        self._sessionmaker = sessionmaker

    def _sessions(self) -> async_sessionmaker[AsyncSession]:
        return self._sessionmaker or get_sessionmaker()

    @staticmethod
    def _timestamp(value: datetime) -> float:
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.timestamp()

    async def record(
        self,
        *,
        source_id: str,
        source_name: str,
        query: str,
        created_at: float | None = None,
    ) -> dict[str, Any]:
        # The existing columns retain their database names to avoid a data-only
        # migration; at the API boundary they are always projected as source_*.
        record = WorkerAccessLog(
            id="wal_" + uuid.uuid4().hex,
            created_at=datetime.fromtimestamp(
                float(created_at if created_at is not None else time.time()),
                tz=timezone.utc,
            ),
            key_id=str(source_id or "local-cli"),
            key_name=str(source_name or source_id or "PuddingClaw CLI")[:120],
            query=str(query or ""),
        )
        async with self._sessions()() as session:
            session.add(record)
            await session.commit()
        return {
            "id": record.id,
            "created_at": self._timestamp(record.created_at),
            "source_id": record.key_id,
            "source_name": record.key_name,
            "query": record.query,
        }

    async def list(
        self,
        *,
        page: int = 1,
        page_size: int = 10,
        source_name: str | None = None,
        query: str | None = None,
        start_at: float | None = None,
        end_at: float | None = None,
    ) -> dict[str, Any]:
        safe_page = max(1, int(page))
        safe_page_size = min(100, max(1, int(page_size)))
        conditions: list[Any] = []
        clean_source_name = str(source_name or "").strip()
        clean_query = str(query or "").strip()
        if clean_source_name:
            conditions.append(WorkerAccessLog.key_name == clean_source_name)
        if clean_query:
            conditions.append(
                func.lower(WorkerAccessLog.query).contains(clean_query.lower(), autoescape=True)
            )
        if start_at is not None:
            conditions.append(
                WorkerAccessLog.created_at
                >= datetime.fromtimestamp(float(start_at), tz=timezone.utc)
            )
        if end_at is not None:
            conditions.append(
                WorkerAccessLog.created_at
                <= datetime.fromtimestamp(float(end_at), tz=timezone.utc)
            )
        async with self._sessions()() as session:
            total = int(
                await session.scalar(
                    select(func.count()).select_from(WorkerAccessLog).where(*conditions)
                )
                or 0
            )
            rows = list(
                (
                    await session.scalars(
                        select(WorkerAccessLog)
                        .where(*conditions)
                        .order_by(WorkerAccessLog.created_at.desc(), WorkerAccessLog.id.desc())
                        .limit(safe_page_size)
                        .offset((safe_page - 1) * safe_page_size)
                    )
                ).all()
            )
            source_names = [
                str(value)
                for value in (
                    await session.scalars(
                        select(WorkerAccessLog.key_name)
                        .distinct()
                        .order_by(WorkerAccessLog.key_name)
                    )
                ).all()
                if str(value).strip()
            ]
        return {
            "items": [
                {
                    "id": row.id,
                    "created_at": self._timestamp(row.created_at),
                    "source_id": row.key_id,
                    "source_name": row.key_name,
                    "query": row.query,
                }
                for row in rows
            ],
            "page": safe_page,
            "page_size": safe_page_size,
            "total": total,
            "total_pages": (total + safe_page_size - 1) // safe_page_size,
            "source_names": source_names,
        }


headless_activity_log_store = HeadlessActivityLogStore()
