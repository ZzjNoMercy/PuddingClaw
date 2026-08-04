from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from fastapi import HTTPException
from starlette.requests import Request

from api.headless import _admin
from graph.headless_resolver import HeadlessInterruptResolver
from graph.permission_resume import permission_resume_registry
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
