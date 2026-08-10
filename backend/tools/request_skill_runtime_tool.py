"""Explicitly select the non-default Docker runtime for an installed Skill."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Literal

from langchain_core.tools import BaseTool
from pydantic import BaseModel, ConfigDict, Field

from runtime_identity.paths import PuddingClawPaths
from runtime_identity.skill_runtimes import SkillRuntimeBindingStore
from runtime_identity.software_runtime import skill_content_version


class RequestSkillRuntimeArgs(BaseModel):
    skill_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
    runtime: Literal["docker", "host"]
    reason: str = Field(min_length=1, max_length=300)


class RequestSkillRuntimeTool(BaseTool):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    name: str = "request_skill_runtime"
    description: str = (
        "Explicitly bind an installed Skill to Docker for Linux, Chromium, system-library, "
        "native-ABI, or stronger-isolation requirements, or back to the default host Seatbelt runtime. "
        "This is never an automatic fallback."
    )
    args_schema: type[BaseModel] = RequestSkillRuntimeArgs
    risk_level: str = "high"
    skills_dir: str = Field(exclude=True)
    paths: PuddingClawPaths = Field(exclude=True)

    def _run(self, skill_id: str, runtime: str, reason: str) -> str:
        del reason
        skills = Path(self.skills_dir).resolve(strict=True)
        root = (skills / skill_id).resolve(strict=True)
        root.relative_to(skills)
        if root.is_symlink() or not (root / "SKILL.md").is_file():
            raise ValueError("Skill is not installed")
        version = skill_content_version(root)
        revision = SkillRuntimeBindingStore(self.paths).bind(
            skill_id=skill_id,
            skill_version=version,
            runtime=runtime,
        )
        return json.dumps(
            {"skill_id": skill_id, "skill_version": version, "runtime": runtime, "revision": revision},
            sort_keys=True,
        )

    async def _arun(self, **kwargs: Any) -> str:
        return self._run(**kwargs)


def create_request_skill_runtime_tool(*, skills_dir: Path, paths: PuddingClawPaths) -> RequestSkillRuntimeTool:
    return RequestSkillRuntimeTool(skills_dir=str(skills_dir), paths=paths)
