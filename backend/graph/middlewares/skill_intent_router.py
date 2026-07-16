"""Soft intent routing that recommends a Skill, never a concrete tool."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from langchain.agents.middleware import AgentMiddleware
from langchain.agents.middleware.types import ModelRequest, ModelResponse
from langchain_core.messages import HumanMessage

from harness.task_profiles import TaskProfileClassifier

_MARKER = "[系统 Skill 提示]"


class SkillIntentRouterMiddleware(AgentMiddleware):
    """Suggest the first project Skill to read from the user intent.

    The middleware intentionally does not activate a Toolset. A successful
    ``read_file(/skills/<id>/SKILL.md)`` is the only activation signal.
    """

    def _classify_intent(self, text: str) -> dict[str, Any]:
        profile = TaskProfileClassifier.classify(message=text)
        skills = TaskProfileClassifier.skill_ids(profile)
        if not skills:
            return {"matched": False, "skill_ids": [], "routing_prompt": ""}
        paths = ", ".join(f"/skills/{skill_id}/SKILL.md" for skill_id in skills)
        return {
            "matched": True,
            "skill_ids": skills,
            "routing_prompt": f"本轮问题匹配到项目 Skill。先读取以下最相关的 SKILL.md，再按其中流程执行：{paths}。不要猜测尚未加载 Skill 的业务工具。",
        }

    def _request_with_routing_prompt(self, request: ModelRequest) -> ModelRequest:
        """Add a transient routing hint without writing messages back to state."""
        messages = list(request.messages or [])
        index = next((index for index in range(len(messages) - 1, -1, -1) if isinstance(messages[index], HumanMessage)), None)
        if index is None or not isinstance(messages[index].content, str):
            return request
        original = messages[index]
        content = original.content.split(f"\n\n{_MARKER}")[0]
        decision = self._classify_intent(content)
        if not decision["matched"]:
            return request
        active_skill_ids = {str(item) for item in request.state.get("active_skill_ids") or []}
        missing_skill_ids = [skill_id for skill_id in decision["skill_ids"] if skill_id not in active_skill_ids]
        if not missing_skill_ids:
            return request
        paths = ", ".join(f"/skills/{skill_id}/SKILL.md" for skill_id in missing_skill_ids)
        routing_prompt = (
            "本轮问题匹配到尚未加载的项目 Skill。先读取以下最相关的 SKILL.md，再按其中流程执行："
            f"{paths}。不要猜测尚未加载 Skill 的业务工具。"
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
