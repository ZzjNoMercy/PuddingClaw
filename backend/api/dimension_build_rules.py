"""Resolve or cancel in-flight semantic-dimension rule HITL requests."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from graph.dimension_build_resume import dimension_build_resume_registry


router = APIRouter(prefix="/analytics/dimension-build-requests", tags=["analytics"])


class DimensionBindingDecision(BaseModel):
    candidate_id: str
    key_fields: list[str] = Field(min_length=1)
    output_fields: list[str] = Field(min_length=1)
    # Keep the source-routing decision selected in the HITL card. Without these
    # fields Pydantic drops them, causing a selected "append" to become "new".
    source_id: str = ""
    source_name: str = ""
    source_mode: str = "new"


class ResolveDimensionBuildRequest(BaseModel):
    action: str = Field(pattern="^(confirm|cancel)$")
    canonical_candidate_id: str = ""
    bindings: list[DimensionBindingDecision] = Field(default_factory=list)
    conflict_policy: str = "candidate"


@router.get("/{request_id}")
async def get_dimension_build_request(request_id: str) -> dict[str, Any]:
    request = dimension_build_resume_registry.get(request_id)
    if request is None:
        raise HTTPException(status_code=404, detail="Dimension build request not found")
    return {"request": request}


@router.post("/{request_id}/resolve")
async def resolve_dimension_build_request(
    request_id: str,
    body: ResolveDimensionBuildRequest,
) -> dict[str, Any]:
    try:
        decision = dimension_build_resume_registry.resolve(request_id, body.model_dump())
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if decision is None:
        raise HTTPException(status_code=404, detail="Dimension build request not found or no longer pending")
    return {"request_id": request_id, "decision": decision, "resumed": True}
