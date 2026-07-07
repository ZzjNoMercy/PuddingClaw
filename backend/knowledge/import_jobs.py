"""Knowledge import job queue helpers."""

from __future__ import annotations

import hashlib
import asyncio
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from knowledge.indexer import refresh_local_knowledge_index
from knowledge.models import KnowledgeDocument, KnowledgeImportEvent, KnowledgeImportJob, new_id
from knowledge.paths import get_knowledge_root
from knowledge.service import (
    DEFAULT_KNOWLEDGE_BASE_ID,
    GENERIC_UPLOAD_SUFFIXES,
    MARKDOWN_SUFFIXES,
    PDF_SUFFIXES,
    KnowledgeService,
    KnowledgeServiceError,
    _slugify,
)

logger = logging.getLogger(__name__)

JOB_TERMINAL_STATUSES = {"succeeded", "failed", "cancelled"}
VECTOR_PUBLISH_KIND = "vector_publish"


def job_kind(job: KnowledgeImportJob) -> str:
    value = (job.job_metadata or {}).get("kind")
    return str(value or "import")


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
        "metadata": job.job_metadata,
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


async def create_vector_publish_job(
    session: AsyncSession,
    *,
    base_dir: Path,
    source_job: KnowledgeImportJob,
) -> KnowledgeImportJob:
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
    job.retry_count += 1
    session.add(KnowledgeImportEvent(job_id=job.id, level="info", message="任务已重新加入队列"))
    await session.commit()
    await session.refresh(job)
    return job


async def claim_next_job(session: AsyncSession) -> KnowledgeImportJob | None:
    stmt = (
        select(KnowledgeImportJob)
        .where(KnowledgeImportJob.status == "queued")
        .order_by(KnowledgeImportJob.created_at.asc())
        .limit(1)
    )
    result = await session.execute(stmt)
    job = result.scalar_one_or_none()
    if job is None:
        return None
    job.status = "running"
    job.current_step = "starting"
    job.progress = 5
    job.started_at = datetime.now(timezone.utc)
    job.finished_at = None
    job.error_message = None
    message = "开始导入向量" if job_kind(job) == VECTOR_PUBLISH_KIND else "开始导入"
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
) -> None:
    job.current_step = step
    job.progress = max(0, min(100, progress))
    if metadata_patch:
        job.job_metadata = {**(job.job_metadata or {}), **metadata_patch}
    if message and record_event:
        session.add(KnowledgeImportEvent(job_id=job.id, level="info", message=message, event_metadata=event_metadata or {}))
    await session.commit()


async def mark_job_failed(session: AsyncSession, job: KnowledgeImportJob, error: Exception | str) -> None:
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
    await session.commit()


async def process_import_job(session: AsyncSession, *, base_dir: Path, job: KnowledgeImportJob) -> KnowledgeImportJob:
    if job_kind(job) == VECTOR_PUBLISH_KIND:
        return await process_vector_publish_job(session, base_dir=base_dir, job=job)

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
    job.status = "succeeded"
    job.current_step = "done"
    job.progress = 100
    job.document_id = document.id
    job.error_message = None
    job.finished_at = datetime.now(timezone.utc)
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

    result = await asyncio.to_thread(refresh_local_knowledge_index, base_dir, _progress_callback)
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

    job.status = "succeeded"
    job.current_step = "done"
    job.progress = 100
    job.error_message = None
    job.finished_at = datetime.now(timezone.utc)
    session.add(KnowledgeImportEvent(job_id=job.id, level="info", message="Milvus 向量导入完成", event_metadata=result))
    await session.commit()
    await session.refresh(job)
    return job
