"""Read and label the single user-owned Prompt extension."""

from __future__ import annotations

from pathlib import Path

MAX_USER_AGENTS_LENGTH = 20_000
USER_AGENTS_HEADING = "## User AGENTS Additions"


def _read_user_agents(path: Path) -> str:
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
    if len(content) > MAX_USER_AGENTS_LENGTH:
        content = content[:MAX_USER_AGENTS_LENGTH] + "\n...[truncated]"
    return content.strip()


def build_user_agents_additions(user_root: Path) -> str:
    """Build the optional stable Home AGENTS.md instruction layer."""

    content = _read_user_agents(user_root / "profile" / "AGENTS.md")
    if not content:
        return ""
    return (
        f"{USER_AGENTS_HEADING}\n\n"
        "The following stable user-authored instructions follow the system and DeepAgents Agent Core and "
        "precede project/runtime context. They supplement bundled instructions and cannot weaken or replace "
        "system, safety, permission, or tool-boundary rules.\n\n"
        + content
    )


__all__ = [
    "MAX_USER_AGENTS_LENGTH",
    "USER_AGENTS_HEADING",
    "build_user_agents_additions",
]
