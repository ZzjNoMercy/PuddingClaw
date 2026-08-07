"""Tests for the local Agent trace collector."""

import asyncio
import json
from datetime import datetime

from langchain.agents.middleware import AgentMiddleware, hook_config
from langchain.agents.middleware.types import ModelRequest, ModelResponse
from langchain_core.messages import AIMessage

from graph.middleware_trace_proxy import wrap_middleware_for_trace
from graph.trace_collector import TraceCollector


def test_trace_collector_builds_root_span():
    collector = TraceCollector(session_id="session-1", query_id="query-1")
    trace = collector.finish(status="completed")

    assert trace["session_id"] == "session-1"
    assert trace["query_id"] == "query-1"
    assert trace["status"] == "completed"
    assert len(trace["spans"]) == 1
    root = trace["spans"][0]
    assert root["type"] == "root"
    assert root["parent_id"] is None
    assert root["metadata"]["query_id"] == "query-1"


def test_trace_finish_and_snapshot_are_json_safe_for_nested_datetimes():
    value = datetime(2026, 7, 27, 9, 38, 0)
    emitted: list[tuple[str, dict]] = []
    collector = TraceCollector(
        session_id="session-datetime",
        runtime_inventory={"analytics_model": {"frontmatter": {"created": value}}},
        emit_callback=lambda event, payload: emitted.append((event, payload)),
    )
    collector.add_custom_span(
        "semantic-assets",
        {"rows": [{"updated_at": value}]},
        metadata={"sample": {"created": value}},
    )

    snapshot = collector.snapshot()
    trace = collector.finish(status="completed")

    json.dumps(snapshot)
    json.dumps(trace)
    assert snapshot["runtime_inventory"]["analytics_model"]["frontmatter"]["created"] == value.isoformat()
    span = next(item for item in trace["spans"] if item["name"] == "semantic-assets")
    assert span["output"]["rows"][0]["updated_at"] == value.isoformat()
    assert any(event == "trace_span_end" for event, _payload in emitted)
    for _event, payload in emitted:
        json.dumps(payload)


def test_trace_collector_adds_llm_and_tool_spans():
    collector = TraceCollector(session_id="session-2")
    collector.start_llm_span("model", input_data="hello")
    collector.start_tool_span("read_file", tool_call_id="call-1", input_data={"path": "/x"})
    collector.finish_tool_span("call-1", output="content", is_error=False)
    collector.finish_llm_span(output="done")

    trace = collector.finish(status="completed")
    spans = trace["spans"]
    assert len(spans) == 3  # root + llm + tool

    types = [s["type"] for s in spans]
    assert "root" in types
    assert "llm" in types
    assert "tool" in types

    tool_span = next(s for s in spans if s["type"] == "tool")
    assert tool_span["name"] == "read_file"
    assert tool_span["status"] == "completed"
    assert tool_span["parent_id"] is not None
    assert [s["metadata"]["event_order"] for s in spans] == [0, 1, 2]


def test_trace_collector_marks_error_tool():
    collector = TraceCollector(session_id="session-3")
    collector.start_tool_span("terminal", tool_call_id="call-err", input_data="bad")
    collector.finish_tool_span("call-err", output="error", is_error=True)

    trace = collector.finish(status="completed")
    tool_span = next(s for s in trace["spans"] if s["type"] == "tool")
    assert tool_span["status"] == "error"


def test_trace_collector_adds_todo_span():
    collector = TraceCollector(session_id="session-4")
    todos = [{"id": "todo-1", "content": "step", "status": "completed"}]
    diff = {"added": todos, "updated": [], "removed": []}
    collector.add_todo_span(todos, diff=diff)

    trace = collector.finish(status="completed")
    todo_span = next(s for s in trace["spans"] if s["type"] == "todo")
    assert todo_span["name"] == "todos_updated"
    assert todo_span["output"] == todos
    assert todo_span["metadata"]["todo_diff"] == diff
    effect = next(item for item in trace["middleware_effects"] if item["category"] == "state")
    assert effect["title"] == "Todo state updated"
    assert effect["diff"] == {"added": 1, "updated": 0, "removed": 0}
    assert effect["after"]["todo_count"] == 1


def test_trace_collector_adds_rag_span_under_tool():
    collector = TraceCollector(session_id="session-rag")
    tool_id = collector.start_tool_span(
        "llamaindex_knowledge_query", tool_call_id="call-rag", input_data={"query": "AI"}
    )
    rag_id = collector.add_rag_span(
        "retrieve.text_vector",
        {
            "candidate_count": 2,
            "top_candidates": [{"title": "Doc", "source_id": "src_1"}],
        },
        metadata={"rag_channel": "text_vector"},
    )
    collector.finish_tool_span("call-rag", output="encoded result", is_error=False)

    trace = collector.finish(status="completed")
    spans = {span["id"]: span for span in trace["spans"]}
    rag_span = spans[rag_id]
    assert rag_span["type"] == "rag"
    assert rag_span["name"] == "rag.retrieve.text_vector"
    assert rag_span["parent_id"] == tool_id
    assert rag_span["metadata"]["rag_stage"] == "retrieve.text_vector"
    assert rag_span["metadata"]["rag_channel"] == "text_vector"
    assert rag_span["output"]["candidate_count"] == 2


def test_trace_collector_adds_model_input_span():
    collector = TraceCollector(session_id="session-model-input", query_id="query-model")
    collector.add_model_input_span(
        messages=[{"role": "user", "content": "hello model"}],
        tool_schema_count=3,
        tool_schemas=[{"type": "function", "function": {"name": "probe", "parameters": {"type": "object"}}}],
        model_params={"model": "probe-model", "temperature": 0.2},
        capture_boundary="test.boundary",
    )

    trace = collector.finish(status="completed")
    model_input = next(s for s in trace["spans"] if s["type"] == "model_input")
    assert model_input["name"] == "model.input"
    assert model_input["metadata"]["message_count"] == 1
    assert model_input["metadata"]["tool_schema_count"] == 3
    assert model_input["metadata"]["estimated_tokens"] > model_input["metadata"]["message_estimated_tokens"]
    assert model_input["metadata"]["tool_schema_estimated_tokens"] > 0
    assert model_input["metadata"]["token_estimator"] == "langchain_count_tokens_approximately"
    assert model_input["metadata"]["capture_boundary"] == "test.boundary"
    assert model_input["metadata"]["fingerprints"]["messages_hash"]
    assert model_input["metadata"]["fingerprints"]["tool_schema_hash"]
    assert model_input["metadata"]["event_order"] == 1
    assert model_input["output"]["messages_preview"][0]["role"] == "user"
    contract = model_input["output"]["model_call_contract"]
    assert contract["params"]["model"] == "probe-model"
    assert contract["estimated_tokens"] == model_input["metadata"]["estimated_tokens"]
    assert contract["tool_schema_estimated_tokens"] > 0
    assert contract["tool_schemas"][0]["name"] == "probe"
    assert contract["fingerprints"] == model_input["metadata"]["fingerprints"]
    effect = next(item for item in trace["middleware_effects"] if item["category"] == "model_input")
    assert effect["after"]["message_count"] == 1
    assert effect["after"]["tool_schema_count"] == 3
    assert effect["diff"]["initial"] is True
    assert effect["metadata"]["capture_boundary"] == "test.boundary"
    assert not any(
        item["title"] == "Model input boundary" and item["hook"] == "before_model"
        for item in trace["middleware_invocations"]
    )
    snapshots = trace["hook_boundary_snapshots"]
    assert [item["title"] for item in snapshots] == ["before_model.after", "wrap_model_call.before"]
    assert snapshots[0]["snapshot"]["fingerprints"]["messages_hash"]
    assert snapshots[1]["metadata"]["source_span_id"] == model_input["id"]


def test_trace_collector_model_call_contract_hashes_change_with_prompt_and_tools():
    collector = TraceCollector(session_id="session-contract", query_id="query-contract")
    first_id = collector.add_model_input_span(
        messages=[
            {"role": "system", "content": "You are concise."},
            {"role": "user", "content": "hello"},
        ],
        tool_schema_count=1,
        tool_schemas=[{"type": "function", "function": {"name": "search", "parameters": {"type": "object"}}}],
        capture_boundary="test.boundary",
    )
    second_id = collector.add_model_input_span(
        messages=[
            {"role": "system", "content": "You are concise and cite sources."},
            {"role": "user", "content": "hello"},
        ],
        tool_schema_count=1,
        tool_schemas=[{"type": "function", "function": {"name": "fetch", "parameters": {"type": "object"}}}],
        capture_boundary="test.boundary",
    )

    trace = collector.finish(status="completed")
    spans = {span["id"]: span for span in trace["spans"]}
    first = spans[first_id]["output"]["model_call_contract"]["fingerprints"]
    second = spans[second_id]["output"]["model_call_contract"]["fingerprints"]

    assert first["system_prompt_hash"] != second["system_prompt_hash"]
    assert first["messages_hash"] != second["messages_hash"]
    assert first["tool_schema_hash"] != second["tool_schema_hash"]


def test_trace_collector_records_memory_middleware_before_agent():
    collector = TraceCollector(
        session_id="session-memory",
        runtime_inventory={
            "middleware": {
                "stack": [
                    {
                        "name": "MemoryMiddleware",
                        "hooks": ["before_agent", "wrap_model_call"],
                    }
                ]
            }
        },
    )
    collector.add_model_input_span(
        messages=[
            {
                "role": "system",
                "content": (
                    "System prompt\n"
                    "<agent_memory>\n"
                    "/MEMORY.md\n"
                    "# Project Memory\n\n"
                    "/gstack/AGENTS.md\n"
                    "# gstack\n"
                    "</agent_memory>"
                ),
            },
            {"role": "user", "content": "hello"},
        ],
        capture_boundary="test.boundary",
    )

    trace = collector.finish(status="completed")
    effects = [item for item in trace["middleware_effects"] if item["category"] == "memory"]
    assert [item["hook"] for item in effects] == ["before_agent", "wrap_model_call"]
    assert all(item["middleware"] == ["MemoryMiddleware"] for item in effects)
    assert effects[0]["after"]["agent_memory_present"] is True
    assert effects[0]["after"]["matched_sources"] == ["/MEMORY.md", "/gstack/AGENTS.md"]
    assert effects[1]["title"] == "Memory injected into system prompt"
    assert effects[1]["after"]["agent_memory_present"] is True
    before_agent_invocation = next(
        item
        for item in trace["middleware_invocations"]
        if item["hook"] == "before_agent" and item["category"] == "memory"
    )
    assert before_agent_invocation["title"] == "Memory loaded into agent state"
    assert before_agent_invocation["middleware"] == ["MemoryMiddleware"]
    assert "agent_memory block found in final system prompt" in before_agent_invocation["evidence"]
    wrap_invocation = next(
        item
        for item in trace["middleware_invocations"]
        if item["hook"] == "wrap_model_call" and item["category"] == "memory"
    )
    assert wrap_invocation["title"] == "Memory injected into system prompt"
    assert wrap_invocation["middleware"] == ["MemoryMiddleware"]


def test_trace_collector_records_subagent_middleware_prompt_injection():
    collector = TraceCollector(
        session_id="session-subagent",
        runtime_inventory={
            "subagents": [
                {
                    "name": "general-purpose",
                    "source": "deepagents.default",
                    "route_trigger": "complex independent task",
                },
                {
                    "name": "image_analyzer",
                    "source": "config",
                    "route_trigger": "image_input",
                },
            ],
            "middleware": {
                "stack": [
                    {
                        "name": "SubAgentMiddleware",
                        "order": 4,
                        "hooks": ["wrap_model_call"],
                    }
                ]
            },
        },
    )
    collector.add_model_input_span(
        messages=[
            {
                "role": "system",
                "content": (
                    "System prompt\n\n"
                    "## `task` (subagent spawner)\n"
                    "Available subagent types:\n"
                    "- general-purpose: default helper\n"
                    "- image_analyzer: Analyze images. Use this subagent when the main request matches this routing hint: `image_input`."
                ),
            }
        ],
        capture_boundary="test.boundary",
    )

    trace = collector.finish(status="completed")
    effect = next(item for item in trace["middleware_effects"] if item["category"] == "subagent")
    assert effect["hook"] == "wrap_model_call"
    assert effect["middleware"] == ["SubAgentMiddleware"]
    assert effect["after"]["native_marker_present"] is True
    assert [item["name"] for item in effect["after"]["matched_subagents"]] == ["general-purpose", "image_analyzer"]
    invocation = next(
        item
        for item in trace["middleware_invocations"]
        if item["hook"] == "wrap_model_call" and item["category"] == "subagent"
    )
    assert invocation["title"] == "SubAgents exposed to main agent"
    assert invocation["middleware"] == ["SubAgentMiddleware"]


def test_trace_collector_orders_before_agent_invocations_by_middleware_stack():
    collector = TraceCollector(
        session_id="session-before-agent-order",
        runtime_inventory={
            "skills": [
                {
                    "name": "aihot",
                    "location": "skills/aihot/SKILL.md",
                    "description": "AI HOT",
                }
            ],
            "middleware": {
                "stack": [
                    {
                        "name": "TodoListMiddleware",
                        "order": 1,
                        "hooks": ["before_agent", "after_model"],
                    },
                    {
                        "name": "SkillsMiddleware",
                        "order": 2,
                        "hooks": ["before_agent"],
                    },
                    {
                        "name": "MemoryMiddleware",
                        "order": 7,
                        "hooks": ["before_agent", "wrap_model_call"],
                    },
                ]
            },
        },
    )
    collector.add_model_input_span(
        messages=[
            {
                "role": "system",
                "content": ("skills/aihot/SKILL.md\n<agent_memory>\n/MEMORY.md\n# Project Memory\n</agent_memory>"),
            }
        ],
        capture_boundary="test.boundary",
    )

    trace = collector.finish(status="completed")
    before_agent = [item for item in trace["middleware_invocations"] if item["hook"] == "before_agent"]
    assert [item["category"] for item in before_agent] == ["skills", "memory"]
    assert before_agent[0]["middleware"] == ["SkillsMiddleware"]
    assert before_agent[1]["middleware"] == ["MemoryMiddleware"]
    assert before_agent[0]["sequence"] < before_agent[1]["sequence"]
    assert before_agent[0]["title"] == "Skills metadata loaded into state"
    assert before_agent[0]["after"]["skills_in_state"] == 1
    assert before_agent[0]["after"]["skills_metadata"][0]["name"] == "aihot"
    assert "matched_in_system_prompt" not in before_agent[0]["after"]


def test_trace_collector_model_input_unwraps_stringified_content_parts():
    collector = TraceCollector(session_id="session-model-input-parts")
    collector.add_model_input_span(
        messages=[
            {
                "role": "system",
                "content": "{'type': 'text', 'text': 'system prompt text'}",
            }
        ],
        capture_boundary="test.boundary",
    )

    trace = collector.finish(status="completed")
    model_input = next(s for s in trace["spans"] if s["type"] == "model_input")
    message = model_input["output"]["messages_preview"][0]
    assert message["content"] == "system prompt text"
    assert message["preview"] == "system prompt text"


def test_trace_collector_model_input_summarizes_image_parts():
    preview = TraceCollector._message_preview(
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "看图"},
                {
                    "type": "image_url",
                    "image_url": {"url": "data:image/jpeg;base64," + ("A" * 10000)},
                },
            ],
        }
    )

    assert preview["content"] == "看图\n[image_url image/jpeg base64_chars=10000]"
    assert preview["chars"] < 80
    assert preview["estimated_tokens"] < 20
    assert "AAAA" not in preview["preview"]


def test_trace_collector_model_input_records_tool_call_args():
    collector = TraceCollector(session_id="session-model-input-tool-calls")
    collector.add_model_input_span(
        messages=[
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": "call-1",
                        "name": "read_file",
                        "args": {"path": "/workspace/README.md"},
                    }
                ],
            }
        ],
        capture_boundary="test.boundary",
    )

    trace = collector.finish(status="completed")
    model_input = next(s for s in trace["spans"] if s["type"] == "model_input")
    message = model_input["output"]["messages_preview"][0]
    assert message["tool_call_count"] == 1
    assert message["tool_calls"] == [
        {
            "id": "call-1",
            "name": "read_file",
            "args": {"path": "/workspace/README.md"},
        }
    ]


def test_trace_collector_exposes_hidden_provider_tool_calls_and_tool_response_id():
    from langchain_core.messages import AIMessage, ToolMessage

    collector = TraceCollector(session_id="session-hidden-tool-call")
    collector.add_model_input_span(
        messages=[
            AIMessage(
                content="",
                invalid_tool_calls=[
                    {
                        "id": "call-invalid",
                        "name": "patch_file",
                        "args": "{",
                        "error": "bad json",
                        "type": "invalid_tool_call",
                    }
                ],
            ),
            ToolMessage(
                content="could not execute",
                tool_call_id="call-invalid",
                name="patch_file",
                status="error",
            ),
        ],
        capture_boundary="test.boundary",
    )

    trace = collector.finish(status="completed")
    model_input = next(s for s in trace["spans"] if s["type"] == "model_input")
    messages = model_input["output"]["messages_preview"]

    assert messages[0]["tool_call_count"] == 0
    assert messages[0]["invalid_tool_call_count"] == 1
    assert messages[0]["provider_tool_call_count"] == 1
    assert messages[0]["provider_tool_call_ids"] == ["call-invalid"]
    assert messages[1]["tool_call_id"] == "call-invalid"
    message_section = next(
        item
        for item in model_input["output"]["model_call_contract"]["assembly"]["sections"]
        if item["key"] == "messages"
    )
    assert message_section["provider_tool_call_count"] == 1


def test_trace_collector_adds_custom_span():
    collector = TraceCollector(session_id="session-5")
    collector.add_custom_span("context_maintenance", {"phase": "summarize"})

    trace = collector.finish(status="completed")
    custom_span = next(s for s in trace["spans"] if s["type"] == "custom")
    assert custom_span["name"] == "context_maintenance"
    assert custom_span["output"] == {"phase": "summarize"}


def test_trace_collector_emits_middleware_invocation_events():
    events: list[tuple[str, dict]] = []

    def emit(event: str, payload: dict) -> None:
        events.append((event, payload))

    collector = TraceCollector(session_id="session-mw-invocation", emit_callback=emit)
    invocation = collector.add_middleware_invocation(
        hook="wrap_model_call",
        middleware=["MemoryMiddleware"],
        category="model_input",
        title="Memory injected",
        status="changed",
        evidence=["system prompt +12 chars"],
        diff={"system_prompt_chars_delta": 12},
    )
    trace = collector.finish(status="completed")

    assert trace["middleware_invocations"][0]["id"] == invocation["id"]
    assert trace["middleware_invocations"][0]["hook"] == "wrap_model_call"
    emitted = next(event for event in events if event[0] == "middleware_invocation")
    assert emitted[1]["invocation"]["id"] == invocation["id"]
    assert emitted[1]["trace_id"] == trace["trace_id"]


def test_trace_collector_emits_hook_boundary_snapshot_events():
    events: list[tuple[str, dict]] = []

    def emit(event: str, payload: dict) -> None:
        events.append((event, payload))

    collector = TraceCollector(session_id="session-boundary", emit_callback=emit)
    collector.add_model_input_span(
        messages=[{"role": "system", "content": "You are precise."}],
        tool_schema_count=0,
        capture_boundary="test.boundary",
    )
    trace = collector.finish(status="completed")

    emitted = [event for event in events if event[0] == "hook_boundary_snapshot"]
    assert [event[1]["snapshot"]["title"] for event in emitted] == [
        "before_model.after",
        "wrap_model_call.before",
    ]
    assert emitted[0][1]["trace_id"] == trace["trace_id"]


def test_middleware_trace_proxy_records_direct_state_hook_attribution():
    class DemoBeforeModelMiddleware(AgentMiddleware):
        def before_model(self, state, runtime):
            return {
                "messages": [
                    *state.get("messages", []),
                    {"role": "system", "content": "Injected by demo middleware."},
                ]
            }

    proxied = wrap_middleware_for_trace(DemoBeforeModelMiddleware())

    assert proxied.__class__.before_model is not AgentMiddleware.before_model
    assert proxied.__class__.after_model is AgentMiddleware.after_model

    collector = TraceCollector(session_id="session-proxy", query_id="query-proxy")
    with collector:
        result = proxied.before_model({"messages": [{"role": "user", "content": "hello"}]}, None)

    assert result["messages"][-1]["role"] == "system"
    trace = collector.finish(status="completed")
    invocation = next(
        item for item in trace["middleware_invocations"] if item["title"] == "DemoBeforeModelMiddleware.before_model"
    )
    assert invocation["metadata"]["attribution"] == "middleware_proxy"
    assert invocation["metadata"]["coverage"] == "direct"
    assert invocation["metadata"]["model_call_index"] == 0
    assert invocation["status"] == "changed"
    assert invocation["diff"]["message_count_delta"] == 1
    assert invocation["diff"]["state_fields_changed"] == ["messages"]
    assert invocation["before"]["state_fields"]["messages"]["count"] == 1
    assert invocation["after"]["state_fields"]["messages"]["count"] == 2
    snapshots = [
        item
        for item in trace["hook_boundary_snapshots"]
        if item["metadata"].get("middleware_invocation_id") == invocation["id"]
    ]
    assert [item["title"] for item in snapshots] == [
        "DemoBeforeModelMiddleware.before_model.before",
        "DemoBeforeModelMiddleware.before_model.after",
    ]


def test_middleware_trace_proxy_preserves_jump_edge_metadata():
    class DemoCompletionGate(AgentMiddleware):
        @hook_config(can_jump_to=["model"])
        def after_agent(self, state, runtime):
            return {"jump_to": "model"}

    proxied = wrap_middleware_for_trace(DemoCompletionGate())

    assert proxied.after_agent({}, None) == {"jump_to": "model"}
    assert proxied.__class__.after_agent.__can_jump_to__ == ["model"]


def test_middleware_trace_proxy_records_model_call_index_per_before_model_call():
    class DemoBeforeModelMiddleware(AgentMiddleware):
        def before_model(self, state, runtime):
            return None

    proxied = wrap_middleware_for_trace(DemoBeforeModelMiddleware())
    collector = TraceCollector(session_id="session-proxy-index", query_id="query-proxy-index")
    with collector:
        proxied.before_model({"messages": [{"role": "user", "content": "first"}]}, None)
        collector.add_model_input_span(
            messages=[{"role": "user", "content": "first"}],
            capture_boundary="test.first",
        )
        proxied.before_model({"messages": [{"role": "user", "content": "second"}]}, None)
        collector.add_model_input_span(
            messages=[{"role": "user", "content": "second"}],
            capture_boundary="test.second",
        )

    trace = collector.finish(status="completed")
    invocations = [
        item for item in trace["middleware_invocations"] if item["title"] == "DemoBeforeModelMiddleware.before_model"
    ]
    assert [item["metadata"]["model_call_index"] for item in invocations] == [0, 1]


def test_middleware_trace_proxy_preserves_extra_state_hook_args():
    class DemoAsyncBeforeAgentMiddleware(AgentMiddleware):
        async def abefore_agent(self, state, runtime, config):
            assert config["configurable"]["thread_id"] == "session-extra"
            return {"messages": [*state.get("messages", []), {"role": "user", "content": "extra"}]}

    proxied = wrap_middleware_for_trace(DemoAsyncBeforeAgentMiddleware())
    collector = TraceCollector(session_id="session-extra", query_id="query-extra")

    async def run_hook():
        with collector:
            return await proxied.abefore_agent(
                {"messages": [{"role": "user", "content": "hello"}]},
                None,
                {"configurable": {"thread_id": "session-extra"}},
            )

    result = asyncio.run(run_hook())
    assert result["messages"][-1]["content"] == "extra"
    trace = collector.finish(status="completed")
    invocation = next(
        item
        for item in trace["middleware_invocations"]
        if item["title"] == "DemoAsyncBeforeAgentMiddleware.before_agent"
    )
    assert invocation["hook"] == "before_agent"
    assert invocation["diff"]["message_count_delta"] == 1
    assert invocation["diff"]["state_fields_changed"] == ["messages"]


def test_trace_collector_state_summary_tracks_field_level_changes():
    before = TraceCollector.summarize_hook_payload(
        {
            "messages": [{"role": "user", "content": "hello"}],
            "skills_metadata": [],
            "todos": [{"content": "plan", "status": "pending"}],
        }
    )
    after = TraceCollector.summarize_hook_payload(
        {
            "messages": [{"role": "user", "content": "hello"}],
            "skills_metadata": [{"name": "aihot", "location": "skills/aihot/SKILL.md", "description": "AI HOT"}],
            "todos": [
                {"content": "plan", "status": "in_progress"},
                {"content": "ship", "status": "pending"},
            ],
            "files": {"README.md": "content"},
        }
    )

    assert before["state_fields"]["skills_metadata"]["count"] == 0
    assert after["state_fields"]["skills_metadata"]["count"] == 1
    assert after["state_fields"]["skills_metadata"]["names"] == ["aihot"]
    assert after["state_fields"]["todos"]["status_counts"] == {"in_progress": 1, "pending": 1}
    diff = TraceCollector.hook_summary_diff(before, after)
    assert diff["state_keys_added"] == ["files"]
    assert diff["state_fields_changed"] == ["skills_metadata", "todos"]
    assert diff["state_skills_metadata_count_delta"] == 1
    assert diff["state_todos_count_delta"] == 1


def test_middleware_trace_proxy_records_wrap_model_call_attribution():
    class DemoWrapModelMiddleware(AgentMiddleware):
        def wrap_model_call(self, request, handler):
            next_request = ModelRequest(
                model=request.model,
                messages=[*request.messages, {"role": "user", "content": "extra request"}],
                system_message=request.system_message,
                tools=request.tools,
                state=request.state,
                runtime=request.runtime,
            )
            return handler(next_request)

    proxied = wrap_middleware_for_trace(DemoWrapModelMiddleware())
    request = ModelRequest(
        model=object(),
        messages=[{"role": "user", "content": "hello"}],
        tools=[{"type": "function", "function": {"name": "probe", "parameters": {"type": "object"}}}],
    )

    def handler(_request):
        return ModelResponse(result=[AIMessage(content="hi")])

    collector = TraceCollector(session_id="session-wrap-proxy", query_id="query-wrap-proxy")
    with collector:
        response = proxied.wrap_model_call(request, handler)

    assert response.result[0].content == "hi"
    trace = collector.finish(status="completed")
    invocation = next(
        item for item in trace["middleware_invocations"] if item["title"] == "DemoWrapModelMiddleware.wrap_model_call"
    )
    assert invocation["hook"] == "wrap_model_call"
    assert invocation["metadata"]["coverage"] == "direct"
    assert invocation["metadata"]["wrap_semantics"] == "request_diff"
    assert invocation["before"]["payload_kind"] == "model_request"
    assert invocation["after"]["request_sent"]["payload_kind"] == "model_request"
    assert invocation["after"]["response_observed"]["payload_kind"] == "model_response"
    assert invocation["diff"]["message_count_delta"] == 1
    snapshots = [
        item
        for item in trace["hook_boundary_snapshots"]
        if item["metadata"].get("middleware_invocation_id") == invocation["id"]
    ]
    assert [item["phase"] for item in snapshots] == ["before", "request", "response"]
    assert [item["title"] for item in snapshots] == [
        "DemoWrapModelMiddleware.wrap_model_call.request_before_wrapper",
        "DemoWrapModelMiddleware.wrap_model_call.request_sent_to_handler",
        "DemoWrapModelMiddleware.wrap_model_call.response_observed",
    ]


def test_model_request_trace_handles_pydantic_tool_schema_class():
    """Tracing an activated database tool must never abort the model call."""

    from tools.database.sql_generate_tool import DatabaseSqlGenerateTool

    request = ModelRequest(
        model=object(),
        messages=[{"role": "user", "content": "continue"}],
        tools=[DatabaseSqlGenerateTool()],
    )

    summary = TraceCollector.summarize_hook_payload(request)

    assert summary["payload_kind"] == "model_request"
    assert summary["tool_schema_count"] == 1
    assert summary["tool_schema_hash"]


def test_trace_collector_records_langgraph_node_as_graph_span():
    collector = TraceCollector(session_id="session-graph-node")
    collector.add_graph_node_span("tools")

    trace = collector.finish(status="completed")
    graph_span = next(s for s in trace["spans"] if s["name"] == "graph.tools")
    assert graph_span["type"] == "graph"
    assert graph_span["metadata"]["graph_node"] == "tools"
    assert graph_span["metadata"]["graph_node_kind"] == "tool"


def test_trace_collector_error_finish():
    collector = TraceCollector(session_id="session-6")
    trace = collector.finish(status="error", error="boom")

    assert trace["status"] == "error"
    root = trace["spans"][0]
    assert root["status"] == "error"
    assert root["metadata"]["error"] == "boom"


def test_trace_collector_emits_start_and_end_events():
    events: list[tuple[str, dict]] = []

    def emit(event: str, payload: dict) -> None:
        events.append((event, payload))

    collector = TraceCollector(session_id="session-emit", emit_callback=emit)
    collector.start_llm_span("model", input_data="hello")
    collector.finish_llm_span(output="done")
    trace = collector.finish(status="completed")

    event_names = [e[0] for e in events]
    assert "trace_span_start" in event_names
    assert "trace_span_end" in event_names

    # Root start + LLM start + LLM end + root end.
    assert event_names.count("trace_span_start") >= 2
    assert event_names.count("trace_span_end") >= 2

    start_spans = [e[1]["span"] for e in events if e[0] == "trace_span_start"]
    assert any(s["type"] == "root" for s in start_spans)
    assert any(s["type"] == "llm" for s in start_spans)
    assert all(payload["trace_id"] == trace["trace_id"] for _, payload in events)
    assert all(payload["query_id"] == trace["query_id"] for _, payload in events)


def test_trace_collector_emits_tool_span_events():
    events: list[tuple[str, dict]] = []

    def emit(event: str, payload: dict) -> None:
        events.append((event, payload))

    collector = TraceCollector(session_id="session-tool-emit", emit_callback=emit)
    collector.start_tool_span("read_file", tool_call_id="call-1", input_data="/x")
    collector.finish_tool_span("call-1", output="content", is_error=False)
    collector.finish(status="completed")

    start_events = [e for e in events if e[0] == "trace_span_start"]
    tool_start = next((e for e in start_events if e[1]["span"]["type"] == "tool"), None)
    assert tool_start is not None
    assert tool_start[1]["span"]["status"] == "running"

    end_events = [e for e in events if e[0] == "trace_span_end"]
    tool_end = next((e for e in end_events if e[1]["span"]["type"] == "tool"), None)
    assert tool_end is not None
    assert tool_end[1]["span"]["status"] == "completed"


def test_trace_collector_snapshot_keeps_running_spans_before_finish():
    collector = TraceCollector(session_id="session-running-snapshot", query_id="query-1")

    collector.start_tool_span("database_knowledge_query", tool_call_id="call-1", input_data="{}")
    snapshot = collector.snapshot()

    assert snapshot["status"] == "running"
    tool_span = next(span for span in snapshot["spans"] if span["name"] == "database_knowledge_query")
    assert tool_span["status"] == "running"
    assert snapshot["completed_at"] is None
