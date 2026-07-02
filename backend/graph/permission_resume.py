"""In-process pending permission decisions for active Agent streams."""

from __future__ import annotations

import asyncio
import time
import uuid
from pathlib import Path
from typing import Any


class PermissionResumeRegistry:
    """Bridge permission API decisions back into active LangGraph streams."""

    def __init__(self) -> None:
        self._pending: dict[str, asyncio.Future[dict[str, Any]]] = {}
        self._requests: dict[str, dict[str, Any]] = {}

    def create_external_file_request(
        self,
        *,
        session_id: str,
        query_id: str,
        tool_call_id: str,
        path: Path,
    ) -> dict[str, Any]:
        request_id = f"perm-req-{uuid.uuid4().hex[:12]}"
        request = {
            "id": request_id,
            "type": "external_file_read",
            "session_id": session_id,
            "query_id": query_id,
            "tool_call_id": tool_call_id,
            "path": str(path),
            "target_kind": "exact_file",
            "capabilities": ["read", "external_path"],
            "status": "pending",
            "created_at": time.time(),
            "options": [
                "exact_file_session",
                "all_external_files_session",
            ],
        }
        self._requests[request_id] = request
        self._pending[request_id] = asyncio.get_running_loop().create_future()
        return dict(request)

    async def wait(self, request_id: str) -> dict[str, Any]:
        future = self._pending.get(request_id)
        if future is None:
            return {"type": "reject", "message": "Permission request is no longer active."}
        return await future

    def resolve(self, request_id: str, decision: dict[str, Any]) -> bool:
        future = self._pending.pop(request_id, None)
        request = self._requests.get(request_id)
        if request is not None:
            request["status"] = "resolved"
            request["resolved_at"] = time.time()
            request["decision"] = decision
        if future is None:
            return False
        if not future.done():
            future.set_result(decision)
        return True

    def get(self, request_id: str) -> dict[str, Any] | None:
        request = self._requests.get(request_id)
        return dict(request) if request else None


permission_resume_registry = PermissionResumeRegistry()
