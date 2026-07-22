"""SQL validation tool for database Agent workflows."""

from __future__ import annotations

import asyncio

from langchain.tools import ToolRuntime
from langchain_core.tools import BaseTool
from pydantic import BaseModel

from analytics.nl2sql.guardrails import detect_guardrail_conflicts
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
        runtime: ToolRuntime | None = None,
    ) -> str:
        generation = None
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
                return "🧮 SQL 校验失败：Agent 模式必须提供当前会话有效的 generation_id。请先调用 database_sql_generate。"
            sql = generation.result.sql
            database_source_id = generation.request.get("database_source_id")
            table_names = list(generation.request.get("table_names") or generation.result.route.table_names)
        try:
            _source, public_source, allowed_tables = await resolve_database_source_scope(database_source_id, table_names)
            clean_sql = validate_readonly_sql(sql, allowed_tables=allowed_tables)
            semantic_guardrail_ids: list[str] = []
            semantic_evidence_refs: list[str] = []
            if generation is not None:
                semantic_evidence_refs = sorted(
                    {
                        str(item.get("id") or "")
                        for key in ("matched", "references")
                        for item in generation.result.semantic_assets.get(key) or []
                        if isinstance(item, dict) and item.get("id")
                    }
                )
                semantic_conflicts = detect_guardrail_conflicts(
                    clean_sql,
                    source_name=generation.result.route.source_name,
                    route=generation.result.route,
                    semantic_trace=generation.result.semantic_assets,
                    question=str(generation.request.get("question") or generation.result.question),
                )
                semantic_guardrail_ids = sorted({item.rule_id for item in semantic_conflicts})
                blocking_conflicts = [
                    item for item in semantic_conflicts if item.action in {"rewrite", "block"}
                ]
                if blocking_conflicts:
                    repair = "；".join(
                        f"{item.rule_id}：{item.message}" for item in blocking_conflicts
                    )
                    return (
                        "🧮 SQL 语义校验失败：当前 Generation 未满足确定性语义规则，未签发 Receipt。\n"
                        f"- generation_id：{generation.id}\n"
                        f"- 修复协议：使用该 generation_id 作为 parent_generation_id 调用 "
                        "database_sql_generate，并把以下内容原样作为技术 revision_instruction；"
                        "Generator 必须生成 child generation，Agent 不得修改 SQL。\n"
                        f"- 技术修复反馈：{repair}\n"
                        "- 有界恢复：同一语义缺口最多重新生成两次；仍失败时返回 semantic_profile_required，"
                        "不得重复执行或静默放行。"
                    )
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
            metadata={
                "database_source_id": public_source.get("id"),
                "generation_id": generation.id if generation is not None else None,
                "sql_sha256": generation.sql_sha256 if generation is not None else None,
            },
        )
        lines = [
            "🧮 SQL 校验通过",
            f"- 数据源：{public_source.get('name')} ({public_source.get('id')})",
            f"- 授权表：{', '.join(allowed_tables)}",
        ]
        if self.session_id:
            receipt = database_sql_revision_resume_registry.register_validation_receipt(
                generation=generation,
                database_source_id=str(public_source.get("id") or ""),
                allowed_tables=allowed_tables,
                semantic_guardrail_ids=semantic_guardrail_ids,
                semantic_evidence_refs=semantic_evidence_refs,
            )
            lines.extend(
                [
                    "- SQL 来源：generation_id 登记结果",
                    f"- generation_id：{generation.id}",
                    f"- sql_sha256：{generation.sql_sha256}",
                    f"- validation_receipt_id：{receipt.id}",
                    "- 语义校验：passed（已重放当前语义资产与 Guardrail）",
                    "- 语义证据：" + (", ".join(receipt.semantic_evidence_refs or []) or "generalized"),
                    "- 下一步：仅使用 generation_id 与 validation_receipt_id 调用 database_sql_execute。",
                ]
            )
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
