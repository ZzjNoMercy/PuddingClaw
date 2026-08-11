"""Resolve explicit Kernel execution fallback requests."""

from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from graph.kernel_fallback_resume import kernel_fallback_resume_registry
from graph.session_manager import session_manager

router = APIRouter(prefix="/sessions/{session_id}/kernel-fallback-requests", tags=["sessions"])


class ResolveKernelFallbackRequest(BaseModel):
    request_version: int = Field(ge=1)
    action: Literal["switch_project_to_spawn", "fallback_once", "reject"]


@router.get("/{request_id}")
async def get_kernel_fallback_request(session_id: str, request_id: str) -> dict[str, Any]:
    request = kernel_fallback_resume_registry.get(request_id)
    if request is None or request.get("session_id") != session_id:
        raise HTTPException(status_code=404, detail="Kernel fallback request not found")
    return {"request": request}


@router.post("/{request_id}/resolve")
async def resolve_kernel_fallback_request(
    session_id: str,
    request_id: str,
    body: ResolveKernelFallbackRequest,
) -> dict[str, Any]:
    request = kernel_fallback_resume_registry.get(request_id)
    if request is None or request.get("session_id") != session_id:
        raise HTTPException(status_code=404, detail="Kernel fallback request not found")
    run_id = str(request.get("run_id") or "")
    run = session_manager.get_run_state(session_id, run_id)
    if request.get("status") != "resolved" and (not isinstance(run, dict) or run.get("status") != "waiting_hitl"):
        raise HTTPException(status_code=409, detail="Owning Run is no longer waiting for Kernel fallback")
    try:
        decision, resumed = kernel_fallback_resume_registry.resolve(
            request_id,
            body.action,
            request_version=body.request_version,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if decision is None:
        raise HTTPException(status_code=404, detail="Kernel fallback request not found")
    return {"session_id": session_id, "request_id": request_id, "decision": decision, "resumed": resumed}
