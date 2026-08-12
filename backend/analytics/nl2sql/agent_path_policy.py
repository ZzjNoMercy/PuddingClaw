"""Policy and audit helpers for the Agent-led database path.

Fallback is a reliability mechanism, not a way around a deterministic
security or business-fact rejection.  This module keeps that distinction in
one place so tools and routers cannot gradually grow different interpretations.
"""

from __future__ import annotations

import time
import uuid
from typing import Any

from graph.session_manager import session_manager

_INFRASTRUCTURE_FALLBACK_CODES = frozenset(
    {
        "evidence_search_timeout",
        "database_unavailable",
        "agent_protocol_unavailable",
    }
)


def unconsumed_legacy_fallback_offer(
    *,
    session_id: str,
    query_id: str,
    run_id: str,
    goal_id: str,
    goal_revision: int | None,
) -> dict[str, Any] | None:
    """Return the latest same-scope infrastructure fallback offer, if any."""

    if not session_id or not getattr(session_manager, "is_initialized", False) or not (query_id or run_id or goal_id):
        return None
    try:
        events = session_manager.list_database_path_events(
            session_id,
            query_id=query_id,
            run_id=run_id,
            goal_id=goal_id,
            goal_revision=goal_revision,
        )
    except (FileNotFoundError, RuntimeError, ValueError):
        return None
    used_offer_ids = {
        str(item.get("offer_event_id") or "")
        for item in events
        if item.get("event_type") == "fallback_used"
    }
    return next(
        (
            item
            for item in reversed(events)
            if item.get("event_type") == "fallback_offered"
            and item.get("target_path") == "legacy_generation"
            and str(item.get("event_id") or "") not in used_offer_ids
        ),
        None,
    )


def classify_evidence_exception(exc: BaseException) -> str:
    """Map only transport/dependency failures to fallback-eligible codes."""

    if isinstance(exc, TimeoutError):
        return "evidence_search_timeout"
    if isinstance(exc, PermissionError):
        return "permission_denied"
    if isinstance(exc, (ConnectionError, OSError, ImportError)):
        return "database_unavailable"
    # Validation, missing semantic context, malformed requests and unknown
    # programming errors remain non-fallback errors by design.
    return "evidence_search_failed"


def fallback_policy(error_code: str, *, config: dict[str, Any] | None = None) -> dict[str, Any]:
    """Reject legacy SQL fallback for every error class.

    The Agent-led database path is the only supported path. Infrastructure
    failures are reported to the caller and never switch execution engines.
    ``config`` remains accepted only so older callers fail closed.
    """

    del config
    normalized_code = str(error_code or "").strip()
    infrastructure = normalized_code in _INFRASTRUCTURE_FALLBACK_CODES
    return {
        "eligible": False,
        "infrastructure_error": infrastructure,
        "enabled": False,
        "error_code": normalized_code,
        "target_path": "",
        "blocked_reason": (
            "business_or_security_boundary"
            if not infrastructure
            else "fallback_removed"
        ),
    }


def record_database_path_event(
    *,
    session_id: str,
    query_id: str,
    run_id: str,
    goal_id: str,
    goal_revision: int | None,
    event_type: str,
    error_code: str,
    source_path: str = "agent",
    target_path: str = "legacy_generation",
    evidence_search_id: str = "",
    sql_submission_id: str = "",
    offer_event_id: str = "",
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Write an append-only path transition/offer event when storage exists."""

    event = {
        "event_id": f"database-path-{uuid.uuid4().hex[:16]}",
        "event_type": str(event_type or "unknown"),
        "session_id": str(session_id or ""),
        "query_id": str(query_id or ""),
        "run_id": str(run_id or ""),
        "goal_id": str(goal_id or ""),
        "goal_revision": goal_revision,
        "source_path": str(source_path or ""),
        "target_path": str(target_path or ""),
        "error_code": str(error_code or ""),
        "evidence_search_id": str(evidence_search_id or ""),
        "sql_submission_id": str(sql_submission_id or ""),
        "offer_event_id": str(offer_event_id or ""),
        "created_at": time.time(),
        "metadata": dict(metadata or {}),
    }
    if session_id:
        try:
            if session_manager.is_initialized:
                session_manager.record_database_path_event(session_id, event["event_id"], event)
        except (FileNotFoundError, RuntimeError, ValueError):
            # A tool invoked before Session bootstrap must still return its
            # structured error.  It cannot claim durable audit in that mode.
            pass
    return event


def record_legacy_fallback_used_if_offered(
    *,
    session_id: str,
    query_id: str,
    run_id: str,
    goal_id: str,
    goal_revision: int | None,
    generation_id: str = "",
) -> dict[str, Any] | None:
    """Close the latest same-scope fallback offer when legacy generation runs."""

    offer = unconsumed_legacy_fallback_offer(
        session_id=session_id,
        query_id=query_id,
        run_id=run_id,
        goal_id=goal_id,
        goal_revision=goal_revision,
    )
    if not offer:
        return None
    return record_database_path_event(
        session_id=session_id,
        query_id=query_id,
        run_id=run_id,
        goal_id=goal_id,
        goal_revision=goal_revision,
        event_type="fallback_used",
        error_code=str(offer.get("error_code") or ""),
        source_path="agent",
        target_path="legacy_generation",
        evidence_search_id=str(offer.get("evidence_search_id") or ""),
        sql_submission_id=str(offer.get("sql_submission_id") or ""),
        offer_event_id=str(offer.get("event_id") or ""),
        metadata={"generation_id": str(generation_id or "")},
    )
