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

from analytics.models import AnalyticsModelError, get_analytics_model_registry
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


def _has_any(value: str, needles: tuple[str, ...]) -> bool:
    return any(needle in value for needle in needles)


def _semantic_table_boost(question: str, table_name: str, columns: list[str]) -> tuple[float, list[str]]:
    """Add deterministic domain hints for Chinese BI questions.

    Token matching works for explicit table/column names, but most user
    questions say "上市/纯电/皮卡/配置率" instead of launch_year/energy_type.
    Keep this as a small router hint, not SQL-generation logic.
    """

    normalized_table = _normalize_table_name(table_name).split(".")[-1]
    column_set = {str(column) for column in columns}
    reasons: list[str] = []
    score = 0.0

    dimension_terms = (
        "上市",
        "新车",
        "年份",
        "月份",
        "今年",
        "纯电",
        "能源",
        "级别",
        "皮卡",
        "价格",
        "价位",
        "轴距",
        "品牌",
        "车系",
        "在售",
        "停售",
        "销售状态",
    )
    has_model_dimension_columns = {
        "launch_year",
        "launch_date",
        "energy_type",
        "vehicle_level",
        "wheelbase_mm",
        "price_band",
        "brand",
        "serial_name",
        "sale_status",
    } <= column_set
    if normalized_table == "vehicle_model_base" and has_model_dimension_columns and _has_any(question, dimension_terms):
        score += 18.0
        reasons.append("语义命中：款型基础表")

    motor_power_terms = ("电机功率", "电机总功率", "电动机总功率", "功率段")
    if (
        normalized_table == "vehicle_model_base"
        and "motor_power_kw" in column_set
        and _has_any(question, motor_power_terms)
    ):
        score += 18.0
        reasons.append("语义命中：款型基础表电机功率")

    config_terms = (
        "配置率",
        "配备率",
        "搭载率",
        "配置",
        "配备",
        "搭载",
        "空气悬架",
        "空气悬挂",
        "激光雷达",
    )
    has_eav_columns = {"type_name", "type_value", "car_name"} <= column_set
    if normalized_table == "vehicle_params" and has_eav_columns and _has_any(question, config_terms):
        score += 16.0
        reasons.append("语义命中：配置明细 EAV 表")

    return score, reasons


def _match_requested_tables(
    requested: list[str],
    available: list[str],
    *,
    configured_aliases: dict[str, str] | None = None,
    scope_label: str = "当前数据源的已选表",
) -> list[str]:
    if not requested:
        return []
    alias_to_available: dict[str, str] = {}
    for table in available:
        for alias in _table_aliases(table):
            alias_to_available[alias.lower()] = table
    available_by_normalized = {_normalize_table_name(table).lower(): table for table in available}
    for alias, target in (configured_aliases or {}).items():
        available_table = available_by_normalized.get(_normalize_table_name(target).lower())
        if available_table:
            alias_key = str(alias).strip().lower()
            existing = alias_to_available.get(alias_key)
            if existing and _normalize_table_name(existing).lower() != _normalize_table_name(available_table).lower():
                raise TableRouterError(f"分析模型表别名与真实数据表标识冲突：{alias}")
            alias_to_available[alias_key] = available_table
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
        raise TableRouterError(f"以下数据表未在{scope_label}中：{', '.join(missing)}")
    return matched


def _model_table_aliases_by_source(model_id: str | None) -> dict[str, dict[str, str]]:
    """Return explicit, model-owned user aliases for declared database tables."""

    clean_model_id = str(model_id or "").strip()
    if not clean_model_id:
        return {}
    try:
        model = get_analytics_model_registry().get_model(clean_model_id)
    except AnalyticsModelError as exc:
        raise TableRouterError(str(exc)) from exc

    data_assets = (model.get("frontmatter") or {}).get("data_assets") or {}
    table_refs = [str(item or "").strip() for item in data_assets.get("tables") or []]
    declared = {ref for ref in table_refs if ref and not ref.startswith("table_asset:") and "." in ref}
    raw_aliases = data_assets.get("table_aliases") or {}
    if not isinstance(raw_aliases, dict):
        raise TableRouterError("分析模型 data_assets.table_aliases 必须是映射。")

    grouped: dict[str, dict[str, str]] = {}
    for raw_ref, raw_values in raw_aliases.items():
        ref = str(raw_ref or "").strip()
        if ref not in declared:
            raise TableRouterError(f"分析模型表别名引用了未声明的数据表：{ref}")
        if not isinstance(raw_values, list):
            raise TableRouterError(f"分析模型表别名必须是列表：{ref}")
        source_id, table_name = ref.split(".", 1)
        aliases = grouped.setdefault(source_id, {})
        for raw_alias in raw_values:
            alias = str(raw_alias or "").strip().lower()
            if not alias:
                continue
            existing = aliases.get(alias)
            if existing and existing != table_name:
                raise TableRouterError(f"分析模型表别名存在冲突：{alias} 同时指向 {existing} 和 {table_name}")
            aliases[alias] = table_name
    return grouped


def _contains_explicit_alias(question: str, alias: str) -> bool:
    """Match a configured alias without treating it as fuzzy model inference."""

    value = str(alias or "").strip().lower()
    if not value:
        return False
    if re.fullmatch(r"[a-z0-9_\-]+", value):
        return bool(
            re.search(
                rf"(?<![a-z0-9_\-]){re.escape(value)}(?![a-z0-9_\-])",
                question.lower(),
            )
        )
    return value in question.lower()


def _resolve_question_table_aliases(
    question: str,
    configured_aliases: dict[str, str],
    *,
    model_id: str,
) -> list[dict[str, str]]:
    resolutions: list[dict[str, str]] = []
    for alias, table in configured_aliases.items():
        if _contains_explicit_alias(question, alias):
            resolutions.append(
                {
                    "alias": alias,
                    "table": table,
                    "source": f"analytics_model:{model_id}",
                }
            )
    return resolutions


def _ambiguous_requested_aliases(
    question: str,
    requested_tables: list[str],
    aliases_by_source: dict[str, dict[str, str]],
) -> dict[str, list[str]]:
    """Find source-backed aliases that cannot identify one physical relation."""

    requested = {str(item or "").strip().lower() for item in requested_tables if str(item or "").strip()}
    targets_by_alias: dict[str, set[str]] = {}
    for source_id, aliases in aliases_by_source.items():
        for alias, table in aliases.items():
            if alias not in requested and not _contains_explicit_alias(question, alias):
                continue
            targets_by_alias.setdefault(alias, set()).add(f"{source_id}.{table}")
    return {alias: sorted(targets) for alias, targets in targets_by_alias.items() if len(targets) > 1}


def _model_database_tables_by_source(model_id: str | None) -> dict[str, list[str]]:
    """Return database tables declared by an analytics model, grouped by source.

    Model table references use ``<database_source_id>.<table_name>``. Logical
    ``table_asset:`` references belong to the file/table runtime and are not
    part of database NL2SQL routing.
    """

    clean_model_id = str(model_id or "").strip()
    if not clean_model_id:
        return {}
    try:
        model = get_analytics_model_registry().get_model(clean_model_id)
    except AnalyticsModelError as exc:
        raise TableRouterError(str(exc)) from exc

    table_refs = ((model.get("frontmatter") or {}).get("data_assets") or {}).get("tables") or []
    grouped: dict[str, list[str]] = {}
    for raw_ref in table_refs:
        ref = str(raw_ref or "").strip()
        if not ref or ref.startswith("table_asset:") or "." not in ref:
            continue
        source_id, table_name = ref.split(".", 1)
        source_id = source_id.strip()
        table_name = table_name.strip()
        if source_id and table_name and table_name not in grouped.setdefault(source_id, []):
            grouped[source_id].append(table_name)
    return grouped


async def _load_columns(
    source: KnowledgeDatabaseSource | dict[str, Any], table_names: list[str]
) -> dict[str, list[str]]:
    if not table_names:
        return {}

    source_type = str(_source_value(source, "source_type", _source_value(source, "type", "postgresql")) or "postgresql").lower()
    default_schema = str(_source_value(source, "database", "") or "") if source_type == "mysql" else "public"
    parsed: list[tuple[str, str, str]] = []
    for table_name in table_names:
        normalized = _normalize_table_name(table_name)
        if "." in normalized:
            schema, table = normalized.split(".", 1)
        else:
            schema, table = default_schema, normalized
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
    semantic_score, semantic_reasons = _semantic_table_boost(question, table_name, columns)
    if semantic_score:
        score += semantic_score
        reasons.extend(semantic_reasons)
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
    if route.alias_resolutions:
        lines.append("- 用户表简称解析（来自已选分析模型）：")
        for item in route.alias_resolutions:
            lines.append(f"  - {item['alias']} -> {item['table']} ({item['source']})")
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
        "alias_resolutions": route.alias_resolutions,
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
    model_tables_by_source = _model_database_tables_by_source(request.model_id)
    model_aliases_by_source = _model_table_aliases_by_source(request.model_id)
    if not request.database_source_id:
        ambiguous_aliases = _ambiguous_requested_aliases(
            question,
            request.table_names,
            model_aliases_by_source,
        )
        if ambiguous_aliases:
            details = "；".join(
                f"{alias} -> {', '.join(targets)}" for alias, targets in sorted(ambiguous_aliases.items())
            )
            raise TableRouterError(f"表别名跨数据源存在歧义，请指定数据库源：{details}")
    if request.database_source_id:
        source_ids = [request.database_source_id]
    elif model_tables_by_source:
        available_source_ids = {str(source.get("id")) for source in source_records if source.get("id")}
        source_ids = [source_id for source_id in model_tables_by_source if source_id in available_source_ids]
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

            declared_model_tables = model_tables_by_source.get(source_id, [])
            if request.model_id:
                declared_normalized = {_normalize_table_name(table).lower() for table in declared_model_tables}
                routable_tables = [
                    table for table in selected_tables if _normalize_table_name(table).lower() in declared_normalized
                ]
                if not routable_tables:
                    raise TableRouterError(f"当前分析模型未在数据源 {source_id} 声明可用数据表")
            else:
                routable_tables = selected_tables

            configured_aliases = model_aliases_by_source.get(source_id, {})
            model_reference_aliases = {f"{source_id}.{table}".lower(): table for table in declared_model_tables}
            for alias, target in configured_aliases.items():
                reference_target = model_reference_aliases.get(alias)
                if (
                    reference_target
                    and _normalize_table_name(reference_target).lower() != _normalize_table_name(target).lower()
                ):
                    raise TableRouterError(f"分析模型表别名与数据资产引用冲突：{alias}")
            selection_aliases = {**configured_aliases, **model_reference_aliases}
            alias_resolutions = _resolve_question_table_aliases(
                question,
                configured_aliases,
                model_id=str(request.model_id or ""),
            )
            for raw_name in request.table_names:
                alias = str(raw_name or "").strip().lower()
                table = configured_aliases.get(alias)
                if table and not any(item["alias"] == alias for item in alias_resolutions):
                    alias_resolutions.append(
                        {
                            "alias": alias,
                            "table": table,
                            "source": f"analytics_model:{request.model_id}",
                        }
                    )
                model_ref_table = model_reference_aliases.get(alias)
                if model_ref_table and not any(item["alias"] == alias for item in alias_resolutions):
                    alias_resolutions.append(
                        {
                            "alias": alias,
                            "table": model_ref_table,
                            "source": f"analytics_model:{request.model_id}:data_asset_ref",
                        }
                    )
            model_table_names = routable_tables if request.model_id and not request.table_names else []
            table_names = _match_requested_tables(
                request.table_names or model_table_names,
                routable_tables,
                configured_aliases=selection_aliases,
                scope_label=("当前分析模型的数据资产范围" if request.model_id else "当前数据源的已选表"),
            )
            if request.table_names and alias_resolutions:
                selected_normalized = {_normalize_table_name(table).lower() for table in table_names}
                conflicting_aliases = [
                    item
                    for item in alias_resolutions
                    if _normalize_table_name(item["table"]).lower() not in selected_normalized
                ]
                if conflicting_aliases:
                    details = ", ".join(f"{item['alias']}->{item['table']}" for item in conflicting_aliases)
                    raise TableRouterError(f"问题中的表别名与显式 table_names 冲突：{details}")
            if not table_names and len(routable_tables) == 1:
                table_names = routable_tables[:1]

            columns_by_table = await _load_columns(source, routable_tables)
            candidates = [_score_table(question, table, columns_by_table.get(table, [])) for table in routable_tables]
            for resolution in alias_resolutions:
                for candidate in candidates:
                    if _normalize_table_name(candidate.name) == _normalize_table_name(resolution["table"]):
                        candidate.score += 24.0
                        candidate.reasons.append(f"分析模型表别名命中：{resolution['alias']}→{resolution['table']}")
            candidates.sort(key=lambda item: item.score, reverse=True)

            if not table_names:
                alias_tables = list(dict.fromkeys(item["table"] for item in alias_resolutions))
                positive = [item.name for item in candidates if item.score > 0]
                table_names = alias_tables or positive[:3] or [candidates[0].name]

            selected_candidates = [item for item in candidates if item.name in set(table_names)]
            top_score = max((item.score for item in selected_candidates), default=0.0)
            confidence = 1.0 if request.table_names or model_table_names else min(0.95, 0.45 + top_score / 20)
            if len(routable_tables) == 1:
                confidence = max(confidence, 0.8)

            source_name = str(_source_value(source, "name", source_id) or source_id)
            database = str(_source_value(source, "database", "") or "")
            source_type = str(_source_value(source, "source_type", _source_value(source, "type", "postgresql")) or "postgresql").lower()
            route = TableRoute(
                database_source_id=source_id,
                source_name=source_name,
                database=database,
                dialect="MySQL" if source_type == "mysql" else "PostgreSQL",
                table_names=table_names,
                available_tables=routable_tables,
                candidates=_route_candidates_for_prompt(candidates, table_names),
                confidence=confidence,
                reason=(
                    ("调用方显式指定表（分析模型别名已解析）" if alias_resolutions else "调用方显式指定表")
                    if request.table_names
                    else (
                        "分析模型声明的数据资产"
                        if model_table_names
                        else ("单表模型范围" if len(routable_tables) == 1 else "按问题与模型表/字段轻量匹配")
                    )
                ),
                prompt_context="",
                alias_resolutions=alias_resolutions,
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
        raise TableRouterError("无法可靠判断要查询哪张数据库表。请在问数工作台选择数据表，或在问题里明确表/业务对象。")

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
