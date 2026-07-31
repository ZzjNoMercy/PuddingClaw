"""Schema inspection tool for database Agent workflows."""

from __future__ import annotations

import asyncio

from langchain.tools import ToolRuntime
from langchain_core.tools import BaseTool
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from analytics.nl2sql.eav_evidence import extract_eav_type_names
from analytics.nl2sql.sql_runner import run_readonly_sql
from graph.database_schema_evidence import database_schema_evidence_registry
from graph.database_sql_revision_resume import database_sql_revision_resume_registry
from knowledge.database_sources import database_source_url

from .formatting import markdown_table
from .models import DatabaseSchemaInspectInput
from .scope import normalize_table_name_for_match, quote_table_identifier, resolve_database_source_scope, table_in_scope
from .spans import emit_database_span, preview_rows


class DatabaseSchemaInspectTool(BaseTool):
    name: str = "database_schema_inspect"
    description: str = (
        "Inspect configured database metadata without Vanna: list selected tables, columns, EAV type_name values, "
        "or sample rows. type_names mode returns a server-owned schema_evidence_receipt_id; when diagnosing an "
        "existing SQL generation, pass parent_generation_id and use the receipt for an automatic physical repair. "
        "Use for schema/debug questions, not for answering business metrics directly."
    )
    args_schema: type[BaseModel] = DatabaseSchemaInspectInput
    risk_level: str = "safe"
    session_id: str = ""
    query_id: str = ""

    class Config:
        arbitrary_types_allowed = True

    async def _arun(
        self,
        mode: str = "tables",
        database_source_id: str | None = None,
        table_name: str | None = None,
        search: str | None = None,
        limit: int = 100,
        parent_generation_id: str | None = None,
        runtime: ToolRuntime | None = None,
    ) -> str:
        try:
            source, public_source, allowed_tables = await resolve_database_source_scope(database_source_id, [])
            normalized_mode = str(mode or "tables").strip().lower()
            if normalized_mode == "tables":
                rows = [{"table": table, "source": public_source.get("name")} for table in allowed_tables]
                emit_database_span(
                    "schema_inspect",
                    {
                        "mode": normalized_mode,
                        "source": public_source,
                        "allowed_tables": allowed_tables,
                        "rows_preview": rows[:20],
                    },
                    metadata={"database_source_id": public_source.get("id")},
                )
                return "\n".join(
                    [
                        "🧮 数据库结构",
                        f"- 数据源：{public_source.get('name')} ({public_source.get('id')})",
                        "",
                        *markdown_table(rows, ["table", "source"], max_rows=len(rows) or 20),
                    ]
                )
            if not table_name:
                raise RuntimeError(f"{normalized_mode} 模式需要 table_name。")
            if not table_in_scope(table_name, allowed_tables):
                raise RuntimeError(f"表 {table_name} 不在授权表范围内：{', '.join(allowed_tables)}")

            if normalized_mode == "sample":
                quoted_table = quote_table_identifier(table_name)
                execution = await run_readonly_sql(
                    source,
                    f"SELECT * FROM {quoted_table}",
                    allowed_tables=allowed_tables,
                    limit=limit,
                )
                emit_database_span(
                    "schema_inspect",
                    {
                        "mode": normalized_mode,
                        "source": public_source,
                        "table": table_name,
                        "columns": execution.columns,
                        "row_count": execution.row_count,
                        "rows_preview": preview_rows(execution.rows, limit=20),
                    },
                    metadata={"database_source_id": public_source.get("id")},
                )
                return "\n".join(
                    [
                        "🧮 表样例",
                        f"- 数据源：{public_source.get('name')} ({public_source.get('id')})",
                        f"- 表：{table_name}",
                        "",
                        *markdown_table(execution.rows, execution.columns, max_rows=len(execution.rows) or 20),
                    ]
                )

            engine = create_async_engine(database_source_url(source), pool_pre_ping=True)
            try:
                async with engine.connect() as conn:
                    table_only = normalize_table_name_for_match(table_name)
                    table_parts = [
                        part.strip().strip('"')
                        for part in str(table_name).split(".")
                        if part.strip()
                    ]
                    table_schema = table_parts[-2] if len(table_parts) >= 2 else "public"
                    if normalized_mode == "columns":
                        result = await conn.execute(
                            text(
                                """
                                SELECT column_name, data_type, is_nullable
                                FROM information_schema.columns
                                WHERE table_schema = :table_schema
                                  AND table_name = :table_name
                                ORDER BY ordinal_position
                                """
                            ),
                            {"table_schema": table_schema, "table_name": table_only},
                        )
                        rows = [dict(row) for row in result.mappings().all()]
                        emit_database_span(
                            "schema_inspect",
                            {
                                "mode": normalized_mode,
                                "source": public_source,
                                "table": table_name,
                                "columns": ["column_name", "data_type", "is_nullable"],
                                "row_count": len(rows),
                                "rows_preview": rows[:20],
                            },
                            metadata={"database_source_id": public_source.get("id")},
                        )
                        return "\n".join(
                            [
                                "🧮 表字段",
                                f"- 数据源：{public_source.get('name')} ({public_source.get('id')})",
                                f"- 表：{table_name}",
                                "",
                                *markdown_table(rows, ["column_name", "data_type", "is_nullable"], max_rows=len(rows) or 20),
                            ]
                        )
                    if normalized_mode == "type_names":
                        result = await conn.execute(
                            text(
                                f"""
                                SELECT type_name, COUNT(*) AS count
                                FROM {quote_table_identifier(table_name)}
                                WHERE type_name IS NOT NULL
                                  AND (:search_text = '' OR type_name ILIKE '%' || :search_text || '%')
                                GROUP BY type_name
                                ORDER BY COUNT(*) DESC, type_name
                                LIMIT :limit_value
                                """
                            ),
                            {"search_text": str(search or ""), "limit_value": max(1, min(int(limit or 100), 1000))},
                        )
                        rows = [dict(row) for row in result.mappings().all()]
                        runtime_context = getattr(runtime, "context", None)
                        context = runtime_context if isinstance(runtime_context, dict) else {}
                        receipt = None
                        if parent_generation_id:
                            parent = database_sql_revision_resume_registry.get_generation(
                                parent_generation_id,
                                session_id=self.session_id,
                                run_id=str(context.get("run_id") or ""),
                                goal_id=str(context.get("goal_id") or ""),
                                goal_revision=context.get("goal_revision"),
                            )
                            if parent is None:
                                raise RuntimeError(
                                    "parent_generation_id 不存在或不属于当前 Session/Run/Goal。"
                                )
                            if str(parent.query_id or "") != str(self.query_id or ""):
                                raise RuntimeError("父 generation 与本次 schema 检查不属于同一 Query。")
                            if str(parent.result.source.get("id") or "") != str(public_source.get("id") or ""):
                                raise RuntimeError("父 generation 与本次 schema 检查不属于同一数据源。")
                            if not table_in_scope(table_name, list(parent.result.route.table_names)):
                                raise RuntimeError("父 generation 未授权本次检查的表。")
                            parent_type_names = sorted(extract_eav_type_names(parent.result.sql))
                            if not parent_type_names:
                                raise RuntimeError("父 SQL 没有可诊断的 vehicle_params.type_name 字面量。")
                            receipt = database_schema_evidence_registry.register(
                                session_id=self.session_id,
                                query_id=self.query_id,
                                run_id=str(context.get("run_id") or ""),
                                goal_id=str(context.get("goal_id") or ""),
                                goal_revision=context.get("goal_revision"),
                                parent_generation_id=str(parent_generation_id),
                                database_source_id=str(public_source.get("id") or ""),
                                table_name=str(table_name),
                                mode=normalized_mode,
                                search=str(search or ""),
                                rows=rows,
                                parent_sql_sha256=parent.sql_sha256,
                                parent_type_names=parent_type_names,
                            )
                        emit_database_span(
                            "schema_inspect",
                            {
                                "mode": normalized_mode,
                                "source": public_source,
                                "table": table_name,
                                "search": search or "",
                                "columns": ["type_name", "count"],
                                "row_count": len(rows),
                                "rows_preview": rows[:20],
                            },
                            metadata={"database_source_id": public_source.get("id")},
                        )
                        receipt_lines = (
                            [
                                f"- schema_evidence_receipt_id：{receipt['id']}",
                                f"- evidence_sha256：{receipt['sha256']}",
                            ]
                            if receipt
                            else ["- schema_evidence_receipt_id：未签发（诊断修复时需传 parent_generation_id）"]
                        )
                        return "\n".join(
                            [
                                "🧮 EAV type_name 枚举",
                                f"- 数据源：{public_source.get('name')} ({public_source.get('id')})",
                                f"- 表：{table_name}",
                                f"- 搜索：{search or '<empty>'}",
                                *receipt_lines,
                                "",
                                *markdown_table(rows, ["type_name", "count"], max_rows=len(rows) or 20),
                            ]
                        )
            finally:
                await engine.dispose()
            raise RuntimeError(f"不支持的 mode：{mode}")
        except Exception as exc:
            return f"🧮 数据库结构检查失败：{type(exc).__name__}: {exc}"

    def _run(
        self,
        mode: str = "tables",
        database_source_id: str | None = None,
        table_name: str | None = None,
        search: str | None = None,
        limit: int = 100,
        parent_generation_id: str | None = None,
    ) -> str:
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(
                self._arun(
                    mode=mode,
                    database_source_id=database_source_id,
                    table_name=table_name,
                    search=search,
                    limit=limit,
                    parent_generation_id=parent_generation_id,
                )
            )
        return "🧮 数据库结构检查失败：当前运行环境不支持同步调用，请使用异步工具调用。"
