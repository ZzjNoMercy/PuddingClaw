"""Asset-relation graph validation for reusable analytics models."""

from __future__ import annotations

import json

import pytest

from analytics.models.registry import AnalyticsModelError, AnalyticsModelRegistry
from analytics.semantic_assets.registry import SemanticAssetRegistry


def _prepare_relations(tmp_path) -> tuple[str, str]:
    assets = SemanticAssetRegistry(tmp_path)
    assets.create_asset(name="车系", asset_type="dimension")
    binding = assets.create_asset(
        name="上险量接入车系",
        asset_type="relation",
        relation_definition={
            "type": "dimension_binding",
            "asset": {"ref": "table_asset:sales", "key_fields": ["品牌", "车系"]},
            "dimension": {"ref": "dimension:车系"},
        },
    )
    direct = assets.create_asset(
        name="订单关联配置",
        asset_type="relation",
        relation_definition={
            "type": "direct_join",
            "left": {"ref": "table_asset:orders", "key_fields": ["order_id"]},
            "right": {"ref": "insight_data.vehicle_model_base", "key_fields": ["order_id"]},
            "field_mapping": {"left": ["order_id"], "right": ["order_id"]},
            "cardinality": "many_to_one",
            "join_type": "left",
        },
    )
    return binding["id"], direct["id"]


def test_model_requires_connected_selected_relation(tmp_path) -> None:
    binding, _ = _prepare_relations(tmp_path)
    models = AnalyticsModelRegistry(tmp_path)

    with pytest.raises(AnalyticsModelError, match="必须选择资产关联"):
        models.create_model(
            name="无关系模型",
            data_assets={"tables": ["table_asset:sales", "insight_data.vehicle_model_base"]},
            semantic_assets={"dimensions": ["dimension:车系"]},
        )

    with pytest.raises(AnalyticsModelError, match="资产和维度必须均已被模型选择"):
        models.create_model(
            name="缺维度模型",
            data_assets={"tables": ["table_asset:sales", "insight_data.vehicle_model_base"]},
            semantic_assets={"dimensions": []},
            asset_relations=[binding],
        )


def test_model_context_resolves_published_relation(tmp_path) -> None:
    _, direct = _prepare_relations(tmp_path)
    models = AnalyticsModelRegistry(tmp_path)
    created = models.create_model(
        name="订单产品分析",
        data_assets={"tables": ["table_asset:orders", "insight_data.vehicle_model_base"]},
        semantic_assets={},
        asset_relations=[direct],
    )

    context = models.get_model_context(created["id"])
    assert context["missing_references"] == []
    assert context["asset_relations"][0]["type"] == "direct_join"
    assert context["asset_relations"][0]["definition"]["cardinality"] == "many_to_one"


def test_model_context_expands_selected_semantic_asset_frontmatter(tmp_path) -> None:
    assets = SemanticAssetRegistry(tmp_path)
    measure = assets.create_asset(
        name="上市周期",
        asset_type="measure",
        description="使用完整上市事件序列计算相邻间隔。",
    )
    dimension = assets.create_asset(name="上市时间", asset_type="dimension")
    models = AnalyticsModelRegistry(tmp_path)
    created = models.create_model(
        name="产品配置分析",
        semantic_assets={
            "measures": [measure["id"]],
            "dimensions": [dimension["id"]],
        },
    )

    context = models.get_model_context(created["id"])

    assert [item["id"] for item in context["semantic_assets"]] == [measure["id"], dimension["id"]]
    assert context["semantic_assets"][0]["description"] == "使用完整上市事件序列计算相邻间隔。"
    assert context["semantic_assets"][0]["frontmatter"]["name"] == "上市周期"
    json.dumps(context["semantic_assets"][0]["frontmatter"])
    assert "body" not in context["semantic_assets"][0]
    assert context["missing_references"] == []


def test_model_context_derives_common_dimension_path(tmp_path) -> None:
    assets = SemanticAssetRegistry(tmp_path)
    assets.create_asset(name="车系", asset_type="dimension")
    sales = assets.create_asset(
        name="上险量接入车系",
        asset_type="relation",
        relation_definition={
            "type": "dimension_binding",
            "asset": {"ref": "table_asset:sales", "key_fields": ["品牌", "车系"]},
            "dimension": {"ref": "dimension:车系"},
        },
    )
    config = assets.create_asset(
        name="配置接入车系",
        asset_type="relation",
        relation_definition={
            "type": "dimension_binding",
            "asset": {"ref": "insight_data.vehicle_model_base", "key_fields": ["brand", "serial_name"]},
            "dimension": {"ref": "dimension:车系"},
        },
    )
    models = AnalyticsModelRegistry(tmp_path)
    created = models.create_model(
        name="销量配置联合分析",
        data_assets={"tables": ["table_asset:sales", "insight_data.vehicle_model_base"]},
        semantic_assets={"dimensions": ["dimension:车系"]},
        asset_relations=[sales["id"], config["id"]],
    )

    context = models.get_model_context(created["id"])
    assert context["derived_dimension_paths"] == [
        {
            "dimension": "dimension:车系",
            "assets": ["insight_data.vehicle_model_base", "table_asset:sales"],
            "rule": "这些资产通过同一已选维度关联；联合分析必须经由该维度的规范键。",
        }
    ]


def test_model_normalizes_legacy_repeated_semantic_prefixes(tmp_path) -> None:
    binding, _ = _prepare_relations(tmp_path)
    models = AnalyticsModelRegistry(tmp_path)

    created = models.create_model(
        name="兼容旧前缀模型",
        data_assets={"tables": ["table_asset:sales"]},
        semantic_assets={"dimensions": ["dimension:dimension:车系"]},
        asset_relations=[binding],
    )

    assert created["frontmatter"]["semantic_assets"]["dimensions"] == ["dimension:车系"]


def test_model_context_injects_selected_virtual_dataset_summary(tmp_path) -> None:
    dataset_dir = tmp_path / "data" / "analytics-concat-datasets" / "tbl_sales_2023"
    dataset_dir.mkdir(parents=True)
    (dataset_dir / "dataset.json").write_text(
        json.dumps(
            {
                "formatter": "logical-data-asset",
                "name": "2023月度上险量",
                "kind": "vertical_union",
                "materialization": "virtual",
                "schema": {"fields": ["年份", "月份", "销量"]},
                "coverage": [{"field": "年份", "values": [2023]}],
                "statistics": {"source_count": 7, "rows_estimate": 100},
                "routing": {"preferred_intents": ["trend"]},
                "sources": [{"asset_id": "tbl_jan", "name": "1月"}],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    models = AnalyticsModelRegistry(tmp_path)
    created = models.create_model(name="月度分析", data_assets={"tables": ["table_asset:tbl_sales_2023"]})

    context = models.get_model_context(created["id"])

    assert context["logical_datasets"] == [
        {
            "asset_id": "tbl_sales_2023",
            "name": "2023月度上险量",
            "description": "",
            "tags": [],
            "kind": "vertical_union",
            "materialization": "virtual",
            "schema": {"fields": ["年份", "月份", "销量"]},
            "coverage": [{"field": "年份", "values": [2023]}],
            "statistics": {"source_count": 7, "rows_estimate": 100},
            "routing": {"preferred_intents": ["trend"]},
            "sources": [{"asset_id": "tbl_jan", "name": "1月", "sheet_name": None}],
        }
    ]


def test_model_context_injects_raw_table_profile_and_database_table(tmp_path, monkeypatch) -> None:
    knowledge_root = tmp_path / "knowledge"
    monkeypatch.setenv("PUDDINGCLAW_KNOWLEDGE_DIR", str(knowledge_root))
    profile_dir = knowledge_root / ".puddingclaw" / "table_profiles"
    profile_dir.mkdir(parents=True)
    (profile_dir / "tbl_sales_2022.profile.json").write_text(
        json.dumps(
            {
                "asset_id": "tbl_sales_2022",
                "kind": "table_asset_profile",
                "source_type": "excel",
                "file_name": "2022年乘用车市场上险量_宽表.xlsx",
                "virtual_path": "/knowledge/imported/2022年乘用车市场上险量_宽表.xlsx",
                "sheet_name": "Sheet1",
                "size_bytes": 1234,
                "shape": [464069, 40],
                "columns": [
                    {"name": "年份", "dtype": "int64", "sample_values": [2022]},
                    {"name": "品牌", "dtype": "str", "sample_values": ["比亚迪"]},
                    {"name": "10", "dtype": "int64", "sample_values": [1]},
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    models = AnalyticsModelRegistry(tmp_path)
    created = models.create_model(
        name="跨年销量分析",
        data_assets={"tables": ["table_asset:tbl_sales_2022"]},
    )

    context = models.get_model_context(created["id"])

    assert context["missing_data_assets"] == []
    assert context["data_assets"][0] == {
        "ref": "table_asset:tbl_sales_2022",
        "asset_id": "tbl_sales_2022",
        "asset_type": "raw_table",
        "name": "2022年乘用车市场上险量_宽表.xlsx",
        "source_type": "excel",
        "virtual_path": "/knowledge/imported/2022年乘用车市场上险量_宽表.xlsx",
        "sheet_name": "Sheet1",
        "schema": {
            "fields": ["年份", "品牌", "10"],
            "field_types": {"年份": "int64", "品牌": "str", "10": "int64"},
        },
        "coverage": {"years": ["2022"], "month_fields": ["10"]},
        "statistics": {"shape": [464069, 40], "size_bytes": 1234},
    }
    database_model = models.create_model(
        name="数据库资产分析",
        data_assets={"tables": ["dbs_demo.vehicle_model_base"]},
    )
    database_context = models.get_model_context(database_model["id"])
    assert database_context["data_assets"][0] == {
        "ref": "dbs_demo.vehicle_model_base",
        "asset_type": "database_table",
        "name": "vehicle_model_base",
        "database_source_id": "dbs_demo",
        "table_name": "vehicle_model_base",
    }
