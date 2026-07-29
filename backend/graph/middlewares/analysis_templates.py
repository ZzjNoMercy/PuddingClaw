"""Progressively activate trusted analysis templates after their guide is read."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from langchain.agents.middleware import AgentMiddleware
from langchain.agents.middleware.types import ToolCallRequest
from langchain_core.messages import ToolMessage
from langgraph.types import Command

from analytics.models import AnalyticsModelError, get_analytics_model_registry


def _normalized_virtual_path(value: object) -> str:
    path = str(value or "").strip().replace("\\", "/")
    return path if path.startswith("/") else f"/{path}"


def _successful_tool_message(result: ToolMessage | Command[Any]) -> ToolMessage | None:
    if isinstance(result, ToolMessage):
        return result if result.status == "success" else None
    update = result.update if isinstance(result.update, dict) else {}
    for message in reversed(update.get("messages") or []):
        if isinstance(message, ToolMessage) and message.status == "success":
            return message
    return None


def _with_state_update(
    result: ToolMessage | Command[Any],
    *,
    activation: dict[str, Any],
) -> Command[Any]:
    if isinstance(result, ToolMessage):
        return Command(
            update={
                "_active_analysis_template": activation,
                "messages": [result],
            }
        )
    update = dict(result.update) if isinstance(result.update, dict) else {}
    update["_active_analysis_template"] = activation
    return Command(
        graph=result.graph,
        update=update,
        resume=result.resume,
        goto=result.goto,
    )


class AnalysisTemplateMiddleware(AgentMiddleware):
    """Treat a successful registered TEMPLATE.md read as Agent template selection.

    The model catalog only helps the Agent discover a template.  The server
    does not route templates from query keywords.  Once the Agent reads a
    registered guide, this middleware recompiles its trusted manifest and
    writes the selected template into private graph state for downstream
    tools and delegated agents.
    """

    def __init__(self, *, base_dir: Path) -> None:
        super().__init__()
        self.base_dir = base_dir.resolve()

    def _activation(self, request: ToolCallRequest) -> dict[str, Any] | None:
        if str(request.tool_call.get("name") or "") != "read_file":
            return None
        args = request.tool_call.get("args") or {}
        read_path = _normalized_virtual_path(args.get("file_path") or args.get("path"))
        model_id = str(request.state.get("analytics_model_id") or "").strip()
        if not model_id:
            return None
        try:
            context = get_analytics_model_registry(self.base_dir).get_model_context(model_id)
        except AnalyticsModelError:
            return None
        for template_id, template in (context.get("resolved_templates") or {}).items():
            if not isinstance(template, dict) or template.get("available") is not True:
                continue
            if _normalized_virtual_path(template.get("guide_virtual_path")) != read_path:
                continue
            return {
                "model_id": model_id,
                "template_id": str(template_id),
                "template_version": str(
                    (template.get("guide_frontmatter") or {}).get("version") or ""
                ),
                "guide_content_sha256": str(template.get("guide_content_sha256") or ""),
                "virtual_path": template.get("virtual_path"),
                "guide_virtual_path": template.get("guide_virtual_path"),
                "asset_virtual_paths": list(template.get("asset_virtual_paths") or []),
                "semantic_scope": template.get("compiled_semantic_scope") or {},
                "source": "authoritative_guide_read",
                "source_tool_call_id": str(request.tool_call.get("id") or ""),
            }
        return None

    def wrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], ToolMessage | Command[Any]],
    ) -> ToolMessage | Command[Any]:
        result = handler(request)
        if _successful_tool_message(result) is None:
            return result
        activation = self._activation(request)
        return _with_state_update(result, activation=activation) if activation else result

    async def awrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], Awaitable[ToolMessage | Command[Any]]],
    ) -> ToolMessage | Command[Any]:
        result = await handler(request)
        if _successful_tool_message(result) is None:
            return result
        activation = self._activation(request)
        return _with_state_update(result, activation=activation) if activation else result
