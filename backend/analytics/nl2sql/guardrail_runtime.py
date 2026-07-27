"""Dependency-free deterministic runtime for portable SQL guardrails.

This module is deliberately free of PuddingClaw models and services. The
platform guardrail middleware calls it directly and project export copies the
same source into the portable package, preventing detector drift.
"""

from __future__ import annotations

import re
from typing import Any, Literal

EvaluationStatus = Literal["passed", "failed", "not_applicable", "not_evaluated"]
SUPPORTED_RULE_TYPES = frozenset(
    {
        "forbid_sql_pattern",
        "require_sql_contains",
        "require_table_when_available",
        "require_group_by",
        "forbid_exists_distinct_pattern",
    }
)


def _as_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    return [str(value).strip()] if str(value).strip() else []


def _normalize_identifier(value: str) -> str:
    return str(value or "").strip().strip('"').strip("() \n\t;").split(".")[-1].strip('"').lower()


def _sql_contains(sql: str, needle: str) -> bool:
    return str(needle or "").lower() in sql.lower()


def _extract_group_by_columns(sql: str) -> list[str]:
    match = re.search(
        r"\bgroup\s+by\s+(?P<columns>.*?)(?:\border\s+by\b|\blimit\b|\bhaving\b|\bwhere\b|\bselect\b|$)",
        sql,
        re.IGNORECASE | re.DOTALL,
    )
    if not match:
        return []
    return [_normalize_identifier(column) for column in match.group("columns").split(",") if column.strip()]


def _uses_table(sql: str, table_name: str) -> bool:
    table = re.escape(str(table_name).strip())
    return re.search(rf"\b(?:from|join)\s+{table}\b", sql, re.IGNORECASE) is not None


def scope_status(rule: dict[str, Any], context: dict[str, Any] | None) -> EvaluationStatus:
    """Evaluate rule scope without guessing absent execution context."""

    scope = rule.get("scope") if isinstance(rule.get("scope"), dict) else {}
    table_scope = scope.get("table_scope") if isinstance(scope.get("table_scope"), dict) else {}
    table_values = {str(item) for item in table_scope.get("values") or [] if str(item).strip()}
    semantic_values = {str(item) for item in scope.get("semantic_assets") or [] if str(item).strip()}
    intents = [str(item).strip().lower() for item in scope.get("intent_any") or [] if str(item).strip()]
    required_context = bool(table_values or semantic_values or intents)
    if required_context and context is None:
        return "not_evaluated"
    context = context or {}

    if table_values:
        if "available_tables" not in context:
            return "not_evaluated"
        available: set[str] = set()
        for item in context.get("available_tables") or []:
            value = str(item).strip().strip('"')
            if value:
                available.update({value, value.split(".")[-1]})
        if table_scope.get("mode", "any") == "all":
            if not table_values.issubset(available):
                return "not_applicable"
        elif not table_values.intersection(available):
            return "not_applicable"
    if semantic_values:
        if "semantic_asset_ids" not in context:
            return "not_evaluated"
        selected = {str(item) for item in context.get("semantic_asset_ids") or []}
        if not semantic_values.issubset(selected):
            return "not_applicable"
    if intents:
        if "question" not in context:
            return "not_evaluated"
        question = str(context.get("question") or "").lower()
        if not any(intent in question for intent in intents):
            return "not_applicable"
    return "passed"


def detector_failed(sql: str, rule: dict[str, Any]) -> bool | None:
    """Return True on conflict, False on pass, None for unsupported types."""

    params = rule.get("params") if isinstance(rule.get("params"), dict) else {}
    rule_type = str(rule.get("type") or "")
    if rule_type == "forbid_sql_pattern":
        pattern = str(params.get("pattern") or "")
        if not pattern:
            return False
        flags = 0 if "case_sensitive" in {item.lower() for item in _as_list(params.get("flags"))} else re.IGNORECASE
        if not re.search(pattern, sql, flags):
            return False
        unless_contains = str(params.get("unless_contains") or "")
        if unless_contains and _sql_contains(sql, unless_contains):
            return False
        unless_pattern = str(params.get("unless_pattern") or "")
        return not bool(unless_pattern and re.search(unless_pattern, sql, flags))
    if rule_type == "require_sql_contains":
        contains = str(params.get("contains") or "")
        if not contains:
            return False
        triggers = _as_list(params.get("when_contains_any"))
        if triggers and not any(_sql_contains(sql, item) for item in triggers):
            return False
        return not _sql_contains(sql, contains)
    if rule_type == "require_table_when_available":
        required = str(params.get("required_table") or "")
        fallback = str(params.get("fallback_table") or "")
        if not required or _uses_table(sql, required):
            return False
        return not fallback or _uses_table(sql, fallback)
    if rule_type == "require_group_by":
        group_columns = set(_extract_group_by_columns(sql))
        if not group_columns:
            return False
        required = {_normalize_identifier(item) for item in _as_list(params.get("require_columns"))}
        forbidden_only = {_normalize_identifier(item) for item in _as_list(params.get("forbidden_columns_only"))}
        return bool(
            (forbidden_only and group_columns == forbidden_only) or (required and not required.issubset(group_columns))
        )
    if rule_type == "forbid_exists_distinct_pattern":
        lowered = " ".join(sql.split()).lower()
        table = str(params.get("table") or "")
        distinct_column = str(params.get("distinct_column") or "")
        min_exists = int(params.get("min_exists_count") or 2)
        if table and not _uses_table(lowered, table):
            return False
        if len(re.findall(r"\b(?:not\s+)?exists\s*\(", lowered)) < min_exists:
            return False
        if "count(distinct" not in lowered:
            return False
        if (
            distinct_column
            and re.search(rf"\bselect\s+distinct\s+[\w.]*{re.escape(distinct_column.lower())}\b", lowered) is None
        ):
            return False
        return True
    return None


def evaluate_rule(sql: str, rule: dict[str, Any], context: dict[str, Any] | None = None) -> dict[str, Any]:
    if not rule.get("enabled", True):
        return {"id": rule.get("id"), "status": "not_applicable", "reason": "disabled"}
    scoped = scope_status(rule, context)
    if scoped != "passed":
        return {"id": rule.get("id"), "status": scoped, "reason": "scope"}
    failed = detector_failed(sql, rule)
    if failed is None:
        return {"id": rule.get("id"), "status": "not_evaluated", "reason": "unsupported_rule_type"}
    action = rule.get("action") if isinstance(rule.get("action"), dict) else {}
    return {
        "id": rule.get("id"),
        "name": rule.get("name"),
        "type": rule.get("type"),
        "status": "failed" if failed else "passed",
        "action": str(action.get("type") or "block"),
        "message": str(action.get("message") or "SQL guardrail failed"),
    }


def validate_rules(
    sql: str,
    rules: list[dict[str, Any]],
    context: dict[str, Any] | None = None,
    *,
    strict: bool = True,
) -> dict[str, Any]:
    evaluations = [evaluate_rule(sql, rule, context) for rule in rules]
    blocking = [item for item in evaluations if item["status"] == "failed" and item.get("action") != "warn"]
    warnings = [item for item in evaluations if item["status"] == "failed" and item.get("action") == "warn"]
    not_evaluated = [item for item in evaluations if item["status"] == "not_evaluated"]
    return {
        "passed": not blocking and (not strict or not not_evaluated),
        "failures": blocking,
        "warnings": warnings,
        "not_evaluated": not_evaluated,
        "evaluations": evaluations,
    }
