"""Internal Vanna-backed database knowledge query service."""

from __future__ import annotations

import asyncio
import json
import logging
from time import perf_counter
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from analytics.models.registry import get_analytics_model_registry
from analytics.nl2sql.guardrails import (
    GuardrailConflict,
    conflicts_to_messages,
    detect_guardrail_conflicts,
)
from analytics.nl2sql.result_store import attach_persisted_query_result
from analytics.nl2sql.runtime import build_vanna_client_from_app_config
from analytics.nl2sql.schemas import DatabaseQueryRequest, DatabaseQueryResult, DatabaseSqlGenerationResult
from analytics.nl2sql.sql_runner import SqlRunnerError, extract_sql, run_readonly_sql
from analytics.nl2sql.table_router import TableRouterError, route_database_tables, summarize_table_route
from analytics.semantic_assets.resolver import (
    format_semantic_assets_for_prompt,
    resolve_semantic_assets,
    resolve_semantic_assets_by_ids,
    semantic_resolution_to_trace,
)
from config import get_vanna_config
from knowledge.database_sources import get_database_source


class DatabaseKnowledgeQueryError(RuntimeError):
    """Raised when the database knowledge query pipeline fails."""

    def __init__(self, message: str, *, sql: str | None = None) -> None:
        super().__init__(message)
        self.sql = sql


logger = logging.getLogger(__name__)

VANNA_REFERENCE_TOP_K = 5
VANNA_ENTITY_TOP_K_PER_TYPE = 10
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


def _compose_vanna_question(question: str, route_context: str, semantic_context: str = "") -> str:
    semantic_block = ""
    if semantic_context:
        semantic_block = f"\n\n{semantic_context.strip()}\n"
    return (
        "用户问题：\n"
        f"{question.strip()}\n\n"
        f"{route_context}\n\n"
        f"{semantic_block}"
        "生成 SQL 要求：\n"
        "- 只能生成 PostgreSQL 只读 SQL。\n"
        "- 只能使用上面允许的数据表和字段。\n"
        "- 不要写 INSERT、UPDATE、DELETE、DROP、ALTER、CREATE 等修改语句。\n"
        "- 汇总、统计、趋势、占比、排名类问题，优先使用聚合函数和 GROUP BY。\n"
        "- 不要对聚合结果使用 LIMIT 来近似回答；只有用户明确要求 top-N 时才在聚合后 ORDER BY ... LIMIT。\n"
        "- 用户明确要求列出、明细、所有记录、价格表时，生成明细 SELECT；不要为了减少结果而擅自加 LIMIT，执行层会处理计数、预览、分页和导出。\n"
        "- 当用户按月份查询 ISO 日期字符串时，优先使用日期函数或 '-MM-' 形式匹配，不要只使用中文月份字符串如 '%6月%'。\n"
        "- PostgreSQL 中 COUNT(DISTINCT (right.col1, right.col2, ...)) 会把 LEFT JOIN 未命中的全 NULL 元组当作一个 distinct 值；LEFT JOIN 后统计右表命中数时必须加 FILTER (WHERE right.key IS NOT NULL)、COUNT(right.key)，或先在非空子查询/CTE 中去重计数。\n"
        "- vehicle_params 的配置率/配备率/搭载率等多条件分析，优先一次扫描所需 type_name 并按 brand, serial_name, car_name 聚合成 flags；不要生成多层 EXISTS/NOT EXISTS 自关联。\n"
        "- 只返回 SQL，不要返回解释。"
    )


def _format_analytics_model_for_sql_prompt(model_id: str | None) -> tuple[str, dict[str, Any]]:
    """Load the selected model's global playbook into the inner SQL generator."""
    normalized_id = str(model_id or "").strip()
    if not normalized_id:
        return "", {}

    model = get_analytics_model_registry().get_model_context(normalized_id)
    frontmatter = model.get("frontmatter") or {}
    body = str(model.get("body") or "").strip()
    prompt = (
        "<analytics_model_sql_context>\n"
        "当前 SQL 属于用户已选择的分析模型，必须遵守模型正文中的全局业务边界和默认分析范围。\n"
        "规则优先级：用户明确要求 > 具体 Measure/Reference > 模型全局规则 > Dimension/通用 SQL 规则。\n"
        "只应用与本次 SQL 有关的模型规则；报告布局、HTML 和输出工作流不属于 SQL 生成任务。\n\n"
        f"模型 ID：{model.get('id')}\n"
        f"模型名称：{model.get('name')}\n"
        f"模型路径：{model.get('path')}\n\n"
        "模型 Frontmatter：\n"
        f"```json\n{json.dumps(frontmatter, ensure_ascii=False, indent=2)}\n```\n\n"
        "模型正文：\n"
        f"{body}\n"
        "</analytics_model_sql_context>"
    )
    trace = {
        "id": model.get("id"),
        "name": model.get("name"),
        "version": model.get("version"),
        "path": model.get("path"),
        "body_preview": body[:2000] + ("...[truncated]" if len(body) > 2000 else ""),
    }
    return prompt, trace


def _compose_guardrail_rewrite_question(original_question: str, sql: str, conflicts: list[str]) -> str:
    return (
        f"{original_question.strip()}\n\n"
        "你刚才生成的 SQL 被后端 SQL guardrail 拦截，禁止执行。\n"
        "冲突原因：\n"
        + "\n".join(f"- {item}" for item in conflicts)
        + "\n\n原 SQL：\n```sql\n"
        + sql.strip()
        + "\n```\n\n请只重写 SQL，不要解释。\n\n"
        + _CONFIG_RATE_SQL_TEMPLATE
    )


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


async def query_database_knowledge(
    session: AsyncSession,
    request: DatabaseQueryRequest,
) -> DatabaseQueryResult:
    """Run table routing, Vanna SQL generation, and read-only execution."""

    stage_timings: dict[str, float] = {}
    total_started = perf_counter()

    def record_stage(name: str, started: float) -> None:
        stage_timings[name] = round((perf_counter() - started) * 1000, 2)

    try:
        stage_started = perf_counter()
        route = await route_database_tables(session, request)
        record_stage("router_ms", stage_started)

        stage_started = perf_counter()
        resolver = resolve_semantic_assets_by_ids if request.model_id else resolve_semantic_assets
        semantic_resolution = await asyncio.to_thread(
            resolver,
            request.question,
            requested_ids=request.measure_ids,
        )
        semantic_trace = semantic_resolution_to_trace(semantic_resolution)
        model_context, model_trace = await asyncio.to_thread(
            _format_analytics_model_for_sql_prompt,
            request.model_id,
        )
        if model_trace:
            semantic_trace["analytics_model"] = model_trace
        semantic_context = "\n\n".join(
            item
            for item in (
                model_context,
                format_semantic_assets_for_prompt(semantic_resolution),
            )
            if item
        )
        record_stage("semantic_assets_ms", stage_started)

        stage_started = perf_counter()
        source = await get_database_source(session, route.database_source_id)
        vanna = build_vanna_client_from_app_config()
        record_stage("setup_ms", stage_started)

        routed_question = _compose_vanna_question(request.question, route.prompt_context, semantic_context)
        guardrail_note = ""
        stage_started = perf_counter()
        references = await asyncio.to_thread(_collect_vanna_references, vanna, routed_question, route)
        record_stage("vanna_references_ms", stage_started)
        prompt_entities = references.pop("_prompt_entities", [])
        entities_summary = references.get("entities") or {}
        entity_types = entities_summary.get("entity_types") or []
        top_k_config = entities_summary.get("top_k") or {}

        logger.info(
            "[nl2sql-service] generate_sql question=%r route=%s semantic_assets=%s entity_types=%s entity_prompt_items=%s",
            request.question[:160],
            summarize_table_route(route),
            semantic_trace.get("matched_count", 0),
            entity_types,
            len(prompt_entities),
        )
        def _generate_sql_blocking(question: str) -> str:
            return vanna.generate_sql(
                question=question,
                allow_llm_to_see_data=request.allow_llm_to_see_data,
                entity_types=entity_types,
                entity_list=prompt_entities,
                entity_top_k_per_type=max(1, int(top_k_config.get("default") or VANNA_ENTITY_TOP_K_PER_TYPE)),
                entity_top_k_by_type=top_k_config.get("by_type") or {},
            )

        try:
            stage_started = perf_counter()
            raw_sql = await asyncio.to_thread(_generate_sql_blocking, routed_question)
            record_stage("sql_generation_ms", stage_started)
        except Exception as exc:
            record_stage("sql_generation_ms", stage_started)
            llm_context = _vanna_llm_context(vanna)
            raise DatabaseKnowledgeQueryError(
                "Vanna SQL 生成阶段调用 LLM 失败："
                f"{type(exc).__name__}: {exc}。"
                f" 当前 Vanna LLM 配置 model={llm_context['model'] or '<empty>'}, "
                f"base_url={llm_context['base_url'] or '<empty>'}。"
                "请检查该 OpenAI-compatible 地址的 /chat/completions 路由、Higress/Provider 配置和本机网络连通性。"
            ) from exc
        sql = extract_sql(raw_sql)
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
                "[nl2sql-service] sql_guardrail_conflict_retry source=%s tables=%s conflicts=%s sql=%s",
                route.source_name,
                ",".join(route.table_names),
                semantic_conflicts,
                " ".join(sql.split())[:500],
            )
            rewrite_question = _compose_guardrail_rewrite_question(routed_question, sql, semantic_conflicts)
            stage_started = perf_counter()
            try:
                raw_sql = await asyncio.to_thread(_generate_sql_blocking, rewrite_question)
            finally:
                record_stage("sql_regeneration_ms", stage_started)
            rewritten_sql = extract_sql(raw_sql)
            rewritten_guardrail_conflicts = _detect_sql_guardrail_conflicts(
                rewritten_sql,
                source_name=route.source_name,
                route=route,
                semantic_trace=semantic_trace,
                question=request.question,
            )
            rewritten_blocking_conflicts = [
                conflict for conflict in rewritten_guardrail_conflicts if conflict.action in {"rewrite", "block"}
            ]
            if rewritten_blocking_conflicts:
                raise DatabaseKnowledgeQueryError(
                    "生成 SQL 与 SQL guardrail 规则冲突，已拦截执行："
                    + "；".join(_guardrail_messages(rewritten_blocking_conflicts)),
                    sql=rewritten_sql,
                )
            rewrite_note = "SQL guardrail 已拦截首版 SQL 并重写一次：" + "；".join(semantic_conflicts)
            guardrail_note = f"{guardrail_note}；{rewrite_note}" if guardrail_note else rewrite_note
            sql = rewritten_sql
        logger.info(
            "[nl2sql-service] sql_generated source=%s tables=%s sql=%s",
            route.source_name,
            ",".join(route.table_names),
            " ".join(sql.split())[:500],
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
                "[nl2sql-service] sql_execution_failed source=%s tables=%s timings=%s sql=%s",
                route.source_name,
                ",".join(route.table_names),
                stage_timings,
                " ".join(sql.split())[:500],
            )
            raise
        stage_started = perf_counter()
        if await attach_persisted_query_result(
            session,
            execution,
            question=request.question,
            sql=sql,
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
) -> DatabaseSqlGenerationResult:
    """Run table routing, semantic context loading, Vanna SQL generation, and SQL guardrails only."""

    stage_timings: dict[str, float] = {}
    total_started = perf_counter()

    def record_stage(name: str, started: float) -> None:
        stage_timings[name] = round((perf_counter() - started) * 1000, 2)

    try:
        stage_started = perf_counter()
        route = await route_database_tables(session, request)
        record_stage("router_ms", stage_started)

        stage_started = perf_counter()
        resolver = resolve_semantic_assets_by_ids if request.model_id else resolve_semantic_assets
        semantic_resolution = await asyncio.to_thread(
            resolver,
            request.question,
            requested_ids=request.measure_ids,
        )
        semantic_trace = semantic_resolution_to_trace(semantic_resolution)
        model_context, model_trace = await asyncio.to_thread(
            _format_analytics_model_for_sql_prompt,
            request.model_id,
        )
        if model_trace:
            semantic_trace["analytics_model"] = model_trace
        semantic_context = "\n\n".join(
            item
            for item in (
                model_context,
                format_semantic_assets_for_prompt(semantic_resolution),
            )
            if item
        )
        record_stage("semantic_assets_ms", stage_started)

        stage_started = perf_counter()
        vanna = build_vanna_client_from_app_config()
        record_stage("setup_ms", stage_started)

        routed_question = _compose_vanna_question(request.question, route.prompt_context, semantic_context)
        guardrail_note = ""
        stage_started = perf_counter()
        references = await asyncio.to_thread(_collect_vanna_references, vanna, routed_question, route)
        record_stage("vanna_references_ms", stage_started)
        prompt_entities = references.pop("_prompt_entities", [])
        entities_summary = references.get("entities") or {}
        entity_types = entities_summary.get("entity_types") or []
        top_k_config = entities_summary.get("top_k") or {}

        logger.info(
            "[nl2sql-service] generate_sql_only question=%r route=%s semantic_assets=%s entity_types=%s entity_prompt_items=%s",
            request.question[:160],
            summarize_table_route(route),
            semantic_trace.get("matched_count", 0),
            entity_types,
            len(prompt_entities),
        )

        def _generate_sql_blocking(question: str) -> str:
            return vanna.generate_sql(
                question=question,
                allow_llm_to_see_data=request.allow_llm_to_see_data,
                entity_types=entity_types,
                entity_list=prompt_entities,
                entity_top_k_per_type=max(1, int(top_k_config.get("default") or VANNA_ENTITY_TOP_K_PER_TYPE)),
                entity_top_k_by_type=top_k_config.get("by_type") or {},
            )

        try:
            stage_started = perf_counter()
            raw_sql = await asyncio.to_thread(_generate_sql_blocking, routed_question)
            record_stage("sql_generation_ms", stage_started)
        except Exception as exc:
            record_stage("sql_generation_ms", stage_started)
            llm_context = _vanna_llm_context(vanna)
            raise DatabaseKnowledgeQueryError(
                "Vanna SQL 生成阶段调用 LLM 失败："
                f"{type(exc).__name__}: {exc}。"
                f" 当前 Vanna LLM 配置 model={llm_context['model'] or '<empty>'}, "
                f"base_url={llm_context['base_url'] or '<empty>'}。"
                "请检查该 OpenAI-compatible 地址的 /chat/completions 路由、Higress/Provider 配置和本机网络连通性。"
            ) from exc

        sql = extract_sql(raw_sql)
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
                "[nl2sql-service] sql_guardrail_conflict_retry source=%s tables=%s conflicts=%s sql=%s",
                route.source_name,
                ",".join(route.table_names),
                semantic_conflicts,
                " ".join(sql.split())[:500],
            )
            rewrite_question = _compose_guardrail_rewrite_question(routed_question, sql, semantic_conflicts)
            stage_started = perf_counter()
            try:
                raw_sql = await asyncio.to_thread(_generate_sql_blocking, rewrite_question)
            finally:
                record_stage("sql_regeneration_ms", stage_started)
            rewritten_sql = extract_sql(raw_sql)
            rewritten_guardrail_conflicts = _detect_sql_guardrail_conflicts(
                rewritten_sql,
                source_name=route.source_name,
                route=route,
                semantic_trace=semantic_trace,
                question=request.question,
            )
            rewritten_blocking_conflicts = [
                conflict for conflict in rewritten_guardrail_conflicts if conflict.action in {"rewrite", "block"}
            ]
            if rewritten_blocking_conflicts:
                raise DatabaseKnowledgeQueryError(
                    "生成 SQL 与 SQL guardrail 规则冲突，已拦截执行："
                    + "；".join(_guardrail_messages(rewritten_blocking_conflicts)),
                    sql=rewritten_sql,
                )
            rewrite_note = "SQL guardrail 已拦截首版 SQL 并重写一次：" + "；".join(semantic_conflicts)
            guardrail_note = f"{guardrail_note}；{rewrite_note}" if guardrail_note else rewrite_note
            sql = rewritten_sql

        logger.info(
            "[nl2sql-service] sql_generated_only source=%s tables=%s sql=%s",
            route.source_name,
            ",".join(route.table_names),
            " ".join(sql.split())[:500],
        )
        stage_timings["total_ms"] = round((perf_counter() - total_started) * 1000, 2)
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
        )
    except DatabaseKnowledgeQueryError:
        raise
    except (TableRouterError, SqlRunnerError) as exc:
        raise DatabaseKnowledgeQueryError(str(exc), sql=getattr(exc, "sql", None)) from exc
    except Exception as exc:
        raise DatabaseKnowledgeQueryError(f"{type(exc).__name__}: {exc}") from exc
