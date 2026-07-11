"""Regression coverage for the configuration-first vehicle-series Crosswalk."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pandas as pd


def _load_adapter():
    path = Path(__file__).parents[1] / "skills" / "build-semantic-dimension" / "scripts" / "vehicle_series_full.py"
    spec = importlib.util.spec_from_file_location("vehicle_series_full_test_adapter", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_configuration_table_defines_canonical_entity_universe(tmp_path) -> None:
    adapter = _load_adapter()
    config_series = [
        {"brand": "比亚迪", "series": "秦PLUS"},
        {"brand": "丰田", "series": "凯美瑞"},
    ]
    source_frame = pd.DataFrame(
        {
            "品牌": ["比亚迪汽车", "未知品牌"],
            "1-子车型": ["比亚迪秦PLUS", "未知车系"],
            "1-brandcn车型": ["比亚迪秦PLUS", "未知车系"],
        }
    )

    source_records = adapter.build_records(source_frame, config_series)
    crosswalk = adapter.build_portable_crosswalk(
        asset={"asset_id": "tbl_demo", "file_name": "source.xlsx", "sheet_name": "Sheet1"},
        source_id="dbs_demo",
        records=source_records,
        config_series=config_series,
    )

    canonical_records = [record for record in crosswalk["records"] if record["record_kind"] == "canonical_entity"]
    assert len(crosswalk["records"]) == len(config_series)
    assert {(record["entity"]["canonical_brand"], record["entity"]["canonical_series"]) for record in canonical_records} == {
        ("比亚迪", "秦PLUS"),
        ("丰田", "凯美瑞"),
    }

    byd = next(record for record in canonical_records if record["entity"]["canonical_brand"] == "比亚迪")
    assert byd["entity"]["entity_key"] == "比亚迪::秦plus"
    assert byd["resolution"]["join_eligible"] is True
    assert byd["bindings"][0]["key_fields"] == {"brand": "比亚迪", "serial_name": "秦PLUS"}
    assert byd["bindings"][1]["key_fields"] == {"品牌": "比亚迪汽车", "1-子车型": "比亚迪秦PLUS"}

    toyota = next(record for record in canonical_records if record["entity"]["canonical_brand"] == "丰田")
    assert toyota["resolution"]["status"] == "canonical_only"
    assert toyota["resolution"]["join_eligible"] is False
    assert toyota["bindings"][0]["key_fields"] == {"brand": "丰田", "serial_name": "凯美瑞"}

    diagnostics = crosswalk["source_diagnostics"]
    assert len(diagnostics) == 1
    assert diagnostics[0]["entity"] is None
    assert diagnostics[0]["resolution"]["status"] == "unmatched"

    _json_path, canonical_csv, diagnostics_csv, _reference_path, _summary = adapter.write_results(
        output_dir=tmp_path / "artifacts",
        asset={"asset_id": "tbl_demo", "file_name": "source.xlsx", "sheet_name": "Sheet1"},
        source_id="dbs_demo",
        records=source_records,
        config_series=config_series,
        semantic_reference_path=tmp_path / "reference" / "full_crosswalk.json",
    )
    assert len(canonical_csv.read_text(encoding="utf-8-sig").splitlines()) == 1 + len(config_series)
    assert len(diagnostics_csv.read_text(encoding="utf-8-sig").splitlines()) == 1 + len(diagnostics)


def test_unique_series_can_bind_when_source_brand_is_an_alias_or_parent_group() -> None:
    adapter = _load_adapter()
    unique_candidate = {"brand": "规范品牌", "series": "唯一车系", "series_key": adapter.normalize_key("唯一车系")}

    resolved = adapter.resolve_series(
        sales_brand="来源集团",
        sales_series="唯一车系",
        sales_model_samples=[],
        canonical_brand=None,
        config_by_brand={},
        config_by_series={unique_candidate["series_key"]: [unique_candidate]},
    )

    assert resolved["status"] == "auto_matched"
    assert resolved["method"] == "series_global_unique_normalized_exact"
    assert resolved["confidence"] == 0.99
    assert resolved["config_brand"] == "规范品牌"


def test_global_unique_series_match_is_attached_to_its_canonical_entity() -> None:
    adapter = _load_adapter()
    config_series = [{"brand": "规范品牌", "series": "唯一车系"}]
    source_records = adapter.build_records(
        pd.DataFrame({"品牌": ["来源集团"], "1-子车型": ["唯一车系"]}),
        config_series,
    )
    crosswalk = adapter.build_portable_crosswalk(
        asset={"asset_id": "tbl_demo", "file_name": "source.xlsx", "sheet_name": "Sheet1"},
        source_id="dbs_demo",
        records=source_records,
        config_series=config_series,
    )

    canonical = crosswalk["records"][0]
    assert canonical["entity"]["entity_key"] == "规范品牌::唯一车系"
    assert canonical["resolution"]["join_eligible"] is True
    assert canonical["bindings"][1]["key_fields"] == {"品牌": "来源集团", "1-子车型": "唯一车系"}
    assert crosswalk["source_diagnostics"] == []


def test_duplicate_global_series_stays_unmatched_without_brand_resolution() -> None:
    adapter = _load_adapter()
    shared_key = adapter.normalize_key("同名车系")
    candidates = [
        {"brand": "品牌甲", "series": "同名车系", "series_key": shared_key},
        {"brand": "品牌乙", "series": "同名车系", "series_key": shared_key},
    ]

    resolved = adapter.resolve_series(
        sales_brand="来源集团",
        sales_series="同名车系",
        sales_model_samples=[],
        canonical_brand=None,
        config_by_brand={},
        config_by_series={shared_key: candidates},
    )

    assert resolved["status"] == "unmatched"
    assert resolved["method"] == "brand_and_series_unresolved"
