"""Cross-database queue lease protocol for Core background job queues.

Both Core queues (knowledge import, semantic dimension build) use one
at-least-once lease protocol instead of leaking database-specific lock syntax
into business services:

- claiming is a single atomic state transition:
  - SQLite: ``UPDATE ... WHERE id = (SELECT ...) AND (...) RETURNING``; the
    write transaction is serialized by SQLite itself;
  - PostgreSQL: the candidate is selected with ``FOR UPDATE SKIP LOCKED``
    inside the same UPDATE statement;
- lease-critical timestamps (``lease_expires_at``, ``heartbeat_at`` and the
  COALESCE'd ``started_at``) are written with database-authoritative time
  (``clock_timestamp()`` on PostgreSQL, ``CURRENT_TIMESTAMP``/``datetime()``
  on SQLite), never with worker-host clocks; lease granularity is seconds;
- expired leases (including legacy ``running`` rows whose lease columns are
  still NULL) become claimable again, which is how a crashed worker's jobs are
  recovered;
- only the worker holding a matching ``lease_owner`` may heartbeat, update
  progress or finish a job; business code must route those writes through the
  guards here (``lease_valid`` / ``LeaseLostError``).
"""

from __future__ import annotations

import asyncio
import logging
import os
import socket
from contextvars import ContextVar
from typing import Any
from uuid import uuid4

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

logger = logging.getLogger(__name__)

# Lease owner of the job being processed in the current asyncio task. Workers
# bind it around handler execution so nested progress/terminal writes are
# guarded without changing every call site's signature.
_current_lease_owner: ContextVar[str | None] = ContextVar("puddingclaw_queue_lease_owner", default=None)


def bind_lease_owner(worker_id: str | None):
    """Bind the lease owner for the current task; returns a reset token."""

    return _current_lease_owner.set(worker_id)


def reset_lease_owner(token) -> None:
    _current_lease_owner.reset(token)


def current_lease_owner() -> str | None:
    return _current_lease_owner.get()


def _default_lease_seconds() -> int:
    return max(5, int(os.getenv("PUDDINGCLAW_QUEUE_LEASE_SECONDS", "120") or "120"))


class LeaseLostError(RuntimeError):
    """Raised when a worker no longer holds the lease for the job it processes."""


def new_worker_id(role: str) -> str:
    """A lease owner identity unique per process and per worker start."""

    return f"{socket.gethostname()}:{os.getpid()}:{role}:{uuid4().hex[:8]}"


def _dialect_name(session: AsyncSession) -> str:
    return session.get_bind().dialect.name


def _now_expr(dialect: str) -> str:
    return "clock_timestamp()" if dialect == "postgresql" else "CURRENT_TIMESTAMP"


def _expiry_expr(dialect: str) -> str:
    if dialect == "postgresql":
        return "clock_timestamp() + make_interval(secs => :lease_seconds)"
    return "datetime('now', :lease_modifier)"


def _lease_params(dialect: str, lease_seconds: int) -> dict[str, Any]:
    if dialect == "postgresql":
        return {"lease_seconds": int(lease_seconds)}
    return {"lease_modifier": f"+{int(lease_seconds)} seconds"}


# Public aliases for other database-level protocols (e.g. runtime_control's
# maintenance lease) that must share the same database-authoritative time
# expressions and bind-parameter shapes.


def db_now_expr(dialect: str) -> str:
    """Database-authoritative current-timestamp expression for ``dialect``."""

    return _now_expr(dialect)


def db_lease_expiry_expr(dialect: str) -> str:
    """Lease-expiry expression (now + :lease_seconds/:lease_modifier)."""

    return _expiry_expr(dialect)


def lease_bind_params(dialect: str, lease_seconds: int) -> dict[str, Any]:
    """Bind parameters matching ``db_lease_expiry_expr`` for ``dialect``."""

    return _lease_params(dialect, lease_seconds)


def _claimable_predicate(dialect: str) -> str:
    # Legacy rows stuck in 'running' without lease columns (NULL) are treated
    # as expired so pre-lease crashes remain recoverable.
    return (
        "(status = 'queued' OR (status = 'running' AND "
        f"(lease_expires_at IS NULL OR lease_expires_at < {_now_expr(dialect)})))"
    )


async def claim_next(
    session: AsyncSession,
    model: type,
    *,
    worker_id: str,
    lease_seconds: int | None = None,
    extra_sets: dict[str, Any] | None = None,
) -> Any | None:
    """Atomically claim the next queued (or expired-lease) job.

    ``extra_sets`` carries queue-specific claim fields (current_step, progress,
    ...); keys are validated against the model's columns. Returns the claimed
    ORM row, or None when no claimable job exists. The caller owns the commit.
    """

    lease_seconds = lease_seconds or _default_lease_seconds()
    table = model.__tablename__
    columns = set(model.__table__.columns.keys())
    extra_sets = dict(extra_sets or {})
    unknown = set(extra_sets) - columns
    if unknown:
        raise ValueError(f"claim extra_sets reference unknown columns on {table}: {sorted(unknown)}")

    dialect = _dialect_name(session)
    now = _now_expr(dialect)
    set_parts = [
        "status = 'running'",
        "lease_owner = :worker_id",
        f"lease_expires_at = {_expiry_expr(dialect)}",
        f"heartbeat_at = {now}",
        f"started_at = COALESCE(started_at, {now})",
        "attempt = attempt + 1",
    ]
    params: dict[str, Any] = {"worker_id": worker_id, **_lease_params(dialect, lease_seconds)}
    for key, value in extra_sets.items():
        param = f"extra_{key}"
        set_parts.append(f"{key} = :{param}")
        params[param] = value

    predicate = _claimable_predicate(dialect)
    candidate = f"SELECT id FROM {table} WHERE {predicate} ORDER BY created_at, id LIMIT 1"
    if dialect == "postgresql":
        candidate += " FOR UPDATE SKIP LOCKED"
    statement = text(
        f"UPDATE {table} SET {', '.join(set_parts)} "
        f"WHERE id = ({candidate}) AND {predicate} RETURNING id"
    )
    result = await session.execute(statement, params)
    row = result.first()
    if row is None:
        return None
    return await session.get(model, row[0])


async def heartbeat(
    session: AsyncSession,
    model: type,
    job_id: str,
    worker_id: str,
    *,
    lease_seconds: int | None = None,
) -> bool:
    """Renew the lease for a running job. Returns False when the lease is lost."""

    lease_seconds = lease_seconds or _default_lease_seconds()
    dialect = _dialect_name(session)
    statement = text(
        f"UPDATE {model.__tablename__} "
        f"SET heartbeat_at = {_now_expr(dialect)}, lease_expires_at = {_expiry_expr(dialect)} "
        "WHERE id = :job_id AND status = 'running' AND lease_owner = :worker_id "
        f"AND lease_expires_at IS NOT NULL AND lease_expires_at >= {_now_expr(dialect)}"
    )
    result = await session.execute(
        statement,
        {"job_id": job_id, "worker_id": worker_id, **_lease_params(dialect, lease_seconds)},
    )
    return result.rowcount == 1


async def lease_valid(session: AsyncSession, model: type, job_id: str, worker_id: str) -> bool:
    """Check that ``worker_id`` still owns the lease of the running job."""

    dialect = _dialect_name(session)
    statement = text(
        f"SELECT 1 FROM {model.__tablename__} "
        "WHERE id = :job_id AND status = 'running' AND lease_owner = :worker_id "
        f"AND lease_expires_at IS NOT NULL AND lease_expires_at >= {_now_expr(dialect)}"
    )
    result = await session.execute(statement, {"job_id": job_id, "worker_id": worker_id})
    return result.first() is not None


async def require_lease(session: AsyncSession, model: type, job_id: str, worker_id: str) -> None:
    """Fence the job row for this transaction or raise ``LeaseLostError``.

    A read-then-write lease check has a TOCTOU window: another worker can reclaim
    the row after the SELECT and before the stale worker commits.  The no-op
    conditional UPDATE below both verifies an unexpired lease and takes the row's
    write lock until the caller commits or rolls back. PostgreSQL claimers skip
    the locked row; SQLite serializes the competing write transaction.

    ``no_autoflush`` is essential because callers may already have dirty ORM
    state. Those changes must not be flushed until after this fence succeeds.
    """

    dialect = _dialect_name(session)
    statement = text(
        f"UPDATE {model.__tablename__} SET lease_owner = lease_owner "
        "WHERE id = :job_id AND status = 'running' AND lease_owner = :worker_id "
        f"AND lease_expires_at IS NOT NULL AND lease_expires_at >= {_now_expr(dialect)} "
        "RETURNING id"
    )
    with session.no_autoflush:
        result = await session.execute(statement, {"job_id": job_id, "worker_id": worker_id})
    if result.first() is None:
        raise LeaseLostError(f"lease lost for job {job_id} (worker {worker_id})")


async def require_current_lease(session: AsyncSession, model: type, job_id: str) -> None:
    """Require the lease bound to the current task (``bind_lease_owner``).

    Terminal state transitions call this right before writing so a worker
    whose lease was reclaimed cannot overwrite the new owner's state. Calls
    without a bound owner (direct API/test usage) skip the guard.
    """

    owner = current_lease_owner()
    if owner is not None:
        await require_lease(session, model, job_id, owner)


async def release_lease(
    session: AsyncSession,
    model: type,
    job_id: str,
    worker_id: str,
) -> None:
    """Clear lease fields on a job the caller owns (terminal states clear them).

    Safe to call unconditionally after a guarded terminal transition: the WHERE
    clause restricts the write to rows still owned by ``worker_id``.
    """

    statement = text(
        f"UPDATE {model.__tablename__} "
        "SET lease_owner = NULL, lease_expires_at = NULL, heartbeat_at = NULL "
        "WHERE id = :job_id AND lease_owner = :worker_id"
    )
    await session.execute(statement, {"job_id": job_id, "worker_id": worker_id})


async def heartbeat_loop(
    sessionmaker: async_sessionmaker[AsyncSession],
    model: type,
    job_id: str,
    worker_id: str,
    *,
    lease_seconds: int | None = None,
    stop_event: asyncio.Event | None = None,
    lost_event: asyncio.Event | None = None,
) -> None:
    """Renew a job's lease until ``stop_event`` is set or the lease is lost.

    Transient database errors are logged but do not abort the job: if they
    persist, the lease expires and another worker reclaims the job, while this
    worker's terminal writes are rejected by the lease guards.
    """

    lease_seconds = lease_seconds or _default_lease_seconds()
    interval = max(1.0, lease_seconds / 3)
    while True:
        if stop_event is not None and stop_event.is_set():
            return
        try:
            async with sessionmaker() as session:
                renewed = await heartbeat(session, model, job_id, worker_id, lease_seconds=lease_seconds)
                await session.commit()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("[queue-lease] heartbeat error job_id=%s", job_id)
            renewed = True
        if not renewed:
            logger.error("[queue-lease] lease lost job_id=%s worker=%s", job_id, worker_id)
            if lost_event is not None:
                lost_event.set()
            return
        if stop_event is None:
            await asyncio.sleep(interval)
        else:
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=interval)
            except asyncio.TimeoutError:
                pass
