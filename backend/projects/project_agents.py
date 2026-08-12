"""Trusted project-root AGENTS.md context for Agent prompt assembly."""

from __future__ import annotations

from pathlib import Path


PROJECT_AGENTS_RELATIVE_PATH = Path("AGENTS.md")


def project_agents_path(project_path: Path) -> Path:
    root = project_path.expanduser().resolve()
    path = root / PROJECT_AGENTS_RELATIVE_PATH
    resolved = path.resolve(strict=False)
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise PermissionError("Project AGENTS.md resolves outside the trusted project") from exc
    return path


def read_project_agents(project_path: Path) -> tuple[str, Path, bool]:
    """Read a project-root AGENTS.md without creating files or using defaults."""

    path = project_agents_path(project_path)
    if not path.is_file():
        return "", path, False
    return path.read_text(encoding="utf-8"), path, True


__all__ = [
    "PROJECT_AGENTS_RELATIVE_PATH",
    "project_agents_path",
    "read_project_agents",
]
