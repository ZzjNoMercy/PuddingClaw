"""Deterministic checks around LLM-authored semantic Markdown."""

from __future__ import annotations

import re
from typing import Any

from .contracts import (
    KIND_EFFECTS,
    AuthoringBrief,
    DefinitionKind,
    kind_from_logical_path,
)
from .documents import MarkdownDocument, MarkdownDocumentError, parse_markdown_document

_TOPICS: dict[DefinitionKind, dict[str, tuple[str, ...]]] = {
    "measure": {
        "business_meaning": ("业务含义", "业务口径", "定义"),
        "sources": ("数据来源", "来源字段", "语义输入", "输入"),
        "calculation": ("计算", "分子", "分母", "聚合"),
        "grain": ("颗粒度", "粒度"),
        "rules": ("业务规则", "边界", "空值", "零"),
        "unit_and_time": ("单位", "币种", "时间口径", "时间字段", "不适用"),
        "duplicates": ("去重", "重复", "唯一键", "不适用"),
        "examples": ("验收", "示例", "反例"),
    },
    "dimension": {
        "business_meaning": ("业务含义", "业务口径", "成员"),
        "resolution": ("解析", "映射", "来源", "创建方式"),
        "unknowns": ("未知", "空值", "其他值", "未匹配"),
        "examples": ("验收", "示例", "反例"),
    },
    "grain": {
        "business_object": ("业务对象", "统计对象", "业务含义"),
        "identity": ("唯一键", "组合键", "身份"),
        "deduplication": ("去重", "重复"),
        "rollup": ("上卷", "汇总", "父级"),
        "examples": ("验收", "示例", "反例"),
    },
    "relation": {
        "endpoints": ("两端", "来源资产", "目标资产", "关联对象"),
        "keys": ("字段映射", "关联键", "join key", "连接键"),
        "cardinality": ("基数", "一对一", "一对多", "多对一", "多对多"),
        "risk": ("重复计数", "未匹配", "空键", "覆盖率"),
    },
    "analytics_model": {
        "goal": ("模型目标", "业务目标", "目标问题"),
        "questions": ("适用问题", "典型问题", "用户问题"),
        "dependencies": ("依赖", "数据资产", "语义资产", "关系"),
        "scope": ("适用范围", "默认范围", "过滤", "限制"),
        "output": ("输出要求", "结果要求", "交付"),
        "acceptance": ("验收", "示例", "反例"),
    },
}

_REQUIRED_BRIEF_TOPICS: dict[DefinitionKind, frozenset[str]] = {
    kind: frozenset(topics) for kind, topics in _TOPICS.items()
}

_PLACEHOLDER_PATTERNS = (
    r"\bTODO\b",
    r"\bTBD\b",
    r"在这里描述",
    r"待补充",
    r"待确认",
)


def _validate_common_frontmatter(
    document: MarkdownDocument,
    kind: DefinitionKind,
) -> list[dict[str, str]]:
    diagnostics: list[dict[str, str]] = []
    expected_formatter = "analytics-model" if kind == "analytics_model" else (
        "asset-relation" if kind == "relation" else "semantic-asset"
    )
    expected_type = "analysis_model" if kind == "analytics_model" else kind
    required_strings = {
        "formatter": expected_formatter,
        "type": expected_type,
        "name": None,
        "description": None,
    }
    for field, expected in required_strings.items():
        value = document.frontmatter.get(field)
        if not isinstance(value, str) or not value.strip():
            diagnostics.append(
                {
                    "code": "missing_required_frontmatter",
                    "severity": "error",
                    "message": f"frontmatter field '{field}' must be a non-empty string",
                }
            )
        elif expected is not None and value.strip() != expected:
            diagnostics.append(
                {
                    "code": "frontmatter_path_conflict",
                    "severity": "error",
                    "message": f"frontmatter field '{field}' must be '{expected}' for this target path",
                }
            )
    for field in ("aliases", "tags"):
        value = document.frontmatter.get(field)
        if value is not None and (
            not isinstance(value, list)
            or any(not isinstance(item, str) or not item.strip() for item in value)
        ):
            diagnostics.append(
                {
                    "code": "invalid_frontmatter_list",
                    "severity": "error",
                    "message": f"frontmatter field '{field}' must be a list of non-empty strings",
                }
            )
    return diagnostics


def _body_mentions(value: Any, body_lower: str) -> bool:
    if isinstance(value, str):
        text = value.strip().lower()
        return not text or text in body_lower
    if isinstance(value, list):
        strings = [item for item in value if isinstance(item, str) and item.strip()]
        return all(item.strip().lower() in body_lower for item in strings)
    if isinstance(value, dict):
        strings: list[str] = []
        for nested in value.values():
            if isinstance(nested, str) and (":" in nested or "/" in nested):
                strings.append(nested)
            elif isinstance(nested, list):
                strings.extend(item for item in nested if isinstance(item, str) and (":" in item or "/" in item))
        return all(item.strip().lower() in body_lower for item in strings if item.strip())
    return True


def _validate_business_frontmatter(
    document: MarkdownDocument,
    kind: DefinitionKind,
) -> list[dict[str, str]]:
    body_lower = document.body.lower()
    diagnostics: list[dict[str, str]] = []
    for effect in KIND_EFFECTS[kind]:
        if effect.body_projection != "required" or effect.field not in document.frontmatter:
            continue
        if not _body_mentions(document.frontmatter[effect.field], body_lower):
            diagnostics.append(
                {
                    "code": "business_frontmatter_not_auditable_in_body",
                    "severity": "error",
                    "message": f"frontmatter field '{effect.field}' affects runtime but is not auditable in the body",
                }
            )
    return diagnostics


def validate_markdown_definition(
    content: str,
    *,
    logical_path: str,
    brief: AuthoringBrief | None = None,
) -> dict[str, Any]:
    try:
        kind = kind_from_logical_path(logical_path)
        document = parse_markdown_document(content)
    except (ValueError, MarkdownDocumentError) as exc:
        return {
            "valid": False,
            "kind": None,
            "diagnostics": [{"code": "invalid_document", "severity": "error", "message": str(exc)}],
        }
    diagnostics: list[dict[str, str]] = []
    if not document.frontmatter:
        diagnostics.append({"code": "missing_frontmatter", "severity": "error", "message": "frontmatter is required"})
    else:
        diagnostics.extend(_validate_common_frontmatter(document, kind))
    if not re.search(r"(?m)^#\s+\S", document.body):
        diagnostics.append({"code": "missing_title", "severity": "error", "message": "body needs a readable H1 title"})
    for pattern in _PLACEHOLDER_PATTERNS:
        if re.search(pattern, document.body, flags=re.IGNORECASE):
            diagnostics.append(
                {
                    "code": "placeholder_business_content",
                    "severity": "error",
                    "message": f"body still contains unresolved placeholder text matching '{pattern}'",
                }
            )
    body_lower = document.body.lower()
    for topic, terms in _TOPICS[kind].items():
        if not any(term.lower() in body_lower for term in terms):
            diagnostics.append(
                {
                    "code": "missing_authoring_topic",
                    "severity": "error" if kind == "measure" else "warning",
                    "message": f"body may not explain required topic: {topic}",
                }
            )
    diagnostics.extend(_validate_business_frontmatter(document, kind))
    if brief is not None:
        if brief.kind != kind:
            diagnostics.append({"code": "brief_kind_mismatch", "severity": "error", "message": "brief kind does not match path"})
        if brief.unresolved:
            diagnostics.append(
                {
                    "code": "unresolved_business_decisions",
                    "severity": "error",
                    "message": f"{len(brief.unresolved)} business decisions are unresolved",
                }
            )
        if not brief.goal or not brief.confirmed or not brief.evidence:
            diagnostics.append(
                {
                    "code": "brief_missing_control_evidence",
                    "severity": "error",
                    "message": "brief requires a goal, confirmed decisions, and evidence references",
                }
            )
        missing_reviewed = sorted(_REQUIRED_BRIEF_TOPICS[kind] - set(brief.reviewed_topics))
        if missing_reviewed:
            diagnostics.append(
                {
                    "code": "brief_topics_not_reviewed",
                    "severity": "error",
                    "message": f"brief has not reviewed required topics: {', '.join(missing_reviewed)}",
                }
            )
        if brief.confirmed:
            diagnostics.append(
                {
                    "code": "confirmed_decisions_require_llm_review",
                    "severity": "info",
                    "message": "Agent must confirm that every confirmed decision is faithfully expressed in the body",
                }
            )
    else:
        diagnostics.append(
            {
                "code": "missing_authoring_brief",
                "severity": "error",
                "message": "a control-only Authoring Brief is required before preparation",
            }
        )
    return {
        "valid": not any(item["severity"] == "error" for item in diagnostics),
        "kind": kind,
        "diagnostics": diagnostics,
        "frontmatter": document.frontmatter,
        "body_chars": len(document.body),
    }
