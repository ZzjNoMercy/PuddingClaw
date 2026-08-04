"""Authenticated synchronous Worker API for unattended PuddingClaw Runs."""

from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import json
import os
import re
import threading
import time
import uuid
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException, Request, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, model_validator

from analytics.models import get_analytics_model_registry
from cli_runtime import current_cli_runtime_status
from graph.deepagents_manager import deepagents_agent_manager
from graph.headless_resolver import headless_authority_from_environment
from graph.session_manager import session_manager
from projects.registry import project_registry
from worker_access import WORKER_SCOPES, WorkerAccessError, worker_access_store

router = APIRouter(prefix="/headless", tags=["headless-worker"])
worker_access_router = APIRouter(tags=["worker-access-keys"])
BASE_DIR = Path(__file__).resolve().parent.parent
_IDENTITY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_idempotency_lock = threading.RLock()


class HeadlessRunRequest(BaseModel):
    message: str = Field(min_length=1, max_length=100_000)
    analytics_model_id: str | None = Field(default=None, max_length=200)
    session_id: str | None = Field(default=None, max_length=200)
    metadata: dict[str, Any] = Field(default_factory=dict)
    request_id: str | None = Field(default=None, max_length=200)

    @model_validator(mode="after")
    def validate_message(self):
        if not self.message.strip():
            raise ValueError("message must not be empty")
        return self


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
    snapshot = get_analytics_model_registry(BASE_DIR).list_models()
    allowed = {str(item) for item in principal.get("allowed_analytics_models") or [] if str(item).strip()}
    models = [_safe_model(item) for item in snapshot.get("models") or [] if isinstance(item, dict)]
    if allowed:
        models = [item for item in models if str(item.get("id")) in allowed]
    return models


def _model_binding(model_id: str) -> dict[str, Any]:
    model = get_analytics_model_registry(BASE_DIR).get_model(model_id)
    body = str(model.get("body") or "").encode("utf-8")
    return {
        "id": model_id,
        "version": str(model.get("version") or ""),
        "content_sha256": hashlib.sha256(body).hexdigest(),
    }


def _platform_id() -> str:
    value = os.getenv("PUDDING_PLATFORM_ID", "puddingteams").strip()
    if not _IDENTITY_RE.fullmatch(value) or value in {".", ".."}:
        raise HTTPException(status_code=503, detail="PUDDING_PLATFORM_ID is invalid")
    return value


def _projects_root(platform_id: str) -> Path:
    configured = os.getenv("PUDDINGCLAW_PROJECTS_ROOT", "").strip()
    return (Path(configured).expanduser() if configured else Path.home()).resolve() / platform_id


def _ensure_worker_project(platform_id: str) -> tuple[str, Path]:
    path = _projects_root(platform_id)
    try:
        path.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise HTTPException(status_code=503, detail="Worker projects root is unavailable") from exc
    try:
        record = project_registry.register(str(path), name=platform_id)
    except (FileNotFoundError, NotADirectoryError, KeyError) as exc:
        raise HTTPException(status_code=503, detail="Worker project is unavailable") from exc
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
    data_dir = BASE_DIR / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    return data_dir / "headless-idempotency.json"


def _idempotency_key(request: HeadlessRunRequest, header: str | None) -> str:
    return str(header or request.request_id or "").strip()


def _request_hash(request: HeadlessRunRequest, *, principal: dict[str, Any], platform_id: str) -> str:
    payload = {
        "message": request.message,
        "analytics_model_id": request.analytics_model_id,
        "session_id": request.session_id,
        "metadata": request.metadata,
        "platform_id": platform_id,
        "key_id": principal.get("key_id"),
    }
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str).encode()).hexdigest()


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


def _needs_input(event_name: str, payload: dict[str, Any]) -> dict[str, Any] | None:
    mapping = {
        "user_input_required": "user_input",
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
    if input_type == "user_input":
        result["questions"] = payload.get("questions") or []
    return result


async def _consume_run(
    *,
    request: HeadlessRunRequest,
    session_id: str,
    project_id: str,
    platform_id: str,
    approval_mode: str,
    authority: dict[str, Any],
) -> dict[str, Any]:
    final_content = ""
    outcome: dict[str, Any] = {}
    verification: dict[str, Any] = {"status": "not_required", "summary": ""}
    needs_input: dict[str, Any] | None = None
    stream = deepagents_agent_manager.astream(
        message=request.message,
        session_id=session_id,
        project_id=project_id,
        analytics_model_id=request.analytics_model_id,
        analytics_model_snapshot=_model_binding(str(request.analytics_model_id)),
        user_id="worker",
        interaction_mode="auto",
        authority_profile=str(authority.get("profile") or "workspace"),
        authority_directories=list(authority.get("directories") or []),
        authority_network_origins=list(authority.get("network_origins") or []),
    )
    try:
        timeout_s = max(1.0, float(os.getenv("PUDDINGCLAW_TIMEOUT_S", "600")))
        async with asyncio.timeout(timeout_s):
            async for event in stream:
                name = str(event.get("event") or "")
                try:
                    payload = json.loads(event.get("data") or "{}")
                except (TypeError, ValueError):
                    payload = {}
                if not isinstance(payload, dict):
                    payload = {}
                if name == "run_outcome":
                    outcome = payload
                elif name == "verification_report":
                    report = payload.get("report") if isinstance(payload.get("report"), dict) else {}
                    verification = {
                        "status": report.get("status") or "not_required",
                        "summary": report.get("explanation") or "",
                    }
                elif name == "done":
                    final_content = str(payload.get("content") or "")
                elif name.endswith("_required"):
                    needs_input = _needs_input(name, payload) or needs_input
    finally:
        await stream.aclose()
    final_outcome = str(outcome.get("outcome") or "failed")
    status_value = str(outcome.get("status") or final_outcome)
    return {
        "schema_version": "1",
        "run_id": outcome.get("run_id"),
        "session_id": session_id,
        "project_id": project_id,
        "platform_id": platform_id,
        "analytics_model_id": request.analytics_model_id,
        "approval_mode": approval_mode,
        "status": status_value,
        "outcome": final_outcome,
        "reply": final_content,
        "verification": verification,
        "budget_exhaustion_reason": outcome.get("budget_exhaustion_reason"),
        "model_call_count": outcome.get("model_call_count", 0),
        "auto_resolved": outcome.get("auto_resolved") or [],
        "interrupt_summary": outcome.get("interrupt_summary") or {"total": 0, "auto_approved": 0, "auto_rejected": 0, "by_type": {}},
        "needs_input": needs_input,
    }


@router.get("/health")
async def worker_health(authorization: str | None = Header(default=None)):
    principal = _principal_for_scope(authorization, "worker:health")
    platform_id = _platform_id()
    project_id, path = _ensure_worker_project(platform_id)
    cli_status = current_cli_runtime_status(BASE_DIR)
    cli_status.pop("path", None)
    cli_status.pop("package_dir", None)
    return {
        "schema_version": "1",
        "agent_id": "puddingclaw",
        "cli_version": "0.1.0",
        "protocol_version": "1",
        "configured": True,
        "authenticated": True,
        "reachable": True,
        "server_version": "0.1.0",
        "platform_id": platform_id,
        "project_id": project_id,
        "workspace_ready": path.is_dir(),
        "capabilities": ["data.query", "data.analysis", "data.nl2sql", "knowledge.query"],
        "key_id": principal.get("key_id"),
        "cli": cli_status,
    }


@router.get("/models")
async def worker_models(authorization: str | None = Header(default=None)):
    principal = _principal_for_scope(authorization, "worker:models:read")
    return {
        "schema_version": "1",
        "model_type": "analytics_model",
        "required": True,
        "models": _model_options(principal),
    }


@router.post("/runs")
async def create_headless_run(
    request: HeadlessRunRequest,
    authorization: str | None = Header(default=None),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
):
    principal = _principal_for_scope(authorization, "worker:runs:create")
    platform_id = _platform_id()
    models = _model_options(principal)
    selected = str(request.analytics_model_id or "").strip()
    if not selected or not any(str(item.get("id")) == selected for item in models):
        response = {
            "schema_version": "1",
            "status": "needs_input",
            "outcome": "model_selection_required",
            "needs_input": {
                "type": "analytics_model_selection",
                "prompt": "请选择 PuddingClaw 分析模型",
                "options": models,
            },
        }
        return response
    key = _idempotency_key(request, idempotency_key)
    previous = _reserve_idempotency(key, _request_hash(request, principal=principal, platform_id=platform_id))
    if previous is not None:
        if previous.get("status") == "completed" and isinstance(previous.get("response"), dict):
            return previous["response"]
        raise HTTPException(status_code=409, detail="An identical Worker Run is already in progress")

    requested_session_id = str(request.session_id or "").strip()
    session_id = requested_session_id or f"worker-session-{uuid.uuid4().hex[:16]}"
    authority = headless_authority_from_environment()
    configured_mode = os.getenv("PUDDINGCLAW_HEADLESS_APPROVAL_MODE", "smart").strip().lower()
    if configured_mode not in {"smart", "full_access"}:
        configured_mode = "smart"
    authority_profile = str(principal.get("authority_profile") or "smart").strip().lower()
    approval_mode = "full_access" if configured_mode == "full_access" and authority_profile != "smart" else "smart"
    if not requested_session_id:
        session_manager.create_session(
            session_id,
            metadata={
                "runtime_mode": "headless_worker",
                "headless_enabled": True,
                "worker_id": "puddingclaw",
                "worker_key_id": principal.get("key_id"),
                "platform_id": platform_id,
                "interaction_mode": "auto",
                "analytics_model_id": selected,
            },
            approval_mode=approval_mode,
        )
    else:
        if not session_manager.session_exists(session_id):
            raise HTTPException(status_code=404, detail="Headless Session not found")
        metadata = session_manager.get_metadata(session_id)
        if not metadata.get("headless_enabled") or metadata.get("worker_key_id") != principal.get("key_id"):
            raise HTTPException(status_code=403, detail="Session is not authorized for this Worker")
        harness = session_manager.get_harness_state(session_id)
        active = [item for item in (harness.get("runs") or {}).values() if isinstance(item, dict) and item.get("status") not in {"completed", "cancelled", "failed", "blocked", "budget_exceeded", "verification_failed"}]
        if active:
            raise HTTPException(status_code=409, detail="Session already has an active Run")
        approval_mode = str(metadata.get("approval_mode") or approval_mode)
    project_id, _workspace_path = _ensure_worker_project(platform_id)
    try:
        response = await _consume_run(
            request=request,
            session_id=session_id,
            project_id=project_id,
            platform_id=platform_id,
            approval_mode=approval_mode,
            authority={**authority, "profile": authority_profile if approval_mode == "full_access" else "smart"},
        )
    except asyncio.TimeoutError:
        response = {
            "schema_version": "1",
            "session_id": session_id,
            "platform_id": platform_id,
            "status": "failed",
            "outcome": "timeout",
            "needs_input": None,
        }
        if key:
            _finish_idempotency(key, response)
        return JSONResponse(status_code=status.HTTP_504_GATEWAY_TIMEOUT, content=response)
    if key:
        _finish_idempotency(key, response)
    return response


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
