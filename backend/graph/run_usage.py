"""In-memory model usage aggregation for one Agent Run.

Provider usage metadata is the authority for token counts.  This module does
not estimate tokens and deliberately has no database dependency; the resulting
summary is carried by SSE and persisted with the assistant message JSON.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any


TOKEN_FIELDS = (
    "input_tokens",
    "output_tokens",
    "total_tokens",
    "cache_read_tokens",
    "cache_creation_tokens",
    "reasoning_tokens",
)


def _non_negative_int(value: Any) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def _usage_detail(usage: dict[str, Any], section: str, key: str) -> int:
    details = usage.get(section)
    return _non_negative_int(details.get(key)) if isinstance(details, dict) else 0


@dataclass
class RunUsageAccumulator:
    """Aggregate deduplicated model completion events for a single Run."""

    started_at: float = field(default_factory=time.perf_counter)
    _call_ids: set[str] = field(default_factory=set)
    _totals: dict[str, int] = field(
        default_factory=lambda: {field_name: 0 for field_name in TOKEN_FIELDS}
    )
    _observed_calls: int = 0
    _measured_calls: int = 0
    _agent_calls: int = 0
    _measured_agent_calls: int = 0
    _last_agent_event: dict[str, Any] | None = None

    def add_model_event(self, payload: dict[str, Any]) -> bool:
        """Add one provider call event, returning False for duplicates."""

        call_id = str(payload.get("call_id") or "").strip()
        if not call_id or call_id in self._call_ids:
            return False
        self._call_ids.add(call_id)
        self._observed_calls += 1

        measured = bool(payload.get("measured"))
        if measured:
            self._measured_calls += 1
            for field_name in TOKEN_FIELDS:
                self._totals[field_name] += _non_negative_int(payload.get(field_name))

        if str(payload.get("role") or "") == "agent":
            self._agent_calls += 1
            if measured:
                self._measured_agent_calls += 1
            self._last_agent_event = dict(payload)
        return True

    def add_langchain_usage(
        self,
        usage: dict[str, Any],
        *,
        call_id: str,
        role: str,
        duration_ms: int,
    ) -> bool:
        """Add usage metadata observed on LangGraph's authoritative message stream."""

        input_tokens = _non_negative_int(usage.get("input_tokens"))
        output_tokens = _non_negative_int(usage.get("output_tokens"))
        total_tokens = _non_negative_int(usage.get("total_tokens")) or (
            input_tokens + output_tokens
        )
        duration_ms = _non_negative_int(duration_ms)
        return self.add_model_event(
            {
                "call_id": call_id,
                "role": role,
                "measured": bool(usage) and bool(input_tokens or output_tokens or total_tokens),
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "total_tokens": total_tokens,
                "cache_read_tokens": _usage_detail(
                    usage, "input_token_details", "cache_read"
                ),
                "cache_creation_tokens": _usage_detail(
                    usage, "input_token_details", "cache_creation"
                ),
                "reasoning_tokens": _usage_detail(
                    usage, "output_token_details", "reasoning"
                ),
                "duration_ms": duration_ms,
                "tokens_per_second": (
                    round(output_tokens / (duration_ms / 1000), 1)
                    if output_tokens > 0 and duration_ms > 0
                    else None
                ),
            }
        )

    def summary(
        self,
        *,
        run_id: str,
        query_id: str,
        rounds: int | None = None,
        tool_calls: int = 0,
    ) -> dict[str, Any]:
        """Build the transport/persistence shape at the current instant."""

        resolved_rounds = max(0, int(rounds if rounds is not None else self._agent_calls))
        resolved_tools = max(0, int(tool_calls))
        result: dict[str, Any] = {
            "run_id": run_id,
            "query_id": query_id,
            "rounds": resolved_rounds,
            "tool_calls": resolved_tools,
            "steps": resolved_rounds + resolved_tools,
            "run_duration_ms": max(0, round((time.perf_counter() - self.started_at) * 1000)),
            **self._totals,
            "observed_calls": self._observed_calls,
            "measured_calls": self._measured_calls,
            "measured": self._measured_calls > 0,
            "partial": self._observed_calls > self._measured_calls,
        }
        input_tokens = self._totals["input_tokens"]
        cache_read_tokens = self._totals["cache_read_tokens"]
        result["cache_hit_rate"] = (
            round(cache_read_tokens / input_tokens * 100, 1) if input_tokens > 0 else None
        )

        if self._last_agent_event is not None:
            result["last_model_duration_ms"] = _non_negative_int(
                self._last_agent_event.get("duration_ms")
            )
            tokens_per_second = self._last_agent_event.get("tokens_per_second")
            if isinstance(tokens_per_second, (int, float)) and tokens_per_second >= 0:
                result["last_model_tokens_per_second"] = round(float(tokens_per_second), 1)
        return result
