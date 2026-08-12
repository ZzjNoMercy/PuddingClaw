"""Authenticated synchronous Worker API for unattended PuddingClaw Runs."""

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
from worker_access import WorkerAccessError, worker_access_log_store, worker_access_store

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/headless", tags=["headless-worker"])
worker_access_router = APIRouter(tags=["worker-access-keys"])
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
        worker_key_id: str,
    ) -> None:
        self.stream = stream
        self.session_id = session_id
        self.project_id = project_id
        self.approval_mode = approval_mode
        self.analytics_model_id = analytics_model_id
        self.analytics_model_match = dict(analytics_model_match)
        self.worker_key_id = worker_key_id
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
        self.events: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue()
        self.error: BaseException | None = None
        self.updated = asyncio.Condition()
        self.task: asyncio.Task[None] | None = None

    def start(self) -> None:
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
                self.events.put_nowait({"event": name, "data": payload})
                self.run_id = str(payload.get("run_id") or self.run_id)
                self.query_id = str(payload.get("query_id") or self.query_id)
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
                        await self._signal()
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
            self.events.put_nowait(None)
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


class WorkerAccessKeyCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    scopes: list[str] | None = None
    allowed_analytics_models: list[str] = Field(default_factory=list, max_length=500)
    authority_profile: str = "smart"
    expires_at: float | None = None


def _safe_model(item: dict[str, Any]) -> dict[str, Any]:
    return {
        key: item.get(key)
        for key in ("id", "name", "description", "version", "tags")
    }


def _model_options(principal: dict[str, Any]) -> list[dict[str, Any]]:
    from runtime_identity.paths import PuddingClawPaths

    snapshot = get_analytics_model_registry(PuddingClawPaths.from_environment().user_definitions()).list_models()
    allowed = {str(item) for item in principal.get("allowed_analytics_models") or [] if str(item).strip()}
    models = [_safe_model(item) for item in snapshot.get("models") or [] if isinstance(item, dict)]
    if allowed:
        models = [item for item in models if str(item.get("id")) in allowed]
    return models


def _model_routing_candidates(principal: dict[str, Any]) -> list[dict[str, Any]]:
    """Return only allowed models, enriched with bounded routing guidance."""

    from runtime_identity.paths import PuddingClawPaths

    registry = get_analytics_model_registry(PuddingClawPaths.from_environment().user_definitions())
    candidates: list[dict[str, Any]] = []
    for option in _model_options(principal):
        candidate = dict(option)
        try:
            detail = registry.get_model(str(option.get("id") or ""))
            candidate["applicability"] = str(detail.get("body") or "")[:4_000]
        except Exception:
            # Registry summaries still provide a useful fail-closed candidate.
            candidate["applicability"] = ""
        candidates.append(candidate)
    return candidates


async def _route_analytics_model(message: str, principal: dict[str, Any]) -> AnalyticsModelRoute:
    """Resolve one allowed Analytics Model without giving the CLI selection authority."""

    candidates = _model_routing_candidates(principal)
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
        prompt = "当前 Worker Key 没有可用的分析模型，请联系管理员配置模型权限。"
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


def _principal_for_scope(
    authorization: str | None,
    scope: str,
) -> dict[str, Any]:
    scheme, _, token = str(authorization or "").partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        raise HTTPException(status_code=401, detail="Worker Access Key required", headers={"WWW-Authenticate": "Bearer"})
    principal = worker_access_store.authenticate(token, scope)
    if principal is None:
        raise HTTPException(status_code=403, detail="Worker Access Key is invalid, revoked, expired, or out of scope")
    return principal


def _is_loopback_request(request: Request) -> bool:
    host = str(request.client.host if request.client else "").strip().lower()
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _admin(request: Request) -> None:
    configured = os.getenv("PUDDINGCLAW_ADMIN_TOKEN", "").strip()
    supplied = request.headers.get("x-puddingclaw-admin-key", "")
    if not configured:
        if _is_loopback_request(request):
            return
        raise HTTPException(
            status_code=503,
            detail="Remote Worker Access Key administration requires PUDDINGCLAW_ADMIN_TOKEN",
        )
    import hmac

    if not hmac.compare_digest(supplied, configured):
        raise HTTPException(status_code=403, detail="Worker administrator authentication failed")


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


def _request_hash(request: HeadlessRunRequest, *, principal: dict[str, Any]) -> str:
    payload = {
        "message": request.message,
        "session_id": request.session_id,
        "workspace_path": request.workspace_path,
        "metadata": request.metadata,
        "key_id": principal.get("key_id"),
    }
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str).encode()).hexdigest()


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
    worker_key_id: str = "",
) -> dict[str, Any]:
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
        authority_profile=str(authority.get("profile") or "workspace"),
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
        worker_key_id=worker_key_id,
    )
    _prune_headless_executions()
    with _headless_executions_lock:
        _headless_executions[session_id] = execution
    execution.start()
    try:
        await execution.wait_for_boundary()
    except BaseException:
        await execution.cancel()
        raise
    return execution.response()


async def _stream_headless_execution(
    execution: _HeadlessExecution,
    initial_response: dict[str, Any],
):
    """Yield live Agent events, then one canonical result event."""

    if execution.pending_inputs and not execution.done:
        yield {"event": "result", "data": initial_response}
        return
    while True:
        item = await execution.events.get()
        if item is None:
            break
        yield item
        if execution.pending_inputs and not execution.done:
            yield {"event": "result", "data": execution.response()}
            return
    yield {"event": "result", "data": execution.response()}


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
async def worker_health(authorization: str | None = Header(default=None)):
    principal = _principal_for_scope(authorization, "worker:health")
    _maybe_cleanup_stale_headless_sessions()
    project_id, path = _ensure_worker_project()
    cli_status = current_cli_runtime_status(BASE_DIR)
    cli_status.pop("path", None)
    cli_status.pop("package_dir", None)
    return {
        "schema_version": "1",
        "agent_id": "puddingclaw",
        "cli_version": "0.2.0",
        "protocol_version": "1",
        "configured": True,
        "authenticated": True,
        "reachable": True,
        "server_version": "0.1.0",
        "project_id": project_id,
        "workspace_ready": path.is_dir(),
        "capabilities": ["data.query", "data.analysis", "data.nl2sql", "knowledge.query"],
        "operations": {"run": True, "continue": True, "respond": True, "cancel": True},
        "interaction_kinds": ["permission_request"],
        "progress": "jsonl",
        "key_id": principal.get("key_id"),
        "cli": cli_status,
    }


@router.get("/models")
async def worker_models(authorization: str | None = Header(default=None)):
    principal = _principal_for_scope(authorization, "worker:models:read")
    return {
        "schema_version": "1",
        "model_type": "analytics_model",
        "required": False,
        "selection": "backend_auto",
        "models": _model_options(principal),
    }


@router.post("/runs")
async def create_headless_run(
    request: HeadlessRunRequest,
    authorization: str | None = Header(default=None),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    stream: bool = Query(default=False),
):
    request_received_at = time.time()
    principal = _principal_for_scope(authorization, "worker:runs:create")
    try:
        await worker_access_log_store.record(
            key_id=str(principal.get("key_id") or "unknown-worker-key"),
            key_name=str(principal.get("name") or principal.get("key_id") or "Unknown Worker Key"),
            query=request.message,
            created_at=request_received_at,
        )
    except Exception as exc:
        # Audit persistence must not make an otherwise valid Worker unavailable.
        logger.warning("Failed to persist Worker access log: %s", type(exc).__name__)
    models = _model_options(principal)
    key = _idempotency_key(request, idempotency_key)
    request_hash = _request_hash(request, principal=principal)
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
    authority_profile = str(principal.get("authority_profile") or "smart").strip().lower()
    approval_mode = configured_mode
    if not _claim_headless_session(session_id):
        _abandon_idempotency(key, request_hash)
        raise HTTPException(status_code=409, detail="Headless Session already has an active request")
    try:
        if requested_session_id:
            if not session_manager.session_exists(session_id):
                raise HTTPException(status_code=404, detail="Headless Session not found")
            metadata = session_manager.get_metadata(session_id)
            if not metadata.get("headless_enabled") or metadata.get("worker_key_id") != principal.get("key_id"):
                raise HTTPException(status_code=403, detail="Session is not authorized for this Worker")
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
                model_route = await _route_analytics_model(request.message, principal)
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
            if metadata.get("workspace_path") != str(workspace_path) or metadata.get("project_id") != project_id:
                session_manager.update_metadata(
                    session_id,
                    {"workspace_path": str(workspace_path), "project_id": project_id},
                )
        else:
            project_id, workspace_path = _resolve_worker_project(request.workspace_path)
            model_route = await _route_analytics_model(request.message, principal)
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
                    "worker_key_id": principal.get("key_id"),
                    "interaction_mode": "external",
                    "analytics_model_id": selected,
                    "workspace_path": str(workspace_path),
                    "project_id": project_id,
                },
                approval_mode=approval_mode,
            )

        _maybe_cleanup_stale_headless_sessions(now=request_received_at)
        assert workspace_path is not None
        assert model_route is not None
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
                worker_key_id=str(principal.get("key_id") or ""),
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
        if stream is True and response.get("run_id"):
            with _headless_executions_lock:
                execution = next(
                    (
                        item
                        for item in _headless_executions.values()
                        if item.run_id == str(response.get("run_id"))
                    ),
                    None,
                )
            if execution is not None:
                streaming_lifecycle = True

                async def event_stream():
                    nonlocal idempotency_finished
                    terminal_response = response
                    try:
                        async for item in _stream_headless_execution(execution, response):
                            if item.get("event") == "result" and isinstance(item.get("data"), dict):
                                terminal_response = item["data"]
                            yield f"{json.dumps(item, ensure_ascii=False)}\n"
                        if key:
                            _finish_idempotency(key, terminal_response)
                            idempotency_finished = True
                    finally:
                        if execution.done:
                            _release_headless_session(session_id)

                return StreamingResponse(event_stream(), media_type="application/x-ndjson")
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
    authorization: str | None = Header(default=None),
):
    """Resolve external approval and continue the exact suspended Run."""

    principal = _principal_for_scope(authorization, "worker:runs:create")
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
    if execution.worker_key_id != str(principal.get("key_id") or ""):
        raise HTTPException(status_code=403, detail="Run is not authorized for this Worker")
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
    try:
        previous_revision = execution.revision
        for decision in request.decisions:
            await _resolve_external_permission(
                session_id=execution.session_id,
                decision=decision,
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
        _release_headless_session(execution.session_id)


@router.post("/runs/{run_id}/cancel")
async def cancel_headless_run(
    run_id: str,
    authorization: str | None = Header(default=None),
):
    """Cancel a live Headless Run while retaining its Session."""

    principal = _principal_for_scope(authorization, "worker:runs:cancel")
    with _headless_executions_lock:
        execution = next(
            (item for item in _headless_executions.values() if item.run_id == run_id),
            None,
        )
    if execution is None:
        raise HTTPException(
            status_code=status.HTTP_410_GONE,
            detail="Headless Run is no longer active",
        )
    if execution.worker_key_id != str(principal.get("key_id") or ""):
        raise HTTPException(status_code=403, detail="Run is not authorized for this Worker")
    if not _claim_headless_session(execution.session_id):
        raise HTTPException(status_code=409, detail="Headless Session already has an active request")
    try:
        await execution.cancel()
        response = execution.response()
        _attach_session_lifecycle(response, execution.session_id)
        return response
    finally:
        _release_headless_session(execution.session_id)


@router.post("/access-keys")
async def create_worker_access_key(request: Request, payload: WorkerAccessKeyCreateRequest):
    _admin(request)
    try:
        public, token = worker_access_store.create(**payload.model_dump())
    except WorkerAccessError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {**public, "token": token}


@router.get("/access-keys")
async def list_worker_access_keys(request: Request):
    _admin(request)
    return {"keys": worker_access_store.list_public()}


@worker_access_router.get("/worker-access-logs")
async def list_worker_access_logs(
    request: Request,
    page: int = Query(default=1, ge=1),
    key_name: str | None = Query(default=None, max_length=120),
    query_keyword: str | None = Query(default=None, alias="query", max_length=200),
    start_at: float | None = Query(default=None, ge=0),
    end_at: float | None = Query(default=None, ge=0),
):
    _admin(request)
    if start_at is not None and end_at is not None and start_at > end_at:
        raise HTTPException(status_code=422, detail="start_at must not be later than end_at")
    result = await worker_access_log_store.list(
        page=page,
        page_size=10,
        key_name=key_name,
        query=query_keyword,
        start_at=start_at,
        end_at=end_at,
    )
    result["items"] = [
        {
            **item,
            "created_at_beijing": datetime.fromtimestamp(
                float(item["created_at"]),
                tz=_BEIJING_TIMEZONE,
            ).strftime("%Y-%m-%d %H:%M:%S"),
        }
        for item in result["items"]
    ]
    result["timezone"] = "Asia/Shanghai"
    return result


@router.post("/access-keys/{key_id}/rotate")
async def rotate_worker_access_key(key_id: str, request: Request):
    _admin(request)
    try:
        public, token = worker_access_store.rotate(key_id)
    except WorkerAccessError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {**public, "token": token}


@router.delete("/access-keys/{key_id}")
async def revoke_worker_access_key(key_id: str, request: Request):
    _admin(request)
    try:
        worker_access_store.revoke(key_id)
    except WorkerAccessError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {"status": "revoked", "key_id": key_id}


# Stable management paths from the Worker contract. The /headless/access-keys
# aliases above remain useful for local discovery, while these paths avoid
# making callers know the implementation grouping of the runtime router.
@worker_access_router.post("/worker-access-keys")
async def create_worker_access_key_contract(request: Request, payload: WorkerAccessKeyCreateRequest):
    return await create_worker_access_key(request, payload)


@worker_access_router.get("/worker-access-keys")
async def list_worker_access_keys_contract(request: Request):
    return await list_worker_access_keys(request)


@worker_access_router.post("/worker-access-keys/{key_id}/rotate")
async def rotate_worker_access_key_contract(key_id: str, request: Request):
    return await rotate_worker_access_key(key_id, request)


@worker_access_router.delete("/worker-access-keys/{key_id}")
async def revoke_worker_access_key_contract(key_id: str, request: Request):
    return await revoke_worker_access_key(key_id, request)
