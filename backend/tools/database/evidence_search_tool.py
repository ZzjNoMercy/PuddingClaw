"""Agent-facing database evidence search tool."""

from __future__ import annotations

import asyncio
import json

from langchain.tools import ToolRuntime
from langchain_core.tools import BaseTool
from pydantic import BaseModel

from analytics.nl2sql.agent_path_policy import (
    classify_evidence_exception,
    fallback_policy,
    record_database_path_event,
)
from analytics.nl2sql.evidence_service import search_database_evidence
from tools.database.spans import emit_database_span

from .models import DatabaseEvidenceSearchInput
from .sql_generate_tool import _trusted_user_scope_text


class DatabaseEvidenceSearchTool(BaseTool):
    name: str = "database_evidence_search"
    description: str = (
        "Retrieve database evidence for an Agent business question: authorized tables, Vanna DDL/documentation/"
        "similar SQL references, entity candidates, and current EAV values. This tool never generates SQL, selects a "
        "business enum, registers a generation, calls a refinement LLM, or executes a query. Similar SQL is always "
        "reference_only; current values include revision and completeness metadata."
    )
    args_schema: type[BaseModel] = DatabaseEvidenceSearchInput
    risk_level: str = "safe"
    session_id: str = ""
    query_id: str = ""

    class Config:
        arbitrary_types_allowed = True

    async def _arun(
        self,
        question: str,
        database_source_id: str | None = None,
        table_names: list[str] | None = None,
        selected_semantic_asset_ids: list[str] | None = None,
        focus_fields: list[str] | None = None,
        entity_types: list[str] | None = None,
        include_similar_sql: bool = True,
        reference_top_k: int = 5,
        value_profile_limit: int = 50,
        runtime: ToolRuntime | None = None,
    ) -> str:
        try:
            # The Agent may focus/rephrase the retrieval query, but the
            # resulting receipt must remain bound to the server-owned user
            # objective rather than to Agent-authored text.
            trusted_question = _trusted_user_scope_text(runtime).strip() or question
            payload = await search_database_evidence(
                question=question,
                trusted_question=trusted_question,
                database_source_id=database_source_id,
                table_names=list(table_names or []),
                model_id=None,
                selected_semantic_asset_ids=list(selected_semantic_asset_ids or []),
                focus_fields=list(focus_fields or []),
                entity_types=list(entity_types or []),
                include_similar_sql=bool(include_similar_sql),
                reference_top_k=reference_top_k,
                value_profile_limit=value_profile_limit,
                session_id=self.session_id,
                query_id=self.query_id,
                runtime=runtime,
            )
            return json.dumps(payload, ensure_ascii=False, indent=2, default=str)
        except Exception as exc:
            runtime_context = getattr(runtime, "context", None)
            runtime_context = runtime_context if isinstance(runtime_context, dict) else {}
            error_code = classify_evidence_exception(exc)
            policy = fallback_policy(error_code)
            event = record_database_path_event(
                session_id=self.session_id,
                query_id=self.query_id,
                run_id=str(runtime_context.get("run_id") or ""),
                goal_id=str(runtime_context.get("goal_id") or ""),
                goal_revision=runtime_context.get("goal_revision"),
                event_type="fallback_blocked",
                error_code=error_code,
                metadata={
                    "error_type": type(exc).__name__,
                    "server_eligible": bool(policy["eligible"]),
                    "blocked_reason": "legacy_tool_not_exposed",
                },
            )
            emit_database_span(
                "fallback",
                {
                    "status": "blocked",
                    "from_path": "agent",
                    "to_path": policy["target_path"] or None,
                    "error_code": error_code,
                    "fallback_event_id": event["event_id"],
                },
                metadata={"agent_path": True},
            )
            return json.dumps(
                {
                    "status": "rejected",
                    "code": error_code,
                    "recoverable": False,
                    "stage": "routing",
                    "error_type": type(exc).__name__,
                    "message": str(exc),
                    "fallback": {
                        "available": False,
                        "tool": None,
                        "event_id": event["event_id"],
                        "server_eligible": bool(policy["eligible"]),
                        "blocked_reason": "legacy_tool_not_exposed",
                    },
                },
                ensure_ascii=False,
                indent=2,
            )

    def _run(self, **kwargs: object) -> str:
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(self._arun(**kwargs))  # type: ignore[arg-type]
        return json.dumps({"status": "rejected", "code": "async_only"}, ensure_ascii=False)
