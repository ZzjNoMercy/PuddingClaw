"""Session permission APIs for external file access."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from graph.permission_resume import permission_resume_registry
from graph.session_manager import session_manager

router = APIRouter()


class ExternalFileGrantRequest(BaseModel):
    target_kind: str = Field(pattern="^(exact_file|all_external_files)$")
    path: str | None = None
    permission_request_id: str | None = None


class PermissionDenyRequest(BaseModel):
    permission_request_id: str
    message: str | None = None


@router.get("/sessions/{session_id}/permissions")
async def list_permissions(session_id: str) -> dict[str, Any]:
    """List active permission grants for a session."""

    return {"session_id": session_id, "grants": session_manager.list_permission_grants(session_id)}


@router.post("/sessions/{session_id}/permissions/external-files")
async def grant_external_file_read(
    session_id: str,
    req: ExternalFileGrantRequest,
) -> dict[str, Any]:
    """Grant read access to one external file or all explicitly provided external files."""

    if req.target_kind == "exact_file":
        if not req.path:
            raise HTTPException(status_code=400, detail="path is required for exact_file grants")
        target = str(Path(req.path).expanduser().resolve())
    else:
        target = "*"

    grant = session_manager.add_permission_grant(
        session_id,
        grant_type="external_file_read",
        target_kind=req.target_kind,
        target=target,
        capabilities=["read", "external_path"],
        scope="session",
        source="user",
    )
    resumed = False
    if req.permission_request_id:
        resumed = permission_resume_registry.resolve(
            req.permission_request_id,
            {"type": "approve", "grant_id": grant["id"]},
        )
    return {"session_id": session_id, "grant": grant, "resumed": resumed}


@router.post("/sessions/{session_id}/permissions/deny")
async def deny_permission_request(
    session_id: str,
    req: PermissionDenyRequest,
) -> dict[str, Any]:
    """Reject a pending permission request without creating a grant."""

    resumed = permission_resume_registry.resolve(
        req.permission_request_id,
        {
            "type": "reject",
            "message": req.message or "User denied permission.",
        },
    )
    if not resumed:
        raise HTTPException(status_code=404, detail="Permission request not found")
    return {
        "session_id": session_id,
        "permission_request_id": req.permission_request_id,
        "resumed": True,
    }


@router.post("/sessions/{session_id}/permissions/{grant_id}/revoke")
async def revoke_permission(session_id: str, grant_id: str) -> dict[str, Any]:
    """Revoke an active session permission grant."""

    if not session_manager.revoke_permission_grant(session_id, grant_id):
        raise HTTPException(status_code=404, detail="Permission grant not found")
    return {"session_id": session_id, "grant_id": grant_id, "revoked": True}
