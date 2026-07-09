"""Legacy all-in-one database knowledge query tool."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Type

from langchain_core.tools import BaseTool
from pydantic import BaseModel

from analytics.nl2sql.schemas import DatabaseQueryRequest
from analytics.nl2sql.service import DatabaseKnowledgeQueryError, query_database_knowledge
from db import get_sessionmaker

from .formatting import format_query_error, format_result
from .models import DatabaseKnowledgeInput
from .spans import emit_trace_spans


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
        emit_trace_spans(result)
        return format_result(result)

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
            return format_query_error(exc)
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


def create_legacy_database_query_tool(base_dir: Path) -> DatabaseKnowledgeQueryTool:
    return DatabaseKnowledgeQueryTool(base_dir=str(base_dir))
