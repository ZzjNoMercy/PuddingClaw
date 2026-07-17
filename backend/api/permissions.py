"""Session permission APIs for external file access."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

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


class ToolActionGrantRequest(BaseModel):
    permission_request_id: str
    scope: str = Field(pattern="^(once|session)$")


class ApprovalModeUpdateRequest(BaseModel):
    approval_mode: Literal["strict", "smart"]
    expected_epoch: int | None = Field(default=None, ge=1)


@router.get("/sessions/{session_id}/permissions")
async def list_permissions(session_id: str) -> dict[str, Any]:
    """List active permission grants for a session."""

    try:
        session_manager.get_permission_policy(session_id)
        grants = session_manager.list_permission_grants(session_id)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Session not found") from exc
    for grant in grants:
        if grant.get("type") != "tool_action" or grant.get("metadata"):
            continue
        request = permission_resume_registry.find_tool_action_request(str(grant.get("target") or ""))
        if request is not None:
            grant["metadata"] = {
                "tool_name": request.get("tool_name"),
                "command": request.get("command"),
                "reason": request.get("reason"),
                "risk": request.get("risk"),
            }
    return {"session_id": session_id, "grants": grants}


@router.get("/sessions/{session_id}/permissions/mode")
async def get_approval_mode(session_id: str) -> dict[str, Any]:
    try:
        policy = session_manager.get_permission_policy(session_id)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Session not found") from exc
    return {"session_id": session_id, **policy}


@router.patch("/sessions/{session_id}/permissions/mode")
async def update_approval_mode(
    session_id: str,
    req: ApprovalModeUpdateRequest,
) -> dict[str, Any]:
    try:
        policy = session_manager.set_approval_mode_if_idle(
            session_id,
            req.approval_mode,
            expected_epoch=req.expected_epoch,
        )
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Session not found") from exc
    except (RuntimeError, ValueError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"session_id": session_id, **policy}


@router.post("/sessions/{session_id}/permissions/external-files")
async def grant_external_file_permission(
    session_id: str,
    req: ExternalFileGrantRequest,
) -> dict[str, Any]:
    """Grant the read/write capability declared by a pending external-file request."""

    pending = permission_resume_registry.get(req.permission_request_id) if req.permission_request_id else None
    if req.permission_request_id and pending is None:
        raise HTTPException(status_code=404, detail="permission request not found")
    if pending is not None and pending.get("session_id") != session_id:
        raise HTTPException(status_code=400, detail="permission request belongs to another session")
    if pending is not None and pending.get("status") != "pending":
        raise HTTPException(status_code=409, detail="permission request is no longer pending")
    if pending is not None and pending.get("type") not in {
        "external_file_read",
        "external_file_write",
    }:
        raise HTTPException(status_code=400, detail="permission request is not an external file action")
    access = "write" if pending and pending.get("type") == "external_file_write" else "read"
    if access == "write" and req.target_kind != "exact_file":
        raise HTTPException(status_code=400, detail="external write permission only supports exact_file")

    if req.target_kind == "exact_file":
        if not req.path:
            raise HTTPException(status_code=400, detail="path is required for exact_file grants")
        target = str(Path(req.path).expanduser().resolve())
        if pending is not None and target != str(Path(str(pending.get("path") or "")).expanduser().resolve()):
            raise HTTPException(status_code=400, detail="path does not match the pending permission request")
    else:
        target = "*"

    try:
        grant = session_manager.add_permission_grant(
            session_id,
            grant_type=f"external_file_{access}",
            target_kind=req.target_kind,
            target=target,
            capabilities=[access, "external_path"],
            scope="session",
            source="user",
        )
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Session not found") from exc
    resumed = False
    if req.permission_request_id:
        resumed = permission_resume_registry.resolve(
            req.permission_request_id,
            {"type": "approve", "grant_id": grant["id"]},
        )
        if not resumed:
            session_manager.revoke_permission_grant(session_id, grant["id"])
            raise HTTPException(
                status_code=409,
                detail="permission request was resolved concurrently",
            )
    return {"session_id": session_id, "grant": grant, "resumed": resumed}


@router.post("/sessions/{session_id}/permissions/deny")
async def deny_permission_request(
    session_id: str,
    req: PermissionDenyRequest,
) -> dict[str, Any]:
    """Reject a pending permission request without creating a grant."""

    pending = permission_resume_registry.get(req.permission_request_id)
    if pending is None:
        raise HTTPException(status_code=404, detail="Permission request not found")
    if pending.get("session_id") != session_id:
        raise HTTPException(
            status_code=400,
            detail="permission request belongs to another session",
        )
    if pending.get("status") != "pending":
        raise HTTPException(
            status_code=409,
            detail="permission request is no longer pending",
        )
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


@router.post("/sessions/{session_id}/permissions/tool-actions")
async def grant_tool_action_permission(
    session_id: str,
    req: ToolActionGrantRequest,
) -> dict[str, Any]:
    """Approve one managed Tool action once or for this Session."""

    pending = permission_resume_registry.get(req.permission_request_id)
    if pending is None:
        raise HTTPException(status_code=404, detail="permission request not found")
    if pending.get("session_id") != session_id:
        raise HTTPException(
            status_code=400,
            detail="permission request belongs to another session",
        )
    if pending.get("type") != "tool_action":
        raise HTTPException(
            status_code=400,
            detail="permission request is not a tool action",
        )
    if pending.get("status") != "pending":
        raise HTTPException(
            status_code=409,
            detail="permission request is no longer pending",
        )
    fingerprint = str(pending.get("fingerprint") or "")
    if not fingerprint:
        raise HTTPException(status_code=400, detail="permission fingerprint missing")
    session_target_kind = str(pending.get("session_target_kind") or "")
    session_target = str(pending.get("session_target") or "")
    use_reusable_scope = req.scope == "session" and session_target_kind and session_target
    target_kind = session_target_kind if use_reusable_scope else "fingerprint"
    target = session_target if use_reusable_scope else fingerprint
    metadata = {
        "tool_name": pending.get("tool_name"),
        "command": pending.get("command"),
        "reason": pending.get("reason"),
        "risk": pending.get("risk"),
    }
    if pending.get("run_id"):
        metadata["run_id"] = pending.get("run_id")
    if use_reusable_scope:
        metadata["session_scope_label"] = pending.get("session_scope_label")
        metadata["session_target"] = session_target
    try:
        grant = session_manager.add_permission_grant(
            session_id,
            grant_type="tool_action",
            target_kind=target_kind,
            target=target,
            capabilities=[str(item) for item in (pending.get("capabilities") or ["execute"])],
            scope=req.scope,
            source="user",
            metadata=metadata,
            bindings=(dict(pending["grant_bindings"]) if isinstance(pending.get("grant_bindings"), dict) else None),
        )
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Session not found") from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    resumed = permission_resume_registry.resolve(
        req.permission_request_id,
        {"type": "approve", "grant_id": grant["id"]},
    )
    if not resumed:
        session_manager.revoke_permission_grant(session_id, grant["id"])
        raise HTTPException(
            status_code=409,
            detail="permission request was resolved concurrently",
        )
    return {
        "session_id": session_id,
        "grant": grant,
        "resumed": resumed,
    }


@router.post("/sessions/{session_id}/permissions/{grant_id}/revoke")
async def revoke_permission(session_id: str, grant_id: str) -> dict[str, Any]:
    """Revoke an active session permission grant."""

    if not session_manager.revoke_permission_grant(session_id, grant_id):
        raise HTTPException(status_code=404, detail="Permission grant not found")
    return {"session_id": session_id, "grant_id": grant_id, "revoked": True}
