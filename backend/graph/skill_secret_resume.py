"""In-process bridge for secure Skill Secret setup HITL."""

from __future__ import annotations

import asyncio
import hashlib
import time
from typing import Any


class SkillSecretResumeRegistry:
    def __init__(self) -> None:
        self._pending: dict[str, asyncio.Future[dict[str, Any]]] = {}
        self._requests: dict[str, dict[str, Any]] = {}
        self._by_replay_key: dict[str, str] = {}

    @staticmethod
    def _replay_key(session_id: str, run_id: str, tool_call_id: str) -> str:
        return hashlib.sha256(f"{session_id}\0{run_id}\0{tool_call_id}".encode()).hexdigest()

    def create(
        self,
        *,
        session_id: str,
        query_id: str,
        run_id: str,
        goal_id: str,
        goal_revision: int | None,
        tool_call_id: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        replay_key = self._replay_key(session_id, run_id, tool_call_id)
        existing_id = self._by_replay_key.get(replay_key)
        if existing_id and existing_id in self._requests:
            return dict(self._requests[existing_id])
        request_id = f"skill-secret-{hashlib.sha256(replay_key.encode()).hexdigest()[:16]}"
        request = {
            "id": request_id,
            "version": 1,
            "type": "skill_secret",
            "session_id": session_id,
            "query_id": query_id,
            "run_id": run_id,
            "goal_id": goal_id or None,
            "goal_revision": goal_revision,
            "tool_call_id": tool_call_id,
            "replay_key": replay_key,
            "status": "pending",
            "created_at": time.time(),
            **payload,
        }
        self._requests[request_id] = request
        self._by_replay_key[replay_key] = request_id
        self._pending[request_id] = asyncio.get_running_loop().create_future()
        return dict(request)

    async def wait(self, request_id: str) -> dict[str, Any]:
        future = self._pending.get(request_id)
        request = self._requests.get(request_id)
        if future is None:
            decision = request.get("decision") if request else None
            return dict(decision) if isinstance(decision, dict) else {"action": "cancel"}
        try:
            return await future
        finally:
            self._pending.pop(request_id, None)

    def resolve(self, request_id: str, decision: dict[str, Any]) -> tuple[dict[str, Any] | None, bool]:
        request = self._requests.get(request_id)
        if request is None:
            return None, False
        action = str(decision.get("action") or "")
        if action not in {"configured", "cancel"}:
            raise ValueError("Skill Secret decision is invalid")
        normalized = {
            "action": action,
            "env_name": str(request.get("env_name") or ""),
        }
        if request.get("status") == "resolved":
            if request.get("decision") == normalized:
                return dict(normalized), False
            raise RuntimeError("Skill Secret request was already resolved differently")
        if request.get("status") != "pending":
            raise RuntimeError("Skill Secret request is no longer pending")
        future = self._pending.get(request_id)
        if future is None:
            raise RuntimeError("Skill Secret request no longer belongs to an active Run")
        request["status"] = "resolved"
        request["resolved_at"] = time.time()
        request["decision"] = normalized
        if not future.done():
            future.set_result(normalized)
        return dict(normalized), True

    def get(self, request_id: str) -> dict[str, Any] | None:
        request = self._requests.get(request_id)
        return dict(request) if request else None

    def reject_run(self, session_id: str, run_id: str, message: str) -> int:
        del message
        count = 0
        for request_id, request in self._requests.items():
            if (
                request.get("session_id") != session_id
                or request.get("run_id") != run_id
                or request.get("status") != "pending"
            ):
                continue
            self.resolve(request_id, {"action": "cancel"})
            count += 1
        return count

    def reject_session(self, session_id: str, message: str) -> int:
        del message
        count = 0
        for request_id, request in self._requests.items():
            if request.get("session_id") != session_id or request.get("status") != "pending":
                continue
            self.resolve(request_id, {"action": "cancel"})
            count += 1
        return count


skill_secret_resume_registry = SkillSecretResumeRegistry()
