"""Knowledge Source control plane shared by built-ins and remote connectors.

This module owns stable source/item/run identity. Connector implementations
produce source items; the existing import/index pipeline materializes them into
KnowledgeDocument rows. No connector-specific secret is accepted here.
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from knowledge.models import (
    KnowledgeBase,
    KnowledgeSourceConnection,
    KnowledgeSourceItem,
    KnowledgeImportJob,
    KnowledgeSyncRun,
    iso_utc,
    new_id,
)
from knowledge.service import DEFAULT_KNOWLEDGE_BASE_ID, KnowledgeServiceError, assert_writes_allowed_tolerant

BUILTIN_CONNECTORS: tuple[dict[str, Any], ...] = (
    {
        "key": "local_upload",
        "name": "本地上传",
        "description": "PDF、Markdown、Office 与表格文件",
        "auth_types": ["builtin"],
        "capabilities": ["upload", "parse", "index"],
        "builtin": True,
    },
    {
        "key": "web_capture",
        "name": "网页收藏",
        "description": "保存链接、提取正文并加入统一知识库",
        "auth_types": ["builtin"],
        "capabilities": ["capture", "parse", "reading_state", "index"],
        "builtin": True,
    },
    {
        "key": "feishu_wiki",
        "name": "飞书知识库",
        "description": "同步指定 Wiki 空间或根节点下的文档",
        "auth_types": ["tenant", "user"],
        "capabilities": ["discover", "incremental_sync", "full_scan", "docx", "attachments"],
        "builtin": False,
    },
)

_BUILTIN_NAMES = {"local_upload": "本地上传", "web_capture": "网页收藏"}


def _stable_id(prefix: str, *parts: str) -> str:
    digest = hashlib.sha256(":".join(parts).encode("utf-8")).hexdigest()[:24]
    return f"{prefix}_{digest}"


def builtin_source_id(knowledge_base_id: str, connector_key: str) -> str:
    if connector_key not in _BUILTIN_NAMES:
        raise KnowledgeServiceError(f"Unknown built-in knowledge connector: {connector_key}")
    return _stable_id("src", knowledge_base_id, connector_key)


def stable_source_item_id(source_connection_id: str, external_id: str) -> str:
    return _stable_id("sitem", source_connection_id, external_id)


async def ensure_knowledge_base(session: AsyncSession, knowledge_base_id: str) -> KnowledgeBase:
    knowledge_base = await session.get(KnowledgeBase, knowledge_base_id)
    if knowledge_base is not None:
        return knowledge_base
    if knowledge_base_id != DEFAULT_KNOWLEDGE_BASE_ID:
        raise KnowledgeServiceError(f"Knowledge base not found: {knowledge_base_id}")
    knowledge_base = KnowledgeBase(
        id=DEFAULT_KNOWLEDGE_BASE_ID,
        name="Default Knowledge Base",
        description="Default local knowledge base exposed to DeepAgents as /knowledge/.",
    )
    session.add(knowledge_base)
    await session.flush()
    return knowledge_base


async def ensure_builtin_source_connections(
    session: AsyncSession,
    *,
    knowledge_base_id: str = DEFAULT_KNOWLEDGE_BASE_ID,
) -> dict[str, KnowledgeSourceConnection]:
    await ensure_knowledge_base(session, knowledge_base_id)
    sources: dict[str, KnowledgeSourceConnection] = {}
    for connector_key, name in _BUILTIN_NAMES.items():
        source_id = builtin_source_id(knowledge_base_id, connector_key)
        source = await session.get(KnowledgeSourceConnection, source_id)
        if source is None:
            source = KnowledgeSourceConnection(
                id=source_id,
                knowledge_base_id=knowledge_base_id,
                connector_key=connector_key,
                name=name,
                status="ready",
                auth_type="builtin",
                config_json={},
                schedule_json={},
            )
            session.add(source)
            await session.flush()
        sources[connector_key] = source
    return sources


async def create_source_connection(
    session: AsyncSession,
    *,
    connector_key: str,
    name: str,
    auth_type: str,
    knowledge_base_id: str = DEFAULT_KNOWLEDGE_BASE_ID,
    config: dict[str, Any] | None = None,
    schedule: dict[str, Any] | None = None,
    credential_ref: str = "",
) -> KnowledgeSourceConnection:
    await assert_writes_allowed_tolerant(session)
    if connector_key in _BUILTIN_NAMES:
        return (await ensure_builtin_source_connections(session, knowledge_base_id=knowledge_base_id))[connector_key]
    if connector_key not in {item["key"] for item in BUILTIN_CONNECTORS}:
        raise KnowledgeServiceError(f"Unsupported knowledge connector: {connector_key}")
    if auth_type not in {"tenant", "user"}:
        raise KnowledgeServiceError("Feishu source auth_type must be tenant or user.")
    await ensure_knowledge_base(session, knowledge_base_id)
    source = KnowledgeSourceConnection(
        id=new_id("src"),
        knowledge_base_id=knowledge_base_id,
        connector_key=connector_key,
        name=name.strip() or "飞书知识库",
        status="pending_auth",
        auth_type=auth_type,
        credential_ref=credential_ref,
        config_json=dict(config or {}),
        schedule_json=dict(schedule or {}),
    )
    session.add(source)
    await session.flush()
    return source


async def upsert_source_item(
    session: AsyncSession,
    *,
    source: KnowledgeSourceConnection,
    external_id: str,
    external_type: str,
    title: str = "",
    source_url: str | None = None,
    status: str = "discovered",
    document_id: str | None = None,
    content_sha256: str | None = None,
    revision: str | None = None,
    path: list[str] | None = None,
    metadata: dict[str, Any] | None = None,
) -> KnowledgeSourceItem:
    normalized_external_id = external_id.strip()
    if not normalized_external_id:
        raise KnowledgeServiceError("Source item external_id is required.")
    item = (
        await session.execute(
            select(KnowledgeSourceItem).where(
                KnowledgeSourceItem.source_connection_id == source.id,
                KnowledgeSourceItem.external_id == normalized_external_id,
            )
        )
    ).scalar_one_or_none()
    if item is None:
        item = KnowledgeSourceItem(
            id=stable_source_item_id(source.id, normalized_external_id),
            knowledge_base_id=source.knowledge_base_id,
            source_connection_id=source.id,
            external_id=normalized_external_id,
            external_type=external_type,
        )
        session.add(item)
    item.title = title.strip()
    item.source_url = source_url
    item.status = status
    item.document_id = document_id
    item.content_sha256 = content_sha256
    item.revision = revision
    item.path_json = list(path or [])
    if metadata is not None:
        item.metadata_json = dict(metadata)
    await session.flush()
    return item


async def list_source_connections(
    session: AsyncSession,
    *,
    knowledge_base_id: str = DEFAULT_KNOWLEDGE_BASE_ID,
) -> list[dict[str, Any]]:
    await ensure_builtin_source_connections(session, knowledge_base_id=knowledge_base_id)
    await session.commit()
    rows = (
        await session.execute(
            select(KnowledgeSourceConnection)
            .where(KnowledgeSourceConnection.knowledge_base_id == knowledge_base_id)
            .order_by(KnowledgeSourceConnection.created_at.asc())
        )
    ).scalars().all()
    counts = dict(
        (
            await session.execute(
                select(KnowledgeSourceItem.source_connection_id, func.count(KnowledgeSourceItem.id))
                .outerjoin(KnowledgeImportJob, KnowledgeImportJob.source_item_id == KnowledgeSourceItem.id)
                .where(KnowledgeSourceItem.knowledge_base_id == knowledge_base_id)
                .where(
                    or_(
                        KnowledgeSourceItem.status != "staged",
                        ~KnowledgeSourceItem.external_id.like("import-job:%"),
                        KnowledgeImportJob.id.is_not(None),
                    )
                )
                .group_by(KnowledgeSourceItem.source_connection_id)
            )
        ).all()
    )
    return [source_to_dict(source, item_count=int(counts.get(source.id, 0))) for source in rows]


async def get_source_connection(session: AsyncSession, source_id: str) -> KnowledgeSourceConnection:
    source = await session.get(KnowledgeSourceConnection, source_id)
    if source is None:
        raise KnowledgeServiceError("Knowledge source not found.")
    return source


async def list_source_items(
    session: AsyncSession,
    *,
    source_id: str,
    status: str = "all",
    search: str = "",
    limit: int = 200,
) -> list[KnowledgeSourceItem]:
    await get_source_connection(session, source_id)
    stmt = (
        select(KnowledgeSourceItem)
        .outerjoin(KnowledgeImportJob, KnowledgeImportJob.source_item_id == KnowledgeSourceItem.id)
        .where(KnowledgeSourceItem.source_connection_id == source_id)
        .where(
            or_(
                KnowledgeSourceItem.status != "staged",
                ~KnowledgeSourceItem.external_id.like("import-job:%"),
                KnowledgeImportJob.id.is_not(None),
            )
        )
    )
    if status and status != "all":
        if status == "ready":
            stmt = stmt.where(KnowledgeSourceItem.status.in_(["ready", "completed", "succeeded", "success"]))
        else:
            stmt = stmt.where(KnowledgeSourceItem.status == status)
    if search.strip():
        stmt = stmt.where(KnowledgeSourceItem.title.ilike(f"%{search.strip()}%"))
    result = await session.execute(stmt.order_by(KnowledgeSourceItem.updated_at.desc()).limit(max(1, min(limit, 500))))
    return list(result.scalars())


async def create_sync_run(
    session: AsyncSession,
    *,
    source: KnowledgeSourceConnection,
    mode: str,
) -> KnowledgeSyncRun:
    if mode not in {"incremental", "full_scan", "reindex"}:
        raise KnowledgeServiceError("Sync mode must be incremental, full_scan, or reindex.")
    run = KnowledgeSyncRun(
        id=new_id("sync"),
        source_connection_id=source.id,
        mode=mode,
        status="queued",
        current_step="queued",
        stats_json={"discovered": 0, "changed": 0, "unchanged": 0, "failed": 0, "deleted": 0},
    )
    source.last_sync_run_id = run.id
    session.add(run)
    await session.flush()
    return run


async def enqueue_due_feishu_sync_runs(
    session: AsyncSession,
    *,
    now: datetime | None = None,
) -> list[KnowledgeSyncRun]:
    """Queue one incremental run for each due, fully configured Feishu source."""

    current = now or datetime.now(timezone.utc)
    active_source_ids = set(
        (
            await session.execute(
                select(KnowledgeSyncRun.source_connection_id).where(
                    KnowledgeSyncRun.status.in_(["queued", "running"])
                )
            )
        ).scalars()
    )
    sources = (
        await session.execute(
            select(KnowledgeSourceConnection).where(
                KnowledgeSourceConnection.connector_key == "feishu_wiki",
                KnowledgeSourceConnection.status.in_(["ready", "error"]),
            )
        )
    ).scalars()
    queued: list[KnowledgeSyncRun] = []
    for source in sources:
        config = dict(source.config_json or {})
        if source.id in active_source_ids or not str(config.get("space_id") or "").strip():
            continue
        try:
            interval_minutes = int((source.schedule_json or {}).get("interval_minutes") or 0)
        except (TypeError, ValueError):
            interval_minutes = 0
        if interval_minutes <= 0:
            continue
        last_synced = source.last_synced_at
        if last_synced is not None and last_synced.tzinfo is None:
            last_synced = last_synced.replace(tzinfo=timezone.utc)
        if last_synced is not None and last_synced > current - timedelta(minutes=interval_minutes):
            continue
        run = await create_sync_run(session, source=source, mode="incremental")
        source.status = "syncing"
        queued.append(run)
        active_source_ids.add(source.id)
    return queued


def source_to_dict(source: KnowledgeSourceConnection, *, item_count: int | None = None) -> dict[str, Any]:
    payload = {
        "id": source.id,
        "knowledge_base_id": source.knowledge_base_id,
        "connector_key": source.connector_key,
        "name": source.name,
        "status": source.status,
        "auth_type": source.auth_type,
        "credential_configured": bool(source.credential_ref),
        "config": dict(source.config_json or {}),
        "schedule": dict(source.schedule_json or {}),
        "last_sync_run_id": source.last_sync_run_id,
        "last_synced_at": iso_utc(source.last_synced_at),
        "last_error": dict(source.last_error_json or {}),
        "builtin": source.connector_key in _BUILTIN_NAMES,
        "created_at": iso_utc(source.created_at),
        "updated_at": iso_utc(source.updated_at),
    }
    if item_count is not None:
        payload["item_count"] = item_count
    return payload


def source_item_to_dict(item: KnowledgeSourceItem) -> dict[str, Any]:
    status = "ready" if item.status in {"completed", "succeeded", "success"} else item.status
    return {
        "id": item.id,
        "knowledge_base_id": item.knowledge_base_id,
        "source_connection_id": item.source_connection_id,
        "external_id": item.external_id,
        "external_parent_id": item.external_parent_id,
        "external_type": item.external_type,
        "title": item.title,
        "source_url": item.source_url,
        "path": list(item.path_json or []),
        "revision": item.revision,
        "content_sha256": item.content_sha256,
        "document_id": item.document_id,
        "status": status,
        "metadata": dict(item.metadata_json or {}),
        "permissions": dict(item.permissions_json or {}),
        "remote_created_at": iso_utc(item.remote_created_at),
        "remote_updated_at": iso_utc(item.remote_updated_at),
        "created_at": iso_utc(item.created_at),
        "updated_at": iso_utc(item.updated_at),
    }


async def list_recent_sync_runs(
    session: AsyncSession,
    *,
    knowledge_base_id: str = DEFAULT_KNOWLEDGE_BASE_ID,
    limit: int = 50,
) -> list[dict[str, Any]]:
    """Recent sync runs across all sources of a base, with source display fields."""
    stmt = (
        select(KnowledgeSyncRun, KnowledgeSourceConnection.name, KnowledgeSourceConnection.connector_key)
        .join(KnowledgeSourceConnection, KnowledgeSyncRun.source_connection_id == KnowledgeSourceConnection.id)
        .where(KnowledgeSourceConnection.knowledge_base_id == knowledge_base_id)
        .order_by(KnowledgeSyncRun.created_at.desc())
        .limit(max(1, min(limit, 200)))
    )
    result = await session.execute(stmt)
    payload: list[dict[str, Any]] = []
    for run, source_name, connector_key in result.all():
        entry = sync_run_to_dict(run)
        entry["source_name"] = source_name
        entry["connector_key"] = connector_key
        payload.append(entry)
    return payload


def sync_run_to_dict(run: KnowledgeSyncRun) -> dict[str, Any]:
    return {
        "id": run.id,
        "source_connection_id": run.source_connection_id,
        "mode": run.mode,
        "status": run.status,
        "current_step": run.current_step,
        "progress": run.progress,
        "cursor": dict(run.cursor_json or {}),
        "stats": dict(run.stats_json or {}),
        "error": dict(run.error_json or {}),
        "attempt": run.attempt,
        "started_at": iso_utc(run.started_at),
        "finished_at": iso_utc(run.finished_at),
        "created_at": iso_utc(run.created_at),
        "updated_at": iso_utc(run.updated_at),
    }


def complete_builtin_sync_run(source: KnowledgeSourceConnection, run: KnowledgeSyncRun, *, item_count: int) -> None:
    now = datetime.now(timezone.utc)
    run.status = "succeeded"
    run.current_step = "completed"
    run.progress = 100
    run.started_at = now
    run.finished_at = now
    run.stats_json = {"discovered": item_count, "changed": 0, "unchanged": item_count, "failed": 0, "deleted": 0}
    source.last_synced_at = now
    source.status = "ready"
