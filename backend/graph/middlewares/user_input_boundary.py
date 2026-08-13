"""Trusted execution boundary for generic structured user input."""

from __future__ import annotations

import json
import re
from collections.abc import Awaitable, Callable
from typing import Any

from langchain.agents.middleware import AgentMiddleware
from langchain.agents.middleware.types import ModelRequest, ModelResponse, ToolCallRequest
from langchain_core.messages import HumanMessage, ToolMessage
from langgraph.types import Command

_LLM_WIKI_INGEST_RE = re.compile(
    r"(?:编译|整理|加入|添加|写入|转成|做成|生成).{0,16}(?:LLM\s*)?Wiki"
    r"|(?:LLM\s*)?Wiki.{0,16}(?:编译|整理|加入|添加|写入|转成|做成|生成)"
    r"|(?:compile|organize|add|publish|turn).{0,24}(?:llm\s*)?wiki",
    flags=re.IGNORECASE,
)
_CONTEXT_CONTINUATION_RE = re.compile(
    r"^(?:请)?(?:继续|接着|恢复|继续执行|继续处理|继续完成|接着做|接着处理|再试一次|重试)"
    r"(?:吧|啊|呀|一下|任务|工作|这个任务)?[。.!！?？\s]*$",
    flags=re.IGNORECASE,
)


class UserInputBoundaryMiddleware(AgentMiddleware[Any, Any, Any]):
    """Make a user-input request an atomic decision boundary.

    If the model emits request_user_input beside another Tool Call, every call
    in that Assistant message is rejected. This prevents sibling mutations
    from racing ahead of the answer the model said it needed.
    """

    @staticmethod
    def _sibling_names(request: ToolCallRequest) -> list[str]:
        call_id = str(request.tool_call.get("id") or "")
        for message in reversed(list(request.state.get("messages") or [])):
            calls = getattr(message, "tool_calls", None)
            if not isinstance(calls, list):
                continue
            ids = [str(item.get("id") or "") for item in calls if isinstance(item, dict)]
            if call_id in ids:
                return [
                    str(item.get("name") or "")
                    for item in calls
                    if isinstance(item, dict)
                ]
        return []

    @staticmethod
    def _explicit_wiki_ingest(messages: list[Any]) -> bool:
        """Resolve explicit Wiki ingest authority from the visible conversation.

        A short continuation turn inherits the nearest preceding user intent.
        This is an execution guard, not a Skill router: it only removes a
        redundant confirmation path after the user already authorized ingest.
        """

        human_turns = [
            str(message.content or "").strip()
            for message in messages
            if isinstance(message, HumanMessage) and isinstance(message.content, str)
        ]
        if not human_turns:
            return False
        current = human_turns[-1]
        if _LLM_WIKI_INGEST_RE.search(current):
            return True
        if not _CONTEXT_CONTINUATION_RE.fullmatch(current):
            return False
        return any(_LLM_WIKI_INGEST_RE.search(item) for item in reversed(human_turns[-6:-1]))

    @staticmethod
    def _tool_name(tool: Any) -> str:
        if isinstance(tool, dict):
            return str(tool.get("name") or tool.get("function", {}).get("name") or "")
        return str(getattr(tool, "name", "") or "")

    def _without_redundant_wiki_confirmation(self, request: ModelRequest) -> ModelRequest:
        if not self._explicit_wiki_ingest(list(request.messages or [])):
            return request
        filtered = [tool for tool in request.tools if self._tool_name(tool) != "request_user_input"]
        if len(filtered) == len(request.tools):
            return request
        return request.override(tools=filtered)

    @staticmethod
    def _rejection(request: ToolCallRequest) -> ToolMessage | None:
        if (
            str(request.tool_call.get("name") or "") == "request_user_input"
            and UserInputBoundaryMiddleware._explicit_wiki_ingest(
                list(request.state.get("messages") or [])
            )
        ):
            return ToolMessage(
                content=json.dumps(
                    {
                        "error": "用户已经明确授权 LLM Wiki 编译，禁止重复确认。",
                        "next": (
                            "直接调用 llm_wiki_create_raw；若用户指代此前会话材料则使用 "
                            "source=conversation。成功后原样调用 llm_wiki_start_ingest。"
                        ),
                    },
                    ensure_ascii=False,
                ),
                tool_call_id=str(request.tool_call.get("id") or ""),
                name="request_user_input",
                status="error",
                additional_kwargs={
                    "puddingclaw_control_plane": {
                        "type": "redundant_llm_wiki_confirmation_blocked",
                        "original_tool_executed": False,
                    }
                },
            )
        names = UserInputBoundaryMiddleware._sibling_names(request)
        if "request_user_input" not in names or len(names) == 1:
            return None
        return ToolMessage(
            content=json.dumps(
                {
                    "error": (
                        "request_user_input 必须是该 Assistant Message 中唯一的 Tool Call；"
                        "请先单独请求用户选择，恢复后再执行其他工具。"
                    )
                },
                ensure_ascii=False,
            ),
            tool_call_id=str(request.tool_call.get("id") or ""),
            status="error",
        )

    def wrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], ModelResponse],
    ) -> ModelResponse:
        return handler(self._without_redundant_wiki_confirmation(request))

    async def awrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], Awaitable[ModelResponse]],
    ) -> ModelResponse:
        return await handler(self._without_redundant_wiki_confirmation(request))

    def wrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], ToolMessage | Command[Any]],
    ) -> ToolMessage | Command[Any]:
        return self._rejection(request) or handler(request)

    async def awrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], Awaitable[ToolMessage | Command[Any]]],
    ) -> ToolMessage | Command[Any]:
        rejection = self._rejection(request)
        return rejection if rejection is not None else await handler(request)
