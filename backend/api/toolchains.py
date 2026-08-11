"""Managed CLI Toolchain revision lifecycle API."""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from api.connectors import _sandbox_manager
from runtime_identity.service import ManagedCliService

router = APIRouter(prefix="/toolchains", tags=["toolchains"])


class RollbackPreviewRequest(BaseModel):
    target_revision: str


class RollbackCommitRequest(BaseModel):
    plan_id: str
    binding: str
    confirmed: bool = False


def _service() -> ManagedCliService:
    return ManagedCliService(_sandbox_manager())


@router.get("/{adapter_id}/revisions")
async def list_toolchain_revisions(adapter_id: str):
    try:
        revisions = await asyncio.to_thread(_service().list_toolchain_revisions, adapter_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail="Managed CLI Adapter 不存在或不可用。") from exc
    return {"adapter_id": adapter_id, "revisions": revisions}


@router.post("/{adapter_id}/rollback/preview")
async def preview_toolchain_rollback(adapter_id: str, request: RollbackPreviewRequest):
    try:
        plan = await asyncio.to_thread(
            _service().plan_toolchain_rollback,
            adapter_id,
            request.target_revision,
        )
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {
        "status": "approval_required",
        "plan_id": plan.plan_id,
        "binding": plan.binding,
        "expires_at": plan.expires_at,
        "approval_preview": plan.approval_preview(),
    }


@router.post("/{adapter_id}/rollback/commit")
async def commit_toolchain_rollback(adapter_id: str, request: RollbackCommitRequest):
    result = await asyncio.to_thread(
        _service().execute_toolchain_rollback,
        adapter_id,
        request.plan_id,
        request.binding,
        confirmed=request.confirmed,
    )
    if result.exit_code != 0:
        raise HTTPException(status_code=409, detail=result.payload)
    return result.payload
