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
from graph.session_manager import session_manager

from .formatting import format_actions, format_profile, markdown_table
from .models import DatabaseSqlExecuteInput
from .scope import resolve_database_source_scope
from .spans import emit_database_span, preview_rows


class DatabaseSqlExecuteTool(BaseTool):
    name: str = "database_sql_execute"
    description: str = (
        "Execute explicit read-only PostgreSQL SQL against a configured database source. "
        "Use either the legacy database_sql_generate + database_sql_validate_legacy pair, or the Agent pair "
        "database_sql_validate + sql_submission_id/agent_validation_receipt_id. Never execute an unregistered raw SQL. "
        "For the legacy path, generation_id is mandatory and its registered SQL is loaded server-side; omit the SQL "
        "argument. Its validation_receipt_id must match the generation SQL hash. For the Agent path, the paired Receipt "
        "must match the registered submission hash and current scope. Semantic changes on the legacy path must go through "
        "the natural-language revision HITL flow. "
        "A preview-only result is persisted and returned with a qr_* result_id only when result storage is enabled "
        "and the complete row count does not exceed the configured result_materialization_row_cap. The limit argument "
        "controls model preview rows, not that persistence cap. If the output has no result_id because the row cap was "
        "exceeded, do not call result-page/source tools or retry an old ID: narrow/aggregate the query, or raise the "
        "configured cap, then execute the SQL again. When a result_id is returned, use database_query_result_page only "
        "to inspect rows, or database_query_result_source followed by materialize_source_ref to place the complete "
        "result in a file/typed slot without model-context transfer."
    )
    args_schema: type[BaseModel] = DatabaseSqlExecuteInput
    risk_level: str = "moderate"
    session_id: str = ""
    query_id: str = ""

    class Config:
        arbitrary_types_allowed = True

    async def _arun(
        self,
        sql: str = "",
        generation_id: str = "",
        validation_receipt_id: str = "",
        sql_submission_id: str = "",
        agent_validation_receipt_id: str = "",
        database_source_id: str | None = None,
        table_names: list[str] | None = None,
        limit: int = 100,
        timeout_ms: int | None = None,
        runtime: ToolRuntime | None = None,
    ) -> str:
        question = "显式 SQL 执行"
        context: dict[str, object] = {}
        submission = None
        receipt = None
        generation = None
        if self.session_id:
            raw_context = getattr(runtime, "context", None)
            context = raw_context if isinstance(raw_context, dict) else {}
            if sql_submission_id:
                if not str(context.get("run_id") or "") and not str(context.get("goal_id") or ""):
                    return "🧮 SQL 执行失败：Agent submission 缺少当前 Run/Goal scope。"
                submission = database_sql_revision_resume_registry.get_submission(
                    sql_submission_id,
                    session_id=self.session_id,
                    query_id=self.query_id,
                    run_id=str(context.get("run_id") or ""),
                    goal_id=str(context.get("goal_id") or ""),
                    goal_revision=context.get("goal_revision"),
                )
                if submission is None:
                    return "🧮 SQL 执行失败：Agent submission 不存在或不属于当前 Session/Run/Goal。"
                receipt = database_sql_revision_resume_registry.get_submission_validation_receipt(
                    agent_validation_receipt_id or validation_receipt_id,
                    session_id=self.session_id,
                    submission_id=submission.id,
                    query_id=self.query_id,
                    run_id=str(context.get("run_id") or ""),
                    goal_id=str(context.get("goal_id") or ""),
                    goal_revision=context.get("goal_revision"),
                )
                if receipt is None or receipt.sql_sha256 != submission.sql_sha256:
                    return "🧮 SQL 执行失败：Agent submission 与 Validation Receipt 不匹配或 Receipt 已失效。"
                sql = submission.sql
                question = str(submission.request.get("trusted_question") or "Agent SQL 查询")
                database_source_id = submission.request.get("database_source_id")
                table_names = list(submission.request.get("table_names") or [])
                if not getattr(session_manager, "is_initialized", False):
                    return "🧮 SQL 执行失败：当前 permission policy 不可用，拒绝使用旧 Agent Receipt。"
                try:
                    current_epoch = int(session_manager.get_permission_policy(self.session_id)["policy_epoch"])
                except (FileNotFoundError, KeyError, RuntimeError, TypeError, ValueError):
                    return "🧮 SQL 执行失败：当前 permission policy 不可用，拒绝使用旧 Agent Receipt。"
                if current_epoch != receipt.permission_epoch:
                    return "🧮 SQL 执行失败：当前 permission epoch 已变化，旧 Agent Receipt 已失效。"
            else:
                generation = database_sql_revision_resume_registry.get_generation(
                    generation_id,
                    session_id=self.session_id,
                    query_id=self.query_id,
                    run_id=str(context.get("run_id") or ""),
                    goal_id=str(context.get("goal_id") or ""),
                    goal_revision=context.get("goal_revision"),
                )
                if generation is None:
                    return "🧮 SQL 执行失败：Agent 模式必须提供当前会话有效的 generation_id。请先调用 database_sql_generate。"
                receipt = database_sql_revision_resume_registry.get_validation_receipt(
                    validation_receipt_id,
                    session_id=self.session_id,
                    query_id=self.query_id,
                    run_id=str(context.get("run_id") or ""),
                    goal_id=str(context.get("goal_id") or ""),
                    goal_revision=context.get("goal_revision"),
                )
                if receipt is None:
                    return "🧮 SQL 执行失败：缺少当前会话有效的 validation_receipt_id。请先用该 generation_id 调用 database_sql_validate_legacy。"
                if receipt.generation_id != generation.id or receipt.sql_sha256 != generation.sql_sha256:
                    return "🧮 SQL 执行失败：ValidationReceipt 与 generation_id 或 SQL hash 不匹配。请重新校验当前 generation。"
                if receipt.semantic_validation_status != "passed":
                    return "🧮 SQL 执行失败：ValidationReceipt 未通过语义校验。"
                sql = generation.result.sql
                question = generation.result.question
                database_source_id = generation.request.get("database_source_id")
                table_names = list(generation.request.get("table_names") or generation.result.route.table_names)
        try:
            if submission is not None:
                source, public_source, allowed_tables = await resolve_database_source_scope(
                    database_source_id,
                    table_names,
                    enforce_selected_tables=True,
                )
            else:
                # Preserve the legacy resolver call shape for integrations;
                # the resolver itself still enforces selected_tables.
                source, public_source, allowed_tables = await resolve_database_source_scope(
                    database_source_id,
                    table_names,
                )
            if self.session_id and receipt is not None and (
                str(public_source.get("id") or "") != receipt.database_source_id
                or set(allowed_tables) != set(receipt.allowed_tables)
            ):
                return (
                    "🧮 SQL 执行失败：当前数据源/授权表范围与 ValidationReceipt 不一致。"
                    "请重新调用对应的 SQL 校验工具生成新 Receipt。"
                )
            execution = await run_readonly_sql(
                source,
                sql,
                allowed_tables=allowed_tables,
                limit=limit,
                timeout_ms=timeout_ms,
                allow_unregistered_functions=submission is not None,
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
                        tool_call_id=str(getattr(runtime, "tool_call_id", "") or ""),
                        source_query_id=str(context.get("query_id") or ""),
                        source_run_id=str(context.get("run_id") or ""),
                        producer_receipt_ids=(
                            [receipt.id] if self.session_id else []
                        ),
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
        if self.session_id and receipt is not None:
            if submission is not None:
                database_sql_revision_resume_registry.attest_submission_execution(
                    receipt=receipt,
                    submission=submission,
                    row_count=execution.total_row_count or execution.row_count,
                )
            elif generation is not None:
                database_sql_revision_resume_registry.attest_execution(
                    receipt=receipt,
                    generation=generation,
                    row_count=execution.total_row_count or execution.row_count,
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
            if submission is not None:
                lines.extend(["- SQL 来源：Agent submission 登记结果", f"- sql_submission_id：{submission.id}", f"- validation_receipt_id：{receipt.id}", f"- sql_sha256：{submission.sql_sha256}"])
            elif generation is not None:
                lines.extend(["- SQL 来源：generation_id 登记结果", f"- generation_id：{generation.id}", f"- validation_receipt_id：{receipt.id}", f"- sql_sha256：{generation.sql_sha256}"])
        lines.append(f"- 结果：{result_size}")
        if execution.result_id:
            lines.extend(
                [
                    f"- result_id：{execution.result_id}",
                    f"- 持久化：{execution.result_store.get('artifact_path')}"
                    f"（过期时间：{execution.result_store.get('expires_at')}）",
                    f"- artifact_sha256：{execution.result_store.get('artifact_sha256')}",
                    "- 完整结果落文件：先调用 database_query_result_source，"
                    "再调用 materialize_source_ref（无需逐页穿过模型上下文）",
                ]
            )
        elif persistence_error:
            lines.append(f"- 持久化失败：{persistence_error}，本次只能返回预览结果")
        lines.extend(format_profile(execution.profile))
        lines.extend(format_actions(execution.actions))
        lines.extend([
            "",
            "```sql",
            validate_readonly_sql(
                sql,
                allowed_tables=allowed_tables,
                allow_unregistered_functions=submission is not None,
            ),
            "```",
            "",
        ])
        lines.extend(markdown_table(execution.rows, execution.columns, max_rows=len(execution.rows) or 20))
        return "\n".join(lines)

    def _run(
        self,
        sql: str = "",
        generation_id: str = "",
        validation_receipt_id: str = "",
        sql_submission_id: str = "",
        agent_validation_receipt_id: str = "",
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
                    sql_submission_id=sql_submission_id,
                    agent_validation_receipt_id=agent_validation_receipt_id,
                    database_source_id=database_source_id,
                    table_names=table_names,
                    limit=limit,
                    timeout_ms=timeout_ms,
                )
            )
        return "🧮 SQL 执行失败：当前运行环境不支持同步调用，请使用异步工具调用。"
