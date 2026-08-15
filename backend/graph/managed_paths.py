"""PuddingClaw-managed filesystem path helpers.

These paths are not arbitrary external files. They are explicitly configured or
created by PuddingClaw, so Agent tools may read them without asking for an
external-file permission grant.
"""

from __future__ import annotations

from pathlib import Path

from graph.attachment_store import attachment_store
from knowledge.paths import get_knowledge_root


def is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False


def is_managed_resource_path(path: Path, base_dir: Path) -> bool:
    try:
        resolved = path.expanduser().resolve()
    except (OSError, RuntimeError, ValueError):
        # Paths originate in user/model text. Malformed or overlong tokens are
        # ordinary non-managed inputs, never a reason to abort the Agent turn.
        return False
    roots: list[Path] = []

    try:
        roots.append(get_knowledge_root(base_dir).resolve())
    except Exception:
        pass

    if attachment_store.root_dir is not None:
        try:
            roots.append(attachment_store.root_dir.resolve())
        except Exception:
            pass

    return any(is_relative_to(resolved, root) for root in roots)
