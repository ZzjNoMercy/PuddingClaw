"""SQL generation tool for database Agent workflows."""

from __future__ import annotations

import asyncio
import re
from difflib import get_close_matches

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
_TECHNICAL_SQL_REVISION_PATTERN = re.compile(
    r"(?:\bSQL\b|\bEXISTS\b|\bJOIN\b|\bCTE\b|\bFILTER\b|\bILIKE\b|"
    r"\bGROUP\s+BY\b|\bDISTINCT\b|\bquery\b|超时|慢查询|性能|执行计划|"
    r"相关子查询|子查询|语法|括号|表别名|重写查询)",
    re.IGNORECASE,
)
_BUSINESS_SEMANTIC_CHANGE_PATTERN = re.compile(
    r"(?:业务口径|指标口径|分母|分子|统计口径|计算口径|"
    r"(?:新增|取消|包含|排除|改成|改为|只统计|仅统计|范围改为|粒度改为|维度改为)"
    r".{0,24}(?:指标|范围|筛选|时间|年份|能源|品牌|价格|车型|车系|款型|皮卡|分母|分子|粒度|维度))",
    re.IGNORECASE,
)


def _is_technical_sql_revision(instruction: str) -> bool:
    """Identify implementation repair that must not become business HITL."""

    normalized = " ".join(str(instruction or "").split())
    return bool(
        normalized
        and _TECHNICAL_SQL_REVISION_PATTERN.search(normalized)
        and not _BUSINESS_SEMANTIC_CHANGE_PATTERN.search(normalized)
    )


def _normalize_selected_semantic_asset_ids(
    selected_ids: list[str],
    allowed_ids: set[str],
) -> tuple[list[str], str | None]:
    """Resolve model-friendly bare ids against the authoritative model scope.

    The semantic index exposes namespaced ids such as ``measure:launch_cycle``.
    Models occasionally retain only the stable suffix. A unique suffix is safe
    to normalize server-side; ambiguous or unknown values remain fail-closed.
    """

    normalized: list[str] = []
    for raw_id in selected_ids:
        asset_id = str(raw_id or "").strip()
        if not asset_id:
            continue
        if asset_id in allowed_ids:
            resolved = asset_id
        else:
            suffix_matches = sorted(
                candidate
                for candidate in allowed_ids
                if candidate.rsplit(":", 1)[-1] == asset_id
            )
            if len(suffix_matches) == 1:
                resolved = suffix_matches[0]
            elif len(suffix_matches) > 1:
                return [], (
                    f"语义资产 ID“{asset_id}”存在多个候选："
                    + ", ".join(suffix_matches)
                    + "。请使用完整 namespaced id。"
                )
            else:
                close = get_close_matches(asset_id, sorted(allowed_ids), n=5, cutoff=0.25)
                candidates = close or sorted(allowed_ids)[:8]
                suffix = f" 当前模型可用候选：{', '.join(candidates)}。" if candidates else ""
                return [], f"语义资产 ID“{asset_id}”不属于当前分析模型或已被删除。{suffix}"
        if resolved not in normalized:
            normalized.append(resolved)
    return normalized, None


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
    elif disposition == "technical_repair":
        title = "🧮 SQL 技术修复已自动重生成（未执行）"
    lines = [
        title,
        f"- generation_id：{generation.id}",
        f"- sql_sha256：{generation.sql_sha256}",
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
    elif disposition == "technical_repair":
        lines.extend(
            [
                "- 修复类型：SQL 实现/性能修复；业务问题与语义资产保持不变",
                "- HITL 状态：无需业务口径确认",
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
        "- 执行约束：先用 generation_id 调用 database_sql_validate 获得 validation_receipt_id；"
        "再将两者一起传给 database_sql_execute。Agent 模式无需回传 SQL，工具会从服务器账本加载登记结果。"
    )
    lines.extend(["", "```sql", result.sql, "```"])
    return "\n".join(lines)


class DatabaseSqlGenerateTool(BaseTool):
    name: str = "database_sql_generate"
    description: str = (
        "Generate PostgreSQL SQL from a natural-language database question without executing it. "
        "Use this as the first step for database analysis when the Agent needs to inspect, validate, "
        "or execute SQL. It uses the original question for Vanna evidence/candidate retrieval, then applies semantic "
        "assets in a separate final refinement pass before SQL guardrails. Database entity evidence is authoritative "
        "for physical table/column/EAV values. It returns SQL plus its authoritative semantic contract. Do not "
        "manually rewrite semantics from a "
        "matched Measure/Reference. To propose a business-semantic change, call this tool with parent_generation_id and "
        "a natural-language revision_instruction; the user then chooses agree, reject, or modify. SQL timeout, syntax, "
        "query-shape, JOIN/CTE, or performance repair is technical and is automatically regenerated without business HITL."
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
            selected_asset_ids, normalization_error = _normalize_selected_semantic_asset_ids(
                selected_asset_ids,
                allowed_asset_ids,
            )
            if normalization_error:
                return "🧮 SQL 生成失败：" + normalization_error
        request_payload = {
            "question": question,
            "database_source_id": database_source_id,
            "table_names": table_names or [],
            "model_id": effective_model_id,
            "measure_ids": selected_asset_ids,
        }
        parent: RegisteredDatabaseSqlGeneration | None = None
        disposition = "generated"
        applied_instruction = ""
        if parent_generation_id:
            runtime_context = getattr(runtime, "context", None)
            context = runtime_context if isinstance(runtime_context, dict) else {}
            parent = database_sql_revision_resume_registry.get_generation(
                parent_generation_id,
                session_id=self.session_id,
                run_id=str(context.get("run_id") or ""),
                goal_id=str(context.get("goal_id") or ""),
                goal_revision=context.get("goal_revision"),
            )
            if parent is None:
                return "🧮 SQL 重新生成失败：parent_generation_id 不存在或不属于当前会话。"
            proposed = str(revision_instruction or "").strip()
            if not proposed:
                return "🧮 SQL 重新生成失败：必须提供自然语言 revision_instruction，不能提供 SQL。"
            request_payload = dict(parent.request)
            original_question = str(request_payload.get("question") or parent.result.question)
            if _is_technical_sql_revision(proposed):
                applied_instruction = proposed
                request_payload["question"] = (
                    f"原始业务问题（业务语义不可改变）：\n{original_question}\n\n"
                    f"上一版 SQL：\n{parent.result.sql}\n\n"
                    f"SQL 技术修复反馈（只允许改变实现与性能，不得改变指标、分母、粒度、筛选或时间范围）：\n"
                    f"{proposed}"
                )
                disposition = "technical_repair"
            else:
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
                applied_instruction = str(decision.get("revision_instruction") or "").strip()
                if not applied_instruction:
                    return "🧮 SQL 重新生成失败：审批结果缺少自然语言修改说明。"
                request_payload["question"] = (
                    f"原始问题：\n{original_question}\n\n"
                    f"用户确认的本次口径补充：\n{applied_instruction}"
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
                "generation": result.generation,
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
        raw_runtime_context = getattr(runtime, "context", None)
        runtime_context = raw_runtime_context if isinstance(raw_runtime_context, dict) else {}
        generation = database_sql_revision_resume_registry.register_generation(
            session_id=self.session_id,
            query_id=self.query_id,
            run_id=str(runtime_context.get("run_id") or ""),
            goal_id=str(runtime_context.get("goal_id") or ""),
            goal_revision=runtime_context.get("goal_revision"),
            result=result,
            request=generation_request,
            parent_generation_id=parent.id if parent is not None else "",
            revision_instruction=applied_instruction,
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
