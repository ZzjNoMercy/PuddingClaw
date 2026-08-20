"""Local synchronous Worker API for unattended PuddingClaw Runs."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import ipaddress
import json
import logging
import os
import secrets
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Header, HTTPException, Query, Request, status
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field, model_validator

from analytics.models import get_analytics_model_registry
from analytics.models.router import AnalyticsModelRoute, AnalyticsModelRouter
from cli_runtime import current_cli_runtime_status
from graph.deepagents_manager import deepagents_agent_manager
from graph.headless_resolver import headless_authority_from_environment
from graph.permission_resume import permission_resume_registry
from graph.session_manager import session_manager
from headless_session_lifecycle import (
    TERMINAL_RUN_STATUSES,
    cleanup_stale_headless_sessions,
    headless_session_expires_at,
    headless_session_has_pending_resume,
    headless_session_ttl_seconds,
    is_headless_session_expired,
)
from llm.model_client import ModelClientChatModel
from projects.registry import project_registry
from headless_activity import headless_activity_log_store

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/headless", tags=["headless-worker"])
headless_activity_router = APIRouter(tags=["headless-activity"])
BASE_DIR = Path(__file__).resolve().parent.parent
_idempotency_lock = threading.RLock()
_WORKER_PROJECT_NAME = "puddingclaw"
_active_headless_sessions: set[str] = set()
_active_headless_sessions_lock = threading.RLock()
_headless_cleanup_lock = threading.Lock()
_last_headless_cleanup_monotonic = 0.0
_BEIJING_TIMEZONE = ZoneInfo("Asia/Shanghai")
_headless_executions: dict[str, _HeadlessExecution] = {}
_headless_executions_lock = threading.RLock()
_HEADLESS_EXECUTION_RETENTION_SECONDS = 3600.0
_HEADLESS_EVENT_HISTORY_LIMIT = 32768
_HEADLESS_SUBSCRIBER_QUEUE_LIMIT = 16384
# Reasoning and internal diagnostics remain available through the normal Trace
# system, but the public Worker stream is intentionally narrow: lifecycle,
# visible assistant content, tool boundaries and HITL.  This is an allowlist so
# a new internal event cannot accidentally become part of the external API.
_HEADLESS_PUBLIC_EVENTS = frozenset(
    {
        "run_starting",
        "task_preflight_started",
        "task_preflight_completed",
        "run_started",
        "run_outcome",
        "goal_run_continued",
        "new_response",
        "token",
        "segment_break",
        "segment_content_replaced",
        "tool_start",
        "tool_end",
        "permission_required",
        "permission_resolved",
        "final_response",
        "done",
        "error",
    }
)


def _claim_headless_session(session_id: str) -> bool:
    with _active_headless_sessions_lock:
        if session_id in _active_headless_sessions:
            return False
        _active_headless_sessions.add(session_id)
        return True


def _release_headless_session(session_id: str) -> None:
    with _active_headless_sessions_lock:
        _active_headless_sessions.discard(session_id)


def _prune_headless_executions() -> None:
    cutoff = time.time() - _HEADLESS_EXECUTION_RETENTION_SECONDS
    with _headless_executions_lock:
        stale = [
            session_id
            for session_id, execution in _headless_executions.items()
            if execution.done and float(getattr(execution, "done_at", 0.0) or 0.0) <= cutoff
        ]
        for session_id in stale:
            _headless_executions.pop(session_id, None)


def _cleanup_interval_seconds() -> float:
    raw = os.getenv("PUDDINGCLAW_HEADLESS_SESSION_CLEANUP_INTERVAL_S", "3600").strip()
    try:
        return max(0.0, float(raw))
    except ValueError:
        return 3600.0


def _maybe_cleanup_stale_headless_sessions(*, force: bool = False, now: float | None = None) -> list[str]:
    """Run bounded opportunistic cleanup without racing a claimed request."""

    global _last_headless_cleanup_monotonic
    monotonic_now = time.monotonic()
    with _headless_cleanup_lock:
        interval = _cleanup_interval_seconds()
        if not force and monotonic_now - _last_headless_cleanup_monotonic < interval:
            return []
        _last_headless_cleanup_monotonic = monotonic_now
        with _active_headless_sessions_lock:
            return cleanup_stale_headless_sessions(
                now=now,
                protected_session_ids=set(_active_headless_sessions),
            )


def _attach_session_lifecycle(response: dict[str, Any], session_id: str) -> dict[str, Any]:
    ttl_seconds = headless_session_ttl_seconds()
    metadata = session_manager.get_metadata(session_id)
    response["session_ttl_seconds"] = int(ttl_seconds) if ttl_seconds is not None else None
    response["session_expires_at"] = headless_session_expires_at(metadata, ttl_seconds)
    return response


class HeadlessRunRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    message: str = Field(min_length=1, max_length=100_000)
    session_id: str | None = Field(default=None, max_length=200)
    # The local Platform/CLI may provide the host workspace.  It is resolved
    # and registered by PuddingClaw; callers never need to know project_id.
    workspace_path: str | None = Field(default=None, max_length=4_096)
    metadata: dict[str, Any] = Field(default_factory=dict)
    request_id: str | None = Field(default=None, max_length=200)

    @model_validator(mode="after")
    def validate_message(self):
        if not self.message.strip():
            raise ValueError("message must not be empty")
        return self


class HeadlessResumeDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")

    request_id: str = Field(min_length=1, max_length=200)
    decision: str = Field(pattern="^(approve|reject)$")
    scope: str = Field(default="once", pattern="^(once|session)$")
    message: str | None = Field(default=None, max_length=2_000)


class HeadlessResumeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    continuation_token: str = Field(min_length=20, max_length=500)
    decisions: list[HeadlessResumeDecision] = Field(min_length=1, max_length=50)
    request_id: str | None = Field(default=None, max_length=200)
    workspace_path: str | None = Field(default=None, max_length=4_096)


def _headless_artifacts(session_id: str, project_id: str) -> list[dict[str, Any]]:
    """Expose only durable, workspace-relative artifacts for CLI export."""

    try:
        workspace = project_registry.resolve(project_id)
        registered = session_manager.list_delivered_artifacts(
            session_id,
            verify_freshness=True,
            include_inactive=False,
        )
    except Exception:
        return []
    artifacts: list[dict[str, Any]] = []
    for item in registered:
        target = Path(str(item.get("target_path") or "")).expanduser().resolve()
        try:
            relative = target.relative_to(workspace).as_posix()
        except ValueError:
            # A PuddingClaw CLI caller can only safely export files in the
            # worker project; never leak or guess a server-side absolute path.
            continue
        if not target.is_file():
            continue
        try:
            size = target.stat().st_size
        except OSError:
            size = None
        artifacts.append(
            {
                "name": target.name,
                "path": relative,
                "kind": "report",
                "size": size,
                "origin": "push",
            }
        )
    return artifacts


class _HeadlessExecution:
    """One live Headless stream suspended on the normal HITL registries.

    The Agent task, LangGraph checkpoint thread and Run remain the same while
    the synchronous HTTP caller is released.  The consumer later resolves the
    pending request and waits for the next externally observable boundary.
    """

    def __init__(
        self,
        *,
        stream: Any,
        session_id: str,
        project_id: str,
        approval_mode: str,
        analytics_model_id: str,
        analytics_model_match: dict[str, Any],
    ) -> None:
        self.stream = stream
        self.session_id = session_id
        self.project_id = project_id
        self.approval_mode = approval_mode
        self.analytics_model_id = analytics_model_id
        self.analytics_model_match = dict(analytics_model_match)
        self.token = secrets.token_urlsafe(32)
        self.run_id = ""
        self.query_id = ""
        self.final_content = ""
        self.final_response = ""
        self.outcome: dict[str, Any] = {}
        self.verification: dict[str, Any] = {"status": "not_required", "summary": ""}
        self.pending_inputs: dict[str, dict[str, Any]] = {}
        self.terminal_needs_input: dict[str, Any] | None = None
        self.revision = 0
        self.done = False
        self.cancelled = False
        self.done_at = 0.0
        self.resume_results: dict[str, tuple[str, dict[str, Any]]] = {}
        self.event_sequence = 0
        self.event_history: list[dict[str, Any]] = []
        self.event_subscribers: set[asyncio.Queue[dict[str, Any] | None]] = set()
        self.error: BaseException | None = None
        self.updated = asyncio.Condition()
        self.task: asyncio.Task[None] | None = None

    def _publish(self, item: dict[str, Any]) -> None:
        """Publish one event to every observer without transferring ownership.

        The CLI stream, PuddingTeams and the Web UI are observers of the same
        Harness Run.  A slow/disconnected observer must never consume or block
        events for another observer.
        """

        self.event_sequence += 1
        envelope = {"seq": self.event_sequence, **item}
        self.event_history.append(envelope)
        if len(self.event_history) > _HEADLESS_EVENT_HISTORY_LIMIT:
            del self.event_history[: len(self.event_history) - _HEADLESS_EVENT_HISTORY_LIMIT]
        for queue in list(self.event_subscribers):
            try:
                queue.put_nowait(envelope)
            except asyncio.QueueFull:
                # Close only the lagging observer. It can reconnect and replay
                # from the bounded history or fall back to Session History.
                self.event_subscribers.discard(queue)
                try:
                    while True:
                        queue.get_nowait()
                except asyncio.QueueEmpty:
                    pass
                try:
                    queue.put_nowait(
                        {
                            "seq": self.event_sequence,
                            "event": "stream_reset_required",
                            "data": {"session_id": self.session_id},
                        }
                    )
                    queue.put_nowait(None)
                except asyncio.QueueFull:
                    pass

    def subscribe(self, *, after_seq: int = 0) -> asyncio.Queue[dict[str, Any] | None]:
        """Atomically replay retained events and subscribe to future events."""

        queue: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue(
            maxsize=_HEADLESS_SUBSCRIBER_QUEUE_LIMIT
        )
        retained = [item for item in self.event_history if int(item.get("seq") or 0) > after_seq]
        oldest_seq = int(self.event_history[0].get("seq") or 0) if self.event_history else 0
        history_gap = bool(oldest_seq and oldest_seq > after_seq + 1)
        replay_without_reset = _HEADLESS_SUBSCRIBER_QUEUE_LIMIT - 1
        needs_reset = history_gap or len(retained) > replay_without_reset
        replay_capacity = replay_without_reset - int(needs_reset)
        replayed = retained[-replay_capacity:]
        if needs_reset:
            replay_start = int(replayed[0].get("seq") or oldest_seq) if replayed else oldest_seq
            queue.put_nowait(
                {
                    "seq": max(0, replay_start - 1),
                    "event": "stream_reset_required",
                    "data": {
                        "session_id": self.session_id,
                        "oldest_seq": oldest_seq,
                        "replay_start_seq": replay_start,
                    },
                }
            )
        # Always retain one slot for the terminal sentinel; a reset marker uses
        # one more. This also leaves an active replay subscriber able to accept
        # the next live event instead of being disconnected immediately.
        for item in replayed:
            queue.put_nowait(item)
        if self.done:
            queue.put_nowait(None)
        else:
            self.event_subscribers.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue[dict[str, Any] | None]) -> None:
        self.event_subscribers.discard(queue)

    def _close_subscribers(self) -> None:
        for queue in list(self.event_subscribers):
            try:
                queue.put_nowait(None)
            except asyncio.QueueFull:
                try:
                    queue.get_nowait()
                    queue.put_nowait(None)
                except (asyncio.QueueEmpty, asyncio.QueueFull):
                    pass
        self.event_subscribers.clear()

    def start(self) -> None:
        # Emit a first boundary before the Agent generator performs its
        # preflight.  This forces an NDJSON/SSE response to flush immediately
        # instead of making the caller wait for the first terminal boundary.
        self._publish(
            {
                "event": "run_starting",
                "data": {
                    "session_id": self.session_id,
                    "project_id": self.project_id,
                    "status": "starting",
                },
            }
        )
        self.task = asyncio.create_task(self._consume())

    async def _signal(self) -> None:
        async with self.updated:
            self.revision += 1
            self.updated.notify_all()

    async def _consume(self) -> None:
        try:
            async for event in self.stream:
                name = str(event.get("event") or "")
                try:
                    payload = json.loads(event.get("data") or "{}")
                except (TypeError, ValueError):
                    payload = {}
                if not isinstance(payload, dict):
                    payload = {}
                run_payload = payload.get("run") if isinstance(payload.get("run"), dict) else {}
                self.run_id = str(payload.get("run_id") or run_payload.get("run_id") or self.run_id)
                self.query_id = str(payload.get("query_id") or run_payload.get("query_id") or self.query_id)
                boundary_signal = False
                if name == "run_outcome":
                    self.outcome = payload
                elif name == "verification_report":
                    report = payload.get("report") if isinstance(payload.get("report"), dict) else {}
                    self.verification = {
                        "status": report.get("status") or "not_required",
                        "summary": report.get("explanation") or "",
                    }
                elif name == "final_response":
                    self.final_response = str(payload.get("final_response") or "")
                elif name == "done":
                    self.final_content = str(payload.get("content") or "")
                    self.final_response = str(payload.get("final_response") or self.final_response)
                elif name == "permission_required":
                    pending = _needs_input(name, payload)
                    if pending is not None and pending.get("request_id"):
                        self.pending_inputs[str(pending["request_id"])] = pending
                        self._persist_pending_state("pending")
                        boundary_signal = True
                elif name.endswith("_required"):
                    # External Headless continuation is deliberately limited
                    # to permissions. The graph auto-resolves other business
                    # HITL fail-closed, but retain its structured explanation
                    # on the eventual terminal response.
                    self.terminal_needs_input = _needs_input(name, payload) or self.terminal_needs_input
                elif name.endswith("_resolved"):
                    request_id = str(payload.get("request_id") or "")
                    if request_id:
                        self.pending_inputs.pop(request_id, None)
                        self._persist_pending_state("resumed")
                # Publish only after the execution state reflects this event;
                # every observer therefore sees run_id/HITL state atomically.
                if name in _HEADLESS_PUBLIC_EVENTS:
                    self._publish({"event": name, "data": payload})
                if boundary_signal:
                    await self._signal()
        except BaseException as exc:
            if isinstance(exc, asyncio.CancelledError):
                self.cancelled = True
            else:
                self.error = exc
        finally:
            try:
                await self.stream.aclose()
            except Exception:
                pass
            self.stream = None
            self.done = True
            self.done_at = time.time()
            self._close_subscribers()
            self._persist_pending_state("completed" if self.error is None else "failed")
            await self._signal()

    def _persist_pending_state(self, status_value: str) -> None:
        payload = {
            "status": status_value,
            "run_id": self.run_id or None,
            "query_id": self.query_id or None,
            "request_ids": list(self.pending_inputs),
            "requests": [
                {
                    **{
                        "id": request.get("request_id"),
                        "type": request.get("permission_type") or "permission",
                        "session_id": self.session_id,
                        "query_id": self.query_id or None,
                        "status": "pending",
                        "approval_source": "headless_consumer",
                    },
                    **{
                        key: request.get(key)
                        for key in (
                            "tool_name",
                            "command",
                            "path",
                            "paths",
                            "grant_specs",
                            "capabilities",
                            "grant_bindings",
                            "risk",
                            "reason",
                            "options",
                        )
                        if request.get(key) is not None
                    },
                }
                for request in self.pending_inputs.values()
            ],
            "updated_at": time.time(),
        }
        try:
            session_manager.update_metadata(self.session_id, {"headless_pending_input": payload})
        except Exception:
            logger.debug(
                "Failed to persist Headless pending-input state for session=%s",
                self.session_id,
                exc_info=True,
            )

    async def wait_for_boundary(self, *, after_revision: int = -1) -> None:
        timeout_s = max(1.0, float(os.getenv("PUDDINGCLAW_TIMEOUT_S", "600")))
        async with asyncio.timeout(timeout_s):
            async with self.updated:
                await self.updated.wait_for(
                    lambda: self.revision > after_revision and (self.done or bool(self.pending_inputs))
                )
        # Parallel nodes can emit several interrupts in adjacent loop turns.
        # Let the stream collector gather the complete pause set before the
        # synchronous caller renders approval choices.
        if self.pending_inputs and not self.done:
            await asyncio.sleep(0.02)

    async def cancel(self) -> None:
        if self.done:
            return
        task = self.task
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        self.cancelled = True
        self.done = True
        self.done_at = time.time()
        await self._signal()

    def response(self) -> dict[str, Any]:
        if self.pending_inputs and not self.done:
            requests = list(self.pending_inputs.values())
            first = dict(requests[0])
            if len(requests) > 1:
                first["requests"] = requests
            return {
                "schema_version": "1",
                "run_id": self.run_id or None,
                "session_id": self.session_id,
                "project_id": self.project_id,
                "analytics_model_id": self.analytics_model_id,
                "analytics_model_match": self.analytics_model_match,
                "approval_mode": self.approval_mode,
                "status": "needs_input",
                "outcome": "waiting_hitl",
                "reply": self.final_content,
                "final_response": self.final_response,
                "verification": self.verification,
                "needs_input": first,
                "continuation_token": self.token,
            }
        if self.error is not None:
            raise self.error
        if self.cancelled:
            return {
                "schema_version": "1",
                "run_id": self.run_id or None,
                "session_id": self.session_id,
                "project_id": self.project_id,
                "analytics_model_id": self.analytics_model_id,
                "analytics_model_match": self.analytics_model_match,
                "approval_mode": self.approval_mode,
                "status": "cancelled",
                "outcome": "cancelled",
                "reply": self.final_content,
                "final_response": self.final_response,
                "verification": self.verification,
                "needs_input": None,
                "artifacts": _headless_artifacts(self.session_id, self.project_id),
            }
        final_outcome = str(self.outcome.get("outcome") or "failed")
        status_value = str(self.outcome.get("status") or final_outcome)
        artifacts = _headless_artifacts(self.session_id, self.project_id)
        return {
            "schema_version": "1",
            "run_id": self.outcome.get("run_id") or self.run_id or None,
            "session_id": self.session_id,
            "project_id": self.project_id,
            "analytics_model_id": self.analytics_model_id,
            "analytics_model_match": self.analytics_model_match,
            "approval_mode": self.approval_mode,
            "status": status_value,
            "outcome": final_outcome,
            "reply": self.final_content,
            "final_response": self.final_response,
            "verification": self.verification,
            "budget_exhaustion_reason": self.outcome.get("budget_exhaustion_reason"),
            "model_call_count": self.outcome.get("model_call_count", 0),
            "auto_resolved": self.outcome.get("auto_resolved") or [],
            "interrupt_summary": self.outcome.get("interrupt_summary")
            or {"total": 0, "auto_approved": 0, "auto_rejected": 0, "by_type": {}},
            "needs_input": self.terminal_needs_input,
            "artifacts": artifacts,
        }


def _safe_model(item: dict[str, Any]) -> dict[str, Any]:
    return {
        key: item.get(key)
        for key in ("id", "name", "description", "version", "tags")
    }


def _model_options() -> list[dict[str, Any]]:
    from runtime_identity.paths import PuddingClawPaths

    snapshot = get_analytics_model_registry(PuddingClawPaths.from_environment().user_definitions()).list_models()
    return [_safe_model(item) for item in snapshot.get("models") or [] if isinstance(item, dict)]


def _model_routing_candidates() -> list[dict[str, Any]]:
    """Return configured models enriched with bounded routing guidance."""

    from runtime_identity.paths import PuddingClawPaths

    registry = get_analytics_model_registry(PuddingClawPaths.from_environment().user_definitions())
    candidates: list[dict[str, Any]] = []
    for option in _model_options():
        candidate = dict(option)
        try:
            detail = registry.get_model(str(option.get("id") or ""))
            candidate["applicability"] = str(detail.get("body") or "")[:4_000]
        except Exception:
            # Registry summaries still provide a useful fail-closed candidate.
            candidate["applicability"] = ""
        candidates.append(candidate)
    return candidates


async def _route_analytics_model(message: str) -> AnalyticsModelRoute:
    """Resolve one configured Analytics Model without giving the CLI selection authority."""

    candidates = _model_routing_candidates()
    deterministic = AnalyticsModelRouter.deterministic(message, candidates)
    if deterministic is not None:
        return deterministic
    try:
        model = ModelClientChatModel(
            role="analytics_model_router",
            temperature=0,
            streaming=False,
            thinking_enabled=False,
        )
        timeout_s = max(
            1.0,
            float(os.getenv("PUDDINGCLAW_ANALYTICS_MODEL_ROUTER_TIMEOUT_S", "15")),
        )
        return await asyncio.wait_for(
            AnalyticsModelRouter.route(
                message=message,
                candidates=candidates,
                model=model,
            ),
            timeout=timeout_s,
        )
    except TimeoutError:
        return AnalyticsModelRoute("ambiguous", None, 0.0, "fallback", "classifier_timeout")
    except Exception as exc:
        return AnalyticsModelRoute(
            "ambiguous",
            None,
            0.0,
            "fallback",
            f"classifier_error:{type(exc).__name__}",
        )


def _model_routing_needs_input(
    route: AnalyticsModelRoute,
    models: list[dict[str, Any]],
) -> dict[str, Any]:
    unavailable = route.status == "unmatched" and (
        not models or route.reason == "bound_model_no_longer_allowed"
    )
    if route.reason == "bound_model_no_longer_allowed":
        prompt = "该连续任务绑定的分析模型已不可用，请联系管理员恢复权限或创建一个新任务。"
    elif unavailable:
        prompt = "当前 PuddingClaw 没有可用的分析模型，请先在 PuddingClaw 中完成模型配置。"
    else:
        prompt = "无法根据当前问题唯一匹配分析模型，请补充要分析的业务对象、指标或场景。"
    return {
        "schema_version": "1",
        "status": "needs_input",
        "outcome": "analytics_model_unavailable" if unavailable else "analytics_model_clarification_required",
        "analytics_model_match": route.to_dict(),
        "needs_input": {
            "type": "analytics_model_unavailable" if unavailable else "analytics_model_clarification",
            "prompt": prompt,
            "options": models,
        },
    }


def _model_binding(model_id: str) -> dict[str, Any]:
    from runtime_identity.paths import PuddingClawPaths

    model = get_analytics_model_registry(PuddingClawPaths.from_environment().user_definitions()).get_model(model_id)
    body = str(model.get("body") or "").encode("utf-8")
    return {
        "id": model_id,
        "version": str(model.get("version") or ""),
        "content_sha256": hashlib.sha256(body).hexdigest(),
    }


def _projects_root() -> Path:
    configured = os.getenv("PUDDINGCLAW_PROJECTS_ROOT", "").strip()
    return (Path(configured).expanduser() if configured else Path.home()).resolve() / _WORKER_PROJECT_NAME


def _ensure_worker_project() -> tuple[str, Path]:
    path = _projects_root()
    try:
        path.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise HTTPException(status_code=503, detail="Worker projects root is unavailable") from exc
    try:
        record = project_registry.register(
            str(path),
            name=_WORKER_PROJECT_NAME,
            trusted=True,
        )
    except (FileNotFoundError, NotADirectoryError, KeyError) as exc:
        raise HTTPException(status_code=503, detail="Worker project is unavailable") from exc
    return record.project_id, path


def _resolve_worker_project(workspace_path: str | None) -> tuple[str, Path]:
    """Resolve an optional Platform workspace into PuddingClaw's project id.

    The absolute path is an integration boundary between two local processes;
    ``project_id`` remains an internal Backend identifier.  When omitted, keep
    the original standalone CLI behavior and use the default worker project.
    """

    raw = str(workspace_path or "").strip()
    if not raw:
        return _ensure_worker_project()
    candidate = Path(raw).expanduser()
    if not candidate.is_absolute():
        raise HTTPException(status_code=400, detail="workspace_path must be an absolute host path")
    path = candidate.resolve()
    if not path.exists():
        raise HTTPException(status_code=404, detail="workspace_path does not exist")
    if not path.is_dir():
        raise HTTPException(status_code=400, detail="workspace_path must be a directory")
    try:
        record = project_registry.register(
            str(path),
            name=path.name or _WORKER_PROJECT_NAME,
            trusted=True,
        )
    except (FileNotFoundError, NotADirectoryError) as exc:
        raise HTTPException(status_code=400, detail="workspace_path is unavailable") from exc
    except Exception as exc:
        raise HTTPException(status_code=503, detail="workspace project is unavailable") from exc
    return record.project_id, path


def _is_loopback_request(request: Request) -> bool:
    host = str(request.client.host if request.client else "").strip().lower()
    if host in {"localhost", "testclient"}:
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _require_loopback_request(request: Request) -> None:
    """Keep the credential-free Headless transport local to this machine."""

    if not _is_loopback_request(request):
        raise HTTPException(status_code=403, detail="Headless API is available only on the local machine")


def _idempotency_path() -> Path:
    from runtime_identity.paths import PuddingClawPaths

    data_dir = PuddingClawPaths.from_environment().state()
    data_dir.mkdir(parents=True, exist_ok=True)
    return data_dir / "headless-idempotency.json"


def _idempotency_key(request: HeadlessRunRequest, header: str | None) -> str:
    # FastAPI replaces ``Header`` defaults for real HTTP requests, but direct
    # callers (including internal tests/adapters) receive the ``Header``
    # descriptor itself when the argument is omitted.  Never persist that
    # shared descriptor representation as an idempotency key.
    resolved_header = header if isinstance(header, str) else None
    return str(resolved_header or request.request_id or "").strip()


def _request_hash(request: HeadlessRunRequest) -> str:
    payload = {
        "message": request.message,
        "session_id": request.session_id,
        "workspace_path": request.workspace_path,
        "metadata": request.metadata,
    }
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str).encode()).hexdigest()


def _caller_identity(metadata: dict[str, Any] | None) -> tuple[str, str]:
    """Return audit-only caller labels supplied by a trusted local host.

    This is attribution, not authorization. The Headless API has one local
    security boundary and never grants capabilities from these values.
    """

    values = metadata if isinstance(metadata, dict) else {}
    caller_id = str(values.get("caller_id") or values.get("source") or "local-cli").strip()
    caller_name = str(values.get("caller_name") or values.get("source_name") or "PuddingClaw CLI").strip()
    return (caller_id or "local-cli")[:120], (caller_name or "PuddingClaw CLI")[:120]


def _resume_request_hash(request: HeadlessResumeRequest) -> str:
    payload = {
        "continuation_token": request.continuation_token,
        "decisions": [item.model_dump(mode="json") for item in request.decisions],
        "workspace_path": request.workspace_path,
    }
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def _reserve_idempotency(key: str, request_hash: str) -> dict[str, Any] | None:
    if not key:
        return None
    path = _idempotency_path()
    with _idempotency_lock:
        try:
            records = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            records = {}
        if not isinstance(records, dict):
            records = {}
        existing = records.get(key)
        if isinstance(existing, dict):
            if existing.get("request_hash") != request_hash:
                raise HTTPException(status_code=409, detail="Idempotency-Key was already used with another request")
            return existing
        records[key] = {"request_hash": request_hash, "status": "running", "created_at": time.time()}
        temp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
        temp.write_text(json.dumps(records, ensure_ascii=False, indent=2), encoding="utf-8")
        temp.replace(path)
    return None


def _finish_idempotency(key: str, response: dict[str, Any]) -> None:
    if not key:
        return
    path = _idempotency_path()
    with _idempotency_lock:
        try:
            records = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            records = {}
        if not isinstance(records, dict) or not isinstance(records.get(key), dict):
            return
        records[key].update({"status": "completed", "response": response, "completed_at": time.time()})
        temp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
        temp.write_text(json.dumps(records, ensure_ascii=False, indent=2), encoding="utf-8")
        temp.replace(path)


def _abandon_idempotency(key: str, request_hash: str) -> None:
    """Remove this request's unfinished reservation so a corrected retry can run."""

    if not key:
        return
    path = _idempotency_path()
    with _idempotency_lock:
        try:
            records = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        existing = records.get(key) if isinstance(records, dict) else None
        if not isinstance(existing, dict):
            return
        if existing.get("status") != "running" or existing.get("request_hash") != request_hash:
            return
        records.pop(key, None)
        temp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
        temp.write_text(json.dumps(records, ensure_ascii=False, indent=2), encoding="utf-8")
        temp.replace(path)


def _needs_input(event_name: str, payload: dict[str, Any]) -> dict[str, Any] | None:
    mapping = {
        "permission_required": "permission_request",
        "user_input_required": "user_input",
        "skill_secret_required": "skill_secret",
        "database_sql_revision_required": "database_sql_revision",
        "dimension_build_rule_required": "dimension_build_rule",
        "logical_dataset_rule_required": "logical_dataset_rule",
        "skill_plan_confirmation_required": "skill_plan_confirmation",
    }
    input_type = mapping.get(event_name)
    if not input_type:
        return None
    result: dict[str, Any] = {
        "type": input_type,
        "request_id": payload.get("id"),
        "prompt": "该任务需要人工确认，Worker 未自动伪造业务决定。",
    }
    if input_type == "permission_request":
        request_type = str(payload.get("type") or "permission")
        tool_name = str(payload.get("tool_name") or "")
        command = str(payload.get("command") or "")
        path = str(payload.get("path") or "")
        target = tool_name or path or request_type
        result.update(
            {
                "permission_type": request_type,
                "prompt": f"Agent 请求授权：{target}",
                "tool_name": tool_name or None,
                "command": command or None,
                "path": path or None,
                "paths": payload.get("paths") or [],
                "grant_specs": payload.get("grant_specs") or [],
                "capabilities": payload.get("capabilities") or [],
                "grant_bindings": payload.get("grant_bindings") or {},
                "risk": payload.get("risk"),
                "reason": payload.get("reason"),
                "options": payload.get("options") or ["once"],
                "request": payload,
            }
        )
    if input_type == "user_input":
        result["questions"] = payload.get("questions") or []
    if input_type == "skill_secret":
        result.update(
            {
                "prompt": "该 Skill 需要用户在桌面安全输入框配置凭证；Worker 不接收 Secret。",
                "skill_id": payload.get("skill_id"),
                "env_name": payload.get("env_name"),
            }
        )
    return result


async def _consume_run(
    *,
    request: HeadlessRunRequest,
    session_id: str,
    project_id: str,
    approval_mode: str,
    authority: dict[str, Any],
    request_received_at: float,
    analytics_model_id: str,
    analytics_model_match: dict[str, Any],
) -> dict[str, Any]:
    execution = await _start_headless_execution(
        request=request,
        session_id=session_id,
        project_id=project_id,
        approval_mode=approval_mode,
        authority=authority,
        request_received_at=request_received_at,
        analytics_model_id=analytics_model_id,
        analytics_model_match=analytics_model_match,
    )
    try:
        await execution.wait_for_boundary()
    except BaseException:
        await execution.cancel()
        raise
    return execution.response()


async def _start_headless_execution(
    *,
    request: HeadlessRunRequest,
    session_id: str,
    project_id: str,
    approval_mode: str,
    authority: dict[str, Any],
    request_received_at: float,
    analytics_model_id: str,
    analytics_model_match: dict[str, Any],
) -> _HeadlessExecution:
    """Create and start a live Headless Run without waiting for a boundary.

    The HTTP transport can now return a StreamingResponse immediately.  The
    existing synchronous `_consume_run` path remains a compatibility wrapper
    for `--json` callers and HITL resume endpoints.
    """
    stream = deepagents_agent_manager.astream(
        message=request.message,
        session_id=session_id,
        project_id=project_id,
        analytics_model_id=analytics_model_id or None,
        analytics_model_snapshot=_model_binding(analytics_model_id) if analytics_model_id else None,
        user_id="worker",
        # Headless is externally interactive: unlike ``auto`` it must never
        # fabricate or reject a user's approval decision.  The background
        # consumer keeps the original graph invocation alive while the HTTP
        # caller is released with a structured ``needs_input`` response.
        interaction_mode="external",
        authority_profile=str(authority.get("profile") or "restricted"),
        authority_directories=list(authority.get("directories") or []),
        authority_network_origins=list(authority.get("network_origins") or []),
        query_created_at=request_received_at,
    )
    execution = _HeadlessExecution(
        stream=stream,
        session_id=session_id,
        project_id=project_id,
        approval_mode=approval_mode,
        analytics_model_id=analytics_model_id,
        analytics_model_match=analytics_model_match,
    )
    _prune_headless_executions()
    with _headless_executions_lock:
        _headless_executions[session_id] = execution
    execution.start()
    return execution


async def _stream_headless_execution(
    execution: _HeadlessExecution,
    initial_response: dict[str, Any] | None = None,
    *,
    after_seq: int = 0,
):
    """Yield live Agent events, then one canonical result event."""

    if initial_response is not None and execution.pending_inputs and not execution.done:
        yield {"event": "result", "data": initial_response}
        return
    subscriber = execution.subscribe(after_seq=after_seq)
    try:
        while True:
            envelope = await subscriber.get()
            if envelope is None:
                break
            yield {"event": envelope["event"], "data": envelope["data"]}
            if (
                envelope["event"] == "permission_required"
                and execution.pending_inputs
                and not execution.done
            ):
                yield {"event": "result", "data": execution.response()}
                return
        yield {"event": "result", "data": execution.response()}
    finally:
        execution.unsubscribe(subscriber)


def get_headless_execution(session_id: str) -> _HeadlessExecution | None:
    """Return the retained execution observed by CLI/Web subscribers."""

    _prune_headless_executions()
    with _headless_executions_lock:
        return _headless_executions.get(session_id)


async def observe_headless_execution(session_id: str, *, after_seq: int = 0):
    """Observe one Headless Run without consuming another client's events."""

    execution = get_headless_execution(session_id)
    if execution is None:
        return
    subscriber = execution.subscribe(after_seq=after_seq)
    try:
        while True:
            envelope = await subscriber.get()
            if envelope is None:
                break
            yield envelope
    finally:
        execution.unsubscribe(subscriber)


async def _resolve_external_permission(
    *,
    session_id: str,
    decision: HeadlessResumeDecision,
) -> None:
    """Apply one consumer decision through the canonical permission service."""

    # Import lazily so the normal interactive permission router and Headless
    # transport share one grant implementation without creating an API import
    # cycle at application startup.
    from api.permissions import (
        ExternalFileGrantRequest,
        PermissionDenyRequest,
        ShellDirectoryGrantRequest,
        ToolActionGrantRequest,
        deny_permission_request,
        grant_external_file_permission,
        grant_shell_directory_permission,
        grant_tool_action_permission,
    )

    pending = permission_resume_registry.get(decision.request_id)
    if pending is None:
        raise HTTPException(status_code=404, detail="Permission request not found")
    if str(pending.get("session_id") or "") != session_id:
        raise HTTPException(status_code=400, detail="Permission request belongs to another Session")
    if str(pending.get("status") or "") != "pending":
        raise HTTPException(status_code=409, detail="Permission request is no longer pending")
    if decision.decision == "reject":
        await deny_permission_request(
            session_id,
            PermissionDenyRequest(
                permission_request_id=decision.request_id,
                message=decision.message or "User denied permission from the Headless CLI.",
            ),
        )
        return

    request_type = str(pending.get("type") or "")
    if request_type == "tool_action":
        options = {str(item) for item in pending.get("options") or []}
        scope = "session" if decision.scope == "session" and "session" in options else "once"
        await grant_tool_action_permission(
            session_id,
            ToolActionGrantRequest(
                permission_request_id=decision.request_id,
                scope=scope,
            ),
        )
        return
    if request_type == "shell_directory_access":
        await grant_shell_directory_permission(
            session_id,
            ShellDirectoryGrantRequest(
                permission_request_id=decision.request_id,
                scope="session" if decision.scope == "session" else "run",
            ),
        )
        return
    if request_type.startswith("external_file_") or request_type.startswith("external_directory_"):
        is_directory = request_type.startswith("external_directory_")
        if not is_directory and decision.scope != "session":
            raise HTTPException(
                status_code=422,
                detail="External file permissions require Session scope",
            )
        await grant_external_file_permission(
            session_id,
            ExternalFileGrantRequest(
                target_kind="exact_directory" if is_directory else "exact_file",
                path=str(pending.get("path") or ""),
                permission_request_id=decision.request_id,
                scope=("session" if decision.scope == "session" else "run") if is_directory else None,
            ),
        )
        return
    raise HTTPException(status_code=422, detail=f"Unsupported permission request type: {request_type}")


@router.get("/health")
async def worker_health(request: Request):
    _require_loopback_request(request)
    _maybe_cleanup_stale_headless_sessions()
    project_id, path = _ensure_worker_project()
    cli_status = current_cli_runtime_status(BASE_DIR)
    cli_status.pop("path", None)
    cli_status.pop("package_dir", None)
    return {
        "schema_version": "1",
        "agent_id": "puddingclaw",
        "cli_version": "0.1.19",
        "protocol_version": "1",
        "configured": True,
        "reachable": True,
        "server_version": "0.1.19",
        "project_id": project_id,
        "workspace_ready": path.is_dir(),
        "capabilities": ["data.query", "data.analysis", "data.nl2sql", "knowledge.query"],
        "operations": {"run": True, "continue": True, "respond": True, "cancel": True},
        "interaction_kinds": ["permission_request"],
        "progress": "jsonl",
        "transport_scope": "local_loopback",
        "cli": cli_status,
    }


@router.get("/models")
async def worker_models(request: Request):
    _require_loopback_request(request)
    return {
        "schema_version": "1",
        "model_type": "analytics_model",
        "required": False,
        "selection": "backend_auto",
        "models": _model_options(),
    }


@router.post("/runs")
async def create_headless_run(
    request: HeadlessRunRequest,
    http_request: Request,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    stream: bool = Query(default=False),
):
    _require_loopback_request(http_request)
    request_received_at = time.time()
    caller_id, caller_name = _caller_identity(request.metadata)
    try:
        await headless_activity_log_store.record(
            source_id=caller_id,
            source_name=caller_name,
            query=request.message,
            created_at=request_received_at,
        )
    except Exception as exc:
        # Audit persistence must not make an otherwise valid Worker unavailable.
        logger.warning("Failed to persist Headless activity log: %s", type(exc).__name__)
    models = _model_options()
    key = _idempotency_key(request, idempotency_key)
    request_hash = _request_hash(request)
    previous = _reserve_idempotency(key, request_hash)
    if previous is not None:
        if previous.get("status") == "completed" and isinstance(previous.get("response"), dict):
            return previous["response"]
        raise HTTPException(status_code=409, detail="An identical Worker Run is already in progress")
    idempotency_finished = False
    streaming_lifecycle = False

    requested_session_id = str(request.session_id or "").strip()
    session_id = requested_session_id or f"worker-session-{uuid.uuid4().hex[:16]}"
    project_id = ""
    workspace_path: Path | None = None
    selected = ""
    model_route: AnalyticsModelRoute | None = None
    authority = headless_authority_from_environment()
    configured_mode = os.getenv("PUDDINGCLAW_HEADLESS_APPROVAL_MODE", "smart").strip().lower()
    if configured_mode not in {"strict", "smart"}:
        configured_mode = "smart"
    authority_profile = str(
        authority.get("profile")
        or "smart"
    ).strip().lower()
    approval_mode = configured_mode
    if not _claim_headless_session(session_id):
        _abandon_idempotency(key, request_hash)
        raise HTTPException(status_code=409, detail="Headless Session already has an active request")
    try:
        if requested_session_id:
            if not session_manager.session_exists(session_id):
                raise HTTPException(status_code=404, detail="Headless Session not found")
            metadata = session_manager.get_metadata(session_id)
            if not metadata.get("headless_enabled"):
                raise HTTPException(status_code=403, detail="Session is not a Headless CLI Session")
            bound_workspace = str(metadata.get("workspace_path") or "").strip()
            if request.workspace_path and bound_workspace:
                requested_path = Path(request.workspace_path).expanduser()
                if not requested_path.is_absolute() or requested_path.resolve() != Path(bound_workspace).expanduser().resolve():
                    raise HTTPException(status_code=409, detail="workspace_path cannot change for a continuous Session")
            project_id, workspace_path = _resolve_worker_project(request.workspace_path or bound_workspace or None)
            harness = session_manager.get_harness_state(session_id)
            active = [
                item
                for item in (harness.get("runs") or {}).values()
                if isinstance(item, dict) and item.get("status") not in TERMINAL_RUN_STATUSES
            ]
            if active or headless_session_has_pending_resume(session_id):
                raise HTTPException(status_code=409, detail="Session already has an active Run or pending input")
            ttl_seconds = headless_session_ttl_seconds()
            if ttl_seconds is not None and is_headless_session_expired(
                metadata,
                now=request_received_at,
                ttl_seconds=ttl_seconds,
            ):
                deleted = session_manager.delete_session_if_idle_headless_before(
                    session_id,
                    cutoff=request_received_at - ttl_seconds,
                    terminal_run_statuses=TERMINAL_RUN_STATUSES,
                )
                if deleted:
                    raise HTTPException(
                        status_code=status.HTTP_410_GONE,
                        detail="Headless Session expired after its configured inactivity TTL",
                    )
                raise HTTPException(status_code=409, detail="Headless Session changed while expiry was checked")
            session_manager.update_metadata(
                session_id,
                {
                    "workspace_path": str(workspace_path),
                    "project_id": project_id,
                    "session_source": "cli",
                },
            )
            approval_mode = str(metadata.get("approval_mode") or approval_mode)
            selected = str(metadata.get("analytics_model_id") or "").strip()
            allowed_ids = {str(item.get("id") or "") for item in models}
            if selected and selected not in allowed_ids:
                model_route = AnalyticsModelRoute(
                    "unmatched",
                    None,
                    1.0,
                    "session_bound",
                    "bound_model_no_longer_allowed",
                )
                response = _model_routing_needs_input(model_route, models)
                response["session_id"] = session_id
                _attach_session_lifecycle(response, session_id)
                if key:
                    _finish_idempotency(key, response)
                    idempotency_finished = True
                return response
            if selected:
                model_route = AnalyticsModelRoute(
                    "matched",
                    selected,
                    1.0,
                    "session_bound",
                    "continuous_session_model",
                )
            else:
                model_route = await _route_analytics_model(request.message)
                if model_route.status == "general":
                    selected = ""
                else:
                    selected = str(model_route.selected_id or "")
                    if model_route.status != "matched" or not selected:
                        response = _model_routing_needs_input(model_route, models)
                        response["session_id"] = session_id
                        _attach_session_lifecycle(response, session_id)
                        if key:
                            _finish_idempotency(key, response)
                            idempotency_finished = True
                        return response
                    session_manager.update_metadata(session_id, {"analytics_model_id": selected})
        else:
            project_id, workspace_path = _resolve_worker_project(request.workspace_path)
            model_route = await _route_analytics_model(request.message)
            if model_route.status == "general":
                selected = ""
            else:
                selected = str(model_route.selected_id or "")
                if model_route.status != "matched" or not selected:
                    response = _model_routing_needs_input(model_route, models)
                    if key:
                        _finish_idempotency(key, response)
                        idempotency_finished = True
                    return response
            session_manager.create_session(
                session_id,
                metadata={
                    "runtime_mode": "headless_worker",
                    "headless_enabled": True,
                    "worker_id": "puddingclaw",
                    "headless_caller_id": caller_id,
                    "headless_caller_name": caller_name,
                    "interaction_mode": "external",
                    "analytics_model_id": selected,
                    "workspace_path": str(workspace_path),
                    "project_id": project_id,
                    "session_source": "cli",
                },
                approval_mode=approval_mode,
            )

        _maybe_cleanup_stale_headless_sessions(now=request_received_at)
        assert workspace_path is not None
        assert model_route is not None
        if stream is True:
            # Do not wait for the first Agent boundary here.  Starting the
            # consumer task and returning the response immediately is what
            # makes preflight, tool, reasoning and token events observable to
            # CLI/Teams instead of replaying them after the Run completes.
            execution = await _start_headless_execution(
                request=request,
                session_id=session_id,
                project_id=project_id,
                approval_mode=approval_mode,
                authority={**authority, "profile": authority_profile},
                request_received_at=request_received_at,
                analytics_model_id=selected,
                analytics_model_match=model_route.to_dict(),
            )
            streaming_lifecycle = True

            if key:
                async def finish_idempotency_at_boundary() -> None:
                    try:
                        await execution.wait_for_boundary()
                        result = execution.response()
                        _attach_session_lifecycle(result, session_id)
                        _finish_idempotency(key, result)
                    except BaseException:
                        # A failed/cancelled execution must not leave a
                        # permanent "running" reservation after its observer
                        # disconnects.
                        _abandon_idempotency(key, request_hash)

                asyncio.create_task(finish_idempotency_at_boundary())

            async def event_stream():
                nonlocal idempotency_finished
                terminal_response: dict[str, Any] | None = None
                try:
                    async for item in _stream_headless_execution(execution):
                        if item.get("event") == "result" and isinstance(item.get("data"), dict):
                            result = dict(item["data"])
                            _attach_session_lifecycle(result, session_id)
                            item = {"event": "result", "data": result}
                            terminal_response = result
                        yield f"{json.dumps(item, ensure_ascii=False)}\n"
                    if key:
                        _finish_idempotency(key, terminal_response or execution.response())
                        idempotency_finished = True
                finally:
                    # This lock guards concurrent HTTP mutations, not the
                    # lifetime of the Harness Run. Active/pending Run state is
                    # checked separately, so release it at every stream
                    # boundary (terminal, HITL, disconnect or cancellation).
                    _release_headless_session(session_id)

            return StreamingResponse(
                event_stream(),
                media_type="application/x-ndjson",
                headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
            )
        try:
            response = await _consume_run(
                request=request,
                session_id=session_id,
                project_id=project_id,
                approval_mode=approval_mode,
                authority={**authority, "profile": authority_profile},
                request_received_at=request_received_at,
                analytics_model_id=selected,
                analytics_model_match=model_route.to_dict(),
            )
        except asyncio.TimeoutError:
            response = {
                "schema_version": "1",
                "session_id": session_id,
                "analytics_model_id": selected,
                "analytics_model_match": model_route.to_dict(),
                "status": "failed",
                "outcome": "timeout",
                "needs_input": None,
            }
        _attach_session_lifecycle(response, session_id)
        if key:
            _finish_idempotency(key, response)
            idempotency_finished = True
        if response.get("outcome") == "timeout":
            return JSONResponse(status_code=status.HTTP_504_GATEWAY_TIMEOUT, content=response)
        return response
    finally:
        if key and not idempotency_finished and not streaming_lifecycle:
            _abandon_idempotency(key, request_hash)
        if not streaming_lifecycle:
            _release_headless_session(session_id)


@router.post("/runs/{run_id}/resume")
async def resume_headless_run(
    run_id: str,
    request: HeadlessResumeRequest,
    http_request: Request,
    stream: bool = Query(default=False),
):
    """Resolve external approval and continue the exact suspended Run."""

    _require_loopback_request(http_request)
    with _headless_executions_lock:
        execution = next(
            (
                item
                for item in _headless_executions.values()
                if item.run_id == run_id
            ),
            None,
        )
    if execution is None:
        raise HTTPException(
            status_code=status.HTTP_410_GONE,
            detail="Headless continuation is no longer active; start a new Run",
        )
    if not hmac.compare_digest(execution.token, request.continuation_token):
        raise HTTPException(status_code=403, detail="Headless continuation token is invalid")
    if request.workspace_path:
        requested_path = Path(request.workspace_path).expanduser()
        if not requested_path.is_absolute():
            raise HTTPException(status_code=400, detail="workspace_path must be an absolute host path")
        try:
            bound_path = project_registry.resolve(execution.project_id)
        except (KeyError, FileNotFoundError) as exc:
            raise HTTPException(status_code=410, detail="Headless workspace is no longer registered") from exc
        if requested_path.resolve() != bound_path.resolve():
            raise HTTPException(status_code=409, detail="workspace_path does not match the active Run")
    request_id = str(request.request_id or "").strip()
    request_hash = _resume_request_hash(request)
    if request_id:
        cached = execution.resume_results.get(request_id)
        if cached is not None:
            cached_hash, cached_response = cached
            if not hmac.compare_digest(cached_hash, request_hash):
                raise HTTPException(status_code=409, detail="request_id was already used with another response")
            return cached_response
    if execution.done:
        response = execution.response()
        _attach_session_lifecycle(response, execution.session_id)
        return response
    pending_ids = set(execution.pending_inputs)
    decision_ids = {item.request_id for item in request.decisions}
    if not pending_ids:
        raise HTTPException(status_code=409, detail="Run is not waiting for external input")
    if decision_ids != pending_ids:
        raise HTTPException(
            status_code=409,
            detail="Decisions must resolve the complete current interrupt set",
        )
    unsupported = [
        request_id
        for request_id in pending_ids
        if execution.pending_inputs[request_id].get("type") != "permission_request"
    ]
    if unsupported:
        raise HTTPException(
            status_code=422,
            detail="This CLI version can only resume permission requests",
        )

    if not _claim_headless_session(execution.session_id):
        raise HTTPException(status_code=409, detail="Headless Session already has an active resume request")
    streaming_lifecycle = False
    try:
        previous_revision = execution.revision
        previous_sequence = execution.event_sequence
        for decision in request.decisions:
            await _resolve_external_permission(
                session_id=execution.session_id,
                decision=decision,
            )
        if stream is True:
            streaming_lifecycle = True

            if request_id:
                async def cache_resume_at_boundary() -> None:
                    try:
                        await execution.wait_for_boundary(after_revision=previous_revision)
                        result = execution.response()
                        _attach_session_lifecycle(result, execution.session_id)
                        execution.resume_results[request_id] = (request_hash, result)
                    except BaseException:
                        # The next retry will receive the execution's real
                        # terminal/pending state instead of a false cached one.
                        return

                asyncio.create_task(cache_resume_at_boundary())

            async def event_stream():
                terminal_response: dict[str, Any] | None = None
                try:
                    async for item in _stream_headless_execution(
                        execution,
                        after_seq=previous_sequence,
                    ):
                        if item.get("event") == "result" and isinstance(item.get("data"), dict):
                            result = dict(item["data"])
                            _attach_session_lifecycle(result, execution.session_id)
                            item = {"event": "result", "data": result}
                            terminal_response = result
                        yield f"{json.dumps(item, ensure_ascii=False)}\n"
                    if request_id and terminal_response is not None:
                        execution.resume_results[request_id] = (request_hash, terminal_response)
                finally:
                    _release_headless_session(execution.session_id)

            return StreamingResponse(
                event_stream(),
                media_type="application/x-ndjson",
                headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
            )
        await execution.wait_for_boundary(after_revision=previous_revision)
        response = execution.response()
        _attach_session_lifecycle(response, execution.session_id)
        if request_id:
            execution.resume_results[request_id] = (request_hash, response)
        return response
    except asyncio.TimeoutError:
        response = {
            "schema_version": "1",
            "run_id": execution.run_id or run_id,
            "session_id": execution.session_id,
            "status": "failed",
            "outcome": "timeout",
            "needs_input": None,
        }
        _attach_session_lifecycle(response, execution.session_id)
        if request_id:
            execution.resume_results[request_id] = (request_hash, response)
        return JSONResponse(status_code=status.HTTP_504_GATEWAY_TIMEOUT, content=response)
    finally:
        if not streaming_lifecycle:
            _release_headless_session(execution.session_id)


@router.post("/runs/{run_id}/cancel")
async def cancel_headless_run(
    run_id: str,
    request: Request,
):
    """Cancel a live Headless Run while retaining its Session."""

    _require_loopback_request(request)
    with _headless_executions_lock:
        execution = next(
            (
                item
                for item in _headless_executions.values()
                if item.run_id == run_id or item.session_id == run_id
            ),
            None,
        )
    if execution is None:
        raise HTTPException(
            status_code=status.HTTP_410_GONE,
            detail="Headless Run is no longer active",
        )
    # Cancellation is an out-of-band control operation. It must be able to
    # interrupt an active streaming create/resume request instead of waiting
    # for that request's mutation lock to be released.
    await execution.cancel()
    response = execution.response()
    _attach_session_lifecycle(response, execution.session_id)
    return response


@headless_activity_router.get("/headless-activity-logs")
async def list_headless_activity_logs(
    request: Request,
    page: int = Query(default=1, ge=1),
    source_name: str | None = Query(default=None, max_length=120),
    query_keyword: str | None = Query(default=None, alias="query", max_length=200),
    start_at: float | None = Query(default=None, ge=0),
    end_at: float | None = Query(default=None, ge=0),
):
    _require_loopback_request(request)
    if start_at is not None and end_at is not None and start_at > end_at:
        raise HTTPException(status_code=422, detail="start_at must not be later than end_at")
    result = await headless_activity_log_store.list(
        page=page,
        page_size=10,
        source_name=source_name,
        query=query_keyword,
        start_at=start_at,
        end_at=end_at,
    )
    result["items"] = [
        {
            "id": item["id"],
            "source_id": item["source_id"],
            "source_name": item["source_name"],
            "query": item["query"],
            "created_at": item["created_at"],
            "created_at_beijing": datetime.fromtimestamp(
                float(item["created_at"]),
                tz=_BEIJING_TIMEZONE,
            ).strftime("%Y-%m-%d %H:%M:%S"),
        }
        for item in result["items"]
    ]
    result["timezone"] = "Asia/Shanghai"
    return result
