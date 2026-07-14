"""SessionManager 持久化与 reasoning_content 处理测试。"""

from graph.session_manager import session_manager


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
    assert raw["trace"] == trace
    assert raw["latest_query_id"] == "query-1"
    assert raw["latest_trace_id"] == "trace-1"
    assert raw["traces"]["query-1"] == trace


def test_update_trace_keeps_latest_trace_compatible(tmp_path):
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
