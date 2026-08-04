"""Standalone probe for LangGraph's experimental v3 streaming protocol.

This module intentionally does not participate in PuddingClaw's production SSE
path. It exercises the protocol against a deterministic nested graph so we can
evaluate event ordering, lifecycle visibility, final-state projection, and
early-exit cleanup before changing ``DeepAgentsAgentManager``.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import asdict, dataclass
from typing import Any, TypedDict

from langgraph.graph import END, START, StateGraph


class ExperimentState(TypedDict):
    value: str
    steps: list[str]


@dataclass(frozen=True)
class StreamV3ExperimentReport:
    final_state: ExperimentState
    protocol_event_count: int
    sequence_numbers: list[int]
    methods: list[str]
    lifecycle_events: list[dict[str, Any]]
    root_value_snapshots: list[ExperimentState]
    nested_value_snapshots: list[ExperimentState]
    interrupted: bool


def build_experiment_graph() -> Any:
    """Build a deterministic nested graph without model or network calls."""

    def prepare(state: ExperimentState) -> ExperimentState:
        return {
            "value": f"{state['value']}-prepared",
            "steps": [*state["steps"], "prepare"],
        }

    def nested_step(state: ExperimentState) -> ExperimentState:
        return {
            "value": f"{state['value']}-nested",
            "steps": [*state["steps"], "nested"],
        }

    def finish(state: ExperimentState) -> ExperimentState:
        return {
            "value": f"{state['value']}-finished",
            "steps": [*state["steps"], "finish"],
        }

    nested_graph = (
        StateGraph(ExperimentState)
        .add_node("nested_step", nested_step)
        .add_edge(START, "nested_step")
        .add_edge("nested_step", END)
        .compile()
    )
    return (
        StateGraph(ExperimentState)
        .add_node("prepare", prepare)
        .add_node("nested", nested_graph)
        .add_node("finish", finish)
        .add_edge(START, "prepare")
        .add_edge("prepare", "nested")
        .add_edge("nested", "finish")
        .add_edge("finish", END)
        .compile()
    )


async def run_stream_v3_experiment() -> StreamV3ExperimentReport:
    """Run the v3 protocol probe and return its observable contract."""

    graph = build_experiment_graph()
    run = await graph.astream_events(
        {"value": "start", "steps": []},
        version="v3",
    )
    protocol_events: list[dict[str, Any]] = []
    async with run:
        async for event in run:
            protocol_events.append(event)

    final_state = await run.output()
    if final_state is None:
        raise RuntimeError("Stream v3 experiment completed without a final state")

    root_snapshots: list[ExperimentState] = []
    nested_snapshots: list[ExperimentState] = []
    lifecycle_events: list[dict[str, Any]] = []
    for event in protocol_events:
        params = event["params"]
        if event["method"] == "values":
            target = root_snapshots if not params["namespace"] else nested_snapshots
            target.append(params["data"])
        elif event["method"] == "lifecycle":
            lifecycle_events.append(params["data"])

    return StreamV3ExperimentReport(
        final_state=final_state,
        protocol_event_count=len(protocol_events),
        sequence_numbers=[event["seq"] for event in protocol_events],
        methods=[event["method"] for event in protocol_events],
        lifecycle_events=lifecycle_events,
        root_value_snapshots=root_snapshots,
        nested_value_snapshots=nested_snapshots,
        interrupted=await run.interrupted(),
    )


async def _main() -> None:
    report = await run_stream_v3_experiment()
    print(json.dumps(asdict(report), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    asyncio.run(_main())
