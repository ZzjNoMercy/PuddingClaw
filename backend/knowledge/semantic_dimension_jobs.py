"""Queue primitives for long-running semantic-dimension builds.

The queue owns staging and validation only. Publishing remains an explicit,
skill-guided Agent action so a failed or unreviewed build cannot alter the
active semantic asset.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from knowledge.models import SemanticDimensionBuildEvent, SemanticDimensionBuildJob, TaskNotification, new_id


ACTIVE_STATUSES = {"queued", "running"}
RETRYABLE_STATUSES = {"failed", "cancelled"}
DISPLAY_TIMEZONE = ZoneInfo("Asia/Shanghai")


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _display_time(value: datetime | None) -> str | None:
    if value is None:
        return None
    return f"{value.astimezone(DISPLAY_TIMEZONE):%Y-%m-%d %H:%M:%S}（北京时间）"


def _fingerprint(*, dimension_id: str, adapter: str, requested_scope: dict[str, Any], input_snapshot: dict[str, Any]) -> str:
    payload = {
        "dimension_id": dimension_id,
        "adapter": adapter,
        "requested_scope": requested_scope,
        "input_snapshot": input_snapshot,
    }
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")).hexdigest()


def semantic_dimension_job_to_dict(job: SemanticDimensionBuildJob) -> dict[str, Any]:
    return {
        "id": job.id,
        "session_id": job.session_id,
        "query_id": job.query_id,
        "dimension_id": job.dimension_id,
        "adapter": job.adapter,
        "requested_scope": job.requested_scope or {},
        "input_snapshot": {key: value for key, value in (job.input_snapshot or {}).items() if key != "_fingerprint"},
        "status": job.status,
        "current_step": job.current_step,
        "progress": job.progress,
        "staging_path": job.staging_path,
        "published_reference_path": job.published_reference_path,
        "result_summary": job.result_summary or {},
        "error_message": job.error_message,
        "retry_count": job.retry_count,
        "created_at": job.created_at.isoformat() if job.created_at else None,
        "created_at_display": _display_time(job.created_at),
        "updated_at": job.updated_at.isoformat() if job.updated_at else None,
        "updated_at_display": _display_time(job.updated_at),
        "started_at": job.started_at.isoformat() if job.started_at else None,
        "started_at_display": _display_time(job.started_at),
        "finished_at": job.finished_at.isoformat() if job.finished_at else None,
        "finished_at_display": _display_time(job.finished_at),
    }


def semantic_dimension_event_to_dict(event: SemanticDimensionBuildEvent) -> dict[str, Any]:
    return {
        "id": event.id,
        "job_id": event.job_id,
        "level": event.level,
        "message": event.message,
        "metadata": event.event_metadata or {},
        "created_at": event.created_at.isoformat() if event.created_at else None,
    }


def task_notification_to_dict(notification: TaskNotification) -> dict[str, Any]:
    return {
        "id": notification.id,
        "category": notification.category,
        "subject_type": notification.subject_type,
        "subject_id": notification.subject_id,
        "title": notification.title,
        "body": notification.body,
        "payload": notification.payload or {},
        "created_at": notification.created_at.isoformat() if notification.created_at else None,
        "read_at": notification.read_at.isoformat() if notification.read_at else None,
    }


async def create_semantic_dimension_build_job(
    session: AsyncSession,
    *,
    dimension_id: str,
    adapter: str,
    requested_scope: dict[str, Any] | None = None,
    input_snapshot: dict[str, Any] | None = None,
    session_id: str = "",
    query_id: str = "",
) -> tuple[SemanticDimensionBuildJob, bool]:
    clean_dimension_id = str(dimension_id or "").removeprefix("dimension:").strip()
    clean_adapter = str(adapter or "").strip()
    if not clean_dimension_id:
        raise ValueError("dimension_id is required")
    if not clean_adapter:
        raise ValueError("adapter is required")

    scope = dict(requested_scope or {})
    snapshot = dict(input_snapshot or {})
    fingerprint = _fingerprint(
        dimension_id=clean_dimension_id,
        adapter=clean_adapter,
        requested_scope=scope,
        input_snapshot=snapshot,
    )
    existing_stmt = (
        select(SemanticDimensionBuildJob)
        .where(
            SemanticDimensionBuildJob.dimension_id == clean_dimension_id,
            SemanticDimensionBuildJob.adapter == clean_adapter,
            SemanticDimensionBuildJob.status.in_(ACTIVE_STATUSES),
        )
        .order_by(SemanticDimensionBuildJob.created_at.desc())
    )
    existing_result = await session.execute(existing_stmt)
    for existing in existing_result.scalars():
        if (existing.input_snapshot or {}).get("_fingerprint") == fingerprint:
            return existing, False

    job = SemanticDimensionBuildJob(
        id=new_id("sdb"),
        session_id=str(session_id or ""),
        query_id=str(query_id or ""),
        dimension_id=clean_dimension_id,
        adapter=clean_adapter,
        requested_scope=scope,
        input_snapshot={**snapshot, "_fingerprint": fingerprint},
        status="queued",
        current_step="queued",
        progress=0,
    )
    session.add(job)
    session.add(SemanticDimensionBuildEvent(job_id=job.id, level="info", message="语义维度构建任务已加入队列"))
    await session.commit()
    await session.refresh(job)
    return job, True


async def list_semantic_dimension_build_jobs(session: AsyncSession, *, limit: int = 50) -> list[SemanticDimensionBuildJob]:
    result = await session.execute(
        select(SemanticDimensionBuildJob)
        .order_by(SemanticDimensionBuildJob.created_at.desc())
        .limit(max(1, min(limit, 200)))
    )
    return list(result.scalars())


async def get_semantic_dimension_build_job(session: AsyncSession, job_id: str) -> SemanticDimensionBuildJob | None:
    return await session.get(SemanticDimensionBuildJob, str(job_id or ""))


async def list_semantic_dimension_build_events(
    session: AsyncSession, job_id: str, *, limit: int = 200
) -> list[SemanticDimensionBuildEvent]:
    result = await session.execute(
        select(SemanticDimensionBuildEvent)
        .where(SemanticDimensionBuildEvent.job_id == job_id)
        .order_by(SemanticDimensionBuildEvent.created_at.asc())
        .limit(max(1, min(limit, 500)))
    )
    return list(result.scalars())


async def claim_next_semantic_dimension_build_job(session: AsyncSession) -> SemanticDimensionBuildJob | None:
    result = await session.execute(
        select(SemanticDimensionBuildJob)
        .where(SemanticDimensionBuildJob.status == "queued")
        .order_by(SemanticDimensionBuildJob.created_at.asc())
        .limit(1)
    )
    job = result.scalar_one_or_none()
    if job is None:
        return None
    job.status = "running"
    job.current_step = "load_source_profiles"
    job.progress = 5
    job.started_at = _utcnow()
    job.finished_at = None
    job.error_message = None
    session.add(SemanticDimensionBuildEvent(job_id=job.id, level="info", message="开始构建语义维度"))
    await session.commit()
    await session.refresh(job)
    return job


async def update_semantic_dimension_build_progress(
    session: AsyncSession,
    job: SemanticDimensionBuildJob,
    *,
    step: str,
    progress: int,
    message: str | None = None,
    event_metadata: dict[str, Any] | None = None,
) -> None:
    job.current_step = step
    job.progress = max(0, min(100, progress))
    if message:
        session.add(
            SemanticDimensionBuildEvent(
                job_id=job.id,
                level="info",
                message=message,
                event_metadata=event_metadata or {},
            )
        )
    await session.commit()


async def mark_semantic_dimension_build_waiting_publish(
    session: AsyncSession,
    job: SemanticDimensionBuildJob,
    *,
    staging_path: str,
    published_reference_path: str,
    result_summary: dict[str, Any],
) -> None:
    job.status = "waiting_for_publish_confirmation"
    job.current_step = "waiting_for_publish_confirmation"
    job.progress = 100
    job.staging_path = staging_path
    job.published_reference_path = published_reference_path
    job.result_summary = result_summary
    job.finished_at = _utcnow()
    session.add(
        SemanticDimensionBuildEvent(
            job_id=job.id,
            level="info",
            message="构建和校验已完成，等待用户确认发布。",
            event_metadata={"staging_path": staging_path, "summary": result_summary},
        )
    )
    session.add(
        TaskNotification(
            category="semantic_dimension_build",
            subject_type="semantic_dimension_build_job",
            subject_id=job.id,
            title=f"{job.dimension_id} 维度构建已完成",
            body="结果已通过构建校验，等待你在原对话中确认发布。",
            payload={
                "job_id": job.id,
                "dimension_id": job.dimension_id,
                "status": job.status,
                "session_id": job.session_id,
                "query_id": job.query_id,
            },
        )
    )
    await session.commit()


async def mark_semantic_dimension_build_waiting_baseline_change(
    session: AsyncSession,
    job: SemanticDimensionBuildJob,
    *,
    staging_path: str,
    published_reference_path: str,
    result_summary: dict[str, Any],
) -> None:
    job.status = "waiting_for_baseline_change_confirmation"
    job.current_step = "waiting_for_baseline_change_confirmation"
    job.progress = 100
    job.staging_path = staging_path
    job.published_reference_path = published_reference_path
    job.result_summary = result_summary
    job.finished_at = _utcnow()
    session.add(SemanticDimensionBuildEvent(
        job_id=job.id,
        level="warning",
        message="构建发现规范基准变化，staging 已保留，等待在匹配管理中处理。",
        event_metadata={"staging_path": staging_path, "baseline_delta": result_summary.get("baseline_delta") or {}},
    ))
    session.add(TaskNotification(
        category="semantic_dimension_build",
        subject_type="semantic_dimension_build_job",
        subject_id=job.id,
        title=f"{job.dimension_id} 规范基准发生变化",
        body="构建产物已落在 staging；请到该维度的匹配管理处理停用、移除或取消。",
        payload={"job_id": job.id, "dimension_id": job.dimension_id, "status": job.status, "session_id": job.session_id, "query_id": job.query_id},
    ))
    await session.commit()


async def resolve_semantic_dimension_baseline_change(
    session: AsyncSession,
    job: SemanticDimensionBuildJob,
    *,
    action: str,
) -> None:
    """Move a staged baseline change to publish review, or cancel it."""

    if job.status != "waiting_for_baseline_change_confirmation":
        raise ValueError("Job is not waiting for a baseline-change decision")
    if action == "cancel":
        job.status = "cancelled"
        job.current_step = "cancelled"
        job.finished_at = _utcnow()
        session.add(SemanticDimensionBuildEvent(job_id=job.id, level="info", message="用户取消了本次规范基准变更。"))
        await session.commit()
        await session.refresh(job)
        return
    if action not in {"inactive", "remove"}:
        raise ValueError("Baseline-change action must be inactive, remove, or cancel")
    job.status = "waiting_for_publish_confirmation"
    job.current_step = "waiting_for_publish_confirmation"
    job.finished_at = _utcnow()
    session.add(SemanticDimensionBuildEvent(
        job_id=job.id,
        level="info",
        message="规范基准变更已保存为草稿，等待发布版本。",
        event_metadata={"baseline_change_action": action},
    ))
    await session.commit()
    await session.refresh(job)


async def mark_semantic_dimension_build_published(
    session: AsyncSession,
    job: SemanticDimensionBuildJob,
    *,
    active_reference_path: str,
) -> None:
    job.status = "published"
    job.current_step = "published"
    job.progress = 100
    job.finished_at = _utcnow()
    session.add(
        SemanticDimensionBuildEvent(
            job_id=job.id,
            level="info",
            message="已原子发布 staging Crosswalk，活跃语义维度已更新。",
            event_metadata={"active_reference_path": active_reference_path},
        )
    )
    session.add(
        TaskNotification(
            category="semantic_dimension_build",
            subject_type="semantic_dimension_build_job",
            subject_id=job.id,
            title=f"{job.dimension_id} 维度已发布",
            body="活跃 Crosswalk 和语义维度定义已同步更新。",
            payload={"job_id": job.id, "dimension_id": job.dimension_id, "status": job.status},
        )
    )
    await session.commit()
    await session.refresh(job)


async def mark_semantic_dimension_build_failed(
    session: AsyncSession, job: SemanticDimensionBuildJob, error: Exception | str
) -> None:
    message = str(error)
    job.status = "failed"
    job.current_step = "failed"
    job.error_message = message
    job.finished_at = _utcnow()
    session.add(SemanticDimensionBuildEvent(job_id=job.id, level="error", message=message))
    session.add(
        TaskNotification(
            category="semantic_dimension_build",
            subject_type="semantic_dimension_build_job",
            subject_id=job.id,
            title=f"{job.dimension_id} 维度构建失败",
            body=message[:500],
            payload={
                "job_id": job.id,
                "dimension_id": job.dimension_id,
                "status": job.status,
                "session_id": job.session_id,
                "query_id": job.query_id,
            },
        )
    )
    await session.commit()


async def retry_semantic_dimension_build_job(session: AsyncSession, job_id: str) -> SemanticDimensionBuildJob:
    job = await get_semantic_dimension_build_job(session, job_id)
    if job is None:
        raise ValueError("Semantic dimension build job not found")
    if job.status not in RETRYABLE_STATUSES:
        raise ValueError("Only failed or cancelled semantic dimension build jobs can be retried")
    job.status = "queued"
    job.current_step = "queued"
    job.progress = 0
    job.staging_path = ""
    job.result_summary = {}
    job.error_message = None
    job.started_at = None
    job.finished_at = None
    job.retry_count += 1
    session.add(SemanticDimensionBuildEvent(job_id=job.id, level="info", message="任务已重新加入队列"))
    await session.commit()
    await session.refresh(job)
    return job


async def cancel_semantic_dimension_build_job(session: AsyncSession, job_id: str) -> SemanticDimensionBuildJob:
    job = await get_semantic_dimension_build_job(session, job_id)
    if job is None:
        raise ValueError("Semantic dimension build job not found")
    if job.status != "queued":
        raise ValueError("Only queued semantic dimension build jobs can be cancelled")
    job.status = "cancelled"
    job.current_step = "cancelled"
    job.finished_at = _utcnow()
    session.add(SemanticDimensionBuildEvent(job_id=job.id, level="info", message="任务已取消"))
    await session.commit()
    await session.refresh(job)
    return job


async def list_task_notifications(session: AsyncSession, *, unread_only: bool = False, limit: int = 50) -> list[TaskNotification]:
    stmt = select(TaskNotification).order_by(TaskNotification.created_at.desc()).limit(max(1, min(limit, 200)))
    if unread_only:
        stmt = stmt.where(TaskNotification.read_at.is_(None))
    result = await session.execute(stmt)
    return list(result.scalars())


async def mark_task_notification_read(session: AsyncSession, notification_id: str) -> TaskNotification | None:
    notification = await session.get(TaskNotification, notification_id)
    if notification is None:
        return None
    if notification.read_at is None:
        notification.read_at = _utcnow()
        await session.commit()
        await session.refresh(notification)
    return notification
