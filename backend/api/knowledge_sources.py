"""Unified knowledge Source, Item, and Sync Run API."""

from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from db import get_db_session
from knowledge.models import KnowledgeSourceConnection, KnowledgeSourceItem, KnowledgeSyncRun
from knowledge.service import DEFAULT_KNOWLEDGE_BASE_ID, KnowledgeServiceError
from knowledge.sources import (
    BUILTIN_CONNECTORS,
    complete_builtin_sync_run,
    create_source_connection,
    create_sync_run,
    get_source_connection,
    list_recent_sync_runs,
    list_source_connections,
    list_source_items,
    source_item_to_dict,
    source_to_dict,
    sync_run_to_dict,
)

router = APIRouter(prefix="/knowledge", tags=["knowledge-sources"])

_SECRET_KEYS = {
    "app_secret",
    "client_secret",
    "access_token",
    "refresh_token",
    "tenant_access_token",
    "password",
    "secret",
    "token",
}
_PROTECTED_FEISHU_CONFIG_KEYS = {"app_credential_id", "user_grant_id"}


def _assert_secret_free(value: Any, *, path: str = "config") -> None:
    if isinstance(value, dict):
        for key, nested in value.items():
            if str(key).strip().lower() in _SECRET_KEYS:
                raise HTTPException(status_code=400, detail=f"{path}.{key} 必须通过凭据接口保存，不能写入 Source 配置。")
            _assert_secret_free(nested, path=f"{path}.{key}")
    elif isinstance(value, list):
        for index, nested in enumerate(value):
            _assert_secret_free(nested, path=f"{path}[{index}]")


class SourceCreateRequest(BaseModel):
    connector_key: Literal["feishu_wiki"]
    name: str = Field(default="飞书知识库", max_length=200)
    auth_type: Literal["tenant", "user"] = "tenant"
    knowledge_base_id: str = DEFAULT_KNOWLEDGE_BASE_ID
    config: dict[str, Any] = Field(default_factory=dict)
    schedule: dict[str, Any] = Field(default_factory=dict)


class SourceUpdateRequest(BaseModel):
    name: str | None = Field(default=None, max_length=200)
    status: Literal["ready", "disabled"] | None = None
    config: dict[str, Any] | None = None
    schedule: dict[str, Any] | None = None


class SourceSyncRequest(BaseModel):
    mode: Literal["incremental", "full_scan", "reindex"] = "incremental"


@router.get("/connectors")
async def get_knowledge_connectors():
    return {"connectors": list(BUILTIN_CONNECTORS)}


@router.get("/sources")
async def get_knowledge_sources(
    knowledge_base_id: str = DEFAULT_KNOWLEDGE_BASE_ID,
    session: AsyncSession = Depends(get_db_session),
):
    try:
        return {"sources": await list_source_connections(session, knowledge_base_id=knowledge_base_id)}
    except KnowledgeServiceError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/sources", status_code=201)
async def post_knowledge_source(
    request: SourceCreateRequest,
    session: AsyncSession = Depends(get_db_session),
):
    _assert_secret_free(request.config)
    _assert_secret_free(request.schedule, path="schedule")
    if _PROTECTED_FEISHU_CONFIG_KEYS.intersection(request.config):
        raise HTTPException(status_code=400, detail="飞书凭据绑定只能通过专用授权接口修改。")
    try:
        source = await create_source_connection(
            session,
            connector_key=request.connector_key,
            name=request.name,
            auth_type=request.auth_type,
            knowledge_base_id=request.knowledge_base_id,
            config=request.config,
            schedule=request.schedule,
        )
        await session.commit()
        await session.refresh(source)
        return {"source": source_to_dict(source, item_count=0)}
    except KnowledgeServiceError as exc:
        await session.rollback()
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/sources/{source_id}")
async def get_knowledge_source(source_id: str, session: AsyncSession = Depends(get_db_session)):
    try:
        source = await get_source_connection(session, source_id)
        item_count = await session.scalar(
            select(func.count(KnowledgeSourceItem.id)).where(KnowledgeSourceItem.source_connection_id == source.id)
        )
        return {"source": source_to_dict(source, item_count=int(item_count or 0))}
    except KnowledgeServiceError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.patch("/sources/{source_id}")
async def patch_knowledge_source(
    source_id: str,
    request: SourceUpdateRequest,
    session: AsyncSession = Depends(get_db_session),
):
    if request.config is not None:
        _assert_secret_free(request.config)
    if request.schedule is not None:
        _assert_secret_free(request.schedule, path="schedule")
    try:
        source = await get_source_connection(session, source_id)
        if request.name is not None:
            source.name = request.name.strip() or source.name
        if request.status is not None:
            source.status = request.status
        if request.config is not None:
            protected = _PROTECTED_FEISHU_CONFIG_KEYS.intersection(request.config)
            if source.connector_key == "feishu_wiki" and protected:
                raise HTTPException(
                    status_code=400,
                    detail="飞书凭据绑定只能通过专用授权接口修改。",
                )
            source.config_json = {**(source.config_json or {}), **request.config}
        if request.schedule is not None:
            source.schedule_json = dict(request.schedule)
        await session.commit()
        await session.refresh(source)
        return {"source": source_to_dict(source)}
    except KnowledgeServiceError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.delete("/sources/{source_id}")
async def disable_knowledge_source(source_id: str, session: AsyncSession = Depends(get_db_session)):
    try:
        source = await get_source_connection(session, source_id)
        if source.connector_key in {"local_upload", "web_capture"}:
            raise HTTPException(status_code=409, detail="内置 Source 不能删除或停用。")
        source.status = "disabled"
        await session.commit()
        return {"ok": True, "source": source_to_dict(source), "deleted": False}
    except KnowledgeServiceError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/sources/{source_id}/items")
async def get_knowledge_source_items(
    source_id: str,
    status: str = "all",
    search: str = "",
    limit: int = 200,
    session: AsyncSession = Depends(get_db_session),
):
    try:
        items = await list_source_items(session, source_id=source_id, status=status, search=search, limit=limit)
        return {"items": [source_item_to_dict(item) for item in items]}
    except KnowledgeServiceError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/sync-runs")
async def get_knowledge_sync_runs(
    knowledge_base_id: str = DEFAULT_KNOWLEDGE_BASE_ID,
    limit: int = 50,
    session: AsyncSession = Depends(get_db_session),
):
    return {"runs": await list_recent_sync_runs(session, knowledge_base_id=knowledge_base_id, limit=limit)}


@router.get("/sources/{source_id}/runs")
async def get_knowledge_source_runs(
    source_id: str,
    limit: int = 50,
    session: AsyncSession = Depends(get_db_session),
):
    try:
        await get_source_connection(session, source_id)
    except KnowledgeServiceError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    result = await session.execute(
        select(KnowledgeSyncRun)
        .where(KnowledgeSyncRun.source_connection_id == source_id)
        .order_by(KnowledgeSyncRun.created_at.desc())
        .limit(max(1, min(limit, 200)))
    )
    return {"runs": [sync_run_to_dict(run) for run in result.scalars()]}


@router.post("/sources/{source_id}/sync", status_code=202)
async def sync_knowledge_source(
    source_id: str,
    request: SourceSyncRequest,
    session: AsyncSession = Depends(get_db_session),
):
    try:
        source = await get_source_connection(session, source_id)
        if source.status == "disabled":
            raise KnowledgeServiceError("Disabled source cannot be synchronized.")
        if source.status in {"pending_auth", "needs_reauth"}:
            raise KnowledgeServiceError("Source authentication is not ready.")
        active_run = (
            await session.execute(
                select(KnowledgeSyncRun.id).where(
                    KnowledgeSyncRun.source_connection_id == source.id,
                    KnowledgeSyncRun.status.in_(["queued", "running"]),
                ).limit(1)
            )
        ).scalar_one_or_none()
        if active_run is not None:
            raise KnowledgeServiceError("该 Source 已有同步任务在运行，请等待完成或先取消。")
        run = await create_sync_run(session, source=source, mode=request.mode)
        if source.connector_key in {"local_upload", "web_capture"}:
            item_count = await session.scalar(
                select(func.count(KnowledgeSourceItem.id)).where(KnowledgeSourceItem.source_connection_id == source.id)
            )
            complete_builtin_sync_run(source, run, item_count=int(item_count or 0))
        else:
            source.status = "syncing"
        await session.commit()
        await session.refresh(run)
        return {"run": sync_run_to_dict(run), "source": source_to_dict(source)}
    except KnowledgeServiceError as exc:
        await session.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/sources/{source_id}/runs/{run_id}/cancel")
async def cancel_knowledge_sync_run(
    source_id: str,
    run_id: str,
    session: AsyncSession = Depends(get_db_session),
):
    run = await session.get(KnowledgeSyncRun, run_id)
    if run is None or run.source_connection_id != source_id:
        raise HTTPException(status_code=404, detail="Sync run not found.")
    if run.status not in {"queued", "running"}:
        raise HTTPException(status_code=409, detail="Only queued or running sync runs can be cancelled.")
    run.status = "cancelled"
    run.current_step = "cancelled"
    source = await session.get(KnowledgeSourceConnection, source_id)
    if source is not None:
        source.status = "ready"
    await session.commit()
    return {"run": sync_run_to_dict(run)}
