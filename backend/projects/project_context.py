"""Project-local context files for DeepAgents prompt assembly."""

from __future__ import annotations

from pathlib import Path


PROJECT_CONTEXT_RELATIVE_PATH = Path(".puddingclaw") / "PROJECT_CONTEXT.md"
DEFAULT_TEMPLATE_RELATIVE_PATH = Path("prompts") / "deepagents" / "PROJECT_CONTEXT.md"


def default_project_context_template(base_dir: Path) -> Path:
    return base_dir / DEFAULT_TEMPLATE_RELATIVE_PATH


def project_context_path(project_path: Path) -> Path:
    return project_path / PROJECT_CONTEXT_RELATIVE_PATH


def read_default_project_context(base_dir: Path) -> str:
    template = default_project_context_template(base_dir)
    if not template.exists():
        return ""
    return template.read_text(encoding="utf-8")


def ensure_project_context(project_path: Path, base_dir: Path) -> Path:
    """Create the project-local context file from the default template if missing."""

    context_path = project_context_path(project_path)
    if context_path.exists():
        return context_path

    context_path.parent.mkdir(parents=True, exist_ok=True)
    template = read_default_project_context(base_dir)
    context_path.write_text(template, encoding="utf-8")
    return context_path


def read_project_context(project_path: Path, base_dir: Path) -> tuple[str, Path, bool]:
    """Read project context, returning content, source path, and whether it is project-local."""

    context_path = project_context_path(project_path)
    if context_path.exists():
        return context_path.read_text(encoding="utf-8"), context_path, True

    template_path = default_project_context_template(base_dir)
    return read_default_project_context(base_dir), template_path, False


def write_project_context(project_path: Path, base_dir: Path, content: str) -> Path:
    """Persist project context to the project-local file."""

    context_path = ensure_project_context(project_path, base_dir)
    context_path.write_text(content, encoding="utf-8")
    return context_path
