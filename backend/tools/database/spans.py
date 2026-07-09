"""Trace span helpers for database Agent tools."""

from __future__ import annotations

from typing import Any

from analytics.nl2sql.schemas import DatabaseQueryResult
from analytics.nl2sql.table_router import summarize_table_route


def emit_database_span(stage: str, payload: dict[str, Any], *, metadata: dict[str, Any] | None = None) -> None:
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


def preview_rows(rows: list[dict[str, Any]], *, limit: int = 20) -> list[dict[str, Any]]:
    return rows[:limit]


def emit_trace_spans(result: DatabaseQueryResult) -> None:
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

    emit_database_span(
        "semantic_assets",
        {
            **(result.semantic_assets or {}),
            "duration_ms": timings.get("semantic_assets_ms"),
        },
        metadata=stage_metadata("semantic_assets"),
    )
    emit_database_span(
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
    emit_database_span(
        "vanna_references",
        {
            "ddl": references.get("ddl"),
            "documentation": references.get("documentation"),
            "sql_examples": references.get("sql_examples"),
            "duration_ms": timings.get("vanna_references_ms"),
        },
        metadata=stage_metadata("vanna_references"),
    )
    emit_database_span(
        "vanna_entities",
        {
            **(references.get("entities") if isinstance(references.get("entities"), dict) else {}),
            "duration_ms": timings.get("vanna_references_ms"),
        },
        metadata=stage_metadata("vanna_references"),
    )
    emit_database_span(
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
    emit_database_span(
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
            "rows_preview": preview_rows(execution.rows, limit=20),
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
