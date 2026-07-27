"""Session-bound structured approval APIs for staged Skill plans."""

from __future__ import annotations

import hashlib
import json
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any, NoReturn

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

from graph.permission_resume import permission_resume_registry
from graph.session_manager import session_manager
from services.skill_management import (
    SkillManagementError,
    SkillManagementService,
    get_skill_management_service,
)

router = APIRouter()
BACKEND_ROOT = Path(__file__).resolve().parents[1]


class SkillPlanDecision(BaseModel):
    plan_sha256: str = Field(min_length=64, max_length=64)


_plan_locks: dict[str, threading.Lock] = {}
_plan_locks_guard = threading.Lock()
_ERROR_MESSAGES = {
    "plan_not_found": "Skill 计划不存在或已被清理。",
    "invalid_plan_id": "Skill 计划标识无效。",
    "plan_session_mismatch": "Skill 计划不属于当前 Session。",
    "plan_not_session_bound": "该旧版计划需要通过原授权流程处理。",
    "plan_digest_mismatch": "Skill 计划校验值不一致，请刷新后重试。",
    "plan_metadata_changed": "Skill 计划内容已发生变化，请重新准备。",
    "plan_expired": "Skill 计划已过期，请重新准备。",
    "plan_cancelled": "Skill 计划已取消，未修改 Skills 目录。",
    "plan_already_committed": "Skill 计划已经提交。",
    "plan_action_mismatch": "Skill 计划操作类型不一致。",
    "installed_skill_changed": "已安装 Skill 在计划准备后发生了变化，请重新准备。",
    "skill_already_exists": "目标 Skill 已存在，请改用更新流程。",
    "permission_consumption_failed": "本次确认未能绑定到安装操作，请重试。",
}


def _service() -> SkillManagementService:
    return get_skill_management_service(BACKEND_ROOT)


@contextmanager
def _plan_lock(plan_id: str) -> Iterator[None]:
    """Serialize decisions in-process and across workers sharing state_dir."""

    with _plan_locks_guard:
        process_lock = _plan_locks.setdefault(plan_id, threading.Lock())
    with process_lock:
        lock_dir = _service().state_dir / "locks"
        lock_dir.mkdir(parents=True, exist_ok=True)
        lock_name = hashlib.sha256(plan_id.encode("utf-8")).hexdigest() + ".lock"
        with (lock_dir / lock_name).open("a+b") as handle:
            try:
                import fcntl
            except ImportError:  # pragma: no cover - Windows fallback
                fcntl = None  # type: ignore[assignment]
            if fcntl is not None:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                if fcntl is not None:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _raise_http(error: SkillManagementError) -> NoReturn:
    if error.code in {"plan_not_found", "invalid_plan_id", "plan_session_mismatch"}:
        status = 404
    elif error.code in {"plan_digest_mismatch", "plan_metadata_changed"}:
        status = 400
    else:
        status = 409
    message = error.message
    if not message or message == error.code:
        message = _ERROR_MESSAGES.get(error.code, "Skill 计划状态已变化，请刷新后重试。")
    raise HTTPException(
        status_code=status,
        detail={"code": error.code, "message": message},
    ) from error


def _read_plan(session_id: str, plan_id: str) -> dict[str, Any]:
    try:
        session_manager.get_permission_policy(session_id)
    except FileNotFoundError as error:
        raise HTTPException(status_code=404, detail="Session not found") from error
    try:
        return _service().preview_for_session(plan_id, session_id)
    except SkillManagementError as error:
        _raise_http(error)


def _change_preview(plan: dict[str, Any]) -> dict[str, str]:
    diff = plan.get("diff") if isinstance(plan.get("diff"), dict) else {}
    metadata = plan.get("staged_metadata") if isinstance(plan.get("staged_metadata"), dict) else {}
    preview = {
        "action": str(plan.get("action") or ""),
        "skill_name": str(plan.get("skill_name") or ""),
        "source": str(plan.get("source") or ""),
        "version": str(metadata.get("version") or ""),
        "changes": str(diff.get("summary") or ""),
        "plan_sha256": str(plan.get("plan_sha256") or ""),
    }
    return {key: value for key, value in preview.items() if value}


@router.get("/sessions/{session_id}/skill-plans/{plan_id}")
async def get_skill_plan(session_id: str, plan_id: str) -> dict[str, Any]:
    plan = await run_in_threadpool(_read_plan, session_id, plan_id)
    return {"session_id": session_id, "plan": plan}


@router.post("/sessions/{session_id}/skill-plans/{plan_id}/commit")
async def commit_skill_plan(
    session_id: str,
    plan_id: str,
    req: SkillPlanDecision,
) -> dict[str, Any]:
    """Consume one explicit UI approval and atomically commit the staged plan."""

    return await run_in_threadpool(_commit_skill_plan_sync, session_id, plan_id, req)


def _commit_skill_plan_sync(
    session_id: str,
    plan_id: str,
    req: SkillPlanDecision,
) -> dict[str, Any]:

    with _plan_lock(plan_id):
        plan = _read_plan(session_id, plan_id)
        if plan.get("plan_sha256") != req.plan_sha256:
            _raise_http(SkillManagementError("plan_digest_mismatch"))
        if plan.get("status") == "committed":
            return {
                "session_id": session_id,
                "plan": plan,
                "idempotent": True,
                "permission_recorded": False,
            }
        if plan.get("status") != "prepared":
            _raise_http(SkillManagementError(f"plan_{plan.get('status') or 'not_prepared'}"))

        action = str(plan.get("action") or "")
        if action not in {"install", "update"}:
            _raise_http(SkillManagementError("plan_action_mismatch"))
        tool_name = "install_skill" if action == "install" else "update_skill"
        command = json.dumps(
            {
                "action": action,
                "plan_id": plan_id,
                "plan_sha256": req.plan_sha256,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        reason = f"managed_skill_write:{tool_name}"
        fingerprint = permission_resume_registry.tool_action_fingerprint(
            tool_name=tool_name,
            command=command,
            reason=reason,
        )
        try:
            grant = session_manager.add_permission_grant(
                session_id,
                grant_type="tool_action",
                target_kind="fingerprint",
                target=fingerprint,
                capabilities=["execute", "managed_skill_write"],
                scope="once",
                source="user",
                metadata={
                    "tool_name": tool_name,
                    "command": command,
                    "reason": reason,
                    "risk": "managed_skill_write",
                    "policy_source": "structured_skill_plan",
                    "policy_explanation": "User confirmed the immutable staged Skill plan in the plan card.",
                    "change_preview": _change_preview(plan),
                },
                consume_immediately=True,
            )
            result = _service().commit(
                action=action,  # type: ignore[arg-type]
                plan_id=plan_id,
                plan_sha256=req.plan_sha256,
                expected_session_id=session_id,
            )
        except FileNotFoundError as error:
            raise HTTPException(status_code=404, detail="Session not found") from error
        except ValueError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        except SkillManagementError as error:
            _raise_http(error)

        return {
            "session_id": session_id,
            "plan": result,
            "idempotent": False,
            "permission_recorded": True,
            "permission_grant_id": grant.get("id"),
        }


@router.post("/sessions/{session_id}/skill-plans/{plan_id}/cancel")
async def cancel_skill_plan(
    session_id: str,
    plan_id: str,
    req: SkillPlanDecision,
) -> dict[str, Any]:
    return await run_in_threadpool(_cancel_skill_plan_sync, session_id, plan_id, req)


def _cancel_skill_plan_sync(
    session_id: str,
    plan_id: str,
    req: SkillPlanDecision,
) -> dict[str, Any]:
    with _plan_lock(plan_id):
        try:
            plan = _service().cancel(
                plan_id=plan_id,
                plan_sha256=req.plan_sha256,
                expected_session_id=session_id,
            )
        except SkillManagementError as error:
            _raise_http(error)
    return {"session_id": session_id, "plan": plan}
