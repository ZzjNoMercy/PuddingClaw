"""Regression tests for active Crosswalk lookup."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import analytics.semantic_assets.registry as semantic_registry_module
import tools.database.semantic_entity_lookup_tool as lookup_module
from tools.database.semantic_entity_lookup_tool import SemanticEntityLookupTool


@pytest.fixture(autouse=True)
def _temporary_crosswalk(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Build the smallest entity dimension and active Crosswalk in Home."""

    home = tmp_path / "puddingclaw-home"
    monkeypatch.setenv("PUDDINGCLAW_HOME", str(home))
    monkeypatch.setattr(semantic_registry_module, "_REGISTRIES", {})
    monkeypatch.setattr(lookup_module, "_CROSSWALK_CACHE", {})

    asset_dir = home / "definitions" / "semantic-assets" / "dimensions" / "vehicle_series"
    reference_dir = asset_dir / "references"
    reference_dir.mkdir(parents=True)
    (asset_dir / "dimension.md").write_text(
        """---
formatter: semantic-asset
name: Vehicle Series
type: dimension
resolution_mode: entity_lookup
resolution:
  mode: entity_lookup
  canonical:
    key: entity_key
    fields: [brand, serial_name]
  reference_path: references/active_crosswalk.json
---

# Vehicle Series
""",
        encoding="utf-8",
    )
    (reference_dir / "active_crosswalk.json").write_text(
        json.dumps(
            {
                "version": "v0.1.0",
                "records": [
                    {
                        "bindings": [
                            {
                                "source_ref": "table_asset:tbl_73d53d94a3a29ff425235dfa",
                                "key_fields": {"品牌": "比亚迪", "1-子车型": "比亚迪秦PLUS"},
                            }
                        ],
                        "entity": {"entity_key": "比亚迪::秦plus"},
                        "resolution": {
                            "status": "auto_matched",
                            "join_eligible": True,
                            "confidence": 1.0,
                        },
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )


def test_vehicle_series_lookup_uses_active_reference() -> None:
    result = json.loads(
        SemanticEntityLookupTool()._lookup(
            dimension_id="vehicle_series",
            source_ref="table_asset:tbl_73d53d94a3a29ff425235dfa",
            keys=[{"品牌": "比亚迪", "1-子车型": "比亚迪秦PLUS"}],
        )
    )

    assert result["reference_path"] == "references/active_crosswalk.json"
    assert result["matched"] == [
        {
            "source_key": {"品牌": "比亚迪", "1-子车型": "比亚迪秦PLUS"},
            "entity_key": "比亚迪::秦plus",
            "status": "auto_matched",
            "join_eligible": True,
            "confidence": 1.0,
        }
    ]
