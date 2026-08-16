"""Temporary result store for database knowledge query detail rows."""

from __future__ import annotations

import csv
import hashlib
import io
import json
import logging
import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from config import get_database_qa_config
from knowledge.models import AnalyticsQueryResult, new_id, utcnow

from .schemas import SqlExecutionResult

from runtime_identity.paths import PuddingClawPaths

RESULT_DIR = PuddingClawPaths.from_environment().query_results()
logger = logging.getLogger(__name__)


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


def _catalog_path(result_id: str) -> Path:
    return RESULT_DIR / ".catalog" / f"{result_id}.json"


def _safe_result_store_path(path: Path, *, root: Path) -> Path:
    """Return one result-store path only when it cannot escape its managed root."""

    resolved_root = root.resolve()
    resolved = path.resolve(strict=False)
    try:
        resolved.relative_to(resolved_root)
    except ValueError as exc:
        raise QueryResultStoreError(
            f"拒绝清理结果目录之外的路径：{path}"
        ) from exc
    if path.is_symlink():
        raise QueryResultStoreError(f"拒绝清理符号链接结果文件：{path}")
    return resolved


def _write_catalog(result_id: str, payload: dict[str, Any]) -> None:
    target = _catalog_path(result_id)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(".json.tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str),
        encoding="utf-8",
    )
    temporary.replace(target)


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
    source_query_id: str = "",
    source_run_id: str = "",
    producer_receipt_ids: list[str] | None = None,
) -> dict[str, Any]:
    """Persist full detail rows and metadata, returning a result-store contract."""

    config = get_database_qa_config()
    ttl_hours = int(config.get("result_store_ttl_hours") or 168)
    result_id = new_id("qr")
    RESULT_DIR.mkdir(parents=True, exist_ok=True)
    artifact = _artifact_path(result_id)
    temporary_artifact = artifact.with_suffix(".jsonl.tmp")
    digest = hashlib.sha256()
    with temporary_artifact.open("wb") as handle:
        for row in rows:
            line = (json.dumps(row, ensure_ascii=False, default=_json_default) + "\n").encode("utf-8")
            digest.update(line)
            handle.write(line)
    artifact_sha256 = f"sha256:{digest.hexdigest()}"

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
        profile_json={**profile, "_artifact_sha256": artifact_sha256},
        # Store a path relative to the user-owned result root.  The package
        # directory is immutable and may be on a different filesystem.
        artifact_path=str(artifact.relative_to(RESULT_DIR)),
        artifact_format="jsonl",
        status="creating",
        created_at=now,
        expires_at=expires_at,
    )
    session.add(record)
    catalog_payload = {
        "schema_version": "analytics-query-result-catalog-v1",
        "result_id": result_id,
        "session_id": session_id,
        "tool_call_id": tool_call_id,
        "source_query_id": source_query_id,
        "source_run_id": source_run_id,
        "owner_binding_version": "strict-v1" if source_query_id else "legacy-session-tool-v0",
        "artifact_path": record.artifact_path,
        "artifact_format": record.artifact_format,
        "artifact_sha256": artifact_sha256,
        "row_count": len(rows),
        "status": "ready",
        "created_at": now.isoformat(),
        "expires_at": expires_at.isoformat(),
        "producer_receipt_ids": sorted(
            {str(item) for item in producer_receipt_ids or [] if str(item)}
        ),
    }
    creating_committed = False
    try:
        # Publish database ownership before either final file name becomes
        # visible. The scavenger therefore cannot mistake an in-flight result
        # for an orphan, regardless of commit latency or grace configuration.
        await session.commit()
        creating_committed = True
        temporary_artifact.replace(artifact)
        _write_catalog(result_id, catalog_payload)
        record.status = "ready"
        await session.commit()
    except Exception:
        await session.rollback()
        temporary_artifact.unlink(missing_ok=True)
        artifact.unlink(missing_ok=True)
        _catalog_path(result_id).unlink(missing_ok=True)
        if creating_committed:
            try:
                persisted = await session.get(AnalyticsQueryResult, result_id)
                if persisted is not None:
                    await session.delete(persisted)
                    await session.commit()
            except Exception:
                await session.rollback()
                logger.exception(
                    "Failed to remove creating SQL result tombstone %s",
                    result_id,
                )
        raise
    return {
        "enabled": True,
        "artifact_path": f"data/query-results/{record.artifact_path}",
        "storage_path": record.artifact_path,
        "artifact_format": record.artifact_format,
        "expires_at": expires_at.isoformat(),
        "ttl_hours": ttl_hours,
        "artifact_sha256": artifact_sha256,
    } | {"result_id": result_id}


async def attach_persisted_query_result(
    session: AsyncSession,
    execution: SqlExecutionResult,
    *,
    question: str,
    sql: str,
    session_id: str = "",
    tool_call_id: str = "",
    source_query_id: str = "",
    source_run_id: str = "",
    producer_receipt_ids: list[str] | None = None,
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
            source_query_id=source_query_id,
            source_run_id=source_run_id,
            producer_receipt_ids=producer_receipt_ids,
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

    materialization_row_cap = int(
        config.get("result_materialization_row_cap") or 5000
    )
    if not config.get("result_store_enabled", True):
        reason = "result_store_disabled"
        next_action = "enable_result_store_then_rerun_database_query"
    elif not execution.materialized_all:
        reason = "result_exceeds_materialization_row_cap"
        next_action = (
            "narrow_or_aggregate_the_query_or_raise_"
            "result_materialization_row_cap_then_rerun_database_query"
        )
    else:
        reason = "result_not_persisted"
        next_action = "rerun_database_query"
    execution.actions = [
        {
            "type": "fetch_page",
            "available": False,
            "reason": reason,
            "row_count": int(
                execution.total_row_count or execution.row_count or 0
            ),
            "materialization_row_cap": materialization_row_cap,
            "next_action": next_action,
        }
    ]
    return False


async def get_query_result_page(
    session: AsyncSession,
    result_id: str,
    *,
    page: int = 1,
    page_size: int | None = None,
    session_id: str | None = None,
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
    if expired:
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
    artifact, _catalog = _verified_result_artifact(
        record,
        session_id=session_id,
    )

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
    artifact = RESULT_DIR / record.artifact_path
    catalog = _catalog_path(record.id)
    expired = _is_expired(record.expires_at)
    artifact_exists = artifact.is_file()
    catalog_exists = catalog.is_file()
    display_status = (
        "expired"
        if expired
        else "missing_artifact"
        if not artifact_exists
        else "missing_catalog"
        if not catalog_exists
        else record.status
    )
    summary = {
        "result_id": record.id,
        "session_id": record.session_id,
        "tool_call_id": record.tool_call_id,
        "question": record.question,
        "sql": record.sql,
        "columns": record.columns,
        "row_count": record.row_count,
        "artifact_path": f"data/query-results/{record.artifact_path}",
        "storage_path": record.artifact_path,
        "artifact_format": record.artifact_format,
        "status": display_status,
        "expired": expired,
        "artifact_exists": artifact_exists,
        "catalog_exists": catalog_exists,
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


async def get_query_result_source_contract(
    session: AsyncSession,
    result_id: str,
    *,
    session_id: str,
) -> dict[str, Any]:
    """Return a verified server locator suitable for SourceReference registration."""

    record = await session.get(AnalyticsQueryResult, result_id)
    if record is None:
        raise QueryResultStoreError("查询结果不存在或已清理。")
    if record.session_id != session_id:
        raise QueryResultStoreError("查询结果不属于当前 Session。")
    if _is_expired(record.expires_at):
        raise QueryResultStoreError("持久化结果已过期，请重新执行问数。")
    artifact, catalog = _verified_result_artifact(
        record,
        session_id=session_id,
    )
    expected_sha256 = str(catalog.get("artifact_sha256") or "")
    return {
        "result_id": result_id,
        "artifact_path": str(artifact.resolve()),
        "artifact_sha256": expected_sha256,
        "artifact_format": "jsonl",
        "columns": list(record.columns or []),
        "row_count": int(record.row_count or 0),
        "expires_at": record.expires_at.isoformat(),
        "producer_receipt_ids": list(catalog.get("producer_receipt_ids") or []),
        "source_query_id": str(catalog.get("source_query_id") or ""),
        "source_run_id": str(catalog.get("source_run_id") or ""),
    }


def _verified_result_artifact(
    record: AnalyticsQueryResult,
    *,
    session_id: str | None = None,
) -> tuple[Path, dict[str, Any]]:
    """Resolve a ready result only through its immutable owner/hash catalog."""

    if str(record.status or "") != "ready":
        raise QueryResultStoreError(
            f"查询结果尚未就绪（status={record.status or 'unknown'}）。"
        )
    if session_id is not None and record.session_id != session_id:
        raise QueryResultStoreError("查询结果不属于当前 Session。")
    catalog_path = _catalog_path(record.id)
    try:
        catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise QueryResultStoreError("查询结果缺少不可变目录凭证。") from exc
    if (
        str(catalog.get("result_id") or "") != record.id
        or str(catalog.get("session_id") or "") != str(record.session_id or "")
        or str(catalog.get("artifact_format") or "") != "jsonl"
        or str(catalog.get("artifact_path") or "") != str(record.artifact_path or "")
    ):
        raise QueryResultStoreError("查询结果目录所有权或格式不匹配。")
    artifact = _safe_result_store_path(
        RESULT_DIR / str(catalog.get("artifact_path") or ""),
        root=RESULT_DIR,
    )
    if not artifact.is_file():
        raise QueryResultStoreError("查询结果文件不存在，请重新执行问数。")
    digest = hashlib.sha256()
    with artifact.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    actual_sha256 = f"sha256:{digest.hexdigest()}"
    expected_sha256 = str(catalog.get("artifact_sha256") or "")
    profile_sha256 = str((record.profile_json or {}).get("_artifact_sha256") or "")
    if (
        actual_sha256 != expected_sha256
        or (profile_sha256 and profile_sha256 != expected_sha256)
    ):
        raise QueryResultStoreError("查询结果内容与不可变目录 hash 不一致。")
    return artifact, catalog


async def backfill_query_result_catalogs(session: AsyncSession) -> int:
    """Idempotently migrate live pre-catalog SQL results to the safe locator contract."""

    result = await session.execute(
        select(AnalyticsQueryResult).where(
            AnalyticsQueryResult.expires_at > utcnow(),
            AnalyticsQueryResult.status == "ready",
        )
    )
    records = list(result.scalars().all())
    migrated = 0
    changed_records = False
    from graph.session_manager import session_manager

    for record in records:
        artifact = RESULT_DIR / record.artifact_path
        if not artifact.is_file() or record.artifact_format != "jsonl":
            continue
        digest = hashlib.sha256()
        with artifact.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        artifact_sha256 = f"sha256:{digest.hexdigest()}"
        profile = dict(record.profile_json or {})
        database_tool_call_id = str(record.tool_call_id or "")
        existing_catalog_path = _catalog_path(record.id)
        if existing_catalog_path.is_file():
            try:
                existing_catalog = json.loads(existing_catalog_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                logger.error("Invalid immutable SQL result catalog: %s", record.id)
                continue
            catalog_owner_matches = (
                str(existing_catalog.get("result_id") or "") == record.id
                and str(existing_catalog.get("session_id") or "") == record.session_id
                and str(existing_catalog.get("tool_call_id") or "") == database_tool_call_id
            )
            catalog_hash = str(existing_catalog.get("artifact_sha256") or "")
            if not catalog_owner_matches or not catalog_hash:
                logger.error("SQL result catalog owner/hash mismatch: %s", record.id)
            elif catalog_hash != artifact_sha256:
                logger.error("SQL result artifact changed after cataloging: %s", record.id)
            # Existing catalogs are immutable authority. Never bless current
            # bytes again during startup, even when they were modified.
            continue
        if not record.session_id or not session_manager.is_initialized:
            continue
        owner = session_manager.result_owner_tool_call(record.session_id, record.id)
        if owner is None or not owner.get("source_query_id"):
            # A DB tool_call_id is not sufficient because providers may reuse
            # it across Runs. The transcript occurrence is the authority.
            continue
        tool_call_id = str(owner.get("tool_call_id") or "")
        if record.tool_call_id and str(record.tool_call_id) != tool_call_id:
            logger.error("SQL result DB owner disagrees with Session occurrence: %s", record.id)
            continue
        if not record.tool_call_id:
            record.tool_call_id = tool_call_id
            changed_records = True
        trusted_profile_hash = str(profile.get("_artifact_sha256") or "")
        if trusted_profile_hash and trusted_profile_hash != artifact_sha256:
            logger.error("Legacy SQL artifact differs from persisted DB hash: %s", record.id)
            continue
        if not trusted_profile_hash:
            profile["_artifact_sha256"] = artifact_sha256
            record.profile_json = profile
            changed_records = True
        _write_catalog(
            record.id,
            {
                "schema_version": "analytics-query-result-catalog-v1",
                "result_id": record.id,
                "session_id": record.session_id,
                "tool_call_id": tool_call_id,
                "source_query_id": owner.get("source_query_id", ""),
                "source_run_id": owner.get("source_run_id", ""),
                "owner_binding_version": "strict-v1",
                "artifact_path": record.artifact_path,
                "artifact_format": record.artifact_format,
                "artifact_sha256": artifact_sha256,
                "row_count": record.row_count,
                "status": record.status,
                "created_at": record.created_at.isoformat(),
                "expires_at": record.expires_at.isoformat(),
            },
        )
        migrated += 1
    if changed_records:
        await session.commit()
    return migrated


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
    artifact, _catalog = _verified_result_artifact(record)

    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=record.columns, extrasaction="ignore")
    writer.writeheader()
    with artifact.open("r", encoding="utf-8") as handle:
        for line in handle:
            writer.writerow(json.loads(line))
    filename = f"{record.id}.csv"
    return filename, output.getvalue()


async def cleanup_expired_query_results(session: AsyncSession) -> int:
    """Remove expired result metadata and artifacts through a retryable tombstone.

    A database transaction cannot atomically include host-file deletion. Marking
    records as ``deleting`` first makes every interruption state recoverable:
    missing files are idempotent on the next pass, while a failed unlink leaves
    the catalog row available for retry instead of creating an untracked orphan.
    """

    result = await session.execute(
        select(AnalyticsQueryResult).where(AnalyticsQueryResult.expires_at <= utcnow())
    )
    records = list(result.scalars().all())
    retained: list[AnalyticsQueryResult] = []
    if records:
        from graph.session_manager import session_manager

        for record in records:
            if (
                record.session_id
                and session_manager.is_initialized
                and session_manager.session_references_result_id(record.session_id, record.id)
            ):
                retained.append(record)
        if retained:
            retained_ids = {item.id for item in retained}
            records = [item for item in records if item.id not in retained_ids]
    if not records:
        return 0

    for record in records:
        record.status = "deleting"
    await session.commit()

    deleted_records: list[AnalyticsQueryResult] = []
    for record in records:
        try:
            artifact = _safe_result_store_path(
                RESULT_DIR / record.artifact_path,
                root=RESULT_DIR,
            )
            catalog = _safe_result_store_path(
                _catalog_path(record.id),
                root=RESULT_DIR / ".catalog",
            )
            artifact.unlink(missing_ok=True)
            catalog.unlink(missing_ok=True)
        except (OSError, QueryResultStoreError):
            logger.exception(
                "Deferred cleanup for persisted SQL result %s; tombstone retained",
                record.id,
            )
            continue
        await session.delete(record)
        deleted_records.append(record)
    if deleted_records:
        await session.commit()
    return len(deleted_records)


async def scavenge_orphaned_query_result_files(
    session: AsyncSession,
    *,
    grace_seconds: float | None = None,
) -> int:
    """Remove old, unowned result files without racing an in-flight commit.

    Only files with the platform-owned ``qr-*`` naming contract are considered.
    A grace window protects the short interval in ``persist_query_result`` where
    files exist before their database row commits.
    """

    configured_grace = (
        float(os.getenv("PUDDINGCLAW_QUERY_RESULT_ORPHAN_GRACE_SECONDS", "3600"))
        if grace_seconds is None
        else float(grace_seconds)
    )
    effective_grace = max(0.0, configured_grace)
    result = await session.execute(select(AnalyticsQueryResult.id))
    owned_ids = {str(item) for item in result.scalars().all()}
    now = time.time()
    candidates: list[tuple[str, Path, Path]] = []
    catalog_root = RESULT_DIR / ".catalog"
    if RESULT_DIR.exists():
        for artifact in RESULT_DIR.glob("qr[_-]*.jsonl"):
            candidates.append(
                (artifact.stem, artifact, catalog_root / f"{artifact.stem}.json")
            )
    if catalog_root.exists():
        known_candidates = {item[0] for item in candidates}
        for catalog in catalog_root.glob("qr[_-]*.json"):
            result_id = catalog.stem
            if result_id not in known_candidates:
                candidates.append(
                    (result_id, RESULT_DIR / f"{result_id}.jsonl", catalog)
                )

    removed = 0
    for result_id, artifact, catalog in candidates:
        if result_id in owned_ids:
            continue
        existing = [path for path in (artifact, catalog) if path.exists()]
        if not existing:
            continue
        try:
            youngest_mtime = max(path.stat().st_mtime for path in existing)
            if now - youngest_mtime < effective_grace:
                continue
            for path, root in (
                (artifact, RESULT_DIR),
                (catalog, catalog_root),
            ):
                safe = _safe_result_store_path(path, root=root)
                safe.unlink(missing_ok=True)
        except (OSError, QueryResultStoreError):
            logger.exception(
                "Failed to scavenge orphaned persisted SQL result %s",
                result_id,
            )
            continue
        removed += 1
    return removed
