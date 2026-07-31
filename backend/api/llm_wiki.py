"""PuddingClaw Agent boundary for LLM Wiki Ingest, Query, and Lint."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession

from db import get_db_session
from knowledge.import_jobs import create_llm_wiki_ingest_job, job_to_dict
from knowledge.llm_wiki import LlmWikiError, get_llm_wiki_service
from knowledge.service import DEFAULT_KNOWLEDGE_BASE_ID, KnowledgeServiceError

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


class GbrainInitializePayload(StrictPayload):
    database_url: str = Field(min_length=1, max_length=2000)


class WikiIngestJobPayload(StrictPayload):
    raw_paths: list[str] = Field(min_length=1, max_length=200)
    knowledge_base_id: str = Field(default=DEFAULT_KNOWLEDGE_BASE_ID, min_length=1, max_length=64)
    import_gbrain: bool = Field(
        default=False,
        description="Only continue into gbrain when explicitly requested; ordinary compilation stops at Wiki.",
    )


def _service():
    return get_llm_wiki_service(BASE_DIR)


def _bad_request(exc: Exception) -> HTTPException:
    message = str(exc)
    status = 409 if "changed since" in message or "changed while" in message else 400
    return HTTPException(status_code=status, detail=message)


@router.get("/status")
async def workspace_status() -> dict[str, Any]:
    try:
        return await run_in_threadpool(_service().workspace_status)
    except (LlmWikiError, OSError) as exc:
        raise _bad_request(exc) from exc


@router.post("/raw")
async def snapshot_raw(payload: RawSnapshotPayload) -> dict[str, Any]:
    try:
        return await run_in_threadpool(_service().snapshot_raw, **payload.model_dump())
    except (LlmWikiError, OSError) as exc:
        raise _bad_request(exc) from exc


@router.post("/ingest-jobs")
async def create_ingest_job(
    payload: WikiIngestJobPayload,
    session: AsyncSession = Depends(get_db_session),
) -> dict[str, Any]:
    """Queue a background Agent compile without creating a chat Session."""

    try:
        job = await create_llm_wiki_ingest_job(
            session,
            base_dir=BASE_DIR,
            raw_paths=payload.raw_paths,
            knowledge_base_id=payload.knowledge_base_id,
            import_gbrain=payload.import_gbrain,
        )
        return {"job": job_to_dict(job)}
    except (LlmWikiError, KnowledgeServiceError, OSError) as exc:
        await session.rollback()
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


@router.post("/migrations/workspace-prefix")
async def migrate_legacy_workspace_prefix() -> dict[str, Any]:
    """Repair legacy page slugs that redundantly start with ``wiki/``."""

    try:
        return await run_in_threadpool(_service().migrate_legacy_wiki_prefixes)
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


@router.post("/gbrain/initialize")
async def initialize_gbrain(payload: GbrainInitializePayload) -> dict[str, Any]:
    try:
        return await run_in_threadpool(_service().initialize_gbrain_runtime, payload.database_url)
    except (LlmWikiError, OSError, subprocess.SubprocessError) as exc:
        raise _bad_request(exc) from exc
