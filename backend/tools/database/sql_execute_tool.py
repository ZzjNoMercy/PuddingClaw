"""Explicit read-only SQL execution tool for database Agent workflows."""

from __future__ import annotations

import asyncio
from typing import Type

from langchain_core.tools import BaseTool
from pydantic import BaseModel

from analytics.nl2sql.sql_runner import SqlRunnerError, run_readonly_sql, validate_readonly_sql

from .formatting import format_profile, markdown_table
from .models import DatabaseSqlExecuteInput
from .scope import resolve_database_source_scope
from .spans import emit_database_span, preview_rows


class DatabaseSqlExecuteTool(BaseTool):
    name: str = "database_sql_execute"
    description: str = (
        "Execute explicit read-only PostgreSQL SQL against a configured database source. "
        "Use only after SQL is generated or manually written and ready to run. This tool does not call Vanna."
    )
    args_schema: Type[BaseModel] = DatabaseSqlExecuteInput
    risk_level: str = "moderate"

    class Config:
        arbitrary_types_allowed = True

    async def _arun(
        self,
        sql: str,
        database_source_id: str | None = None,
        table_names: list[str] | None = None,
        limit: int = 100,
        timeout_ms: int | None = None,
    ) -> str:
        try:
            source, public_source, allowed_tables = await resolve_database_source_scope(database_source_id, table_names)
            execution = await run_readonly_sql(
                source,
                sql,
                allowed_tables=allowed_tables,
                limit=limit,
                timeout_ms=timeout_ms,
            )
        except SqlRunnerError as exc:
            return f"🧮 SQL 执行失败：{exc}\n\n生成/执行 SQL：\n```sql\n{getattr(exc, 'sql', None) or sql}\n```"
        except Exception as exc:
            return f"🧮 SQL 执行失败：{type(exc).__name__}: {exc}"

        emit_database_span(
            "sql_execute",
            {
                "source": public_source,
                "allowed_tables": allowed_tables,
                "sql": sql,
                "columns": execution.columns,
                "row_count": execution.row_count,
                "preview_count": execution.preview_count,
                "is_complete": execution.is_complete,
                "rows_preview": preview_rows(execution.rows, limit=20),
                "profile": execution.profile,
            },
            metadata={"database_source_id": public_source.get("id")},
        )

        result_size = f"{execution.row_count} 行"
        if not execution.is_complete:
            result_size += f"（展示 {execution.preview_count or len(execution.rows)} 行，省略 {execution.omitted_count} 行）"
        lines = [
            "🧮 SQL 执行结果",
            f"- 数据源：{public_source.get('name')} ({public_source.get('id')})",
            f"- 授权表：{', '.join(allowed_tables)}",
            f"- 结果：{result_size}",
        ]
        lines.extend(format_profile(execution.profile))
        lines.extend(["", "```sql", validate_readonly_sql(sql, allowed_tables=allowed_tables), "```", ""])
        lines.extend(markdown_table(execution.rows, execution.columns, max_rows=len(execution.rows) or 20))
        return "\n".join(lines)

    def _run(
        self,
        sql: str,
        database_source_id: str | None = None,
        table_names: list[str] | None = None,
        limit: int = 100,
        timeout_ms: int | None = None,
    ) -> str:
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(
                self._arun(
                    sql=sql,
                    database_source_id=database_source_id,
                    table_names=table_names,
                    limit=limit,
                    timeout_ms=timeout_ms,
                )
            )
        return "🧮 SQL 执行失败：当前运行环境不支持同步调用，请使用异步工具调用。"
