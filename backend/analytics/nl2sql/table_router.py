"""Deterministic table routing for database-backed NL2SQL.

The router narrows the database/table scope before Vanna generates SQL. It is
not a SQL generator and it must not let Vanna freely guess across every table in
every configured database.
"""

from __future__ import annotations

import logging
import re
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from analytics.nl2sql.schemas import DatabaseQueryRequest, TableCandidate, TableRoute
from knowledge.database_sources import (
    KnowledgeDatabaseSourceError,
    database_source_selected_tables,
    database_source_url,
    get_database_source,
    list_database_sources,
)
from knowledge.models import KnowledgeDatabaseSource


class TableRouterError(RuntimeError):
    """Raised when a database question cannot be routed safely."""


logger = logging.getLogger(__name__)

_TOKEN_RE = re.compile(r"[0-9a-zA-Z_\u4e00-\u9fff]+")


def _source_value(source: KnowledgeDatabaseSource | dict[str, Any], key: str, default: Any = None) -> Any:
    if isinstance(source, dict):
        return source.get(key, default)
    return getattr(source, key, default)


def _normalize_table_name(name: str) -> str:
    value = str(name or "").strip().strip('"')
    if not value:
        return ""
    parts = [part.strip().strip('"') for part in value.split(".") if part.strip()]
    if len(parts) == 1:
        return parts[0]
    return ".".join(parts[-2:])


def _table_aliases(name: str) -> set[str]:
    normalized = _normalize_table_name(name)
    if not normalized:
        return set()
    if "." in normalized:
        schema, table = normalized.split(".", 1)
        return {normalized, table, f'"{schema}"."{table}"', f"{schema}.{table}"}
    return {normalized, f"public.{normalized}", f'"public"."{normalized}"'}


def _tokens(value: str | None) -> set[str]:
    return {token.lower() for token in _TOKEN_RE.findall(str(value or "")) if token.strip()}


def _match_requested_tables(requested: list[str], available: list[str]) -> list[str]:
    if not requested:
        return []
    alias_to_available: dict[str, str] = {}
    for table in available:
        for alias in _table_aliases(table):
            alias_to_available[alias.lower()] = table
    matched: list[str] = []
    missing: list[str] = []
    for raw in requested:
        key = _normalize_table_name(raw).lower()
        table = alias_to_available.get(key)
        if table and table not in matched:
            matched.append(table)
        else:
            missing.append(str(raw))
    if missing:
        raise TableRouterError(f"以下数据表未在当前数据源的已选表中：{', '.join(missing)}")
    return matched


async def _load_columns(source: KnowledgeDatabaseSource | dict[str, Any], table_names: list[str]) -> dict[str, list[str]]:
    if not table_names:
        return {}

    parsed: list[tuple[str, str, str]] = []
    for table_name in table_names:
        normalized = _normalize_table_name(table_name)
        if "." in normalized:
            schema, table = normalized.split(".", 1)
        else:
            schema, table = "public", normalized
        if table:
            parsed.append((table_name, schema, table))
    if not parsed:
        return {}

    engine = create_async_engine(database_source_url(source), pool_pre_ping=True)
    try:
        async with engine.connect() as conn:
            columns_by_table: dict[str, list[str]] = {name: [] for name, _, _ in parsed}
            for original, schema, table in parsed:
                result = await conn.execute(
                    text(
                        """
                        SELECT column_name
                        FROM information_schema.columns
                        WHERE table_schema = :schema
                          AND table_name = :table
                        ORDER BY ordinal_position
                        """
                    ),
                    {"schema": schema, "table": table},
                )
                columns_by_table[original] = [str(row.column_name) for row in result]
            return columns_by_table
    finally:
        await engine.dispose()


def _score_table(question: str, table_name: str, columns: list[str]) -> TableCandidate:
    query_tokens = _tokens(question)
    table_tokens = _tokens(table_name.replace("_", " ")) | _tokens(table_name)
    column_tokens = set()
    for column in columns:
        column_tokens |= _tokens(str(column).replace("_", " "))
        column_tokens |= _tokens(str(column))

    reasons: list[str] = []
    score = 0.0
    table_hits = query_tokens & table_tokens
    column_hits = query_tokens & column_tokens
    if table_hits:
        score += 8.0 * len(table_hits)
        reasons.append(f"表名命中：{', '.join(sorted(table_hits))}")
    if column_hits:
        score += 3.0 * len(column_hits)
        reasons.append(f"字段命中：{', '.join(sorted(column_hits)[:8])}")
    if not reasons:
        reasons.append("已选表候选")
    return TableCandidate(name=table_name, columns=columns, score=score, reasons=reasons)


def _build_prompt_context(route: TableRoute) -> str:
    lines = [
        "PuddingClaw 表路由结果：",
        f"- 数据源：{route.source_name} ({route.database_source_id})",
        f"- 数据库：{route.database}",
        f"- SQL 方言：{route.dialect}",
        "- 允许使用的数据表：",
    ]
    for candidate in route.candidates:
        if candidate.name not in route.table_names:
            continue
        column_preview = ", ".join(candidate.columns[:30])
        lines.append(f"  - {candidate.name}")
        if column_preview:
            lines.append(f"    字段：{column_preview}")
    return "\n".join(lines)


def summarize_table_route(route: TableRoute) -> dict[str, Any]:
    """Return a compact, trace/log-friendly route summary.

    Keep this intentionally small: the full prompt context can contain many
    columns and is still available on ``route.prompt_context`` for Vanna, but
    logs and Trace should only show the decision boundary.
    """

    selected = set(route.table_names)
    candidates: list[dict[str, Any]] = []
    for candidate in route.candidates[:8]:
        candidates.append(
            {
                "table": candidate.name,
                "selected": candidate.name in selected,
                "score": round(candidate.score, 3),
                "reason": "；".join(candidate.reasons[:3]),
                "columns_preview": candidate.columns[:12],
                "columns_count": len(candidate.columns),
            }
        )
    return {
        "database_source_id": route.database_source_id,
        "source_name": route.source_name,
        "database": route.database,
        "dialect": route.dialect,
        "selected_tables": route.table_names,
        "available_tables_count": len(route.available_tables),
        "confidence": round(route.confidence, 3),
        "reason": route.reason,
        "candidates": candidates,
    }


def _route_candidates_for_prompt(candidates: list[TableCandidate], table_names: list[str]) -> list[TableCandidate]:
    """Keep selected tables in prompt context, then append top scored candidates."""

    selected = set(table_names)
    ordered: list[TableCandidate] = []
    seen: set[str] = set()
    for candidate in candidates:
        if candidate.name in selected and candidate.name not in seen:
            ordered.append(candidate)
            seen.add(candidate.name)
    for candidate in candidates:
        if candidate.name not in seen:
            ordered.append(candidate)
            seen.add(candidate.name)
        if len(ordered) >= max(10, len(selected)):
            break
    return ordered


async def route_database_tables(session: AsyncSession, request: DatabaseQueryRequest) -> TableRoute:
    """Resolve a safe database/table scope for the NL2SQL request."""

    question = request.question.strip()
    if not question:
        raise TableRouterError("问题不能为空。")

    source_records = await list_database_sources(session)
    if request.database_source_id:
        source_ids = [request.database_source_id]
    else:
        source_ids = [str(source.get("id")) for source in source_records if source.get("id")]

    best_route: TableRoute | None = None
    route_errors: list[str] = []

    for source_id in source_ids:
        try:
            source = await get_database_source(session, source_id)
            selected_tables = database_source_selected_tables(source)
            if not selected_tables:
                route_errors.append(f"{source_id}: 未选择可问数数据表")
                continue

            table_names = _match_requested_tables(request.table_names, selected_tables)
            if not table_names and len(selected_tables) == 1:
                table_names = selected_tables[:1]

            columns_by_table = await _load_columns(source, selected_tables)
            candidates = [
                _score_table(question, table, columns_by_table.get(table, []))
                for table in selected_tables
            ]
            candidates.sort(key=lambda item: item.score, reverse=True)

            if not table_names:
                positive = [item.name for item in candidates if item.score > 0]
                table_names = positive[:3] or [candidates[0].name]

            selected_candidates = [item for item in candidates if item.name in set(table_names)]
            top_score = max((item.score for item in selected_candidates), default=0.0)
            confidence = 1.0 if request.table_names else min(0.95, 0.45 + top_score / 20)
            if len(selected_tables) == 1:
                confidence = max(confidence, 0.8)

            source_name = str(_source_value(source, "name", source_id) or source_id)
            database = str(_source_value(source, "database", "") or "")
            route = TableRoute(
                database_source_id=source_id,
                source_name=source_name,
                database=database,
                dialect="PostgreSQL",
                table_names=table_names,
                available_tables=selected_tables,
                candidates=_route_candidates_for_prompt(candidates, table_names),
                confidence=confidence,
                reason=(
                    "用户显式指定表"
                    if request.table_names
                    else ("单表数据源" if len(selected_tables) == 1 else "按问题与已选表/字段轻量匹配")
                ),
                prompt_context="",
            )
            route.prompt_context = _build_prompt_context(route)

            if best_route is None or route.confidence > best_route.confidence:
                best_route = route
        except (KnowledgeDatabaseSourceError, TableRouterError) as exc:
            route_errors.append(f"{source_id}: {exc}")

    if best_route is None:
        detail = "；".join(route_errors) if route_errors else "没有可用数据库源。"
        raise TableRouterError(f"无法确定可问数的数据表：{detail}")

    if best_route.confidence < 0.55 and not request.table_names:
        raise TableRouterError(
            "无法可靠判断要查询哪张数据库表。请在问数工作台选择数据表，或在问题里明确表/业务对象。"
        )

    summary = summarize_table_route(best_route)
    logger.info(
        "[nl2sql-router] question=%r source=%s database=%s selected_tables=%s confidence=%.3f reason=%s candidates=%s",
        question[:160],
        summary["source_name"],
        summary["database"],
        ",".join(summary["selected_tables"]),
        summary["confidence"],
        summary["reason"],
        [
            {
                "table": item["table"],
                "selected": item["selected"],
                "score": item["score"],
                "reason": item["reason"],
            }
            for item in summary["candidates"][:5]
        ],
    )
    return best_route
