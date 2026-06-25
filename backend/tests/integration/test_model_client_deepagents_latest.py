"""ModelClient × latest DeepAgents compatibility tests.

These tests intentionally target DeepAgents' latest public API rather than the
older notebook-pinned version. They are skipped in the normal project
environment unless `deepagents` is installed, so the production dependency lock
does not need to include DeepAgents yet.
"""

from __future__ import annotations

from typing import Any
from unittest import mock

import pytest
from langchain_core.runnables import RunnableLambda
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.tools import tool
from pydantic import BaseModel, Field, PrivateAttr

deepagents = pytest.importorskip("deepagents")

from deepagents import CompiledSubAgent, SubAgent, create_deep_agent  # noqa: E402
from deepagents.backends import CompositeBackend, FilesystemBackend, StateBackend  # noqa: E402
from langgraph.checkpoint.memory import MemorySaver  # noqa: E402
from langgraph.types import Command  # noqa: E402

from llm import model_client as mc  # noqa: E402


class ScriptedDeepAgentsModel(BaseChatModel):
    """A deterministic tool-calling model for DeepAgents contract tests."""

    _responses: list[AIMessage] = PrivateAttr()
    _calls: int = PrivateAttr(default=0)
    _bound_tool_names: list[list[str | None]] = PrivateAttr(default_factory=list)
    _bound_kwargs: list[dict[str, Any]] = PrivateAttr(default_factory=list)
    _message_snapshots: list[list[str]] = PrivateAttr(default_factory=list)

    def __init__(self, responses: list[AIMessage]) -> None:
        super().__init__()
        self._responses = responses

    @property
    def _llm_type(self) -> str:
        return "scripted_deepagents_model"

    def bind_tools(self, tools: list[Any], **kwargs: Any) -> "ScriptedDeepAgentsModel":
        self._bound_tool_names.append([_tool_name(tool_obj) for tool_obj in tools])
        self._bound_kwargs.append(dict(kwargs))
        return self

    def _generate(
        self,
        messages: list[Any],
        stop: list[str] | None = None,
        run_manager: Any = None,
        **kwargs: Any,
    ) -> ChatResult:
        self._calls += 1
        self._message_snapshots.append([type(message).__name__ for message in messages])
        try:
            message = self._responses[self._calls - 1]
        except IndexError:
            message = AIMessage(content=f"UNSCRIPTED_CALL_{self._calls}")
        return ChatResult(generations=[ChatGeneration(message=message)])


class ProbeStructuredAnswer(BaseModel):
    """Structured response schema used by DeepAgents latest tests."""

    answer: str = Field(description="short answer")
    score: int = Field(description="confidence score")


def _tool_name(tool_obj: Any) -> str | None:
    if isinstance(tool_obj, dict):
        return tool_obj.get("name") or tool_obj.get("function", {}).get("name")
    return getattr(tool_obj, "name", None)


def _use_fake_direct_model(fake: ScriptedDeepAgentsModel):
    return mock.patch.object(mc.ModelClient, "_direct_model", return_value=fake)


@tool
def pudding_probe(text: str) -> str:
    """Return a deterministic marker for integration tests."""

    return "TOOL_MARKER:" + text


def test_latest_deepagents_middleware_tool_loop_works_through_model_client():
    """P0: middleware-provided tools and custom tools survive ModelClient binding."""

    fake = ScriptedDeepAgentsModel(
        responses=[
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "pudding_probe",
                        "args": {"text": "middleware"},
                        "id": "call_probe",
                    }
                ],
            ),
            AIMessage(content="TOOL_MARKER:middleware"),
        ]
    )

    with _use_fake_direct_model(fake):
        model = mc.ModelClientChatModel(force_direct=True, streaming=False)
        agent = create_deep_agent(
            model=model,
            tools=[pudding_probe],
            system_prompt="Call pudding_probe once and then return its result.",
        )
        result = agent.invoke({"messages": [{"role": "user", "content": "run middleware probe"}]})

    messages = result["messages"]
    assert messages[-1].content == "TOOL_MARKER:middleware"
    assert any(getattr(message, "content", None) == "TOOL_MARKER:middleware" for message in messages)
    assert fake._calls == 2
    assert any("pudding_probe" in names for names in fake._bound_tool_names)
    assert any("write_todos" in names for names in fake._bound_tool_names)


def test_latest_deepagents_general_purpose_subagent_inherits_model_client():
    """P0: the `task` subagent path can call back through the same ModelClient wrapper."""

    fake = ScriptedDeepAgentsModel(
        responses=[
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "task",
                        "args": {
                            "description": "Return exactly SUBAGENT_RESULT",
                            "subagent_type": "general-purpose",
                        },
                        "id": "call_task",
                    }
                ],
            ),
            AIMessage(content="SUBAGENT_RESULT"),
            AIMessage(content="MAIN_FINAL"),
        ]
    )

    with _use_fake_direct_model(fake):
        model = mc.ModelClientChatModel(force_direct=True, streaming=False)
        agent = create_deep_agent(model=model, tools=[])
        result = agent.invoke({"messages": [{"role": "user", "content": "delegate this"}]})

    messages = result["messages"]
    assert messages[-1].content == "MAIN_FINAL"
    assert any(getattr(message, "content", None) == "SUBAGENT_RESULT" for message in messages)
    assert fake._calls == 3
    assert any("task" in names for names in fake._bound_tool_names)
    assert any("task" not in names and "write_todos" in names for names in fake._bound_tool_names)


def test_latest_deepagents_hitl_interrupt_resume_preserves_tool_call_flow():
    """P0: HITL interrupt/resume keeps tool_call_id flow consumable by ModelClient."""

    fake = ScriptedDeepAgentsModel(
        responses=[
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "pudding_probe",
                        "args": {"text": "hitl"},
                        "id": "call_probe",
                    }
                ],
            ),
            AIMessage(content="DONE"),
        ]
    )

    with _use_fake_direct_model(fake):
        model = mc.ModelClientChatModel(force_direct=True, streaming=False)
        agent = create_deep_agent(
            model=model,
            tools=[pudding_probe],
            interrupt_on={"pudding_probe": True},
            checkpointer=MemorySaver(),
        )
        config = {"configurable": {"thread_id": "model-client-hitl-test"}}

        first = agent.invoke({"messages": [{"role": "user", "content": "trigger hitl"}]}, config=config)
        resumed = agent.invoke(Command(resume={"decisions": [{"type": "approve"}]}), config=config)

    assert "__interrupt__" in first
    assert resumed["messages"][-1].content == "DONE"
    assert any(getattr(message, "content", None) == "TOOL_MARKER:hitl" for message in resumed["messages"])
    assert fake._calls == 2


def test_latest_deepagents_response_format_returns_structured_response():
    """P1: DeepAgents `response_format=` works through ModelClientChatModel."""

    fake = ScriptedDeepAgentsModel(
        responses=[
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "ProbeStructuredAnswer",
                        "args": {"answer": "ok", "score": 9},
                        "id": "call_structured",
                    }
                ],
            )
        ]
    )

    with _use_fake_direct_model(fake):
        model = mc.ModelClientChatModel(force_direct=True, streaming=False)
        agent = create_deep_agent(model=model, tools=[], response_format=ProbeStructuredAnswer)
        result = agent.invoke({"messages": [{"role": "user", "content": "return structured"}]})

    assert result["structured_response"] == ProbeStructuredAnswer(answer="ok", score=9)
    assert any("ProbeStructuredAnswer" in names for names in fake._bound_tool_names)
    assert any(kwargs.get("tool_choice") == "any" for kwargs in fake._bound_kwargs)


def test_latest_deepagents_graph_stream_emits_model_tool_and_final_state():
    """P1: `agent.stream(...)` graph events remain consumable through ModelClient."""

    fake = ScriptedDeepAgentsModel(
        responses=[
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "pudding_probe",
                        "args": {"text": "stream"},
                        "id": "call_probe",
                    }
                ],
            ),
            AIMessage(content="DONE"),
        ]
    )

    with _use_fake_direct_model(fake):
        model = mc.ModelClientChatModel(force_direct=True, streaming=False)
        agent = create_deep_agent(model=model, tools=[pudding_probe])
        chunks = list(
            agent.stream(
                {"messages": [{"role": "user", "content": "stream graph"}]},
                stream_mode=["updates", "values"],
            )
        )

    updates = [payload for mode, payload in chunks if mode == "updates"]
    values = [payload for mode, payload in chunks if mode == "values"]
    assert any("model" in update for update in updates)
    assert any("tools" in update for update in updates)
    assert values[-1]["messages"][-1].content == "DONE"
    assert any(getattr(message, "content", None) == "TOOL_MARKER:stream" for message in values[-1]["messages"])


@pytest.mark.parametrize(
    ("decision", "expected_tool_message"),
    [
        (
            {
                "type": "edit",
                "edited_action": {"name": "pudding_probe", "args": {"text": "edited"}},
            },
            "TOOL_MARKER:edited",
        ),
        (
            {"type": "reject", "message": "blocked by human"},
            "blocked by human",
        ),
        (
            {"type": "respond", "message": "manual human result"},
            "manual human result",
        ),
    ],
)
def test_latest_deepagents_hitl_edit_reject_respond_resume_paths(
    decision: dict[str, Any],
    expected_tool_message: str,
):
    """P1: HITL edit/reject/respond produce ToolMessages the model can consume."""

    fake = ScriptedDeepAgentsModel(
        responses=[
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "pudding_probe",
                        "args": {"text": "original"},
                        "id": "call_probe",
                    }
                ],
            ),
            AIMessage(content="DONE"),
        ]
    )

    with _use_fake_direct_model(fake):
        model = mc.ModelClientChatModel(force_direct=True, streaming=False)
        agent = create_deep_agent(
            model=model,
            tools=[pudding_probe],
            interrupt_on={"pudding_probe": True},
            checkpointer=MemorySaver(),
        )
        config = {"configurable": {"thread_id": f"hitl-{decision['type']}"}}
        first = agent.invoke({"messages": [{"role": "user", "content": "trigger hitl"}]}, config=config)
        resumed = agent.invoke(Command(resume={"decisions": [decision]}), config=config)

    assert "__interrupt__" in first
    assert resumed["messages"][-1].content == "DONE"
    assert any(getattr(message, "content", None) == expected_tool_message for message in resumed["messages"])


def test_latest_deepagents_filesystem_backend_writes_file_through_model_client(tmp_path):
    """P1: FilesystemBackend tool results remain consumable and file lands on disk."""

    fake = ScriptedDeepAgentsModel(
        responses=[
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "write_file",
                        "args": {"file_path": "/artifact.txt", "content": "HELLO"},
                        "id": "call_write",
                    }
                ],
            ),
            AIMessage(content="DONE"),
        ]
    )

    with _use_fake_direct_model(fake):
        model = mc.ModelClientChatModel(force_direct=True, streaming=False)
        agent = create_deep_agent(
            model=model,
            tools=[],
            backend=FilesystemBackend(root_dir=tmp_path, virtual_mode=True),
        )
        result = agent.invoke({"messages": [{"role": "user", "content": "write file"}]})

    assert (tmp_path / "artifact.txt").read_text() == "HELLO"
    assert result["messages"][-1].content == "DONE"
    assert any(getattr(message, "content", None) == "Updated file /artifact.txt" for message in result["messages"])


def test_latest_deepagents_composite_backend_routes_file_write_through_model_client(tmp_path):
    """P1: CompositeBackend route can write to FilesystemBackend and continue model loop."""

    fake = ScriptedDeepAgentsModel(
        responses=[
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "write_file",
                        "args": {"file_path": "/artifacts/artifact.txt", "content": "HELLO"},
                        "id": "call_write",
                    }
                ],
            ),
            AIMessage(content="DONE"),
        ]
    )
    backend = CompositeBackend(
        default=StateBackend(),
        routes={"/artifacts/": FilesystemBackend(root_dir=tmp_path, virtual_mode=True)},
    )

    with _use_fake_direct_model(fake):
        model = mc.ModelClientChatModel(force_direct=True, streaming=False)
        agent = create_deep_agent(model=model, tools=[], backend=backend)
        result = agent.invoke({"messages": [{"role": "user", "content": "write routed file"}]})

    assert (tmp_path / "artifact.txt").read_text() == "HELLO"
    assert result["messages"][-1].content == "DONE"
    assert any(
        getattr(message, "content", None) == "Updated file /artifacts/artifact.txt"
        for message in result["messages"]
    )


def test_latest_deepagents_custom_subagent_uses_model_client_and_returns_to_main_agent():
    """P1: custom SubAgent inherits ModelClientChatModel path and returns a task result."""

    fake = ScriptedDeepAgentsModel(
        responses=[
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "task",
                        "args": {"description": "review", "subagent_type": "reviewer"},
                        "id": "call_task",
                    }
                ],
            ),
            AIMessage(content="CUSTOM_RESULT"),
            AIMessage(content="MAIN_DONE"),
        ]
    )
    subagents = [
        SubAgent(
            name="reviewer",
            description="reviewer",
            system_prompt="Return CUSTOM_RESULT",
        )
    ]

    with _use_fake_direct_model(fake):
        model = mc.ModelClientChatModel(force_direct=True, streaming=False)
        agent = create_deep_agent(model=model, tools=[], subagents=subagents)
        result = agent.invoke({"messages": [{"role": "user", "content": "delegate"}]})

    assert result["messages"][-1].content == "MAIN_DONE"
    assert any(getattr(message, "content", None) == "CUSTOM_RESULT" for message in result["messages"])
    assert fake._calls == 3


def test_latest_deepagents_compiled_subagent_result_is_consumed_by_main_agent():
    """P1: CompiledSubAgent output returns through task ToolMessage and main model continues."""

    fake = ScriptedDeepAgentsModel(
        responses=[
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "task",
                        "args": {"description": "review", "subagent_type": "reviewer"},
                        "id": "call_task",
                    }
                ],
            ),
            AIMessage(content="MAIN_DONE"),
        ]
    )
    subagents = [
        CompiledSubAgent(
            name="reviewer",
            description="compiled reviewer",
            runnable=RunnableLambda(lambda _state: {"messages": [AIMessage(content="COMPILED_RESULT")]}),
        )
    ]

    with _use_fake_direct_model(fake):
        model = mc.ModelClientChatModel(force_direct=True, streaming=False)
        agent = create_deep_agent(model=model, tools=[], subagents=subagents)
        result = agent.invoke({"messages": [{"role": "user", "content": "delegate"}]})

    assert result["messages"][-1].content == "MAIN_DONE"
    assert any(getattr(message, "content", None) == "COMPILED_RESULT" for message in result["messages"])
    assert fake._calls == 2
