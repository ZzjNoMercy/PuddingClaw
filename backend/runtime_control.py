"""Database-level runtime control: drain/maintenance lease protocol.

The singleton ``core_runtime_control`` row (schema migration v3) coordinates
the stop-write protocol required before migrating the Core catalog between
SQLite and PostgreSQL:

- ``normal``: business as usual;
- ``draining``: workers stop claiming new jobs and new-job creation is
  rejected, while in-flight jobs keep heartbeating and finish under the
  queue lease protocol (``knowledge.queue_repository``);
- ``maintenance``: entered only when no job holds an active lease; the
  maintenance owner keeps renewing its lease while the migration runs.

Design rules:

- all lease timestamps are written with database-authoritative time
  expressions (shared with the queue lease protocol), so every replica
  observes the same state in multi-instance PostgreSQL deployments;
- every state transition is a single CAS UPDATE whose WHERE clause encodes
  the precondition, so concurrent contenders resolve deterministically via
  rowcount instead of read-modify-write races;
- ``generation`` increments on every write_mode transition, letting callers
  detect state changes;
- readers tolerate pre-v3 databases (no ``core_runtime_control`` table) by
  treating them as ``normal`` so old catalogs keep working until migrated.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from knowledge.queue_repository import db_lease_expiry_expr, db_now_expr, lease_bind_params

logger = logging.getLogger(__name__)

TABLE_NAME = "core_runtime_control"
SINGLETON_ID = 1

WRITE_MODE_NORMAL = "normal"
WRITE_MODE_DRAINING = "draining"
WRITE_MODE_MAINTENANCE = "maintenance"
WRITE_MODES = (WRITE_MODE_NORMAL, WRITE_MODE_DRAINING, WRITE_MODE_MAINTENANCE)

DEFAULT_LEASE_SECONDS = 300
DEFAULT_RETRY_AFTER_SECONDS = 30

_JOB_TABLES = ("knowledge_import_jobs", "semantic_dimension_build_jobs")

_SELECT_STATE = (
    "SELECT write_mode, maintenance_owner, lease_expires_at, generation, reason, updated_at "
    "FROM core_runtime_control WHERE id = 1"
)


class MaintenanceConflictError(RuntimeError):
    """A maintenance CAS transition lost to the current runtime state."""

    def __init__(self, message: str, *, state: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.state = state or {}


class MaintenanceModeError(RuntimeError):
    """A write was attempted while Core is draining or under maintenance."""

    def __init__(self, write_mode: str, *, retry_after: int = DEFAULT_RETRY_AFTER_SECONDS) -> None:
        super().__init__(f"Core is in '{write_mode}' mode; new writes are temporarily rejected")
        self.write_mode = write_mode
        self.retry_after = retry_after


def _dialect(session: AsyncSession) -> str:
    return session.get_bind().dialect.name


def _serialize(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


def _row_to_state(row: Any) -> dict[str, Any]:
    return {
        "write_mode": str(row[0]),
        "maintenance_owner": row[1],
        "lease_expires_at": _serialize(row[2]),
        "generation": int(row[3]),
        "reason": str(row[4] or ""),
        "updated_at": _serialize(row[5]),
    }


def _default_state() -> dict[str, Any]:
    return {
        "write_mode": WRITE_MODE_NORMAL,
        "maintenance_owner": None,
        "lease_expires_at": None,
        "generation": 0,
        "reason": "",
        "updated_at": None,
    }


async def _ensure_row(session: AsyncSession) -> bool:
    """Insert the default singleton row when missing; returns True if inserted.

    ``INSERT ... SELECT ... WHERE NOT EXISTS`` is portable across SQLite and
    PostgreSQL and loses silently against a concurrent insert instead of
    raising a unique-violation that would poison the transaction.
    """

    result = await session.execute(
        text(
            f"INSERT INTO core_runtime_control (id, updated_at) "
            f"SELECT 1, {db_now_expr(_dialect(session))} "
            "WHERE NOT EXISTS (SELECT 1 FROM core_runtime_control WHERE id = 1)"
        )
    )
    return result.rowcount == 1


async def _read_state(session: AsyncSession) -> dict[str, Any]:
    row = (await session.execute(text(_SELECT_STATE))).first()
    return _row_to_state(row) if row is not None else _default_state()


async def get_state(session: AsyncSession, *, create_if_missing: bool = True) -> dict[str, Any]:
    """Return the singleton state, lazily creating the default row.

    With ``create_if_missing=False`` the read never writes: a missing row
    returns the default ``normal`` view with ``note="not_initialized"`` so
    read-only callers (e.g. GET /api/maintenance/status) stay side-effect free.
    """

    if create_if_missing and await _ensure_row(session):
        await session.commit()
    row = (await session.execute(text(_SELECT_STATE))).first()
    if row is not None:
        return _row_to_state(row)
    state = _default_state()
    if not create_if_missing:
        state["note"] = "not_initialized"
    return state


async def queue_running_counts(session: AsyncSession) -> dict[str, dict[str, int]]:
    """Per-queue count of running jobs, split by whether the lease is active."""

    now = db_now_expr(_dialect(session))
    counts: dict[str, dict[str, int]] = {}
    for table in _JOB_TABLES:
        running = int(
            (
                await session.execute(text(f"SELECT COUNT(*) FROM {table} WHERE status = 'running'"))
            ).scalar_one()
        )
        active = int(
            (
                await session.execute(
                    text(
                        f"SELECT COUNT(*) FROM {table} WHERE status = 'running' "
                        f"AND lease_expires_at IS NOT NULL AND lease_expires_at >= {now}"
                    )
                )
            ).scalar_one()
        )
        counts[table] = {"running": running, "active_lease": active}
    return counts


async def acquire_maintenance(
    session: AsyncSession,
    *,
    owner: str,
    lease_seconds: int = DEFAULT_LEASE_SECONDS,
    reason: str = "",
) -> dict[str, Any]:
    """Take the maintenance lease and switch to ``draining`` (CAS).

    Succeeds only when the mode is ``normal``, the current lease has expired
    (or was never set), or the same owner re-acquires. Otherwise raises
    MaintenanceConflictError carrying the current state.
    """

    dialect = _dialect(session)
    now = db_now_expr(dialect)
    await _ensure_row(session)
    result = await session.execute(
        text(
            "UPDATE core_runtime_control SET "
            "write_mode = 'draining', maintenance_owner = :owner, "
            f"lease_expires_at = {db_lease_expiry_expr(dialect)}, "
            "generation = generation + 1, reason = :reason, "
            f"updated_at = {now} "
            "WHERE id = 1 AND ("
            "write_mode = 'normal' "
            "OR lease_expires_at IS NULL "
            f"OR lease_expires_at < {now} "
            "OR maintenance_owner = :owner)"
        ),
        {"owner": owner, "reason": reason or "", **lease_bind_params(dialect, lease_seconds)},
    )
    if result.rowcount != 1:
        state = await _read_state(session)
        raise MaintenanceConflictError(
            f"maintenance lease is held by {state.get('maintenance_owner')!r} "
            f"until {state.get('lease_expires_at')} (write_mode={state.get('write_mode')})",
            state=state,
        )
    await session.commit()
    return await _read_state(session)


async def renew_maintenance(
    session: AsyncSession,
    *,
    owner: str,
    lease_seconds: int = DEFAULT_LEASE_SECONDS,
) -> dict[str, Any]:
    """Extend the lease held by ``owner``; ``generation`` is unchanged."""

    dialect = _dialect(session)
    result = await session.execute(
        text(
            "UPDATE core_runtime_control SET "
            f"lease_expires_at = {db_lease_expiry_expr(dialect)}, "
            f"updated_at = {db_now_expr(dialect)} "
            "WHERE id = 1 AND maintenance_owner = :owner "
            "AND write_mode IN ('draining', 'maintenance')"
        ),
        {"owner": owner, **lease_bind_params(dialect, lease_seconds)},
    )
    if result.rowcount != 1:
        state = await _read_state(session)
        raise MaintenanceConflictError(
            f"cannot renew: lease owner is {state.get('maintenance_owner')!r} "
            f"(write_mode={state.get('write_mode')})",
            state=state,
        )
    await session.commit()
    return await _read_state(session)


async def enter_maintenance(
    session: AsyncSession,
    *,
    owner: str,
    lease_seconds: int = DEFAULT_LEASE_SECONDS,
) -> dict[str, Any]:
    """Switch from ``draining`` to ``maintenance`` once the queues are quiet.

    The "no running job with an unexpired lease" precondition lives inside the
    CAS UPDATE's WHERE clause, so a job claimed between a check and the write
    cannot slip through (no TOCTOU window). Running jobs whose lease already
    expired are safe to take over (their owner is gone) and do not block the
    transition.
    """

    dialect = _dialect(session)
    now = db_now_expr(dialect)
    quiet_predicates = "".join(
        f"AND NOT EXISTS (SELECT 1 FROM {table} WHERE status = 'running' "
        f"AND lease_expires_at IS NOT NULL AND lease_expires_at >= {now}) "
        for table in _JOB_TABLES
    )
    result = await session.execute(
        text(
            "UPDATE core_runtime_control SET "
            "write_mode = 'maintenance', "
            f"lease_expires_at = {db_lease_expiry_expr(dialect)}, "
            "generation = generation + 1, "
            f"updated_at = {now} "
            "WHERE id = 1 AND write_mode = 'draining' AND maintenance_owner = :owner "
            + quiet_predicates
        ),
        {"owner": owner, **lease_bind_params(dialect, lease_seconds)},
    )
    if result.rowcount != 1:
        # The CAS lost; SELECT only to explain why.
        state = await _read_state(session)
        if state["write_mode"] == WRITE_MODE_DRAINING and state["maintenance_owner"] == owner:
            counts = await queue_running_counts(session)
            active = sum(queue["active_lease"] for queue in counts.values())
            raise MaintenanceConflictError(
                f"还有 {active} 个运行中任务（lease 未过期），不能进入 maintenance",
                state=state,
            )
        raise MaintenanceConflictError(
            f"cannot enter maintenance: write_mode={state.get('write_mode')}, "
            f"owner={state.get('maintenance_owner')!r}",
            state=state,
        )
    await session.commit()
    return await _read_state(session)


async def release_maintenance(
    session: AsyncSession,
    *,
    owner: str,
    reason: str = "",
) -> dict[str, Any]:
    """Return to ``normal``, clearing owner and lease (owner must match)."""

    dialect = _dialect(session)
    result = await session.execute(
        text(
            "UPDATE core_runtime_control SET "
            "write_mode = 'normal', maintenance_owner = NULL, lease_expires_at = NULL, "
            "generation = generation + 1, reason = :reason, "
            f"updated_at = {db_now_expr(dialect)} "
            "WHERE id = 1 AND maintenance_owner = :owner "
            "AND write_mode IN ('draining', 'maintenance')"
        ),
        {"owner": owner, "reason": reason or ""},
    )
    if result.rowcount != 1:
        state = await _read_state(session)
        raise MaintenanceConflictError(
            f"cannot release: lease owner is {state.get('maintenance_owner')!r} "
            f"(write_mode={state.get('write_mode')})",
            state=state,
        )
    await session.commit()
    return await _read_state(session)


async def _read_write_mode_tolerant(session: AsyncSession) -> str:
    """Read the current write_mode; pre-v3 databases count as ``normal``.

    A missing ``core_runtime_control`` table (catalog not yet migrated to
    schema v3) fails open so old databases keep working. Table existence is
    probed with a metadata query instead of try/except so the caller's
    session is never poisoned: an exception path would force a rollback,
    which expires every ORM object the caller has already loaded.
    """

    dialect = _dialect(session)
    if dialect == "postgresql":
        exists = await session.scalar(text("SELECT to_regclass('core_runtime_control')"))
    else:
        exists = await session.scalar(
            text("SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'core_runtime_control'")
        )
    if not exists:
        return WRITE_MODE_NORMAL
    row = (await session.execute(text("SELECT write_mode FROM core_runtime_control WHERE id = 1"))).first()
    return str(row[0]) if row is not None else WRITE_MODE_NORMAL


async def writes_allowed(session: AsyncSession) -> bool:
    """True only in ``normal`` mode; missing table counts as normal."""

    return await _read_write_mode_tolerant(session) == WRITE_MODE_NORMAL


async def assert_writes_allowed(session: AsyncSession) -> None:
    """Raise MaintenanceModeError unless writes are currently allowed."""

    write_mode = await _read_write_mode_tolerant(session)
    if write_mode != WRITE_MODE_NORMAL:
        raise MaintenanceModeError(write_mode, retry_after=DEFAULT_RETRY_AFTER_SECONDS)
