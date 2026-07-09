"""SQL generation tool for database Agent workflows."""

from __future__ import annotations

import asyncio
from typing import Type

from langchain_core.tools import BaseTool
from pydantic import BaseModel

from analytics.nl2sql.schemas import DatabaseQueryRequest
from analytics.nl2sql.service import DatabaseKnowledgeQueryError, generate_database_sql
from analytics.nl2sql.table_router import summarize_table_route
from db import get_sessionmaker

from .formatting import format_query_error
from .models import DatabaseSqlGenerateInput
from .spans import emit_database_span


class DatabaseSqlGenerateTool(BaseTool):
    name: str = "database_sql_generate"
    description: str = (
        "Generate PostgreSQL SQL from a natural-language database question without executing it. "
        "Use this as the first step for database analysis when the Agent needs to inspect, validate, "
        "or revise SQL before execution. It runs table routing, semantic asset injection, Vanna references, "
        "and SQL guardrails, then returns SQL plus traceable context."
    )
    args_schema: Type[BaseModel] = DatabaseSqlGenerateInput
    risk_level: str = "moderate"

    class Config:
        arbitrary_types_allowed = True

    async def _arun(
        self,
        question: str,
        database_source_id: str | None = None,
        table_names: list[str] | None = None,
        model_id: str | None = None,
        measure_ids: list[str] | None = None,
    ) -> str:
        request = DatabaseQueryRequest(
            question=question,
            database_source_id=database_source_id,
            table_names=table_names or [],
            model_id=model_id,
            measure_ids=measure_ids or [],
        )
        try:
            sessionmaker = get_sessionmaker()
            async with sessionmaker() as session:
                result = await generate_database_sql(session, request)
        except DatabaseKnowledgeQueryError as exc:
            return format_query_error(exc)
        except Exception as exc:
            return f"🧮 SQL 生成失败：{type(exc).__name__}: {exc}"

        emit_database_span(
            "sql_generate",
            {
                "question": result.question,
                "sql": result.sql,
                "source": result.source,
                "route": summarize_table_route(result.route),
                "semantic_assets": result.semantic_assets,
                "references": result.references,
                "guardrail_note": result.guardrail_note,
                "stage_timings": result.stage_timings,
            },
            metadata={
                "database_source_id": result.route.database_source_id,
                "stage_timings": result.stage_timings,
                "duration_ms": result.stage_timings.get("total_ms"),
            },
        )

        matched_assets = result.semantic_assets.get("matched") if isinstance(result.semantic_assets.get("matched"), list) else []
        asset_names = [
            f"{item.get('id') or item.get('name')}({item.get('type')})"
            for item in matched_assets[:8]
            if isinstance(item, dict)
        ]
        lines = [
            "🧮 SQL 生成结果（未执行）",
            f"- 数据源：{result.source.get('name')} ({result.source.get('id')})",
            f"- 表：{', '.join(result.route.table_names)}",
            f"- 路由：{result.route.reason}，confidence={result.route.confidence:.2f}",
            f"- 语义资产：{', '.join(asset_names) if asset_names else '未命中'}",
        ]
        if result.guardrail_note:
            lines.append(f"- Guardrail：{result.guardrail_note}")
        if result.stage_timings:
            total_seconds = (result.stage_timings.get("total_ms") or 0) / 1000
            generation_seconds = (result.stage_timings.get("sql_generation_ms") or 0) / 1000
            lines.append(f"- 耗时：总计 {total_seconds:.2f}s，SQL生成 {generation_seconds:.2f}s")
        lines.extend(["", "```sql", result.sql, "```"])
        return "\n".join(lines)

    def _run(
        self,
        question: str,
        database_source_id: str | None = None,
        table_names: list[str] | None = None,
        model_id: str | None = None,
        measure_ids: list[str] | None = None,
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
                )
            )
        return "🧮 SQL 生成失败：当前运行环境不支持同步调用，请使用异步工具调用。"
