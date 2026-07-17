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
    assert created["policy_version"] == "tool-execution-v2"
    mode = client.get(f"/api/sessions/{created['id']}/permissions/mode")
    assert mode.status_code == 200
    assert mode.json() == {
        "session_id": created["id"],
        "approval_mode": "smart",
        "policy_epoch": 1,
        "policy_version": "tool-execution-v2",
    }


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
