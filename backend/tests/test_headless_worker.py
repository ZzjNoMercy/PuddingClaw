from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

import pytest
from fastapi import HTTPException
from starlette.requests import Request

from api import headless as headless_api
from api.headless import (
    HeadlessRunRequest,
    _admin,
    _consume_run,
    _ensure_worker_project,
    create_headless_run,
)
from graph.headless_resolver import HeadlessInterruptResolver
from graph.permission_resume import permission_resume_registry
from graph.session_manager import session_manager
from headless_session_lifecycle import cleanup_stale_headless_sessions
from projects.registry import project_registry
from worker_access import WorkerAccessStore


def _request_from(host: str) -> Request:
    return Request({
        "type": "http",
        "method": "GET",
        "path": "/api/worker-access-keys",
        "headers": [],
        "client": (host, 1234),
        "server": ("127.0.0.1", 8888),
    })


def test_local_worker_key_management_does_not_require_admin_token(monkeypatch):
    monkeypatch.delenv("PUDDINGCLAW_ADMIN_TOKEN", raising=False)
    _admin(_request_from("127.0.0.1"))


def test_remote_worker_key_management_requires_admin_token(monkeypatch):
    monkeypatch.delenv("PUDDINGCLAW_ADMIN_TOKEN", raising=False)
    with pytest.raises(HTTPException) as error:
        _admin(_request_from("192.0.2.10"))
    assert error.value.status_code == 503


def test_worker_project_has_internal_identity(tmp_path, monkeypatch):
    monkeypatch.setenv("PUDDINGCLAW_PROJECTS_ROOT", str(tmp_path))
    project_registry.initialize(tmp_path)
    _project_id, path = _ensure_worker_project()
    assert path == tmp_path / "puddingclaw"


def _set_session_updated_at(session_id: str, updated_at: float) -> None:
    path = session_manager._session_path(session_id)
    data = json.loads(path.read_text(encoding="utf-8"))
    data["updated_at"] = updated_at
    path.write_text(json.dumps(data), encoding="utf-8")


def test_headless_ttl_cleanup_never_deletes_normal_fresh_or_active_sessions(tmp_path: Path):
    session_manager.initialize(tmp_path)
    now = 2_000_000.0
    stale_at = now - 86_401

    session_manager.create_session("normal-session")
    _set_session_updated_at("normal-session", stale_at)

    session_manager.create_session(
        "fresh-headless",
        metadata={"runtime_mode": "headless_worker", "headless_enabled": True, "worker_key_id": "key-1"},
    )

    session_manager.create_session(
        "active-headless",
        metadata={"runtime_mode": "headless_worker", "headless_enabled": True, "worker_key_id": "key-1"},
    )
    active_path = session_manager._session_path("active-headless")
    active = json.loads(active_path.read_text(encoding="utf-8"))
    active["updated_at"] = stale_at
    active["harness"] = {"runs": {"run-active": {"run_id": "run-active", "status": "running"}}}
    active_path.write_text(json.dumps(active), encoding="utf-8")

    session_manager.create_session(
        "stale-headless",
        metadata={"runtime_mode": "headless_worker", "headless_enabled": True, "worker_key_id": "key-1"},
    )
    _set_session_updated_at("stale-headless", stale_at)
    trace_path = session_manager._trace_path("stale-headless")
    trace_path.write_text("{}", encoding="utf-8")

    deleted = cleanup_stale_headless_sessions(
        manager=session_manager,
        now=now,
        ttl_seconds=86_400,
        resume_registries=(),
    )

    assert deleted == ["stale-headless"]
    assert session_manager.session_exists("normal-session")
    assert session_manager.session_exists("fresh-headless")
    assert session_manager.session_exists("active-headless")
    assert not session_manager.session_exists("stale-headless")
    assert not trace_path.exists()


@pytest.mark.asyncio
async def test_headless_ttl_cleanup_skips_pending_resume_future(tmp_path: Path):
    session_manager.initialize(tmp_path)
    now = 3_000_000.0
    session_id = "pending-headless"
    session_manager.create_session(
        session_id,
        metadata={"runtime_mode": "headless_worker", "headless_enabled": True, "worker_key_id": "key-1"},
    )
    _set_session_updated_at(session_id, now - 86_401)
    permission_resume_registry.create_tool_action_request(
        session_id=session_id,
        query_id="query-pending",
        tool_call_id="tool-pending",
        tool_name="execute",
        command="pwd",
        reason="test",
        risk="low",
    )

    assert cleanup_stale_headless_sessions(
        manager=session_manager,
        now=now,
        ttl_seconds=86_400,
    ) == []
    assert session_manager.session_exists(session_id)

    permission_resume_registry.reject_session(session_id, "test complete")
    assert cleanup_stale_headless_sessions(
        manager=session_manager,
        now=now,
        ttl_seconds=86_400,
    ) == [session_id]
    assert not session_manager.session_exists(session_id)


@pytest.mark.asyncio
async def test_expired_headless_session_cannot_be_reused(tmp_path: Path, monkeypatch):
    session_manager.initialize(tmp_path)
    monkeypatch.setenv("PUDDINGCLAW_HEADLESS_SESSION_TTL_HOURS", "24")
    monkeypatch.setattr(headless_api, "_model_options", lambda _principal: [{"id": "analysis"}])
    monkeypatch.setattr(
        headless_api,
        "_principal_for_scope",
        lambda _authorization, _scope: {"key_id": "key-owner", "authority_profile": "smart"},
    )
    session_id = "expired-headless"
    session_manager.create_session(
        session_id,
        metadata={
            "runtime_mode": "headless_worker",
            "headless_enabled": True,
            "worker_key_id": "key-owner",
        },
    )
    _set_session_updated_at(session_id, time.time() - 86_401)

    with pytest.raises(HTTPException) as error:
        await create_headless_run(
            HeadlessRunRequest(
                message="继续任务",
                analytics_model_id="analysis",
                session_id=session_id,
            ),
            authorization="Bearer test",
        )

    assert error.value.status_code == 410
    assert not session_manager.session_exists(session_id)


@pytest.mark.asyncio
async def test_new_headless_run_returns_session_retention_metadata(tmp_path: Path, monkeypatch):
    session_manager.initialize(tmp_path)
    monkeypatch.setenv("PUDDINGCLAW_HEADLESS_SESSION_TTL_HOURS", "24")
    monkeypatch.setattr(headless_api, "_model_options", lambda _principal: [{"id": "analysis"}])
    monkeypatch.setattr(
        headless_api,
        "_principal_for_scope",
        lambda _authorization, _scope: {"key_id": "key-owner", "authority_profile": "smart"},
    )
    monkeypatch.setattr(headless_api, "_ensure_worker_project", lambda: ("project-test", tmp_path))

    async def fake_consume(**kwargs):
        return {
            "schema_version": "1",
            "session_id": kwargs["session_id"],
            "status": "completed",
            "outcome": "completed",
            "final_response": "完成",
        }

    monkeypatch.setattr(headless_api, "_consume_run", fake_consume)
    response = await create_headless_run(
        HeadlessRunRequest(message="新任务", analytics_model_id="analysis"),
        authorization="Bearer test",
    )

    assert response["session_id"].startswith("worker-session-")
    assert response["session_ttl_seconds"] == 86_400
    assert response["session_expires_at"] > time.time()


def test_worker_access_key_is_one_time_and_revocable(tmp_path: Path):
    store = WorkerAccessStore()
    store.initialize(tmp_path)
    public, secret = store.create(name="teams", scopes=["worker:models:read"])
    assert public["prefix"] == secret[:12]
    assert "secret_hash" not in public
    assert store.authenticate(secret, "worker:models:read")["key_id"] == public["key_id"]
    assert store.authenticate(secret, "worker:runs:create") is None
    store.revoke(public["key_id"])
    assert store.authenticate(secret, "worker:models:read") is None


@pytest.mark.asyncio
async def test_headless_smart_permission_is_resolved_through_registry():
    request = permission_resume_registry.create_tool_action_request(
        session_id="s",
        query_id="q",
        tool_call_id="t",
        tool_name="execute",
        command="rm -rf /workspace/x",
        reason="destructive",
        risk="high",
    )
    context = {
        "permission_policy": {"approval_mode": "smart"},
        "authority_profile": "workspace",
        "workspace_path": "/workspace",
    }
    resolver = HeadlessInterruptResolver(context=context)
    decision = resolver.resolve("permission_request", request)
    assert decision["type"] == "reject"
    assert context["_headless_interrupt_summary"]["auto_rejected"] == 1
    assert permission_resume_registry.get(request["id"])["status"] == "resolved"


@pytest.mark.asyncio
async def test_full_access_only_approves_workspace_scoped_permission():
    request = permission_resume_registry.create_tool_action_request(
        session_id="full-scope-s",
        query_id="q",
        tool_call_id="t",
        tool_name="execute_external_directory",
        command="cat /workspace/report.csv",
        reason="external_directory_command",
        risk="managed_write",
    )
    request["path"] = "/workspace/report.csv"
    context = {
        "permission_policy": {"approval_mode": "full_access"},
        "authority_profile": "workspace",
        "workspace_path": "/workspace",
    }
    decision = HeadlessInterruptResolver(context=context).resolve("permission_request", request)
    assert decision["type"] == "approve"

    outside = permission_resume_registry.create_tool_action_request(
        session_id="full-scope-s",
        query_id="q",
        tool_call_id="t2",
        tool_name="execute",
        command="cat /etc/passwd",
        reason="external",
        risk="high",
    )
    outside["path"] = "/etc/passwd"
    denied = HeadlessInterruptResolver(context=context).resolve("permission_request", outside)
    assert denied["type"] == "reject"


def test_worker_access_store_does_not_persist_secret(tmp_path: Path):
    store = WorkerAccessStore()
    store.initialize(tmp_path)
    _public, secret = store.create(name="private")
    contents = (tmp_path / "data" / "worker-access-keys.json").read_text()
    assert secret not in contents
    assert "secret_hash" in contents


@pytest.mark.asyncio
async def test_headless_response_separates_aggregate_reply_from_final_assistant_content(monkeypatch):
    async def fake_stream(**_kwargs):
        yield {
            "event": "run_outcome",
            "data": '{"run_id":"run-test","status":"completed","outcome":"completed"}',
        }
        yield {
            "event": "final_response",
            "data": '{"content":"现在执行查询。\\n\\n最终答案。","final_response":"最终答案。"}',
        }
        yield {
            "event": "done",
            "data": '{"content":"现在执行查询。\\n\\n最终答案。","final_response":"最终答案。"}',
        }

    monkeypatch.setattr(headless_api.deepagents_agent_manager, "astream", fake_stream)
    monkeypatch.setattr(headless_api, "_model_binding", lambda model_id: {"id": model_id})

    response = await _consume_run(
        request=HeadlessRunRequest(message="查询", analytics_model_id="analysis"),
        session_id="worker-session-test",
        project_id="project-test",
        approval_mode="smart",
        authority={"profile": "smart"},
        request_received_at=1234.5,
    )

    assert response["reply"] == "现在执行查询。\n\n最终答案。"
    assert response["final_response"] == "最终答案。"
