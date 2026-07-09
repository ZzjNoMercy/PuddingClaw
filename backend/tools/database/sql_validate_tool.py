"""SQL validation tool for database Agent workflows."""

from __future__ import annotations

import asyncio
from typing import Type

from langchain_core.tools import BaseTool
from pydantic import BaseModel

from analytics.nl2sql.sql_runner import SqlRunnerError, validate_readonly_sql

from .models import DatabaseSqlValidateInput
from .scope import resolve_database_source_scope
from .spans import emit_database_span


class DatabaseSqlValidateTool(BaseTool):
    name: str = "database_sql_validate"
    description: str = (
        "Validate explicit SQL without executing it. Checks SELECT/WITH-only safety, multi-statement blocking, "
        "dangerous keywords, and authorized table scope from the configured database source."
    )
    args_schema: Type[BaseModel] = DatabaseSqlValidateInput
    risk_level: str = "safe"

    class Config:
        arbitrary_types_allowed = True

    async def _arun(
        self,
        sql: str,
        database_source_id: str | None = None,
        table_names: list[str] | None = None,
    ) -> str:
        try:
            _source, public_source, allowed_tables = await resolve_database_source_scope(database_source_id, table_names)
            clean_sql = validate_readonly_sql(sql, allowed_tables=allowed_tables)
        except SqlRunnerError as exc:
            return f"🧮 SQL 校验失败：{exc}\n\n```sql\n{getattr(exc, 'sql', None) or sql}\n```"
        except Exception as exc:
            return f"🧮 SQL 校验失败：{type(exc).__name__}: {exc}"
        emit_database_span(
            "sql_validate",
            {
                "source": public_source,
                "allowed_tables": allowed_tables,
                "sql": clean_sql,
                "valid": True,
            },
            metadata={"database_source_id": public_source.get("id")},
        )
        return "\n".join(
            [
                "🧮 SQL 校验通过",
                f"- 数据源：{public_source.get('name')} ({public_source.get('id')})",
                f"- 授权表：{', '.join(allowed_tables)}",
                "",
                "```sql",
                clean_sql,
                "```",
            ]
        )

    def _run(self, sql: str, database_source_id: str | None = None, table_names: list[str] | None = None) -> str:
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(self._arun(sql=sql, database_source_id=database_source_id, table_names=table_names))
        return "🧮 SQL 校验失败：当前运行环境不支持同步调用，请使用异步工具调用。"
