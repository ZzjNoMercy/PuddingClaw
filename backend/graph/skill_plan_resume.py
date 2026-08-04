"""In-process bridge from Skill plan commits to the active Agent HITL run."""

from __future__ import annotations

import asyncio
import hashlib
import time
from typing import Any


class SkillPlanResumeRegistry:
    def __init__(self) -> None:
        self._requests: dict[str, dict[str, Any]] = {}
        self._pending: dict[str, asyncio.Future[dict[str, Any]]] = {}
        self._loops: dict[str, asyncio.AbstractEventLoop] = {}

    def create(self, *, session_id: str, query_id: str, run_id: str, tool_call_id: str, plans: list[dict[str, Any]]) -> dict[str, Any]:
        key = "\0".join((session_id, query_id, run_id, tool_call_id))
        request_id = "skill-confirm-" + hashlib.sha256(key.encode()).hexdigest()[:16]
        existing = self._requests.get(request_id)
        if existing is not None:
            return dict(existing)
        plan_ids = [str(plan.get("plan_id") or "") for plan in plans if plan.get("plan_id")]
        request = {
            "id": request_id,
            "type": "skill_plan_confirmation",
            "session_id": session_id,
            "query_id": query_id,
            "run_id": run_id,
            "tool_call_id": tool_call_id,
            "plan_ids": plan_ids,
            "skill_names": [str(plan.get("skill_name") or "") for plan in plans],
            "statuses": {plan_id: "prepared" for plan_id in plan_ids},
            "status": "pending",
            "created_at": time.time(),
        }
        self._requests[request_id] = request
        loop = asyncio.get_running_loop()
        self._pending[request_id] = loop.create_future()
        self._loops[request_id] = loop
        return dict(request)

    async def wait(self, request_id: str) -> dict[str, Any]:
        future = self._pending.get(request_id)
        if future is None:
            request = self._requests.get(request_id) or {}
            return dict(request.get("decision") or {"action": "cancel"})
        try:
            return await future
        finally:
            self._pending.pop(request_id, None)

    def record(self, *, session_id: str, plan_id: str, status: str) -> bool:
        for request_id, request in self._requests.items():
            if request.get("session_id") != session_id or request.get("status") != "pending":
                continue
            statuses = request.get("statuses") or {}
            if plan_id not in statuses:
                continue
            statuses[plan_id] = status
            if any(value == "prepared" for value in statuses.values()):
                return False
            action = "confirm" if all(value == "committed" for value in statuses.values()) else "cancel"
            decision = {"action": action, "statuses": dict(statuses)}
            request["status"] = "resolved"
            request["resolved_at"] = time.time()
            request["decision"] = decision
            future = self._pending.get(request_id)
            if future is not None and not future.done():
                loop = self._loops.get(request_id)
                if loop is not None:
                    loop.call_soon_threadsafe(future.set_result, decision)
            return True
        return False

    def cancel(self, request_id: str, message: str = "") -> dict[str, Any] | None:
        """Cancel a pending plan request through the registry's own future."""

        request = self._requests.get(request_id)
        if request is None or request.get("status") != "pending":
            return None
        decision = {"action": "cancel"}
        if message:
            decision["reason"] = message
        request["status"] = "resolved"
        request["resolved_at"] = time.time()
        request["decision"] = decision
        future = self._pending.get(request_id)
        if future is not None and not future.done():
            future.set_result(decision)
        return decision

    def reject_session(self, session_id: str, message: str) -> int:
        count = 0
        for request_id, request in list(self._requests.items()):
            if request.get("session_id") != session_id or request.get("status") != "pending":
                continue
            if self.cancel(request_id, message) is not None:
                count += 1
        return count

    def has_pending_session(self, session_id: str) -> bool:
        """Return whether a live Skill-plan decision belongs to the Session."""

        return any(
            request.get("session_id") == session_id
            and request.get("status") == "pending"
            and request_id in self._pending
            and not self._pending[request_id].done()
            for request_id, request in self._requests.items()
        )

    def reject_run(self, session_id: str, run_id: str, message: str) -> int:
        count = 0
        for request_id, request in list(self._requests.items()):
            if (
                request.get("session_id") != session_id
                or str(request.get("run_id") or "") != run_id
                or request.get("status") != "pending"
            ):
                continue
            if self.cancel(request_id, message) is not None:
                count += 1
        return count


skill_plan_resume_registry = SkillPlanResumeRegistry()
