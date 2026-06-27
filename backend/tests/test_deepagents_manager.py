"""Tests for PuddingClaw's DeepAgents runtime event adapter."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from deepagents.middleware.memory import MemoryMiddleware
from langchain_core.messages import AIMessage, AIMessageChunk, ToolMessage


def test_deepagents_manager_emits_and_persists_tool_events(tmp_path, monkeypatch):
    """Agent mode should expose DeepAgents tool calls like Chat mode does."""

    from graph import deepagents_manager as manager_module
    from graph.session_manager import session_manager
    from projects.registry import project_registry

    session_manager.initialize(tmp_path)
    project_registry.initialize(tmp_path)
    session_manager.create_session("agent-tool-session")

    class FakeDeepAgent:
        async def astream(self, *_args, **_kwargs):
            yield (
                "updates",
                {
                    "model": {
                        "messages": [
                            AIMessage(
                                content="",
                                tool_calls=[
                                    {
                                        "name": "read_file",
                                        "args": {"path": "/README.md"},
                                        "id": "call_read",
                                    }
                                ],
                            )
                        ]
                    }
                },
            )
            yield (
                "updates",
                {
                    "tools": {
                        "messages": [
                            ToolMessage(
                                content="README content",
                                tool_call_id="call_read",
                                name="read_file",
                            )
                        ]
                    }
                },
            )
            yield (
                "messages",
                (AIMessageChunk(content="已读取。"), {"langgraph_node": "model"}),
            )
            yield ("values", {"messages": [AIMessage(content="已读取。")]})

    create_kwargs = {}

    def fake_create_deep_agent(**kwargs):
        create_kwargs.update(kwargs)
        return FakeDeepAgent()

    monkeypatch.setattr(manager_module, "create_deep_agent", fake_create_deep_agent)

    async def no_title(_session_id: str):
        return None

    monkeypatch.setattr(manager_module, "_generate_title", no_title)

    runtime = manager_module.DeepAgentsAgentManager()
    runtime.initialize(Path(tmp_path))

    async def collect():
        return [
            event
            async for event in runtime.astream(
                message="读取 README",
                session_id="agent-tool-session",
                project_id=None,
                user_id="test-user",
            )
        ]

    events = asyncio.run(collect())
    event_names = [event["event"] for event in events]
    tool_start = next(event for event in events if event["event"] == "tool_start")
    tool_end = next(event for event in events if event["event"] == "tool_end")
    done = next(event for event in events if event["event"] == "done")
    history = session_manager.load_session("agent-tool-session")
    assistant_with_tool = next(
        message for message in history if message["role"] == "assistant" and message.get("tool_calls")
    )

    assert event_names == ["tool_start", "tool_end", "content_reset", "token", "citations_finalized", "done"]
    assert create_kwargs["skills"] == ["/skills/"]
    assert "memory" not in create_kwargs
    assert "middleware" in create_kwargs
    assert any(isinstance(m, MemoryMiddleware) for m in create_kwargs["middleware"])
    assert json.loads(tool_start["data"]) == {
        "tool": "read_file",
        "input": '{"path": "/README.md"}',
        "id": "call_read",
    }
    assert json.loads(tool_end["data"])["output"] == "README content"
    assert json.loads(done["data"])["content"] == "已读取。"
    assert assistant_with_tool["tool_calls"][0]["tool"] == "read_file"
    assert assistant_with_tool["tool_calls"][0]["output"] == "README content"


def test_deepagents_manager_emits_sources_citations_and_title(tmp_path, monkeypatch):
    """Agent mode should keep the Chat-mode source/citation/title contract."""

    from graph import deepagents_manager as manager_module
    from graph.citations import encode_tool_result
    from graph.session_manager import session_manager
    from projects.registry import project_registry

    session_manager.initialize(tmp_path)
    project_registry.initialize(tmp_path)
    session_manager.create_session("agent-citation-session")

    source = {
        "source_id": "src_aihot_demo",
        "title": "AI HOT 示例",
        "uri": "https://example.com/aihot",
        "document_id": "https://example.com/aihot",
        "chunk_id": "aihot-item",
        "source_type": "web",
        "quote": "AI HOT 返回的结构化来源。",
    }
    encoded = encode_tool_result("AI HOT 返回 1 条动态 [src_aihot_demo]", [source])

    class FakeDeepAgent:
        async def astream(self, *_args, **_kwargs):
            yield (
                "updates",
                {
                    "model": {
                        "messages": [
                            AIMessage(
                                content="",
                                tool_calls=[
                                    {
                                        "name": "terminal",
                                        "args": {"command": "python3 /skills/aihot/scripts/aihot_query.py"},
                                        "id": "call_aihot",
                                    }
                                ],
                            )
                        ]
                    }
                },
            )
            yield (
                "updates",
                {
                    "tools": {
                        "messages": [
                            ToolMessage(
                                content=f"[scripts/aihot_query.py] {encoded}",
                                tool_call_id="call_aihot",
                                name="terminal",
                            )
                        ]
                    }
                },
            )
            yield (
                "messages",
                (AIMessageChunk(content="今天的 AI 热点来自 AI HOT。[^src_aihot_demo]"), {"langgraph_node": "model"}),
            )
            yield ("values", {"messages": [AIMessage(content="今天的 AI 热点来自 AI HOT。[^src_aihot_demo]")]})

    monkeypatch.setattr(manager_module, "create_deep_agent", lambda **_kwargs: FakeDeepAgent())

    async def fake_generate_title(session_id: str):
        session_manager.update_title(session_id, "AI热点")
        return "AI热点"

    monkeypatch.setattr(manager_module, "_generate_title", fake_generate_title)

    runtime = manager_module.DeepAgentsAgentManager()
    runtime.initialize(Path(tmp_path))

    async def collect():
        return [
            event
            async for event in runtime.astream(
                message="今天 AI 有什么热点",
                session_id="agent-citation-session",
                project_id=None,
                user_id="test-user",
            )
        ]

    events = asyncio.run(collect())
    event_names = [event["event"] for event in events]
    source_found = next(event for event in events if event["event"] == "source_found")
    citations_finalized = next(event for event in events if event["event"] == "citations_finalized")
    title_event = next(event for event in events if event["event"] == "title")
    history = session_manager.load_session("agent-citation-session")
    tool_message = next(message for message in history if message["role"] == "assistant" and message.get("tool_calls"))
    final_message = history[-1]

    assert "source_found" in event_names
    assert "citations_finalized" in event_names
    assert json.loads(source_found["data"])["source"]["source_id"] == "src_aihot_demo"
    assert json.loads(citations_finalized["data"])["citations"][0]["source_id"] == "src_aihot_demo"
    assert json.loads(title_event["data"])["title"] == "AI热点"
    assert tool_message["tool_calls"][0]["output"] == "AI HOT 返回 1 条动态 [src_aihot_demo]"
    assert tool_message["tool_calls"][0]["raw_output"].startswith("[scripts/aihot_query.py]")
    assert final_message["sources"][0]["source_id"] == "src_aihot_demo"
    assert final_message["citations"][0]["source_id"] == "src_aihot_demo"


def test_deepagents_manager_separates_reasoning_from_final_answer(tmp_path, monkeypatch):
    """Reasoning-only chunks should not be persisted as the final answer."""

    from graph import deepagents_manager as manager_module
    from graph.session_manager import session_manager
    from projects.registry import project_registry

    monkeypatch.setattr(manager_module.config, "load_config", lambda: {"thinking_mode": True})

    session_manager.initialize(tmp_path)
    project_registry.initialize(tmp_path)
    session_manager.create_session("agent-reasoning-session")

    class FakeDeepAgent:
        async def astream(self, *_args, **_kwargs):
            yield (
                "messages",
                (
                    AIMessageChunk(
                        content="",
                        additional_kwargs={"reasoning_content": "这里是模型内部推理，不应作为正式答案。"},
                    ),
                    {"langgraph_node": "model"},
                ),
            )
            yield ("values", {"messages": [AIMessage(content="")]})

    monkeypatch.setattr(manager_module, "create_deep_agent", lambda **_kwargs: FakeDeepAgent())

    async def no_title(_session_id: str):
        return None

    monkeypatch.setattr(manager_module, "_generate_title", no_title)

    runtime = manager_module.DeepAgentsAgentManager()
    runtime.initialize(Path(tmp_path))

    async def collect():
        return [
            event
            async for event in runtime.astream(
                message="测试推理模型",
                session_id="agent-reasoning-session",
                project_id=None,
                user_id="test-user",
            )
        ]

    events = asyncio.run(collect())
    reasoning = next(event for event in events if event["event"] == "reasoning")
    token = next(event for event in events if event["event"] == "token")
    history = session_manager.load_session("agent-reasoning-session")
    assistant = next(message for message in history if message["role"] == "assistant")

    assert json.loads(reasoning["data"])["chars"] > 0
    assert "模型内部推理" in json.loads(reasoning["data"])["content"]
    assert "模型本轮只返回了 reasoning_content" in json.loads(token["data"])["content"]
    assert "模型内部推理" not in json.loads(token["data"])["content"]
    assert "模型内部推理" not in assistant["content"]


def test_deepagents_manager_extracts_reasoning_from_thinking_blocks(tmp_path, monkeypatch):
    """OpenAI-style reasoning models emit thinking blocks inside content."""

    from graph import deepagents_manager as manager_module
    from graph.session_manager import session_manager
    from projects.registry import project_registry

    monkeypatch.setattr(manager_module.config, "load_config", lambda: {"thinking_mode": True})

    session_manager.initialize(tmp_path)
    project_registry.initialize(tmp_path)
    session_manager.create_session("agent-thinking-session")

    class FakeDeepAgent:
        async def astream(self, *_args, **_kwargs):
            yield (
                "messages",
                (
                    AIMessageChunk(
                        content=[
                            {"type": "thinking", "thinking": "分析用户需求：查询今日 AI 热点。"},
                        ],
                    ),
                    {"langgraph_node": "model"},
                ),
            )
            yield (
                "messages",
                (
                    AIMessageChunk(
                        content=[
                            {"type": "thinking", "thinking": "调用 AI HOT 工具。"},
                            {"type": "text", "text": "以下是"},
                        ],
                    ),
                    {"langgraph_node": "model"},
                ),
            )
            yield (
                "values",
                {"messages": [AIMessage(content="以下是 AI HOT 热点新闻。")]},
            )

    monkeypatch.setattr(manager_module, "create_deep_agent", lambda **_kwargs: FakeDeepAgent())

    async def no_title(_session_id: str):
        return None

    monkeypatch.setattr(manager_module, "_generate_title", no_title)

    runtime = manager_module.DeepAgentsAgentManager()
    runtime.initialize(Path(tmp_path))

    async def collect():
        return [
            event
            async for event in runtime.astream(
                message="今天 AI 有什么热点",
                session_id="agent-thinking-session",
                project_id=None,
                user_id="test-user",
            )
        ]

    events = asyncio.run(collect())
    reasoning_events = [e for e in events if e["event"] == "reasoning"]
    token_events = [e for e in events if e["event"] == "token"]

    reasoning_text = "".join(json.loads(e["data"])["content"] for e in reasoning_events)
    assert "分析用户需求" in reasoning_text
    assert "调用 AI HOT 工具" in reasoning_text
    assert any("以下是" in json.loads(e["data"])["content"] for e in token_events)


def test_deepagents_manager_emits_interleaved_reasoning_and_content(tmp_path, monkeypatch):
    """A single chunk can carry both reasoning and visible text."""

    from graph import deepagents_manager as manager_module
    from graph.session_manager import session_manager
    from projects.registry import project_registry

    monkeypatch.setattr(manager_module.config, "load_config", lambda: {"thinking_mode": True})

    session_manager.initialize(tmp_path)
    project_registry.initialize(tmp_path)
    session_manager.create_session("agent-interleaved-session")

    class FakeDeepAgent:
        async def astream(self, *_args, **_kwargs):
            yield (
                "messages",
                (
                    AIMessageChunk(
                        content="正式回答。",
                        additional_kwargs={"reasoning_content": "内部推理过程。"},
                    ),
                    {"langgraph_node": "model"},
                ),
            )
            yield ("values", {"messages": [AIMessage(content="正式回答。")]})

    monkeypatch.setattr(manager_module, "create_deep_agent", lambda **_kwargs: FakeDeepAgent())

    async def no_title(_session_id: str):
        return None

    monkeypatch.setattr(manager_module, "_generate_title", no_title)

    runtime = manager_module.DeepAgentsAgentManager()
    runtime.initialize(Path(tmp_path))

    async def collect():
        return [
            event
            async for event in runtime.astream(
                message="测试交错输出",
                session_id="agent-interleaved-session",
                project_id=None,
                user_id="test-user",
            )
        ]

    events = asyncio.run(collect())
    reasoning = next(e for e in events if e["event"] == "reasoning")
    token = next(e for e in events if e["event"] == "token")

    assert json.loads(reasoning["data"])["content"] == "内部推理过程。"
    assert json.loads(token["data"])["content"] == "正式回答。"


def test_deepagents_manager_persists_reasoning_for_tool_call_turns(tmp_path, monkeypatch):
    """含工具调用的 assistant 消息必须把 reasoning_content 持久化以便回传 API。"""

    from graph import deepagents_manager as manager_module
    from graph.session_manager import session_manager
    from projects.registry import project_registry

    monkeypatch.setattr(manager_module.config, "load_config", lambda: {"thinking_mode": True})

    session_manager.initialize(tmp_path)
    project_registry.initialize(tmp_path)
    session_manager.create_session("agent-tool-reasoning-session")

    class FakeDeepAgent:
        async def astream(self, *_args, **_kwargs):
            yield (
                "updates",
                {
                    "model": {
                        "messages": [
                            AIMessage(
                                content="",
                                tool_calls=[
                                    {
                                        "name": "terminal",
                                        "args": {"command": "date"},
                                        "id": "call_date",
                                    }
                                ],
                            )
                        ]
                    }
                },
            )
            yield (
                "updates",
                {
                    "tools": {
                        "messages": [
                            ToolMessage(
                                content="2026-06-26",
                                tool_call_id="call_date",
                                name="terminal",
                            )
                        ]
                    }
                },
            )
            yield (
                "messages",
                (
                    AIMessageChunk(
                        content="今天",
                        additional_kwargs={"reasoning_content": "查看日期结果后回答。"},
                    ),
                    {"langgraph_node": "model"},
                ),
            )
            yield ("values", {"messages": [AIMessage(content="今天是 2026-06-26。")]})

    monkeypatch.setattr(manager_module, "create_deep_agent", lambda **_kwargs: FakeDeepAgent())

    async def no_title(_session_id: str):
        return None

    monkeypatch.setattr(manager_module, "_generate_title", no_title)

    runtime = manager_module.DeepAgentsAgentManager()
    runtime.initialize(Path(tmp_path))

    async def collect():
        return [
            event
            async for event in runtime.astream(
                message="今天几号",
                session_id="agent-tool-reasoning-session",
                project_id=None,
                user_id="test-user",
            )
        ]

    events = asyncio.run(collect())
    assert any(e["event"] == "reasoning" for e in events)

    history = session_manager.load_session("agent-tool-reasoning-session")
    assistant = next(
        msg for msg in history
        if msg["role"] == "assistant" and msg.get("tool_calls")
    )
    assert assistant["reasoning_content"] == "查看日期结果后回答。"

    # 验证下轮重建消息时同时包含 tool_calls 与 reasoning_content
    built = runtime._build_messages(history, "明天呢")  # noqa: SLF001
    assistant_entry = next(
        msg for msg in built if msg["role"] == "assistant" and msg.get("tool_calls")
    )
    assert assistant_entry["reasoning_content"] == "查看日期结果后回答。"
    assert assistant_entry["tool_calls"][0]["function"]["name"] == "terminal"


def test_deepagents_manager_adds_puddingclaw_terminal_scoped_to_workspace(tmp_path):
    """Agent mode keeps DeepAgents fs tools but adds PuddingClaw terminal."""

    from graph.deepagents_manager import DeepAgentsAgentManager

    workspace = tmp_path / "workspace"
    workspace.mkdir()

    runtime = DeepAgentsAgentManager()
    runtime.initialize(Path(__file__).resolve().parent.parent)

    tools = runtime._build_tools(workspace)  # noqa: SLF001 - intentional contract test
    by_name = {tool.name: tool for tool in tools}

    assert "terminal" in by_name
    assert by_name["terminal"].root_dir == str(workspace)
    assert by_name["terminal"].path_aliases["/skills"] == str(Path(__file__).resolve().parent.parent / "skills")
    assert "fetch_url" in by_name
    assert "read_file" not in by_name
    assert "write_file" not in by_name
    assert "execute_skill" not in by_name


def test_memory_dir_and_agents_md_creation(tmp_path):
    """Project memory should live under data/deepagents-memory and auto-create AGENTS.md."""

    from graph.deepagents_manager import DeepAgentsAgentManager

    runtime = DeepAgentsAgentManager()
    runtime.initialize(tmp_path)

    project_memory = runtime._memory_dir_for("proj_abc123")  # noqa: SLF001
    assert project_memory == tmp_path / "data" / "deepagents-memory" / "projects" / "proj_abc123"

    global_memory = runtime._memory_dir_for(None)  # noqa: SLF001
    assert global_memory == tmp_path / "data" / "deepagents-memory" / "global"

    agents_md = runtime._ensure_agents_md(project_memory)  # noqa: SLF001
    assert agents_md.exists()
    assert "Project Memory" in agents_md.read_text(encoding="utf-8")


def test_memory_middleware_includes_project_and_gstack(tmp_path):
    """When gstack/AGENTS.md exists, a single MemoryMiddleware loads both sources."""

    from graph.deepagents_manager import DeepAgentsAgentManager

    # Simulate backend layout with bundled gstack index
    backend_dir = tmp_path
    gstack_dir = backend_dir / "gstack"
    gstack_dir.mkdir(parents=True)
    (gstack_dir / "AGENTS.md").write_text("# GStack Skills\n", encoding="utf-8")

    runtime = DeepAgentsAgentManager()
    runtime.initialize(backend_dir)

    middlewares = runtime._build_middlewares("proj_abc123")  # noqa: SLF001
    assert len(middlewares) == 1
    mw = middlewares[0]
    assert isinstance(mw, MemoryMiddleware)
    assert "/AGENTS.md" in mw.sources
    assert "/gstack/AGENTS.md" in mw.sources
