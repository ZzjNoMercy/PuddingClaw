"""Evaluation-scoped evidence collection; not a second durable trace system."""

from __future__ import annotations

from typing import Any

from langchain_core.callbacks.base import BaseCallbackHandler

from .contracts import AgentRunEnvelope, ToolCallEvidence, TraceEvidence
from .langsmith_backend import _redact


class EnvelopeEvidenceProvider:
    """Reliable minimum: final output plus tool name/order/status."""

    def collect(self, run: AgentRunEnvelope) -> TraceEvidence:
        return TraceEvidence(
            provider="envelope",
            run_id=run.run_id,
            available_kinds={"final_output", "tool_name", "tool_order", "tool_status"},
            tool_calls=run.tool_calls,
            trajectory=[call.name for call in run.tool_calls],
            metadata={"agent_error": run.error is not None},
        )


class EvaluationEvidenceCallback(BaseCallbackHandler):
    """Capture structured tool evidence inside the isolated evaluation process."""

    def __init__(self, *, max_total_chars: int = 200_000) -> None:
        self.max_total_chars = max_total_chars
        self._calls: dict[str, ToolCallEvidence] = {}
        self._order: list[str] = []
        self._used_chars = 0
        self.complete = True

    def _bounded(self, value: Any) -> Any:
        redacted = _redact(value, max_string=16_000)
        rendered = str(redacted)
        if self._used_chars + len(rendered) > self.max_total_chars:
            self.complete = False
            return {"_capture": "omitted_total_size_limit"}
        self._used_chars += len(rendered)
        return redacted

    def on_tool_start(
        self,
        serialized: dict[str, Any],
        input_str: str,
        *,
        run_id: Any,
        parent_run_id: Any | None = None,
        tags: list[str] | None = None,
        metadata: dict[str, Any] | None = None,
        inputs: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> Any:
        del parent_run_id, tags, metadata, kwargs
        key = str(run_id)
        name = str(serialized.get("name") or "unknown_tool")
        arguments = inputs if isinstance(inputs, dict) else {"input": input_str, "_structured": False}
        if inputs is None:
            self.complete = False
        call = ToolCallEvidence(
            name=name,
            arguments=self._bounded(arguments),
            sequence=len(self._order),
        )
        self._calls[key] = call
        self._order.append(key)

    def on_tool_end(self, output: Any, *, run_id: Any, **kwargs: Any) -> Any:
        del kwargs
        key = str(run_id)
        call = self._calls.get(key)
        if call is not None:
            self._calls[key] = call.model_copy(
                update={"output_summary": str(self._bounded(output))[:16_000], "succeeded": True}
            )

    def on_tool_error(self, error: BaseException, *, run_id: Any, **kwargs: Any) -> Any:
        del kwargs
        key = str(run_id)
        call = self._calls.get(key)
        if call is not None:
            self._calls[key] = call.model_copy(update={"output_summary": str(error)[:2_000], "succeeded": False})

    def evidence(self, *, run_id: str | None = None) -> TraceEvidence:
        calls = [self._calls[key] for key in self._order if key in self._calls]
        kinds = {"tool_name", "tool_order", "tool_status", "tool_arguments", "tool_output"}
        if not self.complete:
            kinds.discard("tool_arguments")
            kinds.discard("tool_output")
        return TraceEvidence(
            provider="envelope",
            run_id=run_id,
            available_kinds=kinds,
            tool_calls=calls,
            trajectory=[call.name for call in calls],
            metadata={"capture_complete": self.complete},
        )
