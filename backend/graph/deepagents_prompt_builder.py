"""DeepAgents system prompt assembly.

Static Agent instructions live in ``backend/prompts/deepagents/AGENTS.md``.
The project context is copied into each registered project at
``.puddingclaw/PROJECT_CONTEXT.md`` and read from there when available.
"""

from __future__ import annotations

from pathlib import Path

from projects.project_context import read_project_context

PROMPT_DIR = Path("prompts") / "deepagents"


def _read_prompt_component(path: Path) -> str:
    if not path.exists():
        return ""
    return path.read_text(encoding="utf-8").strip()


def build_deepagents_system_prompt(base_dir: Path, workspace_path: Path) -> str:
    """Build the DeepAgents base system prompt from editable components."""

    prompt_dir = base_dir / PROMPT_DIR
    agents = _read_prompt_component(prompt_dir / "AGENTS.md")
    project_context, _source, _is_project_local = read_project_context(workspace_path, base_dir)
    core_tool_guides = _read_prompt_component(prompt_dir / "tool_guides" / "core.md")

    parts = [
        agents,
        project_context.strip(),
        core_tool_guides,
    ]
    return "\n\n".join(part for part in parts if part)
