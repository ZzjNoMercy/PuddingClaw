"""In-process generation registry and HITL bridge for database SQL revisions."""

from __future__ import annotations

import asyncio
import hashlib
import time
import uuid
from dataclasses import dataclass
from typing import Any

from analytics.nl2sql.schemas import (
    DatabaseSqlGenerationResult,
    TableRoute,
    to_plain_dict,
)


@dataclass(slots=True)
class RegisteredDatabaseSqlGeneration:
    id: str
    session_id: str
    query_id: str
    run_id: str
    goal_id: str
    goal_revision: int | None
    result: DatabaseSqlGenerationResult
    request: dict[str, Any]
    parent_generation_id: str = ""
    revision_instruction: str = ""
    sql_sha256: str = ""
    created_at: float = 0.0


@dataclass(slots=True)
class DatabaseSqlValidationReceipt:
    id: str
    session_id: str
    query_id: str
    run_id: str
    goal_id: str
    goal_revision: int | None
    generation_id: str
    sql_sha256: str
    database_source_id: str
    allowed_tables: list[str]
    semantic_validation_status: str = "passed"
    semantic_guardrail_ids: list[str] | None = None
    semantic_evidence_refs: list[str] | None = None
    validator_version: str = "readonly+semantic-guardrails/v2"
    created_at: float = 0.0


def _sql_sha256(sql: str) -> str:
    return f"sha256:{hashlib.sha256(sql.encode()).hexdigest()}"


class DatabaseSqlRevisionResumeRegistry:
    def __init__(self) -> None:
        self._generations: dict[str, RegisteredDatabaseSqlGeneration] = {}
        self._validation_receipts: dict[str, DatabaseSqlValidationReceipt] = {}
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
        run_id: str = "",
        goal_id: str = "",
        goal_revision: int | None = None,
        parent_generation_id: str = "",
        revision_instruction: str = "",
    ) -> RegisteredDatabaseSqlGeneration:
        generation_id = f"sql-gen-{uuid.uuid4().hex[:12]}"
        generation = RegisteredDatabaseSqlGeneration(
            id=generation_id,
            session_id=session_id,
            query_id=query_id,
            run_id=run_id,
            goal_id=goal_id,
            goal_revision=goal_revision,
            result=result,
            request=dict(request),
            parent_generation_id=parent_generation_id,
            revision_instruction=revision_instruction,
            sql_sha256=_sql_sha256(result.sql),
            created_at=time.time(),
        )
        self._generations[generation_id] = generation
        if session_id:
            try:
                from graph.session_manager import session_manager

                if session_manager.is_initialized:
                    session_manager.record_sql_generation(
                        session_id,
                        generation_id,
                        self._serialize_generation(generation),
                    )
            except (FileNotFoundError, RuntimeError, ValueError):
                # Unit-level and bootstrap callers may intentionally use the
                # registry before Session persistence is initialized.
                pass
        return generation

    def get_generation(
        self,
        generation_id: str,
        *,
        session_id: str = "",
        run_id: str = "",
        goal_id: str = "",
        goal_revision: int | None = None,
    ) -> RegisteredDatabaseSqlGeneration | None:
        generation = self._generations.get(generation_id)
        if generation is None and session_id:
            try:
                from graph.session_manager import session_manager

                payload = (
                    session_manager.get_sql_generation(session_id, generation_id)
                    if session_manager.is_initialized
                    else None
                )
                if isinstance(payload, dict):
                    generation = self._deserialize_generation(payload)
                    self._generations[generation_id] = generation
            except (FileNotFoundError, RuntimeError, ValueError):
                generation = None
        if generation is None:
            return None
        if session_id and generation.session_id and generation.session_id != session_id:
            return None
        if not self._scope_matches(
            generation.run_id,
            generation.goal_id,
            generation.goal_revision,
            run_id=run_id,
            goal_id=goal_id,
            goal_revision=goal_revision,
        ):
            return None
        return generation

    def list_generations(
        self,
        *,
        session_id: str,
        run_id: str = "",
        goal_id: str = "",
        goal_revision: int | None = None,
        created_after: float = 0.0,
    ) -> list[RegisteredDatabaseSqlGeneration]:
        """List already-registered authorities visible to one Run/Goal.

        This is intentionally an ID/metadata recovery path for timeout
        handoffs. It never exposes SQL text to the parent Agent.
        """

        return sorted(
            (
                item
                for item in self._generations.values()
                if item.created_at >= created_after
                and self.get_generation(
                    item.id,
                    session_id=session_id,
                    run_id=run_id,
                    goal_id=goal_id,
                    goal_revision=goal_revision,
                )
                is not None
            ),
            key=lambda item: item.created_at,
        )

    @staticmethod
    def _scope_matches(
        authority_run_id: str,
        authority_goal_id: str,
        authority_goal_revision: int | None,
        *,
        run_id: str,
        goal_id: str,
        goal_revision: int | None,
    ) -> bool:
        if not run_id and not goal_id:
            return True
        if authority_goal_id:
            return (
                authority_goal_id == goal_id
                and int(authority_goal_revision or 1) == int(goal_revision or 1)
            )
        return bool(authority_run_id and authority_run_id == run_id and not goal_id)

    @staticmethod
    def _serialize_generation(
        generation: RegisteredDatabaseSqlGeneration,
    ) -> dict[str, Any]:
        return {
            "id": generation.id,
            "session_id": generation.session_id,
            "query_id": generation.query_id,
            "run_id": generation.run_id,
            "goal_id": generation.goal_id,
            "goal_revision": generation.goal_revision,
            "result": to_plain_dict(generation.result),
            "request": dict(generation.request),
            "parent_generation_id": generation.parent_generation_id,
            "revision_instruction": generation.revision_instruction,
            "sql_sha256": generation.sql_sha256,
            "created_at": generation.created_at,
        }

    @staticmethod
    def _deserialize_generation(payload: dict[str, Any]) -> RegisteredDatabaseSqlGeneration:
        return RegisteredDatabaseSqlGeneration(
            id=str(payload["id"]),
            session_id=str(payload.get("session_id") or ""),
            query_id=str(payload.get("query_id") or ""),
            run_id=str(payload.get("run_id") or ""),
            goal_id=str(payload.get("goal_id") or ""),
            goal_revision=payload.get("goal_revision"),
            result=DatabaseSqlGenerationResult(
                **{
                    **dict(payload["result"]),
                    "route": TableRoute(**dict(payload["result"]["route"])),
                }
            ),
            request=dict(payload.get("request") or {}),
            parent_generation_id=str(payload.get("parent_generation_id") or ""),
            revision_instruction=str(payload.get("revision_instruction") or ""),
            sql_sha256=str(payload.get("sql_sha256") or ""),
            created_at=float(payload.get("created_at") or 0),
        )

    def register_validation_receipt(
        self,
        *,
        generation: RegisteredDatabaseSqlGeneration,
        database_source_id: str,
        allowed_tables: list[str],
        semantic_guardrail_ids: list[str] | None = None,
        semantic_evidence_refs: list[str] | None = None,
    ) -> DatabaseSqlValidationReceipt:
        digest = hashlib.sha256(
            (
                f"semantic-v2:{generation.session_id}:{generation.id}:{generation.sql_sha256}:"
                + ",".join(sorted(allowed_tables))
            ).encode()
        ).hexdigest()[:20]
        receipt = DatabaseSqlValidationReceipt(
            id=f"sql-validation-{digest}",
            session_id=generation.session_id,
            query_id=generation.query_id,
            run_id=generation.run_id,
            goal_id=generation.goal_id,
            goal_revision=generation.goal_revision,
            generation_id=generation.id,
            sql_sha256=generation.sql_sha256,
            database_source_id=database_source_id,
            allowed_tables=sorted(set(allowed_tables)),
            semantic_guardrail_ids=sorted(set(semantic_guardrail_ids or [])),
            semantic_evidence_refs=sorted(set(semantic_evidence_refs or [])),
            created_at=time.time(),
        )
        self._validation_receipts[receipt.id] = receipt
        if receipt.session_id:
            try:
                from graph.session_manager import session_manager

                if session_manager.is_initialized:
                    session_manager.record_sql_validation_receipt(
                        receipt.session_id,
                        receipt.id,
                        self._serialize_validation_receipt(receipt),
                    )
            except (FileNotFoundError, RuntimeError, ValueError):
                pass
        return receipt

    def get_validation_receipt(
        self,
        receipt_id: str,
        *,
        session_id: str,
        run_id: str = "",
        goal_id: str = "",
        goal_revision: int | None = None,
    ) -> DatabaseSqlValidationReceipt | None:
        receipt = self._validation_receipts.get(receipt_id)
        if receipt is None and session_id:
            try:
                from graph.session_manager import session_manager

                payload = (
                    session_manager.get_sql_validation_receipt(session_id, receipt_id)
                    if session_manager.is_initialized
                    else None
                )
                if isinstance(payload, dict):
                    receipt = DatabaseSqlValidationReceipt(
                        id=str(payload["id"]),
                        session_id=str(payload.get("session_id") or ""),
                        query_id=str(payload.get("query_id") or ""),
                        run_id=str(payload.get("run_id") or ""),
                        goal_id=str(payload.get("goal_id") or ""),
                        goal_revision=payload.get("goal_revision"),
                        generation_id=str(payload.get("generation_id") or ""),
                        sql_sha256=str(payload.get("sql_sha256") or ""),
                        database_source_id=str(payload.get("database_source_id") or ""),
                        allowed_tables=list(payload.get("allowed_tables") or []),
                        semantic_validation_status=str(
                            payload.get("semantic_validation_status") or "legacy_unverified"
                        ),
                        semantic_guardrail_ids=list(payload.get("semantic_guardrail_ids") or []),
                        semantic_evidence_refs=list(payload.get("semantic_evidence_refs") or []),
                        validator_version=str(payload.get("validator_version") or "readonly-sql/v1"),
                        created_at=float(payload.get("created_at") or 0),
                    )
                    self._validation_receipts[receipt.id] = receipt
            except (FileNotFoundError, RuntimeError, ValueError):
                receipt = None
        if receipt is None or receipt.session_id != session_id:
            return None
        if not self._scope_matches(
            receipt.run_id,
            receipt.goal_id,
            receipt.goal_revision,
            run_id=run_id,
            goal_id=goal_id,
            goal_revision=goal_revision,
        ):
            return None
        return receipt

    def list_validation_receipts(
        self,
        *,
        session_id: str,
        run_id: str = "",
        goal_id: str = "",
        goal_revision: int | None = None,
        created_after: float = 0.0,
    ) -> list[DatabaseSqlValidationReceipt]:
        """List Receipt IDs available to a timeout handoff without SQL prose."""

        return sorted(
            (
                item
                for item in self._validation_receipts.values()
                if item.created_at >= created_after
                and self.get_validation_receipt(
                    item.id,
                    session_id=session_id,
                    run_id=run_id,
                    goal_id=goal_id,
                    goal_revision=goal_revision,
                )
                is not None
            ),
            key=lambda item: item.created_at,
        )

    @staticmethod
    def _serialize_validation_receipt(
        receipt: DatabaseSqlValidationReceipt,
    ) -> dict[str, Any]:
        return {
            "id": receipt.id,
            "session_id": receipt.session_id,
            "query_id": receipt.query_id,
            "run_id": receipt.run_id,
            "goal_id": receipt.goal_id,
            "goal_revision": receipt.goal_revision,
            "generation_id": receipt.generation_id,
            "sql_sha256": receipt.sql_sha256,
            "database_source_id": receipt.database_source_id,
            "allowed_tables": list(receipt.allowed_tables),
            "semantic_validation_status": receipt.semantic_validation_status,
            "semantic_guardrail_ids": list(receipt.semantic_guardrail_ids or []),
            "semantic_evidence_refs": list(receipt.semantic_evidence_refs or []),
            "validator_version": receipt.validator_version,
            "created_at": receipt.created_at,
        }

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
