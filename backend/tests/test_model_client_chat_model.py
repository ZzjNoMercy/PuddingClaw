"""ModelClientChatModel LangChain contract tests."""

from __future__ import annotations

import asyncio
from typing import Any
from unittest import mock

import pytest
from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.messages import AIMessage, AIMessageChunk, HumanMessage
from langchain_core.runnables.config import var_child_runnable_config
from pydantic import BaseModel, Field

from llm.model_client import (
    INTERNAL_CALL_MARKER,
    ModelClient,
    ModelClientChatModel,
    ModelTransportInterruptedError,
)


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

    def bind_tools(self, tools: list[Any], **kwargs: Any) -> FakeBoundModel:
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


def test_bind_tools_preserves_explicit_non_thinking_mode():
    wrapped = ModelClientChatModel(
        force_direct=True,
        streaming=False,
        thinking_enabled=False,
        model_override="deepseek-v4-flash",
    ).bind_tools(
        [{"type": "function", "function": {"name": "grade"}}],
        tool_choice="required",
    )

    assert wrapped._client.thinking_enabled is False
    assert wrapped._client.model_override == "deepseek-v4-flash"
    assert wrapped._client.cfg.get("model") == "deepseek-v4-flash"
    assert wrapped.disable_streaming is True
    assert wrapped._client.cfg.get("reasoning_effort") is None
    assert wrapped._client.cfg.get("extra_body") is None


def test_bind_tools_preserves_conversation_model_route_and_thinking_level():
    route = "kimi:kimi-openai:kimi-k3:llm"
    resolution_calls: list[dict[str, Any]] = []

    def fake_llm_config(**kwargs: Any) -> dict[str, Any]:
        resolution_calls.append(dict(kwargs))
        return {
            "provider": "kimi",
            "model": "kimi-k3",
            "protocol": "openai_compatible",
            "model_id": route,
            "temperature": 0.7,
            "thinking_level": "max",
            "credential_name": "evaluate",
            "reasoning_effort": "max",
        }

    with mock.patch(
        "llm.model_client.get_fallback_llm_config",
        side_effect=fake_llm_config,
    ):
        wrapped = ModelClientChatModel(
            force_direct=True,
            streaming=True,
            model_id_override=route,
            thinking_level="max",
            credential_name="evaluate",
        ).bind_tools(
            [{"type": "function", "function": {"name": "probe"}}],
            tool_choice="auto",
        )

    assert len(resolution_calls) == 2
    assert all(call["model_id_override"] == route for call in resolution_calls)
    assert all(call["thinking_level"] == "max" for call in resolution_calls)
    assert all(call["credential_name"] == "evaluate" for call in resolution_calls)
    assert wrapped._client.model_id_override == route
    assert wrapped._client.thinking_level == "max"
    assert wrapped._client.credential_name == "evaluate"
    assert wrapped._model_trace_params()["credential_name"] == "evaluate"
    assert wrapped._client.cfg["provider"] == "kimi"
    assert wrapped._client.cfg["model"] == "kimi-k3"


def test_model_client_chat_model_marks_context_summary_as_internal():
    """DeepAgents' context compressor output must be distinguishable from user text."""

    fake = FakeBoundModel(content="## SESSION INTENT\ninternal summary")
    model = ModelClientChatModel(force_direct=True, streaming=False)
    summary_prompt = (
        "<role>\nContext Extraction Assistant\n</role>\n"
        "## SESSION INTENT\n"
        "<messages>\nMessages to summarize:\nhello\n</messages>"
    )

    writer = mock.Mock()
    with mock.patch("llm.model_client.ModelClient._direct_model", return_value=fake):
        with mock.patch("llm.model_client.get_stream_writer", return_value=writer):
            result = model.invoke(summary_prompt)

    assert result.additional_kwargs[INTERNAL_CALL_MARKER] == "context_summary"
    context_events = [
        call
        for call in writer.call_args_list
        if call.args[0].get("type") == "context_maintenance"
    ]
    assert context_events == [
        mock.call(
            {
                "type": "context_maintenance",
                "status": "start",
                "phase": "deepagents_summarization",
                "message": "上下文达到压缩阈值，正在压缩，完成后将继续生成...",
            }
        ),
        mock.call(
            {
                "type": "context_maintenance",
                "status": "done",
                "phase": "deepagents_summarization",
            }
        ),
    ]
    usage_events = [
        call.args[0]
        for call in writer.call_args_list
        if call.args[0].get("type") == "model_usage"
    ]
    assert len(usage_events) == 1
    assert usage_events[0]["measured"] is False


def test_model_client_chat_model_does_not_mark_regular_answer_as_internal():
    fake = FakeBoundModel(content="regular answer")
    model = ModelClientChatModel(force_direct=True, streaming=False)

    with mock.patch("llm.model_client.ModelClient._direct_model", return_value=fake):
        result = model.invoke("hello")

    assert INTERNAL_CALL_MARKER not in result.additional_kwargs


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


def test_model_client_stream_does_not_fallback_after_first_chunk():
    """Gateway stream failure after emitting a chunk must not duplicate content via fallback."""

    class FailsAfterFirstChunk(FakeBoundModel):
        def stream(self, *args: Any, **kwargs: Any):
            yield AIMessageChunk(content="partial")
            raise RuntimeError("stream broke")

    client = ModelClient()
    with mock.patch.object(client, "get_chat_model", return_value=FailsAfterFirstChunk()):
        with pytest.raises(RuntimeError, match="stream broke"):
            list(client.stream([HumanMessage(content="hello")]))


@pytest.mark.asyncio
async def test_model_client_astream_emits_first_chunk_before_provider_finishes():
    class PausesAfterFirstChunk(FakeBoundModel):
        def __init__(self) -> None:
            super().__init__()
            self.release = asyncio.Event()

        async def astream(self, *args: Any, **kwargs: Any):
            yield AIMessageChunk(content="first")
            await self.release.wait()
            yield AIMessageChunk(content="second")

    provider = PausesAfterFirstChunk()
    client = ModelClient()

    with mock.patch.object(client, "get_chat_model", return_value=provider):
        stream = client.astream([HumanMessage(content="hello")])
        first = await asyncio.wait_for(anext(stream), timeout=0.1)
        assert first.content == "first"
        provider.release.set()
        remaining = [chunk async for chunk in stream]

    assert [chunk.content for chunk in remaining] == ["second"]


@pytest.mark.asyncio
async def test_model_client_astream_stops_without_retry_after_emitting_partial_chunk():
    class RemoteProtocolError(Exception):
        pass

    class FailsAfterFirstChunk(FakeBoundModel):
        async def astream(self, *args: Any, **kwargs: Any):
            yield AIMessageChunk(content="discarded-partial")
            raise RemoteProtocolError("peer closed connection: incomplete chunked read")

    client = ModelClient()
    with mock.patch.object(client, "get_chat_model", return_value=FailsAfterFirstChunk()):
        chunks = []
        with pytest.raises(ModelTransportInterruptedError) as exc_info:
            async for chunk in client.astream([HumanMessage(content="hello")]):
                chunks.append(chunk)

    assert [chunk.content for chunk in chunks] == ["discarded-partial"]
    assert exc_info.value.chunks_received == 1


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
