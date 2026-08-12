"""Build a small, auditable cross-source vehicle-series resolution demo.

The demo intentionally does not create a production dimension table.  It reads
the real insurance spreadsheet and product configuration database, resolves
only high-confidence names for selected brands, and writes the remaining
records as candidates or unmatched items for review.

Usage (from ``backend``):

    .venv/bin/python skills/build-semantic-dimension/scripts/vehicle_series_demo.py
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
SEMANTIC_REFERENCE_PATH = (
    PuddingClawPaths.from_environment().user_definitions()
    / "semantic-assets" / "dimensions" / "vehicle_series" / "references" / "byd_chery_demo.json"
)
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
) -> dict[str, Any]:
    """Resolve one sales series against config series under an already resolved brand."""

    if not canonical_brand:
        return {
            "status": "unmatched",
            "method": "brand_unresolved",
            "confidence": 0.0,
            "config_brand": None,
            "config_series": None,
            "candidate_series": [],
            "evidence": ["品牌未能映射到配置库规范品牌，未进行跨品牌车系匹配。"],
        }

    candidates = config_by_brand.get(canonical_brand, [])
    source_values = [sales_series, *sales_model_samples]
    prefix_values = [sales_brand, canonical_brand, strip_brand_suffix(sales_brand)]
    source_keys: list[tuple[str, str]] = []
    for value in source_values:
        raw = str(value or "")
        without_prefix = strip_known_prefix(raw, prefix_values)
        for candidate, variant in ((raw, "normalized"), (without_prefix, "brand_prefix_removed")):
            key = normalize_key(candidate)
            if key and key not in {item[0] for item in source_keys}:
                source_keys.append((key, variant))

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
                    "销量车系移除已解析品牌前缀后，经 Unicode、大小写、空格和标点归一，与同品牌配置车系精确一致。"
                    if uses_brand_prefix_rule
                    else "销量车系经 Unicode、大小写、空格和标点归一后，与同品牌配置车系精确一致。"
                )
            ],
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


async def load_config_series(source_id: str, brand_terms: list[str]) -> list[dict[str, str]]:
    """Read only the brand/series keys needed for this demo from the config source."""

    async with get_sessionmaker()() as session:
        source = await get_database_source(session, source_id)
    engine = create_async_engine(database_source_url(source), pool_pre_ping=True)
    where_terms = " OR ".join(f"brand ILIKE :brand_{index}" for index in range(len(brand_terms)))
    statement = text(
        f"""
        SELECT DISTINCT brand, serial_name
        FROM vehicle_model_base
        WHERE serial_name IS NOT NULL
          AND btrim(serial_name) <> ''
          AND ({where_terms})
        ORDER BY brand, serial_name
        """
    )
    params = {f"brand_{index}": f"%{term}%" for index, term in enumerate(brand_terms)}
    try:
        async with engine.connect() as connection:
            result = await connection.execute(statement, params)
            return [{"brand": str(row.brand), "series": str(row.serial_name)} for row in result]
    finally:
        await engine.dispose()


def build_records(sales_frame: pd.DataFrame, config_series: list[dict[str, str]], brand_terms: list[str]) -> list[dict[str, Any]]:
    required_columns = {"品牌", "1-子车型", "1-brandcn车型", "销量"}
    missing = required_columns - set(sales_frame.columns)
    if missing:
        raise RuntimeError(f"销量表缺少关键列：{', '.join(sorted(missing))}")

    sales = sales_frame.loc[:, ["品牌", "1-子车型", "1-brandcn车型", "销量"]].copy()
    sales["品牌"] = sales["品牌"].fillna("").astype(str).str.strip()
    sales["1-子车型"] = sales["1-子车型"].fillna("").astype(str).str.strip()
    sales["1-brandcn车型"] = sales["1-brandcn车型"].fillna("").astype(str).str.strip()
    sales["销量"] = pd.to_numeric(sales["销量"], errors="coerce").fillna(0)
    selected = sales["品牌"].apply(lambda value: any(term in value for term in brand_terms))
    sales = sales[selected & sales["1-子车型"].ne("")]

    grouped = (
        sales.groupby(["品牌", "1-子车型"], as_index=False)
        .agg(
            sales_volume=("销量", "sum"),
            sales_model_samples=("1-brandcn车型", lambda values: sorted({value for value in values if value})[:5]),
        )
        .sort_values(["品牌", "sales_volume", "1-子车型"], ascending=[True, False, True])
    )

    config_brands = sorted({item["brand"] for item in config_series})
    config_by_brand: dict[str, list[dict[str, str]]] = defaultdict(list)
    for item in config_series:
        config_by_brand[item["brand"]].append(
            {"brand": item["brand"], "series": item["series"], "series_key": normalize_key(item["series"])}
        )

    records: list[dict[str, Any]] = []
    for row in grouped.itertuples(index=False):
        sales_brand = str(getattr(row, "品牌"))
        sales_series = str(getattr(row, "_1"))
        sales_volume = int(getattr(row, "sales_volume"))
        sales_model_samples = list(getattr(row, "sales_model_samples"))
        canonical_brand, brand_method, brand_confidence = resolve_brand(sales_brand, config_brands)
        series_resolution = resolve_series(
            sales_brand=sales_brand,
            sales_series=sales_series,
            sales_model_samples=sales_model_samples,
            canonical_brand=canonical_brand,
            config_by_brand=config_by_brand,
        )
        evidence = [f"品牌规则：{brand_method}", *series_resolution["evidence"]]
        records.append(
            {
                "sales_brand": sales_brand,
                "sales_series": sales_series,
                "sales_model_samples": sales_model_samples,
                "sales_volume": sales_volume,
                "canonical_brand": canonical_brand,
                "canonical_series": series_resolution["config_series"],
                "entity_key": (
                    f"{normalize_key(canonical_brand)}::{normalize_key(series_resolution['config_series'])}"
                    if canonical_brand and series_resolution["config_series"]
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
    brand_terms: list[str],
    records: list[dict[str, Any]],
) -> dict[str, Any]:
    """Return a source-to-source crosswalk that an agent can use directly."""

    source_asset_id = str(asset.get("asset_id") or "")
    portable_records: list[dict[str, Any]] = []
    for record in records:
        config_series = record["canonical_series"]
        has_resolved_entity = bool(record["entity_key"])
        join_eligible = record["status"] in {"auto_matched", "accepted"} and has_resolved_entity
        bindings = [
            {
                "source_kind": "table_asset",
                "source_ref": f"table_asset:{source_asset_id}",
                "source_name": str(asset.get("file_name") or "销量上险量表"),
                "table_or_sheet": str(asset.get("sheet_name") or ""),
                "key_fields": {"品牌": record["sales_brand"], "1-子车型": record["sales_series"]},
            }
        ]
        if record["canonical_brand"] and config_series:
            bindings.append(
                {
                    "source_kind": "database_table",
                    "source_ref": source_id,
                    "source_name": "产品配置 PostgreSQL",
                    "table_or_sheet": CONFIGURATION_TABLE,
                    "key_fields": {"brand": record["canonical_brand"], "serial_name": config_series},
                }
            )
        portable_records.append(
            {
                "entity": (
                    {
                        "entity_key": record["entity_key"],
                        "canonical_brand": record["canonical_brand"],
                        "canonical_series": config_series,
                    }
                    if has_resolved_entity
                    else None
                ),
                "bindings": bindings,
                "resolution": {
                    "status": record["status"],
                    "join_eligible": join_eligible,
                    "method": record["method"],
                    "confidence": record["confidence"],
                    "candidate_series": record["candidate_series"],
                    "evidence": record["evidence"],
                },
                "sales_volume": record["sales_volume"],
                "sales_model_samples": record["sales_model_samples"],
            }
        )

    return {
        "formatter": "entity-resolution-crosswalk",
        "version": "0.1.0",
        "entity_type": "vehicle_series",
        **generated_time_fields(),
        "canonical_key": {
            "fields": ["canonical_brand", "canonical_series"],
            "entity_key_template": "normalize(canonical_brand) + '::' + normalize(canonical_series)",
            "warning": "canonical_series 单独不是跨品牌唯一键；所有连接必须使用 entity_key。",
        },
        "source_contract": {
            "sales": {
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
        "scope": {"brands_contains": brand_terms, "grain": "brand + series"},
        "records": portable_records,
    }


def write_results(
    *,
    output_dir: Path,
    asset: dict[str, Any],
    source_id: str,
    brand_terms: list[str],
    records: list[dict[str, Any]],
    config_series: list[dict[str, str]],
) -> tuple[Path, Path, Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    by_status: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for record in records:
        by_status[record["status"]].append(record)

    payload = {
        "kind": "vehicle_series_resolution_demo",
        **generated_time_fields(),
        "scope": {"brands_contains": brand_terms, "grain": "brand + series"},
        "inputs": {
            "sales_asset": {
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
            "brand": "配置库 brand 仅作为本 Demo 的输出规范名；销量侧品牌先做 NFKC、大小写、标点及受约束尾缀归一。",
            "series": "只在已解析为同一品牌的候选中做 NFKC、大小写、空格、标点和品牌前缀归一化精确匹配。",
            "fuzzy": "相似度候选不自动计入联合分析；必须确认后才可写入正式实体解析资产。",
        },
        "summary": {
            "sales_series_count": len(records),
            "sales_volume": sum(record["sales_volume"] for record in records),
            "config_series_count": len(config_series),
            "by_status": {
                status: {
                    "count": len(items),
                    "sales_volume": sum(item["sales_volume"] for item in items),
                }
                for status, items in sorted(by_status.items())
            },
        },
        "records": records,
    }
    portable_crosswalk = build_portable_crosswalk(
        asset=asset,
        source_id=source_id,
        brand_terms=brand_terms,
        records=records,
    )

    json_path = output_dir / "byd-chery-vehicle-series-resolution.json"
    csv_path = output_dir / "byd-chery-vehicle-series-resolution.csv"
    semantic_reference_path = SEMANTIC_REFERENCE_PATH
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    semantic_reference_path.parent.mkdir(parents=True, exist_ok=True)
    semantic_reference_path.write_text(json.dumps(portable_crosswalk, ensure_ascii=False, indent=2), encoding="utf-8")
    with csv_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=[
                "sales_brand",
                "sales_series",
                "sales_model_samples",
                "sales_volume",
                "canonical_brand",
                "canonical_series",
                "entity_key",
                "join_eligible",
                "sales_source_ref",
                "sales_key_fields",
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
        for record in records:
            join_eligible = record["status"] in {"auto_matched", "accepted"} and bool(record["entity_key"])
            writer.writerow(
                {
                    **record,
                    "join_eligible": join_eligible,
                    "sales_source_ref": f"table_asset:{asset.get('asset_id')}",
                    "sales_key_fields": json.dumps(
                        {"品牌": record["sales_brand"], "1-子车型": record["sales_series"]}, ensure_ascii=False
                    ),
                    "config_source_ref": source_id,
                    "config_key_fields": json.dumps(
                        {"brand": record["canonical_brand"], "serial_name": record["canonical_series"]}, ensure_ascii=False
                    ),
                    "sales_model_samples": " | ".join(record["sales_model_samples"]),
                    "candidate_series": json.dumps(record["candidate_series"], ensure_ascii=False),
                    "evidence": " | ".join(record["evidence"]),
                }
            )
    return json_path, csv_path, semantic_reference_path


async def run(args: argparse.Namespace) -> tuple[Path, Path, Path, dict[str, Any]]:
    brand_terms = [item.strip() for item in args.brands.split(",") if item.strip()]
    asset, sales_frame = await load_sales_frame(args.sales_file_name)
    config_series = await load_config_series(args.source_id, brand_terms)
    records = build_records(sales_frame, config_series, brand_terms)
    json_path, csv_path, semantic_reference_path = write_results(
        output_dir=Path(args.output_dir).resolve(),
        asset=asset,
        source_id=args.source_id,
        brand_terms=brand_terms,
        records=records,
        config_series=config_series,
    )
    payload = json.loads(json_path.read_text(encoding="utf-8"))
    return json_path, csv_path, semantic_reference_path, payload["summary"]


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate an auditable BYD/Chery cross-source series resolution demo.")
    parser.add_argument("--sales-file-name", default=DEFAULT_SALES_FILE_NAME)
    parser.add_argument("--source-id", default=DEFAULT_SOURCE_ID)
    parser.add_argument("--brands", default="比亚迪,奇瑞")
    parser.add_argument(
        "--output-dir",
        default=str(PROJECT_DIR / "docs" / "demos" / "vehicle-series-resolution"),
    )
    args = parser.parse_args()
    json_path, csv_path, semantic_reference_path, summary = asyncio.run(run(args))
    print(
        json.dumps(
            {
                "json": str(json_path),
                "csv": str(csv_path),
                "semantic_reference": str(semantic_reference_path),
                "summary": summary,
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
