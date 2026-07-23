"""Read-only access to immutable cross-Run Evidence."""

from __future__ import annotations

import json
from typing import Any

from langchain_core.tools import BaseTool
from pydantic import BaseModel, Field

from graph.session_manager import session_manager
from graph.trace_collector import get_current_trace_collector


class ReadEvidenceInput(BaseModel):
    evidence_id: str = Field(description="Stable evidence-* identifier from historical context")
    offset: int = Field(default=0, ge=0, description="Character offset for text evidence")
    limit: int = Field(default=20_000, ge=1, le=100_000, description="Maximum text characters")
    page: int | None = Field(default=None, ge=1, description="1-based page for SQL JSONL evidence")
    page_size: int | None = Field(default=None, ge=1, le=500, description="Rows per SQL page")


class ReadEvidenceTool(BaseTool):
    name: str = "read_evidence"
    description: str = (
        "Read saved historical Evidence by evidence_id without rerunning the original tool, "
        "network request, or SQL query. Supports text offset/limit and SQL page/page_size."
    )
    args_schema: type[BaseModel] = ReadEvidenceInput
    risk_level: str = "safe"
    session_id: str = ""
    workspace_path: str = ""

    def _run(
        self,
        evidence_id: str,
        offset: int = 0,
        limit: int = 20_000,
        page: int | None = None,
        page_size: int | None = None,
    ) -> str:
        if not self.session_id:
            return json.dumps(
                {"evidence_id": evidence_id, "status": "session_unavailable"},
                ensure_ascii=False,
            )
        payload: dict[str, Any] = session_manager.read_evidence(
            self.session_id,
            evidence_id,
            workspace_path=self.workspace_path or None,
            offset=offset,
            limit=limit,
            page=page,
            page_size=page_size,
        )
        if payload.get("status") in {
            "expired",
            "missing",
            "not_found",
            "hash_mismatch",
            "invalid_locator",
            "catalog_missing",
            "unauthorized",
            "corrupt",
            "workspace_unavailable",
            "store_unavailable",
        }:
            collector = get_current_trace_collector()
            if collector is not None:
                collector.add_custom_span(
                    "evidence.warning",
                    {
                        "evidence_id": evidence_id,
                        "status": payload.get("status"),
                        "kind": (payload.get("raw_output_ref") or {}).get("kind"),
                    },
                    span_type="evidence",
                    metadata={"warning": True},
                )
        return json.dumps(payload, ensure_ascii=False, default=str)


def create_read_evidence_tool() -> ReadEvidenceTool:
    return ReadEvidenceTool()
