"""Explicit read-only SQL execution tool for database Agent workflows."""

from __future__ import annotations

import asyncio

from langchain.tools import ToolRuntime
from langchain_core.tools import BaseTool
from pydantic import BaseModel

from analytics.nl2sql.result_store import attach_persisted_query_result
from analytics.nl2sql.sql_runner import SqlRunnerError, run_readonly_sql, validate_readonly_sql
from db import get_sessionmaker
from graph.database_sql_revision_resume import database_sql_revision_resume_registry

from .formatting import format_actions, format_profile, markdown_table
from .models import DatabaseSqlExecuteInput
from .scope import resolve_database_source_scope
from .spans import emit_database_span, preview_rows


class DatabaseSqlExecuteTool(BaseTool):
    name: str = "database_sql_execute"
    description: str = (
        "Execute explicit read-only PostgreSQL SQL against a configured database source. "
        "Use only after database_sql_generate. In Agent mode, generation_id is mandatory and its registered SQL is "
        "loaded server-side; omit the SQL argument. A validation_receipt_id from database_sql_validate is also mandatory "
        "and must match the generation SQL hash. Semantic changes must go through the natural-language revision HITL flow. "
        "When the result is preview-only, the full materialized rows are persisted and returned with a result_id; "
        "use database_query_result_page to fetch subsequent pages."
    )
    args_schema: type[BaseModel] = DatabaseSqlExecuteInput
    risk_level: str = "moderate"
    session_id: str = ""

    class Config:
        arbitrary_types_allowed = True

    async def _arun(
        self,
        sql: str = "",
        generation_id: str = "",
        validation_receipt_id: str = "",
        database_source_id: str | None = None,
        table_names: list[str] | None = None,
        limit: int = 100,
        timeout_ms: int | None = None,
        runtime: ToolRuntime | None = None,
    ) -> str:
        question = "显式 SQL 执行"
        if self.session_id:
            raw_context = getattr(runtime, "context", None)
            context = raw_context if isinstance(raw_context, dict) else {}
            generation = database_sql_revision_resume_registry.get_generation(
                generation_id,
                session_id=self.session_id,
                run_id=str(context.get("run_id") or ""),
                goal_id=str(context.get("goal_id") or ""),
                goal_revision=context.get("goal_revision"),
            )
            if generation is None:
                return "🧮 SQL 执行失败：Agent 模式必须提供当前会话有效的 generation_id。请先调用 database_sql_generate。"
            receipt = database_sql_revision_resume_registry.get_validation_receipt(
                validation_receipt_id,
                session_id=self.session_id,
                run_id=str(context.get("run_id") or ""),
                goal_id=str(context.get("goal_id") or ""),
                goal_revision=context.get("goal_revision"),
            )
            if receipt is None:
                return (
                    "🧮 SQL 执行失败：缺少当前会话有效的 validation_receipt_id。"
                    "请先用该 generation_id 调用 database_sql_validate。"
                )
            if (
                receipt.generation_id != generation.id
                or receipt.sql_sha256 != generation.sql_sha256
            ):
                return (
                    "🧮 SQL 执行失败：ValidationReceipt 与 generation_id 或 SQL hash 不匹配。"
                    "请重新校验当前 generation，不要执行或手工改写 SQL。"
                )
            if receipt.semantic_validation_status != "passed":
                return (
                    "🧮 SQL 执行失败：ValidationReceipt 未通过语义校验。"
                    "请让 Generator 根据结构化修复协议生成 child generation，再重新校验。"
                )
            sql = generation.result.sql
            question = generation.result.question
            database_source_id = generation.request.get("database_source_id")
            table_names = list(generation.request.get("table_names") or generation.result.route.table_names)
        try:
            source, public_source, allowed_tables = await resolve_database_source_scope(database_source_id, table_names)
            if self.session_id and (
                str(public_source.get("id") or "") != receipt.database_source_id
                or set(allowed_tables) != set(receipt.allowed_tables)
            ):
                return (
                    "🧮 SQL 执行失败：当前数据源/授权表范围与 ValidationReceipt 不一致。"
                    "请重新调用 database_sql_validate 生成新 Receipt。"
                )
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

        persistence_error = ""
        if not execution.is_complete:
            try:
                sessionmaker = get_sessionmaker()
                async with sessionmaker() as session:
                    await attach_persisted_query_result(
                        session,
                        execution,
                        question=question,
                        sql=sql,
                        session_id=self.session_id,
                    )
            except Exception as exc:
                persistence_error = type(exc).__name__
                execution.actions = [
                    {
                        "type": "fetch_page",
                        "available": False,
                        "reason": "result_store_error",
                    }
                ]

        emit_database_span(
            "sql_execute",
            {
                "source": public_source,
                "allowed_tables": allowed_tables,
                "sql": sql,
                "columns": execution.columns,
                "row_count": execution.row_count,
                "total_row_count": execution.total_row_count or execution.row_count,
                "preview_count": execution.preview_count,
                "omitted_count": execution.omitted_count,
                "is_complete": execution.is_complete,
                "rows_preview": preview_rows(execution.rows, limit=20),
                "profile": execution.profile,
                "result_id": execution.result_id,
                "result_store": execution.result_store,
                "actions": execution.actions,
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
        ]
        if self.session_id:
            lines.extend(
                [
                    "- SQL 来源：generation_id 登记结果",
                    f"- generation_id：{generation.id}",
                    f"- validation_receipt_id：{receipt.id}",
                    f"- sql_sha256：{generation.sql_sha256}",
                ]
            )
        lines.append(f"- 结果：{result_size}")
        if execution.result_id:
            lines.extend(
                [
                    f"- result_id：{execution.result_id}",
                    f"- 持久化：{execution.result_store.get('artifact_path')}"
                    f"（过期时间：{execution.result_store.get('expires_at')}）",
                ]
            )
        elif persistence_error:
            lines.append(f"- 持久化失败：{persistence_error}，本次只能返回预览结果")
        lines.extend(format_profile(execution.profile))
        lines.extend(format_actions(execution.actions))
        lines.extend(["", "```sql", validate_readonly_sql(sql, allowed_tables=allowed_tables), "```", ""])
        lines.extend(markdown_table(execution.rows, execution.columns, max_rows=len(execution.rows) or 20))
        return "\n".join(lines)

    def _run(
        self,
        sql: str = "",
        generation_id: str = "",
        validation_receipt_id: str = "",
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
                    generation_id=generation_id,
                    validation_receipt_id=validation_receipt_id,
                    database_source_id=database_source_id,
                    table_names=table_names,
                    limit=limit,
                    timeout_ms=timeout_ms,
                )
            )
        return "🧮 SQL 执行失败：当前运行环境不支持同步调用，请使用异步工具调用。"
