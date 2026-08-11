"""Server-authoritative decisions for a Kernel -> Spawn fallback.

This is deliberately separate from ordinary permission grants.  A fallback
changes the execution boundary of a Run and therefore must be explicit,
replay-safe, and bound to the exact Kernel probe that failed.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import sys
import time
from typing import Any


class KernelFallbackResumeRegistry:
    _ACTIONS = {"switch_project_to_spawn", "fallback_once", "reject"}

    def __init__(self) -> None:
        self._pending: dict[str, asyncio.Future[dict[str, Any]]] = {}
        self._requests: dict[str, dict[str, Any]] = {}
        self._by_replay_key: dict[str, str] = {}

    @staticmethod
    def _key(*, session_id: str, run_id: str, query_id: str, tool_call_id: str, probe_fingerprint: str) -> str:
        payload = json.dumps(
            [session_id, run_id, query_id, tool_call_id, probe_fingerprint],
            ensure_ascii=False,
            separators=(",", ":"),
        )
        return "sha256:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def create(
        self,
        *,
        session_id: str,
        run_id: str,
        query_id: str,
        goal_id: str = "",
        goal_revision: int | None = None,
        project_id: str | None = None,
        tool_call_id: str,
        workspace_identity: str,
        configured_mode: str,
        availability_class: str,
        reason_code: str,
        reason: str,
        probe_fingerprint: str,
        config_revision: int = 1,
    ) -> dict[str, Any]:
        if configured_mode != "kernel":
            raise ValueError("Kernel fallback can only be requested for configured kernel Runs")
        if availability_class not in {"stable", "transient"}:
            raise ValueError("availability_class must be stable or transient")
        replay_key = self._key(
            session_id=session_id,
            run_id=run_id,
            query_id=query_id,
            tool_call_id=tool_call_id,
            probe_fingerprint=probe_fingerprint,
        )
        existing_id = self._by_replay_key.get(replay_key)
        if existing_id and existing_id in self._requests:
            return dict(self._requests[existing_id])
        request_id = f"kernel-fallback-{hashlib.sha256(replay_key.encode()).hexdigest()[:16]}"
        request = {
            "id": request_id,
            "request_id": request_id,
            "version": 1,
            "type": "kernel_fallback",
            "session_id": session_id,
            "run_id": run_id,
            "query_id": query_id,
            "goal_id": goal_id or None,
            "goal_revision": goal_revision,
            "project_id": project_id,
            "tool_call_id": tool_call_id,
            "workspace_identity": workspace_identity,
            "configured_mode": configured_mode,
            "fallback_runner": "spawn",
            "platform": sys.platform,
            "availability_class": availability_class,
            "reason_code": reason_code,
            "reason": reason,
            "probe_fingerprint": probe_fingerprint,
            "config_revision": config_revision,
            "replay_key": replay_key,
            "status": "pending",
            "created_at": time.time(),
            "options": ["switch_project_to_spawn", "fallback_once", "reject"],
        }
        self._requests[request_id] = request
        self._by_replay_key[replay_key] = request_id
        self._pending[request_id] = asyncio.get_running_loop().create_future()
        return dict(request)

    async def wait(self, request_id: str) -> dict[str, Any]:
        future = self._pending.get(request_id)
        request = self._requests.get(request_id)
        if future is None:
            if request and isinstance(request.get("decision"), dict):
                return dict(request["decision"])
            return {"action": "reject", "reason": "kernel_fallback_request_inactive"}
        try:
            return await future
        finally:
            self._pending.pop(request_id, None)

    def resolve(self, request_id: str, action: str, *, request_version: int) -> tuple[dict[str, Any] | None, bool]:
        request = self._requests.get(request_id)
        if request is None:
            return None, False
        if int(request.get("version") or 0) != request_version:
            raise RuntimeError("Kernel fallback request version is stale")
        if action not in self._ACTIONS:
            raise ValueError("action must be switch_project_to_spawn, fallback_once, or reject")
        decision = {"action": action}
        if request.get("status") == "resolved":
            previous = request.get("decision")
            if previous == decision:
                return dict(previous), False
            raise RuntimeError("Kernel fallback request was resolved with a different action")
        if request.get("status") != "pending":
            raise RuntimeError("Kernel fallback request is no longer pending")
        future = self._pending.get(request_id)
        if future is None:
            raise RuntimeError("Kernel fallback Run is no longer active")
        request["status"] = "resolved"
        request["resolved_at"] = time.time()
        request["decision"] = decision
        if not future.done():
            future.set_result(decision)
        return dict(decision), True

    def reject_run(self, session_id: str, run_id: str, reason: str) -> int:
        count = 0
        for request_id, request in self._requests.items():
            if request.get("session_id") != session_id or request.get("run_id") != run_id:
                continue
            if request.get("status") != "pending":
                continue
            decision = {"action": "reject", "reason": reason}
            request["status"] = "cancelled"
            request["resolved_at"] = time.time()
            request["decision"] = decision
            future = self._pending.get(request_id)
            if future is not None and not future.done():
                future.set_result(decision)
            count += 1
        return count

    def get(self, request_id: str) -> dict[str, Any] | None:
        request = self._requests.get(request_id)
        return dict(request) if request else None


kernel_fallback_resume_registry = KernelFallbackResumeRegistry()
