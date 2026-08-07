"""Internal Vanna-backed database knowledge query service."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import time
from collections.abc import Awaitable, Callable
from time import perf_counter
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlglot.errors import ParseError

from analytics.models.registry import get_analytics_model_registry
from analytics.nl2sql.eav_evidence import (
    bindings_from_semantic_trace,
    bindings_prompt,
    check_eav_evidence,
    eav_concept_key,
    eav_mapping_fingerprint,
    eav_type_name_predicate_fingerprint,
    extract_eav_type_names,
    sql_business_fingerprint,
)
from analytics.nl2sql.guardrails import (
    GuardrailConflict,
    collect_applied_semantic_rules,
    conflicts_to_messages,
    detect_guardrail_conflicts,
)
from analytics.nl2sql.profile_catalog import eav_profile_catalog
from analytics.nl2sql.query_plan import select_derivable_query_plans
from analytics.nl2sql.result_store import attach_persisted_query_result
from analytics.nl2sql.runtime import build_vanna_client_from_app_config
from analytics.nl2sql.schemas import DatabaseQueryRequest, DatabaseQueryResult, DatabaseSqlGenerationResult
from analytics.nl2sql.sql_runner import (
    SqlRunnerError,
    extract_sql,
    run_readonly_sql,
    validate_readonly_sql,
)
from analytics.nl2sql.table_router import TableRouterError, route_database_tables, summarize_table_route
from analytics.semantic_runtime import (
    SemanticQueryContext,
    build_execution_binding_metadata,
    compile_semantic_query_context,
    format_analytics_model_for_sql_prompt,
    render_sql_semantic_context,
)
from config import get_vanna_config
from knowledge.database_sources import database_source_url, get_database_source


class DatabaseKnowledgeQueryError(RuntimeError):
    """Raised when the database knowledge query pipeline fails."""

    def __init__(
        self,
        message: str,
        *,
        sql: str | None = None,
        error_code: str = "database_query_failed",
        stage: str = "",
        recoverable: bool = False,
        next_action: str = "stop",
        evidence_ref: str = "",
        attempt: int = 0,
        max_attempts: int = 0,
        field_or_concept: str = "",
        error_signature: str = "",
        source_id: str = "",
        table_scope: list[str] | None = None,
    ) -> None:
        super().__init__(message)
        self.sql = sql
        self.error_code = error_code
        self.stage = stage
        self.recoverable = recoverable
        self.next_action = next_action
        self.evidence_ref = evidence_ref
        self.attempt = attempt
        self.max_attempts = max_attempts
        self.field_or_concept = field_or_concept
        self.error_signature = error_signature
        self.source_id = source_id
        self.table_scope = list(table_scope or [])

    def protocol(self) -> dict[str, Any]:
        return {
            "status": "error",
            "error_code": self.error_code,
            "recoverable": self.recoverable,
            "stage": self.stage,
            "next_action": self.next_action,
            "attempt": self.attempt,
            "max_attempts": self.max_attempts,
            "evidence_ref": self.evidence_ref or None,
            "field_or_concept": self.field_or_concept or None,
            "error_signature": self.error_signature or None,
            "source_id": self.source_id or None,
            "table_scope": self.table_scope,
            "message": str(self),
        }


logger = logging.getLogger(__name__)

VANNA_REFERENCE_TOP_K = 5
VANNA_ENTITY_TOP_K_PER_TYPE = 10
_SAFE_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
ProgressCallback = Callable[[dict[str, Any]], None]


def _emit_progress(callback: ProgressCallback | None, stage: str, status: str, **payload: Any) -> None:
    if callback is None:
        return
    try:
        callback(
            {
                "type": "database_sql_generation_progress",
                "stage": stage,
                "status": status,
                "timestamp": time.time(),
                **payload,
            }
        )
    except Exception:
        logger.debug("database progress callback failed", exc_info=True)


async def _await_with_progress(
    awaitable: Awaitable[Any],
    *,
    callback: ProgressCallback | None,
    stage: str,
    timeout_seconds: float = 120.0,
    heartbeat_seconds: float = 10.0,
    detail: str = "",
    cancel_callback: Callable[[], None] | None = None,
) -> Any:
    task = asyncio.create_task(awaitable)
    started = time.monotonic()
    _emit_progress(callback, stage, "running", elapsed_ms=0, detail=detail)
    try:
        while True:
            remaining = timeout_seconds - (time.monotonic() - started)
            if remaining <= 0:
                task.cancel()
                timeout_error_code = {
                    "candidate_generation": "sql_candidate_provider_timeout",
                    "vanna_retrieval": "sql_retrieval_timeout",
                }.get(stage, "sql_refinement_provider_timeout")
                raise DatabaseKnowledgeQueryError(
                    f"SQL 生成阶段 {stage} 超过 {int(timeout_seconds)} 秒。",
                    error_code=timeout_error_code,
                    stage=stage,
                    recoverable=True,
                    next_action="retry_once",
                )
            done, _ = await asyncio.wait(
                {task},
                timeout=min(heartbeat_seconds, remaining),
                return_when=asyncio.FIRST_COMPLETED,
            )
            elapsed_ms = round((time.monotonic() - started) * 1000, 2)
            if task in done:
                result = await task
                _emit_progress(callback, stage, "completed", elapsed_ms=elapsed_ms, detail=detail)
                return result
            _emit_progress(callback, stage, "heartbeat", elapsed_ms=elapsed_ms, detail=detail)
    finally:
        if not task.done():
            task.cancel()
            if cancel_callback is not None:
                try:
                    cancel_callback()
                except Exception:
                    logger.debug("database provider cancellation callback failed", exc_info=True)
            await asyncio.gather(task, return_exceptions=True)


def _cancel_vanna_provider(vanna: Any) -> None:
    client = getattr(vanna, "client", None)
    closer = getattr(client, "close", None)
    if callable(closer):
        closer()


def _vehicle_params_table(route: Any) -> str | None:
    """Return a quoted, schema-preserving authorized EAV table name."""

    for raw_name in route.table_names:
        parts = [part.strip().strip('"') for part in str(raw_name).split(".") if part.strip()]
        if not parts or parts[-1].lower() != "vehicle_params":
            continue
        scoped = parts[-2:]
        if all(_SAFE_IDENTIFIER.fullmatch(part) for part in scoped):
            return ".".join(f'"{part}"' for part in scoped)
    return None


def _prompt_entity_type_names(items: list[dict[str, Any]]) -> set[str]:
    result: set[str] = set()
    for item in items:
        column = str(item.get("table_column") or "").strip().lower()
        if not (column.endswith(".vehicle_params.type_name") or column == "vehicle_params.type_name"):
            continue
        name = str(item.get("canonical_name") or item.get("name") or "").strip()
        if name:
            result.add(name)
    return result


def _eav_name_relevant_to_question(type_name: str, question: str) -> bool:
    compact_name = re.sub(r"\s+", "", str(type_name or ""))
    compact_question = re.sub(r"\s+", "", str(question or ""))
    if not compact_name or not compact_question:
        return False
    if compact_name in compact_question:
        return True
    concept = re.sub(r"(?:线数|数量|个数|数目|种类|类型|级别)$", "", compact_name)
    return len(concept) >= 2 and concept in compact_question


def _question_requires_strict_eav_profile(question: str) -> bool:
    """Require a value profile only when value parsing, not mere existence, is risky."""

    normalized = str(question or "").lower()
    return bool(
        re.search(r"(?:数量|个数|颗数|线数|算力|阈值|区间|范围)", normalized)
        or re.search(r"(?:以上|以下|至少|至多|大于|小于|超过|不低于|不高于)", normalized)
        or re.search(
            r"\d+(?:\.\d+)?\s*(?:个|颗|台|枚|组|套|线|tops?|kw|kwh|km|mm|v|寸|l)",
            normalized,
            re.IGNORECASE,
        )
    )


def _profile_progress_detail(profiles: list[dict[str, Any]]) -> str:
    """Expose compact raw database values without interpreting their meaning."""

    details: list[str] = []
    for profile in profiles[:3]:
        type_name = str(profile.get("type_name") or "").strip()
        if not type_name:
            continue
        values = [
            str(item.get("value") or "")
            for item in profile.get("top_values") or []
            if isinstance(item, dict) and str(item.get("value") or "")
        ]
        distinct_count = int(profile.get("distinct_value_count") or len(values))
        shown = values[:20]
        suffix = f"（共 {distinct_count} 个值）"
        details.append(f"{type_name}：{'、'.join(shown)}{suffix}" if shown else f"{type_name}{suffix}")
    return "；".join(details)


def _sql_requires_strict_eav_profile(sql: str) -> bool:
    normalized = str(sql or "").lower()
    return "type_value" in normalized and bool(
        re.search(
            r"(?:regexp_replace|regexp_match|substring|::\s*(?:numeric|decimal|integer|bigint|double)|"
            r"cast\s*\([^)]*type_value[^)]*\bas\s+(?:numeric|decimal|integer|bigint|double)|"
            r"type_value\s*(?:(?:not\s+)?(?:like|ilike)|~|similar\s+to))",
            normalized,
        )
    )


def _complete_eav_value_profiles(profiles: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return profiles whose observed values cover the full distinct domain."""

    complete: list[dict[str, Any]] = []
    for profile in profiles:
        values = [item for item in profile.get("top_values") or [] if isinstance(item, dict)]
        distinct_count = int(profile.get("distinct_value_count") or 0)
        if distinct_count > 0 and len(values) >= distinct_count:
            complete.append(profile)
    return complete


def _normalize_table_scope(value: str) -> str:
    parts = [part.strip().strip('"').lower() for part in str(value or "").split(".") if part.strip()]
    if len(parts) == 1:
        return f"public.{parts[0]}"
    return ".".join(parts[-2:])


def _schema_receipt_payload_is_valid(
    receipt: dict[str, Any],
    *,
    route: Any,
    parent_sql: str,
) -> bool:
    evidence = receipt.get("evidence") if isinstance(receipt.get("evidence"), dict) else {}
    digest = hashlib.sha256(
        json.dumps(evidence, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    parent_digest = f"sha256:{hashlib.sha256(parent_sql.encode()).hexdigest()}"
    return bool(
        receipt.get("id")
        and float(receipt.get("expires_at") or 0) >= time.time()
        and str(receipt.get("sha256") or "") == f"sha256:{digest}"
        and str(evidence.get("mode") or "") == "type_names"
        and str(evidence.get("database_source_id") or "") == route.database_source_id
        and _normalize_table_scope(str(evidence.get("table_name") or ""))
        in {_normalize_table_scope(item) for item in route.table_names}
        and str(evidence.get("parent_sql_sha256") or "") == parent_digest
        and bool(evidence.get("parent_type_names"))
    )


def _schema_discovery_receipt_payload_is_valid(receipt: dict[str, Any], *, route: Any) -> bool:
    evidence = receipt.get("evidence") if isinstance(receipt.get("evidence"), dict) else {}
    digest = hashlib.sha256(
        json.dumps(evidence, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return bool(
        receipt.get("id")
        and str(receipt.get("receipt_kind") or "") == "discovery"
        and float(receipt.get("expires_at") or 0) >= time.time()
        and str(receipt.get("sha256") or "") == f"sha256:{digest}"
        and str(evidence.get("mode") or "") in {"type_names", "type_values", "value_profile"}
        and str(evidence.get("database_source_id") or "") == route.database_source_id
        and _normalize_table_scope(str(evidence.get("table_name") or ""))
        in {_normalize_table_scope(item) for item in route.table_names}
        and bool(evidence.get("profile_revision"))
    )


async def _inspect_live_eav_type_names(
    *,
    source: Any,
    route: Any,
    requested_names: set[str],
) -> list[dict[str, Any]]:
    """Targeted live catalog inspection for generated EAV literals.

    The query is server-built from an authorized table.  Model text is passed
    only as bound search parameters, so it cannot alter the SQL shape.
    """

    table_name = _vehicle_params_table(route)
    if not table_name or not requested_names:
        return []
    names = sorted(requested_names)[:250]
    exact_placeholders = [f":exact_{index}" for index, _ in enumerate(names)]
    parameters: dict[str, Any] = {
        f"exact_{index}": name for index, name in enumerate(names)
    }
    statement = text(
        f"SELECT type_name, COUNT(*) AS count FROM {table_name} "
        f"WHERE type_name IN ({', '.join(exact_placeholders)}) "
        "GROUP BY type_name ORDER BY COUNT(*) DESC, type_name"
    )
    engine = create_async_engine(database_source_url(source), pool_pre_ping=True)
    try:
        async with engine.connect() as connection:
            result = await connection.execute(statement, parameters)
            return [dict(row) for row in result.mappings().all()]
    finally:
        await engine.dispose()


async def _inspect_live_eav_value_profiles_fallback(
    *,
    source: Any,
    route: Any,
    type_names: list[str],
    values_per_type: int,
    timeout_seconds: float,
) -> list[dict[str, Any]]:
    """Build a non-cacheable bounded profile with one aggregate table scan."""

    table_name = _vehicle_params_table(route)
    if not table_name or not type_names:
        return []
    placeholders = [f":fallback_name_{index}" for index, _ in enumerate(type_names)]
    parameters: dict[str, Any] = {
        f"fallback_name_{index}": name for index, name in enumerate(type_names)
    }
    parameters["values_per_type"] = max(1, min(int(values_per_type), 200))
    statement = text(
        f"""
        WITH value_counts AS (
            SELECT
                type_name,
                COALESCE(NULLIF(btrim(type_value), ''), '<NULL_OR_BLANK>') AS type_value,
                COUNT(*) AS row_count
            FROM {table_name}
            WHERE type_name IN ({', '.join(placeholders)})
            GROUP BY type_name, COALESCE(NULLIF(btrim(type_value), ''), '<NULL_OR_BLANK>')
        ),
        ranked AS (
            SELECT
                value_counts.*,
                COUNT(*) OVER (PARTITION BY type_name) AS distinct_value_count,
                SUM(row_count) OVER (PARTITION BY type_name) AS total_row_count,
                ROW_NUMBER() OVER (
                    PARTITION BY type_name
                    ORDER BY row_count DESC, type_value
                ) AS value_rank
            FROM value_counts
        )
        SELECT type_name, type_value, row_count, distinct_value_count, total_row_count
        FROM ranked
        WHERE value_rank <= :values_per_type
        ORDER BY type_name, value_rank
        """
    )
    engine = create_async_engine(database_source_url(source), pool_pre_ping=True)
    try:
        async with engine.connect() as connection:
            result = await asyncio.wait_for(
                connection.execute(statement, parameters),
                timeout=max(1.0, min(20.0, float(timeout_seconds) * 2)),
            )
            rows = [dict(row) for row in result.mappings().all()]
    except Exception as exc:
        logger.warning(
            "[nl2sql-service] eav_value_profile_fallback_failed type_names=%s error_type=%s",
            type_names,
            type(exc).__name__,
        )
        return []
    finally:
        await engine.dispose()

    grouped: dict[str, dict[str, Any]] = {}
    for row in rows:
        type_name = str(row.get("type_name") or "")
        profile = grouped.setdefault(
            type_name,
            {
                "type_name": type_name,
                "distinct_value_count": int(row.get("distinct_value_count") or 0),
                "total_row_count": int(row.get("total_row_count") or 0),
                "total_model_count": None,
                "conflict_model_count": None,
                "conflict_rate": None,
                "conflict_examples": [],
                "conflict_profile_available": False,
                "top_values": [],
                "cache_hit": False,
                "cacheable": False,
                "profile_revision_kind": "bounded_runtime_fallback",
            },
        )
        profile["top_values"].append(
            {
                "value": row.get("type_value"),
                "row_count": int(row.get("row_count") or 0),
                "model_count": None,
            }
        )
    for type_name, profile in grouped.items():
        revision_payload = {
            "source_id": str(route.database_source_id or ""),
            "table": _normalize_table_scope(str(table_name).replace('"', "")),
            "type_name": type_name,
            "profile_revision_kind": profile["profile_revision_kind"],
            "distinct_value_count": profile["distinct_value_count"],
            "total_row_count": profile["total_row_count"],
            "top_values": profile["top_values"],
        }
        profile["source_revision"] = "sha256:" + hashlib.sha256(
            json.dumps(
                revision_payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        profile["value_profile_hash"] = "sha256:" + hashlib.sha256(
            json.dumps(
                profile,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
    return [grouped[name] for name in sorted(grouped)]


async def _inspect_live_eav_value_profiles(
    *,
    source: Any,
    route: Any,
    type_names: set[str],
    values_per_type: int = 200,
    timeout_seconds: float = 10.0,
    semantic_hash: str = "",
    semantic_contract_hashes: dict[str, str] | None = None,
    permission_epoch: int = 1,
) -> list[dict[str, Any]]:
    """Profile exact EAV values before the first semantic SQL refinement.

    The statement shape and table are server-owned; model-derived type names
    are bound parameters.  The bounded profile exposes population counts and
    same-model multi-value conflicts so the refiner does not infer a domain
    from preview rows or choose an arbitrary DISTINCT ON value.
    """

    table_name = _vehicle_params_table(route)
    names = sorted(str(item) for item in type_names if str(item))[:3]
    if not table_name or not names:
        return []
    exact_placeholders = [f":profile_name_{index}" for index, _ in enumerate(names)]
    parameters: dict[str, Any] = {
        f"profile_name_{index}": name for index, name in enumerate(names)
    }
    parameters["values_per_type"] = max(1, min(int(values_per_type), 200))
    grain_contract_hash = "sha256:" + hashlib.sha256(
        b"vehicle_params:(brand,serial_name,car_name):distinct-valid-values/v1"
    ).hexdigest()
    revision_statement = text(
        f"""
        WITH value_counts AS (
            SELECT
                type_name,
                COALESCE(NULLIF(btrim(type_value), ''), '<NULL_OR_BLANK>') AS type_value,
                COUNT(*) AS row_count,
                COUNT(DISTINCT (brand, serial_name, car_name)) AS model_count
            FROM {table_name}
            WHERE type_name IN ({', '.join(exact_placeholders)})
            GROUP BY type_name, COALESCE(NULLIF(btrim(type_value), ''), '<NULL_OR_BLANK>')
        ),
        model_stats AS (
            SELECT
                type_name,
                COUNT(*) AS total_model_count,
                COUNT(*) FILTER (WHERE valid_value_count > 1) AS conflict_model_count
            FROM (
                SELECT
                    type_name,
                    brand,
                    serial_name,
                    car_name,
                    COUNT(DISTINCT NULLIF(btrim(type_value), '')) AS valid_value_count
                FROM {table_name}
                WHERE type_name IN ({', '.join(exact_placeholders)})
                GROUP BY type_name, brand, serial_name, car_name
            ) AS per_model
            GROUP BY type_name
        )
        SELECT
            value_counts.type_name,
            COUNT(*) AS distinct_value_count,
            SUM(value_counts.row_count) AS total_row_count,
            md5(string_agg(
                md5(value_counts.type_value) || ':' || value_counts.row_count::text || ':' || value_counts.model_count::text,
                '|' ORDER BY value_counts.type_value
            )) AS distribution_revision,
            COALESCE(model_stats.total_model_count, 0) AS total_model_count,
            COALESCE(model_stats.conflict_model_count, 0) AS conflict_model_count
        FROM value_counts
        LEFT JOIN model_stats USING (type_name)
        GROUP BY value_counts.type_name, model_stats.total_model_count, model_stats.conflict_model_count
        ORDER BY value_counts.type_name
        """
    )
    statement = text(
        f"""
        WITH value_counts AS (
            SELECT
                type_name,
                COALESCE(NULLIF(btrim(type_value), ''), '<NULL_OR_BLANK>') AS type_value,
                COUNT(*) AS row_count,
                COUNT(DISTINCT (brand, serial_name, car_name)) AS model_count
            FROM {table_name}
            WHERE type_name IN ({', '.join(exact_placeholders)})
            GROUP BY type_name, COALESCE(NULLIF(btrim(type_value), ''), '<NULL_OR_BLANK>')
        ),
        ranked AS (
            SELECT
                value_counts.*,
                COUNT(*) OVER (PARTITION BY type_name) AS distinct_value_count,
                ROW_NUMBER() OVER (
                    PARTITION BY type_name
                    ORDER BY row_count DESC, type_value
                ) AS value_rank
            FROM value_counts
        ),
        model_conflicts AS (
            SELECT type_name, COUNT(*) AS conflict_model_count
            FROM (
                SELECT type_name, brand, serial_name, car_name
                FROM {table_name}
                WHERE type_name IN ({', '.join(exact_placeholders)})
                  AND NULLIF(btrim(type_value), '') IS NOT NULL
                GROUP BY type_name, brand, serial_name, car_name
                HAVING COUNT(DISTINCT btrim(type_value)) > 1
            ) AS conflicting_models
            GROUP BY type_name
        )
        SELECT
            ranked.type_name,
            ranked.type_value,
            ranked.row_count,
            ranked.model_count,
            ranked.distinct_value_count,
            COALESCE(model_conflicts.conflict_model_count, 0) AS conflict_model_count
        FROM ranked
        LEFT JOIN model_conflicts USING (type_name)
        WHERE ranked.value_rank <= :values_per_type
        ORDER BY ranked.type_name, ranked.value_rank
        """
    )
    source_id = str(route.database_source_id or "")
    normalized_table = _normalize_table_scope(str(table_name).replace('"', ""))
    engine = create_async_engine(database_source_url(source), pool_pre_ping=True)
    try:
        try:
            async with engine.connect() as connection:
                revision_result = await asyncio.wait_for(
                    connection.execute(revision_statement, parameters),
                    timeout=max(0.1, float(timeout_seconds)),
                )
                revision_rows = [dict(row) for row in revision_result.mappings().all()]
                revisions: dict[str, dict[str, Any]] = {}
                cached_profiles: dict[str, dict[str, Any]] = {}
                missing_names: list[str] = []
                for revision_row in revision_rows:
                    type_name = str(revision_row.get("type_name") or "")
                    source_revision = "sha256:" + hashlib.sha256(
                        json.dumps(
                            {
                                "source_id": source_id,
                                "table": normalized_table,
                                "type_name": type_name,
                                "distribution_revision": revision_row.get("distribution_revision"),
                                "distinct_value_count": int(revision_row.get("distinct_value_count") or 0),
                                "total_row_count": int(revision_row.get("total_row_count") or 0),
                                "total_model_count": int(revision_row.get("total_model_count") or 0),
                                "conflict_model_count": int(revision_row.get("conflict_model_count") or 0),
                            },
                            ensure_ascii=False,
                            sort_keys=True,
                            separators=(",", ":"),
                        ).encode("utf-8")
                    ).hexdigest()
                    revisions[type_name] = {**revision_row, "source_revision": source_revision}
                    cached = eav_profile_catalog.get(
                        source_id=source_id,
                        table=normalized_table,
                        type_name=type_name,
                        source_revision=source_revision,
                        grain_contract_hash=grain_contract_hash,
                        semantic_hash=(semantic_contract_hashes or {}).get(type_name, semantic_hash),
                        permission_epoch=permission_epoch,
                    )
                    if cached is None:
                        missing_names.append(type_name)
                    else:
                        profile = dict(cached.get("profile") or {})
                        profile.update(
                            {
                                "profile_catalog_id": cached.get("id"),
                                "value_profile_hash": cached.get("value_profile_hash"),
                                "source_revision": source_revision,
                                "cache_hit": True,
                            }
                        )
                        cached_profiles[type_name] = profile
                        eav_profile_catalog.record_reuse(str(cached.get("id") or ""))
                if not missing_names:
                    return [cached_profiles[name] for name in sorted(cached_profiles)]
                result = await asyncio.wait_for(
                    connection.execute(statement, parameters),
                    timeout=max(0.1, float(timeout_seconds)),
                )
                rows = [dict(row) for row in result.mappings().all()]
                conflict_result = await asyncio.wait_for(
                    connection.execute(
                        text(
                            f"""
                            SELECT type_name, brand, serial_name, car_name,
                                   array_agg(DISTINCT btrim(type_value) ORDER BY btrim(type_value)) AS values
                            FROM {table_name}
                            WHERE type_name IN ({', '.join(exact_placeholders)})
                              AND NULLIF(btrim(type_value), '') IS NOT NULL
                            GROUP BY type_name, brand, serial_name, car_name
                            HAVING COUNT(DISTINCT btrim(type_value)) > 1
                            ORDER BY type_name, brand, serial_name, car_name
                            LIMIT 30
                            """
                        ),
                        parameters,
                    ),
                    timeout=max(0.1, float(timeout_seconds)),
                )
                conflict_rows = [dict(row) for row in conflict_result.mappings().all()]
        except Exception as exc:
            logger.warning(
                "[nl2sql-service] eav_value_profile_failed type_names=%s error_type=%s; using bounded fallback",
                names,
                type(exc).__name__,
            )
            return await _inspect_live_eav_value_profiles_fallback(
                source=source,
                route=route,
                type_names=names,
                values_per_type=values_per_type,
                timeout_seconds=timeout_seconds,
            )
    finally:
        await engine.dispose()

    grouped: dict[str, dict[str, Any]] = dict(cached_profiles)
    for row in rows:
        type_name = str(row.get("type_name") or "")
        if type_name in cached_profiles:
            continue
        revision = revisions.get(type_name, {})
        total_model_count = int(revision.get("total_model_count") or 0)
        conflict_model_count = int(revision.get("conflict_model_count") or 0)
        profile = grouped.setdefault(
            type_name,
            {
                "type_name": type_name,
                "distinct_value_count": int(row.get("distinct_value_count") or 0),
                "total_row_count": int(revision.get("total_row_count") or 0),
                "total_model_count": total_model_count,
                "conflict_model_count": conflict_model_count,
                "conflict_rate": round(conflict_model_count / max(1, total_model_count), 6),
                "conflict_examples": [],
                "top_values": [],
                "source_revision": str(revision.get("source_revision") or ""),
                "grain_contract_hash": grain_contract_hash,
                "cache_hit": False,
            },
        )
        profile["top_values"].append(
            {
                "value": row.get("type_value"),
                "row_count": int(row.get("row_count") or 0),
                "model_count": int(row.get("model_count") or 0),
            }
        )
    for row in conflict_rows:
        profile = grouped.get(str(row.get("type_name") or ""))
        if profile is None or profile.get("cache_hit"):
            continue
        profile["conflict_examples"].append(
            {
                "brand": row.get("brand"),
                "serial_name": row.get("serial_name"),
                "car_name": row.get("car_name"),
                "values": list(row.get("values") or []),
            }
        )
    for type_name, profile in grouped.items():
        if profile.get("cache_hit"):
            continue
        record = eav_profile_catalog.put(
            source_id=source_id,
            table=normalized_table,
            type_name=type_name,
            source_revision=str(profile.get("source_revision") or ""),
            grain_contract_hash=grain_contract_hash,
            semantic_hash=(semantic_contract_hashes or {}).get(type_name, semantic_hash),
            permission_epoch=permission_epoch,
            profile=profile,
        )
        profile["profile_catalog_id"] = record["id"]
        profile["value_profile_hash"] = record["value_profile_hash"]
    return [grouped[name] for name in sorted(grouped)]


def _discover_vanna_eav_candidates(vanna: Any, requested_names: set[str]) -> list[dict[str, Any]]:
    """Ask Vanna for candidates, without treating similarity as truth."""

    collector = getattr(vanna, "get_related_entities", None)
    if not callable(collector):
        return []
    found: dict[tuple[str, str], dict[str, Any]] = {}
    for name in sorted(requested_names)[:20]:
        try:
            items = collector(name, entity_types=["配置名称"], limit=30)
        except Exception:  # pragma: no cover - external vector runtime
            continue
        for item in items if isinstance(items, list) else []:
            if not isinstance(item, dict):
                continue
            column = str(item.get("table_column") or "").strip().lower()
            if not (column.endswith(".vehicle_params.type_name") or column == "vehicle_params.type_name"):
                continue
            canonical = str(item.get("canonical_name") or item.get("name") or "").strip()
            if canonical:
                found[(canonical, column)] = dict(item)
    return list(found.values())


def _split_eav_name_unit(value: str) -> tuple[str, str]:
    normalized = str(value or "").strip()
    match = re.search(r"[\[（(]([^\]）)]+)[\]）)]\s*$", normalized)
    unit = re.sub(r"\s+", "", match.group(1)).lower() if match else ""
    base = normalized[: match.start()].strip() if match else normalized
    return eav_concept_key(base), unit


def _eav_alias_authorizes(requested: str, canonical: str, alias: str) -> bool:
    requested_base, requested_unit = _split_eav_name_unit(requested)
    alias_base, alias_unit = _split_eav_name_unit(alias)
    _canonical_base, canonical_unit = _split_eav_name_unit(canonical)
    if requested_base != alias_base:
        return False
    if not requested_unit:
        return True
    expected_unit = alias_unit or canonical_unit
    return bool(expected_unit and requested_unit == expected_unit)


def _entity_authorized_replacement_pairs(
    requested_names: set[str],
    entities: list[dict[str, Any]],
) -> set[tuple[str, str]]:
    """Return only explicit canonical/alias mappings supplied by Vanna."""

    pairs: set[tuple[str, str]] = set()

    for item in entities:
        canonical = str(item.get("canonical_name") or item.get("name") or "").strip()
        if not canonical:
            continue
        aliases = item.get("aliases")
        if isinstance(aliases, str):
            aliases = [aliases]
        elif not isinstance(aliases, list):
            aliases = []
        for requested in requested_names:
            if any(
                _eav_alias_authorizes(requested, canonical, str(alias))
                for alias in [canonical, *aliases]
                if str(alias).strip()
            ):
                pairs.add((requested, canonical))
    return pairs


def _live_rows_as_entities(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "entity_type": "配置名称",
            "canonical_name": str(row.get("type_name") or ""),
            "aliases": [],
            "table_column": "public.vehicle_params.type_name",
            "description": "live database type_name catalog evidence",
            "score": 1.0,
            "evidence_source": "live_database",
            "row_count": int(row.get("count") or 0),
        }
        for row in rows
        if str(row.get("type_name") or "").strip()
    ]


def _compile_request_semantic_context(request: DatabaseQueryRequest) -> SemanticQueryContext:
    """Compile the shared semantic contract for one database request.

    A selected analytics model is an authority boundary, not a signal that the
    outer Agent necessarily supplied explicit semantic ids. When ids are
    absent, fuzzy resolution remains available but is restricted to assets
    declared by that model. A genuine no-match stays in generalized mode.
    """

    return compile_semantic_query_context(
        question=request.semantic_question or request.question,
        model_id=request.model_id,
        selected_semantic_asset_ids=request.measure_ids,
        model_registry=get_analytics_model_registry(),
    )


def _resolve_request_semantic_assets(request: DatabaseQueryRequest) -> dict[str, Any]:
    """Compatibility view of the shared compiler's semantic resolution."""

    return _compile_request_semantic_context(request).resolution


_CONFIG_RATE_SQL_TEMPLATE = """
当允许表包含 vehicle_model_base 时，配置率、配备率、搭载率等问题必须优先使用
vehicle_model_base 计算分母和常用维度筛选，再 JOIN vehicle_params 判断配置明细。

推荐模板：

WITH denominator AS (
  SELECT brand, serial_name, car_name
  FROM vehicle_model_base
  WHERE launch_year = 2026
    AND energy_type = '纯电'
    AND vehicle_level IS DISTINCT FROM '皮卡'
),
numerator AS (
  SELECT DISTINCT d.brand, d.serial_name, d.car_name
  FROM denominator d
  JOIN vehicle_params vp
    ON vp.brand = d.brand
   AND vp.serial_name = d.serial_name
   AND vp.car_name = d.car_name
  WHERE vp.type_name = '可调悬架种类'
    AND vp.type_value IS NOT NULL
    AND vp.type_value NOT IN ('', '-', '无', '未配备', '不配备')
    AND vp.type_value LIKE '%空气悬架%'
)
SELECT
  COUNT(*) AS total_models,
  (SELECT COUNT(*) FROM numerator) AS equipped_models,
  ROUND((SELECT COUNT(*) FROM numerator) * 100.0 / NULLIF(COUNT(*), 0), 2) AS equip_rate_pct
FROM denominator;

如果允许表不包含 vehicle_model_base，才回退使用 vehicle_params EAV flags。vehicle_params 是 EAV 风格配置明细表。
配置率、多条件车型筛选、配备率等问题不要使用多层 EXISTS / NOT EXISTS 反复自关联 vehicle_params，
也不要用 COUNT(DISTINCT ...) 在多层子查询上直接统计。

回退模板：

WITH car_flags AS (
  SELECT
    brand,
    serial_name,
    car_name,
    BOOL_OR(type_name = '上市时间' AND type_value >= '2026-01-01' AND type_value < '2027-01-01') AS is_2026_launch,
    BOOL_OR(type_name = '能源类型' AND type_value = '纯电') AS is_ev,
    BOOL_OR(type_name = '级别' AND type_value = '皮卡') AS is_pickup,
    BOOL_OR(type_name = '可调悬架种类' AND type_value LIKE '%空气悬架%') AS has_air_suspension
  FROM vehicle_params
  WHERE car_name IS NOT NULL
    AND brand IS NOT NULL
    AND serial_name IS NOT NULL
    AND type_name IN ('上市时间', '能源类型', '级别', '可调悬架种类')
  GROUP BY brand, serial_name, car_name
)
SELECT
  COUNT(*) FILTER (WHERE is_2026_launch AND is_ev AND NOT is_pickup) AS total_models,
  COUNT(*) FILTER (WHERE is_2026_launch AND is_ev AND NOT is_pickup AND has_air_suspension) AS equipped_models,
  ROUND(
    COUNT(*) FILTER (WHERE is_2026_launch AND is_ev AND NOT is_pickup AND has_air_suspension) * 100.0
    / NULLIF(COUNT(*) FILTER (WHERE is_2026_launch AND is_ev AND NOT is_pickup), 0),
    2
  ) AS equip_rate_pct
FROM car_flags;

实际 SQL 可按用户问题替换年份、能源类型、配置字段和分组维度。
""".strip()


def _get_entity_top_k_config() -> tuple[int, dict[str, int]]:
    try:
        query_config = (get_vanna_config().get("query") or {})
        default_top_k = max(1, int(query_config.get("entity_top_k_default") or VANNA_ENTITY_TOP_K_PER_TYPE))
        by_type = {
            str(key): max(1, int(value))
            for key, value in (query_config.get("entity_top_k_by_type") or {}).items()
            if str(key).strip()
        }
        return default_top_k, by_type
    except Exception:
        return VANNA_ENTITY_TOP_K_PER_TYPE, {}


def _entity_top_k_for_type(entity_type: str, default_top_k: int, by_type: dict[str, int]) -> int:
    return by_type.get(entity_type) or by_type.get(str(entity_type).strip()) or default_top_k


def _format_analytics_model_for_sql_prompt(model_id: str | None) -> tuple[str, dict[str, Any]]:
    """Compatibility wrapper around the shared SQL semantic adapter."""
    normalized_id = str(model_id or "").strip()
    if not normalized_id:
        return "", {}
    model = get_analytics_model_registry().get_model_context(normalized_id)
    return format_analytics_model_for_sql_prompt(model)


def _detect_semantic_sql_conflicts(
    sql: str,
    semantic_trace: dict[str, Any],
    route: Any | None = None,
    *,
    question: str = "",
) -> list[str]:
    """Compatibility wrapper for tests and callers that expect plain messages."""

    conflicts = detect_guardrail_conflicts(
        sql,
        source_name="",
        route=route,
        semantic_trace=semantic_trace,
        question=question,
    )
    return conflicts_to_messages(conflicts)


def _detect_sql_guardrail_conflicts(
    sql: str,
    *,
    source_name: str,
    route: Any,
    semantic_trace: dict[str, Any],
    question: str,
) -> list[GuardrailConflict]:
    return detect_guardrail_conflicts(
        sql,
        source_name=source_name,
        route=route,
        semantic_trace=semantic_trace,
        question=question,
    )


def _guardrail_messages(conflicts: list[GuardrailConflict]) -> list[str]:
    return conflicts_to_messages(conflicts)


def _vanna_llm_context(vanna: Any) -> dict[str, str]:
    client = getattr(vanna, "client", None)
    base_url = getattr(client, "base_url", None)
    config = getattr(vanna, "config", None) or {}
    return {
        "model": str(config.get("model") or ""),
        "base_url": str(base_url or config.get("base_url") or ""),
    }


def _normalize_reference_value(value: Any, *, max_chars: int = 1200) -> Any:
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, list):
        return [_normalize_reference_value(item, max_chars=max(160, max_chars // 4)) for item in value[:20]]
    if isinstance(value, dict):
        return {str(key): _normalize_reference_value(item, max_chars=max_chars) for key, item in value.items()}
    return str(value)[:max_chars]


def _summarize_reference_items(items: Any, *, max_items: int = 5, max_chars: int = 1200) -> dict[str, Any]:
    if not isinstance(items, list):
        return {"count": 0, "preview_count": 0, "omitted_count": 0, "items": []}
    summarized: list[Any] = []
    for item in items[:max_items]:
        if isinstance(item, dict):
            summarized.append({key: _normalize_reference_value(value, max_chars=max_chars) for key, value in item.items()})
        else:
            summarized.append(str(item)[:max_chars])
    return {
        "count": len(items),
        "preview_count": len(summarized),
        "omitted_count": max(0, len(items) - len(summarized)),
        "items": summarized,
    }


def _score_value(item: dict[str, Any]) -> float:
    try:
        return float(item.get("score") or item.get("distance") or 0)
    except Exception:
        return 0.0


def _compact_entity_item(item: dict[str, Any]) -> dict[str, Any]:
    aliases = item.get("aliases")
    if isinstance(aliases, str):
        compact_aliases: Any = aliases[:240]
    elif isinstance(aliases, list):
        compact_aliases = [str(alias)[:80] for alias in aliases[:5]]
    else:
        compact_aliases = []
    return {
        "name": str(item.get("canonical_name") or item.get("name") or "")[:240],
        "aliases": compact_aliases,
        "column": str(item.get("table_column") or "")[:240],
        "score": _score_value(item),
    }


def _route_table_names(route: Any) -> set[str]:
    names: set[str] = set()
    for table_name in getattr(route, "table_names", []) or []:
        value = str(table_name).strip().strip('"')
        if not value:
            continue
        names.add(value)
        names.add(value.split(".")[-1])
    return names


def _entity_matches_route_table(entity: dict[str, Any], route: Any) -> bool:
    table_column = str(entity.get("table_column") or "")
    if not table_column:
        return False
    for table_name in _route_table_names(route):
        if table_column == table_name:
            return True
        if table_column.startswith(f"{table_name}."):
            return True
        if f".{table_name}." in table_column:
            return True
    return False


def _collect_route_entity_types(vanna: Any, route: Any) -> list[str]:
    try:
        milvus_client = getattr(vanna, "milvus_client", None)
        entity_collection = getattr(vanna, "entity_collection", None)
        if milvus_client is not None and entity_collection:
            iterator = milvus_client.query_iterator(
                collection_name=entity_collection,
                filter=None,
                output_fields=["entity_type", "table_column"],
                batch_size=1000,
            )
            entity_types: set[str] = set()
            try:
                while True:
                    batch = iterator.next()
                    if not batch:
                        break
                    for row in batch:
                        entity_type = str(row.get("entity_type") or "").strip()
                        if entity_type and _entity_matches_route_table(row, route):
                            entity_types.add(entity_type)
            finally:
                iterator.close()
            return sorted(entity_types)
    except Exception as exc:  # pragma: no cover - depends on Milvus/runtime state
        logger.warning("[nl2sql-service] entity_type_scan_failed error=%s", exc)

    collector = getattr(vanna, "get_all_entities", None)
    if not callable(collector):
        return []
    try:
        rows = collector() or []
    except Exception as exc:  # pragma: no cover - depends on Milvus/runtime state
        logger.warning("[nl2sql-service] entity_type_scan_fallback_failed error=%s", exc)
        return []
    entity_types = {
        str(row.get("entity_type") or "").strip()
        for row in rows
        if isinstance(row, dict)
        and str(row.get("entity_type") or "").strip()
        and _entity_matches_route_table(row, route)
    }
    return sorted(entity_types)


def _summarize_entities_by_type(vanna: Any, question: str, route: Any) -> dict[str, Any]:
    entity_types = _collect_route_entity_types(vanna, route)
    default_top_k, top_k_by_type = _get_entity_top_k_config()
    base_result: dict[str, Any] = {
        "strategy": "per_type_top_k",
        "total": 0,
        "top_k": {
            "default": default_top_k,
            "by_type": top_k_by_type,
        },
        "groups": [],
        "_prompt_items": [],
    }
    if not entity_types:
        return base_result

    collector = getattr(vanna, "get_related_entities", None)
    if not callable(collector):
        base_result["entity_types"] = entity_types
        return base_result

    items: list[dict[str, Any]] = []
    recall_errors: dict[str, str] = {}
    recall_stats: dict[str, dict[str, int]] = {}
    for entity_type in entity_types:
        type_top_k = _entity_top_k_for_type(entity_type, default_top_k, top_k_by_type)
        try:
            type_items = collector(
                question,
                entity_types=[entity_type],
                limit=type_top_k,
            ) or []
        except Exception as exc:  # pragma: no cover - depends on Milvus/runtime state
            recall_errors[entity_type] = str(exc)
            logger.warning(
                "[nl2sql-service] entity_recall_failed entity_type=%s top_k=%s error=%s",
                entity_type,
                type_top_k,
                exc,
            )
            type_items = []
        normalized_type_items = [item for item in type_items if isinstance(item, dict)]
        recall_stats[entity_type] = {
            "requested_top_k": type_top_k,
            "recalled_count": len(normalized_type_items),
        }
        items.extend(normalized_type_items)

    if not items and recall_errors:
        base_result["entity_types"] = entity_types
        base_result["stats"] = recall_stats
        base_result["errors"] = recall_errors
        return base_result

    grouped: dict[str, list[dict[str, Any]]] = {}
    for item in items:
        if not isinstance(item, dict):
            continue
        if not _entity_matches_route_table(item, route):
            continue
        entity_type = str(item.get("entity_type") or "未分类")
        grouped.setdefault(entity_type, []).append(item)

    groups: list[dict[str, Any]] = []
    prompt_items: list[dict[str, Any]] = []
    for entity_type in entity_types:
        type_items = sorted(grouped.get(entity_type, []), key=_score_value, reverse=True)
        type_top_k = _entity_top_k_for_type(entity_type, default_top_k, top_k_by_type)
        selected_type_items = type_items[:type_top_k]
        prompt_items.extend(selected_type_items)
        recall_stats.setdefault(entity_type, {"requested_top_k": type_top_k, "recalled_count": 0})
        recall_stats[entity_type]["matched_count"] = len(type_items)
        recall_stats[entity_type]["prompt_count"] = len(selected_type_items)
        if selected_type_items or recall_stats[entity_type].get("recalled_count"):
            first_column = ""
            for selected_item in selected_type_items:
                first_column = str(selected_item.get("table_column") or "")
                if first_column:
                    break
            groups.append(
                {
                    "type": entity_type,
                    "top_k": type_top_k,
                    "count": len(selected_type_items),
                    "column": first_column,
                    "items": [_compact_entity_item(item) for item in selected_type_items],
                }
            )

    return {
        "strategy": "per_type_top_k",
        "entity_types": entity_types,
        "total": len(prompt_items),
        "top_k": {
            "default": default_top_k,
            "by_type": top_k_by_type,
        },
        "groups": groups,
        "stats": recall_stats,
        "errors": recall_errors,
        "_prompt_items": prompt_items,
    }


def _collect_vanna_references(vanna: Any, question: str, route: Any) -> dict[str, Any]:
    references: dict[str, Any] = {}
    collectors = {
        "ddl": getattr(vanna, "get_related_ddl", None),
        "documentation": getattr(vanna, "get_related_documentation", None),
        "sql_examples": getattr(vanna, "get_similar_question_sql", None),
    }
    for key, collector in collectors.items():
        if not callable(collector):
            continue
        try:
            references[key] = _summarize_reference_items(
                collector(question),
                max_items=VANNA_REFERENCE_TOP_K,
            )
        except Exception as exc:  # pragma: no cover - depends on Milvus/runtime state
            references[key] = {"count": 0, "items": [], "error": str(exc)}
    entities_by_type = _summarize_entities_by_type(vanna, question, route)
    prompt_items = entities_by_type.pop("_prompt_items", [])
    references["entities"] = entities_by_type
    references["_prompt_entities"] = prompt_items
    return references


def _format_entity_evidence_for_sql(items: list[dict[str, Any]]) -> str:
    """Format authoritative physical entity evidence for final SQL refinement."""

    evidence: list[dict[str, Any]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        aliases = item.get("aliases")
        if isinstance(aliases, str):
            aliases = [aliases]
        elif not isinstance(aliases, list):
            aliases = []
        evidence.append(
            {
                "entity_type": str(item.get("entity_type") or ""),
                "canonical_name": str(item.get("canonical_name") or item.get("name") or ""),
                "aliases": [str(alias) for alias in aliases[:10]],
                "table_column": str(item.get("table_column") or ""),
                "description": str(item.get("description") or item.get("definition") or "")[:800],
                "score": _score_value(item),
            }
        )
    return json.dumps(evidence, ensure_ascii=False, indent=2)


def _format_reference_evidence_for_sql(references: dict[str, Any]) -> str:
    """Keep Vanna's retrieved schema/examples visible to the final refiner."""

    return json.dumps(
        {
            key: references.get(key)
            for key in (
                "ddl",
                "documentation",
                "sql_examples",
                "schema_discovery",
                "eav_value_profiles",
                "eav_value_profile_warning",
                "verified_query_plans",
            )
            if references.get(key) is not None
        },
        ensure_ascii=False,
        indent=2,
    )


def _compose_sql_refinement_prompt(
    *,
    question: str,
    candidate_sql: str,
    route: Any,
    semantic_context: str,
    references: dict[str, Any],
    prompt_entities: list[dict[str, Any]],
    correction_instruction: str = "",
) -> list[dict[str, str]]:
    """Build the final SQL pass without contaminating Vanna retrieval.

    Semantic assets define business meaning. Retrieved database entities define
    physical facts such as actual table/column names and canonical EAV values.
    Keeping those authorities explicit is what lets fuzzy, AI-native semantic
    assets coexist with deterministic database evidence.
    """

    correction_block = ""
    if correction_instruction:
        correction_block = (
            "\n\n<required_correction>\n"
            f"{correction_instruction.strip()}\n"
            "</required_correction>"
        )
    system_prompt = (
        "你是 NL2SQL 最终校正器。你的任务是基于候选 SQL、数据库证据和业务语义，"
        "输出一条可独立执行的 PostgreSQL 只读 SQL。只输出 SQL，不要解释。\n\n"
        "必须遵守以下证据优先级：\n"
        "1. 用户问题决定要回答的业务目标；\n"
        "2. 数据库实体证据决定物理事实，包括真实表名、列名、EAV 配置名称和枚举标准值；\n"
        "3. 语义资产/分析模型决定指标含义、分子分母、粒度、默认筛选和业务映射；\n"
        "4. Vanna 候选 SQL 只是草案，可以重写。\n\n"
        "冲突处理原则：\n"
        "- 语义资产与数据库实体证据在物理名称或存储值上冲突时，必须以数据库实体证据为准；\n"
        "- 不得把用户或语义资产中的自然语言概念直接当作数据库字面量，除非实体证据支持该值；\n"
        "- 例如用户说“自动驾驶级别”，实体证据给出 canonical_name=驾驶辅助级别，"
        "  则 SQL 必须使用“驾驶辅助级别”；\n"
        "- 数据库实体证据不改变用户的业务目标，语义资产也不得虚构不存在的表、列或枚举值；\n"
        "- 只能使用授权表，禁止任何写操作。"
        "- verified_query_plans 只来自已校验的同 Session 历史计划；仅当其 profile 修订仍匹配时，"
        "保留其中稳定的基础人群、分子谓词、排除项和粒度。追问只增加维度时不得重新解释稳定谓词；"
        "仍必须生成新 SQL，并接受当前轮验证。"
    )
    user_prompt = (
        "<original_question>\n"
        f"{question.strip()}\n"
        "</original_question>\n\n"
        "<authorized_route>\n"
        f"{route.prompt_context}\n"
        "</authorized_route>\n\n"
        "<database_entity_evidence authoritative_for_physical_facts=\"true\">\n"
        f"{_format_entity_evidence_for_sql(prompt_entities)}\n"
        "</database_entity_evidence>\n\n"
        "<vanna_retrieval_evidence>\n"
        f"{_format_reference_evidence_for_sql(references)}\n"
        "</vanna_retrieval_evidence>\n\n"
        "<semantic_assets authoritative_for_business_semantics=\"true\">\n"
        f"{semantic_context.strip() or '本次没有匹配到专用语义资产，按用户问题和数据库证据生成。'}\n"
        "</semantic_assets>\n\n"
        "<vanna_candidate_sql non_authoritative=\"true\">\n"
        f"{candidate_sql.strip()}\n"
        "</vanna_candidate_sql>"
        f"{correction_block}\n\n"
        "生成 SQL 要求：\n"
        "- 返回一条从 SELECT 或 WITH 开始、括号完整的 PostgreSQL 只读 SQL；\n"
        "- 汇总、趋势、占比和排名使用正确的聚合与 GROUP BY；\n"
        "- 不得用 LIMIT 近似聚合结果；只有用户明确要求 top-N 时才使用 LIMIT，明细结果由执行层分页；\n"
        "- 按月份查询 ISO 日期字符串时使用日期函数或 '-MM-' 形式，不要只匹配中文月份；\n"
        "- LEFT JOIN 后统计右表命中数时必须排除未命中的全 NULL 元组；\n"
        "- 配置率问题优先基于 vehicle_model_base 构造分母，再 JOIN vehicle_params 判断配置；\n"
        "- EAV 字段和值只能来自本轮数据库证据；如何选择观测值由你根据用户问题判断；\n"
        "- 当值画像覆盖全部 distinct 值时，先从 observed_values 中自行选择，再用 = 或 IN 写入 SQL；"
        "不得用 LIKE、正则、数值转换或字符串解析去匹配画像外的假想值；\n"
        "- 只输出 SQL。"
    )
    return [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt},
    ]


def _refine_sql_blocking(
    vanna: Any,
    *,
    question: str,
    candidate_sql: str,
    route: Any,
    semantic_context: str,
    references: dict[str, Any],
    prompt_entities: list[dict[str, Any]],
    correction_instruction: str = "",
) -> str:
    prompt = _compose_sql_refinement_prompt(
        question=question,
        candidate_sql=candidate_sql,
        route=route,
        semantic_context=semantic_context,
        references=references,
        prompt_entities=prompt_entities,
        correction_instruction=correction_instruction,
    )
    return extract_sql(vanna.submit_prompt(prompt))


async def _generate_grounded_sql(
    *,
    request: DatabaseQueryRequest,
    route: Any,
    semantic_context: str,
    semantic_trace: dict[str, Any],
    vanna: Any,
    stage_timings: dict[str, float],
    source: Any | None = None,
    progress_callback: ProgressCallback | None = None,
) -> tuple[str, dict[str, Any], str, dict[str, Any]]:
    """Generate one final SQL through a raw-evidence then semantic-refinement pipeline."""

    def record_stage(name: str, started: float) -> None:
        elapsed = round((perf_counter() - started) * 1000, 2)
        stage_timings[name] = round(stage_timings.get(name, 0.0) + elapsed, 2)

    llm_call_count = 0
    max_llm_calls = 3
    parse_repair_used = False

    def reserve_llm_call(stage: str) -> None:
        nonlocal llm_call_count
        if llm_call_count >= max_llm_calls:
            raise DatabaseKnowledgeQueryError(
                "SQL 自动修复预算已耗尽，已停止重复调用模型。",
                error_code="sql_repair_exhausted",
                stage=stage,
                recoverable=False,
                next_action="stop",
                attempt=llm_call_count,
                max_attempts=max_llm_calls,
            )
        llm_call_count += 1

    def refine_with_budget(*, stage: str, **kwargs: Any) -> str:
        reserve_llm_call(stage)
        try:
            return _refine_sql_blocking(vanna, **kwargs)
        except DatabaseKnowledgeQueryError:
            raise
        except Exception as exc:
            raise DatabaseKnowledgeQueryError(
                f"SQL {stage} 模型调用失败：{type(exc).__name__}: {exc}",
                error_code="sql_refinement_provider_error",
                stage=stage,
                recoverable=True,
                next_action="retry_once",
            ) from exc

    retrieval_question = request.question.strip()
    guardrail_note = ""
    started = perf_counter()
    references = await _await_with_progress(
        asyncio.to_thread(_collect_vanna_references, vanna, retrieval_question, route),
        callback=progress_callback,
        stage="vanna_retrieval",
    )
    record_stage("vanna_references_ms", started)
    prompt_entities = references.pop("_prompt_entities", [])
    entities_summary = references.get("entities") or {}
    entity_types = entities_summary.get("entity_types") or []
    top_k_config = entities_summary.get("top_k") or {}

    logger.info(
        "[nl2sql-service] raw_retrieval question_sha256=%s route=%s semantic_assets=%s entity_types=%s entity_prompt_items=%s",
        hashlib.sha256(retrieval_question.encode("utf-8")).hexdigest()[:20],
        summarize_table_route(route),
        semantic_trace.get("matched_count", 0),
        entity_types,
        len(prompt_entities),
    )

    # Physical facts must exist before the first SQL token is authored.  This
    # preflight is owned by the generator, so correctness does not depend on an
    # outer Agent remembering to call schema inspection in a particular order.
    technical_evidence = (
        request.technical_evidence if isinstance(request.technical_evidence, dict) else {}
    )
    try:
        permission_epoch = max(
            1,
            int((technical_evidence.get("cache_scope") or {}).get("permission_epoch") or 1),
        )
    except (AttributeError, TypeError, ValueError):
        permission_epoch = 1
    bindings = bindings_from_semantic_trace(semantic_trace, question=retrieval_question)
    preflight_type_names = _prompt_entity_type_names(prompt_entities) | {
        name for binding in bindings for name in binding.type_names
    }
    for receipt in technical_evidence.get("schema_receipts", []):
        if not isinstance(receipt, dict) or not _schema_discovery_receipt_payload_is_valid(receipt, route=route):
            continue
        evidence = receipt.get("evidence") if isinstance(receipt.get("evidence"), dict) else {}
        preflight_type_names.update(
            str(row.get("type_name") or "").strip()
            for row in evidence.get("rows", [])
            if isinstance(row, dict) and str(row.get("type_name") or "").strip()
        )
        if str(evidence.get("type_name") or "").strip():
            preflight_type_names.add(str(evidence.get("type_name") or "").strip())
    preflight_targeted_entities: list[dict[str, Any]] = []
    preflight_live_rows: list[dict[str, Any]] = []
    preflight_value_profiles: list[dict[str, Any]] = []
    if preflight_type_names:
        discovery_started = perf_counter()
        preflight_targeted_entities = await asyncio.to_thread(
            _discover_vanna_eav_candidates,
            vanna,
            preflight_type_names,
        )
        record_stage("eav_candidate_discovery_ms", discovery_started)
        known_entity_names = _prompt_entity_type_names(prompt_entities)
        prompt_entities.extend(
            item
            for item in preflight_targeted_entities
            if str(item.get("canonical_name") or "") not in known_entity_names
        )
        preflight_type_names |= _prompt_entity_type_names(preflight_targeted_entities)
    if source is not None and _vehicle_params_table(route) and preflight_type_names:
        _emit_progress(
            progress_callback,
            "entity_inspection",
            "running",
            elapsed_ms=0,
            detail="SQL 生成前核对 EAV 字段与值分布",
            field_count=min(len(preflight_type_names), 3),
        )
        inspection_started = perf_counter()
        try:
            preflight_live_rows = await _inspect_live_eav_type_names(
                source=source,
                route=route,
                requested_names=preflight_type_names,
            )
        except Exception as exc:
            raise DatabaseKnowledgeQueryError(
                f"SQL 生成前 EAV 字段核对失败：{type(exc).__name__}: {exc}",
                error_code="eav_type_name_inspection_failed",
                stage="entity_inspection",
                recoverable=True,
                next_action="retry_once",
            ) from exc
        record_stage("eav_live_inspection_ms", inspection_started)
        preflight_live_names = {
            str(row.get("type_name") or "")
            for row in preflight_live_rows
            if str(row.get("type_name") or "")
        }
        available_preflight_profiles = preflight_type_names & preflight_live_names
        relevant_preflight_profiles = {
            name
            for name in available_preflight_profiles
            if _eav_name_relevant_to_question(name, retrieval_question)
        }
        ordered_preflight_profiles = sorted(
            relevant_preflight_profiles or available_preflight_profiles,
            key=lambda name: (
                not _eav_name_relevant_to_question(name, retrieval_question),
                name,
            ),
        )
        preflight_profile_names = set(dict.fromkeys(ordered_preflight_profiles[:3]))
        if preflight_profile_names:
            profile_started = perf_counter()
            empty_eav_contract_hash = "sha256:" + hashlib.sha256(b"{}").hexdigest()
            raw_profile_contract_hash = "sha256:" + hashlib.sha256(
                b"raw-eav-value-profile/v1"
            ).hexdigest()
            preflight_contract_hashes = {
                type_name: raw_profile_contract_hash
                for type_name in preflight_profile_names
            }
            try:
                preflight_value_profiles = await _inspect_live_eav_value_profiles(
                    source=source,
                    route=route,
                    type_names=preflight_profile_names,
                    semantic_hash=empty_eav_contract_hash,
                    semantic_contract_hashes=preflight_contract_hashes,
                    permission_epoch=permission_epoch,
                )
            except Exception as exc:
                raise DatabaseKnowledgeQueryError(
                    f"SQL 生成前 EAV 值画像失败：{type(exc).__name__}: {exc}",
                    error_code="eav_value_profile_inspection_failed",
                    stage="entity_inspection",
                    recoverable=False,
                    next_action="stop_internal_profile_exhausted",
                    field_or_concept=", ".join(sorted(preflight_profile_names)),
                    attempt=1,
                    max_attempts=1,
                ) from exc
            record_stage("eav_value_profile_ms", profile_started)
            references["eav_value_profiles"] = {
                "source": "live_database_pre_generation",
                "bounded": True,
                "revision_bound": True,
                "items": preflight_value_profiles,
            }
        _emit_progress(
            progress_callback,
            "entity_inspection",
            "completed",
            elapsed_ms=round(
                stage_timings.get("eav_live_inspection_ms", 0.0)
                + stage_timings.get("eav_value_profile_ms", 0.0),
                2,
            ),
            detail=_profile_progress_detail(preflight_value_profiles),
            cache_hits=sum(1 for item in preflight_value_profiles if item.get("cache_hit")),
        )

    def generate_candidate_blocking() -> str:
        # Deliberately use only the original question here. Vanna's retrieval
        # must never see model prose, semantic assets, guardrails or repair text.
        reserve_llm_call("candidate_generation")
        try:
            return vanna.generate_sql(
                question=retrieval_question,
                allow_llm_to_see_data=request.allow_llm_to_see_data,
                entity_types=entity_types,
                entity_list=prompt_entities,
                eav_value_profiles=preflight_value_profiles,
                entity_top_k_per_type=max(1, int(top_k_config.get("default") or VANNA_ENTITY_TOP_K_PER_TYPE)),
                entity_top_k_by_type=top_k_config.get("by_type") or {},
            )
        except Exception as exc:
            raise DatabaseKnowledgeQueryError(
                f"候选 SQL 模型调用失败：{type(exc).__name__}: {exc}",
                error_code="sql_candidate_provider_error",
                stage="candidate_generation",
                recoverable=True,
                next_action="retry_once",
            ) from exc

    async def ensure_parseable(sql_text: str, *, stage: str) -> tuple[str, set[str]]:
        nonlocal parse_repair_used
        try:
            return sql_text, extract_eav_type_names(sql_text)
        except ParseError as exc:
            if parse_repair_used:
                raise DatabaseKnowledgeQueryError(
                    "候选 SQL 结构不合法，受限语法修复一次后仍无法解析。",
                    sql=sql_text,
                    error_code="sql_parse_repair_exhausted",
                    stage=stage,
                    recoverable=False,
                    next_action="stop",
                    attempt=1,
                    max_attempts=1,
                ) from exc
            parse_repair_used = True
            repair_started = perf_counter()
            try:
                repaired = await _await_with_progress(
                    asyncio.to_thread(
                        refine_with_budget,
                        stage="sql_parse_repair",
                        question=retrieval_question,
                        candidate_sql=sql_text,
                        route=route,
                        semantic_context=semantic_context,
                        references=references,
                        prompt_entities=prompt_entities,
                        correction_instruction=(
                            "Parser 已确认上一版 SQL 结构不合法。只修复括号、CTE、子查询别名或语法结构；"
                            "不得改变指标、筛选、时间范围、颗粒度、去重键和授权表。\n"
                            f"Parser 错误：{exc}"
                        ),
                    ),
                    callback=progress_callback,
                    stage="repair",
                    detail="正在修复 SQL 结构（1/1）",
                    cancel_callback=lambda: _cancel_vanna_provider(vanna),
                )
            finally:
                record_stage("sql_parse_repair_ms", repair_started)
            try:
                return repaired, extract_eav_type_names(repaired)
            except ParseError as repaired_exc:
                raise DatabaseKnowledgeQueryError(
                    "候选 SQL 结构不合法，受限语法修复一次后仍无法解析。",
                    sql=repaired,
                    error_code="sql_parse_repair_exhausted",
                    stage=stage,
                    recoverable=False,
                    next_action="stop",
                    attempt=1,
                    max_attempts=1,
                ) from repaired_exc

    try:
        started = perf_counter()
        candidate_sql = extract_sql(
            await _await_with_progress(
                asyncio.to_thread(generate_candidate_blocking),
                callback=progress_callback,
                stage="candidate_generation",
                cancel_callback=lambda: _cancel_vanna_provider(vanna),
            )
        )
        record_stage("sql_candidate_generation_ms", started)
        candidate_sql, candidate_type_names = await ensure_parseable(
            candidate_sql,
            stage="candidate_generation",
        )
        vanna_type_names = _prompt_entity_type_names(prompt_entities)
        technical_evidence = (
            request.technical_evidence if isinstance(request.technical_evidence, dict) else {}
        )
        parent_sql = str(technical_evidence.get("parent_sql") or "")
        parent_type_names = extract_eav_type_names(parent_sql)
        bindings = bindings_from_semantic_trace(semantic_trace, question=retrieval_question)
        if technical_evidence and parent_sql:
            # Technical regeneration starts from the registered parent, not a
            # fresh LLM interpretation of the business question.
            candidate_sql = parent_sql
            candidate_type_names = parent_type_names
        schema_receipt = (
            technical_evidence.get("schema_receipt")
            if isinstance(technical_evidence.get("schema_receipt"), dict)
            else {}
        )
        receipt_evidence = (
            schema_receipt.get("evidence")
            if isinstance(schema_receipt.get("evidence"), dict)
            else {}
        )
        discovery_receipts = [
            item
            for item in technical_evidence.get("schema_receipts", [])
            if isinstance(item, dict)
        ]
        invalid_discovery = [
            item
            for item in discovery_receipts
            if not _schema_discovery_receipt_payload_is_valid(item, route=route)
        ]
        if invalid_discovery:
            raise DatabaseKnowledgeQueryError(
                "Discovery Receipt 已过期、被篡改，或与当前数据源/完整表范围不一致。",
                sql=candidate_sql,
                error_code="schema_evidence_scope_invalid",
                stage="entity_inspection",
                recoverable=True,
                next_action="internal_profile_then_regenerate",
            )
        discovery_evidence = [dict(item.get("evidence") or {}) for item in discovery_receipts]
        if technical_evidence.get("kind") == "schema_evidence" and not _schema_receipt_payload_is_valid(
            schema_receipt,
            route=route,
            parent_sql=str(technical_evidence.get("parent_sql") or ""),
        ):
            raise DatabaseKnowledgeQueryError(
                "schema receipt 已过期、被篡改，或与父 SQL/数据源/完整表范围不一致。",
                sql=candidate_sql,
            )
        receipt_candidate_names = {
            str(item.get("type_name") or "").strip()
            for item in receipt_evidence.get("rows", [])
            if isinstance(item, dict) and str(item.get("type_name") or "").strip()
        }
        discovery_candidate_names = {
            name
            for evidence in discovery_evidence
            for name in [
                str(evidence.get("type_name") or "").strip(),
                *[
                    str(row.get("type_name") or "").strip()
                    for row in evidence.get("rows", [])
                    if isinstance(row, dict)
                ],
            ]
            if name
        }
        receipt_candidate_names |= discovery_candidate_names
        discovery_started = perf_counter()
        targeted_entities = await asyncio.to_thread(
            _discover_vanna_eav_candidates,
            vanna,
            candidate_type_names | parent_type_names,
        )
        record_stage("eav_candidate_discovery_ms", discovery_started)
        known_entity_names = _prompt_entity_type_names(prompt_entities)
        prompt_entities.extend(
            item
            for item in targeted_entities
            if str(item.get("canonical_name") or "") not in known_entity_names
        )
        discovered_type_names = _prompt_entity_type_names(targeted_entities)
        vanna_repair_pairs = _entity_authorized_replacement_pairs(
            candidate_type_names | parent_type_names,
            [*prompt_entities, *targeted_entities],
        )
        semantic_repair_pairs = {
            (original, physical)
            for original in candidate_type_names | parent_type_names
            for binding in bindings
            for physical in binding.type_names
            if original == physical
            or any(
                _eav_alias_authorizes(original, physical, alias)
                for alias in binding.aliases
            )
        }
        authorized_repair_pairs = vanna_repair_pairs | semantic_repair_pairs
        authorized_physical_names = {
            physical for _original, physical in authorized_repair_pairs
        } | {name for binding in bindings for name in binding.type_names}
        lookup_type_names = (
            candidate_type_names
            | discovered_type_names
            | receipt_candidate_names
            | authorized_physical_names
        )
        live_rows: list[dict[str, Any]] = list(preflight_live_rows)
        post_candidate_inspection_started: float | None = None
        post_candidate_profiles: list[dict[str, Any]] = []

        def start_post_candidate_inspection(field_count: int) -> None:
            nonlocal post_candidate_inspection_started
            if post_candidate_inspection_started is not None:
                return
            post_candidate_inspection_started = perf_counter()
            _emit_progress(
                progress_callback,
                "entity_inspection",
                "running",
                elapsed_ms=0,
                detail="正在补充核对候选 SQL 新引用的 EAV 字段与值分布",
                field_count=min(field_count, 3),
            )

        preflight_live_names = {
            str(row.get("type_name") or "") for row in live_rows if row.get("type_name")
        }
        missing_lookup_names = lookup_type_names - preflight_live_names
        if source is not None and _vehicle_params_table(route) and missing_lookup_names:
            start_post_candidate_inspection(len(missing_lookup_names))
            inspection_started = perf_counter()
            extra_live_rows = await _inspect_live_eav_type_names(
                source=source,
                route=route,
                requested_names=missing_lookup_names,
            )
            record_stage("eav_live_inspection_ms", inspection_started)
            live_rows = list(
                {
                    str(row.get("type_name") or ""): row
                    for row in [*live_rows, *extra_live_rows]
                    if str(row.get("type_name") or "")
                }.values()
            )
            live_entities = _live_rows_as_entities(live_rows)
            known_entity_names = _prompt_entity_type_names(prompt_entities)
            prompt_entities.extend(
                item
                for item in live_entities
                if str(item.get("canonical_name") or "") in authorized_physical_names
                and str(item.get("canonical_name") or "") not in known_entity_names
            )
        live_type_names = {
            str(row.get("type_name") or "") for row in live_rows if row.get("type_name")
        }
        ambiguous_physical_mappings = {
            original: sorted(
                {
                    physical
                    for candidate_original, physical in authorized_repair_pairs
                    if candidate_original == original and physical in live_type_names
                }
            )
            for original in candidate_type_names | parent_type_names
        }
        ambiguous_physical_mappings = {
            original: choices
            for original, choices in ambiguous_physical_mappings.items()
            if original not in live_type_names and len(choices) > 1
        }
        if ambiguous_physical_mappings:
            concept, choices = next(iter(ambiguous_physical_mappings.items()))
            raise DatabaseKnowledgeQueryError(
                f"EAV 业务概念“{concept}”存在多个实时物理候选：{', '.join(choices)}。",
                sql=candidate_sql,
                error_code="eav_mapping_ambiguous",
                stage="entity_inspection",
                recoverable=True,
                next_action="needs_user_choice",
                field_or_concept=concept,
                attempt=1,
                max_attempts=1,
            )
        live_value_profiles: list[dict[str, Any]] = list(preflight_value_profiles)
        ordered_profile_names = [
            *sorted(candidate_type_names & live_type_names),
            *sorted(parent_type_names & live_type_names),
            *sorted(receipt_candidate_names & live_type_names),
            *sorted(authorized_physical_names & live_type_names),
        ]
        profile_type_names = set(dict.fromkeys(ordered_profile_names[:3]))
        already_profiled_names = {
            str(item.get("type_name") or "") for item in live_value_profiles
        }
        missing_profile_names = profile_type_names - already_profiled_names
        if source is not None and missing_profile_names:
            start_post_candidate_inspection(len(missing_profile_names))
            profile_started = perf_counter()
            empty_eav_contract_hash = "sha256:" + hashlib.sha256(b"{}").hexdigest()
            raw_profile_contract_hash = "sha256:" + hashlib.sha256(
                b"raw-eav-value-profile/v1"
            ).hexdigest()
            eav_contract_hashes = {
                type_name: raw_profile_contract_hash
                for type_name in missing_profile_names
            }
            extra_value_profiles = await _inspect_live_eav_value_profiles(
                source=source,
                route=route,
                type_names=missing_profile_names,
                semantic_hash=empty_eav_contract_hash,
                semantic_contract_hashes=eav_contract_hashes,
                permission_epoch=permission_epoch,
            )
            record_stage("eav_value_profile_ms", profile_started)
            live_value_profiles.extend(extra_value_profiles)
            post_candidate_profiles.extend(extra_value_profiles)
            if live_value_profiles:
                references["eav_value_profiles"] = {
                    "source": "live_database",
                    "bounded": True,
                    "revision_bound": True,
                    "items": live_value_profiles,
                }
        strict_runtime_profile_required = _question_requires_strict_eav_profile(
            retrieval_question
        ) or _sql_requires_strict_eav_profile(candidate_sql)
        required_profile_names: set[str] = set()
        if strict_runtime_profile_required:
            required_profile_names |= profile_type_names
        profiled_names = {str(item.get("type_name") or "") for item in live_value_profiles}
        missing_required_profiles = required_profile_names - profiled_names
        if missing_required_profiles:
            raise DatabaseKnowledgeQueryError(
                "当前轮已尝试精确画像和轻量降级画像，但仍无法取得实时 EAV 值分布："
                + ", ".join(sorted(missing_required_profiles)),
                error_code="eav_value_profile_inspection_failed",
                stage="entity_inspection",
                recoverable=False,
                next_action="stop_internal_profile_exhausted",
                field_or_concept=", ".join(sorted(missing_required_profiles)),
                attempt=1,
                max_attempts=1,
            )
        if profile_type_names and not live_value_profiles:
            references["eav_value_profile_warning"] = {
                "source": "runtime_profile_inspection",
                "status": "unavailable_non_blocking",
                "type_names": sorted(profile_type_names),
                "reason": "当前问题不要求缺失画像时阻断生成",
            }
        profile_evidence_refs: list[str] = []
        for profile in live_value_profiles:
            evidence_ref = "profile-evidence-" + hashlib.sha256(
                (
                    f"{profile.get('source_revision')}:{profile.get('value_profile_hash')}:"
                    f"{request.question}:{time.time_ns()}"
                ).encode()
            ).hexdigest()[:20]
            profile["evidence_ref"] = evidence_ref
            profile_evidence_refs.append(evidence_ref)
        semantic_stale_candidates: list[dict[str, Any]] = []
        catalog_snapshot_revision = "sha256:" + hashlib.sha256(
            json.dumps(
                {
                    "source": route.database_source_id,
                    "table": _vehicle_params_table(route),
                    "rows": sorted(live_rows, key=lambda item: str(item.get("type_name") or "")),
                },
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        for binding in bindings:
            missing_bound_names = sorted(set(binding.type_names) - live_type_names)
            if not missing_bound_names:
                continue
            reminder = eav_profile_catalog.mark_semantic_stale_candidate(
                source_id=str(route.database_source_id or ""),
                table=_normalize_table_scope(str(_vehicle_params_table(route) or "vehicle_params").replace('"', "")),
                semantic_asset_id=binding.concept,
                bound_type_names=list(binding.type_names),
                missing_type_names=missing_bound_names,
                source_revision=catalog_snapshot_revision,
            )
            semantic_stale_candidates.append(
                {
                    "semantic_asset_id": binding.concept,
                    "missing_type_names": missing_bound_names,
                    "maintenance_reminder_id": reminder["id"],
                }
            )
        if post_candidate_inspection_started is not None:
            _emit_progress(
                progress_callback,
                "entity_inspection",
                "completed",
                elapsed_ms=round((perf_counter() - post_candidate_inspection_started) * 1000, 2),
                detail=(
                    _profile_progress_detail(post_candidate_profiles)
                    if post_candidate_profiles
                    else "候选 SQL 新引用的 EAV 字段已核对"
                ),
                evidence_refs=profile_evidence_refs,
                cache_hits=sum(1 for item in post_candidate_profiles if item.get("cache_hit")),
            )
        if discovery_evidence:
            references["schema_discovery"] = {
                "source": "server_receipt_revalidated_by_live_catalog",
                "items": [
                    evidence
                    for evidence in discovery_evidence
                    if not evidence.get("type_name")
                    or str(evidence.get("type_name")) in live_type_names
                ],
            }
        session_plans = select_derivable_query_plans(
            [
                dict(item)
                for item in technical_evidence.get("verified_plans", [])
                if isinstance(item, dict)
            ],
            question=retrieval_question,
            database_source_id=str(route.database_source_id or ""),
            allowed_tables=list(route.table_names),
            permission_epoch=permission_epoch,
        )
        current_profile_revisions = {
            str(item.get("type_name") or ""): str(item.get("source_revision") or "")
            for item in live_value_profiles
            if item.get("type_name") and item.get("source_revision")
        }
        verified_session_plans: list[dict[str, Any]] = []
        current_asset_ids = {
            str(item.get("id") or "")
            for item in semantic_trace.get("matched", [])
            if isinstance(item, dict) and item.get("id")
        }
        for plan in session_plans:
            plan_profile_revisions = {
                str(binding.get("type_name") or ""): str(binding.get("source_revision") or "")
                for binding in plan.get("profile_bindings", [])
                if isinstance(binding, dict) and binding.get("type_name") and binding.get("source_revision")
            }
            plan_eav_type_names = {str(item) for item in plan.get("eav_type_names") or []}
            bindings_match = (
                (not plan_eav_type_names or plan_eav_type_names.issubset(plan_profile_revisions))
                and all(
                    current_profile_revisions.get(type_name) == revision
                    for type_name, revision in plan_profile_revisions.items()
                )
            )
            if not bindings_match:
                continue
            previous_asset_ids = {str(item) for item in plan.get("semantic_asset_ids") or []}
            derivation_kind = (
                "add_dimensions"
                if previous_asset_ids and previous_asset_ids.issubset(current_asset_ids)
                else "stable_predicate_reference"
            )
            verified_session_plans.append({**plan, "derivation_kind": derivation_kind})
        if verified_session_plans:
            references["verified_query_plans"] = {
                "source": "same_session_validated_plan_ledger",
                "revision_bound": True,
                "items": verified_session_plans,
            }
        # Direct unit tests may exercise the generator without a live source.
        # Production callers always pass ``source`` and therefore use the live
        # catalog as physical truth rather than the Vanna snapshot.
        evidence_type_names = live_type_names if source is not None else vanna_type_names
        def receipt_name_is_relevant(name: str) -> bool:
            return any((parent, name) in authorized_repair_pairs for parent in parent_type_names)

        authorized_receipt_names = {
            name
            for name in receipt_candidate_names & live_type_names
            if receipt_name_is_relevant(name)
        }
        if technical_evidence.get("kind") == "schema_evidence" and not authorized_receipt_names:
            raise DatabaseKnowledgeQueryError(
                "schema receipt 未提供与父 SQL 问题相关且经实时库精确复核的 EAV 配置名，已禁止自动修复。",
                sql=candidate_sql,
            )
        trusted_receipt_evidence = {
            **{key: value for key, value in receipt_evidence.items() if key != "rows"},
            "rows": [
                row
                for row in live_rows
                if str(row.get("type_name") or "") in authorized_receipt_names
            ],
        }
        candidate_check = check_eav_evidence(
            candidate_sql,
            live_type_names=evidence_type_names,
            bindings=bindings,
        )
        started = perf_counter()
        sql = await _await_with_progress(
            asyncio.to_thread(
                refine_with_budget,
                stage="semantic_refinement",
                question=retrieval_question,
                candidate_sql=candidate_sql,
                route=route,
                semantic_context=semantic_context,
                references=references,
                prompt_entities=prompt_entities,
            ),
            callback=progress_callback,
            stage="semantic_refinement",
            cancel_callback=lambda: _cancel_vanna_provider(vanna),
        )
        record_stage("sql_semantic_refinement_ms", started)
        baseline_sql = sql
        baseline_sql, baseline_type_names = await ensure_parseable(
            baseline_sql,
            stage="semantic_refinement",
        )
        sql = baseline_sql
        missing_inspection_names = baseline_type_names - live_type_names
        if source is not None and _vehicle_params_table(route) and missing_inspection_names:
            discovery_started = perf_counter()
            extra_entities = await asyncio.to_thread(
                _discover_vanna_eav_candidates,
                vanna,
                missing_inspection_names,
            )
            record_stage("eav_candidate_discovery_ms", discovery_started)
            extra_candidate_names = _prompt_entity_type_names(extra_entities)
            extra_pairs = _entity_authorized_replacement_pairs(
                missing_inspection_names,
                extra_entities,
            )
            authorized_repair_pairs.update(extra_pairs)
            authorized_physical_names.update(physical for _old, physical in extra_pairs)
            inspection_started = perf_counter()
            extra_rows = await _inspect_live_eav_type_names(
                source=source,
                route=route,
                requested_names=missing_inspection_names | extra_candidate_names,
            )
            stage_timings["eav_live_inspection_ms"] = round(
                stage_timings.get("eav_live_inspection_ms", 0.0)
                + (perf_counter() - inspection_started) * 1000,
                2,
            )
            by_name = {
                str(row.get("type_name") or ""): row for row in [*live_rows, *extra_rows]
                if str(row.get("type_name") or "")
            }
            live_rows = list(by_name.values())
            known_entity_names = _prompt_entity_type_names(prompt_entities)
            prompt_entities.extend(
                item for item in [*extra_entities, *_live_rows_as_entities(extra_rows)]
                if str(item.get("canonical_name") or "") in authorized_physical_names
                and str(item.get("canonical_name") or "") not in known_entity_names
            )
            live_type_names = set(by_name)
            evidence_type_names = live_type_names
        baseline_check = check_eav_evidence(
            baseline_sql,
            live_type_names=evidence_type_names,
            bindings=bindings,
        )
        repair_groups = sorted(
            pair
            for pair in authorized_repair_pairs
            if pair[0] != pair[1] and pair[1] in live_type_names
        )
        evidence_correction = ""
        complete_value_profiles = [
            profile
            for profile in _complete_eav_value_profiles(live_value_profiles)
            if str(profile.get("type_name") or "") in baseline_type_names
        ]
        if complete_value_profiles and _sql_requires_strict_eav_profile(baseline_sql):
            evidence_correction = (
                "上一版 SQL 没有直接使用已经完整探测到的 EAV 原始值，而是对 type_value 使用了 "
                "LIKE、正则、数值转换或字符串解析。值画像已经覆盖该字段的全部 distinct 值，"
                "因此不得匹配画像外的假想值。请根据原始业务问题自行选择一个或多个实际观测值，"
                "并仅用精确等值或 IN 写入 type_value 谓词；不要改变其余业务目标、指标、筛选、"
                "时间范围、聚合粒度、去重键和授权表。完整原始值画像：\n"
                + json.dumps(complete_value_profiles, ensure_ascii=False, indent=2)
            )
        if (baseline_type_names or baseline_check.unprovable_predicates) and not baseline_check.passed:
            type_name_correction = (
                "上一版语义校正 SQL 未通过 EAV 物理证据预检。保持业务问题、指标、筛选、时间范围、"
                "聚合粒度、去重键和授权表完全不变，只能依据下列服务器证据修复 type_name 映射。\n"
                f"不受实时数据库支持的 type_name：{sorted(baseline_check.unsupported)}\n"
                f"无法静态证明的 type_name 谓词：{list(baseline_check.unprovable_predicates)}\n"
                "必须从实时数据库候选中自行选择精确 type_name，并使用可静态证明的等值或 IN 谓词；"
                "不得继续对 type_name 使用 LIKE、正则或动态表达式。\n"
                "实时数据库候选（仅证明存在，不证明语义等价）：\n"
                + json.dumps(live_rows, ensure_ascii=False, indent=2)
                + "\n显式 EAV 等价与合并契约（只有这里声明的多字段才允许合并）：\n"
                + bindings_prompt(bindings)
            )
            evidence_correction = "\n\n".join(
                item for item in (evidence_correction, type_name_correction) if item
            )
        # Cache scope, discovery receipts and verified plans are context, not a
        # request to spend the one bounded repair call.  Only an explicitly
        # registered repair kind may open the technical-correction branch.
        has_registered_repair = technical_evidence.get("kind") in {
            "schema_evidence",
            "observed_sql_failure",
        }
        if has_registered_repair:
            typed_correction = (
                "服务器已登记一项 SQL 技术修复。原始业务问题和语义契约不可改变。"
                "修复只能处理 SQL 实现或下列 schema receipt 中的 EAV 物理映射；"
                "不得改变指标、分子分母、筛选、时间范围、粒度、去重键或授权表。\n"
                + json.dumps(
                    {
                        "kind": technical_evidence.get("kind"),
                        "observed_problem_category": technical_evidence.get(
                            "observed_problem_category"
                        ),
                        "schema_evidence": trusted_receipt_evidence,
                    },
                    ensure_ascii=False,
                    indent=2,
                )
            )
            evidence_correction = "\n\n".join(
                item for item in (evidence_correction, typed_correction) if item
            )
        if evidence_correction:
            repair_started = perf_counter()
            sql = await _await_with_progress(
                asyncio.to_thread(
                    refine_with_budget,
                    stage="eav_evidence_repair",
                    question=retrieval_question,
                    candidate_sql=baseline_sql,
                    route=route,
                    semantic_context=semantic_context,
                    references=references,
                    prompt_entities=prompt_entities,
                    correction_instruction=evidence_correction,
                ),
                callback=progress_callback,
                stage="eav_evidence_repair",
                detail="正在修复 EAV 物理映射（1/1）",
                cancel_callback=lambda: _cancel_vanna_provider(vanna),
            )
            record_stage("eav_evidence_repair_ms", repair_started)
            requires_exact_mapping_invariant = (
                technical_evidence.get("kind") == "schema_evidence"
                or not baseline_check.passed
            )
            if baseline_check.unprovable_predicates:
                repair_preserved_structure = eav_type_name_predicate_fingerprint(
                    baseline_sql
                ) == eav_type_name_predicate_fingerprint(sql)
            else:
                repair_preserved_structure = eav_mapping_fingerprint(
                    baseline_sql,
                    bindings=bindings,
                    replacement_groups=repair_groups,
                ) == eav_mapping_fingerprint(
                    sql,
                    bindings=bindings,
                    replacement_groups=repair_groups,
                )
            if requires_exact_mapping_invariant and not repair_preserved_structure:
                raise DatabaseKnowledgeQueryError(
                    "EAV 技术修复改变了 type_name 映射之外的 SQL 结构，已禁止执行。",
                    sql=sql,
                )
        stage_timings["sql_generation_ms"] = round(
            stage_timings.get("sql_candidate_generation_ms", 0.0)
            + stage_timings.get("sql_semantic_refinement_ms", 0.0)
            + stage_timings.get("eav_evidence_repair_ms", 0.0),
            2,
        )
    except DatabaseKnowledgeQueryError:
        raise
    except ParseError as exc:
        raise DatabaseKnowledgeQueryError(
            f"SQL 解析失败：{exc}",
            error_code="sql_candidate_parse_error",
            stage="deterministic_validation",
            recoverable=False,
            next_action="stop",
        ) from exc
    except Exception as exc:
        raise DatabaseKnowledgeQueryError(
            f"SQL 生成本地阶段失败：{type(exc).__name__}: {exc}",
            error_code="sql_generation_stage_error",
            stage="entity_or_semantic_validation",
            recoverable=False,
            next_action="stop",
        ) from exc

    generation_trace: dict[str, Any] = {
        "retrieval_question": retrieval_question,
        "candidate_sql": candidate_sql,
        "entity_evidence_count": len(prompt_entities),
        "entity_authority": "database_entity_evidence_for_physical_facts",
        "semantic_refinement_applied": True,
        "guardrail_rewrites": 0,
        "technical_repairs": 0,
        "llm_call_budget": {"used": llm_call_count, "max": max_llm_calls},
        "profile_evidence_refs": profile_evidence_refs,
        "semantic_asset_stale_candidates": semantic_stale_candidates,
        "permission_epoch": permission_epoch,
        "eav_evidence": {
            "candidate_type_names": sorted(candidate_type_names),
            "vanna_type_names": sorted(vanna_type_names),
            "live_type_names": sorted(live_type_names),
            "live_inspection_count": len(live_rows),
            "bindings": [item.concept for item in bindings],
        },
    }

    if candidate_type_names or extract_eav_type_names(sql):
        final_check = check_eav_evidence(
            sql,
            live_type_names=evidence_type_names,
            bindings=bindings,
        )
        if not final_check.passed:
            details = []
            if final_check.unsupported:
                details.append("无实时数据库证据：" + ", ".join(sorted(final_check.unsupported)))
            if final_check.incomplete_bindings:
                details.extend(
                    f"概念 {item.concept} 必须完整使用：{', '.join(item.type_names)}"
                    for item in final_check.incomplete_bindings
                )
            if final_check.invalid_binding_resolutions:
                details.extend(
                    f"概念 {item.concept} 必须按 {item.value_resolution} 合并，不能求和或任意择值"
                    for item in final_check.invalid_binding_resolutions
                )
            if final_check.unprovable_predicates:
                details.append(
                    "存在无法静态证明的 type_name 谓词："
                    + ", ".join(final_check.unprovable_predicates)
                )
            raise DatabaseKnowledgeQueryError(
                "EAV 物理证据校验失败，已禁止执行：" + "；".join(details),
                sql=sql,
                error_code=(
                    "eav_type_name_not_found"
                    if final_check.unsupported
                    else "eav_type_name_unprovable"
                    if final_check.unprovable_predicates
                    else "semantic_contract_conflict"
                ),
                stage="entity_inspection",
                recoverable=False,
                next_action="stop",
                field_or_concept=", ".join(sorted(final_check.unsupported)),
            )
        generation_trace["eav_evidence"].update(
            {
                "final_type_names": sorted(final_check.used_type_names),
                "unsupported": sorted(final_check.unsupported),
                "complete": final_check.passed,
                "automatic_repair": bool(evidence_correction or not candidate_check.passed),
            }
        )
        references["eav_live_inspection"] = {
            "source": "live_database" if source is not None else "vanna_test_fallback",
            "rows": live_rows,
            "candidate_type_names": sorted(candidate_type_names),
            "final_type_names": sorted(final_check.used_type_names),
        }

    deterministic_started = perf_counter()
    _emit_progress(
        progress_callback,
        "deterministic_validation",
        "running",
        elapsed_ms=0,
        detail="正在执行只读与语义预检",
    )
    guardrail_conflicts = _detect_sql_guardrail_conflicts(
        sql,
        source_name=route.source_name,
        route=route,
        semantic_trace=semantic_trace,
        question=request.question,
    )
    warn_conflicts = [conflict for conflict in guardrail_conflicts if conflict.action == "warn"]
    blocking_conflicts = [conflict for conflict in guardrail_conflicts if conflict.action in {"rewrite", "block"}]
    if warn_conflicts:
        guardrail_note = "SQL guardrail warning：" + "；".join(_guardrail_messages(warn_conflicts))
    if any(conflict.action == "block" for conflict in blocking_conflicts):
        raise DatabaseKnowledgeQueryError(
            "生成 SQL 命中 SQL guardrail 阻断规则，已拦截执行："
            + "；".join(_guardrail_messages(blocking_conflicts)),
            sql=sql,
        )
    if blocking_conflicts:
        semantic_conflicts = _guardrail_messages(blocking_conflicts)
        logger.warning(
            "[nl2sql-service] sql_guardrail_conflict_retry source=%s tables=%s conflict_count=%s sql_sha256=%s",
            route.source_name,
            ",".join(route.table_names),
            len(semantic_conflicts),
            hashlib.sha256(sql.encode("utf-8")).hexdigest()[:20],
        )
        started = perf_counter()
        sql = await _await_with_progress(
            asyncio.to_thread(
                refine_with_budget,
                stage="semantic_guardrail_repair",
                question=retrieval_question,
                candidate_sql=sql,
                route=route,
                semantic_context=semantic_context,
                references=references,
                prompt_entities=prompt_entities,
                correction_instruction=(
                    "上一版 SQL 被确定性 guardrail 拦截。必须修复以下冲突，同时保持用户业务目标不变：\n"
                    + "\n".join(f"- {item}" for item in semantic_conflicts)
                    + "\n\n"
                    + _CONFIG_RATE_SQL_TEMPLATE
                ),
            ),
            callback=progress_callback,
            stage="repair",
            detail="正在修复已识别的语义冲突（1/1）",
            cancel_callback=lambda: _cancel_vanna_provider(vanna),
        )
        record_stage("sql_regeneration_ms", started)
        generation_trace["guardrail_rewrites"] = 1
        rewritten_conflicts = _detect_sql_guardrail_conflicts(
            sql,
            source_name=route.source_name,
            route=route,
            semantic_trace=semantic_trace,
            question=request.question,
        )
        rewritten_blocking = [item for item in rewritten_conflicts if item.action in {"rewrite", "block"}]
        if rewritten_blocking:
            raise DatabaseKnowledgeQueryError(
                "生成 SQL 与 SQL guardrail 规则冲突，已拦截执行："
                + "；".join(_guardrail_messages(rewritten_blocking)),
                sql=sql,
            )
        rewrite_note = "SQL guardrail 已拦截首版 SQL 并重写一次：" + "；".join(semantic_conflicts)
        guardrail_note = f"{guardrail_note}；{rewrite_note}" if guardrail_note else rewrite_note

    technical_repairs: list[str] = []
    technical_repair_ms = 0.0
    for repair_attempt in range(2):
        try:
            sql = validate_readonly_sql(sql, allowed_tables=route.table_names)
            post_conflicts = _detect_sql_guardrail_conflicts(
                sql,
                source_name=route.source_name,
                route=route,
                semantic_trace=semantic_trace,
                question=request.question,
            )
            post_blocking = [item for item in post_conflicts if item.action in {"rewrite", "block"}]
            if post_blocking:
                raise SqlRunnerError(
                    "SQL 技术修复后仍与 guardrail 冲突：" + "；".join(_guardrail_messages(post_blocking)),
                    sql=sql,
                )
            break
        except SqlRunnerError as exc:
            if getattr(exc, "error_code", "") == "sql_builtin_registry_miss":
                raise DatabaseKnowledgeQueryError(
                    str(exc),
                    sql=sql,
                    error_code="sql_builtin_registry_miss",
                    stage="deterministic_validation",
                    recoverable=False,
                    next_action="stop",
                ) from exc
            if repair_attempt >= 1:
                raise DatabaseKnowledgeQueryError(
                    "SQL 技术预检失败，生成器已自动修复 1 次仍未得到完整的只读 SQL："
                    f"{exc}",
                    sql=sql,
                    error_code="sql_repair_exhausted",
                    stage="deterministic_validation",
                    recoverable=False,
                    next_action="stop",
                    attempt=1,
                    max_attempts=1,
                ) from exc
            technical_repairs.append(str(exc))
            repair_started = perf_counter()
            sql = await _await_with_progress(
                asyncio.to_thread(
                    refine_with_budget,
                    stage="deterministic_repair",
                    question=retrieval_question,
                    candidate_sql=sql,
                    route=route,
                    semantic_context=semantic_context,
                    references=references,
                    prompt_entities=prompt_entities,
                    correction_instruction=(
                        "上一版 SQL 未通过确定性技术预检。保持指标口径、维度、筛选条件和授权表范围不变，"
                        "只修复 SQL 语法与结构。\n"
                        f"预检错误：{exc}"
                    ),
                ),
                callback=progress_callback,
                stage="repair",
                detail="正在修复已识别的技术错误（1/1）",
                cancel_callback=lambda: _cancel_vanna_provider(vanna),
            )
            technical_repair_ms += (perf_counter() - repair_started) * 1000
    if technical_repairs:
        stage_timings["sql_technical_repair_ms"] = round(technical_repair_ms, 2)
        generation_trace["technical_repairs"] = len(technical_repairs)
        repair_note = (
            f"SQL 技术预检自动修复 {len(technical_repairs)} 次（未改变业务口径）："
            + "；".join(technical_repairs)
        )
        guardrail_note = f"{guardrail_note}；{repair_note}" if guardrail_note else repair_note

    # True terminal guardrail: every LLM rewrite above (semantic correction,
    # deterministic guardrail regeneration and syntax repair) can change SQL.
    # Re-check physical EAV facts and resolution policy only after all of them.
    terminal_type_names = extract_eav_type_names(sql)
    terminal_missing = terminal_type_names - live_type_names
    if source is not None and _vehicle_params_table(route) and terminal_missing:
        inspection_started = perf_counter()
        terminal_rows = await _inspect_live_eav_type_names(
            source=source,
            route=route,
            requested_names=terminal_missing,
        )
        record_stage("eav_live_inspection_ms", inspection_started)
        by_name = {
            str(row.get("type_name") or ""): row
            for row in [*live_rows, *terminal_rows]
            if str(row.get("type_name") or "")
        }
        live_rows = list(by_name.values())
        live_type_names = set(by_name)
        evidence_type_names = live_type_names
    terminal_check = check_eav_evidence(
        sql,
        live_type_names=evidence_type_names,
        bindings=bindings,
    )
    if not terminal_check.passed:
        details: list[str] = []
        if terminal_check.unsupported:
            details.append("无实时数据库精确证据：" + ", ".join(sorted(terminal_check.unsupported)))
        details.extend(
            f"概念 {item.concept} 缺少必需物理字段：{', '.join(item.type_names)}"
            for item in terminal_check.incomplete_bindings
        )
        details.extend(
            f"概念 {item.concept} 未按 {item.value_resolution} 解析"
            for item in terminal_check.invalid_binding_resolutions
        )
        if terminal_check.unprovable_predicates:
            details.append(
                "动态/变形 type_name 谓词不可证明："
                + ", ".join(terminal_check.unprovable_predicates)
            )
        raise DatabaseKnowledgeQueryError(
            "最终 SQL 的 EAV 证据护栏失败，已禁止执行：" + "；".join(details),
            sql=sql,
            error_code=(
                "eav_type_name_not_found"
                if terminal_check.unsupported
                else "eav_type_name_unprovable"
                if terminal_check.unprovable_predicates
                else "semantic_contract_conflict"
            ),
            stage="entity_inspection",
            recoverable=False,
            next_action="stop",
            field_or_concept=", ".join(sorted(terminal_check.unsupported)),
        )
    if technical_evidence.get("kind") == "schema_evidence":
        parent_sql = str(technical_evidence.get("parent_sql") or "")
        if not parent_sql or eav_mapping_fingerprint(
            parent_sql,
            bindings=bindings,
            replacement_groups=repair_groups,
        ) != eav_mapping_fingerprint(
            sql,
            bindings=bindings,
            replacement_groups=repair_groups,
        ):
            raise DatabaseKnowledgeQueryError(
                "schema receipt 技术修复改变了被授权 EAV 映射之外的 SQL 结构，已禁止执行。",
                sql=sql,
            )
    elif technical_evidence.get("kind") == "observed_sql_failure":
        parent_sql = str(technical_evidence.get("parent_sql") or "")
        if not parent_sql or sql_business_fingerprint(parent_sql) != sql_business_fingerprint(sql):
            raise DatabaseKnowledgeQueryError(
                "自动 SQL 技术修复改变了父 SQL 的指标、筛选、粒度、去重或排序不变量，已禁止执行。",
                sql=sql,
            )
    generation_trace["eav_evidence"].update(
        {
            "final_type_names": sorted(terminal_check.used_type_names),
            "live_type_names": sorted(live_type_names),
            "live_inspection_count": len(live_rows),
            "unsupported": sorted(terminal_check.unsupported),
            "complete": terminal_check.passed,
        }
    )
    references["eav_live_inspection"] = {
        "source": "live_database" if source is not None else "vanna_test_fallback",
        "rows": live_rows,
        "candidate_type_names": sorted(candidate_type_names),
        "final_type_names": sorted(terminal_check.used_type_names),
    }
    if references.get("eav_value_profiles"):
        generation_trace["eav_value_profiles"] = references["eav_value_profiles"]
    generation_trace["llm_call_budget"] = {"used": llm_call_count, "max": max_llm_calls}
    generation_trace["final_sql"] = sql
    generation_trace["applied_rules"] = collect_applied_semantic_rules(sql, semantic_trace)
    _emit_progress(
        progress_callback,
        "deterministic_validation",
        "completed",
        elapsed_ms=round((perf_counter() - deterministic_started) * 1000, 2),
        detail="只读 SQL、表范围与语义 Guardrail 已通过",
    )
    return sql, references, guardrail_note, generation_trace


async def query_database_knowledge(
    session: AsyncSession,
    request: DatabaseQueryRequest,
    *,
    session_id: str = "",
    tool_call_id: str = "",
    source_query_id: str = "",
    source_run_id: str = "",
) -> DatabaseQueryResult:
    """Run the shared grounded SQL pipeline and execute its final SQL."""

    stage_timings: dict[str, float] = {}
    total_started = perf_counter()

    def record_stage(name: str, started: float) -> None:
        stage_timings[name] = round((perf_counter() - started) * 1000, 2)

    try:
        stage_started = perf_counter()
        route = await route_database_tables(session, request)
        record_stage("router_ms", stage_started)

        stage_started = perf_counter()
        compiled_semantic_context = await asyncio.to_thread(
            _compile_request_semantic_context,
            request,
        )
        semantic_trace = compiled_semantic_context.to_trace()
        semantic_trace.update(
            build_execution_binding_metadata(
                compiled_semantic_context,
                adapter="sql",
                source_refs=[
                    f"{route.database_source_id}.{table_name}"
                    for table_name in route.table_names
                ],
            )
        )
        semantic_context = render_sql_semantic_context(compiled_semantic_context)
        record_stage("semantic_assets_ms", stage_started)

        stage_started = perf_counter()
        source = await get_database_source(session, route.database_source_id)
        vanna = build_vanna_client_from_app_config()
        record_stage("setup_ms", stage_started)

        sql, references, guardrail_note, generation = await _generate_grounded_sql(
            request=request,
            route=route,
            semantic_context=semantic_context,
            semantic_trace=semantic_trace,
            vanna=vanna,
            stage_timings=stage_timings,
            source=source,
        )
        logger.info(
            "[nl2sql-service] sql_generated source=%s tables=%s sql_sha256=%s",
            route.source_name,
            ",".join(route.table_names),
            hashlib.sha256(sql.encode("utf-8")).hexdigest()[:20],
        )
        stage_started = perf_counter()
        try:
            execution = await run_readonly_sql(
                source,
                sql,
                allowed_tables=route.table_names,
                limit=request.limit,
            )
            if guardrail_note:
                execution.llm_guardrail = guardrail_note
            record_stage("sql_execution_ms", stage_started)
        except Exception:
            record_stage("sql_execution_ms", stage_started)
            stage_timings["total_ms"] = round((perf_counter() - total_started) * 1000, 2)
            logger.warning(
                "[nl2sql-service] sql_execution_failed source=%s tables=%s timings=%s sql_sha256=%s",
                route.source_name,
                ",".join(route.table_names),
                stage_timings,
                hashlib.sha256(sql.encode("utf-8")).hexdigest()[:20],
            )
            raise
        stage_started = perf_counter()
        if await attach_persisted_query_result(
            session,
            execution,
            question=request.question,
            sql=sql,
            session_id=session_id,
            tool_call_id=tool_call_id,
            source_query_id=source_query_id,
            source_run_id=source_run_id,
        ):
            record_stage("result_store_ms", stage_started)
        logger.info(
            "[nl2sql-service] sql_executed source=%s tables=%s rows=%s limited=%s",
            route.source_name,
            ",".join(route.table_names),
            execution.row_count,
            execution.limited,
        )
        stage_timings["total_ms"] = round((perf_counter() - total_started) * 1000, 2)
        return DatabaseQueryResult(
            question=request.question,
            sql=sql,
            source={
                "id": route.database_source_id,
                "name": route.source_name,
                "database": route.database,
                "dialect": route.dialect,
            },
            route=route,
            execution=execution,
            references=references,
            semantic_assets=semantic_trace,
            stage_timings=stage_timings,
            generation=generation,
        )
    except DatabaseKnowledgeQueryError:
        raise
    except (TableRouterError, SqlRunnerError) as exc:
        raise DatabaseKnowledgeQueryError(str(exc), sql=getattr(exc, "sql", None)) from exc
    except Exception as exc:
        raise DatabaseKnowledgeQueryError(f"{type(exc).__name__}: {exc}") from exc


async def generate_database_sql(
    session: AsyncSession,
    request: DatabaseQueryRequest,
    *,
    progress_callback: ProgressCallback | None = None,
) -> DatabaseSqlGenerationResult:
    """Generate but do not execute the shared grounded final SQL."""

    stage_timings: dict[str, float] = {}
    total_started = perf_counter()

    def record_stage(name: str, started: float) -> None:
        stage_timings[name] = round((perf_counter() - started) * 1000, 2)

    route: Any | None = None
    try:
        stage_started = perf_counter()
        _emit_progress(progress_callback, "routing", "running", elapsed_ms=0)
        route = await route_database_tables(session, request)
        record_stage("router_ms", stage_started)
        _emit_progress(
            progress_callback,
            "routing",
            "completed",
            elapsed_ms=stage_timings["router_ms"],
        )

        stage_started = perf_counter()
        compiled_semantic_context = await asyncio.to_thread(
            _compile_request_semantic_context,
            request,
        )
        semantic_trace = compiled_semantic_context.to_trace()
        semantic_trace.update(
            build_execution_binding_metadata(
                compiled_semantic_context,
                adapter="sql",
                source_refs=[
                    f"{route.database_source_id}.{table_name}"
                    for table_name in route.table_names
                ],
            )
        )
        semantic_context = render_sql_semantic_context(compiled_semantic_context)
        record_stage("semantic_assets_ms", stage_started)

        stage_started = perf_counter()
        vanna = build_vanna_client_from_app_config()
        source = await get_database_source(session, route.database_source_id)
        record_stage("setup_ms", stage_started)

        sql, references, guardrail_note, generation = await _generate_grounded_sql(
            request=request,
            route=route,
            semantic_context=semantic_context,
            semantic_trace=semantic_trace,
            vanna=vanna,
            stage_timings=stage_timings,
            source=source,
            progress_callback=progress_callback,
        )

        logger.info(
            "[nl2sql-service] sql_generated_only source=%s tables=%s sql_sha256=%s",
            route.source_name,
            ",".join(route.table_names),
            hashlib.sha256(sql.encode("utf-8")).hexdigest()[:20],
        )
        stage_timings["total_ms"] = round((perf_counter() - total_started) * 1000, 2)
        _emit_progress(
            progress_callback,
            "completed",
            "completed",
            elapsed_ms=stage_timings["total_ms"],
            stage_timings=stage_timings,
        )
        return DatabaseSqlGenerationResult(
            question=request.question,
            sql=sql,
            source={
                "id": route.database_source_id,
                "name": route.source_name,
                "database": route.database,
                "dialect": route.dialect,
            },
            route=route,
            references=references,
            semantic_assets=semantic_trace,
            stage_timings=stage_timings,
            guardrail_note=guardrail_note,
            generation=generation,
        )
    except DatabaseKnowledgeQueryError as exc:
        if route is not None:
            exc.source_id = exc.source_id or str(route.database_source_id or "")
            exc.table_scope = exc.table_scope or list(route.table_names)
        raise
    except (TableRouterError, SqlRunnerError) as exc:
        raise DatabaseKnowledgeQueryError(str(exc), sql=getattr(exc, "sql", None)) from exc
    except Exception as exc:
        raise DatabaseKnowledgeQueryError(f"{type(exc).__name__}: {exc}") from exc
