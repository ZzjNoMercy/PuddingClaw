from __future__ import annotations

import pytest

from scripts.experiment_langgraph_stream_v3 import build_experiment_graph, run_stream_v3_experiment


@pytest.mark.asyncio
async def test_stream_v3_experiment_surfaces_ordered_lifecycle_and_final_state() -> None:
    report = await run_stream_v3_experiment()

    assert report.final_state == {
        "value": "start-prepared-nested-finished",
        "steps": ["prepare", "nested", "finish"],
    }
    assert report.protocol_event_count == len(report.sequence_numbers)
    assert report.sequence_numbers == sorted(report.sequence_numbers)
    assert len(report.sequence_numbers) == len(set(report.sequence_numbers))
    assert report.root_value_snapshots[-1] == report.final_state
    assert report.nested_value_snapshots[-1]["steps"] == ["prepare", "nested"]
    assert [event["event"] for event in report.lifecycle_events] == ["started", "completed"]
    assert report.lifecycle_events[0]["graph_name"] == "nested"
    assert report.interrupted is False


@pytest.mark.asyncio
async def test_stream_v3_context_manager_aborts_an_early_consumer() -> None:
    graph = build_experiment_graph()
    run = await graph.astream_events(
        {"value": "start", "steps": []},
        version="v3",
    )

    async with run:
        first_event = await anext(aiter(run))
        assert first_event["method"] == "values"

    assert run._exhausted is True
