from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from fastapi import HTTPException
from starlette.requests import Request

from api import headless as headless_api
from api.headless import HeadlessRunRequest, _admin, _consume_run, _ensure_worker_project
from graph.headless_resolver import HeadlessInterruptResolver
from graph.permission_resume import permission_resume_registry
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
    )

    assert response["reply"] == "现在执行查询。\n\n最终答案。"
    assert response["final_response"] == "最终答案。"
