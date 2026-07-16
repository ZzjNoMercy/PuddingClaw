"""Session CRUD API — list / create / rename / delete / raw messages / generate title."""

import uuid
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from starlette.concurrency import run_in_threadpool

from config import get_rag_mode
from graph.prompt_builder import build_system_prompt
from graph.session_manager import session_manager

router = APIRouter()

BASE_DIR = Path(__file__).resolve().parent.parent


# ── Request models ──────────────────────────────────────────

class RenameRequest(BaseModel):
    title: str


class SessionAnalyticsModelRequest(BaseModel):
    analytics_model_id: str | None = None


# ── Endpoints ───────────────────────────────────────────────

@router.get("/sessions")
async def list_sessions():
    """List all sessions with title and metadata."""
    sessions = await run_in_threadpool(session_manager.list_sessions)
    return {"sessions": sessions}


@router.post("/sessions")
async def create_session():
    """Create a new empty session."""
    session_id = f"session-{uuid.uuid4().hex[:12]}"
    meta = session_manager.create_session(session_id)
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
    meta = session_manager.update_metadata(
        session_id,
        {"analytics_model_id": req.analytics_model_id},
    )
    return meta


@router.delete("/sessions/{session_id}")
async def delete_session(session_id: str):
    """Delete a session."""
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
    if "graph" in data:
        result["graph"] = data["graph"]
    return result


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
    """Get conversation history for display (no system prompt, includes tool_calls)."""
    messages = await run_in_threadpool(session_manager.load_session, session_id)
    return {"session_id": session_id, "messages": messages}


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
        title = result.content.strip().strip('"\'""''')[:20]

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
