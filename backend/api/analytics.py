"""Analytics workbench API."""

from __future__ import annotations

import asyncio
import logging
import json
import time
import uuid
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile
from fastapi.responses import Response
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from analytics.semantic_assets import SemanticAssetError, get_semantic_asset_registry
from analytics.models import AnalyticsModelError, get_analytics_model_registry
from analytics.nl2sql.entity_candidates import recommend_entity_candidates
from analytics.nl2sql.guardrails import (
    RULE_TYPE_DEFINITIONS,
    delete_guardrail_rule,
    list_guardrail_rules,
    replace_guardrail_rules,
    reset_guardrail_rules,
    upsert_guardrail_rule,
)
from analytics.nl2sql.result_store import (
    QueryResultStoreError,
    export_query_result_csv,
    get_query_result_page,
    get_query_result_summary,
    list_query_results,
)
from analytics.table_catalog import TableAssetCatalog, TableCatalogError
from config import get_database_qa_config
from db import get_db_session, get_sessionmaker
from knowledge.semantic_dimension_jobs import (
    cancel_semantic_dimension_build_job,
    create_semantic_dimension_build_job,
    get_semantic_dimension_build_job,
    list_semantic_dimension_build_events,
    list_semantic_dimension_build_jobs,
    list_task_notifications,
    mark_task_notification_read,
    resolve_semantic_dimension_baseline_change,
    retry_semantic_dimension_build_job,
    semantic_dimension_event_to_dict,
    semantic_dimension_job_to_dict,
    task_notification_to_dict,
)
from knowledge.semantic_dimension_publisher import publish_semantic_dimension_build
from knowledge.semantic_dimension_crosswalk import (
    SemanticDimensionCrosswalkError,
    delete_override as delete_semantic_dimension_override,
    get_matching_overview,
    get_matching_view,
    publish_draft_overrides as publish_semantic_dimension_draft_overrides,
    retain_staged_entities_as_inactive,
    save_entity_override as save_semantic_dimension_entity_override,
    save_override as save_semantic_dimension_override,
    save_source_registry_entry,
)
from knowledge.models import SemanticDimensionBuildJob
from knowledge.import_jobs import job_to_list_dict, list_import_jobs


logger = logging.getLogger(__name__)

router = APIRouter(prefix="/analytics", tags=["analytics"])
BASE_DIR = Path(__file__).resolve().parent.parent

_profile_jobs: dict[str, dict[str, Any]] = {}
_profile_jobs_by_asset: dict[str, str] = {}
_profile_jobs_lock = asyncio.Lock()


def _public_profile_job(job: dict[str, Any]) -> dict[str, Any]:
    return {
        "job_id": job["job_id"],
        "asset_id": job["asset_id"],
        "status": job["status"],
        "created_at": job["created_at"],
        "updated_at": job["updated_at"],
        "started_at": job.get("started_at"),
        "finished_at": job.get("finished_at"),
        "error": job.get("error"),
        "asset": job.get("asset"),
    }


async def _run_profile_job(job_id: str) -> None:
    async with _profile_jobs_lock:
        job = _profile_jobs.get(job_id)
        if not job:
            return
        job["status"] = "running"
        job["started_at"] = time.time()
        job["updated_at"] = job["started_at"]
        asset_id = str(job["asset_id"])

    sessionmaker = get_sessionmaker()
    try:
        async with sessionmaker() as session:
            catalog = TableAssetCatalog(BASE_DIR)
            asset = await catalog.generate_profile(session, asset_id, include_profile=False)
        async with _profile_jobs_lock:
            job = _profile_jobs.get(job_id)
            if job:
                job["status"] = "succeeded"
                job["asset"] = asset
                job["finished_at"] = time.time()
                job["updated_at"] = job["finished_at"]
                if _profile_jobs_by_asset.get(asset_id) == job_id:
                    _profile_jobs_by_asset.pop(asset_id, None)
    except Exception as exc:
        async with _profile_jobs_lock:
            job = _profile_jobs.get(job_id)
            if job:
                job["status"] = "failed"
                job["error"] = str(exc)
                job["finished_at"] = time.time()
                job["updated_at"] = job["finished_at"]
                if _profile_jobs_by_asset.get(asset_id) == job_id:
                    _profile_jobs_by_asset.pop(asset_id, None)


async def _enqueue_profile_job(asset_id: str) -> dict[str, Any]:
    async with _profile_jobs_lock:
        existing_id = _profile_jobs_by_asset.get(asset_id)
        existing = _profile_jobs.get(existing_id or "")
        if existing and existing.get("status") in {"queued", "running"}:
            return _public_profile_job(existing)

        now = time.time()
        job_id = "profile_job_" + uuid.uuid4().hex[:16]
        job = {
            "job_id": job_id,
            "asset_id": asset_id,
            "status": "queued",
            "created_at": now,
            "updated_at": now,
        }
        _profile_jobs[job_id] = job
        _profile_jobs_by_asset[asset_id] = job_id

    asyncio.create_task(_run_profile_job(job_id))
    return _public_profile_job(job)


class SemanticAssetCreateRequest(BaseModel):
    name: str
    type: str = Field(pattern="^(measure|dimension|grain|relation)$")
    description: str = ""
    aliases: list[str] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)
    version: str = "0.1.0"
    slug: str | None = None
    dimension_definition: dict[str, object] = Field(default_factory=dict)
    relation_definition: dict[str, object] = Field(default_factory=dict)


class ConcatDatasetCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=160)
    description: str = Field(default="", max_length=1000)
    tags: list[str] = Field(default_factory=list, max_length=20)
    source_asset_ids: list[str] = Field(min_length=2, max_length=120)
    schema_mode: str = Field(default="strict", pattern="^(strict|baseline_fill_missing|union_fill_missing)$")
    preferred_intents: list[str] = Field(default_factory=list, max_length=12)
    direct_source_allowed: bool = True


class ConcatDatasetPreviewRequest(BaseModel):
    source_asset_ids: list[str] = Field(min_length=2, max_length=120)


class ConcatDatasetAppendRequest(BaseModel):
    source_asset_ids: list[str] = Field(min_length=1, max_length=120)
    schema_mode: str = Field(default="strict", pattern="^(strict|baseline_fill_missing|union_fill_missing)$")


class LogicalDatasetDefinitionUpdateRequest(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=160)
    description: str | None = Field(default=None, max_length=1000)
    tags: list[str] | None = Field(default=None, max_length=20)
    preferred_intents: list[str] | None = Field(default=None, max_length=12)
    direct_source_allowed: bool | None = None


class SemanticDimensionDefinitionUpdateRequest(BaseModel):
    dimension_definition: dict[str, object] = Field(default_factory=dict)
    name: str | None = None
    description: str | None = None
    aliases: list[str] | None = None
    tags: list[str] | None = None
    version: str | None = None


class SemanticRelationDefinitionUpdateRequest(BaseModel):
    relation_definition: dict[str, object] = Field(default_factory=dict)
    name: str | None = None
    description: str | None = None
    aliases: list[str] | None = None
    tags: list[str] | None = None
    version: str | None = None


class AnalyticsModelCreateRequest(BaseModel):
    name: str
    description: str = ""
    version: str = "0.1.0"
    tags: list[str] = Field(default_factory=list)
    slug: str | None = None
    data_assets: dict[str, object] = Field(default_factory=dict)
    semantic_assets: dict[str, object] = Field(default_factory=dict)
    asset_relations: list[str] = Field(default_factory=list)
    guardrails: list[str] = Field(default_factory=list)
    templates: dict[str, object] = Field(default_factory=dict)
    default_template: str | None = None


class SqlGuardrailRuleRequest(BaseModel):
    id: str
    name: str
    enabled: bool = True
    type: str
    scope: dict[str, object] = Field(default_factory=dict)
    params: dict[str, object] = Field(default_factory=dict)
    action: dict[str, object] = Field(default_factory=dict)
    document_body: str | None = None
    document_content: str | None = None


class SqlGuardrailRulesReplaceRequest(BaseModel):
    guardrails: list[SqlGuardrailRuleRequest] = Field(default_factory=list)


class SemanticDimensionBuildJobRequest(BaseModel):
    dimension_id: str = Field(description="Semantic dimension id, for example vehicle_series.")
    adapter: str = Field(default="vehicle_series_full")
    requested_scope: dict[str, object] = Field(default_factory=dict)
    input_snapshot: dict[str, object] = Field(default_factory=dict)
    session_id: str = ""
    query_id: str = ""


class SemanticDimensionOverrideRequest(BaseModel):
    source_ref: str
    source_key: dict[str, object]
    source_id: str = ""
    scope: str = Field(default="source_id", pattern="^(source_id|source_ref)$")
    action: str = Field(pattern="^(bind|exclude)$")
    target_entity_key: str = ""
    reason: str = ""
    source_name: str = ""
    source_kind: str = ""
    table_or_sheet: str = ""


class SemanticDimensionEntityOverrideRequest(BaseModel):
    entity_key: str
    action: str = Field(pattern="^(active|inactive|remove)$")
    reason: str = ""


class SemanticDimensionBaselineChangeDecisionRequest(BaseModel):
    action: str = Field(pattern="^(inactive|remove|cancel)$")


class SemanticDimensionSourceRegistryRequest(BaseModel):
    id: str
    name: str
    kind: str = "unknown"
    table_or_sheet: str = ""
    identity_fields: list[str] = Field(default_factory=list)
    mapping: list[dict[str, object]] = Field(default_factory=list)


@router.get("/semantic-assets")
async def list_semantic_assets():
    try:
        return get_semantic_asset_registry(BASE_DIR).list_assets()
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"Failed to list semantic assets: {exc}") from exc


@router.post("/semantic-dimension-jobs")
async def enqueue_semantic_dimension_build_job(
    request: SemanticDimensionBuildJobRequest,
    session: AsyncSession = Depends(get_db_session),
):
    try:
        job, queued = await create_semantic_dimension_build_job(
            session,
            dimension_id=request.dimension_id,
            adapter=request.adapter,
            requested_scope=dict(request.requested_scope),
            input_snapshot=dict(request.input_snapshot),
            session_id=request.session_id,
            query_id=request.query_id,
        )
        return {"job": semantic_dimension_job_to_dict(job), "queued": queued}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"Failed to enqueue semantic dimension build: {exc}") from exc


@router.get("/semantic-dimension-jobs")
async def list_semantic_dimension_jobs(
    limit: int = Query(default=50, ge=1, le=200),
    session: AsyncSession = Depends(get_db_session),
):
    try:
        jobs = await list_semantic_dimension_build_jobs(session, limit=limit)
        return {"jobs": [semantic_dimension_job_to_dict(job) for job in jobs], "count": len(jobs)}
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"Failed to list semantic dimension jobs: {exc}") from exc


@router.get("/semantic-dimension-jobs/{job_id}")
async def get_semantic_dimension_job(
    job_id: str,
    include_events: bool = True,
    session: AsyncSession = Depends(get_db_session),
):
    job = await get_semantic_dimension_build_job(session, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Semantic dimension build job not found")
    events = await list_semantic_dimension_build_events(session, job_id) if include_events else []
    return {"job": semantic_dimension_job_to_dict(job), "events": [semantic_dimension_event_to_dict(event) for event in events]}


@router.post("/semantic-dimension-jobs/{job_id}/retry")
async def retry_semantic_dimension_job(
    job_id: str,
    session: AsyncSession = Depends(get_db_session),
):
    try:
        job = await retry_semantic_dimension_build_job(session, job_id)
        return {"job": semantic_dimension_job_to_dict(job)}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/semantic-dimension-jobs/{job_id}/cancel")
async def cancel_semantic_dimension_job(
    job_id: str,
    session: AsyncSession = Depends(get_db_session),
):
    try:
        job = await cancel_semantic_dimension_build_job(session, job_id)
        return {"job": semantic_dimension_job_to_dict(job)}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/semantic-dimension-jobs/{job_id}/publish")
async def publish_semantic_dimension_job(
    job_id: str,
    session: AsyncSession = Depends(get_db_session),
):
    try:
        result = await publish_semantic_dimension_build(session, base_dir=BASE_DIR, job_id=job_id)
        return {
            "job": semantic_dimension_job_to_dict(result["job"]),
            "already_published": result["already_published"],
            "active_crosswalk": result.get("active_crosswalk"),
        }
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"Failed to publish semantic dimension build: {exc}") from exc


@router.get("/semantic-dimensions/{dimension_id}/matching")
async def get_semantic_dimension_matching(
    dimension_id: str,
    status: str = "",
    source_ref: str = "",
    query: str = "",
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=100, ge=1, le=500),
):
    try:
        return get_matching_view(
            BASE_DIR,
            dimension_id,
            status=status,
            source_ref=source_ref,
            query=query,
            offset=offset,
            limit=limit,
        )
    except SemanticDimensionCrosswalkError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"Failed to load semantic dimension matching: {exc}") from exc


@router.get("/semantic-dimensions/{dimension_id}/matching/overview")
async def get_semantic_dimension_matching_overview(
    dimension_id: str,
    query: str = "",
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=100, ge=1, le=500),
):
    try:
        return get_matching_overview(BASE_DIR, dimension_id, query=query, offset=offset, limit=limit)
    except SemanticDimensionCrosswalkError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"Failed to load semantic dimension overview: {exc}") from exc


@router.get("/semantic-dimensions/{dimension_id}/matching/baseline-changes")
async def get_semantic_dimension_baseline_change(
    dimension_id: str,
    session: AsyncSession = Depends(get_db_session),
):
    result = await session.execute(
        select(SemanticDimensionBuildJob)
        .where(
            SemanticDimensionBuildJob.dimension_id == dimension_id,
            SemanticDimensionBuildJob.status == "waiting_for_baseline_change_confirmation",
        )
        .order_by(SemanticDimensionBuildJob.created_at.desc())
        .limit(1)
    )
    job = result.scalar_one_or_none()
    if job is None:
        return {"change": None}
    summary = job.result_summary or {}
    return {"change": {"job": semantic_dimension_job_to_dict(job), "baseline_delta": summary.get("baseline_delta") or {}}}


@router.post("/semantic-dimension-jobs/{job_id}/baseline-change/resolve")
async def resolve_semantic_dimension_baseline_change_request(
    job_id: str,
    request: SemanticDimensionBaselineChangeDecisionRequest,
    session: AsyncSession = Depends(get_db_session),
):
    job = await get_semantic_dimension_build_job(session, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Semantic dimension build job not found")
    try:
        if request.action != "cancel":
            summary = job.result_summary or {}
            delta = summary.get("baseline_delta") or {}
            removed = [item for item in delta.get("removed") or [] if isinstance(item, dict)]
            entity_keys = [str(item.get("entity_key") or "") for item in removed]
            artifact_paths = summary.get("artifact_paths") or {}
            staged_path = Path(str(artifact_paths.get("crosswalk") or "")).resolve()
            if not staged_path.is_file():
                raise ValueError("Staging Crosswalk artifact is missing")
            staged = json.loads(staged_path.read_text(encoding="utf-8"))
            if request.action == "inactive":
                retained = retain_staged_entities_as_inactive(BASE_DIR, job.dimension_id, staged, entity_keys)
                staged = retained["staged"]
                staged_path.write_text(json.dumps(staged, ensure_ascii=False, indent=2), encoding="utf-8")
            for item in removed:
                save_semantic_dimension_entity_override(BASE_DIR, job.dimension_id, {
                    "entity_key": str(item.get("entity_key") or ""),
                    "action": request.action,
                    "reason": "用户处理构建发现的规范基准变化。",
                })
        await resolve_semantic_dimension_baseline_change(session, job, action=request.action)
        return {"job": semantic_dimension_job_to_dict(job), "status": "resolved"}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except SemanticDimensionCrosswalkError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"Failed to resolve semantic baseline change: {exc}") from exc


@router.post("/semantic-dimensions/{dimension_id}/matching/overrides")
async def save_semantic_dimension_matching_override(
    dimension_id: str,
    request: SemanticDimensionOverrideRequest,
):
    try:
        result = save_semantic_dimension_override(BASE_DIR, dimension_id, request.model_dump(mode="json"))
        return {"override": result["override"], "status": "saved"}
    except SemanticDimensionCrosswalkError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"Failed to save semantic dimension override: {exc}") from exc


@router.delete("/semantic-dimensions/{dimension_id}/matching/overrides/{override_id}")
async def delete_semantic_dimension_matching_override(dimension_id: str, override_id: str):
    try:
        return delete_semantic_dimension_override(BASE_DIR, dimension_id, override_id)
    except SemanticDimensionCrosswalkError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"Failed to delete semantic dimension override: {exc}") from exc


@router.post("/semantic-dimensions/{dimension_id}/matching/entities/lifecycle")
async def save_semantic_dimension_entity_lifecycle(
    dimension_id: str,
    request: SemanticDimensionEntityOverrideRequest,
):
    """Save a draft canonical lifecycle decision; publication remains explicit."""

    try:
        result = save_semantic_dimension_entity_override(BASE_DIR, dimension_id, request.model_dump(mode="json"))
        return {"override": result["override"], "status": "saved", "has_unpublished_changes": result["has_unpublished_changes"]}
    except SemanticDimensionCrosswalkError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"Failed to save semantic dimension lifecycle: {exc}") from exc


@router.post("/semantic-dimensions/{dimension_id}/matching/publish")
async def publish_semantic_dimension_matching_overrides(dimension_id: str):
    """Publish reviewed matching overrides as a new active Crosswalk version."""

    try:
        return publish_semantic_dimension_draft_overrides(BASE_DIR, dimension_id)
    except SemanticDimensionCrosswalkError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"Failed to publish semantic dimension matching: {exc}") from exc


@router.put("/semantic-dimensions/{dimension_id}/sources/{source_id}")
async def save_semantic_dimension_source_registry(
    dimension_id: str,
    source_id: str,
    request: SemanticDimensionSourceRegistryRequest,
):
    if source_id != request.id:
        raise HTTPException(status_code=400, detail="source_id path must match request id")
    try:
        return save_source_registry_entry(BASE_DIR, dimension_id, request.model_dump(mode="json"))
    except SemanticDimensionCrosswalkError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"Failed to save semantic dimension source: {exc}") from exc


@router.get("/task-notifications")
async def get_task_notifications(
    unread_only: bool = False,
    limit: int = Query(default=50, ge=1, le=200),
    session: AsyncSession = Depends(get_db_session),
):
    notifications = await list_task_notifications(session, unread_only=unread_only, limit=limit)
    return {"notifications": [task_notification_to_dict(item) for item in notifications], "count": len(notifications)}


@router.get("/task-center")
async def get_task_center(
    limit: int = Query(default=50, ge=1, le=200),
    session: AsyncSession = Depends(get_db_session),
):
    """Display adapter only: preserves the ownership of each underlying queue."""
    semantic_jobs = await list_semantic_dimension_build_jobs(session, limit=limit)
    import_jobs = await list_import_jobs(session, limit=limit)
    items: list[dict[str, Any]] = []
    for job in semantic_jobs:
        item = semantic_dimension_job_to_dict(job)
        items.append(
            {
                "task_type": "semantic_dimension_build",
                "title": f"构建语义维度：{job.dimension_id}",
                "job": item,
                "created_at": item.get("created_at"),
            }
        )
    for job in import_jobs:
        item = job_to_list_dict(job)
        items.append(
            {
                "task_type": "knowledge_import",
                "title": job.title or job.file_name,
                "job": item,
                "created_at": item.get("created_at"),
            }
        )
    items.sort(key=lambda item: str(item.get("created_at") or ""), reverse=True)
    return {"tasks": items[:limit], "count": min(len(items), limit)}


@router.post("/task-notifications/{notification_id}/read")
async def read_task_notification(
    notification_id: str,
    session: AsyncSession = Depends(get_db_session),
):
    notification = await mark_task_notification_read(session, notification_id)
    if notification is None:
        raise HTTPException(status_code=404, detail="Task notification not found")
    return {"notification": task_notification_to_dict(notification)}


@router.post("/semantic-assets/refresh")
async def refresh_semantic_assets():
    try:
        return get_semantic_asset_registry(BASE_DIR).refresh()
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"Failed to refresh semantic assets: {exc}") from exc


@router.get("/sql-guardrail-types")
async def list_sql_guardrail_types():
    return {"types": RULE_TYPE_DEFINITIONS}


@router.get("/sql-guardrails")
async def list_sql_guardrails():
    try:
        return list_guardrail_rules()
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"Failed to list SQL guardrails: {exc}") from exc


@router.put("/sql-guardrails")
async def replace_sql_guardrails(request: SqlGuardrailRulesReplaceRequest):
    try:
        return replace_guardrail_rules([rule.model_dump(mode="json") for rule in request.guardrails])
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Failed to save SQL guardrails: {exc}") from exc


@router.post("/sql-guardrails")
async def upsert_sql_guardrail(request: SqlGuardrailRuleRequest):
    try:
        return {"rule": upsert_guardrail_rule(request.model_dump(mode="json")), "status": "saved"}
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Failed to save SQL guardrail: {exc}") from exc


@router.delete("/sql-guardrails/{rule_id}")
async def delete_sql_guardrail(rule_id: str):
    try:
        deleted = delete_guardrail_rule(rule_id)
        if not deleted:
            raise HTTPException(status_code=404, detail="SQL guardrail not found")
        return {"status": "deleted"}
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"Failed to delete SQL guardrail: {exc}") from exc


@router.post("/sql-guardrails/reset")
async def reset_sql_guardrails():
    try:
        return reset_guardrail_rules()
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"Failed to reset SQL guardrails: {exc}") from exc


@router.post("/semantic-assets")
async def create_semantic_asset(request: SemanticAssetCreateRequest):
    try:
        asset = get_semantic_asset_registry(BASE_DIR).create_asset(
            name=request.name,
            asset_type=request.type,
            description=request.description,
            aliases=request.aliases,
            tags=request.tags,
            version=request.version,
            slug=request.slug,
            dimension_definition=request.dimension_definition,
            relation_definition=request.relation_definition,
        )
        return {"asset": asset, "status": "created"}
    except SemanticAssetError as exc:
        message = str(exc)
        status_code = 409 if "already exists" in message else 400
        raise HTTPException(status_code=status_code, detail=message) from exc
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"Failed to create semantic asset: {exc}") from exc


@router.post("/semantic-assets/import")
async def import_semantic_assets(files: list[UploadFile] = File(...)):
    if not files:
        raise HTTPException(status_code=400, detail="No files uploaded")
    registry = get_semantic_asset_registry(BASE_DIR)
    try:
        first_filename = files[0].filename or ""
        if len(files) == 1 and first_filename.lower().endswith(".zip"):
            return registry.import_zip(files[0].file)
        payload = []
        for uploaded in files:
            payload.append((uploaded.filename or "uploaded", await uploaded.read()))
        return registry.import_files(payload)
    except SemanticAssetError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"Failed to import semantic assets: {exc}") from exc


@router.patch("/semantic-assets/{asset_id:path}/dimension-definition")
async def update_semantic_dimension_definition(asset_id: str, request: SemanticDimensionDefinitionUpdateRequest):
    try:
        asset = get_semantic_asset_registry(BASE_DIR).update_dimension_definition(
            asset_id,
            dict(request.dimension_definition),
            name=request.name,
            description=request.description,
            aliases=request.aliases,
            tags=request.tags,
            version=request.version,
        )
        return {"asset": asset, "status": "saved"}
    except SemanticAssetError as exc:
        message = str(exc)
        status_code = 404 if "not found" in message else 400
        raise HTTPException(status_code=status_code, detail=message) from exc
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"Failed to save dimension definition: {exc}") from exc


@router.patch("/semantic-assets/{asset_id:path}/relation-definition")
async def update_semantic_relation_definition(asset_id: str, request: SemanticRelationDefinitionUpdateRequest):
    try:
        asset = get_semantic_asset_registry(BASE_DIR).update_relation_definition(
            asset_id,
            dict(request.relation_definition),
            name=request.name,
            description=request.description,
            aliases=request.aliases,
            tags=request.tags,
            version=request.version,
        )
        return {"asset": asset, "status": "saved"}
    except SemanticAssetError as exc:
        message = str(exc)
        status_code = 404 if "not found" in message else 400
        raise HTTPException(status_code=status_code, detail=message) from exc
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"Failed to save relation definition: {exc}") from exc


@router.get("/models")
async def list_analytics_models():
    try:
        return get_analytics_model_registry(BASE_DIR).list_models()
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"Failed to list analytics models: {exc}") from exc


@router.post("/models/refresh")
async def refresh_analytics_models():
    try:
        return get_analytics_model_registry(BASE_DIR).refresh()
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"Failed to refresh analytics models: {exc}") from exc


@router.post("/models")
async def create_analytics_model(request: AnalyticsModelCreateRequest):
    try:
        model = get_analytics_model_registry(BASE_DIR).create_model(
            name=request.name,
            description=request.description,
            version=request.version,
            tags=request.tags,
            slug=request.slug,
            data_assets=request.data_assets,
            semantic_assets=request.semantic_assets,
            asset_relations=request.asset_relations,
            guardrails=request.guardrails,
            templates=request.templates,
            default_template=request.default_template,
        )
        return {"model": model, "status": "created"}
    except AnalyticsModelError as exc:
        message = str(exc)
        status_code = 409 if "already exists" in message else 400
        raise HTTPException(status_code=status_code, detail=message) from exc
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"Failed to create analytics model: {exc}") from exc


@router.post("/models/import")
async def import_analytics_models(files: list[UploadFile] = File(...)):
    if not files:
        raise HTTPException(status_code=400, detail="No files uploaded")
    registry = get_analytics_model_registry(BASE_DIR)
    try:
        first_filename = files[0].filename or ""
        if len(files) == 1 and first_filename.lower().endswith(".zip"):
            return registry.import_zip(files[0].file)
        payload = []
        for uploaded in files:
            payload.append((uploaded.filename or "uploaded", await uploaded.read()))
        return registry.import_files(payload)
    except AnalyticsModelError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"Failed to import analytics models: {exc}") from exc


@router.get("/models/{model_id:path}")
async def get_analytics_model(model_id: str):
    try:
        return {"model": get_analytics_model_registry(BASE_DIR).get_model(model_id)}
    except AnalyticsModelError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"Failed to load analytics model: {exc}") from exc


@router.get("/semantic-assets/{asset_id:path}")
async def get_semantic_asset(asset_id: str):
    try:
        return {"asset": get_semantic_asset_registry(BASE_DIR).get_asset(asset_id)}
    except SemanticAssetError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"Failed to load semantic asset: {exc}") from exc


@router.get("/query-results")
async def list_database_query_results(
    limit: int = Query(default=50, ge=1, le=200),
    include_expired: bool = Query(default=True),
    include_profile: bool = Query(default=False),
    session: AsyncSession = Depends(get_db_session),
):
    try:
        return await list_query_results(
            session,
            limit=limit,
            include_expired=include_expired,
            include_profile=include_profile,
        )
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"Failed to list query results: {exc}") from exc


@router.get("/query-results/{result_id}/summary")
async def get_database_query_result_summary(
    result_id: str,
    session: AsyncSession = Depends(get_db_session),
):
    try:
        return await get_query_result_summary(session, result_id)
    except QueryResultStoreError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"Failed to load query result: {exc}") from exc


@router.get("/query-results/{result_id}")
async def get_database_query_result_page(
    result_id: str,
    page: int = Query(default=1, ge=1),
    page_size: int | None = Query(default=None, ge=1),
    session: AsyncSession = Depends(get_db_session),
):
    try:
        return await get_query_result_page(session, result_id, page=page, page_size=page_size)
    except QueryResultStoreError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"Failed to load query result page: {exc}") from exc


@router.get("/query-results/{result_id}/export.csv")
async def export_database_query_result_csv(
    result_id: str,
    session: AsyncSession = Depends(get_db_session),
):
    if not get_database_qa_config().get("export_enabled", False):
        raise HTTPException(status_code=403, detail="CSV 导出已在智能问数设置中关闭。")
    try:
        filename, content = await export_query_result_csv(session, result_id)
        return Response(
            content=content,
            media_type="text/csv; charset=utf-8",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )
    except QueryResultStoreError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"Failed to export query result: {exc}") from exc


@router.get("/table-assets")
async def list_table_assets(
    include_profile: bool = Query(default=False),
    limit: int = Query(default=500, ge=1, le=2000),
    session: AsyncSession = Depends(get_db_session),
):
    catalog = TableAssetCatalog(BASE_DIR)
    try:
        assets = await catalog.list_assets(session, include_profile=include_profile, limit=limit)
        return {"assets": assets, "count": len(assets)}
    except TableCatalogError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"Failed to list table assets: {exc}") from exc


@router.post("/table-assets/concat-datasets")
async def create_concat_dataset(
    request: ConcatDatasetCreateRequest,
    session: AsyncSession = Depends(get_db_session),
):
    try:
        asset = await TableAssetCatalog(BASE_DIR).create_concat_dataset(
            session,
            name=request.name,
            description=request.description,
            tags=request.tags,
            source_asset_ids=request.source_asset_ids,
            schema_mode=request.schema_mode,
            routing={"preferred_intents": request.preferred_intents, "direct_source_allowed": request.direct_source_allowed},
        )
        return {"asset": asset, "status": "created"}
    except TableCatalogError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        logger.exception("Failed to create logical concat dataset")
        raise HTTPException(status_code=503, detail=f"Failed to create logical dataset: {exc}") from exc


@router.post("/table-assets/concat-datasets/preview")
async def preview_concat_dataset(
    request: ConcatDatasetPreviewRequest,
    session: AsyncSession = Depends(get_db_session),
):
    try:
        return await TableAssetCatalog(BASE_DIR).preview_concat_dataset(
            session,
            source_asset_ids=request.source_asset_ids,
        )
    except TableCatalogError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        logger.exception("Failed to preview logical concat dataset")
        raise HTTPException(status_code=503, detail=f"Failed to inspect logical dataset fields: {exc}") from exc


@router.post("/table-assets/{asset_id}/refresh-concat")
async def refresh_concat_dataset(
    asset_id: str,
    session: AsyncSession = Depends(get_db_session),
):
    try:
        asset = await TableAssetCatalog(BASE_DIR).refresh_concat_dataset(session, asset_id)
        return {"asset": asset, "status": "refreshed"}
    except TableCatalogError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        logger.exception("Failed to refresh logical concat dataset %s", asset_id)
        raise HTTPException(status_code=503, detail=f"Failed to refresh logical dataset: {exc}") from exc


@router.post("/table-assets/{asset_id}/concat-sources")
async def append_concat_dataset_sources(
    asset_id: str,
    request: ConcatDatasetAppendRequest,
    session: AsyncSession = Depends(get_db_session),
):
    try:
        asset = await TableAssetCatalog(BASE_DIR).append_concat_dataset_sources(
            session,
            asset_id=asset_id,
            source_asset_ids=request.source_asset_ids,
            schema_mode=request.schema_mode,
        )
        return {"asset": asset, "status": "sources_appended"}
    except TableCatalogError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        logger.exception("Failed to append sources to logical concat dataset %s", asset_id)
        raise HTTPException(status_code=503, detail=f"Failed to append logical dataset sources: {exc}") from exc


@router.patch("/table-assets/{asset_id}/logical-definition")
async def update_logical_dataset_definition(
    asset_id: str,
    request: LogicalDatasetDefinitionUpdateRequest,
    session: AsyncSession = Depends(get_db_session),
):
    try:
        asset = await TableAssetCatalog(BASE_DIR).update_logical_dataset_definition(
            session,
            asset_id=asset_id,
            name=request.name,
            description=request.description,
            tags=request.tags,
            preferred_intents=request.preferred_intents,
            direct_source_allowed=request.direct_source_allowed,
        )
        return {"asset": asset, "status": "definition_updated"}
    except TableCatalogError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        logger.exception("Failed to update logical dataset definition %s", asset_id)
        raise HTTPException(status_code=503, detail=f"Failed to update logical dataset definition: {exc}") from exc


@router.post("/table-assets/refresh-profiles")
async def refresh_table_asset_profiles(
    limit: int = Query(default=200, ge=1, le=1000),
    session: AsyncSession = Depends(get_db_session),
):
    catalog = TableAssetCatalog(BASE_DIR)
    try:
        return await catalog.refresh_profiles(session, limit=limit)
    except TableCatalogError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"Failed to refresh table profiles: {exc}") from exc


@router.get("/table-assets/{asset_id}")
async def get_table_asset(
    asset_id: str,
    session: AsyncSession = Depends(get_db_session),
):
    catalog = TableAssetCatalog(BASE_DIR)
    try:
        return {"asset": await catalog.get_asset(session, asset_id, include_profile=True)}
    except TableCatalogError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"Failed to load table asset: {exc}") from exc


@router.delete("/table-assets/{asset_id}")
async def remove_table_asset(
    asset_id: str,
    session: AsyncSession = Depends(get_db_session),
):
    catalog = TableAssetCatalog(BASE_DIR)
    try:
        return await catalog.remove_asset(session, asset_id)
    except TableCatalogError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"Failed to remove table asset: {exc}") from exc


@router.post("/table-assets/{asset_id}/profile")
async def generate_table_asset_profile(
    asset_id: str,
    session: AsyncSession = Depends(get_db_session),
):
    catalog = TableAssetCatalog(BASE_DIR)
    try:
        await catalog.get_asset(session, asset_id, include_profile=False)
        return {"job": await _enqueue_profile_job(asset_id)}
    except TableCatalogError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"Failed to enqueue table profile: {exc}") from exc


@router.get("/table-assets/profile-jobs/{job_id}")
async def get_table_asset_profile_job(job_id: str):
    async with _profile_jobs_lock:
        job = _profile_jobs.get(job_id)
        if not job:
            raise HTTPException(status_code=404, detail="Profile job not found.")
        return {"job": _public_profile_job(job)}


@router.get("/table-assets/{asset_id}/entity-candidates")
async def get_table_asset_entity_candidates(
    asset_id: str,
    limit: int = Query(default=12, ge=1, le=50),
    session: AsyncSession = Depends(get_db_session),
):
    catalog = TableAssetCatalog(BASE_DIR)
    try:
        asset = await catalog.get_asset(session, asset_id, include_profile=True)
        profile = asset.get("profile") or {}
        candidates = recommend_entity_candidates(
            profile,
            table_name=asset.get("sheet_name") or asset.get("file_name"),
            max_candidates=limit,
        )
        return {"asset_id": asset_id, "candidates": candidates, "count": len(candidates)}
    except TableCatalogError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"Failed to recommend entity candidates: {exc}") from exc
