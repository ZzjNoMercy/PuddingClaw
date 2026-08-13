"""Route Skill intent, separate invocation tokens, and enforce activation order."""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable
from typing import Any

from langchain.agents.middleware import AgentMiddleware
from langchain.agents.middleware.types import ModelRequest, ModelResponse, ToolCallRequest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from graph.prompt_cache import append_control_message
from graph.session_manager import session_manager
from harness.models import RunTaskProfile

_MARKER = "[系统 Skill 提示]"


class SkillIntentRouterMiddleware(AgentMiddleware):
    """Suggest project Skills without deciding the Agent capability surface.

    The router is advisory. It can normalize explicit invocation tokens and
    recommend authoritative SKILL.md files, but it neither activates Skills
    nor blocks sibling tools. Capability restoration belongs to Toolset and
    concrete authorization remains in the per-call Tool Gate.
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
                "explicit_skill_ids": [],
                "required_skill_ids": [],
                "missing_required_skill_ids": [],
                "missing_explicit_skill_ids": [],
                "routing_prompt": "",
            }
        candidates = sorted(
            profile.skill_candidates,
            key=lambda item: (not item.explicit, -item.confidence),
        )
        skill_ids = [item.skill_id for item in candidates]
        explicit_skill_ids = [item.skill_id for item in candidates if item.explicit]
        required_skill_ids = [item.skill_id for item in candidates if item.required]
        missing_required = [
            reason.split(":", 1)[1]
            for reason in profile.reasons
            if reason.startswith("missing_required_skill:") and reason.split(":", 1)[1]
        ]
        missing = list(profile.missing_explicit_skill_ids)
        if not skill_ids and not missing and not missing_required:
            return {
                "matched": False,
                "skill_ids": [],
                "explicit_skill_ids": [],
                "required_skill_ids": [],
                "missing_required_skill_ids": [],
                "missing_explicit_skill_ids": [],
                "routing_prompt": "",
            }
        paths = ", ".join(f"/skills/{skill_id}/SKILL.md" for skill_id in skill_ids)
        missing_notice = (
            f"用户明确指定但当前未安装以下 Skill：{', '.join(missing)}。这是可恢复的安装流程，不是任务失败。"
            if missing
            else ""
        )
        missing_required_notice = (
            "当前文件类型必须由专用 Skill 处理，但尚未安装："
            f"{', '.join(missing_required)}。禁止直接读取文件 bytes/Base64 或使用通用工具绕过；"
            "请明确告知用户当前缺少处理能力。"
            if missing_required
            else ""
        )
        load_notice = (
            f"先读取以下语义匹配的 SKILL.md，再按其中流程执行：{paths}。不要猜测尚未加载 Skill 的业务工具。"
            if skill_ids
            else ""
        )
        return {
            "matched": True,
            "skill_ids": skill_ids,
            "explicit_skill_ids": explicit_skill_ids,
            "required_skill_ids": required_skill_ids,
            "missing_required_skill_ids": missing_required,
            "missing_explicit_skill_ids": missing,
            "routing_prompt": " ".join(
                item for item in (missing_required_notice, missing_notice, load_notice) if item
            ),
        }

    def _profile_payload(self, request: Any) -> dict[str, Any] | None:
        profile_payload = (
            request.state.get("task_profile") if isinstance(request.state.get("task_profile"), dict) else None
        )
        runtime = request.runtime
        context = runtime.context if runtime is not None and isinstance(runtime.context, dict) else {}
        session_id = str(context.get("session_id") or "")
        run_id = str(context.get("run_id") or "")
        persisted = session_manager.get_run_state(session_id, run_id) if session_id and run_id else None
        if isinstance(persisted, dict) and isinstance(persisted.get("task_profile"), dict):
            return persisted["task_profile"]
        return profile_payload

    @staticmethod
    def _without_explicit_skill_tokens(content: str, skill_ids: list[str]) -> tuple[str, list[str]]:
        """Separate slash Skill invocations from the task text shown to the model."""

        normalized = content
        removed: list[str] = []
        for skill_id in skill_ids:
            pattern = re.compile(
                rf"(?<!\S)/{re.escape(skill_id)}(?=$|[\s，,。.!！?？；;])",
                flags=re.IGNORECASE,
            )
            normalized, count = pattern.subn("", normalized)
            if count:
                removed.append(skill_id)
        if removed:
            normalized = re.sub(r"[ \t]{2,}", " ", normalized).strip()
        return normalized, removed

    def _pending_barrier_skill_ids(self, request: Any) -> list[str]:
        decision = self._routing_decision(self._profile_payload(request))
        if not decision["matched"]:
            return []
        active_skill_ids = {str(item) for item in request.state.get("active_skill_ids") or []}
        barrier_ids = list(
            dict.fromkeys([*decision["explicit_skill_ids"], *decision["required_skill_ids"]])
        )
        return [skill_id for skill_id in barrier_ids if skill_id not in active_skill_ids]

    @staticmethod
    def _is_required_skill_read(tool_call: dict[str, Any], pending_skill_ids: list[str]) -> bool:
        if str(tool_call.get("name") or "") != "read_file":
            return False
        args = tool_call.get("args")
        if not isinstance(args, dict):
            return False
        file_path = str(args.get("file_path") or "")
        return file_path in {f"/skills/{skill_id}/SKILL.md" for skill_id in pending_skill_ids}

    def _activation_barrier_message(self, request: ToolCallRequest) -> ToolMessage | None:
        """Block sibling work until every explicit or file-required Skill is active."""

        decision = self._routing_decision(self._profile_payload(request))
        missing_required = decision["missing_required_skill_ids"]
        tool_name = str(request.tool_call.get("name") or "")
        if missing_required:
            return ToolMessage(
                content=(
                    "Tool execution was blocked because this file type requires an installed Skill: "
                    f"{', '.join(missing_required)}. Do not read the file bytes/Base64 or bypass the Skill. "
                    "Tell the user that the required file-processing Skill is not installed."
                ),
                tool_call_id=str(request.tool_call.get("id") or ""),
                name=tool_name,
                status="error",
                additional_kwargs={
                    "puddingclaw_control_plane": {
                        "type": "required_skill_missing",
                        "missing_skill_ids": missing_required,
                        "original_tool_executed": False,
                    }
                },
            )
        pending_skill_ids = self._pending_barrier_skill_ids(request)
        if not pending_skill_ids or self._is_required_skill_read(request.tool_call, pending_skill_ids):
            return None
        paths = ", ".join(f"/skills/{skill_id}/SKILL.md" for skill_id in pending_skill_ids)
        return ToolMessage(
            content=(
                f"Tool `{tool_name}` was not executed because a required Skill is not active yet. "
                f"First call `read_file` for: {paths}. After those reads succeed, reconsider the original task "
                "and issue any workspace file calls in a new model turn. A slash Skill invocation is not a file path."
            ),
            tool_call_id=str(request.tool_call.get("id") or ""),
            name=tool_name,
            status="error",
            additional_kwargs={
                "puddingclaw_control_plane": {
                    "type": "explicit_skill_activation_barrier",
                    "pending_skill_ids": pending_skill_ids,
                    "original_tool_executed": False,
                }
            },
        )

    def _response_with_activation_barrier(
        self,
        request: ModelRequest,
        response: ModelResponse,
    ) -> ModelResponse:
        """Drop sibling calls when the model already emitted the required Skill read."""

        pending_skill_ids = self._pending_barrier_skill_ids(request)
        if not pending_skill_ids:
            return response
        required_read_present = any(
            self._is_required_skill_read(tool_call, pending_skill_ids)
            for message in response.result
            if isinstance(message, AIMessage)
            for tool_call in message.tool_calls
        )
        if not required_read_present:
            return response
        filtered_result = [
            message.model_copy(
                update={
                    "tool_calls": [
                        tool_call
                        for tool_call in message.tool_calls
                        if self._is_required_skill_read(tool_call, pending_skill_ids)
                    ]
                }
            )
            if isinstance(message, AIMessage) and message.tool_calls
            else message
            for message in response.result
        ]
        return ModelResponse(
            result=filtered_result,
            structured_response=response.structured_response,
        )

    def _request_with_routing_prompt(self, request: ModelRequest) -> ModelRequest:
        """Add a transient routing hint without writing messages back to state."""
        messages = list(request.messages or [])
        index = next(
            (index for index in range(len(messages) - 1, -1, -1) if isinstance(messages[index], HumanMessage)), None
        )
        if index is None or not isinstance(messages[index].content, str):
            return request
        original = messages[index]
        content = original.content.split(f"\n\n{_MARKER}")[0]
        decision = self._routing_decision(self._profile_payload(request))
        if not decision["matched"]:
            return request
        content, removed_skill_ids = self._without_explicit_skill_tokens(
            content,
            decision["explicit_skill_ids"],
        )
        active_skill_ids = {str(item) for item in request.state.get("active_skill_ids") or []}
        missing_skill_ids = [skill_id for skill_id in decision["skill_ids"] if skill_id not in active_skill_ids]
        if (
            not missing_skill_ids
            and not decision["missing_explicit_skill_ids"]
            and not decision["missing_required_skill_ids"]
            and not removed_skill_ids
        ):
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
        missing_required_notice = (
            "当前文件类型只能由专用 Skill 处理，但以下 Skill 未安装："
            f"{', '.join(decision['missing_required_skill_ids'])}。"
            "禁止读取原始文件、bytes 或 Base64，也不得调用通用工具绕过。"
            "请直接向用户说明当前缺少该文件处理能力。"
            if decision["missing_required_skill_ids"]
            else ""
        )
        load_notice = (
            "Task Router 建议当前任务考虑以下尚未加载的项目 Skill。"
            + (
                "这些 Skill 是用户显式指定或当前文件类型强制要求的；"
                "请优先读取权威 SKILL.md；该建议不决定工具可用性，也不替代每次调用的 Tool Gate。"
                if decision["explicit_skill_ids"] or decision["required_skill_ids"]
                else ""
            )
            + f"先读取：{paths}。再按其中流程执行；不要猜测尚未加载 Skill 的业务工具。"
            if missing_skill_ids
            else ""
        )
        invocation_notice = (
            f"用户消息中的 {'、'.join('/' + item for item in removed_skill_ids)} 是 Skill 调用标记，"
            "不是文件路径，也不得与后续任务文字拼接为 file_path。调用标记已经从下方任务正文中移除。"
            if removed_skill_ids
            else ""
        )
        routing_prompt = " ".join(
            item
            for item in (invocation_notice, missing_required_notice, missing_notice, load_notice)
            if item
        )
        # Keep the user's HumanMessage byte-for-byte intact. The control tail
        # is request-scoped and is removed before Session persistence. A
        # routing hint must never rewrite user-authored content, regardless of
        # prompt-cache configuration.
        return request.override(
            messages=append_control_message(
                messages,
                section="skill_routing",
                content=f"{routing_prompt}\n\n规范化任务文本（仅供路由参考）：{content}",
            )
        )

    def wrap_model_call(self, request: ModelRequest, handler: Any) -> ModelResponse:
        prepared = self._request_with_routing_prompt(request)
        return handler(prepared)

    async def awrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], Awaitable[ModelResponse]],
    ) -> ModelResponse:
        prepared = self._request_with_routing_prompt(request)
        return await handler(prepared)

    def wrap_tool_call(self, request: ToolCallRequest, handler: Any) -> Any:
        return handler(request)

    async def awrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], Awaitable[Any]],
    ) -> Any:
        return await handler(request)


class RequiredSkillBoundaryMiddleware(SkillIntentRouterMiddleware):
    """Keep deterministic file-protocol safety separate from routing advice."""

    def _pending_barrier_skill_ids(self, request: Any) -> list[str]:
        decision = self._routing_decision(self._profile_payload(request))
        active_skill_ids = {str(item) for item in request.state.get("active_skill_ids") or []}
        return [
            skill_id
            for skill_id in decision["required_skill_ids"]
            if skill_id not in active_skill_ids
        ]

    def wrap_model_call(self, request: ModelRequest, handler: Any) -> ModelResponse:
        return self._response_with_activation_barrier(request, handler(request))

    async def awrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], Awaitable[ModelResponse]],
    ) -> ModelResponse:
        return self._response_with_activation_barrier(request, await handler(request))

    def wrap_tool_call(self, request: ToolCallRequest, handler: Any) -> Any:
        blocked = self._activation_barrier_message(request)
        if blocked is not None:
            return blocked
        return handler(request)

    async def awrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], Awaitable[Any]],
    ) -> Any:
        blocked = self._activation_barrier_message(request)
        if blocked is not None:
            return blocked
        return await handler(request)
