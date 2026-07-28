"""Lossless live projections for tool results that drive frontend controls."""

from __future__ import annotations

import json

_LIVE_SKILL_PLAN_FIELDS = (
    "ok",
    "plan_id",
    "plan_sha256",
    "skill_name",
    "action",
    "status",
    "phase",
    "requires_confirmation",
    "ui_commit_supported",
    "source",
    "expires_at",
)

_LIVE_AUTHORIZATION_FIELDS = (
    "type",
    "flow_id",
    "revision",
    "attempt",
    "provider",
    "profile_id",
    "status",
    "phase",
    "verification_url",
    "user_code",
    "qr_ascii",
    "expires_at",
    "completion_hint",
)


def project_live_tool_output(
    *,
    tool_name: str,
    raw_output: str,
    fallback_output: str,
) -> str:
    """Preserve control-plane JSON while bounding ordinary live output.

    Skill Manager plans are frontend control data, not an optional command
    preview. Truncating their JSON makes a successful prepare indistinguishable
    from no plan at all, so emit a compact, structurally complete envelope.
    Durable session storage continues to retain the full tool result.
    """

    if tool_name != "execute":
        return fallback_output
    try:
        value = json.loads(raw_output)
    except (TypeError, ValueError):
        return fallback_output
    if (
        isinstance(value, dict)
        and value.get("managed_by") == "managed_cli"
        and value.get("status") == "awaiting_user_browser"
        and isinstance(value.get("authorization_request"), dict)
        and value["authorization_request"].get("type") == "managed_authorization_request"
    ):
        request = value["authorization_request"]
        projected_request = {
            key: request[key]
            for key in _LIVE_AUTHORIZATION_FIELDS
            if key in request
        }
        return json.dumps(
            {
                "ok": True,
                "managed_by": "managed_cli",
                "event_kind": "managed_authorization_request",
                "status": "awaiting_user_browser",
                "authorization_completed": False,
                "authorization_request": projected_request,
                "output": value.get("output"),
                "next_action": value.get("next_action"),
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )
    if not (
        isinstance(value, dict)
        and value.get("managed_by") == "skill_management"
        and value.get("intercepted") is True
        and isinstance(value.get("plans"), list)
        and value["plans"]
    ):
        return fallback_output
    plans = [
        {key: plan[key] for key in _LIVE_SKILL_PLAN_FIELDS if key in plan}
        for plan in value["plans"]
        if isinstance(plan, dict)
    ]
    return json.dumps(
        {
            "ok": bool(value.get("ok")),
            "managed_by": "skill_management",
            "intercepted": True,
            "event_kind": "skill_plan_batch_confirmation",
            "confirmation_required": True,
            "source": value.get("source"),
            "prepared_count": len(plans),
            "error_count": len(value.get("errors") or []),
            "plans": plans,
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )
