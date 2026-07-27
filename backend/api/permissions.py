"""Session permission APIs for external file access."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from graph.permission_policy import RunPermissionContext
from graph.permission_resume import permission_resume_registry
from graph.session_manager import session_manager

router = APIRouter()


class ExternalFileGrantRequest(BaseModel):
    target_kind: str = Field(pattern="^(exact_file|all_external_files|exact_directory)$")
    path: str | None = None
    permission_request_id: str | None = None
    scope: Literal["run", "session"] | None = None


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
    """List active grants and recent consumed/revoked grant history."""

    try:
        session_manager.get_permission_policy(session_id)
        session_manager.migrate_permission_grants(session_id)
        grants = session_manager.list_permission_grants(session_id)
        history = session_manager.list_permission_grant_history(session_id)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Session not found") from exc
    for grant in [*grants, *history]:
        if grant.get("type") == "tool_action" and not grant.get("metadata"):
            request = permission_resume_registry.find_tool_action_request(str(grant.get("target") or ""))
            if request is not None:
                grant["metadata"] = {
                    "tool_name": request.get("tool_name"),
                    "command": request.get("command"),
                    "reason": request.get("reason"),
                    "risk": request.get("risk"),
                    "policy_source": request.get("policy_source"),
                    "policy_explanation": request.get("policy_explanation"),
                    "control_descriptor": request.get("control_descriptor"),
                    "change_preview": request.get("change_preview"),
                }
    return {"session_id": session_id, "grants": grants, "history": history}


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
        "external_file_delete",
        "external_directory_read",
        "external_directory_write",
    }:
        # Keep the legacy response text for API compatibility. The endpoint
        # now accepts both external files and exact external directories.
        raise HTTPException(status_code=400, detail="permission request is not an external file action")
    pending_type = str((pending or {}).get("type") or "external_file_read")
    is_directory = pending_type.startswith("external_directory_")
    access = pending_type.rsplit("_", 1)[-1]
    if access not in {"read", "write", "delete"}:
        raise HTTPException(status_code=400, detail="unsupported external path capability")
    effective_scope = str(req.scope or ("run" if is_directory else "session"))
    if is_directory and effective_scope not in {"run", "session"}:
        raise HTTPException(status_code=400, detail="external directory scope must be run or session")
    if not is_directory and req.scope not in {None, "session"}:
        raise HTTPException(status_code=400, detail="external file grants are Session-scoped")
    expected_target_kind = "exact_directory" if is_directory else "exact_file"
    if pending is None and req.target_kind == "exact_directory":
        raise HTTPException(status_code=400, detail="external directory permission requires a pending request")
    effective_target_kind = "exact_directory" if is_directory else req.target_kind
    if access in {"write", "delete"} and effective_target_kind != expected_target_kind:
        raise HTTPException(
            status_code=400,
            detail=f"external {access} permission requires {expected_target_kind}",
        )

    if effective_target_kind in {"exact_file", "exact_directory"}:
        # Older frontends render directory requests as external-file cards.
        # For a trusted pending directory request, collapse exact_file or the
        # old broad button back to the pending exact directory. This preserves
        # compatibility without widening the authorization boundary.
        requested_path = req.path or (str((pending or {}).get("path") or "") if is_directory else "")
        if not requested_path:
            raise HTTPException(status_code=400, detail="path is required for exact path grants")
        target = str(Path(requested_path).expanduser().resolve())
        if pending is not None and target != str(Path(str(pending.get("path") or "")).expanduser().resolve()):
            raise HTTPException(status_code=400, detail="path does not match the pending permission request")
    else:
        target = "*"

    grant_bindings = (
        dict(pending.get("grant_bindings"))
        if isinstance((pending or {}).get("grant_bindings"), dict)
        else None
    )
    if is_directory and grant_bindings is None:
        run_id = str((pending or {}).get("run_id") or "")
        run = session_manager.get_run_state(session_id, run_id) if run_id else None
        if isinstance(run, dict):
            grant_bindings = RunPermissionContext.from_config_snapshot(
                run.get("config_snapshot")
            ).grant_bindings()
    if is_directory and effective_scope == "session" and grant_bindings is None:
        raise HTTPException(
            status_code=409,
            detail="Session directory permission requires an active bound Run",
        )

    requested_capabilities = [
        str(item)
        for item in (pending or {}).get("capabilities") or []
        if str(item)
    ]
    if not requested_capabilities:
        requested_capabilities = [
            access,
            *(["recursive"] if is_directory else []),
            "external_path",
        ]
    if access not in requested_capabilities:
        requested_capabilities.insert(0, access)
    if not is_directory:
        requested_capabilities = [
            item
            for item in requested_capabilities
            if item in {access, "external_path"}
        ]
    try:
        grant = session_manager.add_permission_grant(
            session_id,
            grant_type=f"external_{'directory' if is_directory else 'file'}_{access}",
            target_kind=effective_target_kind,
            target=target,
            capabilities=requested_capabilities,
            scope=effective_scope if is_directory else "session",
            source="user",
            metadata=(
                {
                    "run_id": str((pending or {}).get("run_id") or ""),
                    "requested_target_kind": req.target_kind,
                    "requested_scope": effective_scope,
                }
                if is_directory
                else None
            ),
            bindings=grant_bindings if is_directory else None,
        )
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Session not found") from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    resumed = False
    auto_resumed: list[str] = []
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
        if is_directory and effective_scope == "session" and grant_bindings is not None:
            def resolve_pending_bindings(request: dict[str, Any]) -> dict[str, Any] | None:
                request_run_id = str(request.get("run_id") or "")
                request_run = (
                    session_manager.get_run_state(session_id, request_run_id)
                    if request_run_id
                    else None
                )
                if not isinstance(request_run, dict):
                    return None
                return RunPermissionContext.from_config_snapshot(
                    request_run.get("config_snapshot")
                ).grant_bindings()

            auto_resumed = permission_resume_registry.resolve_compatible_session_external_directories(
                session_id=session_id,
                path=target,
                access=access,
                capabilities=list(grant.get("capabilities") or []),
                decision={"type": "approve", "grant_id": grant["id"]},
                grant_bindings=grant_bindings,
                exclude_request_id=req.permission_request_id,
                binding_resolver=resolve_pending_bindings,
            )
    return {
        "session_id": session_id,
        "grant": grant,
        "resumed": resumed,
        "auto_resumed_permission_request_ids": auto_resumed,
    }


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
    capabilities = [str(item) for item in (pending.get("capabilities") or ["execute"])]
    is_skill_management = str(pending.get("tool_name") or "") in {
        "prepare_skill_install",
        "prepare_skill_update",
        "install_skill",
        "update_skill",
    } or str(pending.get("reason") or "").startswith("managed_skill_source_download:")
    if is_skill_management and req.scope != "once":
        raise HTTPException(
            status_code=400,
            detail="Skill management actions only support one-time approval",
        )
    fingerprint = str(pending.get("fingerprint") or "")
    if not fingerprint:
        raise HTTPException(status_code=400, detail="permission fingerprint missing")
    session_target_kind = str(pending.get("session_target_kind") or "")
    session_target = str(pending.get("session_target") or "")
    if req.scope == "session" and not (session_target_kind and session_target):
        raise HTTPException(
            status_code=400,
            detail="This action only supports one-time approval",
        )
    use_reusable_scope = req.scope == "session" and session_target_kind and session_target
    target_kind = session_target_kind if use_reusable_scope else "fingerprint"
    target = session_target if use_reusable_scope else fingerprint
    metadata = {key: value for key, value in {
        "tool_name": pending.get("tool_name"),
        "command": pending.get("command"),
        "reason": pending.get("reason"),
        "risk": pending.get("risk"),
        "policy_source": pending.get("policy_source"),
        "policy_explanation": pending.get("policy_explanation"),
        "control_descriptor": pending.get("control_descriptor"),
    }.items() if value is not None}
    if isinstance(pending.get("change_preview"), dict):
        metadata["change_preview"] = dict(pending["change_preview"])
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
            capabilities=capabilities,
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
    auto_resumed: list[str] = []
    if use_reusable_scope:
        auto_resumed = permission_resume_registry.resolve_compatible_session_tool_actions(
            session_id=session_id,
            target_kind=target_kind,
            target=target,
            capabilities=capabilities,
            decision={"type": "approve", "grant_id": grant["id"]},
            grant_bindings=(
                dict(pending["grant_bindings"])
                if isinstance(pending.get("grant_bindings"), dict)
                else None
            ),
            exclude_request_id=req.permission_request_id,
        )
    return {
        "session_id": session_id,
        "grant": grant,
        "resumed": resumed,
        "auto_resumed_permission_request_ids": auto_resumed,
    }


@router.post("/sessions/{session_id}/permissions/{grant_id}/revoke")
async def revoke_permission(session_id: str, grant_id: str) -> dict[str, Any]:
    """Revoke an active session permission grant."""

    if not session_manager.revoke_permission_grant(session_id, grant_id):
        raise HTTPException(status_code=404, detail="Permission grant not found")
    return {"session_id": session_id, "grant_id": grant_id, "revoked": True}
