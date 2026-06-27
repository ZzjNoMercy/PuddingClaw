"""SessionManager 持久化与 reasoning_content 处理测试。"""

from graph.session_manager import session_manager


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
