"""Securely configure an environment Secret without exposing it to the Agent."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field, SecretStr

from graph.session_manager import session_manager
from graph.skill_secret_resume import skill_secret_resume_registry
from runtime_identity.paths import PuddingClawPaths, trusted_owner_user_id
from runtime_identity.skill_secrets import SkillSecretStore
from runtime_identity.software_runtime import skill_content_version
from tools.skills_scanner import resolve_effective_skill_root

router = APIRouter(
    prefix="/sessions/{session_id}/skill-secret-requests",
    tags=["sessions"],
)

_PACKAGE_ROOT = Path(__file__).resolve().parents[1]


class ResolveSkillSecretRequest(BaseModel):
    request_version: int = Field(ge=1)
    action: Literal["configure", "reuse", "cancel"]
    secret_value: SecretStr | None = None


def _request(session_id: str, request_id: str) -> dict[str, Any]:
    request = skill_secret_resume_registry.get(request_id)
    if request is None or request.get("session_id") != session_id:
        raise HTTPException(status_code=404, detail="Skill Secret request not found")
    return request


def _validate_live_request(session_id: str, request: dict[str, Any]) -> None:
    run_id = str(request.get("run_id") or "")
    run = session_manager.get_run_state(session_id, run_id)
    if not isinstance(run, dict) or str(run.get("status") or "") != "waiting_hitl":
        raise HTTPException(status_code=409, detail="Owning Run is no longer waiting for input")
    goal_id = str(request.get("goal_id") or "")
    if goal_id:
        goal = session_manager.get_goal_state(session_id, goal_id)
        if (
            not isinstance(goal, dict)
            or str(goal.get("status") or "") != "active"
            or bool(goal.get("requested_status"))
            or goal.get("current_run_id") != run_id
            or int(goal.get("objective_revision") or 1)
            != int(request.get("goal_revision") or 1)
        ):
            raise HTTPException(status_code=409, detail="Skill Secret request belongs to a stale Goal Run")


def _current_skill_version(skill_id: str) -> str:
    paths = PuddingClawPaths.from_environment()
    root = resolve_effective_skill_root(_PACKAGE_ROOT, paths.user_skills(), skill_id)
    if root is None or root.is_symlink() or not (root / "SKILL.md").is_file():
        raise ValueError("Skill is not installed")
    return skill_content_version(root)


@router.get("/{request_id}")
async def get_skill_secret_request(session_id: str, request_id: str) -> dict[str, Any]:
    return {"request": _request(session_id, request_id)}


@router.post("/{request_id}/resolve")
async def resolve_skill_secret_request(
    session_id: str,
    request_id: str,
    body: ResolveSkillSecretRequest,
) -> dict[str, Any]:
    request = _request(session_id, request_id)
    if int(request.get("version") or 0) != body.request_version:
        raise HTTPException(status_code=409, detail="Skill Secret request version is stale")

    # A retry after a lost HTTP response never needs the Secret again and must
    # not perform another credential write.
    if request.get("status") == "resolved":
        decision = request.get("decision")
        if not isinstance(decision, dict):
            raise HTTPException(status_code=409, detail="Skill Secret request has an invalid result")
        requested_action = "cancel" if body.action == "cancel" else "configured"
        if decision.get("action") != requested_action:
            raise HTTPException(status_code=409, detail="Skill Secret request was resolved differently")
        return {"request_id": request_id, "decision": decision, "resumed": False}

    _validate_live_request(session_id, request)
    skill_id = str(request.get("skill_id") or "")
    try:
        current_version = _current_skill_version(skill_id)
    except (FileNotFoundError, OSError, RuntimeError, ValueError) as exc:
        raise HTTPException(status_code=409, detail="Installed Skill changed or is unavailable") from exc
    if current_version != str(request.get("skill_version") or ""):
        raise HTTPException(status_code=409, detail="Installed Skill changed after the request")

    if body.action == "cancel":
        decision, resumed = skill_secret_resume_registry.resolve(
            request_id,
            {"action": "cancel"},
        )
    else:
        mode = str(request.get("mode") or "")
        if (body.action == "configure" and mode != "enter") or (
            body.action == "reuse" and mode != "reuse"
        ):
            raise HTTPException(status_code=409, detail="Skill Secret setup mode changed")
        store = SkillSecretStore(PuddingClawPaths.from_environment(), trusted_owner_user_id())
        try:
            if body.action == "configure":
                if body.secret_value is None:
                    raise ValueError("Secret value is required")
                store.set_and_bind(
                    skill_id=skill_id,
                    skill_version=current_version,
                    env_name=str(request.get("env_name") or ""),
                    secret_value=body.secret_value.get_secret_value(),
                )
            else:
                if body.secret_value is not None:
                    raise ValueError("Secret value must not be supplied when reusing a Secret")
                store.bind_existing(
                    skill_id=skill_id,
                    skill_version=current_version,
                    env_name=str(request.get("env_name") or ""),
                )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        decision, resumed = skill_secret_resume_registry.resolve(
            request_id,
            {"action": "configured"},
        )
    return {"request_id": request_id, "decision": decision, "resumed": resumed}


@router.delete("/bindings/{skill_id}/{env_name}")
async def revoke_skill_secret_binding(
    session_id: str,
    skill_id: str,
    env_name: str,
) -> dict[str, Any]:
    # session_id keeps this mutation on the authenticated desktop session API;
    # no Run needs to be live because this is a direct user settings action.
    try:
        session_manager.get_raw_messages(session_id)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Session not found") from exc
    try:
        _current_skill_version(skill_id)
        revision = SkillSecretStore(
            PuddingClawPaths.from_environment(),
            trusted_owner_user_id(),
        ).revoke_binding(skill_id=skill_id, env_name=env_name)
    except (OSError, RuntimeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"skill_id": skill_id, "env_name": env_name, "revision": revision, "revoked": True}
