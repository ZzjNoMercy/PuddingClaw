"""Control API for the Core drain/maintenance runtime protocol.

These endpoints drive the database-level stop-write protocol in
``runtime_control`` (singleton table ``core_runtime_control``, schema
migration v3) that must run before migrating the Core catalog between
SQLite and PostgreSQL:

- ``drain`` takes the maintenance lease and flips the catalog to
  ``draining``: workers stop claiming new jobs and new-job creation is
  rejected, while in-flight jobs finish under the queue lease protocol;
- ``enter`` flips to ``maintenance`` once no job holds an active lease;
- ``renew`` extends the lease while the maintenance window is open;
- ``release`` returns the catalog to ``normal``.

Auth note: like every other local Core API (settings, knowledge, ...), these
endpoints currently carry no authentication dependency; they are bound to
the same trusted-local surface as the rest of the control plane.
"""

from __future__ import annotations

import os
import socket
from typing import Any
from uuid import uuid4

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

import runtime_control
from db import get_db_session
from runtime_control import MaintenanceConflictError, MaintenanceModeError

router = APIRouter(prefix="/maintenance", tags=["maintenance"])

LEASE_SECONDS_MIN = 5
LEASE_SECONDS_MAX = 3600


class DrainRequest(BaseModel):
    owner: str | None = Field(default=None, max_length=120)
    reason: str = Field(default="", max_length=2000)
    lease_seconds: int = Field(default=runtime_control.DEFAULT_LEASE_SECONDS, ge=LEASE_SECONDS_MIN, le=LEASE_SECONDS_MAX)


class OwnerRequest(BaseModel):
    owner: str = Field(min_length=1, max_length=120)
    lease_seconds: int = Field(default=runtime_control.DEFAULT_LEASE_SECONDS, ge=LEASE_SECONDS_MIN, le=LEASE_SECONDS_MAX)


class ReleaseRequest(BaseModel):
    owner: str = Field(min_length=1, max_length=120)
    reason: str = Field(default="", max_length=2000)


def _default_owner() -> str:
    """A lease owner identity unique per process and per API call."""

    return f"api:{socket.gethostname()}:{os.getpid()}:{uuid4().hex[:8]}"


def _conflict_response(exc: MaintenanceConflictError) -> JSONResponse:
    return JSONResponse(status_code=409, content={"detail": str(exc), "state": exc.state})


def _maintenance_mode_response(exc: MaintenanceModeError) -> JSONResponse:
    return JSONResponse(
        status_code=503,
        headers={"Retry-After": str(exc.retry_after)},
        content={"detail": str(exc), "write_mode": exc.write_mode},
    )


@router.get("/status")
async def maintenance_status(session: AsyncSession = Depends(get_db_session)) -> dict[str, Any]:
    """Current runtime-control state plus per-queue running job counts.

    Read-only: a missing singleton row reports the default ``normal`` view with
    ``note="not_initialized"`` instead of lazily inserting it.
    """

    state = await runtime_control.get_state(session, create_if_missing=False)
    queues = await runtime_control.queue_running_counts(session)
    return {"state": state, "queues": queues}


@router.post("/drain")
async def maintenance_drain(
    payload: DrainRequest,
    session: AsyncSession = Depends(get_db_session),
) -> Any:
    """Take the maintenance lease and switch the catalog to ``draining``."""

    try:
        return await runtime_control.acquire_maintenance(
            session,
            owner=payload.owner or _default_owner(),
            lease_seconds=payload.lease_seconds,
            reason=payload.reason,
        )
    except MaintenanceConflictError as exc:
        return _conflict_response(exc)
    except MaintenanceModeError as exc:
        return _maintenance_mode_response(exc)


@router.post("/renew")
async def maintenance_renew(
    payload: OwnerRequest,
    session: AsyncSession = Depends(get_db_session),
) -> Any:
    """Extend the maintenance lease held by ``owner``."""

    try:
        return await runtime_control.renew_maintenance(
            session, owner=payload.owner, lease_seconds=payload.lease_seconds
        )
    except MaintenanceConflictError as exc:
        return _conflict_response(exc)
    except MaintenanceModeError as exc:
        return _maintenance_mode_response(exc)


@router.post("/enter")
async def maintenance_enter(
    payload: OwnerRequest,
    session: AsyncSession = Depends(get_db_session),
) -> Any:
    """Switch from ``draining`` to ``maintenance`` once the queues are quiet."""

    try:
        return await runtime_control.enter_maintenance(
            session, owner=payload.owner, lease_seconds=payload.lease_seconds
        )
    except MaintenanceConflictError as exc:
        return _conflict_response(exc)
    except MaintenanceModeError as exc:
        return _maintenance_mode_response(exc)


@router.post("/release")
async def maintenance_release(
    payload: ReleaseRequest,
    session: AsyncSession = Depends(get_db_session),
) -> Any:
    """Release the maintenance lease and return the catalog to ``normal``."""

    try:
        return await runtime_control.release_maintenance(session, owner=payload.owner, reason=payload.reason)
    except MaintenanceConflictError as exc:
        return _conflict_response(exc)
    except MaintenanceModeError as exc:
        return _maintenance_mode_response(exc)
