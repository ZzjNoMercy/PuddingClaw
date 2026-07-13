"""In-process generation registry and HITL bridge for database SQL revisions."""

from __future__ import annotations

import asyncio
import time
import uuid
from dataclasses import dataclass
from typing import Any

from analytics.nl2sql.schemas import DatabaseSqlGenerationResult


@dataclass(slots=True)
class RegisteredDatabaseSqlGeneration:
    id: str
    session_id: str
    query_id: str
    result: DatabaseSqlGenerationResult
    request: dict[str, Any]
    parent_generation_id: str = ""
    revision_instruction: str = ""


class DatabaseSqlRevisionResumeRegistry:
    def __init__(self) -> None:
        self._generations: dict[str, RegisteredDatabaseSqlGeneration] = {}
        self._pending: dict[str, asyncio.Future[dict[str, Any]]] = {}
        self._requests: dict[str, dict[str, Any]] = {}
        self._revision_keys: dict[tuple[str, str, str, str, str], str] = {}

    def register_generation(
        self,
        *,
        session_id: str,
        query_id: str,
        result: DatabaseSqlGenerationResult,
        request: dict[str, Any],
        parent_generation_id: str = "",
        revision_instruction: str = "",
    ) -> RegisteredDatabaseSqlGeneration:
        generation_id = f"sql-gen-{uuid.uuid4().hex[:12]}"
        generation = RegisteredDatabaseSqlGeneration(
            id=generation_id,
            session_id=session_id,
            query_id=query_id,
            result=result,
            request=dict(request),
            parent_generation_id=parent_generation_id,
            revision_instruction=revision_instruction,
        )
        self._generations[generation_id] = generation
        return generation

    def get_generation(
        self,
        generation_id: str,
        *,
        session_id: str = "",
    ) -> RegisteredDatabaseSqlGeneration | None:
        generation = self._generations.get(generation_id)
        if generation is None:
            return None
        if session_id and generation.session_id and generation.session_id != session_id:
            return None
        return generation

    def create_revision_request(
        self,
        *,
        generation: RegisteredDatabaseSqlGeneration,
        proposed_revision_instruction: str,
        tool_call_id: str,
        query_id: str = "",
    ) -> dict[str, Any]:
        effective_query_id = query_id or generation.query_id
        replay_key = (
            generation.session_id,
            effective_query_id,
            generation.id,
            proposed_revision_instruction,
            tool_call_id,
        )
        existing_id = self._revision_keys.get(replay_key)
        if existing_id:
            existing = self._requests.get(existing_id)
            if existing is not None:
                return dict(existing)
        request_id = f"sql-revision-{uuid.uuid4().hex[:12]}"
        semantic_assets = generation.result.semantic_assets
        request = {
            "id": request_id,
            "type": "database_sql_revision",
            "session_id": generation.session_id,
            "query_id": effective_query_id,
            "tool_call_id": tool_call_id,
            "status": "pending",
            "created_at": time.time(),
            "generation_id": generation.id,
            "original_question": generation.request.get("question") or generation.result.question,
            "original_sql": generation.result.sql,
            "proposed_revision_instruction": proposed_revision_instruction,
            "semantic_assets": {
                "matched": semantic_assets.get("matched", []),
                "references": semantic_assets.get("references", []),
            },
        }
        self._requests[request_id] = request
        self._revision_keys[replay_key] = request_id
        self._pending[request_id] = asyncio.get_running_loop().create_future()
        return dict(request)

    async def wait(self, request_id: str) -> dict[str, Any]:
        future = self._pending.get(request_id)
        if future is None:
            return {"action": "reject", "message": "SQL revision request is no longer active."}
        try:
            return await future
        finally:
            self._pending.pop(request_id, None)

    def resolve(self, request_id: str, decision: dict[str, Any]) -> dict[str, Any] | None:
        request = self._requests.get(request_id)
        future = self._pending.get(request_id)
        if request is None or future is None or request.get("status") != "pending":
            return None

        action = str(decision.get("action") or "")
        if action not in {"agree", "reject", "modify"}:
            raise ValueError("action 必须是 agree、reject 或 modify")
        if action == "agree":
            normalized = {
                "action": "agree",
                "revision_instruction": request["proposed_revision_instruction"],
            }
        elif action == "modify":
            instruction = str(decision.get("revision_instruction") or "").strip()
            if not instruction:
                raise ValueError("修改时必须填写自然语言补充说明")
            normalized = {"action": "modify", "revision_instruction": instruction}
        else:
            normalized = {"action": "reject"}

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
                if self.resolve(request_id, {"action": "reject", "message": message}) is not None:
                    count += 1
        return count

    def get(self, request_id: str) -> dict[str, Any] | None:
        item = self._requests.get(request_id)
        return dict(item) if item else None


database_sql_revision_resume_registry = DatabaseSqlRevisionResumeRegistry()
