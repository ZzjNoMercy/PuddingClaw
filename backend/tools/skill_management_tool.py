"""Typed two-phase Skill installation and update tools."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Literal

from langchain_core.tools import BaseTool
from pydantic import BaseModel, ConfigDict, Field

from services.skill_management import SkillManagementError, SkillManagementService, get_skill_management_service


class PrepareSkillInput(BaseModel):
    source: str | None = Field(
        default=None,
        description=(
            "HTTPS Skill source: a GitHub repository/directory URL, a ZIP URL, or a web directory containing "
            "SKILL.md. May be omitted for an update when the Skill was installed by skill_management."
        ),
    )
    skill_name: str | None = Field(default=None, description="Optional managed directory name")
    ref: str | None = Field(default=None, description="Git ref for repository sources; defaults to main")
    subpath: str | None = Field(default=None, description="Skill directory inside a repository or ZIP")
    files: list[str] | None = Field(
        default=None,
        max_length=128,
        description="Additional relative files for a web-directory source",
    )


class CommitSkillInput(BaseModel):
    plan_id: str = Field(description="Immutable plan_id returned by the matching prepare tool")
    plan_sha256: str = Field(description="plan_sha256 returned by the matching prepare tool")


class _SkillTool(BaseTool):
    model_config = ConfigDict(arbitrary_types_allowed=True)
    service: SkillManagementService = Field(exclude=True, repr=False)
    session_id: str = Field(default="", exclude=True, repr=False)
    query_id: str = Field(default="", exclude=True, repr=False)
    run_id: str = Field(default="", exclude=True, repr=False)

    @staticmethod
    def _render(callable_):
        try:
            return json.dumps(callable_(), ensure_ascii=False, sort_keys=True)
        except SkillManagementError as exc:
            return json.dumps(exc.as_dict(), ensure_ascii=False, sort_keys=True)


class PrepareSkillInstallTool(_SkillTool):
    name: str = "prepare_skill_install"
    description: str = (
        "Download and validate a remote Skill into managed staging, without changing /skills. "
        "Returns an immutable plan, file diff and digest with status=prepared. Prepared means NOT installed. "
        "When ui_commit_supported=true, the frontend owns confirmation and commit. Otherwise the legacy "
        "approval-gated install_skill continuation remains available."
    )
    args_schema: type[BaseModel] = PrepareSkillInput
    risk_level: str = "network"

    def _run(self, **kwargs) -> str:
        return self._render(
            lambda: self.service.prepare(
                action="install",
                request_context={
                    "session_id": self.session_id,
                    "query_id": self.query_id,
                    "run_id": self.run_id,
                },
                **kwargs,
            )
        )

    async def _arun(self, **kwargs) -> str:
        return await asyncio.to_thread(self._run, **kwargs)


class PrepareSkillUpdateTool(_SkillTool):
    name: str = "prepare_skill_update"
    description: str = (
        "Download and validate an update into managed staging, compare it with the installed Skill, "
        "and return an immutable plan with status=prepared. This does not modify /skills. "
        "When ui_commit_supported=true, the frontend owns confirmation and commit. Otherwise the legacy "
        "approval-gated update_skill continuation remains available."
    )
    args_schema: type[BaseModel] = PrepareSkillInput
    risk_level: str = "network"

    def _run(self, **kwargs) -> str:
        return self._render(
            lambda: self.service.prepare(
                action="update",
                request_context={
                    "session_id": self.session_id,
                    "query_id": self.query_id,
                    "run_id": self.run_id,
                },
                **kwargs,
            )
        )

    async def _arun(self, **kwargs) -> str:
        return await asyncio.to_thread(self._run, **kwargs)


class CommitSkillTool(_SkillTool):
    action: Literal["install", "update"]
    args_schema: type[BaseModel] = CommitSkillInput
    risk_level: str = "managed_skill_write"

    def _run(self, plan_id: str, plan_sha256: str) -> str:
        return self._render(
            lambda: self.service.commit(
                action=self.action,
                plan_id=plan_id,
                plan_sha256=plan_sha256,
            )
        )

    async def _arun(self, plan_id: str, plan_sha256: str) -> str:
        return await asyncio.to_thread(self._run, plan_id, plan_sha256)


def create_skill_management_tools(base_dir: Path) -> list[BaseTool]:
    service = get_skill_management_service(base_dir)
    return [
        PrepareSkillInstallTool(service=service),
        PrepareSkillUpdateTool(service=service),
        CommitSkillTool(
            name="install_skill",
            description=(
                "Commit an approved prepare_skill_install plan into the managed /skills directory. "
                "Requires one-time Harness approval and cannot overwrite an existing Skill."
            ),
            action="install",
            service=service,
        ),
        CommitSkillTool(
            name="update_skill",
            description=(
                "Commit an approved prepare_skill_update plan atomically. Verifies the installed baseline, "
                "creates a rollback snapshot, and requires one-time Harness approval."
            ),
            action="update",
            service=service,
        ),
    ]
