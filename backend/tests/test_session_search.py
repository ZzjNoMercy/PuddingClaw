from fastapi import FastAPI
from fastapi.testclient import TestClient

from graph.session_manager import SessionManager, session_manager


def test_search_sessions_matches_title_and_visible_content(tmp_path):
    manager = SessionManager()
    manager.initialize(tmp_path)
    manager.create_session("session-title", metadata={"runtime_mode": "chat"})
    manager.rename_session("session-title", "季度复盘")
    manager.save_message("session-title", "user", "这条消息不包含目标词")

    manager.create_session(
        "session-content",
        metadata={"runtime_mode": "agent", "project_id": "project-1"},
    )
    manager.rename_session("session-content", "普通对话")
    manager.save_message("session-content", "user", "请帮我检查 SearchWidget 的交互细节")

    title_results = manager.search_sessions("季度")
    assert [item["id"] for item in title_results] == ["session-title"]
    assert title_results[0]["matched_in"] == "title"

    content_results = manager.search_sessions("searchwidget")
    assert [item["id"] for item in content_results] == ["session-content"]
    assert content_results[0]["matched_in"] == "content"
    assert "SearchWidget" in content_results[0]["snippet"]
    assert content_results[0]["project_id"] == "project-1"


def test_search_sessions_ignores_tool_payloads_and_includes_archived_messages(tmp_path):
    manager = SessionManager()
    manager.initialize(tmp_path)
    manager.create_session("session-archive")
    manager.save_message(
        "session-archive",
        "assistant",
        "可见回复",
        tool_calls=[{"tool": "read_file", "output": "tool-only-secret"}],
    )
    manager.save_message("session-archive", "user", "需要长期保留的归档关键词")
    manager.compress_history("session-archive", "摘要", 2)

    assert manager.search_sessions("tool-only-secret") == []
    results = manager.search_sessions("归档关键词")
    assert [item["id"] for item in results] == ["session-archive"]


def test_search_sessions_api(tmp_path):
    from api import sessions as sessions_api

    session_manager.initialize(tmp_path)
    session_manager.create_session("session-api")
    session_manager.rename_session("session-api", "接口搜索")
    session_manager.save_message("session-api", "user", "正文也可以命中")

    app = FastAPI()
    app.include_router(sessions_api.router, prefix="/api")
    client = TestClient(app)

    response = client.get("/api/sessions/search", params={"q": "正文"})

    assert response.status_code == 200
    assert response.json()["results"][0]["id"] == "session-api"
