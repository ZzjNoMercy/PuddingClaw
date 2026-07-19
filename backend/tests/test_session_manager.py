"""SessionManager 持久化与 reasoning_content 处理测试。"""

import pytest

from graph.session_manager import session_manager


def test_metadata_cannot_create_or_overwrite_control_plane_state(tmp_path):
    session_manager.initialize(tmp_path)

    with pytest.raises(FileNotFoundError):
        session_manager.update_metadata("missing", {"runtime_mode": "agent"})

    session_manager.create_session("protected")
    with pytest.raises(ValueError, match="Unsupported Session metadata"):
        session_manager.update_metadata(
            "protected",
            {"permissions": {"approval_mode": "smart"}},
        )
    assert session_manager.get_permission_policy("protected")["approval_mode"] == "strict"


def test_deleted_session_cannot_be_recreated_by_message_writes(tmp_path):
    session_manager.initialize(tmp_path)
    session_manager.create_session("deleted")
    session_manager.delete_session("deleted")

    with pytest.raises(FileNotFoundError):
        session_manager.save_message("deleted", "user", "must not reappear")
    with pytest.raises(FileNotFoundError):
        session_manager.upsert_assistant_message(
            "deleted",
            query_id="query-1",
            content="must not reappear",
        )

    assert not (tmp_path / "sessions" / "deleted.json").exists()


def test_analytics_model_id_round_trips_in_session_metadata(tmp_path):
    session_manager.initialize(tmp_path)
    session_manager.create_session(
        "analytics-model-session",
        metadata={"analytics_model_id": "产品配置分析"},
    )

    assert session_manager.get_metadata("analytics-model-session")["analytics_model_id"] == "产品配置分析"
    listed = {item["id"]: item for item in session_manager.list_sessions()}
    assert listed["analytics-model-session"]["analytics_model_id"] == "产品配置分析"

    cleared = session_manager.update_metadata(
        "analytics-model-session",
        {"analytics_model_id": None},
    )
    assert "analytics_model_id" in cleared
    assert cleared["analytics_model_id"] is None
    assert session_manager.get_metadata("analytics-model-session")["analytics_model_id"] is None


def test_todo_ledgers_continue_same_goal_revision_without_cross_contamination(tmp_path):
    session_manager.initialize(tmp_path)
    session_manager.create_session("todo-scope")
    todos = [
        {
            "id": "todo-1",
            "content": "更新图表",
            "status": "in_progress",
            "goal_id": "goal-1",
            "goal_revision": 1,
            "created_run_id": "run-1",
        }
    ]
    session_manager.update_todos(
        "todo-scope",
        todos,
        goal_id="goal-1",
        goal_revision=1,
        run_id="run-1",
    )

    assert session_manager.get_todos(
        "todo-scope", goal_id="goal-1", goal_revision=1, run_id="run-2"
    ) == todos
    assert session_manager.get_todos(
        "todo-scope", goal_id="goal-1", goal_revision=2, run_id="run-3"
    ) == []
    assert session_manager.get_todos(
        "todo-scope", goal_id="goal-2", goal_revision=1, run_id="run-4"
    ) == []
    assert session_manager.get_todos("todo-scope", run_id="standalone-run") == []


def test_save_and_load_reasoning_content_for_tool_call_turn(tmp_path):
    session_manager.initialize(tmp_path)
    session_manager.create_session("reasoning-session")

    session_manager.save_message(
        "reasoning-session",
        "assistant",
        "正式回答",
        tool_calls=[{"tool": "terminal", "input": "ls"}],
        reasoning_content="我需要先列出目录内容。",
    )

    history = session_manager.load_session("reasoning-session")
    assistant = history[0]
    assert assistant["role"] == "assistant"
    assert assistant["content"] == "正式回答"
    assert assistant["reasoning_content"] == "我需要先列出目录内容。"


def test_load_session_for_agent_includes_reasoning_for_tool_calls(tmp_path):
    session_manager.initialize(tmp_path)
    session_manager.create_session("agent-reasoning-session")

    session_manager.save_message(
        "agent-reasoning-session",
        "assistant",
        "正式回答",
        tool_calls=[{"tool": "terminal", "input": "ls"}],
        reasoning_content="我需要先列出目录内容。",
    )

    messages = session_manager.load_session_for_agent("agent-reasoning-session")
    assistant = messages[0]
    assert assistant["role"] == "assistant"
    assert assistant["reasoning_content"] == "我需要先列出目录内容。"


def test_load_session_for_agent_includes_tool_output_context_without_tool_calls(tmp_path):
    session_manager.initialize(tmp_path)
    session_manager.create_session("agent-tool-output-session")

    session_manager.save_message(
        "agent-tool-output-session",
        "assistant",
        "现在查询比亚迪 2023 年 5 月销量。",
        tool_calls=[
            *[
                {
                    "tool": "pandas_knowledge_query",
                    "input": f'{{"query": "前置长输出 {idx}"}}',
                    "output": "前置工具输出。" * 120,
                }
                for idx in range(5)
            ],
            {
                "tool": "pandas_knowledge_query",
                "input": '{"query": "比亚迪汽车 2023年5月 销量"}',
                "output": "筛选比亚迪汽车且月份为5后，2023年销量总和为205390。",
            },
        ],
    )

    messages = session_manager.load_session_for_agent("agent-tool-output-session")
    assistant = messages[0]
    assert assistant["role"] == "assistant"
    assert "tool_calls" not in assistant
    assert "历史工具结果摘要" in assistant["content"]
    assert "205390" in assistant["content"]
    assert "pandas_knowledge_query" in assistant["content"]


def test_upsert_assistant_message_replaces_same_query_draft(tmp_path):
    session_manager.initialize(tmp_path)
    session_manager.create_session("draft-session")

    session_manager.save_message("draft-session", "user", "查一下销量")
    session_manager.upsert_assistant_message(
        "draft-session",
        query_id="query-1",
        content="先查到一部分",
        tool_calls=[{"id": "call-1", "tool": "database_knowledge_query", "output": "100"}],
        status="running",
    )
    session_manager.upsert_assistant_message(
        "draft-session",
        query_id="query-1",
        content="最终结果",
        tool_calls=[{"id": "call-1", "tool": "database_knowledge_query", "output": "100"}],
        error_notice="模型连接中断",
        status="error",
    )

    history = session_manager.load_session("draft-session")
    assert [message["role"] for message in history] == ["user", "assistant"]
    assistant = history[1]
    assert assistant["query_id"] == "query-1"
    assert assistant["content"] == "最终结果"
    assert assistant["status"] == "error"
    assert assistant["error_notice"] == "模型连接中断"


def test_reasoning_content_saved_for_plain_assistant(tmp_path):
    session_manager.initialize(tmp_path)
    session_manager.create_session("plain-session")

    # 为了历史回看，即使没有工具调用也持久化 reasoning_content
    session_manager.save_message(
        "plain-session",
        "assistant",
        "你好",
        reasoning_content="单纯问候也保存推理",
    )

    history = session_manager.load_session("plain-session")
    assistant = history[0]
    assert assistant["reasoning_content"] == "单纯问候也保存推理"


def test_update_and_get_todos(tmp_path):
    session_manager.initialize(tmp_path)
    session_manager.create_session("todo-session")

    todos = [
        {"id": "todo-1", "content": "step one", "status": "pending"},
        {"id": "todo-2", "content": "step two", "status": "in_progress"},
    ]
    session_manager.update_todos("todo-session", todos)

    loaded = session_manager.get_todos("todo-session")
    assert loaded == todos

    raw = session_manager.get_raw_messages("todo-session")
    assert raw["todos"] == todos


def test_update_and_get_trace(tmp_path):
    session_manager.initialize(tmp_path)
    session_manager.create_session("trace-session")

    trace = {
        "trace_id": "trace-1",
        "query_id": "query-1",
        "session_id": "trace-session",
        "started_at": 1.0,
        "completed_at": 2.0,
        "status": "completed",
        "spans": [
            {"id": "root", "parent_id": None, "type": "root", "name": "agent.run"}
        ],
    }
    session_manager.update_trace("trace-session", trace, query_id="query-1")

    loaded = session_manager.get_trace("trace-session")
    assert loaded == trace

    raw = session_manager.get_raw_messages("trace-session")
    assert "trace" not in raw
    assert "traces" not in raw

    persisted_session = session_manager._read_file("trace-session")
    assert "trace" not in persisted_session
    assert "traces" not in persisted_session
    trace_state = session_manager.get_trace_state("trace-session")
    assert trace_state["latest_query_id"] == "query-1"
    assert trace_state["latest_trace_id"] == "trace-1"
    assert trace_state["traces"]["query-1"] == trace


def test_update_trace_keeps_history_without_duplicating_latest_trace(tmp_path):
    session_manager.initialize(tmp_path)
    session_manager.create_session("trace-session-2")

    first = {
        "trace_id": "trace-1",
        "query_id": "query-1",
        "session_id": "trace-session-2",
        "started_at": 1.0,
        "completed_at": 2.0,
        "status": "completed",
        "spans": [],
    }
    second = {
        "trace_id": "trace-2",
        "query_id": "query-2",
        "session_id": "trace-session-2",
        "started_at": 3.0,
        "completed_at": 4.0,
        "status": "completed",
        "spans": [],
    }

    session_manager.update_trace("trace-session-2", first, query_id="query-1")
    session_manager.update_trace("trace-session-2", second, query_id="query-2")

    assert session_manager.get_trace("trace-session-2") == second
    traces = session_manager.get_traces("trace-session-2")
    assert traces["query-1"] == first
    assert traces["query-2"] == second
    trace_sidecar = session_manager._read_trace_file("trace-session-2")
    assert "trace" not in trace_sidecar


def test_legacy_embedded_traces_are_migrated_once(tmp_path):
    import json

    session_manager.initialize(tmp_path)
    session_manager.create_session("legacy-trace-session")
    path = session_manager._session_path("legacy-trace-session")
    data = json.loads(path.read_text(encoding="utf-8"))
    trace = {
        "trace_id": "legacy-trace-1",
        "query_id": "legacy-query-1",
        "session_id": "legacy-trace-session",
        "spans": [{"id": "root", "type": "root"}],
    }
    data.update({
        "trace": trace,
        "traces": {"legacy-query-1": trace},
        "latest_query_id": "legacy-query-1",
        "latest_trace_id": "legacy-trace-1",
    })
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

    assert session_manager.list_sessions()[0]["id"] == "legacy-trace-session"
    assert session_manager.load_session("legacy-trace-session") == []
    migrated = json.loads(path.read_text(encoding="utf-8"))
    assert "trace" not in migrated
    assert "traces" not in migrated
    assert session_manager.get_trace("legacy-trace-session") == trace
    sidecar = json.loads(
        session_manager._trace_path("legacy-trace-session").read_text(encoding="utf-8")
    )
    assert "trace" not in sidecar
    assert sidecar["traces"] == {"legacy-query-1": trace}


def test_conversation_history_does_not_read_trace_sidecar(tmp_path, monkeypatch):
    from pathlib import Path

    session_manager.initialize(tmp_path)
    session_manager.create_session("fast-history-session")
    session_manager.save_message("fast-history-session", "user", "只读取消息")
    session_manager.update_trace(
        "fast-history-session",
        {"trace_id": "trace-large", "query_id": "query-large", "spans": []},
        query_id="query-large",
    )
    trace_path = session_manager._trace_path("fast-history-session")
    original_read_text = Path.read_text

    def guarded_read_text(path, *args, **kwargs):
        if path == trace_path:
            raise AssertionError("conversation history must not read the trace sidecar")
        return original_read_text(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", guarded_read_text)

    assert session_manager.load_session("fast-history-session") == [
        {"role": "user", "content": "只读取消息"}
    ]
    assert session_manager.get_raw_messages("fast-history-session")["messages"] == [
        {"role": "user", "content": "只读取消息"}
    ]


def test_assistant_output_attachments_survive_draft_upserts_and_history_reload(tmp_path):
    session_manager.initialize(tmp_path)
    session_manager.create_session("attachment-output-session")
    output = {
        "id": "att_generated123",
        "name": "修改版.html",
        "type": "file",
        "mime_type": "text/html",
        "size": 321,
        "source": "generated",
        "sha256": "sha256:abc",
        "derived_from": "att_source123",
        "created_by_run_id": "run-1",
        "created_by_query_id": "query-1",
        "download_url": "/api/attachments/att_generated123/download?session_id=attachment-output-session",
    }

    session_manager.upsert_assistant_message(
        "attachment-output-session",
        query_id="query-1",
        content="已生成",
        output_attachments=[output],
        status="running",
    )
    session_manager.upsert_assistant_message(
        "attachment-output-session",
        query_id="query-1",
        content="已生成并验证",
        output_attachments=[output],
        status="completed",
    )

    history = session_manager.load_session("attachment-output-session")
    assert len(history) == 1
    assert history[0]["status"] == "completed"
    restored = history[0]["output_attachments"][0]
    assert {key: restored[key] for key in output} == output
