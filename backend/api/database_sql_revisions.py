"""Resolve in-flight natural-language database SQL revision requests."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from graph.database_sql_revision_resume import database_sql_revision_resume_registry

router = APIRouter(prefix="/analytics/database-sql-revision-requests", tags=["analytics"])


class ResolveDatabaseSqlRevisionRequest(BaseModel):
    action: str = Field(pattern="^(agree|reject|modify)$")
    revision_instruction: str = ""


@router.get("/{request_id}")
async def get_database_sql_revision_request(request_id: str) -> dict[str, Any]:
    request = database_sql_revision_resume_registry.get(request_id)
    if request is None:
        raise HTTPException(status_code=404, detail="Database SQL revision request not found")
    return {"request": request}


@router.post("/{request_id}/resolve")
async def resolve_database_sql_revision_request(
    request_id: str,
    body: ResolveDatabaseSqlRevisionRequest,
) -> dict[str, Any]:
    try:
        decision = database_sql_revision_resume_registry.resolve(request_id, body.model_dump())
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if decision is None:
        raise HTTPException(status_code=404, detail="Database SQL revision request not found or no longer pending")
    return {"request_id": request_id, "decision": decision, "resumed": True}
