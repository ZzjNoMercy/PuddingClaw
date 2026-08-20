"""Runtime extension gates shared by API, workers, and Agent tools."""

from __future__ import annotations

import json
import os
from collections.abc import Mapping


EXTENSION_NAMES = ("knowledge", "analytics", "headless_worker")

EXTENSION_DISPLAY_NAMES = {
    "knowledge": "知识库",
    "analytics": "智能问数",
    "headless_worker": "Agent Worker",
}


def _parse_bool(value: object) -> bool | None:
    if isinstance(value, bool):
        return value
    if not isinstance(value, str):
        return None
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    return None


def extension_states(environ: Mapping[str, str] | None = None) -> dict[str, bool]:
    """Return effective extension states.

    A CLI-managed runtime always supplies explicit values.  When no extension
    contract is present we retain the historical all-enabled source checkout
    behaviour so local development is not changed by the deployment CLI.
    """

    env = os.environ if environ is None else environ
    declared: dict[str, object] = {}
    raw = env.get("PUDDINGCLAW_EXTENSIONS")
    if raw:
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, dict):
                declared = parsed
        except (TypeError, ValueError):
            declared = {}

    states: dict[str, bool] = {}
    for name in EXTENSION_NAMES:
        explicit = _parse_bool(env.get(f"PUDDINGCLAW_EXTENSION_{name.upper()}"))
        nested = _parse_bool(declared.get(name))
        states[name] = explicit if explicit is not None else nested if nested is not None else True
    return states


def extension_enabled(name: str, environ: Mapping[str, str] | None = None) -> bool:
    if name not in EXTENSION_NAMES:
        raise ValueError(f"unknown PuddingClaw extension: {name}")
    return extension_states(environ)[name]


def extension_disabled_payload(name: str) -> dict[str, str]:
    """Return the stable, actionable API contract for a disabled extension."""

    if name not in EXTENSION_NAMES:
        raise ValueError(f"unknown PuddingClaw extension: {name}")
    display_name = EXTENSION_DISPLAY_NAMES[name]
    return {
        "code": "extension_disabled",
        # Compatibility for clients that adopted the initial 0.1.2 draft.
        "error_code": "extension_disabled",
        "extension": name,
        "message": f"{display_name}功能尚未启用，请运行 puddingclaw init 进行配置",
    }


def runtime_profile(environ: Mapping[str, str] | None = None) -> dict[str, object]:
    env = os.environ if environ is None else environ
    states = extension_states(env)
    active = [name for name, enabled in states.items() if enabled]
    inferred = (
        "harness"
        if not active
        else "full"
        if len(active) == len(EXTENSION_NAMES)
        else "custom"
    )
    return {
        "schema_version": 1,
        "profile": env.get("PUDDINGCLAW_PROFILE") or inferred,
        "extensions": states,
    }


def disabled_extension_for_api_path(
    path: str,
    environ: Mapping[str, str] | None = None,
) -> str | None:
    states = extension_states(environ)
    prefixes = {
        "knowledge": ("/api/knowledge", "/api/read-later"),
        "analytics": ("/api/analytics",),
        "headless_worker": ("/api/headless", "/api/headless-activity-"),
    }
    for name, candidates in prefixes.items():
        if not states[name] and any(
            path == prefix or path.startswith(f"{prefix}/") or (prefix.endswith("-") and path.startswith(prefix))
            for prefix in candidates
        ):
            return name
    return None
