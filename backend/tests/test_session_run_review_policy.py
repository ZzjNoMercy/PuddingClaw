from fastapi import FastAPI
from fastapi.testclient import TestClient

from api import sessions as sessions_api
from graph.session_manager import session_manager


def _client(tmp_path) -> TestClient:
    session_manager.initialize(tmp_path)
    app = FastAPI()
    app.include_router(sessions_api.router, prefix="/api")
    return TestClient(app)


def test_session_run_review_policy_create_update_and_clear(tmp_path):
    client = _client(tmp_path)

    created = client.post(
        "/api/sessions",
        json={"run_review_policy": "shadow"},
    )
    assert created.status_code == 200
    session_id = created.json()["id"]
    assert created.json()["run_review_policy"] == "shadow"

    updated = client.patch(
        f"/api/sessions/{session_id}/run-review-policy",
        json={"run_review_policy": "blocking_one_shot"},
    )
    assert updated.status_code == 200
    assert updated.json()["run_review_policy"] == "blocking_one_shot"

    cleared = client.patch(
        f"/api/sessions/{session_id}/run-review-policy",
        json={"run_review_policy": None},
    )
    assert cleared.status_code == 200
    assert cleared.json()["run_review_policy"] is None

    listed = client.get("/api/sessions")
    assert listed.status_code == 200
    saved = next(item for item in listed.json()["sessions"] if item["id"] == session_id)
    assert saved["run_review_policy"] is None


def test_session_run_review_policy_rejects_unknown_value(tmp_path):
    client = _client(tmp_path)
    created = client.post("/api/sessions", json={})
    session_id = created.json()["id"]

    response = client.patch(
        f"/api/sessions/{session_id}/run-review-policy",
        json={"run_review_policy": "always"},
    )

    assert response.status_code == 422
