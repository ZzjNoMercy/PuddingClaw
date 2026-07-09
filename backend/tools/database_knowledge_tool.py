"""Agent tool for database-backed natural language analytics."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, Type

from langchain_core.tools import BaseTool
from pydantic import BaseModel, Field

from analytics.nl2sql.schemas import DatabaseQueryRequest, DatabaseQueryResult
from analytics.nl2sql.result_store import QueryResultStoreError, get_query_result_page
from analytics.nl2sql.service import DatabaseKnowledgeQueryError, query_database_knowledge
from analytics.nl2sql.table_router import summarize_table_route
from db import get_sessionmaker


class DatabaseKnowledgeInput(BaseModel):
    question: str = Field(
        description=(
            "Natural-language business question about configured PostgreSQL database tables. "
            "For business analytics questions, pass the user's original question directly; "
            "do not first ask this tool to list tables, inspect schemas, enumerate brands, or discover columns. "
            "The tool routes tables, loads DDL/docs/entities, and generates SQL internally."
        )
    )
    database_source_id: str | None = Field(
        default=None,
        description="Optional configured database source id. If omitted, the router picks from configured sources.",
    )
    table_names: list[str] = Field(
        default_factory=list,
        description="Optional table names such as ['vehicle_params'] or ['public.vehicle_params']. Explicit table names win.",
    )
    model_id: str | None = Field(
        default=None,
        description="Optional analytics data model id. Reserved for BI semantic-model routing.",
    )
    measure_ids: list[str] = Field(
        default_factory=list,
        description=(
            "Optional semantic asset ids such as ['measure:config_rate', 'dimension:launch_time']. "
            "When supplied, their Markdown definitions are forced into SQL-generation context."
        ),
    )
    limit: int = Field(default=100, ge=1, le=1000, description="Maximum result rows returned from read-only SQL.")


def _markdown_table(rows: list[dict[str, Any]], columns: list[str], *, max_rows: int = 20) -> list[str]:
    if not columns:
        return ["无结果行。"]
    visible_rows = rows[:max_rows]
    lines = [
        "| " + " | ".join(columns) + " |",
        "| " + " | ".join("---" for _ in columns) + " |",
    ]
    for row in visible_rows:
        values = [str(row.get(column, ""))[:160].replace("\n", " ") for column in columns]
        lines.append("| " + " | ".join(values) + " |")
    if len(rows) > max_rows:
        lines.append(
            f"\n仅预览前 {max_rows} 行，完整执行明细请在 Trace 面板中选择 database_knowledge_query，查看“问数 Trace 明细”。"
        )
    return lines


def _format_profile(profile: dict[str, Any]) -> list[str]:
    if not profile:
        return []
    lines = ["", "- Profile："]
    group_counts = profile.get("group_counts") if isinstance(profile.get("group_counts"), dict) else {}
    for column, counts in group_counts.items():
        if not isinstance(counts, dict) or not counts:
            continue
        lines.append(f"  - {column} 分布：")
        for value, count in list(counts.items())[:20]:
            lines.append(f"    - {value}: {count}")
    date_ranges = profile.get("date_ranges") if isinstance(profile.get("date_ranges"), dict) else {}
    for column, range_info in date_ranges.items():
        if isinstance(range_info, dict):
            lines.append(f"  - {column} 范围：{range_info.get('min')} ~ {range_info.get('max')}")
    numeric_ranges = profile.get("numeric_ranges") if isinstance(profile.get("numeric_ranges"), dict) else {}
    for column, range_info in numeric_ranges.items():
        if isinstance(range_info, dict):
            lines.append(f"  - {column} 范围：{range_info.get('min')} ~ {range_info.get('max')}")
    return lines


def _format_actions(actions: list[dict[str, Any]]) -> list[str]:
    if not actions:
        return []
    lines = ["", "- 可用动作："]
    for action in actions:
        action_type = action.get("type")
        available = "可用" if action.get("available") else "不可用"
        detail = ""
        if action_type == "fetch_page" and action.get("available"):
            detail = f"，默认 page_size={action.get('page_size')}"
        elif action.get("reason"):
            detail = f"，原因：{action.get('reason')}"
        lines.append(f"  - {action_type}: {available}{detail}")
    return lines


def _format_query_error(exc: DatabaseKnowledgeQueryError) -> str:
    lines = [f"🧮 数据库问数失败：{exc}"]
    sql = str(getattr(exc, "sql", "") or "").strip()
    if sql:
        lines.extend(["", "生成 SQL：", "```sql", sql, "```"])
    return "\n".join(lines)


def _emit_database_span(stage: str, payload: dict[str, Any], *, metadata: dict[str, Any] | None = None) -> None:
    try:
        from graph.trace_collector import get_current_trace_collector
    except Exception:
        return
    collector = get_current_trace_collector()
    if collector is None:
        return
    span_metadata = {
        "database_stage": stage,
        "harness": {
            "mechanism": "database_knowledge_query",
            "pillars": [
                {"name": "semantic_assets", "role": "business_semantics"},
                {"name": "table_router", "role": "scope"},
                {"name": "vanna", "role": "sql_generation"},
                {"name": "readonly_sql_runner", "role": "execution"},
            ],
        },
    }
    if metadata:
        span_metadata.update(metadata)
    collector.add_custom_span(
        f"database.{stage}",
        payload,
        span_type="database",
        metadata=span_metadata,
    )


def _preview_rows(rows: list[dict[str, Any]], *, limit: int = 20) -> list[dict[str, Any]]:
    return rows[:limit]


def _emit_trace_spans(result: DatabaseQueryResult) -> None:
    route_debug = summarize_table_route(result.route)
    references = result.references or {}
    execution = result.execution
    timings = result.stage_timings or {}

    def stage_metadata(stage: str, extra: dict[str, Any] | None = None) -> dict[str, Any]:
        metadata = {
            "database_source_id": result.route.database_source_id,
            "duration_ms": timings.get(f"{stage}_ms"),
            "stage_timings": timings,
        }
        if extra:
            metadata.update(extra)
        return metadata

    _emit_database_span(
        "semantic_assets",
        {
            **(result.semantic_assets or {}),
            "duration_ms": timings.get("semantic_assets_ms"),
        },
        metadata=stage_metadata("semantic_assets"),
    )
    _emit_database_span(
        "router",
        {
            "summary": route_debug,
            "prompt_context": result.route.prompt_context,
            "selected_tables": result.route.table_names,
            "available_tables": result.route.available_tables,
            "duration_ms": timings.get("router_ms"),
        },
        metadata=stage_metadata("router"),
    )
    _emit_database_span(
        "vanna_references",
        {
            "ddl": references.get("ddl"),
            "documentation": references.get("documentation"),
            "sql_examples": references.get("sql_examples"),
            "duration_ms": timings.get("vanna_references_ms"),
        },
        metadata=stage_metadata("vanna_references"),
    )
    _emit_database_span(
        "vanna_entities",
        {
            **(references.get("entities") if isinstance(references.get("entities"), dict) else {}),
            "duration_ms": timings.get("vanna_references_ms"),
        },
        metadata=stage_metadata("vanna_references"),
    )
    _emit_database_span(
        "sql_generation",
        {
            "question": result.question,
            "sql": result.sql,
            "source": result.source,
            "tables": result.route.table_names,
            "duration_ms": timings.get("sql_generation_ms"),
        },
        metadata=stage_metadata("sql_generation"),
    )
    _emit_database_span(
        "sql_execution",
        {
            "columns": execution.columns,
            "row_count": execution.row_count,
            "total_row_count": execution.total_row_count or execution.row_count,
            "preview_count": execution.preview_count if execution.preview_count is not None else len(execution.rows),
            "omitted_count": execution.omitted_count,
            "is_complete": execution.is_complete,
            "limited": execution.limited,
            "estimated_tokens": execution.estimated_tokens,
            "rows_preview": _preview_rows(execution.rows, limit=20),
            "profile": execution.profile,
            "result_id": execution.result_id,
            "result_store": execution.result_store,
            "actions": execution.actions,
            "llm_guardrail": execution.llm_guardrail,
            "duration_ms": timings.get("sql_execution_ms"),
            "stage_timings": timings,
        },
        metadata=stage_metadata("sql_execution"),
    )


def _format_result(result: DatabaseQueryResult) -> str:
    execution = result.execution
    result_size = f"{execution.row_count} 行"
    if execution.is_complete:
        result_size += "（全部）"
    else:
        result_size += f"（展示 {execution.preview_count or len(execution.rows)} 行，省略 {execution.omitted_count} 行）"
    lines = [
        "🧮 数据库问数结果",
        f"- 数据源：{result.source.get('name')}",
        f"- 表：{', '.join(result.route.table_names)}",
        f"- 结果：{result_size}",
        f"- 完整性：{'完整明细已进入模型上下文' if execution.is_complete else '预览明细，不能据此判断未展示类别不存在'}",
    ]
    semantic_assets = result.semantic_assets or {}
    matched_assets = semantic_assets.get("matched") if isinstance(semantic_assets.get("matched"), list) else []
    if matched_assets:
        asset_names = [
            f"{item.get('name')}({item.get('type')})"
            for item in matched_assets[:8]
            if isinstance(item, dict) and item.get("name")
        ]
        lines.append(f"- 语义资产：已注入 {len(matched_assets)} 个，{', '.join(asset_names)}")
    else:
        lines.append("- 语义资产：本轮未命中，SQL 未获得度量值/维度正文约束")
    if result.stage_timings:
        total_seconds = (result.stage_timings.get("total_ms") or 0) / 1000
        sql_seconds = (result.stage_timings.get("sql_execution_ms") or 0) / 1000
        generation_seconds = (result.stage_timings.get("sql_generation_ms") or 0) / 1000
        semantic_seconds = (result.stage_timings.get("semantic_assets_ms") or 0) / 1000
        lines.append(
            f"- 耗时：总计 {total_seconds:.2f}s，语义资产 {semantic_seconds:.2f}s，"
            f"SQL生成 {generation_seconds:.2f}s，SQL执行 {sql_seconds:.2f}s"
        )
    if execution.result_id:
        lines.extend(
            [
                f"- result_id：{execution.result_id}",
                f"- 持久化：{execution.result_store.get('artifact_path')}（过期时间：{execution.result_store.get('expires_at')}）",
            ]
        )
    if execution.llm_guardrail:
        lines.append(f"- Guardrail：{execution.llm_guardrail}")
    lines.extend(_format_profile(execution.profile))
    lines.extend(_format_actions(execution.actions))
    lines.extend(
        [
            "",
            "- SQL：",
            "```sql",
            result.sql,
            "```",
            "",
            *_markdown_table(execution.rows, execution.columns, max_rows=len(execution.rows) or 20),
        ]
    )
    return "\n".join(lines)


class DatabaseKnowledgeQueryTool(BaseTool):
    name: str = "database_knowledge_query"
    description: str = (
        "Analyze configured PostgreSQL database tables using PuddingClaw's internal Vanna NL2SQL service. "
        "Use this for structured database questions, BI-style metrics, SQL-backed analysis, aggregations, "
        "filters, rankings, trends, and entity-aware questions over configured database sources. "
        "For business questions, call it once with the user's original question; its internal table router "
        "already selects allowed tables and injects schema/context for SQL generation. "
        "Do not make preliminary calls that merely list tables, inspect schema, enumerate brands/categories, "
        "or discover fields unless the user explicitly asks for metadata. "
        "Do not use it for Excel/CSV files; use pandas_knowledge_query for spreadsheets. "
        "Do not use it for PDF/Markdown document QA; use llamaindex_knowledge_query for documents."
    )
    args_schema: Type[BaseModel] = DatabaseKnowledgeInput
    risk_level: str = "moderate"
    base_dir: str = ""

    class Config:
        arbitrary_types_allowed = True

    async def _query(
        self,
        *,
        question: str,
        database_source_id: str | None = None,
        table_names: list[str] | None = None,
        model_id: str | None = None,
        measure_ids: list[str] | None = None,
        limit: int = 100,
    ) -> str:
        request = DatabaseQueryRequest(
            question=question,
            database_source_id=database_source_id,
            table_names=table_names or [],
            model_id=model_id,
            measure_ids=measure_ids or [],
            limit=limit,
        )
        sessionmaker = get_sessionmaker()
        async with sessionmaker() as session:
            result = await query_database_knowledge(session, request)
        _emit_trace_spans(result)
        return _format_result(result)

    async def _arun(
        self,
        question: str,
        database_source_id: str | None = None,
        table_names: list[str] | None = None,
        model_id: str | None = None,
        measure_ids: list[str] | None = None,
        limit: int = 100,
    ) -> str:
        try:
            return await self._query(
                question=question,
                database_source_id=database_source_id,
                table_names=table_names,
                model_id=model_id,
                measure_ids=measure_ids,
                limit=limit,
            )
        except DatabaseKnowledgeQueryError as exc:
            return _format_query_error(exc)
        except Exception as exc:
            return f"🧮 数据库问数失败：{type(exc).__name__}: {exc}"

    def _run(
        self,
        question: str,
        database_source_id: str | None = None,
        table_names: list[str] | None = None,
        model_id: str | None = None,
        measure_ids: list[str] | None = None,
        limit: int = 100,
    ) -> str:
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(
                self._arun(
                    question=question,
                    database_source_id=database_source_id,
                    table_names=table_names,
                    model_id=model_id,
                    measure_ids=measure_ids,
                    limit=limit,
                )
            )
        return "🧮 数据库问数失败：当前运行环境不支持同步调用，请使用异步工具调用。"


class DatabaseQueryResultPageInput(BaseModel):
    result_id: str = Field(description="Persisted database query result id returned by database_knowledge_query.")
    page: int = Field(default=1, ge=1, description="1-based page number.")
    page_size: int | None = Field(default=None, ge=1, le=5000, description="Optional page size.")


class DatabaseQueryResultPageTool(BaseTool):
    name: str = "database_query_result_page"
    description: str = (
        "Fetch a page from a persisted database_knowledge_query result_id. "
        "Use this after database_knowledge_query returns preview-only detail rows and the user asks for row-level details."
    )
    args_schema: Type[BaseModel] = DatabaseQueryResultPageInput
    risk_level: str = "safe"

    class Config:
        arbitrary_types_allowed = True

    async def _arun(self, result_id: str, page: int = 1, page_size: int | None = None) -> str:
        try:
            sessionmaker = get_sessionmaker()
            async with sessionmaker() as session:
                page_result = await get_query_result_page(session, result_id, page=page, page_size=page_size)
        except QueryResultStoreError as exc:
            return f"🧮 查询结果分页读取失败：{exc}"
        except Exception as exc:
            return f"🧮 查询结果分页读取失败：{type(exc).__name__}: {exc}"

        rows = page_result.get("rows") or []
        columns = page_result.get("columns") or []
        if page_result.get("expired"):
            return f"🧮 查询结果已过期：{page_result.get('message')}"
        lines = [
            "🧮 数据库问数分页结果",
            f"- result_id：{page_result.get('result_id')}",
            f"- 总行数：{page_result.get('row_count')}",
            f"- 当前页：{page_result.get('page')}，每页：{page_result.get('page_size')}",
            f"- has_next：{page_result.get('has_next')}",
            f"- 过期时间：{page_result.get('expires_at')}",
            "",
            *_markdown_table(rows, columns, max_rows=len(rows) or 20),
        ]
        return "\n".join(lines)

    def _run(self, result_id: str, page: int = 1, page_size: int | None = None) -> str:
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(self._arun(result_id=result_id, page=page, page_size=page_size))
        return "🧮 查询结果分页读取失败：当前运行环境不支持同步调用，请使用异步工具调用。"


def create_database_knowledge_tool(base_dir: Path) -> BaseTool:
    return [
        DatabaseKnowledgeQueryTool(base_dir=str(base_dir)),
        DatabaseQueryResultPageTool(),
    ]
