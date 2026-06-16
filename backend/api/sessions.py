"""Session CRUD API — list / create / rename / delete / raw messages / generate title."""

import os
import uuid
from pathlib import Path

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from graph.session_manager import session_manager
from graph.prompt_builder import build_system_prompt
from config import get_rag_mode, get_llm_config

router = APIRouter()

BASE_DIR = Path(__file__).resolve().parent.parent


# ── Request models ──────────────────────────────────────────

class RenameRequest(BaseModel):
    title: str


# ── Endpoints ───────────────────────────────────────────────

@router.get("/sessions")
async def list_sessions():
    """List all sessions with title and metadata."""
    sessions = session_manager.list_sessions()
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


@router.delete("/sessions/{session_id}")
async def delete_session(session_id: str):
    """Delete a session."""
    session_manager.delete_session(session_id)
    return {"status": "deleted", "id": session_id}


@router.get("/sessions/{session_id}/messages")
async def get_raw_messages(session_id: str):
    """Get complete raw messages including system prompt."""
    data = session_manager.get_raw_messages(session_id)
    system_prompt = build_system_prompt(BASE_DIR, rag_mode=get_rag_mode())
    # Prepend system prompt as the first message
    all_messages = [{"role": "system", "content": system_prompt}] + data.get("messages", [])
    return {"session_id": session_id, "title": data.get("title", ""), "messages": all_messages}


@router.get("/sessions/{session_id}/history")
async def get_session_history(session_id: str):
    """Get conversation history for display (no system prompt, includes tool_calls)."""
    messages = session_manager.load_session(session_id)
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
        from langchain_deepseek import ChatDeepSeek
        from langchain_core.messages import HumanMessage as HM

        cfg = get_llm_config()
        llm = ChatDeepSeek(
            model=cfg["model"],
            api_key=cfg["api_key"],
            base_url=cfg["base_url"],
            temperature=0.3,
        )

        prompt = (
            f"根据以下对话内容，生成一个不超过10个字的中文标题，只输出标题文本，不要加引号或标点。\n\n"
            f"用户: {first_user}\n"
            f"助手: {first_assistant}"
        )

        result = await llm.ainvoke([HM(content=prompt)])
        title = result.content.strip().strip('"\'""''')[:20]

        session_manager.update_title(session_id, title)
        return {"session_id": session_id, "title": title}

    except Exception as e:
        # Fallback: use first few chars of user message
        fallback_title = first_user[:10].strip()
        session_manager.update_title(session_id, fallback_title)
        return {"session_id": session_id, "title": fallback_title}


@router.post("/sessions/{session_id}/clear")
async def clear_session_messages(session_id: str):
    """Clear all messages in a session (like Claude Code /clear)."""
    session_manager.clear_messages(session_id)
    return {"status": "cleared", "session_id": session_id}
