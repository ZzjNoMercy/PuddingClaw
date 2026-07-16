from fastapi import FastAPI
from fastapi.testclient import TestClient


def test_stream_agent_persists_user_message_before_stream(monkeypatch, tmp_path):
    from api import agent as agent_api
    from graph.session_manager import session_manager

    session_manager.initialize(tmp_path)

    async def fake_astream(**kwargs):
        history = session_manager.load_session(kwargs["session_id"])
        assert [message["role"] for message in history] == ["user"]
        assert history[0]["content"] == "新任务先落盘"
        assert kwargs["user_message_already_persisted"] is True
        assert kwargs["goal_mode"] is False
        assert kwargs["goal_id"] is None
        yield {"event": "done", "data": "{}"}

    monkeypatch.setattr(agent_api.deepagents_agent_manager, "astream", fake_astream)

    app = FastAPI()
    app.include_router(agent_api.router, prefix="/api")
    client = TestClient(app)

    response = client.post(
        "/api/agent",
        json={
            "message": "新任务先落盘",
            "session_id": "agent-api-session",
            "stream": True,
        },
    )

    assert response.status_code == 200
    history = session_manager.load_session("agent-api-session")
    assert [message["role"] for message in history] == ["user"]
    assert history[0]["content"] == "新任务先落盘"


def test_agent_api_forwards_explicit_goal_mode(monkeypatch, tmp_path):
    from api import agent as agent_api
    from graph.session_manager import session_manager

    session_manager.initialize(tmp_path)

    async def fake_astream(**kwargs):
        assert kwargs["goal_mode"] is True
        assert kwargs["goal_id"] == "goal-existing"
        yield {"event": "done", "data": "{}"}

    monkeypatch.setattr(agent_api.deepagents_agent_manager, "astream", fake_astream)

    app = FastAPI()
    app.include_router(agent_api.router, prefix="/api")
    response = TestClient(app).post(
        "/api/agent",
        json={
            "message": "继续推进",
            "session_id": "goal-api-session",
            "goal_mode": True,
            "goal_id": "goal-existing",
            "stream": True,
        },
    )

    assert response.status_code == 200


def test_agent_api_rejects_goal_id_without_goal_mode():
    from api import agent as agent_api

    app = FastAPI()
    app.include_router(agent_api.router, prefix="/api")
    response = TestClient(app).post(
        "/api/agent",
        json={
            "message": "不得偷偷开启 Goal",
            "session_id": "invalid-goal-api-session",
            "goal_mode": False,
            "goal_id": "goal-forged",
            "stream": True,
        },
    )

    assert response.status_code == 422
    assert "goal_id requires goal_mode=true" in response.text


def test_tool_context_status_endpoint_returns_persisted_job(tmp_path):
    from api import agent as agent_api
    from graph.session_manager import session_manager

    session_manager.initialize(tmp_path)
    session_manager.create_session("status-session", metadata={"runtime_mode": "agent"})
    data = session_manager.get_raw_messages("status-session")
    data["tool_context_job"] = {
        "id": "toolctx-status",
        "status": "running",
        "candidate_count": 3,
        "completed_count": 1,
    }
    session_manager._write_file("status-session", data)

    app = FastAPI()
    app.include_router(agent_api.router, prefix="/api")
    response = TestClient(app).get("/api/agent/tool-context/status/status-session")

    assert response.status_code == 200
    assert response.json() == {**data["tool_context_job"], "revision": 0}
