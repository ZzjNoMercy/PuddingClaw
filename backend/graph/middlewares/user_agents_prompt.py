"""Place stable Home AGENTS.md after Agent Core and before project/runtime layers."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from deepagents.middleware._utils import append_to_system_message
from langchain.agents.middleware import AgentMiddleware
from langchain.agents.middleware.types import ModelRequest, ModelResponse

from graph.prompt_cache import reorder_system_prompt_sections


class UserAgentsPromptMiddleware(AgentMiddleware[Any, Any, Any]):
    """Inject user AGENTS late, then deterministically move it into the stable prefix."""

    def __init__(self, content: str) -> None:
        self.content = str(content or "").strip()

    def _modify_request(self, request: ModelRequest) -> ModelRequest:
        if not self.content:
            return request
        prompt = append_to_system_message(request.system_message, self.content)
        prompt = prompt.model_copy(
            update={"content": reorder_system_prompt_sections(prompt.text)}
        )
        return request.override(system_message=prompt)

    def wrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], ModelResponse],
    ) -> ModelResponse:
        return handler(self._modify_request(request))

    async def awrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], Awaitable[ModelResponse]],
    ) -> ModelResponse:
        return await handler(self._modify_request(request))


__all__ = ["UserAgentsPromptMiddleware"]
