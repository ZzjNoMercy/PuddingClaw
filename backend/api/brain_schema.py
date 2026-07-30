"""LLM Wiki Schema Bundle API."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel, Field, ValidationError

from knowledge.brain_schema import BrainSchemaError, SchemaPackManifest, get_brain_schema_service

router = APIRouter(prefix="/knowledge/brain", tags=["knowledge-brain"])
BASE_DIR = Path(__file__).resolve().parent.parent


class CustomPackPayload(BaseModel):
    manifest: SchemaPackManifest


class SaveCustomPackPayload(CustomPackPayload):
    expected_sha256: str = Field(min_length=64, max_length=64)
    expected_bundle_hash: str = Field(min_length=64, max_length=64)


def _service():
    return get_brain_schema_service(BASE_DIR)


def _http_error(exc: Exception, *, conflict: bool = False) -> HTTPException:
    if isinstance(exc, ValidationError):
        return HTTPException(status_code=422, detail=exc.errors(include_url=False))
    message = str(exc)
    if "changed since" in message:
        conflict = True
    return HTTPException(status_code=409 if conflict else 400, detail=message)


@router.get("/schema/catalog")
async def get_schema_catalog() -> dict[str, Any]:
    try:
        return await run_in_threadpool(_service().catalog)
    except (BrainSchemaError, ValidationError, OSError) as exc:
        raise _http_error(exc) from exc


@router.post("/initialize")
async def initialize_brain() -> dict[str, Any]:
    try:
        return await run_in_threadpool(_service().initialize)
    except (BrainSchemaError, ValidationError, OSError) as exc:
        raise _http_error(exc) from exc


@router.get("/schema/bundle")
async def get_schema_bundle() -> dict[str, Any]:
    try:
        return await run_in_threadpool(_service().bundle)
    except BrainSchemaError as exc:
        if "not been initialized" in str(exc):
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        raise _http_error(exc) from exc
    except (ValidationError, OSError) as exc:
        raise _http_error(exc) from exc


@router.post("/schema/custom/preview")
async def preview_custom_pack(payload: CustomPackPayload) -> dict[str, Any]:
    try:
        return await run_in_threadpool(
            _service().preview_custom,
            payload.manifest.model_dump(mode="json", exclude_none=True),
            validate_official=False,
        )
    except (BrainSchemaError, ValidationError, OSError) as exc:
        raise _http_error(exc) from exc


@router.put("/schema/custom")
async def save_custom_pack(payload: SaveCustomPackPayload) -> dict[str, Any]:
    try:
        return await run_in_threadpool(
            _service().save_custom,
            payload.manifest.model_dump(mode="json", exclude_none=True),
            expected_sha256=payload.expected_sha256,
            expected_bundle_hash=payload.expected_bundle_hash,
        )
    except (BrainSchemaError, ValidationError, OSError) as exc:
        raise _http_error(exc) from exc
