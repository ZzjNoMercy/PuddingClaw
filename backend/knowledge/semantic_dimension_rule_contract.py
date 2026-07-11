"""Fixed, portable contracts for HITL-driven semantic-dimension builds."""

from __future__ import annotations

import re
from copy import deepcopy
from typing import Any


RULE_SCHEMA_VERSION = "semantic-dimension-build-rule/v1"
ARTIFACT_SCHEMA_VERSION = "entity-resolution-crosswalk/v1"
SUPPORTED_INPUT_KINDS = {"attachment", "table_asset", "database_table", "active_crosswalk"}
_ID_RE = re.compile(r"^[A-Za-z0-9_-]+$")


class SemanticDimensionRuleError(ValueError):
    """Raised when an HITL build rule is malformed or unsafe."""


def _strings(values: Any, *, field: str, minimum: int = 0) -> list[str]:
    if not isinstance(values, list):
        raise SemanticDimensionRuleError(f"{field} must be a list")
    cleaned = [str(value).strip() for value in values if str(value).strip()]
    if len(cleaned) < minimum:
        raise SemanticDimensionRuleError(f"{field} requires at least {minimum} value(s)")
    if len(cleaned) != len(set(cleaned)):
        raise SemanticDimensionRuleError(f"{field} contains duplicate values")
    return cleaned


def build_rule_from_decision(request: dict[str, Any], decision: dict[str, Any]) -> dict[str, Any]:
    """Validate a user selection against the tool-supplied candidate template."""

    if str(decision.get("action") or "confirm") != "confirm":
        raise SemanticDimensionRuleError("Build rule was not confirmed")
    candidates = request.get("candidates")
    if not isinstance(candidates, list) or len(candidates) < 2:
        raise SemanticDimensionRuleError("At least two candidate inputs are required")
    candidate_by_id = {
        str(item.get("id") or ""): item
        for item in candidates
        if isinstance(item, dict) and str(item.get("id") or "")
    }
    registered_source_ids = {
        str(item.get("id") or "").strip()
        for item in request.get("registered_sources") or []
        if isinstance(item, dict) and str(item.get("id") or "").strip()
    }
    canonical_id = str(decision.get("canonical_candidate_id") or "").strip()
    if canonical_id not in candidate_by_id:
        raise SemanticDimensionRuleError("Canonical input must be selected from candidates")
    locked_canonical_id = str(request.get("locked_canonical_candidate_id") or "").strip()
    if locked_canonical_id and canonical_id != locked_canonical_id:
        raise SemanticDimensionRuleError("This operation must retain the current canonical baseline")
    decisions = decision.get("bindings")
    if not isinstance(decisions, list) or len(decisions) < 2:
        raise SemanticDimensionRuleError("At least two input bindings must be confirmed")

    bindings: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in decisions:
        if not isinstance(item, dict):
            raise SemanticDimensionRuleError("Binding decision must be an object")
        candidate_id = str(item.get("candidate_id") or "").strip()
        candidate = candidate_by_id.get(candidate_id)
        if candidate is None or candidate_id in seen:
            raise SemanticDimensionRuleError("Binding must select each candidate at most once")
        seen.add(candidate_id)
        available_fields = {str(value) for value in candidate.get("fields") or []}
        key_fields = _strings(item.get("key_fields"), field="key_fields", minimum=1)
        if not set(key_fields).issubset(available_fields):
            raise SemanticDimensionRuleError("Selected key field is not available on the chosen input")
        output_fields = _strings(item.get("output_fields"), field="output_fields", minimum=1)
        if len(output_fields) != len(key_fields):
            raise SemanticDimensionRuleError("output_fields must align one-to-one with key_fields")
        input_spec = deepcopy(candidate.get("input") or {})
        if str(input_spec.get("kind") or "") not in SUPPORTED_INPUT_KINDS:
            raise SemanticDimensionRuleError("Input kind is unsupported")
        source_id = str(item.get("source_id") or candidate.get("suggested_source_id") or candidate_id).strip()
        source_mode = str(item.get("source_mode") or "new").strip()
        if candidate_id != canonical_id and not _ID_RE.fullmatch(source_id):
            raise SemanticDimensionRuleError("source_id is invalid")
        if candidate_id != canonical_id and source_mode not in {"new", "append"}:
            raise SemanticDimensionRuleError("source_mode must be new or append")
        if candidate_id != canonical_id and source_id in registered_source_ids and source_mode != "append":
            raise SemanticDimensionRuleError(f"Registered source '{source_id}' must use append mode")
        if candidate_id != canonical_id and source_id not in registered_source_ids and source_mode == "append":
            raise SemanticDimensionRuleError(f"Unknown source '{source_id}' cannot use append mode")
        bindings.append(
            {
                "id": candidate_id,
                "role": "canonical" if candidate_id == canonical_id else "source",
                "display_name": str(candidate.get("display_name") or candidate_id),
                "source_id": source_id,
                "source_name": str(item.get("source_name") or candidate.get("suggested_source_name") or candidate.get("display_name") or candidate_id).strip(),
                "source_mode": source_mode,
                "input": input_spec,
                "key_fields": key_fields,
                "output_fields": output_fields,
            }
        )

    if canonical_id not in seen:
        raise SemanticDimensionRuleError("Canonical input must be included in bindings")
    canonical = next(binding for binding in bindings if binding["role"] == "canonical")
    source_bindings = [binding for binding in bindings if binding["role"] == "source"]
    if not source_bindings:
        raise SemanticDimensionRuleError("At least one source input is required")
    if any(len(canonical["key_fields"]) != len(binding["key_fields"]) for binding in source_bindings):
        raise SemanticDimensionRuleError("Canonical and every source key must have the same number of fields")

    template = request.get("rule_template") if isinstance(request.get("rule_template"), dict) else {}
    dimension_id = str(template.get("dimension_id") or request.get("dimension_id") or "").strip()
    if not _ID_RE.fullmatch(dimension_id):
        raise SemanticDimensionRuleError("dimension_id is invalid")
    reference_path = str(template.get("reference_path") or "references/active_crosswalk.json").strip()
    if not reference_path.startswith("references/") or ".." in reference_path:
        raise SemanticDimensionRuleError("reference_path must stay inside references/")

    registry_snapshot = [
        {
            "id": str(item.get("id") or "").strip(),
            "name": str(item.get("name") or "").strip(),
            "identity_fields": _strings(item.get("identity_fields") or [], field="registered_sources.identity_fields"),
        }
        for item in request.get("registered_sources") or []
        if isinstance(item, dict) and str(item.get("id") or "").strip()
    ]

    canonical_kind = str((canonical.get("input") or {}).get("kind") or "")
    return {
        "schema_version": RULE_SCHEMA_VERSION,
        "dimension_id": dimension_id,
        "adapter": str(template.get("adapter") or "entity_crosswalk_v1"),
        "artifact": {
            "schema": ARTIFACT_SCHEMA_VERSION,
            "reference_path": reference_path,
        },
        "operation": str(request.get("operation") or "refresh"),
        "canonical_strategy": {
            "type": "active_crosswalk" if canonical_kind == "active_crosswalk" else "source_of_truth",
            "binding_id": canonical_id,
        },
        "bindings": bindings,
        "resolution": {
            "mode": "normalized_exact",
            "conflict_policy": str(decision.get("conflict_policy") or "candidate"),
            "auto_publish_min_confidence": 1.0,
        },
        "merge": {"mode": "append_source_bindings"},
        # Workers validate a persisted rule later, outside the original HITL
        # request. Persist the registry context that made append valid.
        "registered_sources_snapshot": registry_snapshot,
    }


def validate_build_rule(rule: dict[str, Any]) -> dict[str, Any]:
    """Validate persisted rules before a worker or adapter consumes them."""

    snapshot = rule.get("registered_sources_snapshot")
    if not isinstance(snapshot, list):
        # Compatibility for rules written before the registry snapshot existed:
        # append was already accepted by the initial HITL validation, so retain
        # those source ids for the worker's structural revalidation.
        snapshot = [
            {"id": binding.get("source_id"), "name": binding.get("source_name")}
            for binding in rule.get("bindings") or []
            if isinstance(binding, dict)
            and binding.get("role") == "source"
            and binding.get("source_mode") == "append"
        ]
    canonical_id = str(((rule.get("canonical_strategy") or {}).get("binding_id")) or "")
    request = {
        "dimension_id": rule.get("dimension_id"),
        "rule_template": {
            "dimension_id": rule.get("dimension_id"),
            "adapter": rule.get("adapter"),
            "reference_path": (rule.get("artifact") or {}).get("reference_path"),
        },
        "registered_sources": snapshot,
        "operation": rule.get("operation") or "refresh",
        "locked_canonical_candidate_id": canonical_id if (rule.get("canonical_strategy") or {}).get("type") == "active_crosswalk" else "",
        "candidates": [
            {
                "id": binding.get("id"),
                "display_name": binding.get("display_name"),
                "input": binding.get("input"),
                "fields": binding.get("key_fields"),
            }
            for binding in rule.get("bindings") or []
            if isinstance(binding, dict)
        ],
    }
    decision = {
        "action": "confirm",
        "canonical_candidate_id": canonical_id,
        "conflict_policy": (rule.get("resolution") or {}).get("conflict_policy") or "candidate",
        "bindings": [
            {
                "candidate_id": binding.get("id"),
                "key_fields": binding.get("key_fields"),
                "output_fields": binding.get("output_fields"),
                "source_id": binding.get("source_id"),
                "source_name": binding.get("source_name"),
                "source_mode": binding.get("source_mode"),
            }
            for binding in rule.get("bindings") or []
            if isinstance(binding, dict)
        ],
    }
    return build_rule_from_decision(request, decision)
