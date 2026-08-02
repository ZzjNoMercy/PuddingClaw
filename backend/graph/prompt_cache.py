"""First-principles helpers for DeepAgents prompt-cache stability.

The provider sees three independently cacheable request parts: system text,
tool schemas, and messages.  This module deliberately contains no persistence
logic and no authorization decisions.  It only provides deterministic
serialization, fingerprints, and request-scoped control-message helpers.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable, Mapping
from typing import Any

from langchain_core.messages import HumanMessage

CONTROL_SOURCE = "puddingclaw_prompt_cache_control"
_SYSTEM_SECTION_HEADINGS = {
    "stable_core": ("## Stable Core", "## Agent Core"),
    "project_context": ("## Project Context", "## Project Context (stable)"),
    "versioned": ("## Versioned Analytics / Semantics", "## Analytics Model Semantic Assets"),
    "memory": ("<agent_memory>",),
    "active_runtime": (
        "## Active Skill Instructions",
        "## Activated Tool Guides",
        "## Activated Tool Guides (request-scoped)",
    ),
    "volatile_tail": (
        "## Current Capability Manifest",
        "## Current Permission Manifest",
        "## Current Run Delta",
    ),
}
_SYSTEM_SECTION_MARKERS = (
    ("stable_core", re.compile(r"(?m)^## Stable Core(?:\s*)$|^## Agent Core(?:\s*)$")),
    ("project_context", re.compile(r"(?m)^## Project Context(?: \(stable\))?(?:\s*)$")),
    (
        "versioned",
        re.compile(r"(?m)^## Versioned Analytics / Semantics(?:\s*)$|^## Analytics Model Semantic Assets(?:\s*)$"),
    ),
    ("memory", re.compile(r"(?m)^<agent_memory>|^<!-- Long-term Memory(?: \([^\n]+\))? -->")),
    (
        "active_runtime",
        re.compile(
            r"(?m)^## Active Skill Instructions(?:[^\n]*)$|^## Activated Tool Guides(?:[^\n]*)$"
        ),
    ),
    (
        "volatile_tail",
        re.compile(
            r"(?m)^## Current Capability Manifest(?:[^\n]*)$|^## Current Permission Manifest(?:[^\n]*)$|"
            r"^## Current Run Delta(?:[^\n]*)$|^## 工具调用提醒(?:[^\n]*)$"
        ),
    ),
)
_SYSTEM_SECTION_ORDER = (
    "stable_core",
    "project_context",
    "versioned",
    "memory",
    "active_runtime",
    "volatile_tail",
)


def stable_json(value: Any) -> str:
    """Serialize JSON using one canonical representation everywhere."""

    return json.dumps(
        canonicalize(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )


def canonicalize(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Mapping):
        return {str(key): canonicalize(item) for key, item in sorted(value.items(), key=lambda item: str(item[0]))}
    if isinstance(value, (list, tuple, set, frozenset)):
        items = [canonicalize(item) for item in value]
        if isinstance(value, (set, frozenset)):
            return sorted(items, key=stable_json)
        return items
    return str(value)


def digest(value: Any, *, prefix: str = "sha256:") -> str:
    return prefix + hashlib.sha256(stable_json(value).encode("utf-8")).hexdigest()


def _section_text(system_prompt: str, headings: Iterable[str]) -> str:
    """Return the stable, deterministic text belonging to known headings.

    Middleware sections are appended as blocks.  Hashing the blocks by their
    semantic class means an unrelated volatile suffix cannot invalidate the
    stable-prefix diagnostics.
    """

    text = str(system_prompt or "")
    starts = [text.find(heading) for heading in headings if text.find(heading) >= 0]
    if not starts:
        return ""
    start = min(starts)
    all_starts = [
        match.start()
        for _, marker in _SYSTEM_SECTION_MARKERS
        for match in marker.finditer(text)
        if match.start() > start
    ]
    end = min(all_starts) if all_starts else len(text)
    return text[start:end].strip()


def _system_section_buckets(system_prompt: str) -> tuple[str, dict[str, list[str]]]:
    """Split every known section occurrence, including repeated run deltas."""

    text = str(system_prompt or "")
    matches: list[tuple[int, str]] = []
    for key, marker in _SYSTEM_SECTION_MARKERS:
        matches.extend((match.start(), key) for match in marker.finditer(text))
    matches.sort(key=lambda item: item[0])
    buckets = {key: [] for key in _SYSTEM_SECTION_ORDER}
    if not matches:
        return text.strip(), buckets
    prefix = text[: matches[0][0]].strip()
    for index, (start, key) in enumerate(matches):
        end = matches[index + 1][0] if index + 1 < len(matches) else len(text)
        chunk = text[start:end].strip()
        if chunk:
            buckets[key].append(chunk)
    return prefix, buckets


def reorder_system_prompt_sections(system_prompt: str) -> str:
    """Assemble one ordered system prompt after all middleware has contributed."""

    prefix, buckets = _system_section_buckets(system_prompt)
    if not any(buckets.values()):
        return str(system_prompt or "")
    if prefix:
        buckets["stable_core"].insert(0, prefix)
    return "\n\n".join(
        chunk
        for key in _SYSTEM_SECTION_ORDER
        for chunk in buckets[key]
        if chunk
    )


def system_part_fingerprints(system_prompt: str) -> dict[str, str]:
    text = str(system_prompt or "")
    prefix, buckets = _system_section_buckets(text)
    # The untagged prefix is the most stable part of older prompts that do not
    # emit section headings.  All repeated known blocks are included in their
    # semantic bucket, so a later Current Run Delta cannot hide a change.
    stable = [prefix, *buckets["stable_core"]]
    return {
        "system_stable_hash": digest("\n\n".join(item for item in stable if item)),
        "system_project_hash": digest(buckets["project_context"]),
        "system_versioned_hash": digest(buckets["versioned"]),
        "system_memory_hash": digest(buckets["memory"]),
        "system_active_runtime_hash": digest(buckets["active_runtime"]),
        "system_volatile_tail_hash": digest(buckets["volatile_tail"]),
        "system_prompt_hash": digest(text),
    }


def tool_schema_part_fingerprints(tool_schemas: list[Any]) -> dict[str, str]:
    contracts = [canonicalize(item) for item in tool_schemas]
    stable_prefix: list[Any] = []
    dynamic: list[Any] = []
    for item in contracts:
        name = str(item.get("name") or "") if isinstance(item, dict) else ""
        if isinstance(item, dict) and item.get("_puddingclaw_dynamic") is True:
            dynamic.append(item)
        elif name.startswith(("mcp_", "mcp__")):
            dynamic.append(item)
        else:
            stable_prefix.append(item)
    return {
        "tool_stable_prefix_hash": digest(stable_prefix),
        "tool_dynamic_suffix_hash": digest(dynamic),
        "tool_full_schema_hash": digest(contracts),
        # Keep the legacy name available for existing trace consumers.
        "tool_schema_hash": digest(contracts),
    }


def messages_part_fingerprints(message_previews: list[Any]) -> dict[str, str]:
    messages = [canonicalize(item) for item in message_previews]
    history = [
        item
        for item in messages
        if not (
            isinstance(item, dict)
            and str(item.get("role") or "").lower() in {"system", "systemmessage"}
        )
        and not (
            isinstance(item, dict)
            and (
                item.get("puddingclaw_prompt_control")
                or item.get("additional_kwargs", {}).get("puddingclaw_prompt_control")
            )
        )
    ]
    volatile = []
    if messages and isinstance(messages[-1], dict):
        extra = messages[-1].get("additional_kwargs") or {}
        if extra.get("puddingclaw_prompt_control") or messages[-1].get("puddingclaw_prompt_control"):
            volatile = [messages[-1]]
            history = messages[:-1]
    return {
        "messages_history_hash": digest(history),
        "messages_volatile_tail_hash": digest(volatile),
        "messages_hash": digest(messages),
    }


def build_part_fingerprints(
    *,
    system_prompt: str,
    tool_schemas: list[Any],
    message_previews: list[Any],
) -> dict[str, str]:
    result: dict[str, str] = {}
    result.update(system_part_fingerprints(system_prompt))
    result.update(tool_schema_part_fingerprints(tool_schemas))
    result.update(messages_part_fingerprints(message_previews))
    return result


def append_control_message(messages: list[Any], *, section: str, content: str) -> list[Any]:
    """Append/replace one deterministic, non-persistent routing control tail."""

    sections: dict[str, str] = {}
    retained: list[Any] = []
    for message in messages:
        extra = getattr(message, "additional_kwargs", None) or {}
        if extra.get("puddingclaw_prompt_control"):
            raw = extra.get("puddingclaw_control_sections")
            if isinstance(raw, dict):
                sections.update({str(key): str(value) for key, value in raw.items()})
            continue
        retained.append(message)
    if content.strip():
        sections[str(section)] = str(content).strip()
    if not sections:
        return retained
    rendered = "\n\n".join(
        f"[PuddingClaw internal control: {key}]\n{sections[key]}"
        for key in sorted(sections)
    )
    retained.append(
        HumanMessage(
            content=rendered,
            name=CONTROL_SOURCE,
            additional_kwargs={
                "lc_source": CONTROL_SOURCE,
                "puddingclaw_prompt_control": True,
                "puddingclaw_control_sections": dict(sorted(sections.items())),
            },
        )
    )
    return retained


def is_prompt_control_message(message: Any) -> bool:
    extra = getattr(message, "additional_kwargs", None) or {}
    return bool(extra.get("puddingclaw_prompt_control"))


def compare_part_inputs(previous: dict[str, Any], current: dict[str, Any]) -> dict[str, Any]:
    """Compare two canonical model-input snapshots without provider assumptions."""

    previous_fp = previous.get("fingerprints") or {}
    current_fp = current.get("fingerprints") or {}
    first_diff = "none"
    first_path = ""
    for part, paths in (
        (
            "system",
            (
                "system_stable_hash",
                "system_project_hash",
                "system_versioned_hash",
                "system_memory_hash",
                "system_active_runtime_hash",
                "system_volatile_tail_hash",
                "system_prompt_hash",
            ),
        ),
        ("tools", ("tool_stable_prefix_hash", "tool_dynamic_suffix_hash", "tool_full_schema_hash", "tool_schema_hash")),
        ("messages", ("messages_history_hash", "messages_volatile_tail_hash", "messages_hash")),
    ):
        for path in paths:
            if previous_fp.get(path) != current_fp.get(path):
                first_diff, first_path = part, path
                break
        if first_diff != "none":
            break
    previous_preview = previous.get("messages_preview") or []
    current_preview = current.get("messages_preview") or []
    common = 0
    for before, after in zip(previous_preview, current_preview):
        if stable_json(before) != stable_json(after):
            break
        common += 1
    common_chars = 0
    for before, after in zip(previous_preview, current_preview):
        if stable_json(before) != stable_json(after):
            break
        common_chars += min(int(before.get("chars") or 0), int(after.get("chars") or 0)) if isinstance(before, dict) and isinstance(after, dict) else 0
    return {
        "first_diff_part": first_diff,
        "first_diff_path": first_path,
        "common_prefix_messages": common,
        "common_prefix_tokens_estimated": common_chars // 4,
    }


__all__ = [
    "CONTROL_SOURCE",
    "append_control_message",
    "build_part_fingerprints",
    "compare_part_inputs",
    "is_prompt_control_message",
    "reorder_system_prompt_sections",
    "stable_json",
]
