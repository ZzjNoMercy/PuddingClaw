"""Adapt persisted database results into generic immutable SourceReferences."""

from __future__ import annotations

import asyncio
import hashlib
import json

from langchain.tools import ToolRuntime
from langchain_core.tools import BaseTool
from pydantic import BaseModel, ConfigDict

from analytics.nl2sql.result_store import (
    QueryResultStoreError,
    get_query_result_source_contract,
)
from db import get_sessionmaker
from harness.source_materialization import (
    SourceMaterializationError,
    public_source_reference,
    register_file_source_reference,
)

from .models import DatabaseQueryResultSourceInput


class DatabaseQueryResultSourceTool(BaseTool):
    """Produce a source handle; materialization remains provider-independent."""

    name: str = "database_query_result_source"
    description: str = (
        "Register a persisted database result_id as an immutable SourceReference. "
        "Use the returned source_ref with materialize_source_ref to write all rows "
        "directly to a file or typed template slot without paging the payload through "
        "model context. This tool only creates the source handle and does not write a "
        "user artifact."
    )
    args_schema: type[BaseModel] = DatabaseQueryResultSourceInput
    risk_level: str = "safe"
    session_id: str = ""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    async def _arun(
        self,
        result_id: str,
        runtime: ToolRuntime | None = None,
    ) -> str:
        context = getattr(runtime, "context", None)
        context = context if isinstance(context, dict) else {}
        session_id = str(context.get("session_id") or self.session_id or "")
        if not session_id:
            return json.dumps(
                {
                    "status": "error",
                    "error_code": "active_session_required",
                    "next_action": "retry_in_active_session",
                },
                ensure_ascii=False,
                sort_keys=True,
            )
        try:
            sessionmaker = get_sessionmaker()
            async with sessionmaker() as session:
                contract = await get_query_result_source_contract(
                    session,
                    result_id,
                    session_id=session_id,
                )
            columns = [str(item) for item in contract.get("columns") or []]
            schema_seed = json.dumps(
                {"columns": columns, "format": "jsonl"},
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            schema_ref = (
                "schema:database-result:"
                + hashlib.sha256(schema_seed.encode("utf-8")).hexdigest()[:24]
            )
            source = register_file_source_reference(
                session_id=session_id,
                kind="database_result",
                file_path=str(contract["artifact_path"]),
                media_type="application/x-ndjson",
                schema_ref=schema_ref,
                row_count=int(contract.get("row_count") or 0),
                producer_receipt_ids=[
                    *list(contract.get("producer_receipt_ids") or []),
                    f"result-store:{result_id}",
                ],
                expires_at=str(contract.get("expires_at") or ""),
                source_ref=f"source-db-{result_id}",
                metadata={
                    "result_id": result_id,
                    "columns": columns,
                    "source_query_id": str(contract.get("source_query_id") or ""),
                    "source_run_id": str(contract.get("source_run_id") or ""),
                },
            )
        except (QueryResultStoreError, SourceMaterializationError, ValueError) as exc:
            error_code = getattr(exc, "code", "query_result_source_invalid")
            next_action = getattr(exc, "next_action", "rerun_database_query")
            return json.dumps(
                {
                    "status": "error",
                    "error_code": error_code,
                    "error": str(exc),
                    "next_action": next_action,
                },
                ensure_ascii=False,
                sort_keys=True,
            )
        except Exception as exc:
            return json.dumps(
                {
                    "status": "error",
                    "error_code": "query_result_source_unavailable",
                    "error": f"{type(exc).__name__}: {exc}",
                    "next_action": "retry_or_rerun_database_query",
                },
                ensure_ascii=False,
                sort_keys=True,
            )
        return json.dumps(
            {
                "status": "completed",
                "source": public_source_reference(source),
                "next_action": "call_materialize_source_ref",
            },
            ensure_ascii=False,
            sort_keys=True,
        )

    def _run(
        self,
        result_id: str,
        runtime: ToolRuntime | None = None,
    ) -> str:
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(self._arun(result_id=result_id, runtime=runtime))
        return json.dumps(
            {
                "status": "error",
                "error_code": "sync_call_in_async_runtime",
                "next_action": "use_async_tool_call",
            },
            sort_keys=True,
        )


__all__ = ["DatabaseQueryResultSourceTool"]
