"""ModelClientChatModel LangChain contract tests."""

from __future__ import annotations

from typing import Any
from unittest import mock

import pytest
from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.messages import AIMessage, AIMessageChunk, HumanMessage
from langchain_core.runnables.config import var_child_runnable_config
from pydantic import BaseModel, Field

from llm.model_client import ModelClient, ModelClientChatModel


class FakeBoundModel:
    """Small fake chat model that records LangChain call contract details."""

    def __init__(self, *, fail: bool = False, content: str = "ok") -> None:
        self.fail = fail
        self.content = content
        self.bound_tools: list[Any] | None = None
        self.bound_kwargs: dict[str, Any] | None = None
        self.invoke_calls: list[dict[str, Any]] = []
        self.ainvoke_calls: list[dict[str, Any]] = []
        self.stream_calls: list[dict[str, Any]] = []
        self.astream_calls: list[dict[str, Any]] = []
        self.stream_contexts: list[Any] = []
        self.astream_contexts: list[Any] = []
        self.chunks: list[AIMessageChunk] = [
            AIMessageChunk(content="hel"),
            AIMessageChunk(content="lo"),
        ]

    def bind_tools(self, tools: list[Any], **kwargs: Any) -> "FakeBoundModel":
        self.bound_tools = list(tools)
        self.bound_kwargs = dict(kwargs)
        return self

    def invoke(
        self,
        messages: list[Any],
        config: Any = None,
        *,
        stop: list[str] | None = None,
        **kwargs: Any,
    ) -> AIMessage:
        self.invoke_calls.append(
            {
                "messages": messages,
                "config": config,
                "stop": stop,
                "kwargs": dict(kwargs),
            }
        )
        if self.fail:
            raise RuntimeError("fake model failed")
        return AIMessage(content=self.content)

    async def ainvoke(
        self,
        messages: list[Any],
        config: Any = None,
        *,
        stop: list[str] | None = None,
        **kwargs: Any,
    ) -> AIMessage:
        self.ainvoke_calls.append(
            {
                "messages": messages,
                "config": config,
                "stop": stop,
                "kwargs": dict(kwargs),
            }
        )
        if self.fail:
            raise RuntimeError("fake model failed")
        return AIMessage(content=self.content)

    def stream(
        self,
        messages: list[Any],
        config: Any = None,
        *,
        stop: list[str] | None = None,
        **kwargs: Any,
    ):
        self.stream_calls.append(
            {
                "messages": messages,
                "config": config,
                "stop": stop,
                "kwargs": dict(kwargs),
            }
        )
        if self.fail:
            raise RuntimeError("fake model failed")
        self.stream_contexts.append(var_child_runnable_config.get())
        yield from self.chunks

    async def astream(
        self,
        messages: list[Any],
        config: Any = None,
        *,
        stop: list[str] | None = None,
        **kwargs: Any,
    ):
        self.astream_calls.append(
            {
                "messages": messages,
                "config": config,
                "stop": stop,
                "kwargs": dict(kwargs),
            }
        )
        if self.fail:
            raise RuntimeError("fake model failed")
        self.astream_contexts.append(var_child_runnable_config.get())
        for chunk in self.chunks:
            yield chunk


class ProbeAnswer(BaseModel):
    """Structured answer for ModelClient tests."""

    answer: str = Field(description="short answer")
    score: int = Field(description="confidence score")


class RecordingCallback(BaseCallbackHandler):
    """Records callback lifecycle events emitted by BaseChatModel."""

    def __init__(self) -> None:
        self.events: list[tuple[str, dict[str, Any]]] = []

    def on_chat_model_start(self, *args: Any, **kwargs: Any) -> Any:
        self.events.append(("start", kwargs))

    def on_llm_end(self, *args: Any, **kwargs: Any) -> Any:
        self.events.append(("end", kwargs))

    def on_llm_error(self, *args: Any, **kwargs: Any) -> Any:
        self.events.append(("error", kwargs))


def test_model_client_bind_tools_preserves_provider_kwargs():
    """`ModelClient` should behave like ChatOpenAI/ChatDeepSeek bind_tools."""

    tool_def = {"type": "function", "function": {"name": "probe", "description": "probe"}}
    fake = FakeBoundModel()
    client = ModelClient(
        force_direct=True,
        tools=[tool_def],
        bind_tools_kwargs={
            "tool_choice": "required",
            "strict": True,
            "parallel_tool_calls": False,
        },
    )

    with mock.patch.object(client, "_direct_model", return_value=fake):
        returned = client.get_chat_model()

    assert returned is fake
    assert fake.bound_tools == [tool_def]
    assert fake.bound_kwargs == {
        "tool_choice": "required",
        "strict": True,
        "parallel_tool_calls": False,
    }


def test_model_client_gateway_fallback_preserves_tools_config_stop_and_kwargs():
    """Fallback direct provider must keep tool schema and invocation params."""

    tool_def = {"type": "function", "function": {"name": "probe", "description": "probe"}}
    gateway = FakeBoundModel(fail=True)
    direct = FakeBoundModel(content="fallback")
    client = ModelClient(
        tools=[tool_def],
        bind_tools_kwargs={"tool_choice": "required", "strict": True},
    )
    client.gateway_cfg = {"fallback_to_direct": True}

    with mock.patch.object(client, "_should_use_gateway", return_value=True):
        with mock.patch.object(client, "get_chat_model", return_value=gateway):
            with mock.patch.object(client, "_direct_model", return_value=direct):
                with mock.patch("llm.model_client.record_token_usage"):
                    result = client.invoke(
                        [HumanMessage(content="hello")],
                        config={"tags": ["probe"]},
                        stop=["END"],
                        timeout=3,
                    )

    assert result.content == "fallback"
    assert direct.bound_tools == [tool_def]
    assert direct.bound_kwargs == {"tool_choice": "required", "strict": True}
    assert direct.invoke_calls[0]["config"] == {"tags": ["probe"]}
    assert direct.invoke_calls[0]["stop"] == ["END"]
    assert direct.invoke_calls[0]["kwargs"] == {"timeout": 3}


def test_model_client_chat_model_uses_base_chat_model_input_conversion_and_kwargs():
    """The wrapper should accept string input like a standard BaseChatModel."""

    tool_def = {"type": "function", "function": {"name": "probe", "description": "probe"}}
    fake = FakeBoundModel(content="wrapped")
    wrapped = ModelClientChatModel(force_direct=True, streaming=False).bind_tools(
        [tool_def],
        tool_choice="required",
        strict=True,
    )

    with mock.patch("llm.model_client.ModelClient._direct_model", return_value=fake):
        result = wrapped.invoke("hello", stop=["END"], timeout=3)

    assert result.content == "wrapped"
    assert fake.bound_tools == [tool_def]
    assert fake.bound_kwargs == {"tool_choice": "required", "strict": True}
    call = fake.invoke_calls[0]
    assert len(call["messages"]) == 1
    assert isinstance(call["messages"][0], HumanMessage)
    assert call["messages"][0].content == "hello"
    assert call["stop"] == ["END"]
    assert call["kwargs"] == {"timeout": 3}


def test_model_client_chat_model_with_structured_output_parses_pydantic_schema():
    """Structured output should work through LangChain's bind-tools parser."""

    fake = FakeBoundModel()
    fake.content = ""

    def fake_invoke(messages: list[Any], config: Any = None, *, stop: list[str] | None = None, **kwargs: Any):
        fake.invoke_calls.append({"messages": messages, "config": config, "stop": stop, "kwargs": dict(kwargs)})
        return AIMessage(
            content="",
            tool_calls=[
                {
                    "name": "ProbeAnswer",
                    "args": {"answer": "ok", "score": 7},
                    "id": "call_structured",
                }
            ],
        )

    fake.invoke = fake_invoke  # type: ignore[method-assign]
    model = ModelClientChatModel(force_direct=True, streaming=False)
    structured = model.with_structured_output(ProbeAnswer)

    with mock.patch("llm.model_client.ModelClient._direct_model", return_value=fake):
        result = structured.invoke("return structured output")

    assert result == ProbeAnswer(answer="ok", score=7)
    assert fake.bound_tools is not None
    assert fake.bound_kwargs is not None
    assert fake.bound_kwargs.get("ls_structured_output_format") is not None


def test_model_client_chat_model_stream_preserves_chunk_order_stop_and_kwargs():
    """Synchronous streaming should preserve chunk order and invocation params."""

    fake = FakeBoundModel()
    callback = RecordingCallback()
    with mock.patch("llm.model_client.ModelClient._direct_model", return_value=fake):
        model = ModelClientChatModel(force_direct=True, streaming=True)
        chunks = list(model.stream("hello", config={"callbacks": [callback]}, stop=["END"], timeout=3))

    assert [chunk.content for chunk in chunks if chunk.content] == ["hel", "lo"]
    assert fake.stream_calls[0]["config"] is None
    assert fake.stream_contexts == [None]
    assert fake.stream_calls[0]["stop"] == ["END"]
    assert fake.stream_calls[0]["kwargs"] == {"timeout": 3}


@pytest.mark.asyncio
async def test_model_client_chat_model_astream_preserves_chunk_order_stop_and_kwargs():
    """Async streaming should preserve chunk order and invocation params."""

    fake = FakeBoundModel()
    callback = RecordingCallback()
    with mock.patch("llm.model_client.ModelClient._direct_model", return_value=fake):
        model = ModelClientChatModel(force_direct=True, streaming=True)
        chunks = [
            chunk
            async for chunk in model.astream(
                "hello",
                config={"callbacks": [callback]},
                stop=["END"],
                timeout=3,
            )
        ]

    assert [chunk.content for chunk in chunks if chunk.content] == ["hel", "lo"]
    assert fake.astream_calls[0]["config"] is None
    assert fake.astream_contexts == [None]
    assert fake.astream_calls[0]["stop"] == ["END"]
    assert fake.astream_calls[0]["kwargs"] == {"timeout": 3}


def test_model_client_stream_fallback_before_first_chunk_preserves_tools_and_kwargs():
    """Gateway stream failure before the first chunk may fallback to direct with tools kept."""

    tool_def = {"type": "function", "function": {"name": "probe", "description": "probe"}}
    gateway = FakeBoundModel(fail=True)
    direct = FakeBoundModel()
    client = ModelClient(tools=[tool_def], bind_tools_kwargs={"tool_choice": "required"})
    client.gateway_cfg = {"fallback_to_direct": True}

    with mock.patch.object(client, "_should_use_gateway", return_value=True):
        with mock.patch.object(client, "get_chat_model", return_value=gateway):
            with mock.patch.object(client, "_direct_model", return_value=direct):
                with mock.patch("llm.model_client.record_token_usage"):
                    chunks = list(client.stream([HumanMessage(content="hello")], stop=["END"], timeout=3))

    assert [chunk.content for chunk in chunks] == ["hel", "lo"]
    assert direct.bound_tools == [tool_def]
    assert direct.bound_kwargs == {"tool_choice": "required"}
    assert direct.stream_calls[0]["stop"] == ["END"]
    assert direct.stream_calls[0]["kwargs"] == {"timeout": 3}


def test_model_client_stream_does_not_fallback_after_first_chunk():
    """Gateway stream failure after emitting a chunk must not duplicate content via fallback."""

    class FailsAfterFirstChunk(FakeBoundModel):
        def stream(self, *args: Any, **kwargs: Any):
            yield AIMessageChunk(content="partial")
            raise RuntimeError("stream broke")

    client = ModelClient()
    client.gateway_cfg = {"fallback_to_direct": True}

    with mock.patch.object(client, "_should_use_gateway", return_value=True):
        with mock.patch.object(client, "get_chat_model", return_value=FailsAfterFirstChunk()):
            with mock.patch.object(client, "_direct_model", return_value=FakeBoundModel(content="fallback")):
                with pytest.raises(RuntimeError, match="stream broke"):
                    list(client.stream([HumanMessage(content="hello")]))


def test_model_client_chat_model_callbacks_receive_success_lifecycle():
    """Wrapper callbacks should receive start/end events through BaseChatModel."""

    fake = FakeBoundModel(content="ok")
    callback = RecordingCallback()
    with mock.patch("llm.model_client.ModelClient._direct_model", return_value=fake):
        model = ModelClientChatModel(force_direct=True, streaming=False)
        result = model.invoke(
            "hello",
            config={
                "callbacks": [callback],
                "tags": ["model-client"],
                "metadata": {"probe": "callbacks"},
                "run_name": "model-client-probe",
            },
        )

    assert result.content == "ok"
    assert [event for event, _ in callback.events] == ["start", "end"]
    start_payload = callback.events[0][1]
    assert start_payload["tags"] == ["model-client"]
    assert start_payload["metadata"]["probe"] == "callbacks"
    assert start_payload["name"] == "model-client-probe"


def test_model_client_chat_model_callbacks_receive_error_lifecycle():
    """Wrapper callbacks should receive error events when the underlying model fails."""

    fake = FakeBoundModel(fail=True)
    callback = RecordingCallback()
    with mock.patch("llm.model_client.ModelClient._direct_model", return_value=fake):
        model = ModelClientChatModel(force_direct=True, streaming=False)
        with pytest.raises(RuntimeError, match="fake model failed"):
            model.invoke("hello", config={"callbacks": [callback]})

    assert [event for event, _ in callback.events] == ["start", "error"]
