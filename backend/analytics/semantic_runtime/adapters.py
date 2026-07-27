"""Execution-language projections of the shared semantic query context."""

from __future__ import annotations

import hashlib
import json
from typing import Any

from analytics.semantic_assets.resolver import MAX_BODY_CHARS, format_semantic_assets_for_prompt

from .schemas import SemanticQueryContext


def build_execution_binding_metadata(
    context: SemanticQueryContext,
    *,
    adapter: str,
    source_refs: list[str] | None = None,
    fields: list[str] | None = None,
) -> dict[str, str]:
    """Hash physical execution bindings separately from business semantics."""

    payload = {
        "adapter": adapter,
        "source_refs": sorted(str(item) for item in source_refs or []),
        "fields": sorted(str(item) for item in fields or []),
    }
    digest = hashlib.sha256(
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    execution_digest = hashlib.sha256(
        f"{context.semantic_hash}:{digest}".encode()
    ).hexdigest()
    return {
        "semantic_hash": context.semantic_hash,
        "binding_hash": f"sha256:{digest}",
        "execution_context_id": f"execctx-{execution_digest[:16]}",
    }


def format_analytics_model_for_sql_prompt(
    model: dict[str, Any],
) -> tuple[str, dict[str, Any]]:
    """Preserve the established SQL model contract outside NL2SQL service."""

    if not model:
        return "", {}
    frontmatter = model.get("frontmatter") or {}
    body = str(model.get("body") or "").strip()
    prompt = (
        "<analytics_model_sql_context>\n"
        "当前 SQL 属于用户已选择的分析模型，必须遵守模型正文中的全局业务边界和默认分析范围。\n"
        "规则优先级：用户明确要求 > 具体 Measure/Reference > 模型全局规则 > Dimension/通用 SQL 规则。\n"
        "只应用与本次 SQL 有关的模型规则；报告布局、HTML 和输出工作流不属于 SQL 生成任务。\n\n"
        f"模型 ID：{model.get('id')}\n"
        f"模型名称：{model.get('name')}\n"
        f"模型路径：{model.get('path')}\n\n"
        "模型 Frontmatter：\n"
        f"```json\n{json.dumps(frontmatter, ensure_ascii=False, indent=2)}\n```\n\n"
        "模型正文：\n"
        f"{body}\n"
        "</analytics_model_sql_context>"
    )
    trace = {
        "id": model.get("id"),
        "name": model.get("name"),
        "version": model.get("version"),
        "path": model.get("path"),
        "body_preview": body[:2000] + ("...[truncated]" if len(body) > 2000 else ""),
    }
    return prompt, trace


def render_sql_semantic_context(context: SemanticQueryContext) -> str:
    """Render only the SQL-facing projection of a compiled context."""

    model_prompt, _ = format_analytics_model_for_sql_prompt(context.model_context)
    return "\n\n".join(
        block
        for block in (
            model_prompt,
            format_semantic_assets_for_prompt(context.resolution),
        )
        if block
    )


def _asset_payload(asset: Any, *, full_body: bool) -> dict[str, Any]:
    body = str(getattr(asset, "body", "") or "").strip()
    if not full_body and len(body) > MAX_BODY_CHARS:
        body = body[:MAX_BODY_CHARS].rstrip() + "\n...（已截断）"
    return {
        "id": str(getattr(asset, "id", "") or ""),
        "name": str(getattr(asset, "name", "") or ""),
        "type": str(getattr(asset, "type", "") or ""),
        "parent_id": str(getattr(asset, "parent_id", "") or ""),
        "frontmatter": getattr(asset, "frontmatter", {}) or {},
        "definition": body,
    }


def render_pandas_semantic_context(
    context: SemanticQueryContext,
    *,
    dataframe_columns: list[str] | None = None,
    source_ref: str = "",
) -> dict[str, Any]:
    """Render a structured, DataFrame-facing projection of shared semantics."""

    full_body = bool(context.resolution.get("full_body"))
    model = context.model_context
    model_body = str(model.get("body") or "").strip()
    binding_metadata = build_execution_binding_metadata(
        context,
        adapter="pandas",
        source_refs=[source_ref] if source_ref else [],
        fields=list(dataframe_columns or []),
    )
    return {
        "context_id": context.context_id,
        "content_hash": context.content_hash,
        **binding_metadata,
        "resolution_mode": context.resolution.get("resolution_mode"),
        "analytics_model": {
            "id": model.get("id"),
            "name": model.get("name"),
            "version": model.get("version"),
            "description": model.get("description") or "",
            "business_rules": model_body[:4000]
            + ("...[truncated]" if len(model_body) > 4000 else ""),
        }
        if model
        else None,
        "semantic_assets": [
            _asset_payload(item, full_body=full_body)
            for item in context.resolution.get("matched") or []
        ],
        "references": [
            _asset_payload(item, full_body=full_body)
            for item in context.resolution.get("references") or []
        ],
        "relations": model.get("asset_relations") or [],
        "source_ref": source_ref,
        "dataframe_columns": list(dataframe_columns or []),
        "rules": [
            "以上语义资产是业务口径的唯一权威来源。",
            "只能使用当前 DataFrame 实际存在的列，不得把逻辑概念猜成物理列名。",
            "必须遵守声明的分子、分母、颗粒度、分类和禁止推断规则。",
            "语义定义与 DataFrame 物理结构冲突时，不得伪造字段；应报告无法执行的绑定。",
        ],
    }
