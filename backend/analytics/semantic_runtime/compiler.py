"""Compile one authoritative semantic context for every analytics adapter."""

from __future__ import annotations

import hashlib
import json
from difflib import get_close_matches
from pathlib import Path
from typing import Any

from analytics.models.registry import get_analytics_model_registry
from analytics.semantic_assets.resolver import (
    resolve_semantic_assets,
    resolve_semantic_assets_by_ids,
    semantic_resolution_to_trace,
)

from .schemas import SemanticQueryContext


def normalize_selected_semantic_asset_ids(
    selected_ids: list[str],
    allowed_ids: set[str],
) -> tuple[list[str], str | None]:
    """Normalize exact or unique-suffix ids against one model authority boundary."""

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


def _canonicalize(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            str(key): _canonicalize(item)
            for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
        }
    if isinstance(value, (list, tuple, set)):
        items = [_canonicalize(item) for item in value]
        return sorted(
            items,
            key=lambda item: json.dumps(
                item,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                default=str,
            ),
        )
    return value


def _model_trace(model: dict[str, Any]) -> dict[str, Any]:
    if not model:
        return {}
    body = str(model.get("body") or "")
    return {
        "id": model.get("id"),
        "name": model.get("name"),
        "version": model.get("version"),
        "path": model.get("path"),
        "body_preview": body[:2000] + ("...[truncated]" if len(body) > 2000 else ""),
    }


def _asset_fingerprint(item: Any) -> dict[str, Any]:
    body = str(getattr(item, "body", "") or "")
    frontmatter = getattr(item, "frontmatter", {}) or {}
    return {
        "id": str(getattr(item, "id", "") or ""),
        "type": str(getattr(item, "type", "") or ""),
        "parent_id": str(getattr(item, "parent_id", "") or ""),
        "body_sha256": hashlib.sha256(body.encode("utf-8")).hexdigest(),
        "frontmatter": _canonicalize(frontmatter),
    }


def _content_hash_payload(
    *,
    model_context: dict[str, Any],
    resolution: dict[str, Any],
) -> dict[str, Any]:
    return {
        "model": {
            "id": model_context.get("id"),
            "version": model_context.get("version"),
            "frontmatter": _canonicalize({
                key: (model_context.get("frontmatter") or {}).get(key)
                for key in (
                    "formatter",
                    "id",
                    "name",
                    "type",
                    "version",
                    "description",
                    "semantic_assets",
                    "asset_relations",
                    "guardrails",
                    "acceptance",
                )
                if key in (model_context.get("frontmatter") or {})
            }),
            "body_sha256": hashlib.sha256(
                str(model_context.get("body") or "").encode("utf-8")
            ).hexdigest(),
            "relations": _canonicalize(model_context.get("asset_relations") or []),
        },
        "resolution_mode": resolution.get("resolution_mode"),
        "matched": sorted(
            (_asset_fingerprint(item) for item in resolution.get("matched") or []),
            key=lambda item: (item["type"], item["id"]),
        ),
        "references": sorted(
            (_asset_fingerprint(item) for item in resolution.get("references") or []),
            key=lambda item: (item["type"], item["id"]),
        ),
        "unmatched_requested_ids": sorted(resolution.get("unmatched_requested_ids") or []),
    }


def compile_semantic_query_context(
    *,
    question: str,
    model_id: str | None = None,
    selected_semantic_asset_ids: list[str] | None = None,
    base_dir: Path | None = None,
    model_registry: Any | None = None,
    allow_global_fuzzy: bool = True,
    normalize_selected_ids: bool = False,
    strict_selected_ids: bool = False,
) -> SemanticQueryContext:
    """Resolve model scope and semantic assets once for all execution paths."""

    normalized_model_id = str(model_id or "").strip()
    registry = model_registry or get_analytics_model_registry(base_dir)
    model_context: dict[str, Any] = {}
    allowed_ids: list[str] | None = None
    if normalized_model_id:
        model_context = registry.get_model_context(normalized_model_id)
        allowed_ids = [
            str(item.get("id") or "").strip()
            for item in model_context.get("semantic_assets") or []
            if str(item.get("id") or "").strip()
        ]

    selected_ids = list(
        dict.fromkeys(
            str(item).strip()
            for item in selected_semantic_asset_ids or []
            if str(item).strip()
        )
    )
    if selected_ids and allowed_ids is not None and (normalize_selected_ids or strict_selected_ids):
        selected_ids, normalization_error = normalize_selected_semantic_asset_ids(
            selected_ids,
            set(allowed_ids),
        )
        if normalization_error and strict_selected_ids:
            raise ValueError(normalization_error)
    if selected_ids:
        resolution = resolve_semantic_assets_by_ids(
            question,
            requested_ids=selected_ids,
            allowed_ids=allowed_ids,
            base_dir=base_dir,
        )
    elif normalized_model_id or allow_global_fuzzy:
        resolution = resolve_semantic_assets(
            question,
            allowed_ids=allowed_ids if normalized_model_id else None,
            base_dir=base_dir,
        )
    else:
        resolution = {
            "matched": [],
            "references": [],
            "matched_count": 0,
            "reference_count": 0,
            "available_count": 0,
            "type_counts": {},
            "unmatched_requested_ids": [],
            "resolution_mode": "generalized",
        }
    if not resolution.get("matched") and not resolution.get("references"):
        resolution["resolution_mode"] = "generalized"

    trace = semantic_resolution_to_trace(resolution)
    model_trace = _model_trace(model_context)
    if model_trace:
        trace["analytics_model"] = model_trace
    source_refs = [
        str(item.get("ref") or "").strip()
        for item in model_context.get("data_assets") or []
        if isinstance(item, dict) and str(item.get("ref") or "").strip()
    ]
    if source_refs:
        trace["source_refs"] = source_refs
    warnings: list[dict[str, Any]] = []
    if model_context.get("missing_references"):
        warnings.append(
            {
                "type": "missing_model_references",
                "ids": list(model_context.get("missing_references") or []),
            }
        )
    if model_context.get("missing_data_assets"):
        warnings.append(
            {
                "type": "missing_model_data_assets",
                "ids": list(model_context.get("missing_data_assets") or []),
            }
        )
    if warnings:
        trace["warnings"] = warnings

    hash_payload = _content_hash_payload(
        model_context=model_context,
        resolution=resolution,
    )
    encoded = json.dumps(
        hash_payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    digest = hashlib.sha256(encoded).hexdigest()
    context_id = f"semctx-{digest[:16]}"
    content_hash = f"sha256:{digest}"
    trace["semantic_context_id"] = context_id
    trace["semantic_context_hash"] = content_hash
    trace["semantic_hash"] = content_hash

    return SemanticQueryContext(
        question=question,
        model_id=normalized_model_id,
        model_version=str(model_context.get("version") or ""),
        model_context=model_context,
        resolution=resolution,
        trace=trace,
        context_id=context_id,
        semantic_hash=content_hash,
    )
