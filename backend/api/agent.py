"""POST /api/agent — SSE streaming Agent mode backed by DeepAgents."""

from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel
from sse_starlette.sse import EventSourceResponse

from graph.deepagents_manager import deepagents_agent_manager

router = APIRouter()


class AgentRequest(BaseModel):
    message: str
    session_id: str = "default"
    user_id: str = "default_user"
    project_id: str | None = None
    stream: bool = True


@router.post("/agent")
async def agent(request: AgentRequest):
    if request.stream:
        return EventSourceResponse(
            deepagents_agent_manager.astream(
                message=request.message,
                session_id=request.session_id,
                project_id=request.project_id,
                user_id=request.user_id,
            )
        )

    # Non-streaming fallback: consume the event stream and return the final content.
    final_content = ""
    async for event in deepagents_agent_manager.astream(
        message=request.message,
        session_id=request.session_id,
        project_id=request.project_id,
        user_id=request.user_id,
    ):
        if event.get("event") == "done":
            import json

            final_content = json.loads(event.get("data", "{}")).get("content", "")
    return {"reply": final_content, "session_id": request.session_id, "project_id": request.project_id}

