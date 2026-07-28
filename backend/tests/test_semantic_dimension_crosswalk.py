from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from knowledge.semantic_dimension_crosswalk import (
    ACTIVE_FILE,
    GENERATED_FILE,
    OVERRIDES_FILE,
    get_matching_overview,
    get_matching_view,
    publish_draft_overrides,
    publish_generated_crosswalk,
    save_entity_override,
    save_override,
    save_source_registry_entry,
)
from knowledge.semantic_dimension_publisher import _validate_registered_source_modes


def _crosswalk() -> dict:
    return {
        "formatter": "entity-resolution-crosswalk",
        "schema_version": "entity-resolution-crosswalk/v1",
        "entity_type": "vehicle_series",
        "records": [
            {
                "entity": {"entity_key": "byd::qinplus", "canonical_brand": "比亚迪", "canonical_serial_name": "秦PLUS"},
                "bindings": [
                    {"source_kind": "database_table", "source_ref": "database:db:vehicle_model_base", "source_name": "insight", "key_fields": {"brand": "比亚迪", "serial_name": "秦PLUS"}},
                    {"source_kind": "attachment", "source_ref": "attachment:sales-jan", "source_name": "1月上险量", "key_fields": {"品牌": "比亚迪", "1-子车型": "秦PLUS"}},
                ],
                "resolution": {"status": "auto_matched", "join_eligible": True},
            },
            {
                "entity": {"entity_key": "byd::han", "canonical_brand": "比亚迪", "canonical_serial_name": "汉"},
                "bindings": [
                    {"source_kind": "database_table", "source_ref": "database:db:vehicle_model_base", "source_name": "insight", "key_fields": {"brand": "比亚迪", "serial_name": "汉"}},
                ],
                "resolution": {"status": "canonical_only", "join_eligible": False},
            },
        ],
        "source_diagnostics": [
            {
                "bindings": [{"source_kind": "attachment", "source_ref": "attachment:sales-jan", "source_name": "1月上险量", "key_fields": {"品牌": "比亚迪", "1-子车型": "比亚迪汉"}}],
                "resolution": {"status": "unmatched", "join_eligible": False},
            }
        ],
    }


def _dimension_root(tmp_path: Path) -> Path:
    root = tmp_path / "backend"
    dimension = root / "semantic-assets" / "dimensions" / "vehicle_series"
    dimension.mkdir(parents=True)
    (dimension / "dimension.md").write_text("---\nid: vehicle_series\n---\n", encoding="utf-8")
    return root


def test_publish_creates_generated_active_registry_and_version(tmp_path: Path) -> None:
    root = _dimension_root(tmp_path)
    result = publish_generated_crosswalk(root, "vehicle_series", _crosswalk())
    references = root / "semantic-assets" / "dimensions" / "vehicle_series" / "references"

    assert result["version"] == "v0.1.0"
    assert (references / GENERATED_FILE).is_file()
    assert (references / ACTIVE_FILE).is_file()
    assert (references / "source_registry.json").is_file()
    assert (references / "versions" / "v0.1.0.json").is_file()
    registry = json.loads((references / "source_registry.json").read_text(encoding="utf-8"))
    assert registry["sources"][0]["id"] == "attachment:sales-jan"


def test_manual_override_is_draft_until_published_and_survives_republish(tmp_path: Path) -> None:
    root = _dimension_root(tmp_path)
    publish_generated_crosswalk(root, "vehicle_series", _crosswalk())
    save_override(root, "vehicle_series", {
        "source_ref": "attachment:sales-jan",
        "source_key": {"品牌": "比亚迪", "1-子车型": "秦PLUS"},
        "action": "bind",
        "target_entity_key": "byd::han",
        "reason": "人工确认实际归属。",
    })
    references = root / "semantic-assets" / "dimensions" / "vehicle_series" / "references"
    generated = json.loads((references / GENERATED_FILE).read_text(encoding="utf-8"))
    active = json.loads((references / ACTIVE_FILE).read_text(encoding="utf-8"))
    assert len(generated["records"][0]["bindings"]) == 2
    assert len(active["records"][0]["bindings"]) == 2
    assert len(active["records"][1]["bindings"]) == 1
    overrides = json.loads((references / OVERRIDES_FILE).read_text(encoding="utf-8"))
    assert len(overrides["overrides"]) == 1
    assert overrides["published_overrides"] == []

    preview = get_matching_view(root, "vehicle_series", source_ref="attachment:sales-jan")
    assert preview["summary"]["has_unpublished_changes"] is True
    assert {row["status"] for row in preview["rows"]} == {"manual_override", "unmatched"}

    published = publish_draft_overrides(root, "vehicle_series")
    assert published["version"] == "v0.1.1"
    active = json.loads((references / ACTIVE_FILE).read_text(encoding="utf-8"))
    assert len(active["records"][0]["bindings"]) == 1
    assert len(active["records"][1]["bindings"]) == 2
    assert active["records"][1]["resolution"]["status"] == "manual_override"

    publish_generated_crosswalk(root, "vehicle_series", _crosswalk())
    republished = json.loads((references / ACTIVE_FILE).read_text(encoding="utf-8"))
    assert len(republished["records"][1]["bindings"]) == 2


def test_source_id_scoped_override_reuses_a_confirmed_key_for_a_new_month(tmp_path: Path) -> None:
    root = _dimension_root(tmp_path)
    january = copy.deepcopy(_crosswalk())
    for record in [*january["records"], *january["source_diagnostics"]]:
        for binding in record["bindings"]:
            if binding.get("source_kind") == "attachment":
                binding["source_id"] = "insurance_sales"
    publish_generated_crosswalk(root, "vehicle_series", january)
    save_override(root, "vehicle_series", {
        "source_ref": "attachment:sales-jan",
        "source_id": "insurance_sales",
        "scope": "source_id",
        "source_key": {"品牌": "比亚迪", "1-子车型": "比亚迪汉"},
        "action": "bind",
        "target_entity_key": "byd::han",
    })
    publish_draft_overrides(root, "vehicle_series")

    february = copy.deepcopy(_crosswalk())
    binding = february["source_diagnostics"][0]["bindings"][0]
    binding.update({"source_ref": "attachment:sales-feb", "source_name": "2月上险量", "source_id": "insurance_sales"})
    publish_generated_crosswalk(root, "vehicle_series", february)

    active = json.loads((root / "semantic-assets" / "dimensions" / "vehicle_series" / "references" / ACTIVE_FILE).read_text(encoding="utf-8"))
    han = next(record for record in active["records"] if record["entity"]["entity_key"] == "byd::han")
    assert han["resolution"]["status"] == "manual_override"
    assert any(binding.get("source_ref") == "attachment:sales-feb" for binding in han["bindings"])
    assert not active["source_diagnostics"]


def test_canonical_lifecycle_is_draft_until_published_and_remove_is_durable(tmp_path: Path) -> None:
    root = _dimension_root(tmp_path)
    publish_generated_crosswalk(root, "vehicle_series", _crosswalk())
    references = root / "semantic-assets" / "dimensions" / "vehicle_series" / "references"

    save_entity_override(root, "vehicle_series", {"entity_key": "byd::han", "action": "inactive", "reason": "停止分析"})
    active_before = json.loads((references / ACTIVE_FILE).read_text(encoding="utf-8"))
    assert next(record for record in active_before["records"] if record["entity"]["entity_key"] == "byd::han")["resolution"]["status"] == "canonical_only"

    publish_draft_overrides(root, "vehicle_series")
    inactive_active = json.loads((references / ACTIVE_FILE).read_text(encoding="utf-8"))
    assert next(record for record in inactive_active["records"] if record["entity"]["entity_key"] == "byd::han")["resolution"]["status"] == "inactive"

    save_entity_override(root, "vehicle_series", {"entity_key": "byd::han", "action": "remove", "reason": "确认移除"})
    publish_draft_overrides(root, "vehicle_series")
    removed_active = json.loads((references / ACTIVE_FILE).read_text(encoding="utf-8"))
    assert {record["entity"]["entity_key"] for record in removed_active["records"]} == {"byd::qinplus"}

    publish_generated_crosswalk(root, "vehicle_series", _crosswalk())
    republished = json.loads((references / ACTIVE_FILE).read_text(encoding="utf-8"))
    assert {record["entity"]["entity_key"] for record in republished["records"]} == {"byd::qinplus"}


def test_matching_view_and_registry_allow_new_order_source(tmp_path: Path) -> None:
    root = _dimension_root(tmp_path)
    publish_generated_crosswalk(root, "vehicle_series", _crosswalk())
    save_source_registry_entry(root, "vehicle_series", {
        "id": "orders",
        "name": "终端订单",
        "kind": "database_table",
        "identity_fields": ["品牌名称", "车系名称"],
        "mapping": [{"canonical_field": "canonical_brand", "source_field": "品牌名称"}],
    })
    view = get_matching_view(root, "vehicle_series", source_ref="attachment:sales-jan")
    assert view["summary"]["sources"] == 2
    assert view["count"] == 2
    assert {row["status"] for row in view["rows"]} == {"auto_matched", "unmatched"}


def test_matching_overview_is_keyed_by_canonical_entity(tmp_path: Path) -> None:
    root = _dimension_root(tmp_path)
    publish_generated_crosswalk(root, "vehicle_series", _crosswalk())

    overview = get_matching_overview(root, "vehicle_series")

    assert overview["count"] == 2
    assert overview["rows"][0]["canonical_label"] == "比亚迪 / 秦PLUS"
    assert overview["rows"][0]["source_cells"]["attachment:sales-jan"][0]["source_key"] == {
        "品牌": "比亚迪",
        "1-子车型": "秦PLUS",
    }


def test_matching_search_filters_before_pagination(tmp_path: Path) -> None:
    root = _dimension_root(tmp_path)
    publish_generated_crosswalk(root, "vehicle_series", _crosswalk())

    overview = get_matching_overview(root, "vehicle_series", query="比亚迪汉", limit=1)
    source = get_matching_view(root, "vehicle_series", source_ref="attachment:sales-jan", query="比亚迪汉", limit=1)

    assert overview["count"] == 1
    assert overview["rows"][0]["entity_key"] == "byd::han"
    assert source["count"] == 1
    assert source["rows"][0]["status"] == "unmatched"


def test_rebinding_unmatched_source_key_replaces_its_diagnostic(tmp_path: Path) -> None:
    root = _dimension_root(tmp_path)
    publish_generated_crosswalk(root, "vehicle_series", _crosswalk())
    save_override(root, "vehicle_series", {
        "source_ref": "attachment:sales-jan",
        "source_key": {"品牌": "比亚迪", "1-子车型": "比亚迪汉"},
        "action": "bind",
        "target_entity_key": "byd::han",
        "reason": "人工确认归属。",
    })

    preview = get_matching_view(root, "vehicle_series", source_ref="attachment:sales-jan")

    assert preview["count"] == 2
    assert {row["status"] for row in preview["rows"]} == {"auto_matched", "manual_override"}
    assert sum(1 for row in preview["rows"] if row["binding"]["key_fields"]["1-子车型"] == "比亚迪汉") == 1


def test_publisher_rejects_stale_new_mode_for_registered_source(tmp_path: Path) -> None:
    root = _dimension_root(tmp_path)
    publish_generated_crosswalk(root, "vehicle_series", _crosswalk())
    payload = {
        "build_rule": {
            "bindings": [{"role": "source", "source_id": "attachment:sales-jan", "source_mode": "new"}],
        },
    }
    with pytest.raises(ValueError, match="rebuild it in append mode"):
        _validate_registered_source_modes(root, "vehicle_series", payload)
