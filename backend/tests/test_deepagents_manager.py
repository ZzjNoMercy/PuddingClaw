"""Tests for PuddingClaw's DeepAgents runtime event adapter."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from deepagents.middleware.memory import MemoryMiddleware
from langchain_core.messages import AIMessage, AIMessageChunk, ToolMessage


def test_middleware_inventory_uses_actual_hook_overrides(tmp_path):
    """Runtime inventory should not treat inherited no-op hooks as mounted hooks."""

    from deepagents.backends import FilesystemBackend
    from graph.deepagents_manager import DeepAgentsAgentManager

    middleware = MemoryMiddleware(
        backend=FilesystemBackend(root_dir=tmp_path, virtual_mode=True),
        sources=["/AGENTS.md"],
    )

    hooks = DeepAgentsAgentManager._middleware_hooks(middleware)
    inventory = DeepAgentsAgentManager._middleware_inventory([middleware], ["/skills/"])

    assert hooks == ["before_agent", "wrap_model_call"]
    assert [item["name"] for item in inventory["hooks"]["before_agent"]] == [
        "TodoListMiddleware",
        "SkillsMiddleware",
        "MemoryMiddleware",
    ]
    assert [item["name"] for item in inventory["hooks"]["after_model"]] == [
        "PatchToolCallsMiddleware",
        "TodoListMiddleware",
    ]


def test_build_middlewares_includes_model_call_limit(tmp_path, monkeypatch):
    import config
    from graph.deepagents_manager import DeepAgentsAgentManager

    config_path = tmp_path / "config.json"
    config_path.write_text(
        json.dumps(
            {
                "harness": {
                    "model_call_limit": {
                        "enabled": True,
                        "run_limit": 7,
                        "thread_limit": None,
                        "exit_behavior": "end",
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(config, "CONFIG_FILE", config_path)

    manager = DeepAgentsAgentManager()
    manager.initialize(tmp_path)

    middlewares = manager._build_middlewares(project_id=None)
    limiter = next(item for item in middlewares if item.__class__.__name__ == "ModelCallLimitMiddleware")

    assert limiter.run_limit == 7
    assert limiter.thread_limit is None
    assert limiter.exit_behavior == "end"


def test_runtime_inventory_lists_skills_for_system_prompt(tmp_path):
    """Skills inventory should expose the skill detail link and prompt-injection flag."""

    from graph.deepagents_manager import DeepAgentsAgentManager

    skill_dir = tmp_path / "skills" / "demo-skill"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\nname: demo-skill\ndescription: Demo skill\n---\n# Demo\n",
        encoding="utf-8",
    )

    manager = DeepAgentsAgentManager()
    manager.initialize(tmp_path)

    inventory = manager._runtime_inventory(tools=[], middleware=[], skills=["/skills/"])

    assert inventory["skills"] == [
        {
            "name": "demo-skill",
            "description": "Demo skill",
            "location": "skills/demo-skill/SKILL.md",
            "system_prompt_source": "/skills/",
            "in_system_prompt": True,
            "href": "/skills?skill=demo-skill",
        }
    ]
    assert inventory["checkpointer"] == {}


def test_runtime_inventory_lists_subagents_for_mount_panel(tmp_path, monkeypatch):
    """SubAgents inventory should expose default and configured delegates."""

    import graph.deepagents_manager as manager_module
    from graph.deepagents_manager import DeepAgentsAgentManager

    monkeypatch.setattr(
        manager_module.config,
        "get_settings_for_display",
        lambda: {
            "subagents": {
                "items": [
                    {
                        "enabled": True,
                        "name": "vision router",
                        "model": "qwen:qwen3.7",
                        "description": "Analyze uploaded images for the main agent.",
                        "route_trigger": "image_input",
                        "tools": {"mode": "inherit"},
                        "skills": {"mode": "custom", "paths": ["/skills/"]},
                    }
                ]
            }
        },
    )

    manager = DeepAgentsAgentManager()
    manager.initialize(tmp_path)

    inventory = manager._runtime_inventory(tools=[], middleware=[], skills=[])

    assert [item["name"] for item in inventory["subagents"]] == [
        "general-purpose",
        "vision router",
    ]
    vision_router = inventory["subagents"][1]
    assert vision_router["enabled"] is True
    assert vision_router["model"] == "qwen:qwen3.7"
    assert vision_router["route_trigger"] == "image_input"
    assert vision_router["tools_mode"] == "inherit"
    assert vision_router["skills_mode"] == "custom"
    assert vision_router["href"] == "/settings?category=harness&tab=subagent&subagent=vision%20router"
    assert "deepagents" in inventory["package_versions"]
    assert "langgraph" in inventory["package_versions"]


def test_build_checkpointer_is_available_for_interrupt_resume(tmp_path):
    """DeepAgents should always receive a checkpointer for interrupt/resume."""

    import asyncio

    from graph.deepagents_manager import DeepAgentsAgentManager

    manager = DeepAgentsAgentManager()
    manager.initialize(tmp_path)

    checkpointer = asyncio.run(manager._build_checkpointer())

    assert checkpointer is not None
    assert manager._checkpointer_info
    assert manager._checkpointer_info["type"] in {"async_sqlite", "memory"}
    if manager._checkpointer_info["type"] == "async_sqlite":
        assert manager._checkpointer_info["path"].endswith("data/checkpoints/deepagents.sqlite")


def test_permission_resume_helper_continues_after_decision(tmp_path):
    """Permission interrupts should resume the same graph stream after approval."""

    import asyncio

    from graph.deepagents_manager import DeepAgentsAgentManager
    from graph.permission_resume import permission_resume_registry
    from graph.trace_collector import TraceCollector
    from langgraph.types import Command, Interrupt

    request = {
        "id": "perm-req-test",
        "type": "external_file_read",
        "session_id": "resume-session",
        "query_id": "query-resume",
        "tool_call_id": "call-read",
        "path": "/tmp/example.md",
    }

    class FakeAgent:
        def __init__(self) -> None:
            self.inputs = []

        async def astream(self, graph_input, **_kwargs):
            self.inputs.append(graph_input)
            if len(self.inputs) == 1:
                yield {"__interrupt__": (Interrupt(value={"type": "permission_request", "request": request}, id="i1"),)}
                return
            yield ("messages", ("resumed", {"langgraph_node": "model"}))

    runtime = DeepAgentsAgentManager()
    runtime.initialize(tmp_path)
    agent = FakeAgent()

    async def run():
        with TraceCollector(session_id="resume-session", query_id="query-resume") as trace:
            events = []
            async for item in runtime._astream_with_permission_resume(
                agent,
                {"messages": []},
                stream_mode=["messages", "updates", "custom", "values"],
                config={"configurable": {"thread_id": "resume-session"}},
                context={"session_id": "resume-session", "query_id": "query-resume"},
                trace_collector=trace,
            ):
                events.append(item)
                if isinstance(item, dict) and item.get("event") == "permission_required":
                    permission_resume_registry._pending["perm-req-test"] = asyncio.get_running_loop().create_future()
                    permission_resume_registry.resolve("perm-req-test", {"type": "approve"})
            return events

    events = asyncio.run(run())

    assert [event.get("event") for event in events if isinstance(event, dict)] == [
        "permission_required",
        "permission_resolved",
    ]
    assert isinstance(agent.inputs[-1], Command)


def test_build_backend_resolves_workspace_and_skills(tmp_path, monkeypatch):
    """/workspace/ and /skills/ routes should resolve to the correct directories."""

    from graph import deepagents_manager as manager_module
    from projects.registry import project_registry

    project_registry.initialize(tmp_path)
    manager = manager_module.DeepAgentsAgentManager()
    manager.initialize(tmp_path)

    workspace = tmp_path / "workspaces" / "test"
    workspace.mkdir(parents=True)
    (workspace / "dashboard.html").write_text("dashboard")

    skills_dir = tmp_path / "skills"
    skills_dir.mkdir(parents=True)
    (skills_dir / "design-html").mkdir()
    (skills_dir / "design-html" / "SKILL.md").write_text("skill doc")

    knowledge_dir = tmp_path / "knowledge"
    knowledge_dir.mkdir(parents=True)
    (knowledge_dir / "test.md").write_text("kb doc")

    backend = manager._build_backend(workspace)

    assert backend.read("/workspace/dashboard.html").file_data["content"] == "dashboard"
    assert backend.read("/skills/design-html/SKILL.md").file_data["content"] == "skill doc"
    assert backend.read("/knowledge/test.md").file_data["content"] == "kb doc"
    # Bare root is an alias for workspace.
    assert backend.read("/dashboard.html").file_data["content"] == "dashboard"


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

    assert "tool_start" in event_names
    assert "tool_end" in event_names
    assert "segment_break" in event_names
    assert "token" in event_names
    assert "citations_finalized" in event_names
    assert "done" in event_names
    # Dynamic trace events should be emitted during the run.
    assert "trace_span_start" in event_names
    assert "trace_span_end" in event_names
    assert create_kwargs["skills"] == ["/skills/"]
    assert "memory" not in create_kwargs
    assert "middleware" in create_kwargs
    assert "checkpointer" in create_kwargs
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
    """When gstack/AGENTS.md exists, one MemoryMiddleware loads both sources."""

    from graph.deepagents_manager import DeepAgentsAgentManager
    from graph.permission_middleware import ExternalFilePermissionMiddleware

    # Simulate backend layout with bundled gstack index
    backend_dir = tmp_path
    gstack_dir = backend_dir / "gstack"
    gstack_dir.mkdir(parents=True)
    (gstack_dir / "AGENTS.md").write_text("# GStack Skills\n", encoding="utf-8")

    runtime = DeepAgentsAgentManager()
    runtime.initialize(backend_dir)

    middlewares = runtime._build_middlewares("proj_abc123")  # noqa: SLF001
    memory_middlewares = [mw for mw in middlewares if isinstance(mw, MemoryMiddleware)]
    assert any(isinstance(mw, ExternalFilePermissionMiddleware) for mw in middlewares)
    assert len(memory_middlewares) == 1
    mw = memory_middlewares[0]
    assert isinstance(mw, MemoryMiddleware)
    assert "/AGENTS.md" in mw.sources
    assert "/gstack/AGENTS.md" in mw.sources


def test_deepagents_manager_emits_graph_structure(tmp_path, monkeypatch):
    """Agent mode should emit the LangGraph structure at the start of the run."""

    import asyncio

    from graph import deepagents_manager as manager_module
    from graph.session_manager import session_manager
    from langchain_core.messages import AIMessage, AIMessageChunk
    from projects.registry import project_registry

    session_manager.initialize(tmp_path)
    project_registry.initialize(tmp_path)
    session_manager.create_session("agent-graph-session")

    class FakeGraph:
        nodes = [("__start__", None), ("model", None), ("tools", None)]
        edges = [("__start__", "model"), ("model", "tools"), ("tools", "model")]

    class FakeDeepAgent:
        def get_graph(self):
            return FakeGraph()

        async def astream(self, *_args, **_kwargs):
            yield (
                "messages",
                (AIMessageChunk(content="hello"), {"langgraph_node": "model"}),
            )
            yield ("values", {"messages": [AIMessage(content="hello")]})

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
                message="hi",
                session_id="agent-graph-session",
                project_id=None,
                user_id="test-user",
            )
        ]

    events = asyncio.run(collect())
    graph_event = next((e for e in events if e["event"] == "graph_structure"), None)
    assert graph_event is not None
    structure = json.loads(graph_event["data"])
    assert {n["id"] for n in structure["nodes"]} == {"__start__", "model", "tools"}
    assert any(e["source"] == "__start__" and e["target"] == "model" for e in structure["edges"])
