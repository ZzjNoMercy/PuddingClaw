"""Regression tests for analytics semantic asset registry."""

from __future__ import annotations

import io
import zipfile

from analytics.semantic_assets.registry import SemanticAssetRegistry
from analytics.semantic_assets.resolver import (
    format_semantic_assets_for_prompt,
    resolve_semantic_assets,
    resolve_semantic_assets_by_ids,
)


def test_semantic_asset_registry_create_and_refresh(tmp_path) -> None:
    registry = SemanticAssetRegistry(tmp_path)

    created = registry.create_asset(
        name="配置率",
        asset_type="measure",
        description="统计某配置在款型中的占比。",
        aliases=["配置渗透率"],
        tags=["vehicle"],
        version="1.2.3",
    )

    assert created["name"] == "配置率"
    assert created["type"] == "measure"
    assert created["path"].endswith("semantic-assets/measures/配置率/measure.md")

    snapshot = registry.refresh()
    assert snapshot["count"] == 1
    assert snapshot["type_counts"]["measure"] == 1
    assert snapshot["assets"][0]["aliases"] == ["配置渗透率"]
    assert registry.get_asset("measure:配置率")["frontmatter"]["version"] == "1.2.3"

    grain = registry.create_asset(
        name="款型颗粒度",
        asset_type="grain",
        description="按 car_name 去重。",
    )
    assert grain["type"] == "grain"
    assert grain["path"].endswith("semantic-assets/grains/款型颗粒度/grain.md")


def test_dimension_creation_preserves_resolution_contract(tmp_path) -> None:
    registry = SemanticAssetRegistry(tmp_path)

    asset = registry.create_asset(
        name="自然周",
        asset_type="dimension",
        description="将业务日期映射到周一开始的自然周。",
        dimension_definition={
            "mode": "calendar_lookup",
            "bindings": [
                {
                    "asset_ref": "table_asset:orders",
                    "display_name": "订单表",
                    "fields": {"date": "order_date"},
                }
            ],
            "date_field": "order_date",
            "week_start_day": "monday",
            "timezone": "Asia/Shanghai",
        },
    )

    assert asset["resolution_mode"] == "calendar_lookup"
    assert asset["resolution_label"] == "日历映射"
    detail = registry.get_asset("dimension:自然周")
    assert detail["frontmatter"]["resolution"]["date_field"] == "order_date"
    assert "日历映射" in detail["body"]


def test_entity_lookup_dimension_keeps_multiple_source_bindings(tmp_path) -> None:
    registry = SemanticAssetRegistry(tmp_path)
    asset = registry.create_asset(
        name="车系",
        asset_type="dimension",
        dimension_definition={
            "mode": "entity_lookup",
            "canonical": {"key": "entity_key", "fields": ["canonical_brand", "canonical_series"]},
            "bindings": [
                {"asset_ref": "table_asset:sales", "fields": {"brand": "品牌", "series": "1-子车型"}},
                {"asset_ref": "dbs:config.series", "fields": {"brand": "brand", "series": "serial_name"}},
            ],
            "reference_path": "references/crosswalk.json",
        },
    )

    assert asset["resolution_mode"] == "entity_lookup"
    detail = registry.get_asset("dimension:车系")
    resolution = detail["frontmatter"]["resolution"]
    assert resolution["canonical"]["key"] == "entity_key"
    assert len(resolution["bindings"]) == 2


def test_relation_asset_preserves_machine_readable_definition(tmp_path) -> None:
    registry = SemanticAssetRegistry(tmp_path)
    registry.create_asset(name="车系", asset_type="dimension")

    created = registry.create_asset(
        name="上险量关联车系",
        asset_type="relation",
        description="将上险量来源映射到规范车系。",
        relation_definition={
            "type": "dimension_binding",
            "asset": {"ref": "table_asset:insurance_sales", "key_fields": ["品牌", "1-子车型"]},
            "dimension": {"ref": "dimension:车系", "output_key": "entity_key"},
            "grain": ["月份", "品牌", "1-子车型"],
        },
    )

    assert created["type"] == "relation"
    assert created["relation_type"] == "dimension_binding"
    summary = registry.list_assets()["assets"][-1]
    assert summary["relation_definition"]["asset"]["ref"] == "table_asset:insurance_sales"
    detail = registry.get_asset(created["id"])
    assert detail["frontmatter"]["relation"]["dimension"]["ref"] == "dimension:车系"


def test_relation_assets_are_not_free_text_retrieved(tmp_path) -> None:
    registry = SemanticAssetRegistry(tmp_path)
    registry.create_asset(name="车系", asset_type="dimension")
    registry.create_asset(
        name="上险量关联车系",
        asset_type="relation",
        aliases=["上险量关联"],
        relation_definition={
            "type": "dimension_binding",
            "asset": {"ref": "table_asset:sales", "key_fields": ["品牌"]},
            "dimension": {"ref": "dimension:车系"},
        },
    )
    registry.refresh()

    resolution = resolve_semantic_assets("查询上险量关联", base_dir=tmp_path)
    assert all(item.type != "relation" for item in resolution["matched"])


def test_update_dimension_definition_preserves_body_and_custom_metadata(tmp_path) -> None:
    registry = SemanticAssetRegistry(tmp_path)
    created = registry.create_asset(
        name="车系",
        asset_type="dimension",
        description="跨源车系。",
        dimension_definition={"mode": "source_field"},
    )
    document = tmp_path / created["path"]
    document.write_text(
        document.read_text(encoding="utf-8").replace(
            "updated_at:",
            "build_skill:\n  name: build-semantic-dimension\n  adapter: vehicle_series_demo\nupdated_at:",
        )
        + "\n## AI 使用规则\n\n- 只使用已审核绑定。\n",
        encoding="utf-8",
    )

    updated = registry.update_dimension_definition(
        "dimension:车系",
        {
            "mode": "entity_lookup",
            "canonical": {"key": "entity_key", "fields": ["brand", "series"]},
            "bindings": [{"asset_ref": "table_asset:sales", "fields": {"brand": "品牌", "series": "车系"}}],
        },
        name="车系（已编辑）",
        description="已更新的跨源车系。",
        aliases=["车系名称"],
        tags=["跨源"],
        version="0.2.0",
    )

    assert updated["resolution_mode"] == "entity_lookup"
    assert updated["name"] == "车系（已编辑）"
    text = document.read_text(encoding="utf-8")
    assert "build_skill:" in text
    assert "只使用已审核绑定" in text
    assert "version: 0.2.0" in text


def test_semantic_asset_registry_import_zip_keeps_asset_folder(tmp_path) -> None:
    archive_bytes = io.BytesIO()
    with zipfile.ZipFile(archive_bytes, "w") as archive:
        archive.writestr(
            "launch_time/dimension.md",
            "---\nname: 上市时间\ntype: dimension\n---\n\n# 上市时间\n",
        )
        archive.writestr(
            "car_model/grain.md",
            "---\nname: 款型颗粒度\ntype: grain\n---\n\n# 款型颗粒度\n",
        )
    archive_bytes.seek(0)

    registry = SemanticAssetRegistry(tmp_path)
    result = registry.import_zip(archive_bytes)

    assert result["count"] == 2
    assert {asset["id"] for asset in result["assets"]} == {"dimension:launch_time", "grain:car_model"}


def test_semantic_asset_resolver_injects_dimension_guardrail(tmp_path) -> None:
    registry = SemanticAssetRegistry(tmp_path)
    registry.create_asset(
        name="上市时间",
        asset_type="dimension",
        description="上市时间取 vehicle_params 中 type_name='上市时间' 的 type_value。",
        aliases=["上市日期", "上市年份"],
        tags=["汽车"],
    )
    dimension_path = tmp_path / "semantic-assets" / "dimensions" / "上市时间" / "dimension.md"
    dimension_path.write_text(
        dimension_path.read_text(encoding="utf-8")
        + "\n## 禁止规则\n\n- 不要从 car_name 中的 25款、26款推断上市年份。\n",
        encoding="utf-8",
    )
    registry.refresh()

    resolution = resolve_semantic_assets(
        "统计 2021-2026 年上市车型的激光雷达配置率",
        base_dir=tmp_path,
    )
    prompt = format_semantic_assets_for_prompt(resolution)

    assert resolution["matched_count"] == 1
    assert resolution["matched"][0].id == "dimension:上市时间"
    assert "type_name='上市时间'" in prompt
    assert "不要从 car_name" in prompt


def test_selected_model_assets_are_exact_and_not_truncated(tmp_path) -> None:
    registry = SemanticAssetRegistry(tmp_path)
    selected = registry.create_asset(name="电机功率", asset_type="dimension", description="电机功率分段。")
    registry.create_asset(name="价格段", asset_type="dimension", description="价格分段。")
    selected_path = tmp_path / selected["path"]
    marker = "完整正文末尾规则"
    selected_path.write_text(
        selected_path.read_text(encoding="utf-8") + "\n" + ("规则内容" * 700) + marker,
        encoding="utf-8",
    )
    registry.refresh()

    resolution = resolve_semantic_assets_by_ids(
        "按电机功率统计价格趋势",
        requested_ids=[selected["id"]],
        base_dir=tmp_path,
    )
    prompt = format_semantic_assets_for_prompt(resolution)

    assert [asset.id for asset in resolution["matched"]] == [selected["id"]]
    assert marker in prompt
    assert "已截断" not in prompt


def test_measure_reference_is_resolved_after_measure_match(tmp_path) -> None:
    registry = SemanticAssetRegistry(tmp_path)
    registry.create_asset(
        name="配置率",
        asset_type="measure",
        description="统计某配置在目标集合中的搭载比例。",
        aliases=["搭载率"],
        tags=["汽车配置"],
    )
    reference_dir = tmp_path / "semantic-assets" / "measures" / "配置率" / "references"
    reference_dir.mkdir(parents=True, exist_ok=True)
    (reference_dir / "air_suspension.md").write_text(
        "# 空气悬架配置率口径\n\n"
        "适用目标配置：空气悬架、空悬。\n\n"
        "配置识别规则：使用 `type_name = '可调悬架种类'`，且 `type_value` 包含 `空气悬架`。\n",
        encoding="utf-8",
    )
    registry.refresh()

    resolution = resolve_semantic_assets("按价格段和年份统计空气悬架搭载率", base_dir=tmp_path)
    prompt = format_semantic_assets_for_prompt(resolution)

    assert resolution["matched_count"] >= 1
    assert resolution["reference_count"] == 1
    assert resolution["references"][0].type == "measure_reference"
    assert "可调悬架种类" in prompt
    assert "度量值 Reference" in prompt


def test_charging_c_rate_does_not_overmatch_generic_assets(tmp_path) -> None:
    registry = SemanticAssetRegistry(tmp_path)
    registry.create_asset(
        name="充电倍率",
        asset_type="measure",
        description="基于快充电量区间和快充时间计算平均快充倍率。",
        aliases=["快充倍率", "平均快充倍率"],
        tags=["充电"],
    )
    registry.create_asset(
        name="配置率",
        asset_type="measure",
        description="统计某配置在目标颗粒度集合中的搭载比例。",
        aliases=["搭载率"],
        tags=["汽车配置"],
    )
    registry.create_asset(
        name="上市时间",
        asset_type="dimension",
        description="vehicle_params 表中车型上市时间的标准取值口径。",
        aliases=["上市日期"],
        tags=["vehicle_params"],
    )
    registry.refresh()

    resolution = resolve_semantic_assets(
        "快充时间为0.5小时，快充电量为60%，快充倍率是多少",
        base_dir=tmp_path,
    )

    assert [asset.id for asset in resolution["matched"]] == ["measure:充电倍率"]
