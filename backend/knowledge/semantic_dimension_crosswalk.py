"""Versioned semantic-dimension Crosswalk state and manual overrides."""

from __future__ import annotations

import copy
import json
import os
import re
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo


DISPLAY_TIMEZONE = ZoneInfo("Asia/Shanghai")
GENERATED_FILE = "generated_crosswalk.json"
ACTIVE_FILE = "active_crosswalk.json"
OVERRIDES_FILE = "manual_overrides.json"
REGISTRY_FILE = "source_registry.json"
VERSIONS_DIR = "versions"
SEMVER_RE = re.compile(r"^v(\d+)\.(\d+)\.(\d+)\.json$")


class SemanticDimensionCrosswalkError(ValueError):
    """Raised for invalid semantic Crosswalk state or override input."""


def _atomic_write(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", delete=False, dir=path.parent, prefix=f".{path.name}.", suffix=".tmp"
    ) as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
        temporary_path = Path(handle.name)
    os.replace(temporary_path, path)


def _read_json(path: Path, default: dict[str, Any]) -> dict[str, Any]:
    if not path.is_file():
        return copy.deepcopy(default)
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise SemanticDimensionCrosswalkError(f"Invalid JSON: {path.name}") from exc
    if not isinstance(value, dict):
        raise SemanticDimensionCrosswalkError(f"Invalid object payload: {path.name}")
    return value


def _dimension_dir(base_dir: Path, dimension_id: str) -> Path:
    candidate = (base_dir / "semantic-assets" / "dimensions" / dimension_id).resolve()
    root = (base_dir / "semantic-assets" / "dimensions").resolve()
    if root not in candidate.parents:
        raise SemanticDimensionCrosswalkError("Invalid semantic dimension id")
    return candidate


def _references_dir(base_dir: Path, dimension_id: str) -> Path:
    return _dimension_dir(base_dir, dimension_id) / "references"


def _empty_overrides(dimension_id: str) -> dict[str, Any]:
    return {
        "formatter": "semantic-dimension-manual-overrides",
        "schema_version": "semantic-dimension-manual-overrides/v1",
        "dimension_id": dimension_id,
        # `overrides` is the editable draft. The runtime only reads the
        # separately materialized active_crosswalk.json after publication.
        "overrides": [],
        "published_overrides": [],
        # Canonical entity lifecycle is independent from source-key rebinding.
        # `inactive` stays visible for audit; `remove` is a durable tombstone.
        "entity_overrides": [],
        "published_entity_overrides": [],
        "published_at": "",
    }


def _normalized_overrides(payload: dict[str, Any], dimension_id: str) -> dict[str, Any]:
    """Normalize legacy override files while keeping their prior state published.

    Older files only had `overrides`, and those edits had already been applied
    to active_crosswalk.json. Treating that list as both draft and published
    preserves the live version when moving to explicit publication.
    """

    normalized = _empty_overrides(dimension_id)
    normalized.update({key: value for key, value in payload.items() if key in normalized or key == "updated_at"})
    draft = [item for item in normalized.get("overrides") or [] if isinstance(item, dict)]
    published_raw = normalized.get("published_overrides")
    published = draft if not isinstance(published_raw, list) else [item for item in published_raw if isinstance(item, dict)]
    normalized["overrides"] = copy.deepcopy(draft)
    normalized["published_overrides"] = copy.deepcopy(published)
    return normalized


def _has_pending_override_changes(overrides: dict[str, Any]) -> bool:
    return any(
        json.dumps(overrides.get(draft_key) or [], ensure_ascii=False, sort_keys=True)
        != json.dumps(overrides.get(published_key) or [], ensure_ascii=False, sort_keys=True)
        for draft_key, published_key in (
            ("overrides", "published_overrides"),
            ("entity_overrides", "published_entity_overrides"),
        )
    )


def _binding_key(source_ref: str, source_key: dict[str, Any]) -> tuple[str, str]:
    return source_ref, json.dumps(source_key, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _source_identity(binding: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    source_ref = str(binding.get("source_ref") or "").strip()
    key_fields = binding.get("key_fields") if isinstance(binding.get("key_fields"), dict) else {}
    if not source_ref or not key_fields:
        raise SemanticDimensionCrosswalkError("A source binding requires source_ref and key_fields")
    return source_ref, {str(key): value for key, value in key_fields.items()}


def _binding_source_id(binding: dict[str, Any]) -> str:
    """Return the logical source identity used across recurring source files."""

    return str(binding.get("source_id") or binding.get("source_ref") or "").strip()


def _override_matches_binding(override: dict[str, Any], binding: dict[str, Any]) -> bool:
    """Match a manual rule to either one file or its reusable logical source."""

    source_key = override.get("source_key") if isinstance(override.get("source_key"), dict) else {}
    if not source_key:
        return False
    try:
        source_ref, binding_key = _source_identity(binding)
    except SemanticDimensionCrosswalkError:
        return False
    if _binding_key(source_ref, binding_key) == _binding_key(str(override.get("source_ref") or ""), source_key):
        return True
    if str(override.get("scope") or "source_ref") != "source_id":
        return False
    source_id = str(override.get("source_id") or "").strip()
    return bool(source_id) and source_id == _binding_source_id(binding) and binding_key == source_key


def _matching_bindings_for_override(crosswalk: dict[str, Any], override: dict[str, Any]) -> list[dict[str, Any]]:
    """Find every current binding covered by an override, including new files."""

    matches: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for collection_name in ("records", "source_diagnostics"):
        for record in crosswalk.get(collection_name) or []:
            if not isinstance(record, dict):
                continue
            for binding in _source_bindings_for_record(record):
                if not _override_matches_binding(override, binding):
                    continue
                try:
                    identity = _binding_key(*_source_identity(binding))
                except SemanticDimensionCrosswalkError:
                    continue
                if identity in seen:
                    continue
                seen.add(identity)
                matches.append(copy.deepcopy(binding))
    return matches


def _is_canonical_binding(binding: dict[str, Any]) -> bool:
    return str(binding.get("source_kind") or "") == "database_table" and str(binding.get("source_ref") or "").startswith("database:")


def _source_bindings_for_record(record: dict[str, Any]) -> list[dict[str, Any]]:
    """Canonical-entity records store their canonical binding first by contract."""

    bindings = [binding for binding in record.get("bindings") or [] if isinstance(binding, dict)]
    if str(record.get("record_kind") or "") == "canonical_entity":
        return bindings[1:]
    return [binding for binding in bindings if not _is_canonical_binding(binding)]


def _entity_label(entity: dict[str, Any]) -> str:
    """Display every canonical attribute in its stored Crosswalk order."""

    values = [str(value).strip() for key, value in entity.items() if key != "entity_key" and str(value or "").strip()]
    return " / ".join(values) or str(entity.get("entity_key") or "")


def _source_registry(crosswalk: dict[str, Any], *, dimension_id: str, existing: dict[str, Any] | None = None) -> dict[str, Any]:
    previous = {
        str(item.get("id") or ""): item
        for item in (existing or {}).get("sources", [])
        if isinstance(item, dict) and item.get("id")
    }
    entries: dict[str, dict[str, Any]] = {}
    build_bindings = {
        str(item.get("id") or ""): item
        for item in ((crosswalk.get("build_rule") or {}).get("bindings") or [])
        if isinstance(item, dict)
    }
    canonical_rule = next((item for item in build_bindings.values() if item.get("role") == "canonical"), {})
    canonical_fields = list(canonical_rule.get("output_fields") or canonical_rule.get("key_fields") or [])
    for record in [*(crosswalk.get("records") or []), *(crosswalk.get("source_diagnostics") or [])]:
        if not isinstance(record, dict):
            continue
        for binding in _source_bindings_for_record(record):
            source_ref, key_fields = _source_identity(binding)
            source_id = str(binding.get("source_id") or source_ref)
            previous_entry = previous.get(source_id, {})
            rule = next((item for item in build_bindings.values() if str(item.get("display_name") or "") == str(binding.get("source_name") or "")), {})
            source_fields = list(rule.get("key_fields") or key_fields.keys())
            derived_mapping = [
                {"canonical_field": canonical_fields[index] if index < len(canonical_fields) else f"key_{index + 1}", "source_field": source_field}
                for index, source_field in enumerate(source_fields)
            ]
            entries[source_id] = {
                "id": source_id,
                "name": str(previous_entry.get("name") or binding.get("source_profile_name") or binding.get("source_name") or source_id),
                "kind": str(binding.get("source_kind") or "unknown"),
                "table_or_sheet": str(binding.get("table_or_sheet") or ""),
                "identity_fields": list(key_fields.keys()),
                "mapping": list(previous_entry.get("mapping") or derived_mapping),
            }
    return {
        "formatter": "semantic-dimension-source-registry",
        "schema_version": "semantic-dimension-source-registry/v1",
        "dimension_id": dimension_id,
        "sources": [entries[key] for key in sorted(entries)],
    }


def _find_binding(crosswalk: dict[str, Any], source_ref: str, source_key: dict[str, Any]) -> dict[str, Any] | None:
    expected = _binding_key(source_ref, source_key)
    for collection_name in ("records", "source_diagnostics"):
        for record in crosswalk.get(collection_name) or []:
            if not isinstance(record, dict):
                continue
            for binding in _source_bindings_for_record(record):
                try:
                    identity = _source_identity(binding)
                except SemanticDimensionCrosswalkError:
                    continue
                if _binding_key(*identity) == expected:
                    return copy.deepcopy(binding)
    return None


def _remove_source_binding(crosswalk: dict[str, Any], source_ref: str, source_key: dict[str, Any]) -> None:
    expected = _binding_key(source_ref, source_key)
    for record in crosswalk.get("records") or []:
        if not isinstance(record, dict):
            continue
        all_bindings = [binding for binding in record.get("bindings") or [] if isinstance(binding, dict)]
        canonical_bindings = all_bindings[:1] if str(record.get("record_kind") or "") == "canonical_entity" else [binding for binding in all_bindings if _is_canonical_binding(binding)]
        source_bindings = [
            binding for binding in _source_bindings_for_record(record)
            if _binding_key(*_source_identity(binding)) != expected
        ]
        record["bindings"] = [*canonical_bindings, *source_bindings]
        resolution = record.setdefault("resolution", {})
        if not source_bindings:
            resolution.update({
                "status": "canonical_only",
                "join_eligible": False,
                "method": "canonical_baseline",
                "confidence": 1.0,
            })
    crosswalk["source_diagnostics"] = [
        record for record in crosswalk.get("source_diagnostics") or []
        if not isinstance(record, dict)
        or not any(_binding_key(*_source_identity(binding)) == expected for binding in _source_bindings_for_record(record))
    ]


def materialize_crosswalk(generated: dict[str, Any], overrides: dict[str, Any]) -> dict[str, Any]:
    """Apply user decisions to a generated Crosswalk without mutating the baseline."""

    crosswalk = copy.deepcopy(generated)
    records = [record for record in crosswalk.get("records") or [] if isinstance(record, dict)]
    by_entity_key = {
        str((record.get("entity") or {}).get("entity_key") or ""): record
        for record in records
    }
    crosswalk["source_diagnostics"] = list(crosswalk.get("source_diagnostics") or [])

    for override in overrides.get("overrides") or []:
        if not isinstance(override, dict):
            continue
        source_ref = str(override.get("source_ref") or "").strip()
        source_key = override.get("source_key") if isinstance(override.get("source_key"), dict) else {}
        action = str(override.get("action") or "").strip()
        if not source_ref or not source_key or action not in {"bind", "exclude"}:
            continue
        # A user normally decides the business meaning of a source key, not
        # just the one monthly file where they first noticed it. Reapply a
        # source_id-scoped decision to every matching source instance.
        bindings = _matching_bindings_for_override(crosswalk, override)
        if not bindings:
            binding = _find_binding(crosswalk, source_ref, source_key)
            if binding:
                bindings = [binding]
        if not bindings:
            continue
        target = by_entity_key.get(str(override.get("target_entity_key") or ""))
        if action == "bind" and target is None:
            raise SemanticDimensionCrosswalkError("Manual override target_entity_key does not exist in generated Crosswalk")
        for binding in bindings:
            binding_ref, binding_key = _source_identity(binding)
            _remove_source_binding(crosswalk, binding_ref, binding_key)
            if action == "exclude":
                crosswalk["source_diagnostics"].append({
                    "record_kind": "source_diagnostic",
                    "entity": None,
                    "bindings": [binding],
                    "resolution": {
                        "status": "manual_excluded",
                        "join_eligible": False,
                        "method": "manual_override",
                        "confidence": 1.0,
                        "candidate_series": [],
                        "evidence": [str(override.get("reason") or "用户明确标记为不关联。")],
                    },
                })
                continue
            assert target is not None
            target.setdefault("bindings", []).append(binding)
            target["resolution"] = {
                "status": "manual_override",
                "join_eligible": True,
                "method": "manual_override",
                "confidence": 1.0,
                "candidate_series": [],
                "evidence": [str(override.get("reason") or "用户在匹配管理中确认。")],
            }

    # Apply canonical lifecycle last: a removed entity cannot be a target for a
    # source override, and an inactive one remains auditable but non-joinable.
    lifecycle_by_entity = {
        str(item.get("entity_key") or ""): item
        for item in overrides.get("entity_overrides") or []
        if isinstance(item, dict) and str(item.get("entity_key") or "")
    }
    materialized_records: list[dict[str, Any]] = []
    for record in records:
        entity_key = str((record.get("entity") or {}).get("entity_key") or "")
        lifecycle = lifecycle_by_entity.get(entity_key)
        action = str((lifecycle or {}).get("action") or "active")
        if action == "remove":
            continue
        if action == "inactive":
            resolution = record.setdefault("resolution", {})
            resolution.update({
                "status": "inactive",
                "join_eligible": False,
                "method": "manual_lifecycle_override",
                "confidence": 1.0,
                "candidate_series": [],
                "evidence": [str((lifecycle or {}).get("reason") or "用户在匹配管理中停用规范实体。")],
            })
            record["lifecycle"] = {"state": "inactive", "override_id": lifecycle.get("id")}
        materialized_records.append(record)

    crosswalk["records"] = materialized_records
    crosswalk["manual_override_count"] = len(overrides.get("overrides") or [])
    crosswalk["manual_entity_override_count"] = len(overrides.get("entity_overrides") or [])
    crosswalk["materialized_at"] = datetime.now(timezone.utc).isoformat()
    return crosswalk


def _next_version(references_dir: Path) -> str:
    highest = (0, 0, 0)
    for path in (references_dir / VERSIONS_DIR).glob("v*.json"):
        match = SEMVER_RE.fullmatch(path.name)
        if match:
            highest = max(highest, tuple(int(value) for value in match.groups()))
    if highest == (0, 0, 0):
        return "v0.1.0"
    return f"v{highest[0]}.{highest[1]}.{highest[2] + 1}"


def load_crosswalk_state(base_dir: Path, dimension_id: str) -> dict[str, Any]:
    references_dir = _references_dir(base_dir, dimension_id)
    generated_path = references_dir / GENERATED_FILE
    active_path = references_dir / ACTIVE_FILE
    if not generated_path.is_file() and active_path.is_file():
        _atomic_write(generated_path, _read_json(active_path, {}))
    generated = _read_json(generated_path, {})
    if not generated:
        raise SemanticDimensionCrosswalkError("该维度尚未发布可编辑的 Crosswalk")
    overrides = _normalized_overrides(
        _read_json(references_dir / OVERRIDES_FILE, _empty_overrides(dimension_id)),
        dimension_id,
    )
    registry = _read_json(references_dir / REGISTRY_FILE, {})
    active = _read_json(active_path, generated)
    # Backfill existing file-scoped overrides. Previous UI versions did not
    # persist source_id, although the active bindings already carried it.
    source_ids_by_ref: dict[str, set[str]] = {}
    for crosswalk in (generated, active):
        for record in [*(crosswalk.get("records") or []), *(crosswalk.get("source_diagnostics") or [])]:
            if not isinstance(record, dict):
                continue
            for binding in _source_bindings_for_record(record):
                source_ref = str(binding.get("source_ref") or "")
                source_id = _binding_source_id(binding)
                if source_ref and source_id:
                    source_ids_by_ref.setdefault(source_ref, set()).add(source_id)
    changed = False
    # Keep editable drafts and the published snapshot on the same schema.
    # Otherwise a one-time metadata migration looks like a user edit waiting
    # for publication even though its target/action did not change.
    for collection_name in ("overrides", "published_overrides"):
        for override in overrides.get(collection_name) or []:
            if not isinstance(override, dict) or override.get("source_id"):
                continue
            candidates = source_ids_by_ref.get(str(override.get("source_ref") or ""), set())
            if len(candidates) == 1:
                override["source_id"] = next(iter(candidates))
                override["scope"] = "source_id"
                changed = True
    if changed:
        overrides["updated_at"] = datetime.now(DISPLAY_TIMEZONE).strftime("%Y-%m-%d %H:%M:%S（北京时间）")
        _atomic_write(references_dir / OVERRIDES_FILE, overrides)
    return {"generated": generated, "overrides": overrides, "registry": registry, "active": active, "references_dir": references_dir}


def list_registered_sources(base_dir: Path, dimension_id: str) -> list[dict[str, Any]]:
    registry = _read_json(_references_dir(base_dir, dimension_id) / REGISTRY_FILE, {})
    return [item for item in registry.get("sources") or [] if isinstance(item, dict)]


def _serialize_rows(active: dict[str, Any], overrides: dict[str, Any]) -> list[dict[str, Any]]:
    override_items = [item for item in overrides.get("overrides") or [] if isinstance(item, dict)]
    rows: list[dict[str, Any]] = []
    for record in active.get("records") or []:
        if not isinstance(record, dict):
            continue
        entity = record.get("entity") if isinstance(record.get("entity"), dict) else {}
        source_bindings = _source_bindings_for_record(record)
        if not source_bindings:
            rows.append({"entity_key": entity.get("entity_key"), "canonical": entity, "canonical_label": _entity_label(entity), "status": record.get("resolution", {}).get("status"), "binding": None, "manual": False})
        for binding in source_bindings:
            source_ref, source_key = _source_identity(binding)
            override_id = next((str(item.get("id") or "") for item in override_items if _override_matches_binding(item, binding)), "")
            rows.append({"entity_key": entity.get("entity_key"), "canonical": entity, "canonical_label": _entity_label(entity), "status": record.get("resolution", {}).get("status"), "binding": binding, "manual": bool(override_id), "override_id": override_id})
    for record in active.get("source_diagnostics") or []:
        if not isinstance(record, dict):
            continue
        resolution = record.get("resolution") if isinstance(record.get("resolution"), dict) else {}
        for binding in _source_bindings_for_record(record):
            source_ref, source_key = _source_identity(binding)
            override_id = next((str(item.get("id") or "") for item in override_items if _override_matches_binding(item, binding)), "")
            rows.append({"entity_key": None, "canonical": None, "canonical_label": "", "status": resolution.get("status"), "binding": binding, "manual": bool(override_id), "override_id": override_id})
    return rows


def _row_matches_query(row: dict[str, Any], query: str) -> bool:
    if not query:
        return True
    binding = row.get("binding") if isinstance(row.get("binding"), dict) else {}
    values = [row.get("entity_key"), row.get("canonical_label"), binding.get("source_name"), binding.get("source_ref")]
    values.extend((binding.get("key_fields") or {}).values() if isinstance(binding.get("key_fields"), dict) else [])
    values.append(" ".join(str(value or "") for value in values))
    needle = re.sub(r"[\s/_:\-]+", "", query).casefold()
    return any(needle in re.sub(r"[\s/_:\-]+", "", str(value or "")).casefold() for value in values)


def get_matching_view(base_dir: Path, dimension_id: str, *, status: str = "", source_ref: str = "", query: str = "", offset: int = 0, limit: int = 100) -> dict[str, Any]:
    state = load_crosswalk_state(base_dir, dimension_id)
    # The editor intentionally renders the draft preview. The active version
    # remains unchanged until publish_draft_overrides() is called.
    preview = materialize_crosswalk(state["generated"], state["overrides"])
    rows = _serialize_rows(preview, state["overrides"])
    if status:
        rows = [row for row in rows if str(row.get("status") or "") == status]
    if source_ref:
        rows = [
            row for row in rows
            if str((row.get("binding") or {}).get("source_id") or (row.get("binding") or {}).get("source_ref") or "") == source_ref
        ]
    if query.strip():
        rows = [row for row in rows if _row_matches_query(row, query.strip())]
    all_rows = _serialize_rows(preview, state["overrides"])
    status_counts: dict[str, int] = {}
    for row in all_rows:
        key = str(row.get("status") or "unknown")
        status_counts[key] = status_counts.get(key, 0) + 1
    return {
        "dimension_id": dimension_id,
        "version": str(state["active"].get("version") or "unversioned"),
        "generated_at_display": state["active"].get("generated_at_display"),
        "summary": {
            "canonical_entities": len(state["active"].get("records") or []),
            "manual_overrides": len(state["overrides"].get("overrides") or []),
            "manual_entity_overrides": len(state["overrides"].get("entity_overrides") or []),
            "published_manual_overrides": len(state["overrides"].get("published_overrides") or []),
            "has_unpublished_changes": _has_pending_override_changes(state["overrides"]),
            "sources": len(state["registry"].get("sources") or []),
            "status_counts": status_counts,
        },
        "sources": state["registry"].get("sources") or [],
        "entity_options": [
            {
                "entity_key": (record.get("entity") or {}).get("entity_key"),
                "label": _entity_label(record.get("entity") or {}),
            }
            for record in state["active"].get("records") or []
            if isinstance(record, dict)
            and (record.get("entity") or {}).get("entity_key")
            and str((record.get("resolution") or {}).get("status") or "") != "inactive"
        ],
        "rows": rows[offset : offset + limit],
        "count": len(rows),
        "offset": offset,
        "limit": limit,
    }


def get_matching_overview(base_dir: Path, dimension_id: str, *, query: str = "", offset: int = 0, limit: int = 100) -> dict[str, Any]:
    """Return one row per canonical entity, with a cell for each registered source."""

    state = load_crosswalk_state(base_dir, dimension_id)
    override_keys = {
        _binding_key(str(item.get("source_ref") or ""), item.get("source_key") or {}): str(item.get("id") or "")
        for item in state["overrides"].get("overrides") or []
        if isinstance(item, dict) and isinstance(item.get("source_key"), dict)
    }
    preview = materialize_crosswalk(state["generated"], state["overrides"])
    source_columns = state["registry"].get("sources") or []
    rows: list[dict[str, Any]] = []
    for record in preview.get("records") or []:
        if not isinstance(record, dict):
            continue
        entity = record.get("entity") if isinstance(record.get("entity"), dict) else {}
        cells: dict[str, list[dict[str, Any]]] = {}
        for binding in _source_bindings_for_record(record):
            source_ref, source_key = _source_identity(binding)
            source_id = str(binding.get("source_id") or source_ref)
            cells.setdefault(source_id, []).append({
                "source_ref": source_ref,
                "source_key": source_key,
                "manual": bool(override_keys.get(_binding_key(source_ref, source_key))),
            })
        resolution = record.get("resolution") if isinstance(record.get("resolution"), dict) else {}
        rows.append({
            "entity_key": entity.get("entity_key"),
            "canonical": entity,
            "canonical_label": _entity_label(entity),
            "status": resolution.get("status"),
            "source_cells": cells,
        })
    if query.strip():
        needle = re.sub(r"[\s/_:\-]+", "", query.strip()).casefold()
        rows = [
            row for row in rows
            if needle in re.sub(r"[\s/_:\-]+", "", str(row.get("entity_key") or "")).casefold()
            or needle in re.sub(r"[\s/_:\-]+", "", str(row.get("canonical_label") or "")).casefold()
            or any(needle in re.sub(r"[\s/_:\-]+", "", " ".join(str(value or "") for value in (cell.get("source_key") or {}).values())).casefold() for cells in (row.get("source_cells") or {}).values() for cell in cells)
        ]
    return {
        "dimension_id": dimension_id,
        "version": str(state["active"].get("version") or "unversioned"),
        "has_unpublished_changes": _has_pending_override_changes(state["overrides"]),
        "summary": {
            "canonical_entities": len(preview.get("records") or []),
            "manual_overrides": len(state["overrides"].get("overrides") or []),
            "manual_entity_overrides": len(state["overrides"].get("entity_overrides") or []),
            "published_manual_overrides": len(state["overrides"].get("published_overrides") or []),
            "sources": len(source_columns),
        },
        "sources": source_columns,
        "rows": rows[offset : offset + limit],
        "count": len(rows),
        "offset": offset,
        "limit": limit,
    }


def save_override(base_dir: Path, dimension_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    state = load_crosswalk_state(base_dir, dimension_id)
    source_ref = str(payload.get("source_ref") or "").strip()
    source_key = payload.get("source_key") if isinstance(payload.get("source_key"), dict) else {}
    action = str(payload.get("action") or "").strip()
    if not source_ref or not source_key or action not in {"bind", "exclude"}:
        raise SemanticDimensionCrosswalkError("source_ref, source_key and action(bind/exclude) are required")
    target = str(payload.get("target_entity_key") or "").strip()
    if action == "bind" and not target:
        raise SemanticDimensionCrosswalkError("target_entity_key is required for bind")
    resolved_binding = _find_binding(state["generated"], source_ref, source_key) or _find_binding(state["active"], source_ref, source_key)
    source_id = str(payload.get("source_id") or (resolved_binding or {}).get("source_id") or "").strip()
    scope = str(payload.get("scope") or ("source_id" if source_id else "source_ref"))
    if scope not in {"source_id", "source_ref"}:
        raise SemanticDimensionCrosswalkError("scope must be source_id or source_ref")
    overrides = state["overrides"]
    existing = [item for item in overrides.get("overrides") or [] if isinstance(item, dict)]
    expected = _binding_key(source_ref, source_key)
    existing = [item for item in existing if not (
        _binding_key(str(item.get("source_ref") or ""), item.get("source_key") or {}) == expected
        or (scope == "source_id" and source_id and str(item.get("source_id") or "") == source_id and item.get("source_key") == source_key)
    )]
    override = {
        "id": str(payload.get("id") or f"ovr_{uuid.uuid4().hex[:16]}"),
        "source_ref": source_ref,
        "source_id": source_id,
        "scope": scope,
        "source_key": source_key,
        "action": action,
        "target_entity_key": target,
        "reason": str(payload.get("reason") or "用户在匹配管理中确认。"),
        "source_name": str(payload.get("source_name") or ""),
        "source_kind": str(payload.get("source_kind") or ""),
        "table_or_sheet": str(payload.get("table_or_sheet") or ""),
        "updated_at": datetime.now(DISPLAY_TIMEZONE).strftime("%Y-%m-%d %H:%M:%S"),
    }
    existing.append(override)
    overrides["overrides"] = existing
    overrides["updated_at"] = datetime.now(DISPLAY_TIMEZONE).strftime("%Y-%m-%d %H:%M:%S（北京时间）")
    _atomic_write(state["references_dir"] / OVERRIDES_FILE, overrides)
    return {"override": override, "has_unpublished_changes": True}


def delete_override(base_dir: Path, dimension_id: str, override_id: str) -> dict[str, Any]:
    state = load_crosswalk_state(base_dir, dimension_id)
    previous = [item for item in state["overrides"].get("overrides") or [] if isinstance(item, dict)]
    remaining = [item for item in previous if str(item.get("id") or "") != override_id]
    if len(previous) == len(remaining):
        raise SemanticDimensionCrosswalkError("Manual override not found")
    state["overrides"]["overrides"] = remaining
    state["overrides"]["updated_at"] = datetime.now(DISPLAY_TIMEZONE).strftime("%Y-%m-%d %H:%M:%S（北京时间）")
    _atomic_write(state["references_dir"] / OVERRIDES_FILE, state["overrides"])
    return {"deleted": override_id, "has_unpublished_changes": _has_pending_override_changes(state["overrides"])}


def save_entity_override(base_dir: Path, dimension_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    """Save a draft lifecycle decision for one canonical entity."""

    state = load_crosswalk_state(base_dir, dimension_id)
    entity_key = str(payload.get("entity_key") or "").strip()
    action = str(payload.get("action") or "").strip()
    if not entity_key or action not in {"active", "inactive", "remove"}:
        raise SemanticDimensionCrosswalkError("entity_key and action(active/inactive/remove) are required")
    known_keys = {
        str((record.get("entity") or {}).get("entity_key") or "")
        for crosswalk in (state["generated"], state["active"])
        for record in crosswalk.get("records") or []
        if isinstance(record, dict)
    }
    if entity_key not in known_keys:
        raise SemanticDimensionCrosswalkError("Canonical entity does not exist in this dimension")
    overrides = state["overrides"]
    existing = [item for item in overrides.get("entity_overrides") or [] if isinstance(item, dict)]
    existing = [item for item in existing if str(item.get("entity_key") or "") != entity_key]
    override = {
        "id": str(payload.get("id") or f"eovr_{uuid.uuid4().hex[:16]}"),
        "entity_key": entity_key,
        "action": action,
        "reason": str(payload.get("reason") or "用户在匹配管理中维护规范实体。"),
        "updated_at": datetime.now(DISPLAY_TIMEZONE).strftime("%Y-%m-%d %H:%M:%S（北京时间）"),
    }
    existing.append(override)
    overrides["entity_overrides"] = existing
    overrides["updated_at"] = datetime.now(DISPLAY_TIMEZONE).strftime("%Y-%m-%d %H:%M:%S（北京时间）")
    _atomic_write(state["references_dir"] / OVERRIDES_FILE, overrides)
    return {"override": override, "has_unpublished_changes": _has_pending_override_changes(overrides)}


def retain_staged_entities_as_inactive(base_dir: Path, dimension_id: str, staged: dict[str, Any], entity_keys: list[str]) -> dict[str, Any]:
    """Carry removed active entities into a staged build as inactive records."""

    state = load_crosswalk_state(base_dir, dimension_id)
    wanted = {str(key) for key in entity_keys if str(key)}
    present = {
        str((record.get("entity") or {}).get("entity_key") or "")
        for record in staged.get("records") or []
        if isinstance(record, dict)
    }
    restored: list[str] = []
    for record in state["active"].get("records") or []:
        if not isinstance(record, dict):
            continue
        entity_key = str((record.get("entity") or {}).get("entity_key") or "")
        if entity_key not in wanted or entity_key in present:
            continue
        copied = copy.deepcopy(record)
        copied["resolution"] = {
            "status": "inactive",
            "join_eligible": False,
            "method": "baseline_change_inactive",
            "confidence": 1.0,
            "candidate_series": [],
            "evidence": ["规范基准已不再提供该实体，用户选择保留为停用状态。"],
        }
        copied["lifecycle"] = {"state": "inactive", "reason": "baseline_change"}
        staged.setdefault("records", []).append(copied)
        restored.append(entity_key)
    return {"staged": staged, "restored": restored}


def publish_draft_overrides(base_dir: Path, dimension_id: str) -> dict[str, Any]:
    """Promote reviewed manual overrides into the active runtime Crosswalk."""

    state = load_crosswalk_state(base_dir, dimension_id)
    active = materialize_crosswalk(state["generated"], state["overrides"])
    version = _next_version(state["references_dir"])
    now = datetime.now(DISPLAY_TIMEZONE)
    active["version"] = version
    active["published_at"] = now.astimezone(timezone.utc).isoformat()
    active["published_at_display"] = now.strftime("%Y-%m-%d %H:%M:%S（北京时间）")
    _atomic_write(state["references_dir"] / ACTIVE_FILE, active)
    _atomic_write(state["references_dir"] / VERSIONS_DIR / f"{version}.json", active)
    state["overrides"]["published_overrides"] = copy.deepcopy(state["overrides"].get("overrides") or [])
    state["overrides"]["published_entity_overrides"] = copy.deepcopy(state["overrides"].get("entity_overrides") or [])
    state["overrides"]["published_at"] = active["published_at_display"]
    _atomic_write(state["references_dir"] / OVERRIDES_FILE, state["overrides"])
    return {"active": active, "version": version, "published_at_display": active["published_at_display"]}


def save_source_registry_entry(base_dir: Path, dimension_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    """Register a reusable source identity mapping without changing entity bindings."""

    state = load_crosswalk_state(base_dir, dimension_id)
    source_id = str(payload.get("id") or "").strip()
    name = str(payload.get("name") or "").strip()
    identity_fields = [str(field).strip() for field in payload.get("identity_fields") or [] if str(field).strip()]
    if not source_id or not name or not identity_fields:
        raise SemanticDimensionCrosswalkError("source id, name and identity_fields are required")
    entry = {
        "id": source_id,
        "name": name,
        "kind": str(payload.get("kind") or "unknown"),
        "table_or_sheet": str(payload.get("table_or_sheet") or ""),
        "identity_fields": identity_fields,
        "mapping": list(payload.get("mapping") or []),
    }
    registry = state["registry"]
    entries = [item for item in registry.get("sources") or [] if isinstance(item, dict) and str(item.get("id") or "") != source_id]
    entries.append(entry)
    registry["sources"] = sorted(entries, key=lambda item: str(item.get("name") or item.get("id") or ""))
    _atomic_write(state["references_dir"] / REGISTRY_FILE, registry)
    return {"source": entry, "registry": registry}


def publish_generated_crosswalk(base_dir: Path, dimension_id: str, staged_crosswalk: dict[str, Any]) -> dict[str, Any]:
    references_dir = _references_dir(base_dir, dimension_id)
    active_path = references_dir / ACTIVE_FILE
    generated_path = references_dir / GENERATED_FILE
    if not generated_path.is_file() and active_path.is_file():
        _atomic_write(generated_path, _read_json(active_path, {}))
    overrides = _normalized_overrides(
        _read_json(references_dir / OVERRIDES_FILE, _empty_overrides(dimension_id)),
        dimension_id,
    )
    registry = _source_registry(
        staged_crosswalk,
        dimension_id=dimension_id,
        existing=_read_json(references_dir / REGISTRY_FILE, {}),
    )
    _atomic_write(generated_path, staged_crosswalk)
    _atomic_write(references_dir / OVERRIDES_FILE, overrides)
    _atomic_write(references_dir / REGISTRY_FILE, registry)
    active = materialize_crosswalk(staged_crosswalk, overrides)
    version = _next_version(references_dir)
    active["version"] = version
    active["published_at"] = datetime.now(timezone.utc).isoformat()
    active["published_at_display"] = datetime.now(DISPLAY_TIMEZONE).strftime("%Y-%m-%d %H:%M:%S（北京时间）")
    _atomic_write(active_path, active)
    _atomic_write(references_dir / VERSIONS_DIR / f"{version}.json", active)
    overrides["published_overrides"] = copy.deepcopy(overrides.get("overrides") or [])
    overrides["published_entity_overrides"] = copy.deepcopy(overrides.get("entity_overrides") or [])
    overrides["published_at"] = active["published_at_display"]
    _atomic_write(references_dir / OVERRIDES_FILE, overrides)
    return {"active": active, "version": version, "registry": registry}
