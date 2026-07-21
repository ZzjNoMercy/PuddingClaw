"""Soft intent routing that recommends a Skill, never a concrete tool."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from langchain.agents.middleware import AgentMiddleware
from langchain.agents.middleware.types import ModelRequest, ModelResponse
from langchain_core.messages import HumanMessage

from graph.session_manager import session_manager
from harness.models import RunTaskProfile

_MARKER = "[系统 Skill 提示]"


class SkillIntentRouterMiddleware(AgentMiddleware):
    """Suggest the first project Skill to read from the user intent.

    The middleware intentionally does not activate a Toolset. A successful
    ``read_file(/skills/<id>/SKILL.md)`` is the only activation signal.
    """

    def _routing_decision(
        self,
        profile_payload: dict[str, Any] | None,
    ) -> dict[str, Any]:
        try:
            profile = RunTaskProfile.model_validate(profile_payload)
        except Exception:
            return {
                "matched": False,
                "skill_ids": [],
                "missing_explicit_skill_ids": [],
                "routing_prompt": "",
            }
        candidates = sorted(
            profile.skill_candidates,
            key=lambda item: (not item.explicit, -item.confidence),
        )
        skill_ids = [item.skill_id for item in candidates]
        missing = list(profile.missing_explicit_skill_ids)
        if not skill_ids and not missing:
            return {
                "matched": False,
                "skill_ids": [],
                "missing_explicit_skill_ids": [],
                "routing_prompt": "",
            }
        paths = ", ".join(f"/skills/{skill_id}/SKILL.md" for skill_id in skill_ids)
        missing_notice = (
            "用户明确指定但当前未安装以下 Skill："
            f"{', '.join(missing)}。这是可恢复的安装流程，不是任务失败。"
            if missing
            else ""
        )
        load_notice = (
            "先读取以下语义匹配的 SKILL.md，再按其中流程执行："
            f"{paths}。不要猜测尚未加载 Skill 的业务工具。"
            if skill_ids
            else ""
        )
        return {
            "matched": True,
            "skill_ids": skill_ids,
            "missing_explicit_skill_ids": missing,
            "routing_prompt": " ".join(item for item in (missing_notice, load_notice) if item),
        }

    def _request_with_routing_prompt(self, request: ModelRequest) -> ModelRequest:
        """Add a transient routing hint without writing messages back to state."""
        messages = list(request.messages or [])
        index = next((index for index in range(len(messages) - 1, -1, -1) if isinstance(messages[index], HumanMessage)), None)
        if index is None or not isinstance(messages[index].content, str):
            return request
        original = messages[index]
        content = original.content.split(f"\n\n{_MARKER}")[0]
        profile_payload = (
            request.state.get("task_profile")
            if isinstance(request.state.get("task_profile"), dict)
            else None
        )
        runtime = request.runtime
        context = runtime.context if runtime is not None and isinstance(runtime.context, dict) else {}
        session_id = str(context.get("session_id") or "")
        run_id = str(context.get("run_id") or "")
        persisted = session_manager.get_run_state(session_id, run_id) if session_id and run_id else None
        if isinstance(persisted, dict) and isinstance(persisted.get("task_profile"), dict):
            profile_payload = persisted["task_profile"]
        decision = self._routing_decision(profile_payload)
        if not decision["matched"]:
            return request
        active_skill_ids = {str(item) for item in request.state.get("active_skill_ids") or []}
        missing_skill_ids = [skill_id for skill_id in decision["skill_ids"] if skill_id not in active_skill_ids]
        if not missing_skill_ids and not decision["missing_explicit_skill_ids"]:
            return request
        paths = ", ".join(f"/skills/{skill_id}/SKILL.md" for skill_id in missing_skill_ids)
        missing_notice = (
            "用户明确指定但当前未安装以下 Skill："
            f"{', '.join(decision['missing_explicit_skill_ids'])}。"
            "不要假装已经加载，也不要直接判定任务失败。向用户提供安装并继续、提供或搜索权威 HTTPS 来源、"
            "以及由用户明确选择通用 Agent 执行三种恢复方式。若已有来源或用户要求安装，先读取 "
            "/skills/skill-management/SKILL.md，按其中审批流程安装；安装成功后读取新 Skill 并继续原任务。"
            if decision["missing_explicit_skill_ids"]
            else ""
        )
        load_notice = (
            "本轮任务已由统一路由器匹配到尚未加载的项目 Skill。先读取："
            f"{paths}。再按其中流程执行；不要猜测尚未加载 Skill 的业务工具。"
            if missing_skill_ids
            else ""
        )
        routing_prompt = " ".join(
            item for item in (missing_notice, load_notice) if item
        )
        messages[index] = original.model_copy(
            update={"content": f"{content}\n\n{_MARKER} {routing_prompt}"}
        )
        return request.override(messages=messages)

    def wrap_model_call(self, request: ModelRequest, handler: Any) -> ModelResponse:
        return handler(self._request_with_routing_prompt(request))

    async def awrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], Awaitable[ModelResponse]],
    ) -> ModelResponse:
        return await handler(self._request_with_routing_prompt(request))
