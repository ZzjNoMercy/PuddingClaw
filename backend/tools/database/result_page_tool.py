"""Persisted database result pagination tool."""

from __future__ import annotations

import asyncio

from langchain.tools import ToolRuntime
from langchain_core.tools import BaseTool
from pydantic import BaseModel

from db import get_sessionmaker

from .formatting import markdown_table
from .models import DatabaseQueryResultPageInput
from .result_store import QueryResultStoreError, read_query_result_page


class DatabaseQueryResultPageTool(BaseTool):
    name: str = "database_query_result_page"
    description: str = (
        "Fetch a page from a persisted database query result_id. "
        "Use this after database_knowledge_query or database_sql_execute returns preview-only detail rows "
        "and the user asks for row-level details."
    )
    args_schema: type[BaseModel] = DatabaseQueryResultPageInput
    risk_level: str = "safe"

    class Config:
        arbitrary_types_allowed = True

    async def _arun(
        self,
        result_id: str,
        page: int = 1,
        page_size: int | None = None,
        runtime: ToolRuntime | None = None,
    ) -> str:
        try:
            context = (
                runtime.context
                if runtime is not None and isinstance(runtime.context, dict)
                else {}
            )
            sessionmaker = get_sessionmaker()
            async with sessionmaker() as session:
                page_result = await read_query_result_page(
                    session,
                    result_id,
                    page=page,
                    page_size=page_size,
                    session_id=str(context.get("session_id") or ""),
                )
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
            *markdown_table(rows, columns, max_rows=len(rows) or 20),
        ]
        return "\n".join(lines)

    def _run(
        self,
        result_id: str,
        page: int = 1,
        page_size: int | None = None,
        runtime: ToolRuntime | None = None,
    ) -> str:
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(
                self._arun(
                    result_id=result_id,
                    page=page,
                    page_size=page_size,
                    runtime=runtime,
                )
            )
        return "🧮 查询结果分页读取失败：当前运行环境不支持同步调用，请使用异步工具调用。"
