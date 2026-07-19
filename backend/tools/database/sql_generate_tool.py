"""SQL generation tool for database Agent workflows."""

from __future__ import annotations

import asyncio

from langchain.tools import ToolRuntime
from langchain_core.tools import BaseTool
from langgraph.types import interrupt
from pydantic import BaseModel

from analytics.nl2sql.schemas import DatabaseQueryRequest
from analytics.nl2sql.service import DatabaseKnowledgeQueryError, generate_database_sql
from analytics.nl2sql.table_router import summarize_table_route
from db import get_sessionmaker
from graph.database_sql_revision_resume import (
    RegisteredDatabaseSqlGeneration,
    database_sql_revision_resume_registry,
)

from .formatting import format_query_error
from .models import DatabaseSqlGenerateInput
from .spans import emit_database_span

_SEMANTIC_CONTRACT_PREVIEW_CHARS = 700


def _format_semantic_contract(semantic_assets: dict[str, object]) -> list[str]:
    """Expose the semantic evidence already used by the SQL generator."""
    analytics_model = semantic_assets.get("analytics_model")
    matched = semantic_assets.get("matched")
    references = semantic_assets.get("references")
    ordered = [
        *([item for item in references if isinstance(item, dict)] if isinstance(references, list) else []),
        *([item for item in matched if isinstance(item, dict)] if isinstance(matched, list) else []),
    ]
    if not ordered and not isinstance(analytics_model, dict):
        return []

    lines = [
        "- 权威语义口径：以下摘要来自生成器已加载的 Measure/Reference，当前 SQL 已按这些规则生成。",
        "  外层 Agent 不得凭字段名或常识直接覆盖；如用户明确改变口径，须携带新约束重新调用 database_sql_generate。",
    ]
    if isinstance(analytics_model, dict):
        lines.append(
            "  - analysis_model:"
            f"{analytics_model.get('id') or analytics_model.get('name')} (analysis_model)"
            + (f"，路径：{analytics_model.get('path')}" if analytics_model.get("path") else "")
        )
        preview = " ".join(str(analytics_model.get("body_preview") or "").split())
        if len(preview) > _SEMANTIC_CONTRACT_PREVIEW_CHARS:
            preview = preview[:_SEMANTIC_CONTRACT_PREVIEW_CHARS].rstrip() + "..."
        if preview:
            lines.append(f"    模型全局规则摘要：{preview}")
    for item in ordered:
        asset_id = str(item.get("id") or item.get("name") or "unknown")
        asset_type = str(item.get("type") or "semantic_asset")
        path = str(item.get("path") or "")
        preview = " ".join(str(item.get("body_preview") or "").split())
        if len(preview) > _SEMANTIC_CONTRACT_PREVIEW_CHARS:
            preview = preview[:_SEMANTIC_CONTRACT_PREVIEW_CHARS].rstrip() + "..."
        lines.append(f"  - {asset_id} ({asset_type})" + (f"，路径：{path}" if path else ""))
        if preview:
            lines.append(f"    摘要：{preview}")
    return lines


def _format_generation(
    generation: RegisteredDatabaseSqlGeneration,
    *,
    disposition: str = "generated",
) -> str:
    result = generation.result
    matched_assets = result.semantic_assets.get("matched") if isinstance(result.semantic_assets.get("matched"), list) else []
    asset_names = [
        f"{item.get('id') or item.get('name')}({item.get('type')})"
        for item in matched_assets
        if isinstance(item, dict)
    ]
    title = "🧮 SQL 生成结果（未执行）"
    if disposition == "rejected_revision":
        title = "🧮 用户拒绝修改，继续使用原 SQL 生成结果（未执行）"
    elif disposition == "approved_revision":
        title = "🧮 已按用户确认的自然语言约束重新生成 SQL（未执行）"
    lines = [
        title,
        f"- generation_id：{generation.id}",
        f"- 数据源：{result.source.get('name')} ({result.source.get('id')})",
        f"- 表：{', '.join(result.route.table_names)}",
        f"- 路由：{result.route.reason}，confidence={result.route.confidence:.2f}",
        f"- 语义资产：{', '.join(asset_names) if asset_names else '未命中（已进入模型泛化模式）'}",
    ]
    if disposition == "rejected_revision":
        lines.extend(
            [
                "- HITL 状态：已完成（resolved）",
                "- 用户决策：reject；保留原 generation 和原 SQL",
                "- 下一步：不要再次询问用户选择。立即仅使用 generation_id 调用 "
                "database_sql_validate，再调用 database_sql_execute。",
            ]
        )
    elif disposition == "approved_revision":
        lines.extend(
            [
                "- HITL 状态：已完成（resolved）",
                "- 用户决策：已确认自然语言修改；当前内容是重新生成后的新 SQL",
                "- 下一步：立即使用新的 generation_id 校验并执行当前 SQL。",
            ]
        )
    if result.guardrail_note:
        lines.append(f"- Guardrail：{result.guardrail_note}")
    if result.stage_timings:
        total_seconds = (result.stage_timings.get("total_ms") or 0) / 1000
        generation_seconds = (result.stage_timings.get("sql_generation_ms") or 0) / 1000
        lines.append(f"- 耗时：总计 {total_seconds:.2f}s，SQL生成 {generation_seconds:.2f}s")
    lines.extend(_format_semantic_contract(result.semantic_assets))
    lines.append(
        "- 执行约束：database_sql_validate/database_sql_execute 必须携带此 generation_id；"
        "Agent 模式无需回传 SQL，工具会从 generation_id 加载登记结果。"
    )
    lines.extend(["", "```sql", result.sql, "```"])
    return "\n".join(lines)


class DatabaseSqlGenerateTool(BaseTool):
    name: str = "database_sql_generate"
    description: str = (
        "Generate PostgreSQL SQL from a natural-language database question without executing it. "
        "Use this as the first step for database analysis when the Agent needs to inspect, validate, "
        "or execute SQL. It runs table routing, semantic asset injection, Vanna references, and SQL guardrails, "
        "then returns SQL plus its authoritative semantic contract. Do not manually rewrite semantics from a "
        "matched Measure/Reference. To propose a change, call this tool with parent_generation_id and a natural-language "
        "revision_instruction; the user then chooses agree, reject, or modify before this tool regenerates SQL."
    )
    args_schema: type[BaseModel] = DatabaseSqlGenerateInput
    risk_level: str = "moderate"
    session_id: str = ""
    query_id: str = ""

    class Config:
        arbitrary_types_allowed = True

    async def _arun(
        self,
        question: str,
        database_source_id: str | None = None,
        table_names: list[str] | None = None,
        model_id: str | None = None,
        measure_ids: list[str] | None = None,
        semantic_asset_ids: list[str] | None = None,
        selected_semantic_asset_ids: list[str] | None = None,
        parent_generation_id: str | None = None,
        revision_instruction: str | None = None,
        tool_call_id: str = "",
        runtime: ToolRuntime | None = None,
    ) -> str:
        state_model_id = ""
        if runtime is not None and isinstance(runtime.state, dict):
            state_model_id = str(runtime.state.get("analytics_model_id") or "").strip()
        effective_model_id = state_model_id or model_id
        selected_asset_ids = list(
            dict.fromkeys(selected_semantic_asset_ids or semantic_asset_ids or measure_ids or [])
        )
        if state_model_id and runtime is not None and isinstance(runtime.state, dict):
            allowed_asset_ids = {
                str(item).strip()
                for item in runtime.state.get("allowed_semantic_asset_ids") or []
                if str(item).strip()
            }
            invalid_asset_ids = [item for item in selected_asset_ids if item not in allowed_asset_ids]
            if invalid_asset_ids:
                return (
                    "🧮 SQL 生成失败：以下语义资产不属于当前分析模型或已被删除："
                    + ", ".join(invalid_asset_ids)
                )
        request_payload = {
            "question": question,
            "database_source_id": database_source_id,
            "table_names": table_names or [],
            "model_id": effective_model_id,
            "measure_ids": selected_asset_ids,
        }
        parent: RegisteredDatabaseSqlGeneration | None = None
        disposition = "generated"
        if parent_generation_id:
            parent = database_sql_revision_resume_registry.get_generation(
                parent_generation_id,
                session_id=self.session_id,
            )
            if parent is None:
                return "🧮 SQL 重新生成失败：parent_generation_id 不存在或不属于当前会话。"
            proposed = str(revision_instruction or "").strip()
            if not proposed:
                return "🧮 SQL 重新生成失败：必须提供自然语言 revision_instruction，不能提供 SQL。"
            revision_request = database_sql_revision_resume_registry.create_revision_request(
                generation=parent,
                proposed_revision_instruction=proposed,
                tool_call_id=tool_call_id,
                query_id=self.query_id,
            )
            decision = interrupt(
                {
                    "type": "database_sql_revision_request",
                    "request": revision_request,
                    "decisions": [
                        {"action": "agree"},
                        {"action": "reject"},
                        {"action": "modify"},
                    ],
                }
            )
            if not isinstance(decision, dict) or decision.get("action") == "reject":
                return _format_generation(parent, disposition="rejected_revision")
            approved_instruction = str(decision.get("revision_instruction") or "").strip()
            if not approved_instruction:
                return "🧮 SQL 重新生成失败：审批结果缺少自然语言修改说明。"
            request_payload = dict(parent.request)
            original_question = str(request_payload.get("question") or parent.result.question)
            request_payload["question"] = (
                f"原始问题：\n{original_question}\n\n"
                f"用户确认的本次口径补充：\n{approved_instruction}"
            )
            disposition = "approved_revision"
        elif revision_instruction:
            return "🧮 SQL 重新生成失败：revision_instruction 必须与 parent_generation_id 一起使用。"

        request = DatabaseQueryRequest(
            question=str(request_payload["question"]),
            database_source_id=request_payload.get("database_source_id"),
            table_names=list(request_payload.get("table_names") or []),
            model_id=request_payload.get("model_id"),
            measure_ids=list(request_payload.get("measure_ids") or []),
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
        generation_request = dict(parent.request) if parent is not None else dict(request_payload)
        approved_instruction = ""
        if parent is not None:
            approved_instruction = str(decision.get("revision_instruction") or "").strip()
        generation = database_sql_revision_resume_registry.register_generation(
            session_id=self.session_id,
            query_id=self.query_id,
            result=result,
            request=generation_request,
            parent_generation_id=parent.id if parent is not None else "",
            revision_instruction=approved_instruction,
        )
        return _format_generation(generation, disposition=disposition)

    def _run(
        self,
        question: str,
        database_source_id: str | None = None,
        table_names: list[str] | None = None,
        model_id: str | None = None,
        measure_ids: list[str] | None = None,
        semantic_asset_ids: list[str] | None = None,
        selected_semantic_asset_ids: list[str] | None = None,
        parent_generation_id: str | None = None,
        revision_instruction: str | None = None,
        runtime: ToolRuntime | None = None,
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
                    semantic_asset_ids=semantic_asset_ids,
                    selected_semantic_asset_ids=selected_semantic_asset_ids,
                    parent_generation_id=parent_generation_id,
                    revision_instruction=revision_instruction,
                    runtime=runtime,
                )
            )
        return "🧮 SQL 生成失败：当前运行环境不支持同步调用，请使用异步工具调用。"
