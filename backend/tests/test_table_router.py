from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from analytics.nl2sql import table_router
from analytics.nl2sql.schemas import DatabaseQueryRequest
from analytics.nl2sql.table_router import (
    TableRouterError,
    _ambiguous_requested_aliases,
    _match_requested_tables,
    _model_database_tables_by_source,
    _model_table_aliases_by_source,
    _score_table,
)

MODEL_BASE_COLUMNS = [
    "brand",
    "serial_name",
    "car_name",
    "car_name_full_year",
    "launch_date",
    "launch_year",
    "launch_month",
    "energy_type",
    "vehicle_level",
    "wheelbase_mm",
    "motor_power_kw",
    "price",
    "price_band",
    "sale_status",
]

EAV_COLUMNS = [
    "id",
    "brand",
    "serial_name",
    "car_name",
    "category",
    "type_name",
    "type_value",
]


def test_model_dimension_question_prefers_vehicle_model_base() -> None:
    model_base = _score_table("统计2026年上市的纯电新车数量，排除皮卡", "vehicle_model_base", MODEL_BASE_COLUMNS)
    eav = _score_table("统计2026年上市的纯电新车数量，排除皮卡", "vehicle_params", EAV_COLUMNS)

    assert model_base.score > eav.score
    assert any("款型基础表" in reason for reason in model_base.reasons)


def test_wheelbase_question_prefers_vehicle_model_base() -> None:
    model_base = _score_table("按轴距区间统计乘用车款型占比", "vehicle_model_base", MODEL_BASE_COLUMNS)
    eav = _score_table("按轴距区间统计乘用车款型占比", "vehicle_params", EAV_COLUMNS)

    assert model_base.score > eav.score
    assert any("款型基础表" in reason for reason in model_base.reasons)


def test_motor_power_question_prefers_vehicle_model_base() -> None:
    model_base = _score_table("按电动机总功率区间统计新能源款型占比", "vehicle_model_base", MODEL_BASE_COLUMNS)
    eav = _score_table("按电动机总功率区间统计新能源款型占比", "vehicle_params", EAV_COLUMNS)

    assert model_base.score > eav.score
    assert any("款型基础表" in reason for reason in model_base.reasons)


def test_config_rate_question_selects_model_base_and_eav_tables() -> None:
    question = "2026年上市的纯电新车中，空气悬架的配备率是多少？"
    model_base = _score_table(question, "vehicle_model_base", MODEL_BASE_COLUMNS)
    eav = _score_table(question, "vehicle_params", EAV_COLUMNS)

    assert model_base.score > 0
    assert eav.score > 0
    assert any("款型基础表" in reason for reason in model_base.reasons)
    assert any("配置明细 EAV 表" in reason for reason in eav.reasons)


class _StubModelRegistry:
    def get_model(self, model_id: str) -> dict:
        assert model_id == "产品配置分析"
        return {
            "frontmatter": {
                "data_assets": {
                    "tables": [
                        "dbs_source.vehicle_params",
                        "dbs_source.vehicle_model_base",
                        "table_asset:tbl_file",
                    ],
                    "table_aliases": {
                        "dbs_source.vehicle_params": ["vehicle", "params"],
                        "dbs_source.vehicle_model_base": ["base", "model_base"],
                    },
                }
            }
        }


def test_model_database_tables_are_grouped_by_source(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(table_router, "get_analytics_model_registry", lambda: _StubModelRegistry())

    assert _model_database_tables_by_source("产品配置分析") == {"dbs_source": ["vehicle_params", "vehicle_model_base"]}
    assert _model_table_aliases_by_source("产品配置分析") == {
        "dbs_source": {
            "vehicle": "vehicle_params",
            "params": "vehicle_params",
            "base": "vehicle_model_base",
            "model_base": "vehicle_model_base",
        }
    }


def test_model_alias_cannot_override_a_real_table_identifier() -> None:
    with pytest.raises(TableRouterError, match="与真实数据表标识冲突"):
        _match_requested_tables(
            ["vehicle_params"],
            ["vehicle_params", "vehicle_model_base"],
            configured_aliases={"vehicle_params": "vehicle_model_base"},
        )


def test_same_alias_across_sources_requires_source_disambiguation() -> None:
    assert _ambiguous_requested_aliases(
        "base表最新上市时间",
        [],
        {
            "source_a": {"base": "vehicle_model_base"},
            "source_b": {"base": "archived_vehicle_base"},
        },
    ) == {
        "base": [
            "source_a.vehicle_model_base",
            "source_b.archived_vehicle_base",
        ]
    }


@pytest.mark.asyncio
async def test_selected_model_routes_all_declared_database_tables(monkeypatch: pytest.MonkeyPatch) -> None:
    source = {"id": "dbs_source", "name": "汽车库", "database": "cars"}
    monkeypatch.setattr(table_router, "get_analytics_model_registry", lambda: _StubModelRegistry())
    monkeypatch.setattr(table_router, "list_database_sources", AsyncMock(return_value=[source]))
    monkeypatch.setattr(table_router, "get_database_source", AsyncMock(return_value=source))
    monkeypatch.setattr(
        table_router,
        "database_source_selected_tables",
        lambda _source: ["vehicle_params", "vehicle_model_base"],
    )
    monkeypatch.setattr(
        table_router,
        "_load_columns",
        AsyncMock(return_value={"vehicle_params": EAV_COLUMNS, "vehicle_model_base": MODEL_BASE_COLUMNS}),
    )

    route = await table_router.route_database_tables(
        None,
        DatabaseQueryRequest(
            question="查询新能源车型年度更新次数和平均更新周期",
            model_id="产品配置分析",
            measure_ids=["measure:launch_update_count", "measure:launch_cycle"],
        ),
    )

    assert route.database_source_id == "dbs_source"
    assert route.table_names == ["vehicle_params", "vehicle_model_base"]
    assert route.confidence == 1.0
    assert route.reason == "分析模型声明的数据资产"


@pytest.mark.asyncio
async def test_agent_selected_table_cannot_escape_active_model_scope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = {"id": "dbs_source", "name": "汽车库", "database": "cars"}
    monkeypatch.setattr(table_router, "get_analytics_model_registry", lambda: _StubModelRegistry())
    monkeypatch.setattr(table_router, "list_database_sources", AsyncMock(return_value=[source]))
    monkeypatch.setattr(table_router, "get_database_source", AsyncMock(return_value=source))
    monkeypatch.setattr(
        table_router,
        "database_source_selected_tables",
        lambda _source: ["vehicle_params", "vehicle_model_base", "private_sales"],
    )

    with pytest.raises(TableRouterError, match="当前分析模型的数据资产范围"):
        await table_router.route_database_tables(
            None,
            DatabaseQueryRequest(
                question="查询私域销量",
                model_id="产品配置分析",
                table_names=["private_sales"],
                database_source_id="dbs_source",
            ),
        )


@pytest.mark.asyncio
async def test_agent_may_select_subset_inside_active_model_scope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = {"id": "dbs_source", "name": "汽车库", "database": "cars"}
    monkeypatch.setattr(table_router, "get_analytics_model_registry", lambda: _StubModelRegistry())
    monkeypatch.setattr(table_router, "list_database_sources", AsyncMock(return_value=[source]))
    monkeypatch.setattr(table_router, "get_database_source", AsyncMock(return_value=source))
    monkeypatch.setattr(
        table_router,
        "database_source_selected_tables",
        lambda _source: ["vehicle_params", "vehicle_model_base", "private_sales"],
    )
    monkeypatch.setattr(
        table_router,
        "_load_columns",
        AsyncMock(return_value={"vehicle_model_base": MODEL_BASE_COLUMNS}),
    )

    route = await table_router.route_database_tables(
        None,
        DatabaseQueryRequest(
            question="查询最新上市时间",
            model_id="产品配置分析",
            table_names=["vehicle_model_base"],
            database_source_id="dbs_source",
        ),
    )

    assert route.table_names == ["vehicle_model_base"]
    assert route.available_tables == ["vehicle_params", "vehicle_model_base"]


@pytest.mark.asyncio
async def test_agent_may_pass_full_model_data_asset_references(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = {"id": "dbs_source", "name": "汽车库", "database": "cars"}
    monkeypatch.setattr(table_router, "get_analytics_model_registry", lambda: _StubModelRegistry())
    monkeypatch.setattr(table_router, "list_database_sources", AsyncMock(return_value=[source]))
    monkeypatch.setattr(table_router, "get_database_source", AsyncMock(return_value=source))
    monkeypatch.setattr(
        table_router,
        "database_source_selected_tables",
        lambda _source: ["vehicle_params", "vehicle_model_base", "private_sales"],
    )
    monkeypatch.setattr(
        table_router,
        "_load_columns",
        AsyncMock(
            return_value={
                "vehicle_params": EAV_COLUMNS,
                "vehicle_model_base": MODEL_BASE_COLUMNS,
            }
        ),
    )

    route = await table_router.route_database_tables(
        None,
        DatabaseQueryRequest(
            question=("分别查询 vehicle_params 表与 vehicle_model_base 表各自的最新上市时间，并判断是否一致"),
            model_id="产品配置分析",
            table_names=[
                "dbs_source.vehicle_params",
                "dbs_source.vehicle_model_base",
            ],
            database_source_id="dbs_source",
        ),
    )

    assert route.table_names == ["vehicle_params", "vehicle_model_base"]
    assert route.available_tables == ["vehicle_params", "vehicle_model_base"]
    assert route.alias_resolutions == [
        {
            "alias": "dbs_source.vehicle_params",
            "table": "vehicle_params",
            "source": "analytics_model:产品配置分析:data_asset_ref",
        },
        {
            "alias": "dbs_source.vehicle_model_base",
            "table": "vehicle_model_base",
            "source": "analytics_model:产品配置分析:data_asset_ref",
        },
    ]


@pytest.mark.asyncio
async def test_model_declared_table_aliases_are_resolved_with_provenance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = {"id": "dbs_source", "name": "汽车库", "database": "cars"}
    monkeypatch.setattr(table_router, "get_analytics_model_registry", lambda: _StubModelRegistry())
    monkeypatch.setattr(table_router, "list_database_sources", AsyncMock(return_value=[source]))
    monkeypatch.setattr(table_router, "get_database_source", AsyncMock(return_value=source))
    monkeypatch.setattr(
        table_router,
        "database_source_selected_tables",
        lambda _source: ["vehicle_params", "vehicle_model_base"],
    )
    monkeypatch.setattr(
        table_router,
        "_load_columns",
        AsyncMock(return_value={"vehicle_params": EAV_COLUMNS, "vehicle_model_base": MODEL_BASE_COLUMNS}),
    )

    route = await table_router.route_database_tables(
        None,
        DatabaseQueryRequest(
            question="vehicle和base表最新的上市时间一致了吗？是什么时候",
            model_id="产品配置分析",
            table_names=["vehicle", "base"],
        ),
    )

    assert route.table_names == ["vehicle_params", "vehicle_model_base"]
    assert route.alias_resolutions == [
        {
            "alias": "vehicle",
            "table": "vehicle_params",
            "source": "analytics_model:产品配置分析",
        },
        {
            "alias": "base",
            "table": "vehicle_model_base",
            "source": "analytics_model:产品配置分析",
        },
    ]
    assert "vehicle -> vehicle_params" in route.prompt_context
    assert any("vehicle→vehicle_params" in reason for candidate in route.candidates for reason in candidate.reasons)


@pytest.mark.asyncio
async def test_question_alias_must_agree_with_explicit_table_scope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = {"id": "dbs_source", "name": "汽车库", "database": "cars"}
    monkeypatch.setattr(table_router, "get_analytics_model_registry", lambda: _StubModelRegistry())
    monkeypatch.setattr(table_router, "list_database_sources", AsyncMock(return_value=[source]))
    monkeypatch.setattr(table_router, "get_database_source", AsyncMock(return_value=source))
    monkeypatch.setattr(
        table_router,
        "database_source_selected_tables",
        lambda _source: ["vehicle_params", "vehicle_model_base"],
    )

    with pytest.raises(TableRouterError, match="表别名与显式 table_names 冲突"):
        await table_router.route_database_tables(
            None,
            DatabaseQueryRequest(
                question="vehicle表最新上市时间",
                model_id="产品配置分析",
                table_names=["base"],
                database_source_id="dbs_source",
            ),
        )
