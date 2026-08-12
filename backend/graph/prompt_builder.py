"""Compatibility prompt builder for deprecated non-Agent entry points.

The active PuddingClaw Harness is assembled by ``deepagents_prompt_builder``
and runtime middleware. Legacy callers still use this module for prompt
preview and deprecated Chat execution, so keep them on the same bundled
``SOUL.md`` -> ``IDENTITY.md`` -> ``AGENTS.md`` order plus final Home
``profile/AGENTS.md`` layering model. ``USER.md`` is intentionally unsupported.
"""

from pathlib import Path

from graph.user_agents import build_user_agents_additions


MAX_COMPONENT_LENGTH = 20_000


def _read_component(path: Path) -> str:
    """Read one bounded text component while tolerating legacy encodings."""

    if not path.exists():
        return ""
    raw = path.read_bytes()
    for encoding in ("utf-8", "gbk", "latin-1"):
        try:
            content = raw.decode(encoding)
            break
        except (UnicodeDecodeError, ValueError):
            continue
    else:
        content = raw.decode("utf-8", errors="replace")
    if len(content) > MAX_COMPONENT_LENGTH:
        return content[:MAX_COMPONENT_LENGTH] + "\n...[truncated]"
    return content


TOOL_REMINDER_SECTION = (
    "## 工具调用提醒\n\n"
    "需要读取、执行或写入时必须实际调用相应工具；不得仅在回复中描述操作。"
)


def build_system_prompt(
    base_dir: Path,
    *,
    runtime_root: Path,
    tool_reminder: bool = False,
) -> str:
    """Build the compatibility prompt from bundled AGENTS then user additions."""

    parts: list[str] = []
    bundled = [
        _read_component(base_dir / "prompts" / name).strip()
        for name in ("SOUL.md", "IDENTITY.md", "AGENTS.md")
    ]
    stable_core = "\n\n".join(part for part in bundled if part)
    if stable_core:
        parts.append(f"## Stable Core\n\n{stable_core}")
    if tool_reminder:
        parts.append(TOOL_REMINDER_SECTION)

    user_additions = build_user_agents_additions(runtime_root)
    if user_additions:
        parts.append(user_additions)
    return "\n\n".join(parts)


__all__ = ["MAX_COMPONENT_LENGTH", "_read_component", "build_system_prompt"]
