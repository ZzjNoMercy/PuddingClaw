"""API tests for persisted Run/Goal state and explicit Goal controls."""

from fastapi import FastAPI
from fastapi.testclient import TestClient

from graph.session_manager import session_manager
from harness.coordinators import HarnessRunCoordinator
from harness.models import GoalRecord


def _client(tmp_path) -> TestClient:
    from api import permissions as permissions_api
    from api import sessions as sessions_api

    session_manager.initialize(tmp_path)
    session_manager.create_session("session-1", metadata={"runtime_mode": "agent"})
    app = FastAPI()
    app.include_router(sessions_api.router, prefix="/api")
    app.include_router(permissions_api.router, prefix="/api")
    return TestClient(app)


def test_create_session_atomically_persists_model_and_approval_mode(tmp_path):
    client = _client(tmp_path)

    response = client.post(
        "/api/sessions",
        json={"analytics_model_id": "sales-model", "approval_mode": "smart"},
    )

    assert response.status_code == 200
    created = response.json()
    assert created["analytics_model_id"] == "sales-model"
    assert created["approval_mode"] == "smart"
    assert created["policy_epoch"] == 1
    assert created["policy_version"] == "tool-execution-v3"
    mode = client.get(f"/api/sessions/{created['id']}/permissions/mode")
    assert mode.status_code == 200
    assert mode.json() == {
        "session_id": created["id"],
        "approval_mode": "smart",
        "policy_epoch": 1,
        "policy_version": "tool-execution-v3",
    }


def test_session_history_restores_persisted_todos_without_opening_trace(tmp_path):
    client = _client(tmp_path)
    todos = [
        {
            "id": "todo-1",
            "content": "更新外部报告",
            "status": "in_progress",
            "position": 0,
        }
    ]
    session_manager.update_todos("session-1", todos)

    response = client.get("/api/sessions/session-1/history")

    assert response.status_code == 200
    assert response.json()["todos"] == todos


def test_session_history_does_not_project_terminal_run_todos_as_current(tmp_path):
    client = _client(tmp_path)
    todos = [{"id": "todo-old", "content": "旧任务", "status": "completed"}]
    session_manager.update_todos("session-1", todos, run_id="run-old")
    data = session_manager._read_file("session-1")
    data["harness"] = {
        "runs": {"run-old": {"run_id": "run-old", "status": "completed"}},
        "run_order": ["run-old"],
        "latest_run_id": "run-old",
        "goals": {},
    }
    session_manager._write_file("session-1", data)

    response = client.get("/api/sessions/session-1/history")

    assert response.status_code == 200
    assert response.json()["todos"] == []
    assert response.json()["todos_authority"] == {"kind": "none"}


def test_permission_mode_api_rejects_stale_epoch_and_active_run(tmp_path):
    client = _client(tmp_path)

    changed = client.patch(
        "/api/sessions/session-1/permissions/mode",
        json={"approval_mode": "smart", "expected_epoch": 1},
    )
    assert changed.status_code == 200
    assert changed.json()["policy_epoch"] == 2

    stale = client.patch(
        "/api/sessions/session-1/permissions/mode",
        json={"approval_mode": "strict", "expected_epoch": 1},
    )
    assert stale.status_code == 409

    HarnessRunCoordinator(session_manager).start_run(
        session_id="session-1",
        query_id="query-active",
        objective="active",
        goal_mode=False,
    )
    active = client.patch(
        "/api/sessions/session-1/permissions/mode",
        json={"approval_mode": "strict", "expected_epoch": 2},
    )
    assert active.status_code == 409


def test_create_session_rejects_unknown_approval_mode(tmp_path):
    client = _client(tmp_path)

    response = client.post(
        "/api/sessions",
        json={"approval_mode": "permissive"},
    )

    assert response.status_code == 422


def test_external_permission_grant_does_not_create_a_missing_session(tmp_path):
    client = _client(tmp_path)

    response = client.post(
        "/api/sessions/missing-session/permissions/external-files",
        json={"target_kind": "all_external_files"},
    )

    assert response.status_code == 404
    assert response.json()["detail"] == "Session not found"

    listed = client.get("/api/sessions/missing-session/permissions")
    assert listed.status_code == 404
    assert listed.json()["detail"] == "Session not found"


def test_external_permission_endpoint_rejects_other_pending_request_types(tmp_path):
    from graph.permission_resume import permission_resume_registry

    client = _client(tmp_path)
    request_id = "tool-action-presented-to-external-endpoint"
    permission_resume_registry._requests[request_id] = {
        "id": request_id,
        "type": "tool_action",
        "session_id": "session-1",
        "status": "pending",
    }

    response = client.post(
        "/api/sessions/session-1/permissions/external-files",
        json={
            "target_kind": "all_external_files",
            "permission_request_id": request_id,
        },
    )

    assert response.status_code == 400
    assert response.json()["detail"] == "permission request is not an external file action"
    assert permission_resume_registry.get(request_id)["status"] == "pending"
    permission_resume_registry._requests.pop(request_id, None)


def test_session_harness_endpoint_returns_persisted_state(tmp_path):
    client = _client(tmp_path)
    goal = GoalRecord(
        goal_id="goal-1",
        session_id="session-1",
        objective="完成分析",
    )
    session_manager.upsert_goal_state(
        "session-1",
        goal.model_dump(mode="json"),
    )

    response = client.get("/api/sessions/session-1/harness")

    assert response.status_code == 200
    payload = response.json()
    assert payload["active_goal_id"] == "goal-1"
    assert payload["goals"]["goal-1"]["objective"] == "完成分析"


def test_goal_pause_resume_cancel_api(tmp_path):
    client = _client(tmp_path)
    goal = GoalRecord(
        goal_id="goal-1",
        session_id="session-1",
        objective="完成分析",
    )
    session_manager.upsert_goal_state(
        "session-1",
        goal.model_dump(mode="json"),
    )

    paused = client.post("/api/sessions/session-1/goals/goal-1/pause")
    assert paused.status_code == 200
    assert paused.json()["status"] == "paused"
    assert session_manager.get_active_goal_state("session-1") is None

    resumed = client.post("/api/sessions/session-1/goals/goal-1/resume")
    assert resumed.status_code == 200
    assert resumed.json()["status"] == "active"

    cancelled = client.post("/api/sessions/session-1/goals/goal-1/cancel")
    assert cancelled.status_code == 200
    assert cancelled.json()["status"] == "cancelled"

    invalid_resume = client.post("/api/sessions/session-1/goals/goal-1/resume")
    assert invalid_resume.status_code == 409


def test_goal_objective_update_api_versions_contract_and_rejects_stale_edit(tmp_path):
    client = _client(tmp_path)
    goal = GoalRecord(
        goal_id="goal-edit",
        session_id="session-1",
        objective="生成 2025 年报告",
    )
    session_manager.upsert_goal_state("session-1", goal.model_dump(mode="json"))

    updated = client.patch(
        "/api/sessions/session-1/goals/goal-edit",
        json={
            "objective": "分析 2026 年销量，并生成报告与趋势总结",
            "expected_revision": 1,
        },
    )

    assert updated.status_code == 200
    payload = updated.json()
    assert payload["objective_revision"] == 2
    assert payload["pending_revision"] is True
    assert [item["revision"] for item in payload["revisions"]] == [1, 2]
    assert payload["revisions"][0]["objective"] == "生成 2025 年报告"
    assert payload["goal_contract"]["contract_id"] == payload["revisions"][1]["contract_id"]
    assert "time_scope" in {
        item["id"] for item in payload["goal_contract"]["criteria"]
    }

    stale = client.patch(
        "/api/sessions/session-1/goals/goal-edit",
        json={"objective": "覆盖新目标", "expected_revision": 1},
    )
    assert stale.status_code == 409
    assert "revision conflict" in stale.json()["detail"]
