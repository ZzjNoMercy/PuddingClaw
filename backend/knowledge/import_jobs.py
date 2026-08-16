"""Knowledge import job queue helpers."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from analytics.nl2sql.training import VannaTrainingError, import_table_entities
from knowledge.indexer import refresh_document_knowledge_index
from knowledge.models import (
    KnowledgeDatabaseSource,
    KnowledgeDocument,
    KnowledgeImportEvent,
    KnowledgeImportJob,
    new_id,
)
from knowledge.paths import get_knowledge_root
from knowledge.queue_repository import (
    claim_next,
    current_lease_owner,
    new_worker_id,
    require_current_lease,
    require_lease,
)
from knowledge.service import (
    DEFAULT_KNOWLEDGE_BASE_ID,
    GENERIC_UPLOAD_SUFFIXES,
    MARKDOWN_SUFFIXES,
    PDF_SUFFIXES,
    KnowledgeService,
    KnowledgeServiceError,
    _slugify,
)
from runtime_control import assert_writes_allowed, writes_allowed

logger = logging.getLogger(__name__)

JOB_TERMINAL_STATUSES = {"succeeded", "failed", "cancelled"}
VECTOR_PUBLISH_KIND = "vector_publish"
VANNA_ENTITY_IMPORT_KIND = "vanna_entity_import"
LLM_WIKI_INGEST_KIND = "llm_wiki_ingest"
READ_LATER_CAPTURE_KIND = "read_later_capture"


def job_kind(job: KnowledgeImportJob) -> str:
    value = (job.job_metadata or {}).get("kind")
    return str(value or "import")


def _database_source_snapshot(source: KnowledgeDatabaseSource | dict[str, Any]) -> dict[str, Any]:
    if isinstance(source, KnowledgeDatabaseSource):
        return {
            "id": source.id,
            "source_type": source.source_type,
            "type": source.source_type,
            "name": source.name,
            "description": source.description,
            "host": source.host,
            "port": source.port,
            "database": source.database,
            "username": source.username,
            "credential_ref": (source.source_metadata or {}).get("credential_ref", ""),
            "selected_tables": source.selected_tables or [],
        }
    payload = dict(source)
    payload["source_type"] = payload.get("source_type") or payload.get("type") or "postgresql"
    payload["type"] = payload.get("type") or payload["source_type"]
    payload["selected_tables"] = payload.get("selected_tables") if isinstance(payload.get("selected_tables"), list) else []
    payload.pop("password", None)
    return payload


def _redact_database_source(source: Any) -> dict[str, Any] | None:
    if not isinstance(source, dict):
        return None
    allowed_keys = (
        "id",
        "source_type",
        "type",
        "name",
        "description",
        "host",
        "port",
        "database",
        "username",
        "selected_tables",
    )
    return {key: source.get(key) for key in allowed_keys if key in source}


def _job_metadata_for_api(job: KnowledgeImportJob) -> dict[str, Any]:
    metadata = dict(job.job_metadata or {})
    kind = str(metadata.get("kind") or "import")
    if kind != VANNA_ENTITY_IMPORT_KIND:
        return metadata

    slim_metadata: dict[str, Any] = {"kind": kind}
    for key in (
        "database_source_id",
        "table_name",
        "column",
        "entity_type",
        "alias_columns",
        "filters",
        "max_values",
        "progress_detail",
        "deepagents_backend",
    ):
        if key in metadata:
            slim_metadata[key] = metadata[key]
    source = _redact_database_source(metadata.get("database_source"))
    if source is not None:
        slim_metadata["database_source"] = source
    result = metadata.get("result")
    if isinstance(result, dict):
        slim_metadata["result"] = {
            key: result.get(key)
            for key in (
                "ok",
                "source_table",
                "table_column",
                "entity_type",
                "count",
                "updated",
                "skipped_duplicates",
                "failed",
                "total",
            )
            if key in result
        }
    return slim_metadata


def job_to_dict(job: KnowledgeImportJob) -> dict[str, Any]:
    return {
        "id": job.id,
        "knowledge_base_id": job.knowledge_base_id,
        "status": job.status,
        "file_name": job.file_name,
        "file_type": job.file_type,
        "file_size": job.file_size,
        "source_path": job.source_path,
        "source_sha256": job.source_sha256,
        "title": job.title,
        "publish_targets": job.publish_targets,
        "current_step": job.current_step,
        "progress": job.progress,
        "document_id": job.document_id,
        "error_message": job.error_message,
        "retry_count": job.retry_count,
        "metadata": _job_metadata_for_api(job),
        "created_at": job.created_at.isoformat() if job.created_at else None,
        "updated_at": job.updated_at.isoformat() if job.updated_at else None,
        "started_at": job.started_at.isoformat() if job.started_at else None,
        "finished_at": job.finished_at.isoformat() if job.finished_at else None,
    }


def job_to_list_dict(job: KnowledgeImportJob) -> dict[str, Any]:
    metadata = dict(job.job_metadata or {})
    kind = str(metadata.get("kind") or "import")
    slim_metadata: dict[str, Any] = {
        "kind": kind,
    }
    for key in (
        "source_job_id",
        "vector_job_status",
        "vector_error_message",
        "vector_progress",
        "database_source_id",
        "table_name",
        "column",
        "entity_type",
        "alias_columns",
        "filters",
        "max_values",
        "progress_detail",
        "raw_paths",
        "raw_count",
        "bundle_hash",
        "agents_sha256",
        "compiler_model_id",
        "compiler_model",
        "compiler_provider",
        "compiler_runtime",
        "published_pages",
        "lint_ok",
        "wiki_stage_complete",
        "import_gbrain",
        "gbrain_import_ok",
        "read_later_item_id",
        "parse_status",
    ):
        if key in metadata:
            slim_metadata[key] = metadata[key]

    return {
        "id": job.id,
        "knowledge_base_id": job.knowledge_base_id,
        "status": job.status,
        "file_name": job.file_name,
        "file_type": job.file_type,
        "file_size": job.file_size,
        "source_path": job.source_path,
        "source_sha256": job.source_sha256,
        "title": job.title,
        "publish_targets": job.publish_targets,
        "current_step": job.current_step,
        "progress": job.progress,
        "document_id": job.document_id,
        "error_message": job.error_message,
        "retry_count": job.retry_count,
        "metadata": slim_metadata,
        "created_at": job.created_at.isoformat() if job.created_at else None,
        "updated_at": job.updated_at.isoformat() if job.updated_at else None,
        "started_at": job.started_at.isoformat() if job.started_at else None,
        "finished_at": job.finished_at.isoformat() if job.finished_at else None,
    }


def event_to_dict(event: KnowledgeImportEvent) -> dict[str, Any]:
    return {
        "id": event.id,
        "job_id": event.job_id,
        "level": event.level,
        "message": event.message,
        "metadata": event.event_metadata,
        "created_at": event.created_at.isoformat() if event.created_at else None,
    }


def detect_file_type(filename: str) -> str:
    suffix = Path(filename).suffix.lower()
    if suffix in PDF_SUFFIXES:
        return "pdf"
    if suffix in MARKDOWN_SUFFIXES:
        return "markdown"
    if suffix in GENERIC_UPLOAD_SUFFIXES:
        return suffix.removeprefix(".") or "file"
    return "file"


def validate_supported_file(filename: str) -> None:
    suffix = Path(filename).suffix.lower()
    if suffix in PDF_SUFFIXES or suffix in MARKDOWN_SUFFIXES or suffix in GENERIC_UPLOAD_SUFFIXES:
        return
    raise KnowledgeServiceError(
        f"Unsupported knowledge file type: {suffix or 'unknown'}. Supported: .pdf, .md, .markdown, .xlsx, .xls, .csv, .tsv, .txt, .docx"
    )


def task_source_path(base_dir: Path, *, job_id: str, filename: str) -> Path:
    knowledge_root = get_knowledge_root(base_dir)
    safe_name = _slugify(filename or "document")
    return knowledge_root / ".tasks" / job_id / "source" / safe_name


async def create_import_job(
    session: AsyncSession,
    *,
    base_dir: Path,
    filename: str,
    source_path: Path,
    file_size: int,
    source_sha256: str,
    title: str | None = None,
    knowledge_base_id: str = DEFAULT_KNOWLEDGE_BASE_ID,
    publish_targets: list[str] | None = None,
) -> KnowledgeImportJob:
    await assert_writes_allowed(session)
    service = KnowledgeService(base_dir)
    await service.ensure_default_knowledge_base(session)
    validate_supported_file(filename)
    job = KnowledgeImportJob(
        id=source_path.parents[1].name if source_path.parent.name == "source" else new_id("job"),
        knowledge_base_id=knowledge_base_id,
        status="queued",
        file_name=filename,
        file_type=detect_file_type(filename),
        file_size=file_size,
        source_path=str(source_path),
        source_sha256=source_sha256,
        title=(title or "").strip() or None,
        publish_targets=publish_targets or ["local_markdown"],
        current_step="queued",
        progress=0,
        job_metadata={"deepagents_backend": "/knowledge/"},
    )
    session.add(job)
    session.add(KnowledgeImportEvent(job_id=job.id, level="info", message="任务已加入导入队列"))
    await session.commit()
    await session.refresh(job)
    return job


async def create_llm_wiki_ingest_job(
    session: AsyncSession,
    *,
    base_dir: Path,
    raw_paths: list[str],
    knowledge_base_id: str = DEFAULT_KNOWLEDGE_BASE_ID,
    import_gbrain: bool = False,
) -> KnowledgeImportJob:
    """Queue one immutable, Schema-bound LLM Wiki compilation."""

    await assert_writes_allowed(session)
    from config import get_llm_wiki_compiler_agent_config
    from knowledge.llm_wiki import get_llm_wiki_service

    service = KnowledgeService(base_dir)
    await service.ensure_default_knowledge_base(session)
    selected_paths = list(dict.fromkeys(str(path).strip() for path in raw_paths if str(path).strip()))
    if not selected_paths:
        raise KnowledgeServiceError("请至少选择一个待编译的 Raw 快照。")

    wiki = get_llm_wiki_service(base_dir)
    context = await asyncio.to_thread(wiki.freeze_ingest_inputs, selected_paths)
    manifest = context.get("raw_manifest") if isinstance(context.get("raw_manifest"), list) else []
    if len(manifest) != len(selected_paths):
        raise KnowledgeServiceError("部分 Raw 快照不在当前不可变清单中，请刷新后重试。")
    manifest_by_path = {str(item.get("snapshot_path") or ""): item for item in manifest if isinstance(item, dict)}
    ordered_manifest = [manifest_by_path[path] for path in selected_paths]
    bundle = context.get("schema_bundle") if isinstance(context.get("schema_bundle"), dict) else {}
    agents = bundle.get("agents") if isinstance(bundle.get("agents"), dict) else {}
    file_size = sum(int(item.get("size_bytes") or 0) for item in ordered_manifest)
    raw_hashes = {path: str(manifest_by_path[path].get("sha256") or "") for path in selected_paths}
    compiler = get_llm_wiki_compiler_agent_config()
    job_fingerprint = hashlib.sha256(
        json.dumps(
            {
                "raw_hashes": sorted((path, raw_hashes[path]) for path in selected_paths),
                "bundle_hash": str(bundle.get("bundle_hash") or ""),
                "compiler_model_id": str(compiler.get("model_id") or ""),
                "import_gbrain": bool(import_gbrain),
            },
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    deterministic_job_id = f"job-wiki-{job_fingerprint[:40]}"
    existing = await session.get(KnowledgeImportJob, deterministic_job_id)
    if existing is not None:
        return existing

    job = KnowledgeImportJob(
        id=deterministic_job_id,
        knowledge_base_id=knowledge_base_id,
        status="queued",
        file_name=f"{len(selected_paths)} 个 Raw 快照",
        file_type="llm_wiki",
        file_size=file_size,
        source_path="llm-wiki://raw",
        source_sha256=job_fingerprint,
        title=(
            f"LLM Wiki 编译并导入 GBrain（{len(selected_paths)} 个 Raw）"
            if import_gbrain
            else f"LLM Wiki 编译（{len(selected_paths)} 个 Raw）"
        ),
        publish_targets=["llm_wiki", *(["gbrain"] if import_gbrain else [])],
        current_step="queued",
        progress=0,
        job_metadata={
            "kind": LLM_WIKI_INGEST_KIND,
            "raw_paths": selected_paths,
            "raw_count": len(selected_paths),
            "raw_hashes": raw_hashes,
            "bundle_hash": str(bundle.get("bundle_hash") or ""),
            "agents_sha256": str(agents.get("sha256") or ""),
            "compiler_model_id": str(compiler.get("model_id") or ""),
            "compiler_model": str(compiler.get("model") or ""),
            "compiler_provider": str(compiler.get("provider") or ""),
            "compiler_runtime": "llm_wiki_compiler_agent",
            "import_gbrain": bool(import_gbrain),
            "deepagents_backend": "/knowledge/brain/wiki/",
        },
    )
    session.add(job)
    session.add(
        KnowledgeImportEvent(
            job_id=job.id,
            level="info",
            message=(
                f"LLM Wiki 编译并导入 GBrain 任务已加入队列，共 {len(selected_paths)} 个 Raw 快照"
                if import_gbrain
                else f"LLM Wiki 编译任务已加入队列，共 {len(selected_paths)} 个 Raw 快照"
            ),
            event_metadata={
                "raw_paths": selected_paths,
                "compiler_model_id": str(compiler.get("model_id") or ""),
                "compiler_model": str(compiler.get("model") or ""),
            },
        )
    )
    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()
        existing = await session.get(KnowledgeImportJob, deterministic_job_id)
        if existing is not None:
            return existing
        raise
    await session.refresh(job)
    return job


async def create_vector_publish_job(
    session: AsyncSession,
    *,
    base_dir: Path,
    source_job: KnowledgeImportJob,
) -> KnowledgeImportJob:
    await assert_writes_allowed(session)
    service = KnowledgeService(base_dir)
    await service.ensure_default_knowledge_base(session)
    if not source_job.document_id:
        raise KnowledgeServiceError("任务还没有生成知识库文档，暂时不能导入向量。")

    document = await session.get(KnowledgeDocument, source_job.document_id)
    if document is None:
        raise KnowledgeServiceError("知识库文档不存在，不能导入向量。")

    source_path = Path(document.storage_path)
    if not source_path.exists():
        source_path = Path(source_job.source_path)

    job = KnowledgeImportJob(
        id=new_id("job"),
        knowledge_base_id=source_job.knowledge_base_id,
        status="queued",
        file_name=source_job.file_name,
        file_type="vector",
        file_size=source_job.file_size,
        source_path=str(source_path),
        source_sha256=source_job.source_sha256 or document.content_sha256 or "",
        title=source_job.title or document.title,
        publish_targets=["vector"],
        current_step="queued",
        progress=0,
        document_id=document.id,
        job_metadata={
            "kind": VECTOR_PUBLISH_KIND,
            "source_job_id": source_job.id,
            "document_virtual_path": document.virtual_path,
            "deepagents_backend": "/knowledge/",
        },
    )
    source_job.job_metadata = {
        **(source_job.job_metadata or {}),
        "active_vector_job_id": job.id,
        "vector_job_status": "queued",
        "vector_progress": {
            "stage": "queued",
            "text_done": 0,
            "text_total": 0,
            "image_done": 0,
            "image_total": 0,
            "done": 0,
            "total": 0,
        },
    }
    session.add(job)
    session.add(KnowledgeImportEvent(job_id=job.id, level="info", message="向量导入任务已加入队列"))
    session.add(
        KnowledgeImportEvent(
            job_id=source_job.id,
            level="info",
            message="已创建向量导入任务",
            event_metadata={"vector_job_id": job.id},
        )
    )
    await session.commit()
    await session.refresh(job)
    return job


async def create_document_vector_publish_job(
    session: AsyncSession,
    *,
    base_dir: Path,
    document: KnowledgeDocument,
) -> KnowledgeImportJob:
    await assert_writes_allowed(session)
    service = KnowledgeService(base_dir)
    await service.ensure_default_knowledge_base(session)

    active_stmt = (
        select(KnowledgeImportJob)
        .where(
            KnowledgeImportJob.file_type == "vector",
            KnowledgeImportJob.document_id == document.id,
            KnowledgeImportJob.status.in_(("queued", "running")),
        )
        .order_by(KnowledgeImportJob.created_at.desc())
        .limit(1)
    )
    active_result = await session.execute(active_stmt)
    active_job = active_result.scalar_one_or_none()
    if active_job is not None:
        return active_job

    job = KnowledgeImportJob(
        id=new_id("job"),
        knowledge_base_id=document.knowledge_base_id,
        status="queued",
        file_name=Path(document.storage_path).name,
        file_type="vector",
        file_size=document.size_bytes,
        source_path=document.storage_path,
        source_sha256=document.content_sha256 or "",
        title=f"重建索引：{document.title}",
        publish_targets=["vector"],
        current_step="queued",
        progress=0,
        document_id=document.id,
        job_metadata={
            "kind": VECTOR_PUBLISH_KIND,
            "document_virtual_path": document.virtual_path,
            "deepagents_backend": "/knowledge/",
        },
    )
    session.add(job)
    session.add(KnowledgeImportEvent(job_id=job.id, level="info", message="文档向量重建任务已加入队列"))
    await session.commit()
    await session.refresh(job)
    return job


async def create_vanna_entity_import_job(
    session: AsyncSession,
    *,
    base_dir: Path,
    source: KnowledgeDatabaseSource | dict[str, Any],
    table_name: str,
    column: str,
    entity_type: str,
    alias_columns: list[str] | None = None,
    filters: list[dict[str, Any]] | None = None,
    max_values: int | None = None,
    knowledge_base_id: str = DEFAULT_KNOWLEDGE_BASE_ID,
) -> KnowledgeImportJob:
    await assert_writes_allowed(session)
    service = KnowledgeService(base_dir)
    await service.ensure_default_knowledge_base(session)

    source_snapshot = _database_source_snapshot(source)
    clean_table = str(table_name or "").strip()
    clean_column = str(column or "").strip()
    clean_entity_type = str(entity_type or "").strip()
    if not clean_table:
        raise KnowledgeServiceError("请选择数据库表。")
    if not clean_column:
        raise KnowledgeServiceError("请选择实体字段。")
    if not clean_entity_type:
        raise KnowledgeServiceError("请填写实体类型。")
    clean_alias_columns = [
        str(item).strip()
        for item in alias_columns or []
        if str(item or "").strip() and str(item).strip() != clean_column
    ]
    clean_filters: list[dict[str, Any]] = []
    for raw_filter in filters or []:
        if not isinstance(raw_filter, dict):
            raise KnowledgeServiceError("实体过滤条件格式无效。")
        filter_column = str(raw_filter.get("column") or "").strip()
        operator = str(raw_filter.get("operator") or "").strip()
        values = list(
            dict.fromkeys(
                str(item).strip()
                for item in raw_filter.get("values") or []
                if str(item or "").strip()
            )
        )
        if not filter_column or operator not in {"in", "not_in"} or not values:
            raise KnowledgeServiceError("实体过滤条件必须选择字段、操作符和已有值。")
        clean_filters.append({"column": filter_column, "operator": operator, "values": values})
    clean_max_values = int(max_values) if max_values is not None else None
    if clean_max_values is not None and clean_max_values < 1:
        raise KnowledgeServiceError("导入数量必须大于 0。")
    source_id = str(source_snapshot.get("id") or "database")
    source_name = str(source_snapshot.get("name") or source_id)

    job = KnowledgeImportJob(
        id=new_id("job"),
        knowledge_base_id=knowledge_base_id,
        status="queued",
        file_name=f"{source_name}:{clean_table}.{clean_column}",
        file_type="vanna_entity",
        file_size=0,
        source_path=f"database://{source_id}/{clean_table}.{clean_column}",
        source_sha256="",
        title=f"{clean_table}.{clean_column} 实体导入",
        publish_targets=["vanna_entity"],
        current_step="queued",
        progress=0,
        job_metadata={
            "kind": VANNA_ENTITY_IMPORT_KIND,
            "database_source_id": source_id,
            "database_source": source_snapshot,
            "table_name": clean_table,
            "column": clean_column,
            "entity_type": clean_entity_type,
            "alias_columns": clean_alias_columns,
            "filters": clean_filters,
            "max_values": clean_max_values,
            "progress_detail": {
                "stage": "queued",
                "done": 0,
                "total": 0,
                "imported": 0,
                "failed": 0,
                "batch_size": 100,
            },
            "deepagents_backend": "/knowledge/",
        },
    )
    session.add(job)
    session.add(KnowledgeImportEvent(job_id=job.id, level="info", message="实体导入任务已加入队列"))
    await session.commit()
    await session.refresh(job)
    return job


async def list_import_jobs(
    session: AsyncSession,
    *,
    knowledge_base_id: str = DEFAULT_KNOWLEDGE_BASE_ID,
    limit: int = 50,
) -> list[KnowledgeImportJob]:
    stmt = (
        select(KnowledgeImportJob)
        .where(KnowledgeImportJob.knowledge_base_id == knowledge_base_id)
        .order_by(KnowledgeImportJob.created_at.desc())
        .limit(max(1, min(limit, 200)))
    )
    result = await session.execute(stmt)
    return list(result.scalars())


async def get_import_job(session: AsyncSession, job_id: str) -> KnowledgeImportJob | None:
    return await session.get(KnowledgeImportJob, job_id)


async def delete_import_job(session: AsyncSession, job_id: str) -> None:
    job = await get_import_job(session, job_id)
    if job is None:
        raise KnowledgeServiceError(f"Import job not found: {job_id}")
    if job.status == "running":
        raise KnowledgeServiceError("任务正在处理中，完成后再删除。")
    await session.delete(job)
    await session.commit()


async def clear_import_jobs(
    session: AsyncSession,
    *,
    knowledge_base_id: str = DEFAULT_KNOWLEDGE_BASE_ID,
) -> int:
    stmt = select(KnowledgeImportJob).where(
        KnowledgeImportJob.knowledge_base_id == knowledge_base_id,
        KnowledgeImportJob.status != "running",
    )
    result = await session.execute(stmt)
    jobs = list(result.scalars())
    for job in jobs:
        await session.delete(job)
    await session.commit()
    return len(jobs)


async def list_import_events(session: AsyncSession, job_id: str, *, limit: int = 100) -> list[KnowledgeImportEvent]:
    stmt = (
        select(KnowledgeImportEvent)
        .where(KnowledgeImportEvent.job_id == job_id)
        .order_by(KnowledgeImportEvent.created_at.asc())
        .limit(max(1, min(limit, 500)))
    )
    result = await session.execute(stmt)
    return list(result.scalars())


async def list_related_import_events(
    session: AsyncSession,
    job: KnowledgeImportJob,
    *,
    limit: int = 500,
) -> list[KnowledgeImportEvent]:
    """Return the full processing timeline for the same document pipeline.

    A parsed file job and its vector-publish job are separate queue records so
    they can run independently, but users expect either detail page to show the
    complete document flow.
    """

    job_ids: set[str] = {job.id}
    metadata = job.job_metadata or {}

    source_job_id = metadata.get("source_job_id")
    if isinstance(source_job_id, str) and source_job_id:
        job_ids.add(source_job_id)

    active_vector_job_id = metadata.get("active_vector_job_id")
    if isinstance(active_vector_job_id, str) and active_vector_job_id:
        job_ids.add(active_vector_job_id)

    if job.document_id:
        related_stmt = select(KnowledgeImportJob).where(KnowledgeImportJob.document_id == job.document_id)
        related_result = await session.execute(related_stmt)
        for related_job in related_result.scalars():
            job_ids.add(related_job.id)

    stmt = (
        select(KnowledgeImportEvent)
        .where(KnowledgeImportEvent.job_id.in_(job_ids))
        .order_by(KnowledgeImportEvent.created_at.asc(), KnowledgeImportEvent.id.asc())
        .limit(max(1, min(limit, 1000)))
    )
    result = await session.execute(stmt)
    return list(result.scalars())


async def retry_import_job(session: AsyncSession, job_id: str) -> KnowledgeImportJob:
    await assert_writes_allowed(session)
    job = await get_import_job(session, job_id)
    if job is None:
        raise KnowledgeServiceError(f"Import job not found: {job_id}")
    if job.status not in {"failed", "cancelled"}:
        raise KnowledgeServiceError("Only failed or cancelled import jobs can be retried.")
    job.status = "queued"
    job.current_step = "queued"
    job.progress = 0
    job.error_message = None
    job.started_at = None
    job.finished_at = None
    job.lease_owner = None
    job.lease_expires_at = None
    job.heartbeat_at = None
    job.retry_count += 1
    session.add(KnowledgeImportEvent(job_id=job.id, level="info", message="任务已重新加入队列"))
    await session.commit()
    await session.refresh(job)
    return job


async def claim_next_job(
    session: AsyncSession,
    *,
    worker_id: str | None = None,
    lease_seconds: int | None = None,
) -> KnowledgeImportJob | None:
    # Drain/maintenance gate: stop claiming new work; in-flight jobs keep
    # heartbeating under the queue lease protocol.
    if not await writes_allowed(session):
        return None
    # End the read transaction opened by the writes_allowed probe so the claim
    # UPDATE starts a fresh transaction. Under SQLite WAL, upgrading a stale
    # read snapshot to a write fails immediately with SQLITE_BUSY_SNAPSHOT,
    # which busy_timeout does not cover.
    await session.rollback()
    job = await claim_next(
        session,
        KnowledgeImportJob,
        worker_id=worker_id or new_worker_id("manual"),
        lease_seconds=lease_seconds,
        extra_sets={"current_step": "starting", "progress": 5, "finished_at": None, "error_message": None},
    )
    if job is None:
        return None
    kind = job_kind(job)
    if kind == VECTOR_PUBLISH_KIND:
        message = "开始导入向量"
    elif kind == VANNA_ENTITY_IMPORT_KIND:
        message = "开始导入实体"
    elif kind == LLM_WIKI_INGEST_KIND:
        message = "开始编译 LLM Wiki"
    elif kind == READ_LATER_CAPTURE_KIND:
        message = "开始解析稍后读链接"
    else:
        message = "开始导入"
    session.add(KnowledgeImportEvent(job_id=job.id, level="info", message=message))
    await session.commit()
    await session.refresh(job)
    return job


async def update_job_progress(
    session: AsyncSession,
    job: KnowledgeImportJob,
    *,
    step: str,
    progress: int,
    message: str | None = None,
    event_metadata: dict[str, Any] | None = None,
    metadata_patch: dict[str, Any] | None = None,
    record_event: bool = True,
    lease_owner: str | None = None,
) -> None:
    lease_owner = lease_owner or current_lease_owner()
    if lease_owner is not None:
        await require_lease(session, KnowledgeImportJob, job.id, lease_owner)
    job.current_step = step
    job.progress = max(0, min(100, progress))
    if metadata_patch:
        job.job_metadata = {**(job.job_metadata or {}), **metadata_patch}
    if message and record_event:
        session.add(KnowledgeImportEvent(job_id=job.id, level="info", message=message, event_metadata=event_metadata or {}))
    await session.commit()


async def mark_job_failed(
    session: AsyncSession,
    job: KnowledgeImportJob,
    error: Exception | str,
    *,
    lease_owner: str | None = None,
) -> None:
    lease_owner = lease_owner or current_lease_owner()
    if lease_owner is not None:
        await require_lease(session, KnowledgeImportJob, job.id, lease_owner)
    message = str(error)
    job.status = "failed"
    job.current_step = "failed"
    job.error_message = message
    job.finished_at = datetime.now(timezone.utc)
    session.add(KnowledgeImportEvent(job_id=job.id, level="error", message=message))
    if job_kind(job) == VECTOR_PUBLISH_KIND:
        source_job_id = (job.job_metadata or {}).get("source_job_id")
        source_job = await session.get(KnowledgeImportJob, source_job_id) if isinstance(source_job_id, str) else None
        if source_job is not None:
            source_job.job_metadata = {
                **(source_job.job_metadata or {}),
                "active_vector_job_id": job.id,
                "vector_job_status": "failed",
                "vector_error_message": message,
            }
            session.add(
                KnowledgeImportEvent(
                    job_id=source_job.id,
                    level="error",
                    message=f"Milvus 向量导入失败：{message}",
                    event_metadata={"vector_job_id": job.id},
                )
            )
    job.lease_owner = None
    job.lease_expires_at = None
    job.heartbeat_at = None
    await session.commit()


async def process_vanna_entity_import_job(
    session: AsyncSession,
    *,
    base_dir: Path,
    job: KnowledgeImportJob,
) -> KnowledgeImportJob:
    metadata = dict(job.job_metadata or {})
    source = metadata.get("database_source")
    if not isinstance(source, dict):
        raise KnowledgeServiceError("实体导入任务缺少数据库源快照。")

    table_name = str(metadata.get("table_name") or "").strip()
    column = str(metadata.get("column") or "").strip()
    entity_type = str(metadata.get("entity_type") or "").strip()
    alias_columns = [str(item).strip() for item in metadata.get("alias_columns") or [] if str(item or "").strip()]
    filters = [dict(item) for item in metadata.get("filters") or [] if isinstance(item, dict)]
    raw_max_values = metadata.get("max_values")
    max_values = int(raw_max_values) if raw_max_values not in (None, "", 0) else None
    batch_size = int((metadata.get("progress_detail") or {}).get("batch_size") or 100)

    await update_job_progress(
        session,
        job,
        step="preparing",
        progress=10,
        message="准备实体导入",
        metadata_patch={
            "progress_detail": {
                "stage": "preparing",
                "done": 0,
                "total": 0,
                "imported": 0,
                "failed": 0,
                "batch_size": batch_size,
            }
        },
    )

    last_event_done = 0

    async def _persist_entity_progress(progress_payload: dict[str, Any]) -> None:
        nonlocal last_event_done
        done = int(progress_payload.get("done") or 0)
        total = int(progress_payload.get("total") or 0)
        stage = str(progress_payload.get("stage") or "indexing")
        percent = 15 + int((done / total) * 75) if total > 0 else 15
        if stage == "done":
            percent = 95
        message: str | None = None
        record_event = False
        if total > 0 and done == 0 and last_event_done == 0:
            message = f"开始写入实体：0/{total}"
            record_event = True
        elif total > 0 and (done == total or done - last_event_done >= max(batch_size, 500)):
            message = f"实体导入进度：{done}/{total}"
            record_event = True
            last_event_done = done
        await update_job_progress(
            session,
            job,
            step="indexing",
            progress=percent,
            message=message,
            event_metadata=progress_payload,
            metadata_patch={"progress_detail": progress_payload},
            record_event=record_event,
        )

    try:
        result = await import_table_entities(
            source,
            table_name=table_name,
            column=column,
            entity_type=entity_type,
            alias_columns=alias_columns,
            filters=filters,
            max_values=max_values,
            batch_size=batch_size,
            continue_on_error=True,
            on_progress=_persist_entity_progress,
        )
    except VannaTrainingError as exc:
        raise KnowledgeServiceError(str(exc)) from exc

    total = int(result.get("total") or result.get("count") or 0)
    failed = int(result.get("failed") or 0)
    count = int(result.get("count") or 0)
    updated = int(result.get("updated") or 0)
    skipped_duplicates = int(result.get("skipped_duplicates") or 0)
    progress_detail = {
        "stage": "done",
        "done": count + updated + skipped_duplicates + failed,
        "total": total,
        "imported": count,
        "updated": updated,
        "skipped_duplicates": skipped_duplicates,
        "failed": failed,
        "batch_size": batch_size,
    }
    await update_job_progress(
        session,
        job,
        step="finalizing",
        progress=95,
        message="更新实体导入记录",
        metadata_patch={"progress_detail": progress_detail, "result": result},
    )

    await require_current_lease(session, KnowledgeImportJob, job.id)
    job.status = "succeeded"
    job.current_step = "done"
    job.progress = 100
    job.error_message = None
    job.finished_at = datetime.now(timezone.utc)
    job.lease_owner = None
    job.lease_expires_at = None
    job.heartbeat_at = None
    job.job_metadata = {**(job.job_metadata or {}), "progress_detail": progress_detail, "result": result}
    session.add(
        KnowledgeImportEvent(
            job_id=job.id,
            level="info",
            message=f"实体导入完成：新增 {count}，更新 {updated}，跳过重复 {skipped_duplicates}，失败 {failed} / {total}",
            event_metadata={
                "count": count,
                "updated": updated,
                "skipped_duplicates": skipped_duplicates,
                "total": total,
                "failed": failed,
                "table_column": result.get("table_column"),
            },
        )
    )
    await session.commit()
    await session.refresh(job)
    return job


async def process_import_job(session: AsyncSession, *, base_dir: Path, job: KnowledgeImportJob) -> KnowledgeImportJob:
    kind = job_kind(job)
    if kind == VECTOR_PUBLISH_KIND:
        return await process_vector_publish_job(session, base_dir=base_dir, job=job)
    if kind == VANNA_ENTITY_IMPORT_KIND:
        return await process_vanna_entity_import_job(session, base_dir=base_dir, job=job)

    service = KnowledgeService(base_dir)
    source_path = Path(job.source_path)
    if not source_path.exists():
        raise KnowledgeServiceError(f"上传文件不存在：{source_path}")

    await update_job_progress(session, job, step="reading", progress=10, message="读取上传文件")
    content = source_path.read_bytes()
    actual_sha256 = hashlib.sha256(content).hexdigest()
    if job.source_sha256 and actual_sha256 != job.source_sha256:
        raise KnowledgeServiceError("上传文件校验失败，请重新导入。")

    suffix = Path(job.file_name).suffix.lower()
    await update_job_progress(session, job, step="parsing", progress=25, message="开始解析文件")
    if suffix in PDF_SUFFIXES:
        document, ingest = await service.ingest_pdf_upload(
            session,
            filename=job.file_name,
            content=content,
            title=job.title,
            knowledge_base_id=job.knowledge_base_id,
            publish_targets=job.publish_targets,
        )
    elif suffix in MARKDOWN_SUFFIXES:
        document, ingest = await service.ingest_markdown_upload(
            session,
            filename=job.file_name,
            content=content,
            title=job.title,
            knowledge_base_id=job.knowledge_base_id,
            publish_targets=job.publish_targets,
        )
        if "llm_wiki_raw" in (job.publish_targets or []):
            from knowledge.llm_wiki import LlmWikiError, get_llm_wiki_service

            await update_job_progress(
                session,
                job,
                step="snapshotting_raw",
                progress=75,
                message="复制最终 Markdown 到 LLM Wiki Raw",
            )
            try:
                raw_snapshot = await asyncio.to_thread(
                    get_llm_wiki_service(base_dir).snapshot_raw_file,
                    source_id="knowledge-upload",
                    asset_id=document.id,
                    title=document.title,
                    path=Path(document.storage_path),
                    source_path=document.virtual_path,
                )
            except (LlmWikiError, OSError) as exc:
                raw_error = f"创建 LLM Wiki Raw 失败：{exc}"
                job.publish_targets = [target for target in (job.publish_targets or []) if target != "llm_wiki_raw"]
                document.publish_targets = [
                    target for target in (document.publish_targets or []) if target != "llm_wiki_raw"
                ]
                ingest = {**(ingest or {}), "llm_wiki_raw": {"ok": False, "error": raw_error}}
                session.add(
                    KnowledgeImportEvent(
                        job_id=job.id,
                        level="warning",
                        message=f"{raw_error}；知识库原文件已正常导入。",
                    )
                )
            else:
                if "llm_wiki_raw" not in (document.publish_targets or []):
                    document.publish_targets = [*(document.publish_targets or []), "llm_wiki_raw"]
                ingest = {**(ingest or {}), "llm_wiki_raw": {"ok": True, **raw_snapshot}}
    elif suffix in GENERIC_UPLOAD_SUFFIXES:
        document, ingest = await service.ingest_generic_upload(
            session,
            filename=job.file_name,
            content=content,
            title=job.title,
            knowledge_base_id=job.knowledge_base_id,
            publish_targets=job.publish_targets,
        )
    else:
        raise KnowledgeServiceError(f"Unsupported knowledge file type: {suffix or 'unknown'}")

    await update_job_progress(session, job, step="finalizing", progress=90, message="写入知识库记录")
    if suffix in {".xlsx", ".xls", ".csv", ".tsv"}:
        from analytics.table_catalog import TableAssetCatalog

        registered_assets = await TableAssetCatalog(base_dir).register_path(
            session,
            Path(document.storage_path),
            virtual_path=document.virtual_path,
            knowledge_base_id=document.knowledge_base_id,
            document_id=document.id,
        )
        ingest = {
            **(ingest or {}),
            "table_assets": [
                {
                    "asset_id": asset.asset_id,
                    "virtual_path": asset.virtual_path,
                    "sheet_name": asset.sheet_name,
                }
                for asset in registered_assets
            ],
        }
    await require_current_lease(session, KnowledgeImportJob, job.id)
    job.status = "succeeded"
    job.current_step = "done"
    job.progress = 100
    job.document_id = document.id
    job.error_message = None
    job.finished_at = datetime.now(timezone.utc)
    job.lease_owner = None
    job.lease_expires_at = None
    job.heartbeat_at = None
    job.job_metadata = {**(job.job_metadata or {}), "ingestion": ingest, "document_virtual_path": document.virtual_path}
    session.add(KnowledgeImportEvent(job_id=job.id, level="info", message="导入完成", event_metadata={"document_id": document.id}))
    await session.commit()
    await session.refresh(job)
    return job


async def process_vector_publish_job(session: AsyncSession, *, base_dir: Path, job: KnowledgeImportJob) -> KnowledgeImportJob:
    if not job.document_id:
        raise KnowledgeServiceError("任务还没有生成知识库文档，暂时不能导入向量。")

    await update_job_progress(session, job, step="preparing", progress=15, message="准备向量导入")
    metadata = dict(job.job_metadata or {})
    source_job_id = metadata.get("source_job_id")
    source_job = await session.get(KnowledgeImportJob, source_job_id) if isinstance(source_job_id, str) else None
    if source_job is not None:
        await require_current_lease(session, KnowledgeImportJob, job.id)
        source_job.job_metadata = {**(source_job.job_metadata or {}), "vector_job_status": "running"}
        await session.commit()

    document = await session.get(KnowledgeDocument, job.document_id)
    if document is None:
        raise KnowledgeServiceError("知识库文档不存在，不能导入向量。")

    await update_job_progress(session, job, step="indexing", progress=35, message="写入 Milvus 向量索引")
    loop = asyncio.get_running_loop()

    async def _persist_vector_progress(progress_payload: dict[str, Any]) -> None:
        text_done = int(progress_payload.get("text_done") or 0)
        text_total = int(progress_payload.get("text_total") or 0)
        image_done = int(progress_payload.get("image_done") or 0)
        image_total = int(progress_payload.get("image_total") or 0)
        done = int(progress_payload.get("done") or (text_done + image_done))
        total = int(progress_payload.get("total") or (text_total + image_total))
        stage = str(progress_payload.get("stage") or "indexing")
        base = 35
        span = 55
        percent = base + int((done / total) * span) if total > 0 else base
        if stage == "nodes_built":
            percent = 38
        elif stage == "done":
            percent = 90
        message = None
        if stage == "nodes_built":
            message = f"准备向量数据：文本 {text_total} 条，图片 {image_total} 张"
        elif stage == "embedding":
            modality = progress_payload.get("modality")
            if modality == "image":
                message = f"图片向量导入：{image_done}/{image_total}"
            else:
                message = f"文本向量导入：{text_done}/{text_total}"
        elif stage == "done":
            message = "向量写入完成"
        record_event = stage in {"nodes_built", "done"}
        await update_job_progress(
            session,
            job,
            step="indexing",
            progress=percent,
            message=message,
            event_metadata=progress_payload,
            metadata_patch={"vector_progress": progress_payload},
            record_event=record_event,
        )
        if source_job is not None:
            await require_current_lease(session, KnowledgeImportJob, job.id)
            source_job.job_metadata = {
                **(source_job.job_metadata or {}),
                "active_vector_job_id": job.id,
                "vector_job_status": "running",
                "vector_progress": progress_payload,
            }
            await session.commit()

    def _progress_callback(progress_payload: dict[str, Any]) -> None:
        future = asyncio.run_coroutine_threadsafe(_persist_vector_progress(dict(progress_payload)), loop)
        try:
            future.result(timeout=10)
        except Exception:
            logger.exception("[knowledge-vector] failed to persist progress job_id=%s", job.id)

    result = await asyncio.to_thread(
        refresh_document_knowledge_index,
        base_dir,
        Path(document.storage_path),
        _progress_callback,
    )
    if not result.get("refreshed"):
        reason = result.get("error") or result.get("reason") or "向量导入未完成，请检查设置或服务状态。"
        raise KnowledgeServiceError(str(reason))

    await update_job_progress(session, job, step="finalizing", progress=90, message="更新知识库记录")
    metadata = dict(job.job_metadata or {})
    ingestion = dict(metadata.get("ingestion") or {})
    ingestion["vector_index"] = result
    metadata["ingestion"] = ingestion
    metadata["vector_published_at"] = result.get("generated_at")
    job.job_metadata = metadata

    targets = list(job.publish_targets or [])
    if "vector" not in targets:
        targets.append("vector")
        job.publish_targets = targets

    doc_targets = list(document.publish_targets or [])
    if "vector" not in doc_targets:
        doc_targets.append("vector")
        document.publish_targets = doc_targets
    document.doc_metadata = {
        **(document.doc_metadata or {}),
        "vector_index": result,
    }

    source_job_id = metadata.get("source_job_id")
    if isinstance(source_job_id, str) and source_job_id:
        source_job = await session.get(KnowledgeImportJob, source_job_id)
        if source_job is not None:
            source_metadata = dict(source_job.job_metadata or {})
            source_ingestion = dict(source_metadata.get("ingestion") or {})
            source_ingestion["vector_index"] = result
            source_metadata["ingestion"] = source_ingestion
            source_metadata["vector_published_at"] = result.get("generated_at")
            source_job.job_metadata = source_metadata
            source_targets = list(source_job.publish_targets or [])
            if "vector" not in source_targets:
                source_targets.append("vector")
                source_job.publish_targets = source_targets
            vector_progress = dict((job.job_metadata or {}).get("vector_progress") or {})
            text_total = int(vector_progress.get("text_total") or 0)
            image_total = int(vector_progress.get("image_total") or 0)
            source_job.job_metadata = {
                **(source_job.job_metadata or {}),
                "active_vector_job_id": job.id,
                "vector_job_status": "succeeded",
                "vector_progress": {
                    "stage": "done",
                    "text_done": text_total,
                    "text_total": text_total,
                    "image_done": image_total,
                    "image_total": image_total,
                    "done": text_total + image_total,
                    "total": text_total + image_total,
                },
            }
            session.add(
                KnowledgeImportEvent(
                    job_id=source_job.id,
                    level="info",
                    message="Milvus 向量导入完成",
                    event_metadata={"vector_job_id": job.id, **result},
                )
            )

    await require_current_lease(session, KnowledgeImportJob, job.id)
    job.status = "succeeded"
    job.current_step = "done"
    job.progress = 100
    job.error_message = None
    job.finished_at = datetime.now(timezone.utc)
    job.lease_owner = None
    job.lease_expires_at = None
    job.heartbeat_at = None
    session.add(KnowledgeImportEvent(job_id=job.id, level="info", message="Milvus 向量导入完成", event_metadata=result))
    await session.commit()
    await session.refresh(job)
    return job
