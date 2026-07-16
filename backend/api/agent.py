"""POST /api/agent — SSE streaming Agent mode backed by DeepAgents."""

from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel, Field
from sse_starlette.sse import EventSourceResponse

from graph.deepagents_manager import deepagents_agent_manager
from graph.session_manager import session_manager

router = APIRouter()


class AgentRequest(BaseModel):
    message: str
    session_id: str = "default"
    user_id: str = "default_user"
    project_id: str | None = None
    analytics_model_id: str | None = None
    attachments: list[dict] = Field(default_factory=list)
    stream: bool = True


@router.post("/agent")
async def agent(request: AgentRequest):
    persisted_user_message = False
    if request.stream:
        session_manager.update_metadata(request.session_id, {"runtime_mode": "agent"})
        session_manager.save_message(
            request.session_id,
            "user",
            deepagents_agent_manager._display_message_with_attachments(
                request.message,
                request.attachments,
            ),
        )
        persisted_user_message = True

    if request.stream:
        return EventSourceResponse(
            deepagents_agent_manager.astream(
                message=request.message,
                session_id=request.session_id,
                project_id=request.project_id,
                analytics_model_id=request.analytics_model_id,
                user_id=request.user_id,
                attachments=request.attachments,
                user_message_already_persisted=persisted_user_message,
            )
        )

    # Non-streaming fallback: consume the event stream and return the final content.
    final_content = ""
    async for event in deepagents_agent_manager.astream(
        message=request.message,
        session_id=request.session_id,
        project_id=request.project_id,
        analytics_model_id=request.analytics_model_id,
        user_id=request.user_id,
        attachments=request.attachments,
    ):
        if event.get("event") == "done":
            import json

            final_content = json.loads(event.get("data", "{}")).get("content", "")
    return {"reply": final_content, "session_id": request.session_id, "project_id": request.project_id}


@router.get("/agent/tool-context/status/{session_id}")
async def tool_context_status(session_id: str):
    """Return the persisted status of the silent Tool Context background job."""

    return session_manager.get_tool_context_status(session_id)
