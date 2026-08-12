"""Build a full-brand cross-source vehicle-series resolution.

Reads ALL brands from the insurance spreadsheet and product configuration
database, resolves high-confidence names, and writes remaining records as
candidates or unmatched items for review.

Usage (from ``backend``):

    .venv/bin/python skills/build-semantic-dimension/scripts/vehicle_series_full.py
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
import re
import unicodedata
from collections import defaultdict
from datetime import datetime, timezone
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pandas as pd
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from analytics.table_catalog import TableAssetCatalog
from db import get_sessionmaker
from knowledge.database_sources import database_source_url, get_database_source
from runtime_identity.paths import PuddingClawPaths

BASE_DIR = Path(__file__).resolve().parents[3]
PROJECT_DIR = BASE_DIR.parent
DEFAULT_SALES_FILE_NAME = "2023年1-5月乘用车市场上险量.xlsx"
DEFAULT_SOURCE_ID = "dbs_77982e981bac4a6fa8"
CONFIGURATION_TABLE = "vehicle_model_base"
USER_DEFINITIONS = PuddingClawPaths.from_environment().user_definitions()
SEMANTIC_REFERENCE_PATH = USER_DEFINITIONS / "semantic-assets" / "dimensions" / "vehicle_series" / "references" / "byd_chery_demo.json"
FULL_CROSSWALK_PATH = USER_DEFINITIONS / "semantic-assets" / "dimensions" / "vehicle_series" / "references" / "full_crosswalk.json"
BRAND_SUFFIXES = ("汽车有限公司", "汽车集团", "汽车", "品牌")
PUNCTUATION_RE = re.compile(r"[^0-9a-z\u4e00-\u9fff]+")
DISPLAY_TIMEZONE = ZoneInfo("Asia/Shanghai")
DISPLAY_TIMEZONE_LABEL = "北京时间"


def generated_time_fields() -> dict[str, str]:
    now_utc = datetime.now(timezone.utc)
    display_time = now_utc.astimezone(DISPLAY_TIMEZONE)
    return {
        "generated_at": now_utc.isoformat(),
        "generated_at_display": f"{display_time:%Y-%m-%d %H:%M:%S}（{DISPLAY_TIMEZONE_LABEL}）",
        "generated_timezone": str(DISPLAY_TIMEZONE),
    }


def normalize_key(value: Any) -> str:
    """Return a comparison key while preserving the original display name."""
    text_value = unicodedata.normalize("NFKC", str(value or "")).casefold().strip()
    return PUNCTUATION_RE.sub("", text_value)


def strip_brand_suffix(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value).strip()
    for suffix in BRAND_SUFFIXES:
        if normalized.endswith(suffix) and len(normalized) > len(suffix):
            return normalized[: -len(suffix)].strip()
    return normalized


def strip_known_prefix(value: str, prefixes: list[str]) -> str:
    normalized = unicodedata.normalize("NFKC", value).strip()
    for prefix in sorted((item for item in prefixes if item), key=len, reverse=True):
        if normalized.casefold().startswith(prefix.casefold()):
            return normalized[len(prefix) :].strip(" -_./")
    return normalized


def _best_fuzzy_candidate(source_key: str, candidates: list[dict[str, str]]) -> tuple[dict[str, str] | None, float, float]:
    scored = sorted(
        (
            (SequenceMatcher(None, source_key, candidate["series_key"]).ratio(), candidate)
            for candidate in candidates
            if source_key and candidate["series_key"]
        ),
        key=lambda item: item[0],
        reverse=True,
    )
    if not scored:
        return None, 0.0, 0.0
    best_score, best = scored[0]
    second_score = scored[1][0] if len(scored) > 1 else 0.0
    return best, best_score, second_score


def resolve_brand(sales_brand: str, config_brands: list[str]) -> tuple[str | None, str, float]:
    """Resolve a source brand only when it maps to one canonical config brand."""
    sales_key = normalize_key(sales_brand)
    exact = [brand for brand in config_brands if normalize_key(brand) == sales_key]
    if len(exact) == 1:
        return exact[0], "brand_normalized_exact", 1.0

    stripped_key = normalize_key(strip_brand_suffix(sales_brand))
    suffix_matches = [brand for brand in config_brands if normalize_key(brand) == stripped_key]
    if len(suffix_matches) == 1:
        return suffix_matches[0], "brand_suffix_normalization", 0.99

    return None, "brand_unresolved", 0.0


def resolve_series(
    *,
    sales_brand: str,
    sales_series: str,
    sales_model_samples: list[str],
    canonical_brand: str | None,
    config_by_brand: dict[str, list[dict[str, str]]],
    config_by_series: dict[str, list[dict[str, str]]],
) -> dict[str, Any]:
    """Resolve a source series, preferring same-brand then global-unique exact matches."""
    source_values = [sales_series, *sales_model_samples]
    prefix_values = [sales_brand, strip_brand_suffix(sales_brand)]
    if canonical_brand:
        prefix_values.append(canonical_brand)
    source_keys: list[tuple[str, str]] = []
    for value in source_values:
        raw = str(value or "")
        without_prefix = strip_known_prefix(raw, prefix_values)
        for candidate, variant in ((raw, "normalized"), (without_prefix, "brand_prefix_removed")):
            key = normalize_key(candidate)
            if key and key not in {item[0] for item in source_keys}:
                source_keys.append((key, variant))

    candidates = config_by_brand.get(canonical_brand, []) if canonical_brand else []
    if candidates:
        exact_matches: dict[tuple[str, str], tuple[dict[str, str], str]] = {}
        for source_key, variant in source_keys:
            for candidate in candidates:
                if source_key == candidate["series_key"]:
                    exact_matches[(candidate["brand"], candidate["series"])] = (candidate, variant)

        if len(exact_matches) == 1:
            match, variant = next(iter(exact_matches.values()))
            uses_brand_prefix_rule = variant == "brand_prefix_removed"
            return {
                "status": "auto_matched",
                "method": "series_brand_prefix_normalized_exact" if uses_brand_prefix_rule else "series_normalized_exact",
                "confidence": 1.0,
                "config_brand": match["brand"],
                "config_series": match["series"],
                "candidate_series": [],
                "evidence": [
                    (
                        "来源车系移除已解析品牌前缀后，经 Unicode、大小写、空格和标点归一，与同品牌配置车系精确一致。"
                        if uses_brand_prefix_rule
                        else "来源车系经 Unicode、大小写、空格和标点归一后，与同品牌配置车系精确一致。"
                    )
                ],
            }

    # A source brand can be a parent group, an alias, or simply missing. A
    # globally unique *exact* series key is still deterministic enough to bind.
    # Do not use global fuzzy matching: duplicate series names across brands are
    # common and must stay reviewable.
    global_exact_matches: dict[tuple[str, str], tuple[dict[str, str], str]] = {}
    for source_key, variant in source_keys:
        for candidate in config_by_series.get(source_key, []):
            global_exact_matches[(candidate["brand"], candidate["series"])] = (candidate, variant)
    if len(global_exact_matches) == 1:
        match, variant = next(iter(global_exact_matches.values()))
        uses_prefix_rule = variant == "brand_prefix_removed"
        return {
            "status": "auto_matched",
            "method": "series_global_unique_prefix_normalized_exact" if uses_prefix_rule else "series_global_unique_normalized_exact",
            "confidence": 0.99,
            "config_brand": match["brand"],
            "config_series": match["series"],
            "candidate_series": [],
            "evidence": [
                (
                    "来源品牌未作为匹配前提；车系移除来源品牌前缀后，在全配置库唯一精确命中。"
                    if uses_prefix_rule
                    else "来源品牌未作为匹配前提；车系在全配置库唯一精确命中。"
                )
            ],
        }

    if not canonical_brand:
        return {
            "status": "unmatched",
            "method": "brand_and_series_unresolved",
            "confidence": 0.0,
            "config_brand": None,
            "config_series": None,
            "candidate_series": [],
            "evidence": ["来源品牌未能映射，且车系在全配置库中不存在唯一精确命中。"],
        }

    fuzzy_results: list[tuple[dict[str, str], float, float]] = []
    for source_key, _variant in source_keys:
        best, best_score, second_score = _best_fuzzy_candidate(source_key, candidates)
        if best:
            fuzzy_results.append((best, best_score, second_score))
    fuzzy_results.sort(key=lambda item: item[1], reverse=True)

    if fuzzy_results:
        best, best_score, second_score = fuzzy_results[0]
        if best_score >= 0.82:
            return {
                "status": "candidate",
                "method": "series_fuzzy_candidate",
                "confidence": round(best_score, 4),
                "config_brand": best["brand"],
                "config_series": best["series"],
                "candidate_series": [
                    {
                        "brand": best["brand"],
                        "series": best["series"],
                        "similarity": round(best_score, 4),
                        "margin_to_second": round(best_score - second_score, 4),
                    }
                ],
                "evidence": [
                    "未出现归一化精确匹配；仅提供同品牌字符串相似候选，默认不自动用于统计。"
                ],
            }

    return {
        "status": "unmatched",
        "method": "series_unresolved",
        "confidence": 0.0,
        "config_brand": canonical_brand,
        "config_series": None,
        "candidate_series": [],
        "evidence": ["同品牌配置车系中不存在归一化精确匹配，且没有足够接近的候选。"],
    }


async def load_sales_frame(file_name: str) -> tuple[dict[str, Any], pd.DataFrame]:
    """Load the registered spreadsheet asset, never scan the filesystem blindly."""
    catalog = TableAssetCatalog(BASE_DIR)
    async with get_sessionmaker()() as session:
        assets = await catalog.list_assets(session, include_profile=False, limit=2000)
        asset = next((item for item in assets if item.get("file_name") == file_name), None)
        if asset is None:
            raise RuntimeError(f"未在数据资产目录中找到销量表：{file_name}")
        _asset_model, frame = await catalog.load_dataframe_for_asset(session, str(asset["asset_id"]))
        return asset, frame


async def load_all_config_series(source_id: str) -> list[dict[str, str]]:
    """Read ALL brand/series keys from the config source (no brand filter)."""
    async with get_sessionmaker()() as session:
        source = await get_database_source(session, source_id)
    engine = create_async_engine(database_source_url(source), pool_pre_ping=True)
    statement = text(
        """
        SELECT DISTINCT brand, serial_name
        FROM vehicle_model_base
        WHERE serial_name IS NOT NULL
          AND btrim(serial_name) <> ''
        ORDER BY brand, serial_name
        """
    )
    try:
        async with engine.connect() as connection:
            result = await connection.execute(statement)
            return [{"brand": str(row.brand), "series": str(row.serial_name)} for row in result]
    finally:
        await engine.dispose()


def build_records(sales_frame: pd.DataFrame, config_series: list[dict[str, str]]) -> list[dict[str, Any]]:
    """Build resolution records for ALL brands (no pre-filtering)."""
    required_columns = {"品牌", "1-子车型"}
    missing = required_columns - set(sales_frame.columns)
    if missing:
        raise RuntimeError(f"来源表缺少车系构建关键列：{', '.join(sorted(missing))}")

    available_columns = ["品牌", "1-子车型"]
    if "1-brandcn车型" in sales_frame.columns:
        available_columns.append("1-brandcn车型")
    sales = sales_frame.loc[:, available_columns].copy()
    sales["品牌"] = sales["品牌"].fillna("").astype(str).str.strip()
    sales["1-子车型"] = sales["1-子车型"].fillna("").astype(str).str.strip()
    if "1-brandcn车型" not in sales:
        sales["1-brandcn车型"] = ""
    sales["1-brandcn车型"] = sales["1-brandcn车型"].fillna("").astype(str).str.strip()
    # The build grain is source brand + source series. Sales values are never
    # read, summed, sorted, or persisted by this semantic-dimension adapter.
    sales = sales[sales["1-子车型"].ne("")]

    grouped = (
        sales.groupby(["品牌", "1-子车型"], as_index=False)
        .agg(
            sales_model_samples=("1-brandcn车型", lambda values: sorted({value for value in values if value})[:5]),
        )
        .sort_values(["品牌", "1-子车型"], ascending=[True, True])
    )

    config_brands = sorted({item["brand"] for item in config_series})
    config_by_brand: dict[str, list[dict[str, str]]] = defaultdict(list)
    config_by_series: dict[str, list[dict[str, str]]] = defaultdict(list)
    for item in config_series:
        candidate = {"brand": item["brand"], "series": item["series"], "series_key": normalize_key(item["series"])}
        config_by_brand[item["brand"]].append(candidate)
        config_by_series[candidate["series_key"]].append(candidate)

    records: list[dict[str, Any]] = []
    for row in grouped.itertuples(index=False):
        sales_brand = str(getattr(row, "品牌"))
        sales_series = str(getattr(row, "_1"))
        sales_model_samples = list(getattr(row, "sales_model_samples"))
        canonical_brand, brand_method, brand_confidence = resolve_brand(sales_brand, config_brands)
        series_resolution = resolve_series(
            sales_brand=sales_brand,
            sales_series=sales_series,
            sales_model_samples=sales_model_samples,
            canonical_brand=canonical_brand,
            config_by_brand=config_by_brand,
            config_by_series=config_by_series,
        )
        resolved_brand = series_resolution["config_brand"] or canonical_brand
        resolved_series = series_resolution["config_series"]
        evidence = [f"品牌规则：{brand_method}", *series_resolution["evidence"]]
        records.append(
            {
                "sales_brand": sales_brand,
                "sales_series": sales_series,
                "sales_model_samples": sales_model_samples,
                "canonical_brand": resolved_brand,
                "canonical_series": resolved_series,
                "entity_key": (
                    f"{normalize_key(resolved_brand)}::{normalize_key(resolved_series)}"
                    if resolved_brand and resolved_series
                    else None
                ),
                "status": series_resolution["status"],
                "method": series_resolution["method"],
                "confidence": min(brand_confidence, float(series_resolution["confidence"])),
                "candidate_series": series_resolution["candidate_series"],
                "evidence": evidence,
            }
        )
    return records


def build_portable_crosswalk(
    *,
    asset: dict[str, Any],
    source_id: str,
    records: list[dict[str, Any]],
    config_series: list[dict[str, str]],
) -> dict[str, Any]:
    """Build a configuration-first Crosswalk plus source-side diagnostics.

    The configuration table defines the canonical vehicle-series universe.  A
    source table can only add a binding to that entity; it must never decide
    whether the canonical entity exists.  Candidate and unmatched source keys
    remain as diagnostic records so a later build can review them without
    turning them into joinable entities.
    """
    source_asset_id = str(asset.get("asset_id") or "")
    source_binding = {
        "source_kind": "table_asset",
        "source_ref": f"table_asset:{source_asset_id}",
        "source_name": str(asset.get("file_name") or "来源表"),
        "table_or_sheet": str(asset.get("sheet_name") or ""),
    }
    resolved_by_entity: dict[str, list[dict[str, Any]]] = defaultdict(list)
    diagnostics: list[dict[str, Any]] = []

    for record in records:
        has_resolved_entity = bool(record["entity_key"])
        is_joinable = record["status"] in {"auto_matched", "accepted"} and has_resolved_entity
        if is_joinable:
            resolved_by_entity[record["entity_key"]].append(record)
            continue

        diagnostics.append(
            {
                "record_kind": "source_diagnostic",
                "entity": None,
                "bindings": [
                    {
                        **source_binding,
                        "key_fields": {"品牌": record["sales_brand"], "1-子车型": record["sales_series"]},
                    }
                ],
                "resolution": {
                    "status": record["status"],
                    "join_eligible": False,
                    "method": record["method"],
                    "confidence": record["confidence"],
                    "candidate_series": record["candidate_series"],
                    "evidence": record["evidence"],
                },
                "sales_model_samples": record["sales_model_samples"],
            }
        )

    portable_records: list[dict[str, Any]] = []
    for config in config_series:
        canonical_brand = config["brand"]
        canonical_series = config["series"]
        entity_key = f"{normalize_key(canonical_brand)}::{normalize_key(canonical_series)}"
        matched_source_records = resolved_by_entity.get(entity_key, [])
        bindings = [
            {
                "source_kind": "database_table",
                "source_ref": source_id,
                "source_name": "产品配置 PostgreSQL",
                "table_or_sheet": CONFIGURATION_TABLE,
                "key_fields": {"brand": canonical_brand, "serial_name": canonical_series},
            }
        ]
        for record in matched_source_records:
            bindings.append(
                {
                    **source_binding,
                    "key_fields": {"品牌": record["sales_brand"], "1-子车型": record["sales_series"]},
                }
            )
        portable_records.append(
            {
                "record_kind": "canonical_entity",
                "entity": {
                    "entity_key": entity_key,
                    "canonical_brand": canonical_brand,
                    "canonical_series": canonical_series,
                },
                "bindings": bindings,
                "resolution": {
                    "status": "auto_matched" if matched_source_records else "canonical_only",
                    "join_eligible": bool(matched_source_records),
                    "method": (
                        "configuration_baseline_with_source_binding"
                        if matched_source_records
                        else "configuration_baseline"
                    ),
                    "confidence": 1.0,
                    "candidate_series": [],
                    "evidence": (
                        ["配置库车系为规范实体基准，已附加高置信度来源绑定。"]
                        if matched_source_records
                        else ["配置库车系为规范实体基准；当前来源表尚无可 Join 的高置信度绑定。"]
                    ),
                },
            }
        )

    return {
        "formatter": "entity-resolution-crosswalk",
        "version": "0.4.0",
        "entity_type": "vehicle_series",
        **generated_time_fields(),
        "canonical_key": {
            "fields": ["canonical_brand", "canonical_series"],
            "entity_key_template": "normalize(canonical_brand) + '::' + normalize(canonical_series)",
            "warning": "canonical_series 单独不是跨品牌唯一键；所有连接必须使用 entity_key。",
        },
        "source_contract": {
            "source": {
                "source_ref": f"table_asset:{source_asset_id}",
                "file_name": asset.get("file_name"),
                "virtual_path": asset.get("virtual_path"),
                "sheet_name": asset.get("sheet_name"),
                "key_fields": ["品牌", "1-子车型"],
            },
            "configuration": {
                "source_ref": source_id,
                "table": CONFIGURATION_TABLE,
                "key_fields": ["brand", "serial_name"],
            },
        },
        "scope": {"brands": "all", "grain": "brand + series"},
        # The published Crosswalk contains exactly the canonical configuration
        # universe. Source-side review records are intentionally separate so
        # consumers cannot mistake diagnostics for canonical entities.
        "records": portable_records,
        "source_diagnostics": diagnostics,
    }


def write_results(
    *,
    output_dir: Path,
    asset: dict[str, Any],
    source_id: str,
    records: list[dict[str, Any]],
    config_series: list[dict[str, str]],
    semantic_reference_path: Path,
) -> tuple[Path, Path, Path, Path, dict[str, Any]]:
    output_dir.mkdir(parents=True, exist_ok=True)
    by_status: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for record in records:
        by_status[record["status"]].append(record)

    unique_source_brands = sorted({record["sales_brand"] for record in records})
    unique_canonical_brands = sorted({record["brand"] for record in config_series})
    portable_crosswalk = build_portable_crosswalk(
        asset=asset,
        source_id=source_id,
        records=records,
        config_series=config_series,
    )
    canonical_records = [
        record for record in portable_crosswalk["records"] if record.get("record_kind") == "canonical_entity"
    ]
    canonical_with_source_count = sum(
        1 for record in canonical_records if record.get("resolution", {}).get("join_eligible")
    )

    payload = {
        "kind": "vehicle_series_resolution_full",
        **generated_time_fields(),
        "scope": {"brands": "all", "grain": "brand + series"},
        "inputs": {
            "source_asset": {
                "asset_id": asset.get("asset_id"),
                "file_name": asset.get("file_name"),
                "virtual_path": asset.get("virtual_path"),
                "sheet_name": asset.get("sheet_name"),
                "rows": asset.get("rows"),
            },
            "configuration_source_id": source_id,
            "configuration_table": CONFIGURATION_TABLE,
        },
        "rules": {
            "canonical": "配置库 brand + serial_name 定义规范实体全集；来源表不会删除或新增规范实体。",
            "brand": "来源侧品牌先做 NFKC、大小写、标点及受约束尾缀归一，再映射至配置库规范品牌。",
            "series": "只在已解析为同一品牌的候选中做 NFKC、大小写、空格、标点和品牌前缀归一化精确匹配。",
            "fuzzy": "相似度候选不自动计入联合分析；必须确认后才可写入正式实体解析资产。",
        },
        "summary": {
            "canonical_brands_count": len(unique_canonical_brands),
            "canonical_series_count": len(config_series),
            "canonical_with_source_binding_count": canonical_with_source_count,
            "canonical_without_source_binding_count": len(canonical_records) - canonical_with_source_count,
            "source_brands_count": len(unique_source_brands),
            "source_series_count": len(records),
            "source_resolution_by_status": {
                status: {
                    "count": len(items),
                }
                for status, items in sorted(by_status.items())
            },
        },
        "records": records,
    }
    json_path = output_dir / "full-vehicle-series-resolution.json"
    csv_path = output_dir / "full-vehicle-series-resolution.csv"
    diagnostics_csv_path = output_dir / "source-vehicle-series-diagnostics.csv"
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    semantic_reference_path.parent.mkdir(parents=True, exist_ok=True)
    semantic_reference_path.write_text(json.dumps(portable_crosswalk, ensure_ascii=False, indent=2), encoding="utf-8")

    with csv_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=[
                "record_kind",
                "entity_key",
                "canonical_brand",
                "canonical_series",
                "join_eligible",
                "source_binding_count",
                "source_keys",
                "config_source_ref",
                "config_key_fields",
                "status",
                "method",
                "confidence",
                "candidate_series",
                "evidence",
            ],
        )
        writer.writeheader()
        for record in portable_crosswalk["records"]:
            entity = record.get("entity") or {}
            resolution = record.get("resolution") or {}
            source_bindings = [binding for binding in record.get("bindings", []) if binding.get("source_kind") == "table_asset"]
            config_binding = next(
                (binding for binding in record.get("bindings", []) if binding.get("source_kind") == "database_table"),
                {},
            )
            writer.writerow(
                {
                    "record_kind": record.get("record_kind", "source_diagnostic"),
                    "entity_key": entity.get("entity_key", ""),
                    "canonical_brand": entity.get("canonical_brand", ""),
                    "canonical_series": entity.get("canonical_series", ""),
                    "join_eligible": resolution.get("join_eligible", False),
                    "source_binding_count": len(source_bindings),
                    "source_keys": json.dumps([binding.get("key_fields", {}) for binding in source_bindings], ensure_ascii=False),
                    "config_source_ref": config_binding.get("source_ref", ""),
                    "config_key_fields": json.dumps(config_binding.get("key_fields", {}), ensure_ascii=False),
                    "status": resolution.get("status", "unknown"),
                    "method": resolution.get("method", ""),
                    "confidence": resolution.get("confidence", 0),
                    "candidate_series": json.dumps(resolution.get("candidate_series", []), ensure_ascii=False),
                    "evidence": " | ".join(resolution.get("evidence", [])),
                }
            )
    with diagnostics_csv_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=["source_brand", "source_series", "status", "method", "confidence", "candidate_series", "evidence"],
        )
        writer.writeheader()
        for record in portable_crosswalk.get("source_diagnostics", []):
            binding = next(
                (item for item in record.get("bindings", []) if item.get("source_kind") == "table_asset"),
                {},
            )
            resolution = record.get("resolution") or {}
            source_key = binding.get("key_fields") or {}
            writer.writerow(
                {
                    "source_brand": source_key.get("品牌", ""),
                    "source_series": source_key.get("1-子车型", ""),
                    "status": resolution.get("status", "unknown"),
                    "method": resolution.get("method", ""),
                    "confidence": resolution.get("confidence", 0),
                    "candidate_series": json.dumps(resolution.get("candidate_series", []), ensure_ascii=False),
                    "evidence": " | ".join(resolution.get("evidence", [])),
                }
            )
    return json_path, csv_path, diagnostics_csv_path, semantic_reference_path, payload["summary"]


def load_prior_crosswalk(path: Path) -> dict[str, Any] | None:
    """Load the prior crosswalk if it exists for delta comparison."""
    if not path.is_file():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def compute_delta(prior: dict[str, Any] | None, current: dict[str, Any]) -> dict[str, Any]:
    """Compare the new crosswalk against the prior one."""
    if prior is None:
        return {"prior_exists": False, "note": "无旧版 crosswalk，本次为首构建。"}

    prior_records = prior.get("records", [])
    current_records = current.get("records", [])

    def record_key(record: dict[str, Any]) -> tuple[str, str, str]:
        entity_key = (record.get("entity") or {}).get("entity_key")
        if entity_key:
            return ("entity", str(entity_key), "")
        binding = next(
            (item for item in record.get("bindings", []) if item.get("source_kind") == "table_asset"),
            {},
        )
        source_key = binding.get("key_fields", {})
        return ("source", str(source_key.get("品牌", "")), str(source_key.get("1-子车型", "")))

    prior_keys = {record_key(record) for record in prior_records}
    current_keys = {record_key(record) for record in current_records}

    prior_by_status: dict[str, int] = defaultdict(int)
    for rec in prior_records:
        prior_by_status[rec.get("resolution", {}).get("status", "unknown")] += 1

    current_by_status: dict[str, int] = defaultdict(int)
    for rec in current_records:
        current_by_status[rec.get("resolution", {}).get("status", "unknown")] += 1

    new_keys = current_keys - prior_keys
    removed_keys = prior_keys - current_keys
    common_keys = prior_keys & current_keys

    # Check status changes in common keys
    status_changes = 0
    prior_common: dict[tuple[str, str, str], str] = {}
    for rec in prior_records:
        prior_common[record_key(rec)] = rec.get("resolution", {}).get("status", "unknown")

    for rec in current_records:
        k = record_key(rec)
        if k in prior_common and prior_common[k] != rec.get("resolution", {}).get("status", "unknown"):
            status_changes += 1

    return {
        "prior_exists": True,
        "prior_total_records": len(prior_records),
        "current_total_records": len(current_records),
        "new_records": len(new_keys),
        "removed_records": len(removed_keys),
        "common_records": len(common_keys),
        "status_changes_in_common": status_changes,
        "prior_by_status": dict(prior_by_status),
        "current_by_status": dict(current_by_status),
    }


async def run(args: argparse.Namespace) -> tuple[Path, Path, Path, Path, dict[str, Any], dict[str, Any]]:
    asset, sales_frame = await load_sales_frame(args.sales_file_name)
    config_series = await load_all_config_series(args.source_id)
    records = build_records(sales_frame, config_series)
    semantic_reference_path = Path(args.semantic_reference_path).resolve()
    prior_reference_path = Path(args.prior_reference_path).resolve()
    prior = load_prior_crosswalk(prior_reference_path)
    json_path, csv_path, diagnostics_csv_path, semantic_reference_path, summary = write_results(
        output_dir=Path(args.output_dir).resolve(),
        asset=asset,
        source_id=args.source_id,
        records=records,
        config_series=config_series,
        semantic_reference_path=semantic_reference_path,
    )

    # Compare staged output to the currently active Crosswalk. The staged file
    # is intentionally never published by this script.
    portable_current = json.loads(semantic_reference_path.read_text(encoding="utf-8"))
    delta = compute_delta(prior, portable_current)

    return json_path, csv_path, diagnostics_csv_path, semantic_reference_path, summary, delta


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate a full-brand cross-source series resolution.")
    parser.add_argument("--sales-file-name", default=DEFAULT_SALES_FILE_NAME)
    parser.add_argument("--source-id", default=DEFAULT_SOURCE_ID)
    parser.add_argument(
        "--output-dir",
        default=str(PROJECT_DIR / "docs" / "demos" / "vehicle-series-resolution-full"),
    )
    parser.add_argument(
        "--semantic-reference-path",
        default=str(FULL_CROSSWALK_PATH),
        help="Target Crosswalk JSON path. Use a job staging path for background builds.",
    )
    parser.add_argument(
        "--prior-reference-path",
        default=str(SEMANTIC_REFERENCE_PATH),
        help="Current active Crosswalk used only to calculate a change summary.",
    )
    args = parser.parse_args()
    json_path, csv_path, diagnostics_csv_path, semantic_reference_path, summary, delta = asyncio.run(run(args))
    print(
        json.dumps(
            {
                "json": str(json_path),
                "csv": str(csv_path),
                "diagnostic_csv": str(diagnostics_csv_path),
                "semantic_reference": str(semantic_reference_path),
                "summary": summary,
                "delta": delta,
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
