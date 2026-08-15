"""Per-Run provider usage aggregation tests."""

from graph.run_usage import RunUsageAccumulator


def test_run_usage_aggregates_provider_facts_and_uses_langchain_cache_semantics():
    usage = RunUsageAccumulator()
    assert usage.add_model_event(
        {
            "call_id": "call-1",
            "role": "agent",
            "measured": True,
            "input_tokens": 100,
            "output_tokens": 20,
            "total_tokens": 120,
            "cache_read_tokens": 80,
            "reasoning_tokens": 4,
            "duration_ms": 500,
            "tokens_per_second": 40,
        }
    )
    assert usage.add_model_event(
        {
            "call_id": "call-2",
            "role": "summary",
            "measured": True,
            "input_tokens": 50,
            "output_tokens": 10,
            "total_tokens": 60,
            "cache_read_tokens": 10,
            "duration_ms": 200,
            "tokens_per_second": 50,
        }
    )

    summary = usage.summary(run_id="run-1", query_id="query-1", rounds=1, tool_calls=2)

    assert summary["rounds"] == 1
    assert summary["tool_calls"] == 2
    assert summary["steps"] == 3
    assert summary["input_tokens"] == 150
    assert summary["output_tokens"] == 30
    assert summary["reasoning_tokens"] == 4
    assert summary["cache_hit_rate"] == 60.0
    assert summary["last_model_duration_ms"] == 500
    assert summary["last_model_tokens_per_second"] == 40.0
    assert summary["partial"] is False


def test_run_usage_deduplicates_calls_and_marks_missing_provider_usage_partial():
    usage = RunUsageAccumulator()
    event = {"call_id": "call-1", "role": "agent", "measured": False}

    assert usage.add_model_event(event) is True
    assert usage.add_model_event(event) is False

    summary = usage.summary(run_id="run-1", query_id="query-1", tool_calls=0)
    assert summary["rounds"] == 1
    assert summary["measured"] is False
    assert summary["partial"] is True
    assert summary["input_tokens"] == 0
    assert summary["cache_hit_rate"] is None


def test_run_usage_accepts_langchain_message_metadata_directly():
    usage = RunUsageAccumulator()

    assert usage.add_langchain_usage(
        {
            "input_tokens": 1_000,
            "output_tokens": 200,
            "total_tokens": 1_200,
            "input_token_details": {"cache_read": 750},
            "output_token_details": {"reasoning": 25},
        },
        call_id="message-1",
        role="agent",
        duration_ms=500,
    )

    summary = usage.summary(run_id="run-1", query_id="query-1")
    assert summary["input_tokens"] == 1_000
    assert summary["output_tokens"] == 200
    assert summary["cache_hit_rate"] == 75.0
    assert summary["reasoning_tokens"] == 25
    assert summary["last_model_tokens_per_second"] == 400.0
