"""Regression tests for OpenAI tool-call protocol repair."""

from langchain.agents.middleware.types import ModelRequest, ModelResponse
from langchain_core.messages import (
    AIMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
    message_to_dict,
    messages_from_dict,
)

from graph.middlewares import tool_protocol
from graph.middlewares.tool_protocol import (
    ToolProtocolIntegrityMiddleware,
    pending_executable_tool_call_ids,
    repair_tool_message_protocol,
)


def _empty_report() -> dict[str, list[str]]:
    return {
        "missing_tool_call_ids": [],
        "orphan_tool_call_ids": [],
        "invalid_tool_call_ids": [],
        "raw_tool_call_ids": [],
        "dropped_unpairable_tool_calls": [],
        "duplicate_tool_call_ids": [],
        "canonicalized_shadow_tool_call_ids": [],
        "canonicalized_tool_call_ids": [],
        "dropped_legacy_function_calls": [],
    }


def test_context_usage_distinguishes_model_input_from_next_turn_context(monkeypatch) -> None:
    events: list[dict] = []
    monkeypatch.setattr(tool_protocol, "get_stream_writer", lambda: events.append)
    request = ModelRequest(
        model=None,
        system_message=SystemMessage(content="system " * 200),
        messages=[HumanMessage(content="hello")],
        tools=[
            {
                "name": "demo",
                "description": "tool schema " * 200,
                "parameters": {"type": "object", "properties": {}},
            }
        ],
        state={"messages": []},
    )
    response = ModelResponse(result=[AIMessage(content="hello back")])

    ToolProtocolIntegrityMiddleware()._emit_context_usage(request, response)

    assert len(events) == 1
    event = events[0]
    assert event["used_tokens"] >= event["input_tokens_estimated"]
    assert event["assistant_tokens_estimated"] == event["used_tokens"] - event["input_tokens_estimated"]
    assert event["scope"] == "next_turn_effective_context"
    assert event["measurement"] == "approximate"
    assert event["includes_tool_schemas"] is True


def test_protocol_repair_inserts_missing_tool_response_before_next_message() -> None:
    messages = [
        AIMessage(
            content="",
            tool_calls=[{"id": "call-1", "name": "read_file", "args": {"path": "/a"}}],
        ),
        HumanMessage(content="继续"),
    ]

    repaired, report = repair_tool_message_protocol(messages)

    assert [type(item) for item in repaired] == [AIMessage, ToolMessage, HumanMessage]
    assert repaired[1].tool_call_id == "call-1"
    assert repaired[1].status == "error"
    assert report["missing_tool_call_ids"] == ["call-1"]


def test_protocol_repair_preserves_complete_parallel_tool_responses() -> None:
    messages = [
        AIMessage(
            content="",
            tool_calls=[
                {"id": "call-1", "name": "read_file", "args": {}},
                {"id": "call-2", "name": "read_file", "args": {}},
            ],
        ),
        ToolMessage(content="one", tool_call_id="call-1"),
        ToolMessage(content="two", tool_call_id="call-2"),
    ]

    repaired, report = repair_tool_message_protocol(messages)

    assert repaired == messages
    assert report == _empty_report()


def test_protocol_repair_removes_orphan_tool_response() -> None:
    messages = [ToolMessage(content="orphan", tool_call_id="call-x"), HumanMessage(content="hi")]

    repaired, report = repair_tool_message_protocol(messages)

    assert repaired == [messages[1]]
    assert report["orphan_tool_call_ids"] == ["call-x"]


def test_protocol_repair_closes_invalid_tool_call_before_rubric_feedback() -> None:
    invalid = AIMessage(
        content="",
        invalid_tool_calls=[
            {
                "id": "call-invalid",
                "name": "patch_file",
                "args": "{broken",
                "error": "invalid json",
                "type": "invalid_tool_call",
            }
        ],
    )
    feedback = HumanMessage(content="请继续修正", name="rubric_grader")

    repaired, report = repair_tool_message_protocol([invalid, feedback])

    assert [type(item) for item in repaired] == [AIMessage, ToolMessage, HumanMessage]
    assert repaired[1].tool_call_id == "call-invalid"
    assert repaired[1].status == "error"
    assert report["invalid_tool_call_ids"] == ["call-invalid"]
    assert report["missing_tool_call_ids"] == ["call-invalid"]


def test_protocol_repair_closes_raw_additional_tool_call() -> None:
    parsed_by_constructor = AIMessage(
        content="",
        additional_kwargs={
            "tool_calls": [
                {
                    "id": "call-raw",
                    "type": "function",
                    "function": {"name": "read_file", "arguments": "{}"},
                }
            ]
        },
    )
    raw = parsed_by_constructor.model_copy(update={"tool_calls": [], "invalid_tool_calls": []})

    repaired, report = repair_tool_message_protocol([raw, HumanMessage(content="grader")])

    assert repaired[1].tool_call_id == "call-raw"
    assert repaired[0].additional_kwargs["tool_calls"][0]["id"] == "call-raw"
    assert report["raw_tool_call_ids"] == ["call-raw"]
    assert report["missing_tool_call_ids"] == ["call-raw"]


def test_protocol_repair_drops_unpairable_raw_call_without_id() -> None:
    parsed_by_constructor = AIMessage(
        content="",
        additional_kwargs={
            "tool_calls": [
                {
                    "type": "function",
                    "function": {"name": "read_file", "arguments": "{}"},
                }
            ]
        },
    )
    raw = parsed_by_constructor.model_copy(update={"tool_calls": [], "invalid_tool_calls": []})

    repaired, report = repair_tool_message_protocol([raw, HumanMessage(content="grader")])

    assert [type(item) for item in repaired] == [AIMessage, HumanMessage]
    assert "tool_calls" not in repaired[0].additional_kwargs
    assert report["dropped_unpairable_tool_calls"] == ["raw:<missing-id>"]


def test_protocol_repair_drops_calls_with_invalid_provider_shape() -> None:
    parsed = AIMessage(
        content="",
        tool_calls=[{"id": "call-parsed", "name": "bad name", "args": {}}],
    )
    invalid = AIMessage(
        content="",
        invalid_tool_calls=[
            {
                "id": "call-invalid",
                "name": "patch_file",
                "args": None,
                "error": "bad",
                "type": "invalid_tool_call",
            }
        ],
    )
    raw_by_constructor = AIMessage(
        content="",
        additional_kwargs={
            "tool_calls": [
                {
                    "id": "call-raw",
                    "type": "custom",
                    "function": {"name": "read_file", "arguments": {}},
                }
            ]
        },
    )
    raw = raw_by_constructor.model_copy(update={"tool_calls": [], "invalid_tool_calls": []})

    repaired, report = repair_tool_message_protocol([parsed, invalid, raw, HumanMessage(content="next")])

    assert [item.type for item in repaired] == ["ai", "ai", "ai", "human"]
    assert report["dropped_unpairable_tool_calls"] == [
        "parsed:call-parsed:<invalid-name>",
        "invalid:call-invalid:<invalid-args>",
        "raw:call-raw:<invalid-shape>",
    ]


def test_pending_executable_calls_deduplicates_call_ids() -> None:
    duplicate = AIMessage(
        content="",
        tool_calls=[
            {"id": "same", "name": "read_file", "args": {}},
            {"id": "same", "name": "read_file", "args": {}},
        ],
    )

    assert pending_executable_tool_call_ids([duplicate, ToolMessage(content="ok", tool_call_id="same")]) == []


def test_tool_response_id_must_match_exactly() -> None:
    call = AIMessage(
        content="",
        tool_calls=[{"id": "call-exact", "name": "read_file", "args": {}}],
    )

    repaired, report = repair_tool_message_protocol([call, ToolMessage(content="wrong", tool_call_id=" call-exact ")])

    assert [item.type for item in repaired] == ["ai", "tool"]
    assert repaired[1].tool_call_id == "call-exact"
    assert repaired[1].status == "error"
    assert report["orphan_tool_call_ids"] == [" call-exact "]
    assert report["missing_tool_call_ids"] == ["call-exact"]


def test_protocol_repair_canonicalizes_shadow_raw_calls() -> None:
    message = AIMessage(
        content="",
        tool_calls=[{"id": "call-1", "name": "read_file", "args": {}}],
        additional_kwargs={
            "tool_calls": [
                {
                    "id": "call-1",
                    "type": "function",
                    "function": {"name": "read_file", "arguments": "{}"},
                }
            ]
        },
    )

    repaired, report = repair_tool_message_protocol([message, ToolMessage(content="ok", tool_call_id="call-1")])

    assert "tool_calls" not in repaired[0].additional_kwargs
    assert report["canonicalized_shadow_tool_call_ids"] == ["call-1"]


def test_protocol_repair_rejects_whitespace_and_non_string_identifiers() -> None:
    whitespace = AIMessage(
        content="",
        tool_calls=[{"id": " call-x ", "name": " read_file ", "args": {}}],
    )
    valid = AIMessage(
        content="",
        tool_calls=[{"id": "call-y", "name": "read_file", "args": {}}],
    )
    non_string = valid.model_copy(update={"tool_calls": [{"id": 123, "name": ["read_file"], "args": {}}]})

    repaired, report = repair_tool_message_protocol([whitespace, non_string, HumanMessage(content="next")])

    assert [item.type for item in repaired] == ["ai", "ai", "human"]
    assert report["dropped_unpairable_tool_calls"] == [
        "parsed:<invalid-id>",
        "parsed:<missing-id>",
    ]


def test_raw_calls_do_not_accept_fields_provider_will_not_serialize() -> None:
    parsed_by_constructor = AIMessage(
        content="",
        additional_kwargs={
            "tool_calls": [
                {
                    "tool_call_id": "fallback-id",
                    "name": "read_file",
                    "type": "function",
                    "function": {"arguments": "{}"},
                }
            ]
        },
    )
    raw = parsed_by_constructor.model_copy(update={"tool_calls": [], "invalid_tool_calls": []})

    repaired, report = repair_tool_message_protocol([raw, HumanMessage(content="next")])

    assert [item.type for item in repaired] == ["ai", "human"]
    assert "tool_calls" not in repaired[0].additional_kwargs
    assert report["dropped_unpairable_tool_calls"] == ["raw:<missing-id>"]


def test_raw_call_is_rebuilt_to_exact_provider_shape() -> None:
    parsed_by_constructor = AIMessage(
        content="",
        additional_kwargs={
            "tool_calls": [
                {
                    "id": "call-raw",
                    "type": "function",
                    "function": {
                        "name": "read_file",
                        "arguments": "{}",
                        "extra": "drop-me",
                    },
                    "index": 0,
                }
            ]
        },
    )
    raw = parsed_by_constructor.model_copy(update={"tool_calls": [], "invalid_tool_calls": []})

    repaired, report = repair_tool_message_protocol([raw, ToolMessage(content="ok", tool_call_id="call-raw")])

    assert repaired[0].additional_kwargs["tool_calls"] == [
        {
            "id": "call-raw",
            "type": "function",
            "function": {"name": "read_file", "arguments": "{}"},
        }
    ]
    assert report["canonicalized_tool_call_ids"] == ["raw:call-raw"]


def test_model_boundary_removes_empty_or_invalid_raw_container() -> None:
    from langchain.agents.middleware.types import ModelRequest

    for raw_value in ([], None, {"bad": "shape"}):
        request = ModelRequest(
            model=None,
            messages=[AIMessage(content="", additional_kwargs={"tool_calls": raw_value})],
            tools=[],
            state={"messages": []},
        )

        prepared = ToolProtocolIntegrityMiddleware._prepare(request)

        assert "tool_calls" not in prepared.messages[0].additional_kwargs


def test_model_boundary_removes_unsupported_legacy_function_call() -> None:
    from langchain.agents.middleware.types import ModelRequest
    from langchain_openai.chat_models.base import _convert_message_to_dict

    request = ModelRequest(
        model=None,
        messages=[
            AIMessage(
                content="",
                additional_kwargs={"function_call": {"name": "legacy_tool", "arguments": "{}"}},
            )
        ],
        tools=[],
        state={"messages": []},
    )

    prepared = ToolProtocolIntegrityMiddleware._prepare(request)
    provider_message = _convert_message_to_dict(prepared.messages[0])

    assert "function_call" not in prepared.messages[0].additional_kwargs
    assert "function_call" not in provider_message


def test_pending_executable_calls_only_reports_live_parsed_tail() -> None:
    live = AIMessage(
        content="",
        tool_calls=[{"id": "call-live", "name": "read_file", "args": {}}],
    )
    invalid = AIMessage(
        content="",
        invalid_tool_calls=[
            {
                "id": "call-invalid",
                "name": "read_file",
                "args": "{",
                "error": "bad",
                "type": "invalid_tool_call",
            }
        ],
    )

    assert pending_executable_tool_call_ids([live]) == ["call-live"]
    assert pending_executable_tool_call_ids([live, ToolMessage(content="ok", tool_call_id="call-live")]) == []
    assert pending_executable_tool_call_ids([invalid]) == []


def test_model_boundary_guard_repairs_hidden_call_on_rubric_jump() -> None:
    from langchain.agents.middleware.types import ModelRequest

    request = ModelRequest(
        model=None,
        messages=[
            AIMessage(
                content="",
                invalid_tool_calls=[
                    {
                        "id": "call-hidden",
                        "name": "patch_file",
                        "args": "{",
                        "error": "bad",
                        "type": "invalid_tool_call",
                    }
                ],
            ),
            HumanMessage(content="rubric feedback", name="rubric_grader"),
        ],
        tools=[],
        state={"messages": []},
    )

    prepared = ToolProtocolIntegrityMiddleware._prepare(request)

    assert [item.type for item in prepared.messages] == ["ai", "tool", "human"]
    assert prepared.messages[1].tool_call_id == "call-hidden"


def test_repaired_protocol_survives_storage_and_openai_serialization_roundtrip() -> None:
    from langchain_openai.chat_models.base import _convert_message_to_dict

    messages = [
        AIMessage(
            content="",
            invalid_tool_calls=[
                {
                    "id": "call-hidden",
                    "name": "patch_file",
                    "args": "{",
                    "error": "bad",
                    "type": "invalid_tool_call",
                }
            ],
        ),
        HumanMessage(content="rubric feedback", name="rubric_grader"),
    ]
    repaired, _report = repair_tool_message_protocol(messages)
    restored = messages_from_dict([message_to_dict(message) for message in repaired])
    provider_messages = [_convert_message_to_dict(message) for message in restored]

    assert provider_messages[0]["tool_calls"][0]["id"] == "call-hidden"
    assert provider_messages[1]["tool_call_id"] == "call-hidden"
    assert provider_messages[2]["role"] == "user"
