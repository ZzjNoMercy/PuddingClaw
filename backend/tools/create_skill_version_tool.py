"""CreateSkillVersionTool — create version snapshots for skills."""

import re
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Type

from langchain_core.tools import BaseTool
from pydantic import BaseModel, Field


class CreateSkillVersionInput(BaseModel):
    skill_name: str = Field(
        description="Name of the skill to create a version for (e.g., 'skill-creator')"
    )
    version_label: str = Field(
        description="Version label (e.g., 'v1.1', 'v2.0', '2024-03-17-fix')"
    )


class CreateSkillVersionTool(BaseTool):
    name: str = "create_skill_version"
    description: str = (
        "Create a version snapshot for a skill. This copies all files in the skill directory "
        "to a versions/{label}/ subdirectory for version tracking. "
        "Version labels can only contain letters, numbers, dots, and hyphens. "
        "Example: create_skill_version('skill-creator', 'v1.1')"
    )
    args_schema: Type[BaseModel] = CreateSkillVersionInput
    risk_level: str = "moderate"
    root_dir: str = ""

    def _run(self, skill_name: str, version_label: str) -> str:
        try:
            root = Path(self.root_dir)

            # Validate skill name (alphanumeric, hyphens, underscores only)
            if not re.match(r"^[a-zA-Z0-9_-]+$", skill_name):
                return f"❌ Invalid skill name: {skill_name}"

            skill_dir = root / "skills" / skill_name

            if not skill_dir.is_dir():
                return f"❌ Skill not found: {skill_name}"

            # Validate version label (alphanumeric, dots, hyphens only)
            if not re.match(r"^[a-zA-Z0-9.\-]+$", version_label):
                return f"❌ Invalid version label: {version_label} (only letters, numbers, dots, hyphens allowed)"

            # Check existing version count (limit to 10 versions)
            versions_base = skill_dir / "versions"
            if versions_base.exists():
                existing_versions = [d.name for d in versions_base.iterdir() if d.is_dir()]
                if len(existing_versions) >= 10:
                    return (
                        f"⚠️ Too many versions ({len(existing_versions)}). "
                        f"Consider deleting old versions first: {', '.join(sorted(existing_versions)[:3])}..."
                    )

            versions_dir = skill_dir / "versions" / version_label

            if versions_dir.exists():
                return f"❌ Version already exists: {version_label}"

            # Create versions directory
            versions_dir.mkdir(parents=True, exist_ok=True)

            # Copy all files and directories (excluding versions/ itself)
            file_count = 0
            for item in skill_dir.iterdir():
                if item.name == "versions":
                    continue

                if item.is_file():
                    shutil.copy2(item, versions_dir / item.name)
                    file_count += 1
                elif item.is_dir():
                    shutil.copytree(item, versions_dir / item.name)
                    # Count files in subdirectory
                    file_count += sum(1 for _ in (versions_dir / item.name).rglob("*") if _.is_file())

            timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

            return (
                f"✅ Version created: {version_label}\n"
                f"📁 Skill: {skill_name}\n"
                f"📄 Files: {file_count}\n"
                f"🕐 Time: {timestamp}"
            )

        except Exception as e:
            return f"❌ Error creating version: {str(e)}"


def create_skill_version_tool(base_dir: Path) -> CreateSkillVersionTool:
    return CreateSkillVersionTool(root_dir=str(base_dir))
