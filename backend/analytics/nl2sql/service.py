"""Internal Vanna-backed database knowledge query service."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from analytics.nl2sql.runtime import build_vanna_client_from_app_config
from analytics.nl2sql.schemas import DatabaseQueryRequest, DatabaseQueryResult
from analytics.nl2sql.result_store import persist_query_result
from analytics.nl2sql.sql_runner import SqlRunnerError, extract_sql, run_readonly_sql
from analytics.nl2sql.table_router import TableRouterError, route_database_tables, summarize_table_route
from config import get_database_qa_config, get_vanna_config
from knowledge.database_sources import get_database_source


class DatabaseKnowledgeQueryError(RuntimeError):
    """Raised when the database knowledge query pipeline fails."""


logger = logging.getLogger(__name__)

VANNA_REFERENCE_TOP_K = 5
VANNA_ENTITY_TOP_K_PER_TYPE = 10


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


def _compose_vanna_question(question: str, route_context: str) -> str:
    return (
        "用户问题：\n"
        f"{question.strip()}\n\n"
        f"{route_context}\n\n"
        "生成 SQL 要求：\n"
        "- 只能生成 PostgreSQL 只读 SQL。\n"
        "- 只能使用上面允许的数据表和字段。\n"
        "- 不要写 INSERT、UPDATE、DELETE、DROP、ALTER、CREATE 等修改语句。\n"
        "- 汇总、统计、趋势、占比、排名类问题，优先使用聚合函数和 GROUP BY。\n"
        "- 不要对聚合结果使用 LIMIT 来近似回答；只有用户明确要求 top-N 时才在聚合后 ORDER BY ... LIMIT。\n"
        "- 用户明确要求列出、明细、所有记录、价格表时，生成明细 SELECT；不要为了减少结果而擅自加 LIMIT，执行层会处理计数、预览、分页和导出。\n"
        "- 当用户按月份查询 ISO 日期字符串时，优先使用日期函数或 '-MM-' 形式匹配，不要只使用中文月份字符串如 '%6月%'。\n"
        "- 只返回 SQL，不要返回解释。"
    )


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

    try:
        route = await route_database_tables(session, request)
        source = await get_database_source(session, route.database_source_id)
        vanna = build_vanna_client_from_app_config()
        references = await asyncio.to_thread(_collect_vanna_references, vanna, request.question, route)
        prompt_entities = references.pop("_prompt_entities", [])
        entities_summary = references.get("entities") or {}
        entity_types = entities_summary.get("entity_types") or []
        top_k_config = entities_summary.get("top_k") or {}

        routed_question = _compose_vanna_question(request.question, route.prompt_context)
        logger.info(
            "[nl2sql-service] generate_sql question=%r route=%s entity_types=%s entity_prompt_items=%s",
            request.question[:160],
            summarize_table_route(route),
            entity_types,
            len(prompt_entities),
        )
        try:
            raw_sql = await asyncio.to_thread(
                vanna.generate_sql,
                question=routed_question,
                allow_llm_to_see_data=request.allow_llm_to_see_data,
                entity_types=entity_types,
                entity_list=prompt_entities,
                entity_top_k_per_type=max(1, int(top_k_config.get("default") or VANNA_ENTITY_TOP_K_PER_TYPE)),
                entity_top_k_by_type=top_k_config.get("by_type") or {},
            )
        except Exception as exc:
            llm_context = _vanna_llm_context(vanna)
            raise DatabaseKnowledgeQueryError(
                "Vanna SQL 生成阶段调用 LLM 失败："
                f"{type(exc).__name__}: {exc}。"
                f" 当前 Vanna LLM 配置 model={llm_context['model'] or '<empty>'}, "
                f"base_url={llm_context['base_url'] or '<empty>'}。"
                "请检查该 OpenAI-compatible 地址的 /chat/completions 路由、Higress/Provider 配置和本机网络连通性。"
            ) from exc
        sql = extract_sql(raw_sql)
        logger.info(
            "[nl2sql-service] sql_generated source=%s tables=%s sql=%s",
            route.source_name,
            ",".join(route.table_names),
            " ".join(sql.split())[:500],
        )
        execution = await run_readonly_sql(
            source,
            sql,
            allowed_tables=route.table_names,
            limit=request.limit,
        )
        database_qa_config = get_database_qa_config()
        if (
            database_qa_config.get("result_store_enabled", True)
            and not execution.is_complete
            and execution.materialized_all
            and execution.materialized_rows
        ):
            store_contract = await persist_query_result(
                session,
                question=request.question,
                sql=sql,
                columns=execution.columns,
                rows=execution.materialized_rows,
                profile=execution.profile,
            )
            execution.result_id = store_contract.get("result_id")
            execution.result_store = {key: value for key, value in store_contract.items() if key != "result_id"}
            execution.actions = [
                {
                    "type": "fetch_page",
                    "available": True,
                    "page_size": database_qa_config.get("default_page_size", 100),
                },
                {
                    "type": "export",
                    "available": bool(database_qa_config.get("export_enabled", True)),
                },
            ]
        elif not execution.is_complete:
            execution.actions = [
                {
                    "type": "fetch_page",
                    "available": False,
                    "reason": "result_not_fully_materialized",
                }
            ]
        logger.info(
            "[nl2sql-service] sql_executed source=%s tables=%s rows=%s limited=%s",
            route.source_name,
            ",".join(route.table_names),
            execution.row_count,
            execution.limited,
        )
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
        )
    except DatabaseKnowledgeQueryError:
        raise
    except (TableRouterError, SqlRunnerError) as exc:
        raise DatabaseKnowledgeQueryError(str(exc)) from exc
    except Exception as exc:
        raise DatabaseKnowledgeQueryError(f"{type(exc).__name__}: {exc}") from exc
