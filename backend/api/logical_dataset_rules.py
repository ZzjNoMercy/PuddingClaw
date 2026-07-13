"""Resolve in-flight logical dataset merge-rule HITL requests."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from graph.logical_dataset_resume import logical_dataset_resume_registry


router = APIRouter(prefix="/analytics/logical-dataset-requests", tags=["analytics"])


class ResolveLogicalDatasetRequest(BaseModel):
    action: str = Field(pattern="^(confirm|cancel)$")
    name: str = ""
    description: str = ""
    tags: list[str] = Field(default_factory=list)
    baseline_asset_id: str = ""
    source_asset_ids: list[str] = Field(default_factory=list)
    schema_mode: str = "strict"
    preferred_intents: list[str] = Field(default_factory=list)
    direct_source_allowed: bool = True


@router.get("/{request_id}")
async def get_logical_dataset_request(request_id: str) -> dict[str, Any]:
    request = logical_dataset_resume_registry.get(request_id)
    if request is None:
        raise HTTPException(status_code=404, detail="Logical dataset request not found")
    return {"request": dict(request)}


@router.post("/{request_id}/resolve")
async def resolve_logical_dataset_request(request_id: str, body: ResolveLogicalDatasetRequest) -> dict[str, Any]:
    try:
        decision = logical_dataset_resume_registry.resolve(request_id, body.model_dump())
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if decision is None:
        raise HTTPException(status_code=404, detail="Logical dataset request not found or no longer pending")
    return {"request_id": request_id, "decision": decision, "resumed": True}
