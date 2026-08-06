import json
import logging

from fastapi import FastAPI
from fastapi.testclient import TestClient


def test_stream_agent_persists_user_message_before_stream(monkeypatch, tmp_path):
    from api import agent as agent_api
    from graph.session_manager import session_manager

    session_manager.initialize(tmp_path)
    session_manager.create_session("agent-api-session")

    async def fake_astream(**kwargs):
        history = session_manager.load_session(kwargs["session_id"])
        assert [message["role"] for message in history] == ["user"]
        assert history[0]["content"] == "新任务先落盘"
        assert kwargs["user_message_already_persisted"] is True
        assert kwargs["goal_mode"] is False
        assert kwargs["goal_id"] is None
        assert kwargs["context_goal_id"] is None
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


def test_agent_request_model_selection_overrides_persisted_session_selection(monkeypatch, tmp_path):
    from api import agent as agent_api
    from graph.session_manager import session_manager

    session_manager.initialize(tmp_path)
    session_manager.create_session(
        "agent-model-request-priority",
        metadata={
            "llm_model_id": "deepseek:deepseek:deepseek-v4-pro:llm",
            "thinking_level": "high",
            "credential_name": "default",
        },
    )

    def fake_llm_config(*, model_id_override=None, thinking_level=None, credential_name=None, **_kwargs):
        assert model_id_override == "kimi:kimi-openai:kimi-k3:llm"
        assert thinking_level == "max"
        assert credential_name == "evaluation"
        return {
            "model_id": model_id_override,
            "provider": "kimi",
            "model": "kimi-k3",
            "thinking_level": thinking_level,
            "credential_name": credential_name,
        }

    async def fake_astream(**kwargs):
        assert kwargs["llm_model_id"] == "kimi:kimi-openai:kimi-k3:llm"
        assert kwargs["thinking_level"] == "max"
        assert kwargs["credential_name"] == "evaluation"
        yield {"event": "done", "data": "{}"}

    monkeypatch.setattr(agent_api, "get_fallback_llm_config", fake_llm_config)
    monkeypatch.setattr(agent_api.deepagents_agent_manager, "astream", fake_astream)

    app = FastAPI()
    app.include_router(agent_api.router, prefix="/api")
    response = TestClient(app).post(
        "/api/agent",
        json={
            "message": "使用对话框刚选的 Kimi",
            "session_id": "agent-model-request-priority",
            "llm_model_id": "kimi:kimi-openai:kimi-k3:llm",
            "thinking_level": "max",
            "credential_name": "evaluation",
            "stream": True,
        },
    )

    assert response.status_code == 200
    metadata = session_manager.get_metadata("agent-model-request-priority")
    assert metadata["llm_model_id"] == "kimi:kimi-openai:kimi-k3:llm"
    assert metadata["thinking_level"] == "max"
    assert metadata["credential_name"] == "evaluation"


def test_agent_uses_persisted_conversation_model_when_request_omits_selection(
    monkeypatch, tmp_path
):
    from api import agent as agent_api
    from graph.session_manager import session_manager

    session_manager.initialize(tmp_path)
    session_manager.create_session(
        "agent-model-session-fallback",
        metadata={
            "llm_model_id": "kimi:kimi-openai:kimi-k3:llm",
            "thinking_level": "high",
        },
    )

    def fake_llm_config(*, model_id_override=None, thinking_level=None, **_kwargs):
        assert model_id_override == "kimi:kimi-openai:kimi-k3:llm"
        assert thinking_level == "high"
        return {
            "model_id": model_id_override,
            "provider": "kimi",
            "model": "kimi-k3",
            "thinking_level": thinking_level,
        }

    async def fake_astream(**kwargs):
        assert kwargs["llm_model_id"] == "kimi:kimi-openai:kimi-k3:llm"
        assert kwargs["thinking_level"] == "high"
        yield {"event": "done", "data": "{}"}

    monkeypatch.setattr(agent_api, "get_fallback_llm_config", fake_llm_config)
    monkeypatch.setattr(agent_api.deepagents_agent_manager, "astream", fake_astream)

    app = FastAPI()
    app.include_router(agent_api.router, prefix="/api")
    response = TestClient(app).post(
        "/api/agent",
        json={
            "message": "继续使用当前对话的 Kimi",
            "session_id": "agent-model-session-fallback",
            "stream": True,
        },
    )

    assert response.status_code == 200


def test_agent_api_forwards_explicit_goal_mode(monkeypatch, tmp_path):
    from api import agent as agent_api
    from graph.session_manager import session_manager

    session_manager.initialize(tmp_path)
    session_manager.create_session("goal-api-session")

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


def test_goal_start_control_is_not_persisted_as_a_user_message(monkeypatch, tmp_path):
    from api import agent as agent_api
    from graph.session_manager import session_manager

    session_manager.initialize(tmp_path)
    session_manager.create_session("goal-start-control-session")

    async def fake_astream(**kwargs):
        assert kwargs["goal_mode"] is True
        assert kwargs["goal_id"] == "goal-existing"
        assert kwargs["goal_control_action"] == "start"
        assert kwargs["user_message_already_persisted"] is True
        assert session_manager.load_session(kwargs["session_id"]) == []
        yield {"event": "done", "data": "{}"}

    monkeypatch.setattr(agent_api.deepagents_agent_manager, "astream", fake_astream)

    app = FastAPI()
    app.include_router(agent_api.router, prefix="/api")
    response = TestClient(app).post(
        "/api/agent",
        json={
            "message": "继续执行当前目标",
            "session_id": "goal-start-control-session",
            "goal_mode": True,
            "goal_id": "goal-existing",
            "goal_control_action": "start",
            "stream": True,
        },
    )

    assert response.status_code == 200
    assert session_manager.load_session("goal-start-control-session") == []


def test_goal_start_control_requires_an_explicit_goal(tmp_path):
    from api import agent as agent_api
    from graph.session_manager import session_manager

    session_manager.initialize(tmp_path)
    session_manager.create_session("invalid-goal-start-control")
    app = FastAPI()
    app.include_router(agent_api.router, prefix="/api")

    response = TestClient(app).post(
        "/api/agent",
        json={
            "message": "继续执行当前目标",
            "session_id": "invalid-goal-start-control",
            "goal_control_action": "start",
            "stream": True,
        },
    )

    assert response.status_code == 422


def test_agent_api_forwards_goal_as_context_without_execution_ownership(monkeypatch, tmp_path):
    from api import agent as agent_api
    from graph.session_manager import session_manager

    session_manager.initialize(tmp_path)
    session_manager.create_session("goal-context-api-session")

    async def fake_astream(**kwargs):
        assert kwargs["goal_mode"] is False
        assert kwargs["goal_id"] is None
        assert kwargs["context_goal_id"] == "goal-existing"
        yield {"event": "done", "data": "{}"}

    monkeypatch.setattr(agent_api.deepagents_agent_manager, "astream", fake_astream)

    app = FastAPI()
    app.include_router(agent_api.router, prefix="/api")
    response = TestClient(app).post(
        "/api/agent",
        json={
            "message": "总结一下当前进度",
            "session_id": "goal-context-api-session",
            "goal_mode": False,
            "context_goal_id": "goal-existing",
            "stream": True,
        },
    )

    assert response.status_code == 200


def test_agent_api_rejects_conflicting_goal_owner_and_context(tmp_path):
    from api import agent as agent_api
    from graph.session_manager import session_manager

    session_manager.initialize(tmp_path)
    session_manager.create_session("goal-conflict-api-session")
    app = FastAPI()
    app.include_router(agent_api.router, prefix="/api")

    response = TestClient(app).post(
        "/api/agent",
        json={
            "message": "继续",
            "session_id": "goal-conflict-api-session",
            "goal_mode": True,
            "goal_id": "goal-a",
            "context_goal_id": "goal-b",
            "stream": True,
        },
    )

    assert response.status_code == 422


def test_agent_api_logs_request_to_first_agent_text_and_tool(monkeypatch, tmp_path, caplog):
    from api import agent as agent_api
    from graph.session_manager import session_manager

    session_manager.initialize(tmp_path)
    session_manager.create_session("latency-session")

    async def fake_astream(**_kwargs):
        yield {
            "event": "run_status_changed",
            "data": json.dumps(
                {
                    "query_id": "query-latency",
                    "run_id": "run-latency",
                    "status": "running",
                }
            ),
        }
        yield {
            "event": "reasoning",
            "data": json.dumps({"status": "delta", "content": "开始分析"}, ensure_ascii=False),
        }
        yield {
            "event": "tool_start",
            "data": json.dumps({"tool": "web_search", "id": "call-latency"}),
        }
        yield {"event": "done", "data": json.dumps({"content": "完成"}, ensure_ascii=False)}

    monkeypatch.setattr(agent_api.deepagents_agent_manager, "astream", fake_astream)
    assert agent_api.logger.isEnabledFor(logging.INFO)
    caplog.set_level(logging.INFO, logger=agent_api.__name__)

    app = FastAPI()
    app.include_router(agent_api.router, prefix="/api")
    response = TestClient(app).post(
        "/api/agent",
        json={
            "message": "测试首字延迟",
            "session_id": "latency-session",
            "stream": True,
        },
    )

    assert response.status_code == 200
    messages = [record.getMessage() for record in caplog.records if "[agent-latency]" in record.getMessage()]
    assert any("metric=request_received" in message for message in messages)
    assert any(
        "metric=first_stream_event" in message and "query=query-latency" in message
        for message in messages
    )
    assert any(
        "metric=first_agent_text" in message and "kind=reasoning" in message
        for message in messages
    )
    assert any(
        "metric=first_tool_start" in message and "tool=web_search" in message
        for message in messages
    )
    assert any(
        "metric=stream_finished" in message and "completed=True" in message
        for message in messages
    )


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
