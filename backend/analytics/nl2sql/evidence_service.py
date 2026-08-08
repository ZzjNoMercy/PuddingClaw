"""Evidence-only database retrieval for the Agent SQL path.

This module deliberately reuses the legacy retrieval primitives without
calling their SQL-generation or refinement entry points.  The returned facts
are references for the Agent; they are not an executable SQL authority.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
from collections.abc import Iterable
from typing import Any

from analytics.nl2sql.agent_path_policy import record_database_path_event
from analytics.nl2sql.schemas import DatabaseQueryRequest, to_plain_dict
from analytics.nl2sql.service import (
    _collect_vanna_references,
    _inspect_live_eav_value_profiles,
)
from analytics.nl2sql.table_router import route_database_tables, summarize_table_route
from analytics.semantic_runtime import compile_semantic_query_context, normalize_selected_semantic_asset_ids
from config import get_database_qa_config, get_vanna_config
from db import get_sessionmaker
from graph.database_evidence import database_evidence_registry
from graph.database_schema_evidence import database_schema_evidence_registry
from knowledge.database_sources import get_database_source
from tools.database.spans import emit_database_span


def _hash(value: Any) -> str:
    return "sha256:" + hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str).encode()
    ).hexdigest()


def _context(runtime: Any) -> dict[str, Any]:
    value = getattr(runtime, "context", None)
    return value if isinstance(value, dict) else {}


def _state(runtime: Any) -> dict[str, Any]:
    value = getattr(runtime, "state", None)
    return value if isinstance(value, dict) else {}


def _append_unique(names: list[str], seen: set[str], value: Any) -> None:
    normalized = str(value or "").strip()
    if normalized and normalized not in seen:
        names.append(normalized)
        seen.add(normalized)


def _prompt_entity_type_names(items: Iterable[dict[str, Any]]) -> list[str]:
    """Keep Vanna relevance order while selecting physical EAV names."""

    names: list[str] = []
    seen: set[str] = set()
    for item in items:
        if not isinstance(item, dict):
            continue
        column = str(item.get("table_column") or "").strip().lower()
        if not (column.endswith(".vehicle_params.type_name") or column == "vehicle_params.type_name"):
            continue
        _append_unique(names, seen, item.get("canonical_name") or item.get("name"))
    return names


def _semantic_type_names(trace: dict[str, Any]) -> list[str]:
    names: list[str] = []
    seen: set[str] = set()
    for item in (trace.get("matched") or []) if isinstance(trace, dict) else []:
        if not isinstance(item, dict):
            continue
        frontmatter = item.get("frontmatter") if isinstance(item.get("frontmatter"), dict) else {}
        resolution = frontmatter.get("resolution") if isinstance(frontmatter.get("resolution"), dict) else {}
        bindings = resolution.get("eav_equivalence") or frontmatter.get("eav_equivalence") or []
        if isinstance(bindings, dict):
            bindings = [bindings]
        for binding in bindings if isinstance(bindings, list) else []:
            if isinstance(binding, dict):
                for value in binding.get("type_names") or []:
                    _append_unique(names, seen, value)
    return names


def _profile_observation(profile: dict[str, Any], *, source_id: str, table: str) -> dict[str, Any]:
    values = [
        {"value": item.get("value"), "count": int(item.get("row_count") or item.get("count") or 0)}
        for item in profile.get("top_values") or []
        if isinstance(item, dict) and str(item.get("value") or "")
    ]
    distinct = int(profile.get("distinct_value_count") or 0)
    complete = bool(distinct > 0 and len(values) >= distinct)
    return {
        "table": table,
        "type_name": str(profile.get("type_name") or ""),
        "values": [str(item["value"]) for item in values],
        "value_counts": values,
        "authority": "observed_current_revision",
        "profile_revision": str(profile.get("source_revision") or ""),
        "value_profile_hash": str(profile.get("value_profile_hash") or ""),
        "complete": complete,
        "distinct_value_count": distinct,
        "total_row_count": int(profile.get("total_row_count") or 0),
        "conflict_model_count": int(profile.get("conflict_model_count") or 0),
        "profile": profile,
        "database_source_id": source_id,
    }


async def search_database_evidence(
    *,
    question: str,
    trusted_question: str | None = None,
    database_source_id: str | None,
    table_names: list[str],
    model_id: str | None,
    selected_semantic_asset_ids: list[str],
    focus_fields: list[str],
    entity_types: list[str],
    include_similar_sql: bool,
    reference_top_k: int,
    value_profile_limit: int,
    session_id: str,
    query_id: str,
    runtime: Any,
) -> dict[str, Any]:
    """Retrieve Vanna references and live EAV observations without SQL generation."""

    context = _context(runtime)
    state = _state(runtime)
    authoritative_question = str(trusted_question or question).strip()
    effective_model_id = str(state.get("analytics_model_id") or model_id or "").strip() or None
    requested_asset_ids = list(selected_semantic_asset_ids or [])
    if requested_asset_ids and "allowed_semantic_asset_ids" not in state:
        raise ValueError("当前分析模型的可信语义资产范围不可用")
    allowed_asset_ids = {
        str(item).strip()
        for item in state.get("allowed_semantic_asset_ids") or []
        if str(item).strip()
    }
    normalized_asset_ids = requested_asset_ids
    if allowed_asset_ids:
        normalized_asset_ids, normalization_error = normalize_selected_semantic_asset_ids(
            requested_asset_ids,
            allowed_asset_ids,
        )
        if normalization_error:
            raise ValueError(normalization_error)
    semantic_context = compile_semantic_query_context(
        question=authoritative_question,
        model_id=effective_model_id,
        selected_semantic_asset_ids=normalized_asset_ids,
        normalize_selected_ids=True,
        strict_selected_ids=bool(normalized_asset_ids),
    )
    request = DatabaseQueryRequest(
        question=question,
        database_source_id=database_source_id,
        table_names=list(table_names or []),
        model_id=effective_model_id,
        measure_ids=normalized_asset_ids,
        semantic_question=authoritative_question,
    )
    started = time.perf_counter()
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        route = await route_database_tables(session, request)
        source = await get_database_source(session, route.database_source_id)

    route_payload = summarize_table_route(route)
    emit_database_span(
        "router",
        {
            "selected_tables": route.table_names,
            "available_tables": route.available_tables,
            "source": route.database_source_id,
            "agent_path": True,
        },
        metadata={"database_source_id": route.database_source_id},
    )
    references: dict[str, Any] = {"ddl": [], "documentation": [], "similar_sql": [], "entities": []}
    warnings: list[dict[str, Any]] = []
    prompt_entities: list[dict[str, Any]] = []
    vanna_started = time.perf_counter()
    try:
        if not get_vanna_config().get("enabled", True):
            warnings.append({"stage": "vanna_retrieval", "code": "vanna_disabled"})
        else:
            from analytics.nl2sql.runtime import build_vanna_client_from_app_config

            vanna = await asyncio.to_thread(build_vanna_client_from_app_config)
            raw_references = await asyncio.to_thread(_collect_vanna_references, vanna, question, route)
            references["ddl"] = raw_references.get("ddl", {}).get("items", [])
            references["documentation"] = raw_references.get("documentation", {}).get("items", [])
            if include_similar_sql:
                references["similar_sql"] = [
                    {**item, "authority": "reference_only"}
                    for item in (raw_references.get("sql_examples", {}).get("items", []) or [])
                    if isinstance(item, dict)
                ][: max(1, min(reference_top_k, 20))]
            references["entities"] = raw_references.get("entities", {})
            prompt_entities = list(raw_references.get("_prompt_entities") or [])
            if entity_types:
                wanted_types = {str(item).strip() for item in entity_types if str(item).strip()}
                prompt_entities = [item for item in prompt_entities if str(item.get("entity_type") or "") in wanted_types]
            if focus_fields:
                wanted_fields = {str(item).strip().lower() for item in focus_fields if str(item).strip()}
                prompt_entities = [
                    item for item in prompt_entities
                    if str(item.get("table_column") or "").lower() in wanted_fields
                    or str(item.get("table_column") or "").lower().split(".")[-1] in wanted_fields
                ]
    except Exception as exc:
            warnings.append({"stage": "vanna_retrieval", "code": "vanna_retrieval_failed", "error_type": type(exc).__name__})
    retrieval_ms = round((time.perf_counter() - vanna_started) * 1000, 2)
    emit_database_span(
        "evidence_search",
        {
            "source": route.database_source_id,
            "tables": route.table_names,
            "ddl_count": len(references["ddl"]),
            "documentation_count": len(references["documentation"]),
            "similar_sql_count": len(references["similar_sql"]),
            "entity_count": len(prompt_entities),
            "duration_ms": retrieval_ms,
        },
        metadata={"database_source_id": route.database_source_id, "agent_path": True},
    )

    # Vanna already ranks prompt_entities by relevance. Preserve that order:
    # converting it to a set caused the bounded profiler to alphabetically
    # select unrelated fields even when the target was the top recall hit.
    type_names = _prompt_entity_type_names(prompt_entities)
    seen_type_names = set(type_names)
    for semantic_type_name in _semantic_type_names(semantic_context.trace):
        _append_unique(type_names, seen_type_names, semantic_type_name)
    observations: list[dict[str, Any]] = []
    profile_started = time.perf_counter()
    if type_names and any(str(item).split(".")[-1].strip('"').lower() == "vehicle_params" for item in route.table_names):
        try:
            permission_epoch = 1
            if session_id:
                from graph.session_manager import session_manager

                permission_epoch = int(session_manager.get_permission_policy(session_id)["policy_epoch"])
            profiles = await _inspect_live_eav_value_profiles(
                source=source,
                route=route,
                type_names=type_names,
                values_per_type=value_profile_limit,
                semantic_hash=semantic_context.semantic_hash,
                permission_epoch=permission_epoch,
                # Vanna recall is already bounded by the operator's per-entity
                # Top-K configuration. Do not apply a second hidden Top-3 cap.
                type_name_limit=None,
            )
            for profile in profiles:
                observation = _profile_observation(
                    profile,
                    source_id=route.database_source_id,
                    table=next((name for name in route.table_names if name.split(".")[-1].strip('"').lower() == "vehicle_params"), "vehicle_params"),
                )
                full_profile = observation.pop("profile")
                receipt = database_schema_evidence_registry.register(
                    session_id=session_id,
                    query_id=query_id,
                    run_id=str(context.get("run_id") or ""),
                    goal_id=str(context.get("goal_id") or ""),
                    goal_revision=context.get("goal_revision"),
                    database_source_id=route.database_source_id,
                    table_name=observation["table"],
                    mode="value_profile",
                    search="",
                    type_name=observation["type_name"],
                    rows=[{"type_value": item["value"], "count": item["count"]} for item in observation["value_counts"]],
                    profile=full_profile,
                    profile_revision=observation["profile_revision"],
                )
                observation["schema_evidence_receipt_id"] = receipt["id"]
                observations.append(observation)
        except Exception as exc:
            warnings.append({"stage": "physical_profile", "code": "eav_profile_failed", "error_type": type(exc).__name__})
    elif type_names:
        warnings.append({"stage": "physical_profile", "code": "eav_table_not_selected"})
    profile_ms = round((time.perf_counter() - profile_started) * 1000, 2)
    emit_database_span(
        "eav_value_profile",
        {
            "observations": observations,
            "profile_count": len(observations),
            "duration_ms": profile_ms,
        },
        metadata={"database_source_id": route.database_source_id, "agent_path": True},
    )

    payload = {
        "status": "partial" if warnings else "completed",
        "route": {"database_source_id": route.database_source_id, "allowed_tables": route.table_names, "detail": route_payload},
        "references": references,
        "observations": observations,
        "warnings": warnings,
        "semantic_context_hash": semantic_context.semantic_hash,
        "timings": {"vanna_retrieval_ms": retrieval_ms, "physical_profile_ms": profile_ms, "total_ms": round((time.perf_counter() - started) * 1000, 2)},
        "authority_rules": [
            "similar_sql is reference_only and is never an execution authority",
            "observations are current-revision physical evidence; complete=false does not prove absence",
            "this search never generates, registers, validates, or executes SQL",
        ],
    }
    payload_sha256 = _hash(payload)
    evidence = database_evidence_registry.register(
        session_id=session_id,
        query_id=query_id,
        run_id=str(context.get("run_id") or ""),
        goal_id=str(context.get("goal_id") or ""),
        goal_revision=context.get("goal_revision"),
        database_source_id=route.database_source_id,
        allowed_tables=route.table_names,
        payload=payload,
        trusted_question_sha256=_hash(authoritative_question),
        analytics_model_id=effective_model_id or "",
        analytics_model_revision=str(semantic_context.model_version or ""),
        semantic_context_hash=semantic_context.semantic_hash,
        selected_semantic_asset_ids=normalized_asset_ids,
    )
    payload["evidence_search_id"] = evidence["id"]
    # The registry hash covers the immutable payload.  Keep the payload hash
    # explicit because the response envelope gains its opaque id after the
    # receipt is registered; callers must not confuse the two hashes.
    payload["payload_sha256"] = payload_sha256
    payload["evidence_sha256"] = evidence["sha256"]
    if get_database_qa_config().get("database_agent_sql_shadow_compare_enabled", False):
        record_database_path_event(
            session_id=session_id,
            query_id=query_id,
            run_id=str(context.get("run_id") or ""),
            goal_id=str(context.get("goal_id") or ""),
            goal_revision=context.get("goal_revision"),
            event_type="shadow_compare_requested",
            error_code="",
            source_path="agent",
            target_path="legacy_generation_shadow",
            evidence_search_id=evidence["id"],
            metadata={"evidence_sha256": evidence["sha256"]},
        )
    return to_plain_dict(payload)
