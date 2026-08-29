"""Versioned online-verification event envelopes."""

from __future__ import annotations

import time
import uuid
from typing import Any

from pydantic import BaseModel, Field

VERIFICATION_EVENT_TYPES = frozenset(
    {
        "verification.snapshot.created",
        "verification.deterministic.started",
        "verification.deterministic.completed",
        "verification.environment.started",
        "verification.environment.completed",
        "verification.grader.started",
        "verification.grader.tool.completed",
        "verification.grader.completed",
        "verification.record.stale",
        "verification.revision.requested",
        "verification.settlement.started",
        "verification.settlement.committed",
        "verification.settlement.rejected",
    }
)


class VerificationEvent(BaseModel):
    schema_version: str = "1"
    event_id: str = Field(default_factory=lambda: f"verification-event-{uuid.uuid4().hex[:20]}")
    event_type: str
    timestamp: float = Field(default_factory=time.time)
    session_id: str
    query_id: str
    run_id: str
    goal_id: str | None = None
    goal_revision: int | None = None
    completion_request_id: str | None = None
    snapshot_id: str | None = None
    verification_id: str | None = None
    operation_id: str | None = None
    status: str = ""
    method: str | None = None
    model: str | None = None
    policy_version: str | None = None
    latency_ms: int | None = None
    usage: dict[str, Any] = Field(default_factory=dict)
    error_kind: str | None = None
    detail: dict[str, Any] = Field(default_factory=dict)

    def model_post_init(self, __context: Any) -> None:  # noqa: PYI063
        if self.event_type not in VERIFICATION_EVENT_TYPES:
            raise ValueError(f"Unsupported verification event type: {self.event_type}")


def emit_verification_event(runtime: Any, event: VerificationEvent) -> None:
    """Best-effort streaming; event delivery never becomes completion authority."""

    writer = getattr(runtime, "stream_writer", None)
    if writer is None:
        return
    try:
        writer({"type": event.event_type, **event.model_dump(mode="json")})
    except Exception:
        return
