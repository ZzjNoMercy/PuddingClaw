"""Temporary result store for database knowledge query detail rows."""

from __future__ import annotations

import csv
import io
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from config import get_database_qa_config
from knowledge.models import AnalyticsQueryResult, new_id, utcnow

from .schemas import SqlExecutionResult

BASE_DIR = Path(__file__).resolve().parents[2]
RESULT_DIR = BASE_DIR / "data" / "database-query-results"


class QueryResultStoreError(RuntimeError):
    """Raised when persisted query results cannot be read."""


def _is_expired(expires_at: datetime) -> bool:
    """Compare catalog timestamps consistently across SQLite and PostgreSQL."""

    normalized = expires_at if expires_at.tzinfo is not None else expires_at.replace(tzinfo=timezone.utc)
    return normalized <= utcnow()


def _json_default(value: Any) -> str:
    return str(value)


def _artifact_path(result_id: str) -> Path:
    return RESULT_DIR / f"{result_id}.jsonl"


async def persist_query_result(
    session: AsyncSession,
    *,
    question: str,
    sql: str,
    columns: list[str],
    rows: list[dict[str, Any]],
    profile: dict[str, Any],
    session_id: str = "",
    tool_call_id: str = "",
) -> dict[str, Any]:
    """Persist full detail rows and metadata, returning a result-store contract."""

    config = get_database_qa_config()
    ttl_hours = int(config.get("result_store_ttl_hours") or 168)
    result_id = new_id("qr")
    RESULT_DIR.mkdir(parents=True, exist_ok=True)
    artifact = _artifact_path(result_id)
    with artifact.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, default=_json_default))
            handle.write("\n")

    now = utcnow()
    expires_at = now + timedelta(hours=ttl_hours)
    record = AnalyticsQueryResult(
        id=result_id,
        session_id=session_id,
        tool_call_id=tool_call_id,
        question=question,
        sql=sql,
        columns=columns,
        row_count=len(rows),
        profile_json=profile,
        artifact_path=str(artifact.relative_to(BASE_DIR)),
        artifact_format="jsonl",
        status="ready",
        created_at=now,
        expires_at=expires_at,
    )
    session.add(record)
    await session.commit()
    return {
        "enabled": True,
        "artifact_path": f"backend/{record.artifact_path}",
        "storage_path": record.artifact_path,
        "artifact_format": record.artifact_format,
        "expires_at": expires_at.isoformat(),
        "ttl_hours": ttl_hours,
    } | {"result_id": result_id}


async def attach_persisted_query_result(
    session: AsyncSession,
    execution: SqlExecutionResult,
    *,
    question: str,
    sql: str,
    session_id: str = "",
    tool_call_id: str = "",
) -> bool:
    """Attach the shared result-store contract to an incomplete SQL execution.

    Both the all-in-one database query service and the explicit
    generate/validate/execute workflow use this helper so preview pagination
    behaves consistently across both paths.
    """

    if execution.is_complete:
        return False

    config = get_database_qa_config()
    if (
        config.get("result_store_enabled", True)
        and execution.materialized_all
        and execution.materialized_rows
    ):
        store_contract = await persist_query_result(
            session,
            question=question,
            sql=sql,
            columns=execution.columns,
            rows=execution.materialized_rows,
            profile=execution.profile,
            session_id=session_id,
            tool_call_id=tool_call_id,
        )
        execution.result_id = store_contract.get("result_id")
        execution.result_store = {key: value for key, value in store_contract.items() if key != "result_id"}
        execution.actions = [
            {
                "type": "fetch_page",
                "available": True,
                "page_size": config.get("default_page_size", 100),
            },
            {
                "type": "export",
                "available": bool(config.get("export_enabled", True)),
            },
        ]
        return True

    execution.actions = [
        {
            "type": "fetch_page",
            "available": False,
            "reason": "result_not_fully_materialized",
        }
    ]
    return False


async def get_query_result_page(
    session: AsyncSession,
    result_id: str,
    *,
    page: int = 1,
    page_size: int | None = None,
) -> dict[str, Any]:
    """Read one page from a persisted JSONL query result."""

    config = get_database_qa_config()
    max_page_size = int(config.get("max_page_size") or 500)
    effective_page_size = max(1, min(int(page_size or config.get("default_page_size") or 100), max_page_size))
    effective_page = max(1, int(page or 1))

    record = await session.get(AnalyticsQueryResult, result_id)
    if record is None:
        raise QueryResultStoreError("查询结果不存在或已清理。")

    expired = _is_expired(record.expires_at)
    artifact = BASE_DIR / record.artifact_path
    if expired or not artifact.exists():
        return {
            "result_id": record.id,
            "expired": True,
            "status": "expired" if expired else "missing_artifact",
            "row_count": record.row_count,
            "columns": record.columns,
            "export_enabled": bool(config.get("export_enabled", False)),
            "page": effective_page,
            "page_size": effective_page_size,
            "rows": [],
            "message": "持久化结果已过期或文件不存在，请重新执行问数。",
            "expires_at": record.expires_at.isoformat(),
        }

    start = (effective_page - 1) * effective_page_size
    end = start + effective_page_size
    rows: list[dict[str, Any]] = []
    with artifact.open("r", encoding="utf-8") as handle:
        for index, line in enumerate(handle):
            if index < start:
                continue
            if index >= end:
                break
            rows.append(json.loads(line))
    return {
        "result_id": record.id,
        "expired": False,
        "status": record.status,
        "row_count": record.row_count,
        "columns": record.columns,
        "profile": record.profile_json,
        "export_enabled": bool(config.get("export_enabled", False)),
        "page": effective_page,
        "page_size": effective_page_size,
        "has_next": end < record.row_count,
        "has_previous": effective_page > 1,
        "rows": rows,
        "expires_at": record.expires_at.isoformat(),
    }


def _record_to_summary(record: AnalyticsQueryResult, *, include_profile: bool = True) -> dict[str, Any]:
    config = get_database_qa_config()
    artifact = BASE_DIR / record.artifact_path
    expired = _is_expired(record.expires_at)
    summary = {
        "result_id": record.id,
        "session_id": record.session_id,
        "tool_call_id": record.tool_call_id,
        "question": record.question,
        "sql": record.sql,
        "columns": record.columns,
        "row_count": record.row_count,
        "artifact_path": f"backend/{record.artifact_path}",
        "storage_path": record.artifact_path,
        "artifact_format": record.artifact_format,
        "status": "expired" if expired else record.status,
        "expired": expired,
        "artifact_exists": artifact.exists(),
        "export_enabled": bool(config.get("export_enabled", False)),
        "created_at": record.created_at.isoformat(),
        "expires_at": record.expires_at.isoformat(),
    }
    if include_profile:
        summary["profile"] = record.profile_json
    return summary


async def list_query_results(
    session: AsyncSession,
    *,
    limit: int = 50,
    include_expired: bool = True,
    include_profile: bool = False,
) -> dict[str, Any]:
    """List persisted database query result metadata."""

    safe_limit = max(1, min(int(limit or 50), 200))
    query = select(AnalyticsQueryResult).order_by(desc(AnalyticsQueryResult.created_at)).limit(safe_limit)
    if not include_expired:
        query = (
            select(AnalyticsQueryResult)
            .where(AnalyticsQueryResult.expires_at > utcnow())
            .order_by(desc(AnalyticsQueryResult.created_at))
            .limit(safe_limit)
        )
    result = await session.execute(query)
    items = [_record_to_summary(record, include_profile=include_profile) for record in result.scalars().all()]
    return {"items": items, "count": len(items)}


async def get_query_result_summary(session: AsyncSession, result_id: str) -> dict[str, Any]:
    record = await session.get(AnalyticsQueryResult, result_id)
    if record is None:
        raise QueryResultStoreError("查询结果不存在或已清理。")
    return _record_to_summary(record)


async def export_query_result_csv(session: AsyncSession, result_id: str) -> tuple[str, str]:
    """Export a persisted query result as CSV text."""

    config = get_database_qa_config()
    if not config.get("export_enabled", False):
        raise QueryResultStoreError("CSV 导出已在智能问数设置中关闭。")

    record = await session.get(AnalyticsQueryResult, result_id)
    if record is None:
        raise QueryResultStoreError("查询结果不存在或已清理。")
    if _is_expired(record.expires_at):
        raise QueryResultStoreError("持久化结果已过期，请重新执行问数。")
    artifact = BASE_DIR / record.artifact_path
    if not artifact.exists():
        raise QueryResultStoreError("持久化结果文件不存在，请重新执行问数。")

    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=record.columns, extrasaction="ignore")
    writer.writeheader()
    with artifact.open("r", encoding="utf-8") as handle:
        for line in handle:
            writer.writerow(json.loads(line))
    filename = f"{record.id}.csv"
    return filename, output.getvalue()


async def cleanup_expired_query_results(session: AsyncSession) -> int:
    """Remove expired result metadata and artifacts."""

    result = await session.execute(
        select(AnalyticsQueryResult).where(AnalyticsQueryResult.expires_at <= utcnow())
    )
    records = list(result.scalars().all())
    for record in records:
        artifact = BASE_DIR / record.artifact_path
        if artifact.exists():
            artifact.unlink()
        await session.delete(record)
    if records:
        await session.commit()
    return len(records)
