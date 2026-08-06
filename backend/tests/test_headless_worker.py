from __future__ import annotations

import json
import time
from pathlib import Path

import pytest
from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from starlette.requests import Request

from analytics.models.router import AnalyticsModelRoute
from api import headless as headless_api
from api.headless import (
    HeadlessResumeDecision,
    HeadlessResumeRequest,
    HeadlessRunRequest,
    _admin,
    _consume_run,
    _ensure_worker_project,
    _resolve_external_permission,
    create_headless_run,
    list_worker_access_logs,
    resume_headless_run,
)
from graph.headless_resolver import HeadlessInterruptResolver
from graph.permission_resume import permission_resume_registry
from graph.session_manager import session_manager
from headless_session_lifecycle import cleanup_stale_headless_sessions
from knowledge.models import WorkerAccessLog
from projects.registry import project_registry
from worker_access import WorkerAccessLogStore, WorkerAccessStore


def _request_from(host: str) -> Request:
    return Request({
        "type": "http",
        "method": "GET",
        "path": "/api/worker-access-keys",
        "headers": [],
        "client": (host, 1234),
        "server": ("127.0.0.1", 8888),
    })


def test_headless_request_rejects_caller_selected_model():
    with pytest.raises(ValidationError):
        HeadlessRunRequest(message="分析销售", analytics_model_id="sales")


def test_model_routing_candidates_are_limited_by_worker_key(monkeypatch):
    class FakeRegistry:
        def list_models(self):
            return {
                "models": [
                    {"id": "allowed", "name": "允许模型"},
                    {"id": "forbidden", "name": "禁止模型"},
                ]
            }

        def get_model(self, model_id):
            return {"id": model_id, "body": f"{model_id} applicability"}

    monkeypatch.setattr(headless_api, "get_analytics_model_registry", lambda _base_dir: FakeRegistry())
    candidates = headless_api._model_routing_candidates(
        {"allowed_analytics_models": ["allowed"]}
    )

    assert [item["id"] for item in candidates] == ["allowed"]
    assert candidates[0]["applicability"] == "allowed applicability"


@pytest.mark.asyncio
async def test_worker_access_log_api_uses_beijing_time_and_fixed_page_size(monkeypatch):
    captured: dict[str, object] = {}

    async def fake_list(**kwargs):
        captured.update(kwargs)
        return {
            "items": [
                {
                    "id": "wal-test",
                    "created_at": 0.0,
                    "key_id": "key-test",
                    "key_name": "puddingteams",
                    "query": "测试 query",
                }
            ],
            "page": 1,
            "page_size": 10,
            "total": 1,
            "total_pages": 1,
            "key_names": ["puddingteams"],
        }

    monkeypatch.delenv("PUDDINGCLAW_ADMIN_TOKEN", raising=False)
    monkeypatch.setattr(headless_api.worker_access_log_store, "list", fake_list)
    result = await list_worker_access_logs(
        _request_from("127.0.0.1"),
        page=1,
        key_name=None,
        query_keyword=None,
        start_at=None,
        end_at=None,
    )

    assert captured["page_size"] == 10
    assert result["timezone"] == "Asia/Shanghai"
    assert result["items"][0]["created_at_beijing"] == "1970-01-01 08:00:00"


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


def test_headless_ttl_survives_runtime_mode_transition_to_agent(tmp_path: Path):
    session_manager.initialize(tmp_path)
    now = 2_500_000.0
    session_id = "headless-runtime-became-agent"
    session_manager.create_session(
        session_id,
        metadata={
            "runtime_mode": "headless_worker",
            "headless_enabled": True,
            "worker_id": "puddingclaw",
            "worker_key_id": "key-1",
        },
    )
    session_manager.update_metadata(session_id, {"runtime_mode": "agent"})
    _set_session_updated_at(session_id, now - 86_401)

    assert cleanup_stale_headless_sessions(
        manager=session_manager,
        now=now,
        ttl_seconds=86_400,
        resume_registries=(),
    ) == [session_id]
    assert not session_manager.session_exists(session_id)


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
            # A completed DeepAgents Run changes the active runtime marker to
            # ``agent``; Headless ownership must still enforce its TTL.
            "runtime_mode": "agent",
            "headless_enabled": True,
            "worker_key_id": "key-owner",
            "analytics_model_id": "analysis",
        },
    )
    _set_session_updated_at(session_id, time.time() - 86_401)

    with pytest.raises(HTTPException) as error:
        await create_headless_run(
            HeadlessRunRequest(
                message="继续任务",
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
        lambda _authorization, _scope: {
            "key_id": "key-owner",
            "name": "puddingteams",
            "authority_profile": "smart",
        },
    )
    monkeypatch.setattr(headless_api, "_ensure_worker_project", lambda: ("project-test", tmp_path))
    logged: dict[str, object] = {}

    async def fake_record(**kwargs):
        logged.update(kwargs)

    monkeypatch.setattr(
        headless_api.worker_access_log_store,
        "record",
        fake_record,
    )

    async def fake_route(_message, _principal):
        return AnalyticsModelRoute("matched", "analysis", 0.96, "semantic", "matched business scope")

    monkeypatch.setattr(headless_api, "_route_analytics_model", fake_route)

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
        HeadlessRunRequest(message="新任务"),
        authorization="Bearer test",
    )

    assert response["session_id"].startswith("worker-session-")
    assert response["session_ttl_seconds"] == 86_400
    assert response["session_expires_at"] > time.time()
    assert session_manager.get_metadata(response["session_id"])["analytics_model_id"] == "analysis"
    assert logged["key_name"] == "puddingteams"
    assert logged["query"] == "新任务"


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
async def test_worker_access_logs_filter_and_paginate_ten_per_page(tmp_path: Path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'worker-logs.db'}")
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as connection:
        await connection.run_sync(WorkerAccessLog.__table__.create)
    store = WorkerAccessLogStore(sessions)
    for index in range(23):
        await store.record(
            key_id="key-teams" if index % 2 == 0 else "key-codex",
            key_name="puddingteams" if index % 2 == 0 else "codex",
            query=f"查询第 {index} 条销量" if index % 3 == 0 else f"查询第 {index} 条配置",
            created_at=1_000.0 + index,
        )

    second_page = await store.list(page=2, page_size=10)
    filtered = await store.list(
        page=1,
        page_size=10,
        key_name="puddingteams",
        query="销量",
        start_at=1_000.0,
        end_at=1_022.0,
    )

    assert second_page["page_size"] == 10
    assert second_page["total"] == 23
    assert len(second_page["items"]) == 10
    assert second_page["items"][0]["created_at"] == 1_012.0
    assert filtered["total"] == 4
    assert {item["key_name"] for item in filtered["items"]} == {"puddingteams"}
    assert all("销量" in item["query"] for item in filtered["items"])
    assert filtered["key_names"] == ["codex", "puddingteams"]
    await engine.dispose()


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
        request=HeadlessRunRequest(message="查询"),
        session_id="worker-session-test",
        project_id="project-test",
        approval_mode="smart",
        authority={"profile": "smart"},
        request_received_at=1234.5,
        analytics_model_id="analysis",
        analytics_model_match={"status": "matched", "selected_id": "analysis"},
    )

    assert response["reply"] == "现在执行查询。\n\n最终答案。"
    assert response["final_response"] == "最终答案。"
    assert response["analytics_model_id"] == "analysis"


@pytest.mark.asyncio
async def test_headless_permission_pauses_and_resume_continues_same_run(tmp_path: Path, monkeypatch):
    session_id = "worker-session-external-hitl"
    session_manager.initialize(tmp_path)
    session_manager.create_session(
        session_id,
        metadata={
            "runtime_mode": "headless_worker",
            "headless_enabled": True,
            "worker_key_id": "key-owner",
        },
    )
    permission = permission_resume_registry.create_tool_action_request(
        session_id=session_id,
        query_id="query-same",
        run_id="run-same",
        tool_call_id="call-same",
        tool_name="execute",
        command="python3 /skills/example/run.py",
        reason="managed_skill_script",
        risk="high",
    )

    async def fake_stream(**kwargs):
        assert kwargs["interaction_mode"] == "external"
        yield {
            "event": "run_started",
            "data": json.dumps(
                {"session_id": session_id, "query_id": "query-same", "run_id": "run-same"}
            ),
        }
        yield {"event": "permission_required", "data": json.dumps(permission)}
        decision = await permission_resume_registry.wait(permission["id"])
        yield {
            "event": "permission_resolved",
            "data": json.dumps(
                {
                    "request_id": permission["id"],
                    "interrupt_id": "interrupt-same",
                    "decision": decision,
                }
            ),
        }
        yield {
            "event": "run_outcome",
            "data": json.dumps(
                {
                    "session_id": session_id,
                    "query_id": "query-same",
                    "run_id": "run-same",
                    "status": "completed",
                    "outcome": "completed",
                }
            ),
        }
        yield {
            "event": "done",
            "data": json.dumps({"content": "完成", "final_response": "完成"}),
        }

    monkeypatch.setattr(headless_api.deepagents_agent_manager, "astream", fake_stream)
    monkeypatch.setattr(headless_api, "_model_binding", lambda model_id: {"id": model_id})
    monkeypatch.setattr(
        headless_api,
        "_principal_for_scope",
        lambda _authorization, _scope: {"key_id": "key-owner"},
    )

    paused = await _consume_run(
        request=HeadlessRunRequest(message="运行 Skill"),
        session_id=session_id,
        project_id="project-test",
        approval_mode="smart",
        authority={"profile": "smart"},
        request_received_at=1234.5,
        analytics_model_id="analysis",
        analytics_model_match={"status": "matched", "selected_id": "analysis"},
        worker_key_id="key-owner",
    )

    assert paused["status"] == "needs_input"
    assert paused["outcome"] == "waiting_hitl"
    assert paused["run_id"] == "run-same"
    assert paused["needs_input"]["request_id"] == permission["id"]
    assert permission_resume_registry.get(permission["id"])["status"] == "pending"
    persisted_pending = session_manager.get_metadata(session_id)["headless_pending_input"]
    assert persisted_pending["status"] == "pending"
    assert persisted_pending["requests"][0]["id"] == permission["id"]

    completed = await resume_headless_run(
        "run-same",
        HeadlessResumeRequest(
            continuation_token=paused["continuation_token"],
            decisions=[
                HeadlessResumeDecision(
                    request_id=permission["id"],
                    decision="reject",
                    scope="once",
                )
            ],
        ),
        authorization="Bearer test",
    )

    assert completed["status"] == "completed"
    assert completed["outcome"] == "completed"
    assert completed["run_id"] == "run-same"
    assert completed["final_response"] == "完成"
    assert permission_resume_registry.get(permission["id"])["decision"]["type"] == "reject"
    assert session_manager.get_metadata(session_id)["headless_pending_input"]["status"] == "completed"

    retried = await resume_headless_run(
        "run-same",
        HeadlessResumeRequest(
            continuation_token=paused["continuation_token"],
            decisions=[
                HeadlessResumeDecision(
                    request_id=permission["id"],
                    decision="reject",
                    scope="once",
                )
            ],
        ),
        authorization="Bearer test",
    )
    assert retried["status"] == "completed"
    assert retried["run_id"] == "run-same"


@pytest.mark.asyncio
async def test_headless_cli_approval_uses_canonical_permission_grant(tmp_path: Path):
    session_id = "worker-session-external-approve"
    session_manager.initialize(tmp_path)
    session_manager.create_session(
        session_id,
        metadata={
            "runtime_mode": "headless_worker",
            "headless_enabled": True,
            "worker_key_id": "key-owner",
        },
    )
    permission = permission_resume_registry.create_tool_action_request(
        session_id=session_id,
        query_id="query-approve",
        run_id="run-approve",
        tool_call_id="call-approve",
        tool_name="execute",
        command="python3 /skills/example/run.py",
        reason="managed_skill_script",
        risk="high",
    )

    await _resolve_external_permission(
        session_id=session_id,
        decision=HeadlessResumeDecision(
            request_id=permission["id"],
            decision="approve",
            scope="once",
        ),
    )

    resolved = permission_resume_registry.get(permission["id"])
    assert resolved["status"] == "resolved"
    assert resolved["decision"]["type"] == "approve"
    grants = session_manager.list_permission_grants(session_id)
    assert len(grants) == 1
    assert grants[0]["scope"] == "once"
    assert grants[0]["metadata"]["run_id"] == "run-approve"


@pytest.mark.asyncio
async def test_ambiguous_model_route_does_not_create_or_run_session(tmp_path: Path, monkeypatch):
    session_manager.initialize(tmp_path)
    monkeypatch.setattr(
        headless_api,
        "_principal_for_scope",
        lambda _authorization, _scope: {"key_id": "key-owner", "authority_profile": "smart"},
    )
    monkeypatch.setattr(
        headless_api,
        "_model_options",
        lambda _principal: [{"id": "sales"}, {"id": "product"}],
    )

    async def fake_route(_message, _principal):
        return AnalyticsModelRoute("ambiguous", None, 0.51, "semantic", "two plausible models")

    async def fail_consume(**_kwargs):
        raise AssertionError("ambiguous routing must not start an Agent Run")

    monkeypatch.setattr(headless_api, "_route_analytics_model", fake_route)
    monkeypatch.setattr(headless_api, "_consume_run", fail_consume)

    response = await create_headless_run(
        HeadlessRunRequest(message="帮我分析一下"),
        authorization="Bearer test",
    )

    assert response["status"] == "needs_input"
    assert response["outcome"] == "analytics_model_clarification_required"
    assert response["needs_input"]["type"] == "analytics_model_clarification"
    assert response["analytics_model_match"]["selected_id"] is None
    assert not list((tmp_path / "sessions").glob("worker-session-*.json"))


@pytest.mark.asyncio
async def test_continuous_session_reuses_bound_model_without_rerouting(tmp_path: Path, monkeypatch):
    session_manager.initialize(tmp_path)
    session_id = "worker-session-continuous"
    session_manager.create_session(
        session_id,
        metadata={
            "runtime_mode": "headless_worker",
            "headless_enabled": True,
            "worker_key_id": "key-owner",
            "analytics_model_id": "product",
        },
    )
    monkeypatch.setattr(
        headless_api,
        "_principal_for_scope",
        lambda _authorization, _scope: {"key_id": "key-owner", "authority_profile": "smart"},
    )
    monkeypatch.setattr(headless_api, "_model_options", lambda _principal: [{"id": "product"}])
    monkeypatch.setattr(headless_api, "_ensure_worker_project", lambda: ("project-test", tmp_path))

    async def fail_route(_message, _principal):
        raise AssertionError("a continuous session must reuse its bound model")

    async def fake_consume(**kwargs):
        return {
            "schema_version": "1",
            "session_id": kwargs["session_id"],
            "analytics_model_id": kwargs["analytics_model_id"],
            "analytics_model_match": kwargs["analytics_model_match"],
            "status": "completed",
            "outcome": "completed",
        }

    monkeypatch.setattr(headless_api, "_route_analytics_model", fail_route)
    monkeypatch.setattr(headless_api, "_consume_run", fake_consume)

    response = await create_headless_run(
        HeadlessRunRequest(message="继续", session_id=session_id),
        authorization="Bearer test",
    )

    assert response["analytics_model_id"] == "product"
    assert response["analytics_model_match"]["strategy"] == "session_bound"


@pytest.mark.asyncio
async def test_general_route_runs_session_without_model(tmp_path: Path, monkeypatch):
    session_manager.initialize(tmp_path)
    monkeypatch.setattr(
        headless_api,
        "_principal_for_scope",
        lambda _authorization, _scope: {"key_id": "key-owner", "authority_profile": "smart"},
    )
    monkeypatch.setattr(
        headless_api,
        "_model_options",
        lambda _principal: [{"id": "sales"}, {"id": "product"}],
    )
    monkeypatch.setattr(headless_api, "_ensure_worker_project", lambda: ("project-test", tmp_path))

    async def fake_route(_message, _principal):
        return AnalyticsModelRoute("general", None, 0.9, "semantic", "weather_question")

    consumed: dict[str, object] = {}

    async def fake_consume(**kwargs):
        consumed.update(kwargs)
        return {
            "schema_version": "1",
            "session_id": kwargs["session_id"],
            "analytics_model_id": kwargs["analytics_model_id"],
            "status": "completed",
            "outcome": "completed",
            "final_response": "今天慈溪中雨转小雨。",
        }

    monkeypatch.setattr(headless_api, "_route_analytics_model", fake_route)
    monkeypatch.setattr(headless_api, "_consume_run", fake_consume)

    response = await create_headless_run(
        HeadlessRunRequest(message="今天宁波慈溪天气如何"),
        authorization="Bearer test",
    )

    assert response["status"] == "completed"
    assert consumed["analytics_model_id"] == ""
    assert session_manager.get_metadata(response["session_id"])["analytics_model_id"] == ""


@pytest.mark.asyncio
async def test_continuous_general_session_reroutes_and_can_bind_later(tmp_path: Path, monkeypatch):
    session_manager.initialize(tmp_path)
    session_id = "worker-session-general"
    session_manager.create_session(
        session_id,
        metadata={
            "runtime_mode": "headless_worker",
            "headless_enabled": True,
            "worker_key_id": "key-owner",
            "analytics_model_id": "",
        },
    )
    monkeypatch.setattr(
        headless_api,
        "_principal_for_scope",
        lambda _authorization, _scope: {"key_id": "key-owner", "authority_profile": "smart"},
    )
    monkeypatch.setattr(
        headless_api,
        "_model_options",
        lambda _principal: [{"id": "sales"}, {"id": "product"}],
    )
    monkeypatch.setattr(headless_api, "_ensure_worker_project", lambda: ("project-test", tmp_path))

    routes = [
        AnalyticsModelRoute("general", None, 0.9, "semantic", "weather_question"),
        AnalyticsModelRoute("matched", "product", 0.96, "semantic", "matched business scope"),
    ]

    async def fake_route(_message, _principal):
        return routes.pop(0)

    consumed: list[dict[str, object]] = []

    async def fake_consume(**kwargs):
        consumed.append(kwargs)
        return {
            "schema_version": "1",
            "session_id": kwargs["session_id"],
            "analytics_model_id": kwargs["analytics_model_id"],
            "status": "completed",
            "outcome": "completed",
        }

    monkeypatch.setattr(headless_api, "_route_analytics_model", fake_route)
    monkeypatch.setattr(headless_api, "_consume_run", fake_consume)

    general = await create_headless_run(
        HeadlessRunRequest(message="今天天气如何", session_id=session_id),
        authorization="Bearer test",
    )
    assert general["status"] == "completed"
    assert consumed[0]["analytics_model_id"] == ""
    assert session_manager.get_metadata(session_id)["analytics_model_id"] == ""

    bound = await create_headless_run(
        HeadlessRunRequest(message="看看空气悬架配置率", session_id=session_id),
        authorization="Bearer test",
    )
    assert bound["status"] == "completed"
    assert consumed[1]["analytics_model_id"] == "product"
    assert session_manager.get_metadata(session_id)["analytics_model_id"] == "product"
