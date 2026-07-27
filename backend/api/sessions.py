"""Session CRUD API — list / create / rename / delete / raw messages / generate title."""

import uuid
from pathlib import Path
from typing import Any, Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

from config import get_rag_mode
from graph.deepagents_manager import deepagents_agent_manager
from graph.permission_resume import permission_resume_registry
from graph.prompt_builder import build_system_prompt
from graph.session_manager import session_manager
from graph.user_input_resume import user_input_resume_registry
from harness.coordinators import GoalActivationError, GoalCoordinator
from harness.models import HarnessStateError
from services.skill_management import get_skill_management_service

router = APIRouter()

BASE_DIR = Path(__file__).resolve().parent.parent
goal_coordinator = GoalCoordinator(session_manager)


# ── Request models ──────────────────────────────────────────


class RenameRequest(BaseModel):
    title: str


class SessionAnalyticsModelRequest(BaseModel):
    analytics_model_id: str | None = None


class SessionCreateRequest(BaseModel):
    analytics_model_id: str | None = None
    approval_mode: Literal["strict", "smart"] = "strict"
    runtime_mode: Literal["chat", "agent"] = "chat"
    project_id: str | None = None


class GoalUpdateRequest(BaseModel):
    objective: str
    expected_revision: int


class GoalBudgetExtensionRequest(BaseModel):
    additional_rounds: int = Field(ge=1, le=100)


# ── Endpoints ───────────────────────────────────────────────


@router.get("/sessions")
async def list_sessions():
    """List all sessions with title and metadata."""
    sessions = await run_in_threadpool(session_manager.list_sessions)
    return {"sessions": sessions}


@router.post("/sessions")
async def create_session(req: SessionCreateRequest | None = None):
    """Create a new empty session."""
    session_id = f"session-{uuid.uuid4().hex[:12]}"
    payload = req or SessionCreateRequest()
    try:
        # Stamp the caller's UI context (runtime mode / project) at creation
        # time so the session lands in the correct sidebar grouping
        # immediately, instead of only after the first Run flips them.
        metadata: dict[str, Any] = {
            "analytics_model_id": payload.analytics_model_id,
            "runtime_mode": payload.runtime_mode,
        }
        if payload.project_id:
            metadata["project_id"] = payload.project_id
        meta = session_manager.create_session(
            session_id,
            metadata=metadata,
            approval_mode=payload.approval_mode,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return meta


@router.put("/sessions/{session_id}")
async def rename_session(session_id: str, req: RenameRequest):
    """Rename an existing session."""
    try:
        session_manager.rename_session(session_id, req.title)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Session not found")
    return {"id": session_id, "title": req.title}


@router.patch("/sessions/{session_id}/analytics-model")
async def update_session_analytics_model(
    session_id: str,
    req: SessionAnalyticsModelRequest,
):
    """Persist or clear the analytics model selected for one session."""
    try:
        meta = session_manager.update_metadata(
            session_id,
            {"analytics_model_id": req.analytics_model_id},
        )
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Session not found") from exc
    return meta


@router.delete("/sessions/{session_id}")
async def delete_session(session_id: str):
    """Delete a session."""
    permission_resume_registry.reject_session(session_id, "Session was deleted.")
    user_input_resume_registry.reject_session(session_id, "Session was deleted.")
    get_skill_management_service(BASE_DIR).delete_session_plans(session_id)
    session_manager.delete_session(session_id)
    return {"status": "deleted", "id": session_id}


@router.get("/sessions/{session_id}/messages")
async def get_raw_messages(session_id: str):
    """Get raw conversation messages without reading execution traces."""
    data = await run_in_threadpool(session_manager.get_raw_messages, session_id)
    system_prompt = build_system_prompt(BASE_DIR, rag_mode=get_rag_mode())
    # Prepend system prompt as the first message
    all_messages = [{"role": "system", "content": system_prompt}] + data.get("messages", [])
    result: dict[str, Any] = {
        "session_id": session_id,
        "title": data.get("title", ""),
        "messages": all_messages,
    }
    if "todos" in data:
        result["todos"] = data["todos"]
    if "todos_authority" in data:
        result["todos_authority"] = data["todos_authority"]
    if "todo_ledger_revision" in data:
        result["todo_ledger_revision"] = data["todo_ledger_revision"]
    if "graph" in data:
        result["graph"] = data["graph"]
    if "harness" in data:
        result["harness"] = data["harness"]
    return result


@router.get("/sessions/{session_id}/harness")
async def get_session_harness_state(session_id: str):
    """Return lightweight Run/Goal product state without reading Trace."""

    harness = await run_in_threadpool(session_manager.get_harness_state, session_id)
    legacy_audit = await run_in_threadpool(
        session_manager.audit_legacy_external_leases,
        session_id,
        migrate=True,
    )
    return {
        "session_id": session_id,
        **harness,
        "legacy_external_lease_audit": legacy_audit,
    }


@router.get("/sessions/{session_id}/todos/current")
async def get_current_session_todos(session_id: str):
    """Return the authoritative lightweight Todo projection for reconciliation."""

    snapshot = await run_in_threadpool(session_manager.get_todo_snapshot, session_id)
    return {"session_id": session_id, **snapshot}


@router.get("/sessions/{session_id}/goals/{goal_id}")
async def get_session_goal(session_id: str, goal_id: str):
    goal = await run_in_threadpool(
        session_manager.get_goal_state,
        session_id,
        goal_id,
    )
    if goal is None:
        raise HTTPException(status_code=404, detail="Goal not found")
    return goal


@router.patch("/sessions/{session_id}/goals/{goal_id}")
async def update_session_goal(
    session_id: str,
    goal_id: str,
    request: GoalUpdateRequest,
):
    try:
        goal = await run_in_threadpool(
            goal_coordinator.update_objective,
            session_id,
            goal_id,
            objective=request.objective,
            expected_revision=request.expected_revision,
        )
        return goal.model_dump(mode="json")
    except GoalActivationError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except HarnessStateError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


async def _transition_goal(session_id: str, goal_id: str, action: str):
    try:
        method = getattr(goal_coordinator, action)
        goal = await run_in_threadpool(method, session_id, goal_id)
        if action in {"pause", "cancel"} and goal.requested_status is not None:
            deepagents_agent_manager.cancel_active_goal_run(session_id, goal_id)
        return goal.model_dump(mode="json")
    except GoalActivationError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except HarnessStateError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/sessions/{session_id}/goals/{goal_id}/pause")
async def pause_session_goal(session_id: str, goal_id: str):
    return await _transition_goal(session_id, goal_id, "pause")


@router.post("/sessions/{session_id}/goals/{goal_id}/resume")
async def resume_session_goal(session_id: str, goal_id: str):
    return await _transition_goal(session_id, goal_id, "resume")


@router.post("/sessions/{session_id}/goals/{goal_id}/cancel")
async def cancel_session_goal(session_id: str, goal_id: str):
    return await _transition_goal(session_id, goal_id, "cancel")


@router.post("/sessions/{session_id}/goals/{goal_id}/extend-budget")
async def extend_session_goal_budget(
    session_id: str,
    goal_id: str,
    request: GoalBudgetExtensionRequest,
):
    try:
        goal = await run_in_threadpool(
            goal_coordinator.extend_budget,
            session_id,
            goal_id,
            additional_rounds=request.additional_rounds,
        )
        return goal.model_dump(mode="json")
    except GoalActivationError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except HarnessStateError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.get("/sessions/{session_id}/traces")
async def get_session_traces(session_id: str):
    """Load heavyweight Agent traces only when the trace workspace requests them."""
    data = await run_in_threadpool(session_manager.get_trace_state, session_id)
    latest_query_id = data.get("latest_query_id")
    traces = data.get("traces") or {}
    latest = traces.get(latest_query_id) if isinstance(latest_query_id, str) else None
    return {
        "session_id": session_id,
        "trace": latest,
        **data,
    }


@router.get("/sessions/{session_id}/history")
async def get_session_history(session_id: str):
    """Get chat history plus lightweight durable Agent UI state.

    Todo/graph recovery must not depend on opening the heavyweight Trace view.
    Both fields live in Session JSON and are therefore returned with the normal
    conversation reload path.
    """

    data = await run_in_threadpool(session_manager.get_raw_messages, session_id)
    result: dict[str, Any] = {
        "session_id": session_id,
        "messages": data.get("messages", []),
    }
    if "todos" in data:
        result["todos"] = data["todos"]
    if "todos_authority" in data:
        result["todos_authority"] = data["todos_authority"]
    if "todo_ledger_revision" in data:
        result["todo_ledger_revision"] = data["todo_ledger_revision"]
    if "graph" in data:
        result["graph"] = data["graph"]
    return result


@router.post("/sessions/{session_id}/generate-title")
async def generate_title(session_id: str):
    """Use DeepSeek to generate a short title from the first conversation turn."""
    messages = session_manager.load_session(session_id)
    if not messages:
        raise HTTPException(status_code=400, detail="No messages to generate title from")

    # Get the first user message and first assistant reply
    first_user = ""
    first_assistant = ""
    for msg in messages:
        if msg["role"] == "user" and not first_user:
            first_user = msg["content"][:200]
        elif msg["role"] == "assistant" and not first_assistant:
            first_assistant = msg["content"][:200]
        if first_user and first_assistant:
            break

    if not first_user:
        raise HTTPException(status_code=400, detail="No user message found")

    try:
        from langchain_core.messages import HumanMessage as HM

        from llm.model_client import ModelClient

        llm = ModelClient(role="title", temperature=0.3)

        prompt = (
            f"根据以下对话内容，生成一个不超过10个字的中文标题，只输出标题文本，不要加引号或标点。\n\n"
            f"用户: {first_user}\n"
            f"助手: {first_assistant}"
        )

        result = await llm.ainvoke([HM(content=prompt)])
        title = result.content.strip().strip('"\'""')[:20]

        session_manager.update_title(session_id, title)
        return {"session_id": session_id, "title": title}

    except Exception:
        # Fallback: use first few chars of user message
        fallback_title = first_user[:10].strip()
        session_manager.update_title(session_id, fallback_title)
        return {"session_id": session_id, "title": fallback_title}


@router.post("/sessions/{session_id}/clear")
async def clear_session_messages(session_id: str):
    """Clear all messages in a session (like Claude Code /clear)."""
    session_manager.clear_messages(session_id)
    return {"status": "cleared", "session_id": session_id}
