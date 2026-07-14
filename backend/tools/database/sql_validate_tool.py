"""SQL validation tool for database Agent workflows."""

from __future__ import annotations

import asyncio

from langchain_core.tools import BaseTool
from pydantic import BaseModel

from analytics.nl2sql.sql_runner import SqlRunnerError, validate_readonly_sql
from graph.database_sql_revision_resume import database_sql_revision_resume_registry

from .models import DatabaseSqlValidateInput
from .scope import resolve_database_source_scope
from .spans import emit_database_span


class DatabaseSqlValidateTool(BaseTool):
    name: str = "database_sql_validate"
    description: str = (
        "Validate explicit SQL without executing it. Checks SELECT/WITH-only safety, multi-statement blocking, "
        "dangerous keywords, and authorized table scope from the configured database source. In Agent mode, "
        "generation_id is mandatory and its registered SQL is loaded server-side; omit the SQL argument."
    )
    args_schema: type[BaseModel] = DatabaseSqlValidateInput
    risk_level: str = "safe"
    session_id: str = ""

    class Config:
        arbitrary_types_allowed = True

    async def _arun(
        self,
        sql: str = "",
        generation_id: str = "",
        database_source_id: str | None = None,
        table_names: list[str] | None = None,
    ) -> str:
        if self.session_id:
            generation = database_sql_revision_resume_registry.get_generation(
                generation_id,
                session_id=self.session_id,
            )
            if generation is None:
                return "🧮 SQL 校验失败：Agent 模式必须提供当前会话有效的 generation_id。请先调用 database_sql_generate。"
            sql = generation.result.sql
            database_source_id = generation.request.get("database_source_id")
            table_names = list(generation.request.get("table_names") or generation.result.route.table_names)
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
        lines = [
            "🧮 SQL 校验通过",
            f"- 数据源：{public_source.get('name')} ({public_source.get('id')})",
            f"- 授权表：{', '.join(allowed_tables)}",
        ]
        if self.session_id:
            lines.append("- SQL 来源：generation_id 登记结果")
        lines.extend(["", "```sql", clean_sql, "```"])
        return "\n".join(lines)

    def _run(
        self,
        sql: str = "",
        generation_id: str = "",
        database_source_id: str | None = None,
        table_names: list[str] | None = None,
    ) -> str:
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(
                self._arun(
                    sql=sql,
                    generation_id=generation_id,
                    database_source_id=database_source_id,
                    table_names=table_names,
                )
            )
        return "🧮 SQL 校验失败：当前运行环境不支持同步调用，请使用异步工具调用。"
