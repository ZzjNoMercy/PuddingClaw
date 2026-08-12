"""Rule-driven entity Crosswalk builder used by the generic dimension Skill.

The script deliberately accepts only a validated build-rule JSON. It is not a
free-form SQL/matching engine: HITL selects inputs and fields, then this worker
performs deterministic normalized-exact matching and writes the fixed artifact
schema consumed by semantic_entity_lookup.
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import hashlib
import json
import re
import unicodedata
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pandas as pd
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from analytics.table_catalog import TableAssetCatalog
from graph.attachment_store import attachment_store
from knowledge.database_sources import database_source_url, get_database_source
from knowledge.semantic_dimension_rule_contract import validate_build_rule
from db import get_sessionmaker


BASE_DIR = Path(__file__).resolve().parents[3]
DISPLAY_TIMEZONE = ZoneInfo("Asia/Shanghai")
IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_$]*$")
# `+` distinguishes real product entities such as 小鹏 P7 and 小鹏 P7+.
# It must survive key normalization; whitespace and punctuation formatting do
# not carry that same business meaning and can still be normalized away.
NORMALIZE_RE = re.compile(r"[^0-9a-z\u4e00-\u9fff+]+")


def _time_fields() -> dict[str, str]:
    now = datetime.now(timezone.utc)
    return {
        "generated_at": now.isoformat(),
        "generated_at_display": f"{now.astimezone(DISPLAY_TIMEZONE):%Y-%m-%d %H:%M:%S}（北京时间）",
        "generated_timezone": "Asia/Shanghai",
    }


def _normalize(value: Any) -> str:
    return NORMALIZE_RE.sub("", unicodedata.normalize("NFKC", str(value or "")).casefold().strip())


def _entity_key(values: list[Any]) -> str:
    return "::".join(_normalize(value) for value in values)


def _canonical_identity(record: dict[str, Any]) -> str:
    """Recompute identity from canonical values, not a historical entity_key.

    Entity-key normalization rules can evolve (for example, retaining a
    meaningful `+`). Old Crosswalks retain the original canonical values, so
    they remain the stable way to distinguish a key migration from removal.
    """

    entity = record.get("entity") or {}
    if not isinstance(entity, dict):
        return ""
    values = [value for field, value in entity.items() if field != "entity_key"]
    return _entity_key(values) if values else str(entity.get("entity_key") or "")


def _quote_identifier(value: str) -> str:
    if not IDENTIFIER_RE.fullmatch(value):
        raise ValueError(f"Invalid SQL identifier in build rule: {value}")
    return f'"{value}"'


def _read_file_frame(path: Path, fields: list[str], sheet_name: str | None = None) -> pd.DataFrame:
    suffix = path.suffix.lower()
    if suffix in {".xlsx", ".xls"}:
        return pd.read_excel(path, sheet_name=sheet_name or 0, usecols=lambda name: str(name) in set(fields))
    if suffix == ".tsv":
        return pd.read_csv(path, sep="\t", usecols=lambda name: str(name) in set(fields))
    try:
        return pd.read_csv(path, usecols=lambda name: str(name) in set(fields))
    except UnicodeDecodeError:
        return pd.read_csv(path, encoding="gb18030", usecols=lambda name: str(name) in set(fields))


async def _load_binding_frame(binding: dict[str, Any], *, session_id: str) -> tuple[dict[str, Any], pd.DataFrame]:
    input_spec = binding["input"]
    kind = str(input_spec["kind"])
    fields = list(binding["key_fields"])
    source_metadata = {
        "source_id": str(binding.get("source_id") or binding.get("id") or ""),
        "source_profile_name": str(binding.get("source_name") or binding.get("display_name") or ""),
    }
    if kind == "attachment":
        # The background worker executes this module in a fresh subprocess.
        # Re-initialize the persistent attachment root instead of relying on
        # the FastAPI lifespan that owns the interactive Agent process.
        if attachment_store.root_dir is None:
            from runtime_identity.paths import PuddingClawPaths

            attachment_store.initialize(PuddingClawPaths.from_environment().root)
        attachment_id = str(input_spec.get("attachment_id") or "")
        item = attachment_store.get(session_id, attachment_id)
        if not item or str(item.get("type")) != "spreadsheet":
            raise RuntimeError(f"Temporary spreadsheet attachment is unavailable: {attachment_id}")
        path = Path(str(item.get("path") or ""))
        if not path.is_file():
            raise RuntimeError(f"Temporary spreadsheet attachment file is missing: {attachment_id}")
        return {
            **source_metadata,
            "source_kind": "attachment",
            "source_ref": f"attachment:{attachment_id}",
            "source_name": str(item.get("name") or path.name),
            "table_or_sheet": "",
            "fingerprint": _file_sha256(path),
        }, _read_file_frame(path, fields)

    if kind == "table_asset":
        asset_id = str(input_spec.get("asset_id") or "")
        async with get_sessionmaker()() as session:
            from runtime_identity.paths import PuddingClawPaths

            catalog = TableAssetCatalog(PuddingClawPaths.from_environment().root)
            asset, frame = await catalog.load_dataframe_for_asset(session, asset_id)
        missing = [field for field in fields if field not in frame.columns]
        if missing:
            raise RuntimeError(f"Table asset is missing selected fields: {', '.join(missing)}")
        return {
            **source_metadata,
            "source_kind": "table_asset",
            "source_ref": f"table_asset:{asset_id}",
            "source_name": asset.file_name,
            "table_or_sheet": asset.sheet_name or "",
            "fingerprint": asset.content_sha256,
        }, frame[fields].copy()

    if kind == "database_table":
        source_id = str(input_spec.get("source_id") or "")
        table_name = str(input_spec.get("table") or "")
        if not source_id or not table_name:
            raise RuntimeError("Database binding requires source_id and table")
        quoted_table = ".".join(_quote_identifier(part) for part in table_name.split("."))
        query = "SELECT " + ", ".join(_quote_identifier(field) for field in fields) + f" FROM {quoted_table}"
        async with get_sessionmaker()() as session:
            source = await get_database_source(session, source_id)
        engine = create_async_engine(database_source_url(source), pool_pre_ping=True)
        try:
            async with engine.connect() as connection:
                result = await connection.execute(text(query))
                rows = result.mappings().all()
        finally:
            await engine.dispose()
        return {
            **source_metadata,
            "source_kind": "database_table",
            "source_ref": f"database:{source_id}:{table_name}",
            "source_name": str(getattr(source, "name", source_id)),
            "table_or_sheet": table_name,
            "fingerprint": "",
        }, pd.DataFrame(rows, columns=fields)
    if kind == "active_crosswalk":
        dimension_id = str(input_spec.get("dimension_id") or "").strip()
        if not dimension_id or not IDENTIFIER_RE.fullmatch(dimension_id):
            raise RuntimeError("Active Crosswalk binding requires a valid dimension_id")
        from runtime_identity.paths import PuddingClawPaths

        active_path = PuddingClawPaths.from_environment().user_definitions() / "semantic-assets" / "dimensions" / dimension_id / "references" / "active_crosswalk.json"
        if not active_path.is_file():
            raise RuntimeError(f"Active Crosswalk is unavailable for dimension: {dimension_id}")
        try:
            active = json.loads(active_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"Active Crosswalk is invalid JSON for dimension: {dimension_id}") from exc
        rows = []
        for record in active.get("records") or []:
            entity = record.get("entity") if isinstance(record, dict) else None
            if not isinstance(entity, dict):
                continue
            if all(str(entity.get(field) or "").strip() for field in fields):
                rows.append({field: entity[field] for field in fields})
        if not rows:
            raise RuntimeError(f"Active Crosswalk has no usable canonical rows for dimension: {dimension_id}")
        return {
            **source_metadata,
            "source_kind": "canonical_reference",
            "source_ref": f"active_crosswalk:{dimension_id}",
            "source_name": f"当前规范基准 {dimension_id}",
            "table_or_sheet": "active_crosswalk.json",
            "fingerprint": str(active.get("version") or ""),
        }, pd.DataFrame(rows, columns=fields)
    raise RuntimeError(f"Unsupported input kind: {kind}")


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _distinct_rows(frame: pd.DataFrame, fields: list[str]) -> list[dict[str, str]]:
    missing = [field for field in fields if field not in frame.columns]
    if missing:
        raise RuntimeError(f"Input is missing selected fields: {', '.join(missing)}")
    subset = frame[fields].copy().fillna("")
    for field in fields:
        subset[field] = subset[field].astype(str).str.strip()
    subset = subset[subset.apply(lambda row: all(bool(value) for value in row), axis=1)]
    return [{field: str(row[field]) for field in fields} for _, row in subset.drop_duplicates().iterrows()]


def build_crosswalk(rule: dict[str, Any], resolved: list[tuple[dict[str, Any], pd.DataFrame]]) -> tuple[dict[str, Any], dict[str, Any]]:
    canonical_rule = next(binding for binding in rule["bindings"] if binding["role"] == "canonical")
    metadata_by_binding = {binding["id"]: metadata for binding, (metadata, _frame) in zip(rule["bindings"], resolved)}
    frame_by_binding = {binding["id"]: frame for binding, (_metadata, frame) in zip(rule["bindings"], resolved)}
    canonical_rows = _distinct_rows(frame_by_binding[canonical_rule["id"]], canonical_rule["key_fields"])
    source_rules = [binding for binding in rule["bindings"] if binding["role"] == "source"]

    canonical_by_key: dict[str, list[dict[str, str]]] = defaultdict(list)
    for row in canonical_rows:
        canonical_by_key[_entity_key([row[field] for field in canonical_rule["key_fields"]])].append(row)

    canonical_meta = metadata_by_binding[canonical_rule["id"]]
    source_by_binding: dict[str, dict[str, list[dict[str, str]]]] = {}
    source_row_counts: dict[str, int] = {}
    for source_rule in source_rules:
        source_rows = _distinct_rows(frame_by_binding[source_rule["id"]], source_rule["key_fields"])
        by_key: dict[str, list[dict[str, str]]] = defaultdict(list)
        for row in source_rows:
            by_key[_entity_key([row[field] for field in source_rule["key_fields"]])].append(row)
        source_by_binding[source_rule["id"]] = by_key
        source_row_counts[source_rule["id"]] = len(source_rows)
    records: list[dict[str, Any]] = []
    source_diagnostics: list[dict[str, Any]] = []
    matched_source_keys: dict[str, set[str]] = {binding["id"]: set() for binding in source_rules}

    for match_key, candidates in sorted(canonical_by_key.items()):
        canonical = candidates[0]
        output_values = {
            output: canonical[field]
            for field, output in zip(canonical_rule["key_fields"], canonical_rule["output_fields"])
        }
        matching_bindings: list[dict[str, Any]] = []
        if len(candidates) == 1:
            for source_rule in source_rules:
                matching_sources = source_by_binding[source_rule["id"]].get(match_key, [])
                if matching_sources:
                    matched_source_keys[source_rule["id"]].add(match_key)
                    source_meta = metadata_by_binding[source_rule["id"]]
                    matching_bindings.extend({**source_meta, "key_fields": source} for source in matching_sources)
        bindings = [{**canonical_meta, "key_fields": canonical}]
        bindings.extend(matching_bindings)
        records.append(
            {
                "record_kind": "canonical_entity",
                "entity": {"entity_key": match_key, **output_values},
                "canonical_values": output_values,
                "bindings": bindings,
                "resolution": {
                    "status": "auto_matched" if matching_bindings else "canonical_only",
                    "join_eligible": bool(matching_bindings),
                    "method": "normalized_exact" if matching_bindings else "canonical_baseline",
                    "confidence": 1.0,
                    "candidate_series": [],
                    "evidence": [
                        "用户确认的基准输入定义规范实体；来源键经 NFKC、大小写、空格和标点归一后精确匹配。"
                        if matching_bindings else "用户确认的基准输入定义规范实体；当前来源没有高置信度绑定。"
                    ],
                },
            }
        )

    for source_rule in source_rules:
        source_meta = metadata_by_binding[source_rule["id"]]
        for match_key, rows in sorted(source_by_binding[source_rule["id"]].items()):
            if match_key in matched_source_keys[source_rule["id"]]:
                continue
            resolution = {
                "status": "candidate" if len(canonical_by_key.get(match_key, [])) > 1 else "unmatched",
                "join_eligible": False,
                "method": "normalized_key_collision" if len(canonical_by_key.get(match_key, [])) > 1 else "normalized_exact_not_found",
                "confidence": 0.0,
                "candidate_series": [],
                "evidence": ["来源键未能唯一映射到用户确认的规范实体，保留待审核。"],
            }
            for row in rows:
                source_diagnostics.append(
                    {
                        "record_kind": "source_diagnostic",
                        "entity": None,
                        "bindings": [{**source_meta, "key_fields": row}],
                        "resolution": resolution,
                    }
                )

    payload = {
        "formatter": "entity-resolution-crosswalk",
        "schema_version": "entity-resolution-crosswalk/v1",
        "version": "1.0.0",
        "entity_type": rule["dimension_id"],
        **_time_fields(),
        "build_rule": rule,
        "canonical_key": {
            "fields": canonical_rule["output_fields"],
            "entity_key_template": "normalize(key_fields).join('::')",
        },
        "records": records,
        "source_diagnostics": source_diagnostics,
    }
    summary = {
        "canonical_entities": len(records),
        "canonical_with_source_binding": sum(1 for record in records if record["resolution"]["join_eligible"]),
        "canonical_only": sum(1 for record in records if not record["resolution"]["join_eligible"]),
        "source_distinct_keys": sum(source_row_counts.values()),
        "source_matched": sum(len(keys) for keys in matched_source_keys.values()),
        "source_diagnostics": len(source_diagnostics),
    }
    return payload, summary


def merge_prior_source_bindings(crosswalk: dict[str, Any], prior: dict[str, Any] | None) -> dict[str, Any]:
    """Keep prior source bindings when an incremental build supplies new sources."""

    if not prior or (crosswalk.get("build_rule") or {}).get("merge", {}).get("mode") != "append_source_bindings":
        return crosswalk
    prior_by_identity = {
        _canonical_identity(record): record
        for record in prior.get("records") or []
        if isinstance(record, dict) and _canonical_identity(record)
    }
    for record in crosswalk.get("records") or []:
        prior_record = prior_by_identity.get(_canonical_identity(record))
        if not prior_record:
            continue
        current_bindings = list(record.get("bindings") or [])
        fingerprints = {
            (item.get("source_ref"), json.dumps(item.get("key_fields") or {}, ensure_ascii=False, sort_keys=True))
            for item in current_bindings
            if isinstance(item, dict)
        }
        for binding in prior_record.get("bindings") or []:
            if not isinstance(binding, dict) or binding.get("source_kind") in {"database_table", "canonical_reference"}:
                continue
            fingerprint = (binding.get("source_ref"), json.dumps(binding.get("key_fields") or {}, ensure_ascii=False, sort_keys=True))
            if fingerprint not in fingerprints:
                current_bindings.append(binding)
                fingerprints.add(fingerprint)
        record["bindings"] = current_bindings
        if len(current_bindings) > 1:
            resolution = record.setdefault("resolution", {})
            resolution["status"] = "auto_matched"
            resolution["join_eligible"] = True
            resolution["method"] = "normalized_exact_with_prior_bindings"

    # Source diagnostics are first-class review work. An append must retain
    # unresolved/candidate keys from older source instances as well as their
    # successful bindings; otherwise a monthly append silently erases the
    # analyst's outstanding queue.
    known_source_keys: set[tuple[str, str]] = set()
    for record in [*(crosswalk.get("records") or []), *(crosswalk.get("source_diagnostics") or [])]:
        if not isinstance(record, dict):
            continue
        for binding in record.get("bindings") or []:
            if not isinstance(binding, dict) or binding.get("source_kind") in {"database_table", "canonical_reference"}:
                continue
            known_source_keys.add((str(binding.get("source_ref") or ""), json.dumps(binding.get("key_fields") or {}, ensure_ascii=False, sort_keys=True)))
    for prior_record in prior.get("source_diagnostics") or []:
        if not isinstance(prior_record, dict):
            continue
        retained_bindings = []
        for binding in prior_record.get("bindings") or []:
            if not isinstance(binding, dict):
                continue
            fingerprint = (str(binding.get("source_ref") or ""), json.dumps(binding.get("key_fields") or {}, ensure_ascii=False, sort_keys=True))
            if fingerprint not in known_source_keys:
                retained_bindings.append(binding)
                known_source_keys.add(fingerprint)
        if retained_bindings:
            retained = dict(prior_record)
            retained["bindings"] = retained_bindings
            crosswalk.setdefault("source_diagnostics", []).append(retained)
    return crosswalk


def incremental_canonical_delta(crosswalk: dict[str, Any], prior: dict[str, Any] | None) -> dict[str, Any]:
    """Describe real canonical changes without treating a key migration as removal."""

    if not prior or (crosswalk.get("build_rule") or {}).get("merge", {}).get("mode") != "append_source_bindings":
        return {"added": [], "removed": []}
    prior_by_identity = {
        _canonical_identity(record): record
        for record in prior.get("records") or []
        if isinstance(record, dict) and _canonical_identity(record)
    }
    current_by_identity = {
        _canonical_identity(record): record
        for record in crosswalk.get("records") or []
        if isinstance(record, dict) and _canonical_identity(record)
    }
    def describe(record: dict[str, Any]) -> dict[str, Any]:
        entity = record.get("entity") if isinstance(record.get("entity"), dict) else {}
        return {
            "entity_key": str(entity.get("entity_key") or ""),
            "label": " / ".join(str(value) for field, value in entity.items() if field != "entity_key" and str(value or "")),
            "canonical": entity,
        }
    return {
        "added": [describe(current_by_identity[key]) for key in sorted(current_by_identity.keys() - prior_by_identity.keys())],
        "removed": [describe(prior_by_identity[key]) for key in sorted(prior_by_identity.keys() - current_by_identity.keys())],
    }


def assert_incremental_canonical_baseline(crosswalk: dict[str, Any], prior: dict[str, Any] | None) -> None:
    """Legacy strict helper retained for callers that explicitly require a hard block."""

    delta = incremental_canonical_delta(crosswalk, prior)
    if delta["removed"]:
        preview = ", ".join(str(item["entity_key"]) for item in delta["removed"][:5])
        raise RuntimeError(f"增量追加的规范实体基准发生缩减：缺少 {len(delta['removed'])} 个实体（如 {preview}）。请先确认基准表或改为显式重建。")


def summarize_crosswalk(crosswalk: dict[str, Any]) -> dict[str, int]:
    source_keys: set[tuple[str, str]] = set()
    matched = 0
    for record in crosswalk.get("records") or []:
        if not isinstance(record, dict):
            continue
        bindings = [item for item in record.get("bindings") or [] if isinstance(item, dict) and item.get("source_kind") not in {"database_table", "canonical_reference"}]
        if bindings:
            matched += len(bindings)
        for binding in bindings:
            source_keys.add((str(binding.get("source_ref") or ""), json.dumps(binding.get("key_fields") or {}, ensure_ascii=False, sort_keys=True)))
    diagnostics = crosswalk.get("source_diagnostics") or []
    for record in diagnostics:
        if not isinstance(record, dict):
            continue
        for binding in record.get("bindings") or []:
            if isinstance(binding, dict):
                source_keys.add((str(binding.get("source_ref") or ""), json.dumps(binding.get("key_fields") or {}, ensure_ascii=False, sort_keys=True)))
    return {
        "canonical_entities": len(crosswalk.get("records") or []),
        "canonical_with_source_binding": sum(1 for record in crosswalk.get("records") or [] if isinstance(record, dict) and len(record.get("bindings") or []) > 1),
        "canonical_only": sum(1 for record in crosswalk.get("records") or [] if isinstance(record, dict) and len(record.get("bindings") or []) <= 1),
        "source_distinct_keys": len(source_keys),
        "source_matched": matched,
        "source_diagnostics": len(diagnostics),
    }


async def run(args: argparse.Namespace) -> dict[str, Any]:
    rule = validate_build_rule(json.loads(Path(args.rule_json).read_text(encoding="utf-8")))
    if rule["adapter"] != "entity_crosswalk_v1":
        raise RuntimeError("Build rule adapter does not match entity_crosswalk_v1")
    if rule["dimension_id"] != args.dimension_id:
        raise RuntimeError("Build rule dimension_id does not match the queued job")
    resolved = [await _load_binding_frame(binding, session_id=args.session_id) for binding in rule["bindings"]]
    crosswalk, summary = build_crosswalk(rule, resolved)
    prior_reference_path = str(getattr(args, "prior_reference_path", "") or "")
    prior_path = Path(prior_reference_path).resolve() if prior_reference_path else None
    prior = json.loads(prior_path.read_text(encoding="utf-8")) if prior_path and prior_path.is_file() else None
    baseline_delta = incremental_canonical_delta(crosswalk, prior)
    crosswalk = merge_prior_source_bindings(crosswalk, prior)
    summary = summarize_crosswalk(crosswalk)
    output_dir = Path(args.output_dir).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    staged_reference = Path(args.semantic_reference_path).resolve()
    staged_reference.parent.mkdir(parents=True, exist_ok=True)
    staged_reference.write_text(json.dumps(crosswalk, ensure_ascii=False, indent=2), encoding="utf-8")
    csv_path = output_dir / "canonical-crosswalk.csv"
    diagnostics_csv_path = output_dir / "source-diagnostics.csv"
    with csv_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=["entity_key", "status", "join_eligible", "bindings"])
        writer.writeheader()
        for record in crosswalk["records"]:
            writer.writerow({
                "entity_key": record["entity"]["entity_key"],
                "status": record["resolution"]["status"],
                "join_eligible": record["resolution"]["join_eligible"],
                "bindings": json.dumps(record["bindings"], ensure_ascii=False),
            })
    with diagnostics_csv_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=["status", "method", "bindings"])
        writer.writeheader()
        for record in crosswalk["source_diagnostics"]:
            writer.writerow({
                "status": record["resolution"]["status"],
                "method": record["resolution"]["method"],
                "bindings": json.dumps(record["bindings"], ensure_ascii=False),
            })
    return {
        "summary": summary,
        "crosswalk": str(staged_reference),
        "csv": str(csv_path),
        "diagnostic_csv": str(diagnostics_csv_path),
        "baseline_delta": baseline_delta,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Build a rule-driven entity Crosswalk")
    parser.add_argument("--dimension-id", required=True)
    parser.add_argument("--session-id", required=True)
    parser.add_argument("--rule-json", required=True)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--semantic-reference-path", required=True)
    parser.add_argument("--prior-reference-path", default="")
    args = parser.parse_args()
    print(json.dumps(asyncio.run(run(args)), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
