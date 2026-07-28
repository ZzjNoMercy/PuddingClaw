"""Contract tests for logical dataset HITL rule normalization."""

from __future__ import annotations

import asyncio

from graph.logical_dataset_resume import LogicalDatasetResumeRegistry


def test_logical_dataset_rule_keeps_user_baseline_first_and_strategy() -> None:
    async def run() -> None:
        registry = LogicalDatasetResumeRegistry()
        request = registry.create(
            session_id="session-test",
            query_id="query-test",
            tool_call_id="tool-test",
            payload={
                "operation": "create",
                "target_asset_id": "",
                "candidates": [
                    {"asset_id": "tbl_jan", "display_name": "1月", "fields": ["品牌", "销量"]},
                    {"asset_id": "tbl_feb", "display_name": "2月", "fields": ["品牌", "上险量"]},
                ],
            },
        )
        waiter = asyncio.create_task(registry.wait(request["id"]))
        await asyncio.sleep(0)
        registry.resolve(
            request["id"],
            {
                "action": "confirm",
                "name": "2023上险量",
                "baseline_asset_id": "tbl_feb",
                "source_asset_ids": ["tbl_jan", "tbl_feb"],
                "schema_mode": "baseline_fill_missing",
            },
        )
        decision = await waiter
        assert decision["dataset_rule"]["source_asset_ids"] == ["tbl_feb", "tbl_jan"]
        assert decision["dataset_rule"]["schema_mode"] == "baseline_fill_missing"

    asyncio.run(run())


def test_append_rule_keeps_target_out_of_new_source_ids() -> None:
    async def run() -> None:
        registry = LogicalDatasetResumeRegistry()
        request = registry.create(
            session_id="session-append",
            query_id="query-append",
            tool_call_id="tool-append",
            payload={
                "operation": "append",
                "target_asset_id": "tbl_concat_sales",
                "candidates": [{"asset_id": "tbl_dec", "display_name": "12月", "fields": ["品牌", "销量"]}],
            },
        )
        waiter = asyncio.create_task(registry.wait(request["id"]))
        await asyncio.sleep(0)
        registry.resolve(
            request["id"],
            {
                "action": "confirm",
                "name": "2023上险量",
                "baseline_asset_id": "tbl_concat_sales",
                "source_asset_ids": ["tbl_dec"],
                "schema_mode": "strict",
            },
        )
        decision = await waiter
        rule = decision["dataset_rule"]
        assert rule["target_asset_id"] == "tbl_concat_sales"
        assert rule["baseline_asset_id"] == "tbl_concat_sales"
        assert rule["source_asset_ids"] == ["tbl_dec"]

    asyncio.run(run())
