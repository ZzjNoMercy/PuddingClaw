"""Trusted completion-request boundary for the main Goal Agent only."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from typing import Any

from langchain.agents.middleware import AgentMiddleware
from langchain.agents.middleware.types import ToolCallRequest, hook_config
from langchain_core.messages import HumanMessage, ToolMessage
from langgraph.types import Command

from graph.session_manager import session_manager

GOAL_COMPLETION_REMINDER_SOURCE = "puddingclaw_goal_completion_protocol"


class GoalCompletionMiddleware(AgentMiddleware[Any, Any, Any]):
    """Persist explicit completion calls and invalidate them when work resumes."""

    @staticmethod
    def _context(request: ToolCallRequest) -> dict[str, Any]:
        runtime = request.runtime
        return runtime.context if runtime is not None and isinstance(runtime.context, dict) else {}

    @staticmethod
    def _tool_message(request: ToolCallRequest, payload: dict[str, Any], *, status: str = "success") -> ToolMessage:
        return ToolMessage(
            content=json.dumps(payload, ensure_ascii=False),
            tool_call_id=str(request.tool_call.get("id") or ""),
            status=status,  # type: ignore[arg-type]
        )

    @staticmethod
    def _same_message_has_sibling_call(request: ToolCallRequest) -> bool:
        call_id = str(request.tool_call.get("id") or "")
        for message in reversed(list(request.state.get("messages") or [])):
            calls = getattr(message, "tool_calls", None)
            if not isinstance(calls, list):
                continue
            ids = [str(item.get("id") or "") for item in calls if isinstance(item, dict)]
            if call_id in ids:
                return len(ids) != 1
        return False

    def _handle_update_goal(self, request: ToolCallRequest) -> ToolMessage:
        context = self._context(request)
        args = request.tool_call.get("args") or {}
        if not isinstance(args, dict):
            return self._tool_message(request, {"error": "update_goal 参数无效。"}, status="error")
        if args.get("completed") is not True:
            return self._tool_message(
                request,
                {"error": "仅支持 completed=true；进度更新请省略 completed。"},
                status="error",
            )
        if self._same_message_has_sibling_call(request):
            return self._tool_message(
                request,
                {"error": "完成声明必须是该 Assistant Message 中唯一的 Tool Call。"},
                status="error",
            )
        try:
            saved = session_manager.record_goal_completion_request(
                str(context.get("session_id") or ""),
                goal_id=str(context.get("goal_id") or ""),
                objective_revision=int(context.get("goal_revision") or 0),
                run_id=str(context.get("run_id") or ""),
                tool_call_id=str(request.tool_call.get("id") or ""),
                message=str(args.get("message") or ""),
            )
        except (FileNotFoundError, ValueError) as exc:
            return self._tool_message(request, {"error": str(exc)}, status="error")
        policy = str(saved.get("policy") or "standard")
        return self._tool_message(
            request,
            {
                "completion_request_id": saved.get("request_id"),
                "status": "requested",
                "message": (
                    "Completion request recorded for Rubric verification. Finish the candidate final response."
                    if policy == "rubric"
                    else "Completion request recorded. Finish the final response."
                ),
            },
        )

    def wrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], ToolMessage | Command[Any]],
    ) -> ToolMessage | Command[Any]:
        name = str(request.tool_call.get("name") or "")
        if name == "update_goal":
            return self._handle_update_goal(request)
        result = handler(request)
        if isinstance(result, ToolMessage) and result.status == "success":
            context = self._context(request)
            if context.get("session_id") and context.get("run_id"):
                session_manager.invalidate_goal_completion_request(
                    str(context["session_id"]), run_id=str(context["run_id"]), reason=f"post_completion_tool:{name}"
                )
        return result

    async def awrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], Awaitable[ToolMessage | Command[Any]]],
    ) -> ToolMessage | Command[Any]:
        name = str(request.tool_call.get("name") or "")
        if name == "update_goal":
            return self._handle_update_goal(request)
        result = await handler(request)
        if isinstance(result, ToolMessage) and result.status == "success":
            context = self._context(request)
            if context.get("session_id") and context.get("run_id"):
                session_manager.invalidate_goal_completion_request(
                    str(context["session_id"]), run_id=str(context["run_id"]), reason=f"post_completion_tool:{name}"
                )
        return result

    @staticmethod
    def _completion_reminder_update(
        state: dict[str, Any],
        *,
        persisted_run: dict[str, Any] | None,
        persisted_goal: dict[str, Any] | None,
        completion_request: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        """Give a naturally-stopping standard Goal one structured retry."""

        run_id = str(persisted_run.get("run_id") or "") if isinstance(persisted_run, dict) else ""
        goal_id = str(persisted_goal.get("goal_id") or "") if isinstance(persisted_goal, dict) else ""
        if (
            not isinstance(persisted_run, dict)
            or not isinstance(persisted_goal, dict)
            or str(persisted_run.get("run_kind") or "") != "goal_execution"
            or str(persisted_run.get("status") or "") != "running"
            or not run_id
            or not goal_id
            or str(persisted_run.get("goal_id") or "") != goal_id
            or str(persisted_goal.get("current_run_id") or "") != run_id
            or int(persisted_run.get("goal_revision") or 0) != int(persisted_goal.get("objective_revision") or 0)
            or str(persisted_goal.get("completion_policy") or "standard") != "standard"
            or str(persisted_goal.get("status") or "") != "active"
            or int(state.get("_goal_completion_reminder_count") or 0) >= 1
        ):
            return None
        request_id = str(persisted_run.get("completion_request_id") or "")
        if (
            request_id
            and isinstance(completion_request, dict)
            and str(completion_request.get("request_id") or "") == request_id
            and str(completion_request.get("status") or "") == "requested"
        ):
            return None
        return {
            "_goal_completion_reminder_count": 1,
            "messages": [
                HumanMessage(
                    name=GOAL_COMPLETION_REMINDER_SOURCE,
                    additional_kwargs={"lc_source": GOAL_COMPLETION_REMINDER_SOURCE},
                    content=(
                        "你正在结束一个标准 Goal Run，但尚未提交结构化完成声明。"
                        "请重新核对原始 Goal、Todo、产物和实际验证结果：如果全部完成且没有已知遗留工作，"
                        "下一条 Assistant Message 只能调用 update_goal(completed=true)，然后再给最终回复；"
                        "如果尚未完成，继续执行剩余工作或使用 request_user_input 获取真正缺失的信息。"
                        "不要在没有 update_goal 的情况下重复最终完成答复。"
                    ),
                )
            ],
            "jump_to": "model",
        }

    @hook_config(can_jump_to=["model"])
    def after_agent(self, state: Any, runtime: Any) -> dict[str, Any] | None:
        context = runtime.context if runtime is not None and isinstance(runtime.context, dict) else {}
        session_id = str(context.get("session_id") or "")
        run_id = str(context.get("run_id") or "")
        goal_id = str(context.get("goal_id") or "")
        if not all((session_id, run_id, goal_id)):
            return None
        harness = session_manager.get_harness_state(session_id)
        runs = harness.get("runs") if isinstance(harness, dict) else None
        goals = harness.get("goals") if isinstance(harness, dict) else None
        requests = harness.get("completion_requests") if isinstance(harness, dict) else None
        persisted_run = runs.get(run_id) if isinstance(runs, dict) else None
        persisted_goal = goals.get(goal_id) if isinstance(goals, dict) else None
        request_id = str(persisted_run.get("completion_request_id") or "") if isinstance(persisted_run, dict) else ""
        return self._completion_reminder_update(
            dict(state),
            persisted_run=persisted_run,
            persisted_goal=persisted_goal,
            completion_request=requests.get(request_id) if request_id and isinstance(requests, dict) else None,
        )

    @hook_config(can_jump_to=["model"])
    async def aafter_agent(self, state: Any, runtime: Any) -> dict[str, Any] | None:
        context = runtime.context if runtime is not None and isinstance(runtime.context, dict) else {}
        session_id = str(context.get("session_id") or "")
        run_id = str(context.get("run_id") or "")
        goal_id = str(context.get("goal_id") or "")
        if not all((session_id, run_id, goal_id)):
            return None
        harness = await asyncio.to_thread(session_manager.get_harness_state, session_id)
        runs = harness.get("runs") if isinstance(harness, dict) else None
        goals = harness.get("goals") if isinstance(harness, dict) else None
        requests = harness.get("completion_requests") if isinstance(harness, dict) else None
        persisted_run = runs.get(run_id) if isinstance(runs, dict) else None
        persisted_goal = goals.get(goal_id) if isinstance(goals, dict) else None
        request_id = str(persisted_run.get("completion_request_id") or "") if isinstance(persisted_run, dict) else ""
        return self._completion_reminder_update(
            dict(state),
            persisted_run=persisted_run,
            persisted_goal=persisted_goal,
            completion_request=requests.get(request_id) if request_id and isinstance(requests, dict) else None,
        )
