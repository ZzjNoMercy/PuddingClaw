"""PuddingClaw Agent boundary for LLM Wiki Ingest, Query, and Lint."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Query
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel, ConfigDict, Field

from knowledge.llm_wiki import LlmWikiError, get_llm_wiki_service

router = APIRouter(prefix="/knowledge/brain/wiki", tags=["llm-wiki"])
BASE_DIR = Path(__file__).resolve().parent.parent


class StrictPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")


class RawSnapshotPayload(StrictPayload):
    source_id: str = Field(min_length=1, max_length=200)
    asset_id: str = Field(min_length=1, max_length=200)
    title: str = Field(min_length=1, max_length=500)
    content: str = Field(min_length=1, max_length=10_000_000)
    source_path: str | None = None


class WikiPagePayload(StrictPayload):
    slug: str = Field(min_length=1, max_length=160)
    content: str = Field(min_length=1, max_length=2_000_000)


class WikiPublishPayload(StrictPayload):
    pages: list[WikiPagePayload] = Field(min_length=1)
    expected_bundle_hash: str = Field(min_length=64, max_length=64)
    summary: str = Field(min_length=1, max_length=500)
    model: str = Field(min_length=1, max_length=200)
    raw_paths: list[str] = Field(default_factory=list)


class WikiQueryPayload(StrictPayload):
    question: str = Field(min_length=1, max_length=4000)
    limit: int = Field(default=6, ge=1, le=20)


class GbrainCompilePayload(StrictPayload):
    import_pages: bool = False


def _service():
    return get_llm_wiki_service(BASE_DIR)


def _bad_request(exc: Exception) -> HTTPException:
    message = str(exc)
    status = 409 if "changed since" in message or "changed while" in message else 400
    return HTTPException(status_code=status, detail=message)


@router.post("/raw")
async def snapshot_raw(payload: RawSnapshotPayload) -> dict[str, Any]:
    try:
        return await run_in_threadpool(_service().snapshot_raw, **payload.model_dump())
    except (LlmWikiError, OSError) as exc:
        raise _bad_request(exc) from exc


@router.get("/context/{operation}")
async def operation_context(
    operation: Literal["ingest", "query", "lint"],
    raw_path: list[str] | None = Query(default=None),
) -> dict[str, Any]:
    try:
        return await run_in_threadpool(_service().operation_context, operation, raw_paths=raw_path)
    except (LlmWikiError, OSError) as exc:
        raise _bad_request(exc) from exc


@router.post("/publish")
async def publish_wiki(payload: WikiPublishPayload) -> dict[str, Any]:
    try:
        return await run_in_threadpool(
            _service().publish,
            pages=[item.model_dump() for item in payload.pages],
            expected_bundle_hash=payload.expected_bundle_hash,
            summary=payload.summary,
            model=payload.model,
            raw_paths=payload.raw_paths,
        )
    except (LlmWikiError, OSError) as exc:
        raise _bad_request(exc) from exc


@router.get("/lint")
async def lint_wiki() -> dict[str, Any]:
    try:
        return await run_in_threadpool(_service().lint)
    except (LlmWikiError, OSError) as exc:
        raise _bad_request(exc) from exc


@router.post("/query")
async def query_wiki(payload: WikiQueryPayload) -> dict[str, Any]:
    try:
        return await run_in_threadpool(_service().query, payload.question, limit=payload.limit)
    except (LlmWikiError, OSError) as exc:
        raise _bad_request(exc) from exc


@router.post("/compile")
async def compile_gbrain(payload: GbrainCompilePayload) -> dict[str, Any]:
    try:
        return await run_in_threadpool(_service().compile_gbrain, import_pages=payload.import_pages)
    except (LlmWikiError, OSError) as exc:
        raise _bad_request(exc) from exc
