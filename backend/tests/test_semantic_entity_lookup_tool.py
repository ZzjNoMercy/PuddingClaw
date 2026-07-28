"""Regression tests for active Crosswalk lookup."""

from __future__ import annotations

import json

from tools.database.semantic_entity_lookup_tool import SemanticEntityLookupTool


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
