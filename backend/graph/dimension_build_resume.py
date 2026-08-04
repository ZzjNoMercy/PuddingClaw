"""In-process HITL bridge for semantic-dimension build rule selection."""

from __future__ import annotations

import asyncio
import time
import uuid
from typing import Any

from knowledge.semantic_dimension_rule_contract import SemanticDimensionRuleError, build_rule_from_decision


class DimensionBuildResumeRegistry:
    def __init__(self) -> None:
        self._pending: dict[str, asyncio.Future[dict[str, Any]]] = {}
        self._requests: dict[str, dict[str, Any]] = {}

    def create(self, *, session_id: str, query_id: str, tool_call_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        request_id = f"dim-rule-{uuid.uuid4().hex[:12]}"
        request = {
            "id": request_id,
            "type": "semantic_dimension_build_rule",
            "session_id": session_id,
            "query_id": query_id,
            "tool_call_id": tool_call_id,
            "status": "pending",
            "created_at": time.time(),
            **payload,
        }
        self._requests[request_id] = request
        self._pending[request_id] = asyncio.get_running_loop().create_future()
        return dict(request)

    async def wait(self, request_id: str) -> dict[str, Any]:
        future = self._pending.get(request_id)
        if future is None:
            return {"action": "cancel", "message": "Dimension build request is no longer active."}
        try:
            return await future
        finally:
            self._pending.pop(request_id, None)

    def resolve(self, request_id: str, decision: dict[str, Any]) -> dict[str, Any] | None:
        request = self._requests.get(request_id)
        future = self._pending.get(request_id)
        if request is None or future is None or request.get("status") != "pending":
            return None
        try:
            normalized = {"action": "cancel"} if decision.get("action") == "cancel" else {
                "action": "confirm",
                "build_rule": build_rule_from_decision(request, decision),
            }
        except SemanticDimensionRuleError as exc:
            raise ValueError(str(exc)) from exc
        request["status"] = "resolved"
        request["resolved_at"] = time.time()
        request["decision"] = normalized
        if not future.done():
            future.set_result(normalized)
        return normalized

    def reject_session(self, session_id: str, message: str) -> int:
        count = 0
        for request_id, request in list(self._requests.items()):
            if request.get("session_id") == session_id and request.get("status") == "pending":
                if self.resolve(request_id, {"action": "cancel", "message": message}) is not None:
                    count += 1
        return count

    def has_pending_session(self, session_id: str) -> bool:
        """Return whether a live dimension-rule decision belongs to the Session."""

        return any(
            request.get("session_id") == session_id
            and request.get("status") == "pending"
            and request_id in self._pending
            and not self._pending[request_id].done()
            for request_id, request in self._requests.items()
        )

    def get(self, request_id: str) -> dict[str, Any] | None:
        item = self._requests.get(request_id)
        return dict(item) if item else None


dimension_build_resume_registry = DimensionBuildResumeRegistry()
