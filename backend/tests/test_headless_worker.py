from __future__ import annotations

import asyncio
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
    _require_loopback_request,
    _consume_run,
    _ensure_worker_project,
    _HeadlessExecution,
    _resolve_external_permission,
    _resolve_worker_project,
    _stream_headless_execution,
    cancel_headless_run,
    create_headless_run,
    list_headless_activity_logs,
    resume_headless_run,
)
from graph.headless_resolver import HeadlessInterruptResolver, headless_authority_from_environment
from graph.permission_resume import permission_resume_registry
from graph.session_manager import session_manager
from headless_session_lifecycle import cleanup_stale_headless_sessions
from knowledge.models import WorkerAccessLog
from projects.registry import project_registry
from headless_activity import HeadlessActivityLogStore


def _request_from(host: str) -> Request:
    return Request({
        "type": "http",
        "method": "GET",
        "path": "/api/headless/health",
        "headers": [],
        "client": (host, 1234),
        "server": ("127.0.0.1", 8888),
    })


def _register_test_worker_project(tmp_path: Path) -> str:
    """Register the fake worker project through the same trusted path as production."""

    project_registry.initialize(tmp_path)
    return project_registry.register(
        str(tmp_path),
        name="puddingclaw-test",
        trusted=True,
    ).project_id


def test_headless_request_rejects_caller_selected_model():
    with pytest.raises(ValidationError):
        HeadlessRunRequest(message="分析销售", analytics_model_id="sales")


def test_headless_authority_does_not_define_a_separate_filesystem_mode(monkeypatch):
    monkeypatch.delenv("PUDDINGCLAW_HEADLESS_AUTHORITY_PROFILE", raising=False)

    authority = headless_authority_from_environment()
    assert authority["profile"] == ""
    assert "filesystem_mode" not in authority


def test_headless_permission_projection_preserves_shell_access_intent() -> None:
    payload = {
        "id": "permission-shell",
        "type": "shell_directory_access",
        "command": 'mkdir -p "/tmp/report"',
        "path": "/tmp/report",
        "paths": ["/tmp/report"],
        "grant_specs": [
            {
                "target": "/tmp/report",
                "access": "write",
                "delete": False,
                "capabilities": ["write", "recursive", "shell_access"],
            }
        ],
        "capabilities": ["write", "recursive", "shell_access"],
        "grant_bindings": {"filesystem_mode": "restricted"},
    }

    projected = headless_api._needs_input("permission_required", payload)

    assert projected is not None
    assert projected["grant_specs"] == payload["grant_specs"]
    assert projected["capabilities"] == payload["capabilities"]
    assert projected["grant_bindings"] == payload["grant_bindings"]


def test_model_routing_candidates_include_configured_models(monkeypatch):
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
    candidates = headless_api._model_routing_candidates()

    assert [item["id"] for item in candidates] == ["allowed", "forbidden"]
    assert candidates[0]["applicability"] == "allowed applicability"


@pytest.mark.asyncio
async def test_headless_activity_log_api_uses_beijing_time_and_fixed_page_size(monkeypatch):
    captured: dict[str, object] = {}

    async def fake_list(**kwargs):
        captured.update(kwargs)
        return {
            "items": [
                {
                    "id": "wal-test",
                    "created_at": 0.0,
                    "source_id": "puddingteams",
                    "source_name": "PuddingTeams",
                    "query": "测试 query",
                }
            ],
            "page": 1,
            "page_size": 10,
            "total": 1,
            "total_pages": 1,
            "source_names": ["PuddingTeams"],
        }

    monkeypatch.setattr(headless_api.headless_activity_log_store, "list", fake_list)
    result = await list_headless_activity_logs(
        _request_from("127.0.0.1"),
        page=1,
        source_name=None,
        query_keyword=None,
        start_at=None,
        end_at=None,
    )

    assert captured["page_size"] == 10
    assert result["timezone"] == "Asia/Shanghai"
    assert result["items"][0]["created_at_beijing"] == "1970-01-01 08:00:00"


def test_local_headless_api_accepts_loopback():
    _require_loopback_request(_request_from("127.0.0.1"))


def test_headless_api_rejects_non_loopback():
    with pytest.raises(HTTPException) as error:
        _require_loopback_request(_request_from("192.0.2.10"))
    assert error.value.status_code == 403


def test_worker_project_has_internal_identity(tmp_path, monkeypatch):
    monkeypatch.setenv("PUDDINGCLAW_PROJECTS_ROOT", str(tmp_path))
    project_registry.initialize(tmp_path)
    _project_id, path = _ensure_worker_project()
    assert path == tmp_path / "puddingclaw"


def test_platform_workspace_path_creates_then_reuses_project_id(tmp_path):
    project_registry.initialize(tmp_path)
    workspace = tmp_path / "room-workspace"
    workspace.mkdir()

    first_id, first_path = _resolve_worker_project(str(workspace))
    second_id, second_path = _resolve_worker_project(str(workspace))

    assert first_path == second_path == workspace.resolve()
    assert first_id == second_id
    assert len(project_registry.list_projects()) == 1


@pytest.mark.asyncio
async def test_headless_run_binds_platform_workspace_path(tmp_path, monkeypatch):
    session_manager.initialize(tmp_path)
    project_registry.initialize(tmp_path)
    workspace = tmp_path / "teams-room"
    workspace.mkdir()
    monkeypatch.setattr(headless_api, "_model_options", lambda: [{"id": "analysis"}])

    async def fake_route(_message):
        return AnalyticsModelRoute("matched", "analysis", 0.96, "semantic", "matched")

    consumed: dict[str, object] = {}

    async def fake_consume(**kwargs):
        consumed.update(kwargs)
        return {
            "schema_version": "1",
            "session_id": kwargs["session_id"],
            "project_id": kwargs["project_id"],
            "status": "completed",
            "outcome": "completed",
        }

    monkeypatch.setattr(headless_api, "_route_analytics_model", fake_route)
    monkeypatch.setattr(headless_api, "_consume_run", fake_consume)

    response = await create_headless_run(
        HeadlessRunRequest(message="使用房间目录", workspace_path=str(workspace)),
        http_request=_request_from("127.0.0.1"),
    )

    assert response["project_id"] == consumed["project_id"]
    assert "filesystem_mode" not in consumed
    assert session_manager.get_metadata(response["session_id"])["workspace_path"] == str(workspace.resolve())


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
    monkeypatch.setattr(headless_api, "_model_options", lambda: [{"id": "analysis"}])
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
            http_request=_request_from("127.0.0.1"),
        )

    assert error.value.status_code == 410
    assert not session_manager.session_exists(session_id)


@pytest.mark.asyncio
async def test_new_headless_run_returns_session_retention_metadata(tmp_path: Path, monkeypatch):
    session_manager.initialize(tmp_path)
    project_id = _register_test_worker_project(tmp_path)
    monkeypatch.setenv("PUDDINGCLAW_HEADLESS_SESSION_TTL_HOURS", "24")
    monkeypatch.setattr(headless_api, "_model_options", lambda: [{"id": "analysis"}])
    monkeypatch.setattr(headless_api, "_ensure_worker_project", lambda: (project_id, tmp_path))
    logged: dict[str, object] = {}

    async def fake_record(**kwargs):
        logged.update(kwargs)

    monkeypatch.setattr(
        headless_api.headless_activity_log_store,
        "record",
        fake_record,
    )

    async def fake_route(_message):
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
        HeadlessRunRequest(
            message="新任务",
            metadata={"caller_id": "puddingteams", "caller_name": "PuddingTeams"},
        ),
        http_request=_request_from("127.0.0.1"),
    )

    assert response["session_id"].startswith("worker-session-")
    assert response["session_ttl_seconds"] == 86_400
    assert response["session_expires_at"] > time.time()
    assert session_manager.get_metadata(response["session_id"])["analytics_model_id"] == "analysis"
    assert session_manager.get_metadata(response["session_id"])["session_source"] == "cli"
    assert logged["source_name"] == "PuddingTeams"
    assert logged["query"] == "新任务"


@pytest.mark.asyncio
async def test_headless_activity_logs_filter_and_paginate_ten_per_page(tmp_path: Path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'worker-logs.db'}")
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as connection:
        await connection.run_sync(WorkerAccessLog.__table__.create)
    store = HeadlessActivityLogStore(sessions)
    for index in range(23):
        await store.record(
            source_id="puddingteams" if index % 2 == 0 else "codex",
            source_name="PuddingTeams" if index % 2 == 0 else "Codex",
            query=f"查询第 {index} 条销量" if index % 3 == 0 else f"查询第 {index} 条配置",
            created_at=1_000.0 + index,
        )

    second_page = await store.list(page=2, page_size=10)
    filtered = await store.list(
        page=1,
        page_size=10,
        source_name="PuddingTeams",
        query="销量",
        start_at=1_000.0,
        end_at=1_022.0,
    )

    assert second_page["page_size"] == 10
    assert second_page["total"] == 23
    assert len(second_page["items"]) == 10
    assert second_page["items"][0]["created_at"] == 1_012.0
    assert filtered["total"] == 4
    assert {item["source_name"] for item in filtered["items"]} == {"PuddingTeams"}
    assert all("销量" in item["query"] for item in filtered["items"])
    assert filtered["source_names"] == ["Codex", "PuddingTeams"]
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
async def test_headless_rejects_unresolved_permission_even_inside_authority_scope():
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
        "permission_policy": {"approval_mode": "smart"},
        "authority_profile": "workspace",
        "workspace_path": "/workspace",
    }
    decision = HeadlessInterruptResolver(context=context).resolve("permission_request", request)
    assert decision["type"] == "reject"

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


@pytest.mark.asyncio
async def test_headless_never_auto_approves_browser_actions():
    request = permission_resume_registry.create_tool_action_request(
        session_id="browser-headless",
        query_id="q-browser",
        tool_call_id="browser-call",
        tool_name="browser",
        command="click",
        reason="browser_interaction_confirmation",
        risk="browser_interaction",
    )
    context = {
        "permission_policy": {"approval_mode": "smart"},
        "authority_profile": "workspace",
        "workspace_path": "/workspace",
    }

    decision = HeadlessInterruptResolver(context=context).resolve("permission_request", request)

    assert decision["type"] == "reject"
    assert permission_resume_registry.get(request["id"])["status"] == "resolved"


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
async def test_headless_stream_emits_starting_before_agent_reaches_boundary():
    gate = asyncio.Event()

    async def stalled_stream(**_kwargs):
        await gate.wait()
        if False:
            yield {}

    execution = _HeadlessExecution(
        stream=stalled_stream(),
        session_id="headless-stream-session",
        project_id="project-test",
        approval_mode="smart",
        analytics_model_id="",
        analytics_model_match={"status": "general"},
    )
    execution.start()
    first = await asyncio.wait_for(
        anext(_stream_headless_execution(execution)),
        timeout=0.2,
    )
    assert first["event"] == "run_starting"
    assert first["data"]["session_id"] == execution.session_id
    gate.set()
    await execution.cancel()


@pytest.mark.asyncio
async def test_streaming_run_endpoint_returns_before_agent_generator_advances(tmp_path, monkeypatch):
    session_manager.initialize(tmp_path)
    project_registry.initialize(tmp_path)
    workspace = tmp_path / "stream-workspace"
    workspace.mkdir()
    gate = asyncio.Event()

    monkeypatch.setattr(headless_api, "_model_options", lambda: [])

    async def fake_route(_message):
        return AnalyticsModelRoute("general", None, 1.0, "deterministic", "general")

    async def gated_stream(**_kwargs):
        await gate.wait()
        yield {"event": "token", "data": json.dumps({"content": "完成"})}
        yield {"event": "done", "data": json.dumps({"content": "完成", "final_response": "完成"})}

    monkeypatch.setattr(headless_api, "_route_analytics_model", fake_route)
    monkeypatch.setattr(headless_api.deepagents_agent_manager, "astream", gated_stream)

    response = await create_headless_run(
        HeadlessRunRequest(message="流式执行", workspace_path=str(workspace)),
        http_request=_request_from("127.0.0.1"),
        stream=True,
    )
    first = await asyncio.wait_for(anext(response.body_iterator), timeout=0.2)
    first_event = json.loads(first)
    assert first_event["event"] == "run_starting"
    session_id = first_event["data"]["session_id"]
    assert session_id in headless_api._active_headless_sessions

    gate.set()
    remaining = [json.loads(chunk) async for chunk in response.body_iterator]
    assert [item["event"] for item in remaining] == ["token", "done", "result"]
    assert session_id not in headless_api._active_headless_sessions
    with headless_api._headless_executions_lock:
        headless_api._headless_executions.pop(session_id, None)


@pytest.mark.asyncio
async def test_headless_stream_hides_reasoning_but_keeps_user_content():
    async def fake_stream():
        yield {
            "event": "reasoning",
            "data": json.dumps({"content": "内部思考不应外送"}),
        }
        yield {
            "event": "model_stream_preview",
            "data": json.dumps({"content": "内部传输预览也不应外送"}),
        }
        yield {
            "event": "token",
            "data": json.dumps({"content": "用户可见内容"}),
        }
        yield {
            "event": "done",
            "data": json.dumps({"content": "用户可见内容", "final_response": "最终回答"}),
        }

    execution = _HeadlessExecution(
        stream=fake_stream(),
        session_id="headless-public-events",
        project_id="project-test",
        approval_mode="smart",
        analytics_model_id="",
        analytics_model_match={"status": "general"},
    )
    execution.start()
    events = [item async for item in _stream_headless_execution(execution)]
    assert [item["event"] for item in events] == ["run_starting", "token", "done", "result"]
    assert all(item["event"] != "reasoning" for item in events)


@pytest.mark.asyncio
async def test_headless_stream_exposes_response_recovery_and_structured_failure():
    failure = {
        "code": "model_response_incomplete",
        "message": "模型未返回完整的最终回答或可执行工具调用。",
        "recoverable": True,
    }
    next_action = {
        "type": "continue_session",
        "session_id": "headless-incomplete-session",
        "message": "继续完成剩余任务，不要重复已成功的工具调用。",
    }
    termination = {
        "finish_reason": None,
        "content_chars": 0,
        "reasoning_chars": 16,
        "tool_call_count": 0,
        "invalid_reason": "reasoning_without_final_content",
        "recovery_attempts": 1,
    }

    async def fake_stream():
        yield {
            "event": "model_response_recovery_started",
            "data": json.dumps({"status": "running", "attempt": 1}),
        }
        yield {
            "event": "model_response_incomplete",
            "data": json.dumps({"status": "failed", "failure": failure}),
        }
        yield {
            "event": "run_outcome",
            "data": json.dumps(
                {
                    "run_id": "run-incomplete",
                    "status": "failed",
                    "outcome": "failed",
                    "error": failure,
                    "next_action": next_action,
                    "termination": termination,
                }
            ),
        }
        yield {"event": "done", "data": json.dumps({"content": "", "run_outcome": "failed"})}

    execution = _HeadlessExecution(
        stream=fake_stream(),
        session_id="headless-incomplete-session",
        project_id="project-test",
        approval_mode="smart",
        analytics_model_id="",
        analytics_model_match={"status": "general"},
    )
    execution.start()
    events = [item async for item in _stream_headless_execution(execution)]

    assert [item["event"] for item in events] == [
        "run_starting",
        "model_response_recovery_started",
        "model_response_incomplete",
        "run_outcome",
        "done",
        "result",
    ]
    result = events[-1]["data"]
    assert result["status"] == "failed"
    assert result["error"] == failure
    assert result["next_action"] == next_action
    assert result["termination"] == termination


@pytest.mark.asyncio
async def test_headless_execution_broadcasts_same_ordered_events_to_multiple_observers():
    async def fake_stream():
        yield {"event": "token", "data": json.dumps({"content": "增量"})}
        yield {"event": "tool_start", "data": json.dumps({"id": "tool-1", "tool": "search"})}
        yield {"event": "done", "data": json.dumps({"content": "增量"})}

    execution = _HeadlessExecution(
        stream=fake_stream(),
        session_id="headless-broadcast-session",
        project_id="project-test",
        approval_mode="smart",
        analytics_model_id="",
        analytics_model_match={"status": "general"},
    )
    execution.start()
    first = execution.subscribe()
    second = execution.subscribe()
    assert execution.task is not None
    await execution.task

    async def drain(queue):
        items = []
        while True:
            item = await queue.get()
            if item is None:
                return items
            items.append(item)

    first_events, second_events = await asyncio.gather(drain(first), drain(second))
    assert first_events == second_events
    assert [item["event"] for item in first_events] == [
        "run_starting",
        "token",
        "tool_start",
        "done",
    ]
    assert [item["seq"] for item in first_events] == [1, 2, 3, 4]


@pytest.mark.asyncio
async def test_headless_late_observer_replays_only_events_after_sequence():
    async def fake_stream():
        yield {"event": "token", "data": json.dumps({"content": "A"})}
        yield {"event": "token", "data": json.dumps({"content": "B"})}
        yield {"event": "done", "data": json.dumps({"content": "AB"})}

    execution = _HeadlessExecution(
        stream=fake_stream(),
        session_id="headless-replay-session",
        project_id="project-test",
        approval_mode="smart",
        analytics_model_id="",
        analytics_model_match={"status": "general"},
    )
    execution.start()
    assert execution.task is not None
    await execution.task
    subscriber = execution.subscribe(after_seq=2)
    replayed = []
    while True:
        item = await subscriber.get()
        if item is None:
            break
        replayed.append(item)
    assert [(item["seq"], item["event"]) for item in replayed] == [
        (3, "token"),
        (4, "done"),
    ]


@pytest.mark.asyncio
async def test_headless_replay_marks_reset_when_initial_backlog_exceeds_queue(monkeypatch):
    monkeypatch.setattr(headless_api, "_HEADLESS_SUBSCRIBER_QUEUE_LIMIT", 4)
    execution = _HeadlessExecution(
        stream=None,
        session_id="headless-reset-session",
        project_id="project-test",
        approval_mode="smart",
        analytics_model_id="",
        analytics_model_match={"status": "general"},
    )
    for index in range(5):
        execution._publish({"event": "token", "data": {"content": str(index)}})
    execution.done = True
    subscriber = execution.subscribe()
    replayed = []
    while True:
        item = await subscriber.get()
        if item is None:
            break
        replayed.append(item)

    assert replayed[0]["event"] == "stream_reset_required"
    assert replayed[0]["data"]["replay_start_seq"] == 4
    assert [(item["seq"], item["event"]) for item in replayed[1:]] == [
        (4, "token"),
        (5, "token"),
    ]


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

    paused = await _consume_run(
        request=HeadlessRunRequest(message="运行 Skill"),
        session_id=session_id,
        project_id="project-test",
        approval_mode="smart",
        authority={"profile": "smart"},
        request_received_at=1234.5,
        analytics_model_id="analysis",
        analytics_model_match={"status": "matched", "selected_id": "analysis"},
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
            request_id="response-same",
            decisions=[
                HeadlessResumeDecision(
                    request_id=permission["id"],
                    decision="reject",
                    scope="once",
                )
            ],
        ),
        http_request=_request_from("127.0.0.1"),
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
            request_id="response-same",
            decisions=[
                HeadlessResumeDecision(
                    request_id=permission["id"],
                    decision="reject",
                    scope="once",
                )
            ],
        ),
        http_request=_request_from("127.0.0.1"),
    )
    assert retried["status"] == "completed"
    assert retried["run_id"] == "run-same"

    with pytest.raises(HTTPException) as conflict:
        await resume_headless_run(
            "run-same",
            HeadlessResumeRequest(
                continuation_token=paused["continuation_token"],
                request_id="response-same",
                decisions=[
                    HeadlessResumeDecision(
                        request_id=permission["id"],
                        decision="approve",
                        scope="once",
                    )
                ],
            ),
            http_request=_request_from("127.0.0.1"),
        )
    assert conflict.value.status_code == 409


@pytest.mark.asyncio
async def test_headless_cancel_ends_run_but_keeps_session_handle(monkeypatch):
    class FakeExecution:
        run_id = "run-cancel"
        session_id = "worker-session-cancel"
        done = False

        async def cancel(self):
            self.done = True

        def response(self):
            return {
                "run_id": self.run_id,
                "session_id": self.session_id,
                "status": "cancelled",
                "outcome": "cancelled",
            }

    execution = FakeExecution()
    monkeypatch.setattr(headless_api, "_headless_executions", {execution.session_id: execution})
    monkeypatch.setattr(
        headless_api,
        "_claim_headless_session",
        lambda _session_id: (_ for _ in ()).throw(
            AssertionError("out-of-band cancellation must not wait for the stream request lock")
        ),
    )
    monkeypatch.setattr(headless_api, "_attach_session_lifecycle", lambda response, _session_id: response)

    # Before run_started provides a Run id, CLI/PuddingTeams can already cancel
    # with the session handle emitted by run_starting.
    response = await cancel_headless_run("worker-session-cancel", request=_request_from("127.0.0.1"))
    assert response["status"] == "cancelled"
    assert execution.done is True


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
        "_model_options",
        lambda: [{"id": "sales"}, {"id": "product"}],
    )

    async def fake_route(_message):
        return AnalyticsModelRoute("ambiguous", None, 0.51, "semantic", "two plausible models")

    async def fail_consume(**_kwargs):
        raise AssertionError("ambiguous routing must not start an Agent Run")

    monkeypatch.setattr(headless_api, "_route_analytics_model", fake_route)
    monkeypatch.setattr(headless_api, "_consume_run", fail_consume)

    response = await create_headless_run(
        HeadlessRunRequest(message="帮我分析一下"),
        http_request=_request_from("127.0.0.1"),
    )

    assert response["status"] == "needs_input"
    assert response["outcome"] == "analytics_model_clarification_required"
    assert response["needs_input"]["type"] == "analytics_model_clarification"
    assert response["analytics_model_match"]["selected_id"] is None
    assert not list((tmp_path / "sessions").glob("worker-session-*.json"))


@pytest.mark.asyncio
async def test_continuous_session_reuses_bound_model_without_rerouting(tmp_path: Path, monkeypatch):
    session_manager.initialize(tmp_path)
    project_id = _register_test_worker_project(tmp_path)
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
    monkeypatch.setattr(headless_api, "_model_options", lambda: [{"id": "product"}])
    monkeypatch.setattr(headless_api, "_ensure_worker_project", lambda: (project_id, tmp_path))

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
        http_request=_request_from("127.0.0.1"),
    )

    assert response["analytics_model_id"] == "product"
    assert response["analytics_model_match"]["strategy"] == "session_bound"


@pytest.mark.asyncio
async def test_general_route_runs_session_without_model(tmp_path: Path, monkeypatch):
    session_manager.initialize(tmp_path)
    project_id = _register_test_worker_project(tmp_path)
    monkeypatch.setattr(
        headless_api,
        "_model_options",
        lambda: [{"id": "sales"}, {"id": "product"}],
    )
    monkeypatch.setattr(headless_api, "_ensure_worker_project", lambda: (project_id, tmp_path))

    async def fake_route(_message):
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
        http_request=_request_from("127.0.0.1"),
    )

    assert response["status"] == "completed"
    assert consumed["analytics_model_id"] == ""
    assert session_manager.get_metadata(response["session_id"])["analytics_model_id"] == ""


@pytest.mark.asyncio
async def test_continuous_general_session_reroutes_and_can_bind_later(tmp_path: Path, monkeypatch):
    session_manager.initialize(tmp_path)
    project_id = _register_test_worker_project(tmp_path)
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
        "_model_options",
        lambda: [{"id": "sales"}, {"id": "product"}],
    )
    monkeypatch.setattr(headless_api, "_ensure_worker_project", lambda: (project_id, tmp_path))

    routes = [
        AnalyticsModelRoute("general", None, 0.9, "semantic", "weather_question"),
        AnalyticsModelRoute("matched", "product", 0.96, "semantic", "matched business scope"),
    ]

    async def fake_route(_message):
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
        http_request=_request_from("127.0.0.1"),
    )
    assert general["status"] == "completed"
    assert consumed[0]["analytics_model_id"] == ""
    assert session_manager.get_metadata(session_id)["analytics_model_id"] == ""

    bound = await create_headless_run(
        HeadlessRunRequest(message="看看空气悬架配置率", session_id=session_id),
        http_request=_request_from("127.0.0.1"),
    )
    assert bound["status"] == "completed"
    assert consumed[1]["analytics_model_id"] == "product"
    assert session_manager.get_metadata(session_id)["analytics_model_id"] == "product"
