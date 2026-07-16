"""API tests for persisted Run/Goal state and explicit Goal controls."""

from fastapi import FastAPI
from fastapi.testclient import TestClient

from graph.session_manager import session_manager
from harness.models import GoalRecord


def _client(tmp_path) -> TestClient:
    from api import sessions as sessions_api

    session_manager.initialize(tmp_path)
    session_manager.create_session("session-1", metadata={"runtime_mode": "agent"})
    app = FastAPI()
    app.include_router(sessions_api.router, prefix="/api")
    return TestClient(app)


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
