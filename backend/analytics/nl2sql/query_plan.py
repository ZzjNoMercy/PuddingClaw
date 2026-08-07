"""Structured, validation-bound query-plan derivation for NL2SQL follow-ups."""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any

import sqlglot
from sqlglot import exp

from analytics.nl2sql.eav_evidence import extract_eav_type_names, sql_business_fingerprint

_MARKER_RE = re.compile(r"[A-Za-z]+\d+(?:\.\d+)?|\d+(?:\.\d+)?|[A-Za-z]{2,}")


def _hash(value: Any) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def _question_markers(question: str, sql: str) -> list[str]:
    question_lower = question.lower()
    markers = {match.group(0).lower() for match in _MARKER_RE.finditer(question)}
    try:
        tree = sqlglot.parse_one(sql, read="postgres")
        for literal in tree.find_all(exp.Literal):
            value = str(literal.this or "").strip()
            if value and value.lower() in question_lower:
                markers.add(value.lower())
    except Exception:
        pass
    return sorted(markers)


def build_verified_query_plan(
    *,
    question: str,
    sql: str,
    generation_id: str,
    validation_receipt_id: str,
    database_source_id: str,
    allowed_tables: list[str],
    semantic_assets: dict[str, Any],
    generation_trace: dict[str, Any],
) -> dict[str, Any]:
    tree = sqlglot.parse_one(sql, read="postgres")
    group_by = tree.args.get("group")
    dimensions = [item.sql(dialect="postgres") for item in group_by.expressions] if group_by else []
    where = tree.args.get("where")
    selected_assets = sorted(
        {
            str(item.get("id") or "")
            for item in semantic_assets.get("matched", [])
            if isinstance(item, dict) and str(item.get("id") or "")
        }
    )
    profiles = (
        generation_trace.get("eav_value_profiles", {}).get("items", [])
        if isinstance(generation_trace.get("eav_value_profiles"), dict)
        else []
    )
    profile_bindings = [
        {
            "type_name": item.get("type_name"),
            "source_revision": item.get("source_revision"),
            "value_profile_hash": item.get("value_profile_hash"),
            "grain_contract_hash": item.get("grain_contract_hash"),
        }
        for item in profiles
        if isinstance(item, dict) and item.get("type_name")
    ]
    plan = {
        "schema_version": "verified-query-plan/v1",
        "originating_generation_id": generation_id,
        "originating_validation_receipt_id": validation_receipt_id,
        "database_source_id": database_source_id,
        "allowed_tables": sorted(set(allowed_tables)),
        "semantic_hash": str(semantic_assets.get("semantic_hash") or ""),
        "permission_epoch": max(1, int(generation_trace.get("permission_epoch") or 1)),
        "semantic_asset_ids": selected_assets,
        "business_fingerprint": sql_business_fingerprint(sql),
        "base_population": where.this.sql(dialect="postgres") if where is not None else "TRUE",
        "grain_keys": dimensions,
        "dimensions": dimensions,
        "eav_type_names": sorted(extract_eav_type_names(sql)),
        "profile_bindings": profile_bindings,
        "question_markers": _question_markers(question, sql),
        # This SQL is server-internal derivation evidence. It is never returned
        # as authority to the Agent and must be regenerated and revalidated.
        "validated_sql": sql,
    }
    plan["plan_hash"] = _hash(plan)
    return plan


def select_derivable_query_plans(
    plans: list[dict[str, Any]],
    *,
    question: str,
    database_source_id: str,
    allowed_tables: list[str],
    permission_epoch: int = 1,
    limit: int = 3,
) -> list[dict[str, Any]]:
    current_markers = {match.group(0).lower() for match in _MARKER_RE.finditer(question)}
    current_scope = set(allowed_tables)
    candidates: list[tuple[int, dict[str, Any]]] = []
    for plan in plans:
        if str(plan.get("schema_version") or "") != "verified-query-plan/v1":
            continue
        if str(plan.get("database_source_id") or "") != database_source_id:
            continue
        try:
            plan_permission_epoch = int(plan.get("permission_epoch") or 0)
        except (TypeError, ValueError):
            continue
        if plan_permission_epoch != max(1, int(permission_epoch or 1)):
            continue
        plan_scope = {str(item) for item in plan.get("allowed_tables") or []}
        if not plan_scope or plan_scope != current_scope:
            continue
        marker_overlap = current_markers & {str(item).lower() for item in plan.get("question_markers") or []}
        type_overlap = {
            str(item).lower()
            for item in plan.get("eav_type_names") or []
            if str(item).lower() in question.lower()
        }
        score = len(marker_overlap) * 3 + len(type_overlap) * 5
        if score <= 0:
            continue
        candidates.append((score, plan))
    candidates.sort(key=lambda item: item[0], reverse=True)
    return [dict(item[1]) for item in candidates[: max(1, limit)]]


__all__ = ["build_verified_query_plan", "select_derivable_query_plans"]
