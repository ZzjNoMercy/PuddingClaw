"""Regression tests for analytics semantic asset registry."""

from __future__ import annotations

import io
import zipfile

from analytics.semantic_assets.registry import SemanticAssetRegistry
from analytics.semantic_assets.resolver import format_semantic_assets_for_prompt, resolve_semantic_assets


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
