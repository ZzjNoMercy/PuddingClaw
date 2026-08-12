"""DeepAgents system prompt assembly.

Static Agent instructions live in ``backend/prompts/AGENTS.md``.
Trusted project instructions are read from the project-root ``AGENTS.md``.
"""

from __future__ import annotations

from pathlib import Path

from projects.project_agents import read_project_agents
from projects.registry import project_registry

PROMPT_DIR = Path("prompts")


def _read_prompt_component(path: Path) -> str:
    if not path.exists():
        return ""
    return path.read_text(encoding="utf-8").strip()


def _is_matching_trusted_project(workspace_path: Path, project_id: str | None) -> bool:
    if not project_id or not project_registry.is_trusted(project_id):
        return False
    try:
        return project_registry.resolve(project_id) == workspace_path.expanduser().resolve()
    except (KeyError, FileNotFoundError, NotADirectoryError, OSError):
        return False


def build_deepagents_system_prompt(
    base_dir: Path,
    workspace_path: Path,
    *,
    project_id: str | None = None,
) -> str:
    """Build bundled and project prompt layers; runtime additions use middleware."""

    prompt_dir = base_dir / PROMPT_DIR
    soul = _read_prompt_component(prompt_dir / "SOUL.md")
    identity = _read_prompt_component(prompt_dir / "IDENTITY.md")
    agents = _read_prompt_component(prompt_dir / "AGENTS.md")
    trusted = _is_matching_trusted_project(workspace_path, project_id)
    project_agents, _source, _is_project_local = (
        read_project_agents(workspace_path) if trusted else ("", Path(), False)
    )
    core_tool_guides = _read_prompt_component(prompt_dir / "tool_guides" / "core.md")

    parts = []
    stable_core = "\n\n".join(
        part for part in (soul, identity, agents, core_tool_guides) if part
    )
    if stable_core:
        parts.append(f"## Stable Core\n\n{stable_core}")
    if project_agents.strip():
        parts.append(f"## Project AGENTS\n\n{project_agents.strip()}")
    return "\n\n".join(part for part in parts if part)
