"""Resolve generic structured user-input HITL requests."""

from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from graph.session_manager import session_manager
from graph.user_input_resume import user_input_resume_registry


router = APIRouter(
    prefix="/sessions/{session_id}/user-input-requests",
    tags=["sessions"],
)


class UserInputAnswer(BaseModel):
    question_id: str
    option_ids: list[str] = Field(default_factory=list)
    text: str = ""


class ResolveUserInputRequest(BaseModel):
    request_version: int = Field(ge=1)
    action: Literal["submit", "cancel", "agent_decide"]
    answers: list[UserInputAnswer] = Field(default_factory=list)


@router.get("/{request_id}")
async def get_user_input_request(session_id: str, request_id: str) -> dict[str, Any]:
    request = user_input_resume_registry.get(request_id)
    if request is None or request.get("session_id") != session_id:
        raise HTTPException(status_code=404, detail="User input request not found")
    return {"request": request}


@router.post("/{request_id}/resolve")
async def resolve_user_input_request(
    session_id: str,
    request_id: str,
    body: ResolveUserInputRequest,
) -> dict[str, Any]:
    request = user_input_resume_registry.get(request_id)
    if request is None or request.get("session_id") != session_id:
        raise HTTPException(status_code=404, detail="User input request not found")
    if int(request.get("version") or 0) != body.request_version:
        raise HTTPException(status_code=409, detail="User input request version is stale")

    # A lost HTTP response must be safely retryable even if the owning Run or
    # Goal has already completed. The registry compares canonical answers and
    # rejects a conflicting second decision.
    if request.get("status") == "resolved":
        try:
            decision, _ = user_input_resume_registry.resolve(
                request_id,
                body.model_dump(mode="json", exclude={"request_version"}),
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return {
            "session_id": session_id,
            "request_id": request_id,
            "decision": decision,
            "resumed": False,
        }

    run_id = str(request.get("run_id") or "")
    run = session_manager.get_run_state(session_id, run_id)
    if not isinstance(run, dict) or str(run.get("status") or "") != "waiting_hitl":
        # Preserve idempotent retries after a successful resolution.
        if request.get("status") != "resolved":
            raise HTTPException(status_code=409, detail="Owning Run is no longer waiting for input")
    goal_id = str(request.get("goal_id") or "")
    if goal_id:
        goal = session_manager.get_goal_state(session_id, goal_id)
        if not isinstance(goal, dict):
            raise HTTPException(status_code=409, detail="Owning Goal no longer exists")
        if (
            str(goal.get("status") or "") != "active"
            or bool(goal.get("requested_status"))
            or goal.get("current_run_id") != run_id
            or int(goal.get("objective_revision") or 1)
            != int(request.get("goal_revision") or 1)
        ):
            raise HTTPException(status_code=409, detail="User input request belongs to a stale Goal Run")

    try:
        decision, resumed = user_input_resume_registry.resolve(
            request_id,
            body.model_dump(mode="json", exclude={"request_version"}),
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if decision is None:
        raise HTTPException(status_code=404, detail="User input request not found")
    return {
        "session_id": session_id,
        "request_id": request_id,
        "decision": decision,
        "resumed": resumed,
    }
