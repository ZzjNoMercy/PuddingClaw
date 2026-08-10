"""Secure HITL request for a generic environment-variable Skill Secret."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Any

from langchain_core.tools import BaseTool, InjectedToolCallId
from langgraph.types import interrupt
from pydantic import BaseModel, ConfigDict, Field

from graph.skill_secret_resume import skill_secret_resume_registry
from runtime_identity.paths import PuddingClawPaths, trusted_owner_user_id
from runtime_identity.skill_secrets import SkillSecretStore, validate_skill_secret_name
from runtime_identity.software_runtime import skill_content_version


class RequestSkillSecretArgs(BaseModel):
    skill_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
    env_name: str = Field(pattern=r"^[A-Z_][A-Z0-9_]{0,127}$")
    reason: str = Field(min_length=1, max_length=300)
    tool_call_id: Annotated[str, InjectedToolCallId]


class RequestSkillSecretTool(BaseTool):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    name: str = "request_skill_secret"
    description: str = (
        "Securely request one environment-variable Secret required by an installed Skill. "
        "The Agent supplies only skill_id, variable name, and reason; the value is entered in a "
        "trusted UI and is never returned to the Agent. Do not use shell prompts or files for secrets."
    )
    args_schema: type[BaseModel] = RequestSkillSecretArgs
    risk_level: str = "moderate"
    session_id: str = ""
    query_id: str = ""
    run_id: str = ""
    goal_id: str = ""
    goal_revision: int | None = None
    skills_dir: str = ""
    paths: PuddingClawPaths = Field(exclude=True)

    def _run(self, **kwargs: Any) -> str:
        raise RuntimeError("Use async execution for Skill Secret HITL")

    async def _arun(self, **kwargs: Any) -> str:
        if not self.session_id or not self.query_id or not self.run_id:
            return "Skill Secret setup is unavailable: missing trusted Run binding."
        tool_call_id = str(kwargs.get("tool_call_id") or "")
        skill_id = str(kwargs.get("skill_id") or "")
        env_name = validate_skill_secret_name(str(kwargs.get("env_name") or ""))
        skills = Path(self.skills_dir).resolve(strict=True)
        skill_root = (skills / skill_id).resolve(strict=True)
        skill_root.relative_to(skills)
        if skill_root.is_symlink() or not (skill_root / "SKILL.md").is_file():
            return "Skill Secret setup failed: Skill is not installed."
        skill_version = skill_content_version(skill_root)
        store = SkillSecretStore(self.paths, trusted_owner_user_id())
        status = store.status(skill_id=skill_id, skill_version=skill_version, env_name=env_name)
        if status == "bound":
            return f"{env_name} is already securely bound to Skill {skill_id}."
        request = skill_secret_resume_registry.create(
            session_id=self.session_id,
            query_id=self.query_id,
            run_id=self.run_id,
            goal_id=self.goal_id,
            goal_revision=self.goal_revision,
            tool_call_id=tool_call_id,
            payload={
                "skill_id": skill_id,
                "skill_version": skill_version,
                "env_name": env_name,
                "reason": str(kwargs.get("reason") or ""),
                "mode": "reuse" if status == "reusable" else "enter",
            },
        )
        decision = interrupt(
            {
                "type": "skill_secret_request",
                "request": request,
                "decisions": [{"action": "configured"}, {"action": "cancel"}],
            }
        )
        if not isinstance(decision, dict) or decision.get("action") != "configured":
            return "User cancelled Skill Secret setup. Do not retry or request the value in chat."
        return f"{env_name} was securely configured for Skill {skill_id}; retry the original command."


def create_request_skill_secret_tool(
    *,
    session_id: str,
    query_id: str,
    run_id: str,
    goal_id: str,
    goal_revision: int | None,
    skills_dir: Path,
    paths: PuddingClawPaths,
) -> RequestSkillSecretTool:
    return RequestSkillSecretTool(
        session_id=session_id,
        query_id=query_id,
        run_id=run_id,
        goal_id=goal_id,
        goal_revision=goal_revision,
        skills_dir=str(skills_dir),
        paths=paths,
    )
