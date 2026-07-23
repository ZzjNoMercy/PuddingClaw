"""POST /api/agent — SSE streaming Agent mode backed by DeepAgents."""

from __future__ import annotations

import json
import logging
import time
import uuid
from collections.abc import AsyncIterator
from typing import Any, Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field, model_validator
from sse_starlette.sse import EventSourceResponse

from graph.deepagents_manager import deepagents_agent_manager
from graph.session_manager import session_manager

router = APIRouter()
logger = logging.getLogger(__name__)
# Agent TTFT is an operational metric. Keep it visible even when the process
# root logger intentionally filters ordinary application INFO messages.
logger.setLevel(logging.INFO)


def _parse_event_payload(event: dict[str, str]) -> dict[str, Any]:
    raw = event.get("data")
    if not isinstance(raw, str) or not raw:
        return {}
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


async def _instrument_agent_stream(
    stream: AsyncIterator[dict[str, str]],
    *,
    request_id: str,
    session_id: str,
    request_started_at: float,
) -> AsyncIterator[dict[str, str]]:
    """Log request-to-first-output latency without changing the SSE contract."""

    first_stream_event_ms: float | None = None
    first_agent_text_ms: float | None = None
    first_tool_start_ms: float | None = None
    query_id = ""
    run_id = ""
    completed = False

    try:
        async for event in stream:
            elapsed_ms = round((time.perf_counter() - request_started_at) * 1000, 2)
            event_name = str(event.get("event") or "")
            payload = _parse_event_payload(event)
            query_id = str(payload.get("query_id") or query_id)
            run_id = str(payload.get("run_id") or run_id)

            if first_stream_event_ms is None:
                first_stream_event_ms = elapsed_ms
                logger.info(
                    "[agent-latency] metric=first_stream_event request_id=%s session=%s "
                    "query=%s run=%s event=%s elapsed_ms=%.2f",
                    request_id,
                    session_id,
                    query_id or "<pending>",
                    run_id or "<pending>",
                    event_name or "<unknown>",
                    elapsed_ms,
                )

            if first_agent_text_ms is None and event_name in {"reasoning", "token"}:
                content = payload.get("content")
                if isinstance(content, str) and content:
                    first_agent_text_ms = elapsed_ms
                    logger.info(
                        "[agent-latency] metric=first_agent_text request_id=%s session=%s "
                        "query=%s run=%s kind=%s elapsed_ms=%.2f chars=%d",
                        request_id,
                        session_id,
                        query_id or "<pending>",
                        run_id or "<pending>",
                        event_name,
                        elapsed_ms,
                        len(content),
                    )

            if first_tool_start_ms is None and event_name == "tool_start":
                first_tool_start_ms = elapsed_ms
                logger.info(
                    "[agent-latency] metric=first_tool_start request_id=%s session=%s "
                    "query=%s run=%s tool=%s elapsed_ms=%.2f",
                    request_id,
                    session_id,
                    query_id or "<pending>",
                    run_id or "<pending>",
                    str(payload.get("tool") or "<unknown>"),
                    elapsed_ms,
                )

            if event_name == "done":
                completed = True
            yield event
    finally:
        total_ms = round((time.perf_counter() - request_started_at) * 1000, 2)
        logger.info(
            "[agent-latency] metric=stream_finished request_id=%s session=%s query=%s run=%s "
            "completed=%s total_ms=%.2f first_stream_event_ms=%s first_agent_text_ms=%s "
            "first_tool_start_ms=%s",
            request_id,
            session_id,
            query_id or "<pending>",
            run_id or "<pending>",
            completed,
            total_ms,
            first_stream_event_ms,
            first_agent_text_ms,
            first_tool_start_ms,
        )


class AgentRequest(BaseModel):
    message: str
    session_id: str = "default"
    user_id: str = "default_user"
    project_id: str | None = None
    analytics_model_id: str | None = None
    attachments: list[dict] = Field(default_factory=list)
    goal_mode: bool = False
    goal_id: str | None = None
    context_goal_id: str | None = None
    goal_control_action: Literal["start"] | None = None
    stream: bool = True

    @model_validator(mode="after")
    def validate_goal_activation(self):
        if self.goal_id and not self.goal_mode:
            raise ValueError("goal_id requires goal_mode=true")
        if self.goal_id and self.context_goal_id and self.goal_id != self.context_goal_id:
            raise ValueError("goal_id and context_goal_id must reference the same Goal")
        if self.goal_control_action == "start":
            if not self.goal_mode or not self.goal_id:
                raise ValueError("goal_control_action=start requires goal_mode=true and goal_id")
            if self.attachments:
                raise ValueError("Goal control actions do not accept attachments")
        return self


@router.post("/agent")
async def agent(request: AgentRequest):
    request_started_at = time.perf_counter()
    request_id = f"agentreq-{uuid.uuid4().hex[:12]}"
    logger.info(
        "[agent-latency] metric=request_received request_id=%s session=%s stream=%s "
        "message_chars=%d attachments=%d",
        request_id,
        request.session_id,
        request.stream,
        len(request.message),
        len(request.attachments),
    )
    persisted_user_message = False
    if request.stream and request.goal_control_action is None:
        try:
            session_manager.update_metadata(request.session_id, {"runtime_mode": "agent"})
            session_manager.save_message(
                request.session_id,
                "user",
                deepagents_agent_manager._display_message_with_attachments(
                    request.message,
                    request.attachments,
                ),
                attachments=request.attachments,
            )
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail="Session not found") from exc
        persisted_user_message = True
    elif request.goal_control_action is not None:
        # Product controls are audited by the Run/Goal ledger. They are not
        # synthetic user chat messages and must not pollute the transcript.
        try:
            session_manager.update_metadata(request.session_id, {"runtime_mode": "agent"})
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail="Session not found") from exc
        persisted_user_message = True

    if request.stream:
        stream = deepagents_agent_manager.astream(
            message=request.message,
            session_id=request.session_id,
            project_id=request.project_id,
            analytics_model_id=request.analytics_model_id,
            user_id=request.user_id,
            attachments=request.attachments,
            user_message_already_persisted=persisted_user_message,
            goal_mode=request.goal_mode,
            goal_id=request.goal_id,
            context_goal_id=request.context_goal_id,
            goal_control_action=request.goal_control_action,
        )
        return EventSourceResponse(
            _instrument_agent_stream(
                stream,
                request_id=request_id,
                session_id=request.session_id,
                request_started_at=request_started_at,
            )
        )

    # Non-streaming fallback: consume the event stream and return the final content.
    final_content = ""
    run_outcome: dict = {}
    stream = deepagents_agent_manager.astream(
        message=request.message,
        session_id=request.session_id,
        project_id=request.project_id,
        analytics_model_id=request.analytics_model_id,
        user_id=request.user_id,
        attachments=request.attachments,
        user_message_already_persisted=request.goal_control_action is not None,
        goal_mode=request.goal_mode,
        goal_id=request.goal_id,
        context_goal_id=request.context_goal_id,
        goal_control_action=request.goal_control_action,
    )
    async for event in _instrument_agent_stream(
        stream,
        request_id=request_id,
        session_id=request.session_id,
        request_started_at=request_started_at,
    ):
        if event.get("event") == "run_outcome":
            run_outcome = json.loads(event.get("data", "{}"))
        if event.get("event") == "done":
            final_content = json.loads(event.get("data", "{}")).get("content", "")
    return {
        "reply": final_content,
        "session_id": request.session_id,
        "project_id": request.project_id,
        **run_outcome,
    }


@router.get("/agent/tool-context/status/{session_id}")
async def tool_context_status(session_id: str):
    """Return the persisted status of the silent Tool Context background job."""

    return session_manager.get_tool_context_status(session_id)
