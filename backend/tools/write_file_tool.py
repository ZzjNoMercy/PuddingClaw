"""WriteFileTool — sandboxed file writing within allowed directories."""

from pathlib import Path
from typing import Type

from langchain_core.tools import BaseTool
from pydantic import BaseModel, Field

from runtime_identity.paths import PuddingClawPaths
from utils.file_cache import file_cache


class WriteFileInput(BaseModel):
    file_path: str = Field(
        description="Relative path of the file to write (relative to project root)"
    )
    content: str = Field(
        description="Full content to write to the file"
    )


class SandboxedWriteFileTool(BaseTool):
    name: str = "write_file"
    description: str = (
        "Write content to a local file. Path is relative to the project root. "
        "Only files in skills/, sql-guardrails/, workspace/, and memory/ directories can be modified. "
        "Semantic assets and analytics models are read-only here; use the Semantic Steward tools. "
        "Use this to update SKILL.md files, memory files, or workspace documents. "
        "Example: write_file('skills/skill-creator/SKILL.md', '---\\nname: skill-creator\\n...')"
    )
    args_schema: Type[BaseModel] = WriteFileInput
    risk_level: str = "moderate"
    root_dir: str = ""

    def _run(self, file_path: str, content: str) -> str:
        try:
            # File size limit check (100KB)
            MAX_FILE_SIZE = 100000
            if len(content) > MAX_FILE_SIZE:
                return f"❌ Content too large: {len(content)} characters (max {MAX_FILE_SIZE})"

            root = Path(self.root_dir).expanduser().resolve()
            # Normalize path
            normalized = file_path.replace("\\", "/").lstrip("./")

            # Whitelist check: only allow curated project authoring roots.
            ALLOWED_PREFIXES = [
                "skills/",
                "sql-guardrails/",
                "workspace/",
                "memory/",
            ]
            if not any(normalized.startswith(prefix) for prefix in ALLOWED_PREFIXES):
                return (
                    f"❌ Access denied: {file_path} "
                    "(only skills/, sql-guardrails/, workspace/, memory/ allowed; use Semantic Steward for definitions)"
                )

            user = PuddingClawPaths.from_environment()
            roots = {
                "skills/": user.user_skills(),
                "sql-guardrails/": user.user_definitions() / "sql-guardrails",
                "workspace/": user.agent_workspaces() / "unscoped" / "default",
                "memory/": user.memory(),
            }
            target_root = next((target for prefix, target in roots.items() if normalized.startswith(prefix)), None)
            if target_root is None:
                return f"❌ Access denied: {file_path}"
            full_path = (target_root / normalized.split("/", 1)[1]).resolve()

            # Sandbox check: prevent path traversal
            if not full_path.is_relative_to(target_root.resolve()):
                return f"❌ Access denied: path escapes project root"

            # Create parent directories if needed
            full_path.parent.mkdir(parents=True, exist_ok=True)

            # Write file with UTF-8 encoding
            full_path.write_text(content, encoding="utf-8")
            file_cache.put(full_path, content)
            return f"✅ File saved: {file_path} ({len(content)} characters)"

        except Exception as e:
            return f"❌ Error writing file: {str(e)}"


def create_write_file_tool(base_dir: Path) -> SandboxedWriteFileTool:
    return SandboxedWriteFileTool(root_dir=str(PuddingClawPaths.from_environment().root))
