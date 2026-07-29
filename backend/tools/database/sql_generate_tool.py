"""SQL generation tool for database Agent workflows."""

from __future__ import annotations

import asyncio
import re

from langchain.tools import ToolRuntime
from langchain_core.messages import HumanMessage
from langchain_core.tools import BaseTool
from langgraph.types import interrupt
from pydantic import BaseModel

from analytics.nl2sql.schemas import DatabaseQueryRequest
from analytics.nl2sql.service import DatabaseKnowledgeQueryError, generate_database_sql
from analytics.nl2sql.table_router import summarize_table_route
from analytics.semantic_runtime import normalize_selected_semantic_asset_ids
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
_PHYSICAL_IDENTIFIER_PATTERN = re.compile(
    r"(?<![A-Za-z0-9_])[A-Za-z][A-Za-z0-9_]*\.[A-Za-z][A-Za-z0-9_]*(?![A-Za-z0-9_])"
    r"|(?<![A-Za-z0-9_])[A-Za-z][A-Za-z0-9]*(?:_[A-Za-z0-9]+)+(?![A-Za-z0-9_])"
)
_ASSET_ID_REFERENCE_PATTERN = re.compile(
    r"\b(?:dimension|measure|grain|relation):[A-Za-z0-9_\-]+"
)
_SQL_IMPLEMENTATION_PATTERN = re.compile(
    r"\b(?:SELECT|FROM|JOIN|WHERE|CTE|EXISTS|DISTINCT|GROUP\s+BY|"
    r"ORDER\s+BY|COUNT|FILTER|LIKE|ILIKE)\b",
    re.IGNORECASE,
)
_PHYSICAL_DIRECTIVE_MARKERS = (
    "判断依据",
    "物理字段",
    "物理表",
    "字段名",
    "列名",
    "表名",
)
_PHYSICAL_CHOICE_DIRECTIVE_PATTERN = re.compile(
    r"(?:使用|采用|改用|指定|选择|匹配|读取|关联|映射到|取自)"
    r".{0,40}(?:字段|列|表|配置项|实体)"
    r"|(?:字段|列|表|配置项|实体).{0,40}(?:使用|采用|指定|选择|匹配|映射)",
    re.IGNORECASE,
)
_PRESCRIPTIVE_REVISION_PATTERN = re.compile(
    r"(?:改用|请用|使用|应使用|优先使用|替换为|改写为|不要用|避免使用|"
    r"改成|改为).{0,80}(?:\bSELECT\b|\bFROM\b|\bJOIN\b|\bWHERE\b|"
    r"\bCTE\b|\bEXISTS\b|\bDISTINCT\b|\bGROUP\s+BY\b|\bCOUNT\b|"
    r"\bFILTER\b|\bLIKE\b|\bILIKE\b|字段|列名|表名|物理表|实体|"
    r"type_name|type_value|EAV)",
    re.IGNORECASE,
)


def _message_text(message: object) -> str:
    content = getattr(message, "content", "")
    if isinstance(message, dict):
        content = message.get("content", "")
        if not content and isinstance(message.get("data"), dict):
            content = message["data"].get("content", "")
    if isinstance(content, list):
        return "".join(
            str(block.get("text") or block.get("content") or "")
            if isinstance(block, dict)
            else str(block)
            for block in content
        )
    return str(content or "")


def _trusted_user_scope_text(runtime: ToolRuntime | None) -> str:
    """Return only user-owned business scope available to the current Run.

    Goal continuations and grader/summarizer prompts are model-owned control
    messages. They must not authorize physical database choices invented by
    the Agent. The durable Run objective is trusted because it originates from
    the user's Goal request.
    """

    if runtime is None:
        return ""
    context = getattr(runtime, "context", None)
    if isinstance(context, dict):
        run_objective = context.get("run_objective")
        if isinstance(run_objective, str) and run_objective.strip():
            # Runtime context is server-owned and is inherited by subagents.
            # Never let a delegated HumanMessage redefine user authorization.
            return run_objective.strip()
    if not isinstance(runtime.state, dict):
        return ""
    state = runtime.state
    objective = state.get("_run_objective")
    if isinstance(objective, str) and objective.strip():
        # PrivateState is populated from the server-owned Run objective and is
        # inherited by delegated subagents. Once present it is the complete
        # authorization boundary; a delegated HumanMessage is an instruction
        # from another Agent, not new user consent.
        return objective.strip()
    parts: list[str] = []
    messages = list(state.get("messages") or [])
    run_query_id = str(state.get("_run_query_id") or "").strip()
    for message in reversed(messages):
        role = ""
        name = getattr(message, "name", "")
        extra = getattr(message, "additional_kwargs", {}) or {}
        if isinstance(message, HumanMessage):
            role = "user"
        elif isinstance(message, dict):
            role = str(message.get("role") or message.get("type") or "")
            name = str(message.get("name") or "")
            extra = message.get("additional_kwargs") or {}
            if isinstance(message.get("data"), dict):
                data = message["data"]
                role = str(data.get("role") or role)
                name = str(data.get("name") or name)
                extra = data.get("additional_kwargs") or extra
        if role not in {"user", "human"}:
            continue
        source = str(extra.get("lc_source") or "") if isinstance(extra, dict) else ""
        if source or name in {"rubric_grader", "puddingclaw_completion_gate"}:
            continue
        message_query_id = str(extra.get("puddingclaw_query_id") or "") if isinstance(extra, dict) else ""
        if run_query_id and message_query_id and message_query_id != run_query_id:
            continue
        text = _message_text(message)
        if "\n\n[系统路由提示]" in text:
            text = text.split("\n\n[系统路由提示]", 1)[0]
        if text.strip():
            parts.append(text.strip())
            break
    return "\n".join(dict.fromkeys(parts))


def _agent_added_physical_guidance(
    *,
    question: str,
    table_names: list[str],
    runtime: ToolRuntime | None,
) -> list[str]:
    """Find physical implementation choices absent from trusted user scope."""

    trusted_text = _trusted_user_scope_text(runtime)
    if not trusted_text:
        # Non-Agent callers and older tests do not expose message provenance.
        return []
    # Asset-id references (dimension:energy_type) are legitimate vocabulary of
    # the semantic channel — strip them so an id containing a column-shaped
    # name is not misread as a bare physical hint. A bare `energy_type`
    # elsewhere in the question is still flagged. (The SQL-level guardrail
    # remains the deterministic backstop; this check is heuristic.)
    scanned = _ASSET_ID_REFERENCE_PATTERN.sub(" ", question)
    trusted_lower = trusted_text.lower()
    findings: list[str] = []
    for token in _PHYSICAL_IDENTIFIER_PATTERN.findall(scanned):
        if token.lower() not in trusted_lower and token not in findings:
            findings.append(token)
    for match in _SQL_IMPLEMENTATION_PATTERN.finditer(scanned):
        token = " ".join(match.group(0).upper().split())
        if token.lower() not in trusted_lower and token not in findings:
            findings.append(token)
    for marker in _PHYSICAL_DIRECTIVE_MARKERS:
        if marker in scanned and marker not in trusted_text and marker not in findings:
            findings.append(marker)
    for match in _PHYSICAL_CHOICE_DIRECTIVE_PATTERN.finditer(scanned):
        directive = " ".join(match.group(0).split())
        if directive not in trusted_text and directive not in findings:
            findings.append(directive)
    for table_name in table_names:
        normalized = str(table_name or "").strip()
        if normalized and normalized.lower() not in trusted_lower and normalized not in findings:
            findings.append(normalized)
    return findings


def _agent_added_enum_caliber(
    *,
    question: str,
    selected_asset_ids: list[str],
    trusted_text: str,
    authorized_terms_by_asset: dict[str, set[str]] | None = None,
    strict: bool = False,
) -> list[str]:
    """Enum literals the Agent added to the question beyond the user's scope.

    Chinese enum values (柴油, 汽油+48V轻混系统, ...) never match the
    physical-identifier patterns above, so caliber injection through the
    question channel needs its own vocabulary check. The vocabulary is driven
    by the selected assets' declarations — nothing is hardcoded per dimension.
    Values the user actually wrote are legitimate business overrides and pass.
    """

    if not trusted_text or not selected_asset_ids:
        return []
    try:
        from analytics.semantic_assets.registry import get_semantic_asset_registry

        registry = get_semantic_asset_registry()
    except Exception as exc:
        if strict:
            raise RuntimeError("semantic asset registry unavailable") from exc
        return []
    vocabulary: dict[str, set[str]] = {}
    for asset_id in selected_asset_ids:
        try:
            detail = registry.get_asset(str(asset_id))
        except Exception as exc:
            if strict:
                raise RuntimeError(f"governed semantic asset unavailable: {asset_id}") from exc
            continue
        frontmatter = detail.get("frontmatter") if isinstance(detail, dict) else None
        if not isinstance(frontmatter, dict):
            continue
        asset_vocabulary: set[str] = set()
        for value in frontmatter.get("enum_universe") or []:
            value = str(value).strip()
            if value:
                asset_vocabulary.add(value)
        classifications = frontmatter.get("classifications")
        if isinstance(classifications, dict):
            for label, values in classifications.items():
                label = str(label).strip()
                if label:
                    asset_vocabulary.add(label)
                for value in values or []:
                    value = str(value).strip()
                    if value:
                        asset_vocabulary.add(value)
        vocabulary[str(asset_id)] = asset_vocabulary
    authorized = authorized_terms_by_asset or {}
    return sorted(
        term
        for asset_id, terms in vocabulary.items()
        for term in terms
        if term in question
        and term not in trusted_text
        and term not in authorized.get(asset_id, set())
    )


def _trusted_template_enum_terms(runtime: ToolRuntime | None) -> dict[str, set[str]]:
    """Load enum authorization from the template guide selected by the Agent.

    AnalysisTemplateMiddleware writes this private field only after a
    successful read of a registered TEMPLATE.md manifest.  The SQL tool never
    infers template use from query keywords or delegated messages.
    """

    if runtime is None or not isinstance(runtime.state, dict):
        return {}
    active = runtime.state.get("_active_analysis_template")
    if not isinstance(active, dict):
        return {}
    if str(active.get("model_id") or "") != str(runtime.state.get("analytics_model_id") or ""):
        return {}
    scope = active.get("semantic_scope") if isinstance(active.get("semantic_scope"), dict) else {}
    filters = scope.get("enum_filters") if isinstance(scope.get("enum_filters"), dict) else {}
    result: dict[str, set[str]] = {}
    for asset_id, definition in filters.items():
        if not isinstance(definition, dict):
            continue
        result[str(asset_id)] = {
            str(value).strip()
            for value in [
                *(definition.get("members") or []),
                *(definition.get("classifications") or []),
            ]
            if str(value).strip()
        }
    return result


def _is_prescriptive_sql_revision(instruction: str) -> bool:
    """Whether feedback tells the generator how to implement the repair."""

    return bool(_PRESCRIPTIVE_REVISION_PATTERN.search(" ".join(str(instruction or "").split())))


def _is_technical_sql_revision(instruction: str) -> bool:
    """Identify implementation repair that must not become business HITL."""

    normalized = " ".join(str(instruction or "").split())
    return bool(
        normalized
        and _TECHNICAL_SQL_REVISION_PATTERN.search(normalized)
        and not _BUSINESS_SEMANTIC_CHANGE_PATTERN.search(normalized)
    )


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
        "  外层 Agent 不得凭字段名或常识直接覆盖；如用户明确改变业务口径，只能携带原 "
        "parent_generation_id 和用户确认的自然语言 revision_instruction 请求重新生成。",
    ]
    context_id = str(semantic_assets.get("semantic_context_id") or "").strip()
    semantic_hash = str(
        semantic_assets.get("semantic_hash")
        or semantic_assets.get("semantic_context_hash")
        or ""
    ).strip()
    if context_id:
        lines.append(f"  - semantic_context_id: {context_id}")
    if semantic_hash:
        lines.append(f"  - semantic_hash: {semantic_hash}")
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
        "Generate PostgreSQL SQL from a business-level database question without executing it. "
        "Use this as the first step for database analysis when the Agent needs to inspect, validate, "
        "or execute SQL. For a Goal, the Agent may decompose the Goal into a focused business sub-question, but it "
        "must not add physical tables, columns, EAV names/values, entities, or SQL implementation choices that the "
        "user did not specify. It uses that business question for Vanna evidence/candidate retrieval, then applies semantic "
        "assets in a separate final refinement pass before SQL guardrails. Database entity evidence is authoritative "
        "for physical table/column/EAV values. It returns SQL plus its authoritative semantic contract. Do not "
        "manually rewrite semantics from a "
        "matched Measure/Reference. To propose a business-semantic change, call this tool with parent_generation_id and "
        "a natural-language revision_instruction; the user then chooses agree, reject, or modify. For SQL timeout, "
        "syntax, validation, or performance failures, report the observed problem through revision_instruction without "
        "prescribing fields, tables, entities, JOIN/CTE shape, or replacement SQL."
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
        requested_table_names = list(table_names or [])
        selected_asset_ids = list(
            dict.fromkeys(selected_semantic_asset_ids or semantic_asset_ids or measure_ids or [])
        )
        allowed_asset_ids: set[str] = set()
        if state_model_id and runtime is not None and isinstance(runtime.state, dict):
            if "allowed_semantic_asset_ids" not in runtime.state:
                return "🧮 SQL 生成失败：当前分析模型的可信语义资产范围不可用，请重新开始本轮任务。"
            allowed_asset_ids = {
                str(item).strip()
                for item in runtime.state.get("allowed_semantic_asset_ids") or []
                if str(item).strip()
            }
            selected_asset_ids, normalization_error = normalize_selected_semantic_asset_ids(
                selected_asset_ids,
                allowed_asset_ids,
            )
            if normalization_error:
                return "🧮 SQL 生成失败：" + normalization_error
        if not parent_generation_id:
            physical_guidance = _agent_added_physical_guidance(
                question=question,
                table_names=requested_table_names,
                runtime=runtime,
            )
            template_terms = _trusted_template_enum_terms(runtime)
            unauthorized_scope_assets = sorted(set(template_terms) - allowed_asset_ids)
            if state_model_id and unauthorized_scope_assets:
                return (
                    "🧮 SQL 生成失败：当前模板的语义范围引用了模型未授权资产："
                    + ", ".join(unauthorized_scope_assets)
                )
            try:
                enum_caliber = _agent_added_enum_caliber(
                    question=question,
                    # The Agent must not bypass enum governance by omitting an
                    # asset from selected_semantic_asset_ids. In model mode the
                    # server-owned allowlist is the vocabulary authority.
                    selected_asset_ids=sorted(allowed_asset_ids) if state_model_id else selected_asset_ids,
                    trusted_text=_trusted_user_scope_text(runtime),
                    authorized_terms_by_asset=template_terms,
                    strict=bool(state_model_id),
                )
            except RuntimeError as exc:
                return f"🧮 SQL 生成失败：当前分析模型的可信枚举范围不可用：{exc}"
            injected = physical_guidance + [
                term for term in enum_caliber if term not in physical_guidance
            ]
            if injected:
                return (
                    "🧮 SQL 生成失败：检测到 Agent 在业务子任务中新增了用户未指定的物理实现或口径枚举："
                    + ", ".join(injected[:12])
                    + "。Goal 模式允许拆解指标、维度、粒度、筛选和时间范围，但表、字段、"
                    "EAV 配置项/枚举、实体映射及 SQL 写法必须由 SQL 生成器根据数据库证据与语义资产决定。"
                    "请删除这些实现提示，仅保留业务问题后重新调用。"
                )
        elif revision_instruction and _is_prescriptive_sql_revision(revision_instruction):
            return (
                "🧮 SQL 重新生成失败：revision_instruction 只能反馈已观察到的问题，"
                "不能指导使用哪个字段、表、实体或 SQL 实现。请保留 parent_generation_id，"
                "仅描述错误、超时、空结果、校验冲突或结果异常后重试。"
            )
        request_payload = {
            "question": question,
            "semantic_question": question,
            "database_source_id": database_source_id,
            "table_names": requested_table_names,
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
            semantic_question = str(
                request_payload.get("semantic_question") or original_question
            )
            if _is_technical_sql_revision(proposed):
                applied_instruction = proposed
                request_payload["question"] = (
                    f"原始业务问题（业务语义不可改变）：\n{original_question}\n\n"
                    f"上一版 SQL：\n{parent.result.sql}\n\n"
                    f"SQL 技术修复反馈（只允许改变实现与性能，不得改变指标、分母、粒度、筛选或时间范围）：\n"
                    f"{proposed}"
                )
                request_payload["semantic_question"] = semantic_question
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
                approved_question = (
                    f"原始问题：\n{original_question}\n\n"
                    f"用户确认的本次口径补充：\n{applied_instruction}"
                )
                request_payload["question"] = approved_question
                request_payload["semantic_question"] = approved_question
                disposition = "approved_revision"
        elif revision_instruction:
            return "🧮 SQL 重新生成失败：revision_instruction 必须与 parent_generation_id 一起使用。"

        request = DatabaseQueryRequest(
            question=str(request_payload["question"]),
            database_source_id=request_payload.get("database_source_id"),
            table_names=list(request_payload.get("table_names") or []),
            model_id=request_payload.get("model_id"),
            measure_ids=list(request_payload.get("measure_ids") or []),
            semantic_question=request_payload.get("semantic_question"),
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
        generation_request = (
            dict(parent.request)
            if parent is not None and disposition == "technical_repair"
            else dict(request_payload)
        )
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
