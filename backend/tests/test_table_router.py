from __future__ import annotations

from analytics.nl2sql.table_router import _score_table


WIDE_COLUMNS = [
    "brand",
    "serial_name",
    "car_name",
    "car_name_full_year",
    "launch_date",
    "launch_year",
    "launch_month",
    "energy_type",
    "vehicle_level",
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


def test_model_dimension_question_prefers_vehicle_params_wide() -> None:
    wide = _score_table("统计2026年上市的纯电新车数量，排除皮卡", "vehicle_params_wide", WIDE_COLUMNS)
    eav = _score_table("统计2026年上市的纯电新车数量，排除皮卡", "vehicle_params", EAV_COLUMNS)

    assert wide.score > eav.score
    assert any("款型基础维度宽表" in reason for reason in wide.reasons)


def test_config_rate_question_selects_wide_and_eav_tables() -> None:
    question = "2026年上市的纯电新车中，空气悬架的配备率是多少？"
    wide = _score_table(question, "vehicle_params_wide", WIDE_COLUMNS)
    eav = _score_table(question, "vehicle_params", EAV_COLUMNS)

    assert wide.score > 0
    assert eav.score > 0
    assert any("款型基础维度宽表" in reason for reason in wide.reasons)
    assert any("配置明细 EAV 表" in reason for reason in eav.reasons)
