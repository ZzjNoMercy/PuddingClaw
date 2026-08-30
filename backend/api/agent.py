"""POST /api/agent — SSE streaming Agent mode backed by DeepAgents."""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
import uuid
from collections.abc import AsyncIterator
from typing import Any, Literal, NoReturn

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field, model_validator
from sse_starlette.sse import EventSourceResponse

from config import get_fallback_llm_config, load_config
from graph.agent_context_compaction import (
    AgentContextCompactionError,
    agent_context_compaction_service,
)
from graph.deepagents_manager import deepagents_agent_manager
from graph.session_manager import session_manager

router = APIRouter()
logger = logging.getLogger(__name__)
# Agent TTFT is an operational metric. Keep it visible even when the process
# root logger intentionally filters ordinary application INFO messages.
logger.setLevel(logging.INFO)
_agent_compaction_tasks: dict[str, asyncio.Task[None]] = {}


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
    llm_model_id: str | None = None
    thinking_level: Literal["low", "high", "max"] | None = None
    credential_name: str | None = None
    attachments: list[dict] = Field(default_factory=list)
    skill_hints: list[str] | None = Field(default=None, max_length=8)
    goal_mode: bool = False
    goal_id: str | None = None
    context_goal_id: str | None = None
    goal_control_action: Literal["start"] | None = None
    run_review_policy: Literal["off", "shadow", "blocking_one_shot"] | None = None
    stream: bool = True

    @model_validator(mode="after")
    def validate_goal_activation(self):
        if self.skill_hints is not None:
            normalized = [str(item).strip() for item in self.skill_hints if str(item).strip()]
            if any(not re.fullmatch(r"[A-Za-z0-9][\w.-]{0,127}", item) for item in normalized):
                raise ValueError("skill_hints contains an invalid Skill id")
            if any(
                not re.search(
                    rf"(?<!\S)/{re.escape(item)}(?=$|[\s，,。.!！?？；;])",
                    self.message,
                    flags=re.IGNORECASE,
                )
                for item in normalized
            ):
                raise ValueError("skill_hints must reference a visible slash token in message")
            self.skill_hints = list(dict.fromkeys(normalized))
        if self.goal_id and not self.goal_mode:
            raise ValueError("goal_id requires goal_mode=true")
        if self.goal_id and self.context_goal_id and self.goal_id != self.context_goal_id:
            raise ValueError("goal_id and context_goal_id must reference the same Goal")
        if self.goal_control_action == "start":
            if not self.goal_mode or not self.goal_id:
                raise ValueError("goal_control_action=start requires goal_mode=true and goal_id")
            if self.attachments:
                raise ValueError("Goal control actions do not accept attachments")
        if self.goal_mode and self.run_review_policy not in {None, "off"}:
            raise ValueError("run_review_policy is available only for ordinary Runs")
        return self


class AgentCompactRequest(BaseModel):
    """Optional user emphasis for an Agent-only manual compaction."""

    focus: str = Field(default="", max_length=1000)


class RunReviewRequest(BaseModel):
    """Manual ordinary-Run review is intentionally option-free and idempotent."""

    pass


def _public_run_review_report(
    harness: dict[str, Any],
    report: dict[str, Any],
) -> dict[str, Any]:
    """Attach only the immutable records owned by this review attempt.

    The stored report remains compact.  The API view carries the criterion
    verdicts needed by the transcript UI without exposing the frozen prompt or
    transcript snapshot.
    """

    records = harness.get("verification_records")
    records_by_id = records if isinstance(records, dict) else {}
    report_view = dict(report)
    report_view["verification_records"] = [
        dict(record)
        for record_id in report.get("verification_record_ids") or []
        if isinstance((record := records_by_id.get(record_id)), dict)
    ]
    return report_view


@router.post("/agent/sessions/{session_id}/runs/{run_id}/review", status_code=202)
async def review_agent_run(
    session_id: str,
    run_id: str,
    _request: RunReviewRequest,
) -> dict[str, Any]:
    review_config = load_config().get("harness", {}).get("completion", {}).get("run_review", {})
    if not bool(review_config.get("manual_enabled", True)):
        raise HTTPException(status_code=403, detail="Manual Run review is disabled")
    try:
        result = await deepagents_agent_manager.begin_run_review(
            session_id,
            run_id,
            manual=True,
        )
        report = result.get("report") if isinstance(result, dict) else None
        if isinstance(report, dict):
            harness = session_manager.get_harness_state(session_id)
            public_report = _public_run_review_report(harness, report)
            return {
                **result,
                "status": str(public_report.get("status") or result.get("status") or "completed"),
                "report": public_report,
            }
        return result
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except (PermissionError, ValueError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.get("/agent/sessions/{session_id}/runs/{run_id}/review")
async def agent_run_review_status(session_id: str, run_id: str) -> dict[str, Any]:
    raw_run = session_manager.get_run_state(session_id, run_id)
    if not isinstance(raw_run, dict):
        raise HTTPException(status_code=404, detail=f"Run {run_id} does not exist")
    harness = session_manager.get_harness_state(session_id)
    report_id = str(raw_run.get("run_review_report_id") or "")
    report = (harness.get("run_review_reports") or {}).get(report_id)
    if isinstance(report, dict):
        public_report = _public_run_review_report(harness, report)
        return {
            "status": str(public_report.get("status") or "completed"),
            "run_id": run_id,
            "report": public_report,
        }
    snapshot_id = str(raw_run.get("evaluation_snapshot_id") or "")
    operations = [
        item
        for item in (harness.get("verification_operations") or {}).values()
        if isinstance(item, dict) and item.get("snapshot_id") == snapshot_id
    ]
    if operations:
        latest = max(operations, key=lambda item: int(item.get("attempt_no") or 0))
        return {
            "status": str(latest.get("status") or "pending"),
            "operation_id": latest.get("operation_id"),
            "snapshot_id": snapshot_id,
            "run_id": run_id,
        }
    return {"status": "not_requested", "run_id": run_id}


def _raise_compaction_http_error(exc: AgentContextCompactionError) -> NoReturn:
    raise HTTPException(
        status_code=exc.status_code,
        detail={"code": exc.code, "message": str(exc)},
    ) from exc


async def _run_agent_compaction(
    session_id: str,
    *,
    operation_id: str,
    focus: str,
    claim: dict[str, Any],
) -> None:
    try:
        await agent_context_compaction_service.compact(
            session_id,
            focus=focus,
            operation_id=operation_id,
            claim=claim,
        )
    except AgentContextCompactionError as exc:
        logger.warning(
            "Agent context compaction ended with status=%s session=%s operation=%s error=%s",
            exc.code,
            session_id,
            operation_id,
            exc,
        )
    except Exception:
        logger.exception(
            "Unexpected Agent context compaction failure session=%s operation=%s",
            session_id,
            operation_id,
        )
    finally:
        _agent_compaction_tasks.pop(operation_id, None)


@router.post("/agent/sessions/{session_id}/compact", status_code=202)
async def compact_agent_session(
    session_id: str,
    request: AgentCompactRequest,
) -> dict[str, Any]:
    """Claim background compaction and return before any Provider call."""

    try:
        operation_id, claim = agent_context_compaction_service.begin(
            session_id,
            focus=request.focus,
        )
    except AgentContextCompactionError as exc:
        _raise_compaction_http_error(exc)
    try:
        task = asyncio.create_task(
            _run_agent_compaction(
                session_id,
                operation_id=operation_id,
                focus=request.focus,
                claim=claim,
            ),
            name=f"agent-context-{operation_id}",
        )
    except Exception as exc:
        session_manager.fail_agent_context_compaction(
            session_id,
            operation_id=operation_id,
            error=f"Failed to launch compaction task: {type(exc).__name__}: {exc}",
        )
        raise HTTPException(status_code=500, detail="Failed to launch compaction task") from exc
    _agent_compaction_tasks[operation_id] = task
    return agent_context_compaction_service.result_payload(session_id, claim)


@router.get("/agent/sessions/{session_id}/compact/{operation_id}")
async def agent_compaction_status(session_id: str, operation_id: str) -> dict[str, Any]:
    """Poll durable compaction state after the short start request returns."""

    try:
        return agent_context_compaction_service.status(
            session_id,
            operation_id=operation_id,
        )
    except AgentContextCompactionError as exc:
        _raise_compaction_http_error(exc)


@router.post("/agent")
async def agent(request: AgentRequest):
    request_started_at = time.perf_counter()
    request_received_at = time.time()
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
    # A conversation-level model choice is durable.  New clients include the
    # complete route in every Run request, while older/stale renderer bundles
    # may only have persisted it through PATCH /sessions/{id}.  In that case,
    # read the Session choice instead of silently falling back to the global
    # default model.
    session_selection = session_manager.get_metadata(request.session_id)
    persisted_model_id = str(session_selection.get("llm_model_id") or "").strip() or None
    persisted_thinking_level = str(session_selection.get("thinking_level") or "").strip() or None
    persisted_credential_name = str(session_selection.get("credential_name") or "").strip() or None
    if persisted_thinking_level not in {"low", "high", "max"}:
        persisted_thinking_level = None

    requested_model_id = str(request.llm_model_id or "").strip() or None
    selected_model_id = requested_model_id or persisted_model_id
    selected_thinking_level = request.thinking_level
    selected_credential_name = request.credential_name
    if selected_thinking_level is None and requested_model_id is None:
        selected_thinking_level = persisted_thinking_level
    if selected_credential_name is None and requested_model_id is None:
        selected_credential_name = persisted_credential_name

    # Validate the complete Provider route and its normalized thinking level
    # before an SSE response is opened.  This also prevents a model name from
    # being sent through the endpoint of a different Provider.
    try:
        effective_llm = get_fallback_llm_config(
            model_id_override=selected_model_id,
            thinking_level=selected_thinking_level,
            credential_name=selected_credential_name,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    selection_explicit = bool(selected_model_id or selected_thinking_level or selected_credential_name)
    runtime_model_id = str(effective_llm.get("model_id") or "") if selection_explicit else None
    # The inherited default route still freezes the model Profile's default
    # reasoning strength for this Run. It is not persisted as a user override.
    runtime_thinking_level = effective_llm.get("thinking_level")
    runtime_credential_name = effective_llm.get("credential_name") if selection_explicit else None
    selection_source = (
        "request"
        if requested_model_id or request.thinking_level is not None or request.credential_name is not None
        else "session"
        if persisted_model_id or persisted_thinking_level or persisted_credential_name
        else "default"
    )
    logger.info(
        "[agent-model] session=%s source=%s route=%s provider=%s model=%s thinking_level=%s credential_name=%s",
        request.session_id,
        selection_source,
        runtime_model_id or effective_llm.get("model_id") or "<default>",
        effective_llm.get("provider") or "<unknown>",
        effective_llm.get("model") or "<unknown>",
        runtime_thinking_level or "<none>",
        runtime_credential_name or "default",
    )
    persisted_user_message = False
    session_metadata = {"runtime_mode": "agent"}
    if selection_explicit:
        session_metadata.update(
            {
                "llm_model_id": runtime_model_id,
                "thinking_level": runtime_thinking_level,
                "credential_name": runtime_credential_name,
            }
        )
    if request.goal_control_action is None:
        try:
            session_manager.update_metadata(request.session_id, session_metadata)
            session_manager.save_message(
                request.session_id,
                "user",
                deepagents_agent_manager._display_message_with_attachments(
                    request.message,
                    request.attachments,
                ),
                attachments=request.attachments,
                created_at=request_received_at,
            )
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail="Session not found") from exc
        except RuntimeError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        persisted_user_message = True
    elif request.goal_control_action is not None:
        # Product controls are audited by the Run/Goal ledger. They are not
        # synthetic user chat messages and must not pollute the transcript.
        try:
            session_manager.update_metadata(request.session_id, session_metadata)
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail="Session not found") from exc
        persisted_user_message = True

    if request.stream:
        stream = deepagents_agent_manager.astream(
            message=request.message,
            session_id=request.session_id,
            project_id=request.project_id,
            analytics_model_id=request.analytics_model_id,
            llm_model_id=runtime_model_id,
            thinking_level=runtime_thinking_level,
            credential_name=runtime_credential_name,
            user_id=request.user_id,
            attachments=request.attachments,
            skill_hints=request.skill_hints,
            user_message_already_persisted=persisted_user_message,
            goal_mode=request.goal_mode,
            goal_id=request.goal_id,
            context_goal_id=request.context_goal_id,
            goal_control_action=request.goal_control_action,
            run_review_policy=request.run_review_policy,
            query_created_at=request_received_at,
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
        llm_model_id=runtime_model_id,
        thinking_level=runtime_thinking_level,
        credential_name=runtime_credential_name,
        user_id=request.user_id,
        attachments=request.attachments,
        skill_hints=request.skill_hints,
        user_message_already_persisted=persisted_user_message,
        goal_mode=request.goal_mode,
        goal_id=request.goal_id,
        context_goal_id=request.context_goal_id,
        goal_control_action=request.goal_control_action,
        run_review_policy=request.run_review_policy,
        query_created_at=request_received_at,
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
