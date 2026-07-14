"""Regression tests for hard semantic SQL guardrails."""

from __future__ import annotations

from types import SimpleNamespace

from analytics.nl2sql.service import _detect_semantic_sql_conflicts
from tools.database.sql_generate_tool import _format_semantic_contract


def test_sql_generate_semantic_contract_exposes_authoritative_measure_and_reference_rules() -> None:
    semantic_trace = {
        "matched": [
            {
                "id": "measure:config_rate",
                "name": "配置率",
                "type": "measure",
                "path": "semantic-assets/measures/config_rate/measure.md",
                "body_preview": "配置率默认排除车型级别为皮卡的车型。除非用户明确要求包含皮卡。",
            }
        ],
        "references": [
            {
                "id": "measure:config_rate:references/air_suspension",
                "name": "air suspension",
                "type": "measure_reference",
                "path": "semantic-assets/measures/config_rate/references/air_suspension.md",
                "body_preview": "空气悬架应使用 type_name = '可调悬架种类'，type_value 包含空气悬架。",
            }
        ],
    }

    output = "\n".join(_format_semantic_contract(semantic_trace))

    assert "权威语义口径" in output
    assert "不得凭字段名或常识直接覆盖" in output
    assert "可调悬架种类" in output
    assert "默认排除车型级别为皮卡" in output
    assert "air_suspension.md" in output


def test_launch_time_dimension_blocks_model_year_from_car_name() -> None:
    sql = "SELECT COUNT(DISTINCT car_name) FROM vehicle_params WHERE car_name LIKE '26款%'"
    semantic_trace = {
        "matched": [
            {"id": "dimension:launch_time", "name": "上市时间", "type": "dimension"},
        ],
        "references": [],
    }

    route = SimpleNamespace(table_names=["vehicle_params"])

    conflicts = _detect_semantic_sql_conflicts(sql, semantic_trace, route)

    assert conflicts
    assert "launch_time_no_car_name_year" in conflicts[0]
    assert "type_name = '上市时间'" in conflicts[0]


def test_air_suspension_reference_blocks_type_name_guess() -> None:
    sql = """
    SELECT COUNT(DISTINCT car_name)
    FROM vehicle_params
    WHERE type_name LIKE '%空气悬架%'
      AND type_value <> '-'
    """
    semantic_trace = {
        "matched": [
            {"id": "measure:config_rate", "name": "配置率", "type": "measure"},
        ],
        "references": [
            {
                "id": "measure:config_rate:references/air_suspension",
                "name": "air suspension",
                "type": "measure_reference",
            }
        ],
    }

    route = SimpleNamespace(table_names=["vehicle_params"])

    conflicts = _detect_semantic_sql_conflicts(sql, semantic_trace, route)

    assert conflicts
    assert "可调悬架种类" in conflicts[0]


def test_launch_time_dimension_allows_real_launch_time_filter() -> None:
    sql = """
    SELECT COUNT(DISTINCT car_name)
    FROM vehicle_params
    WHERE type_name = '上市时间'
      AND type_value LIKE '2026-%'
    """
    semantic_trace = {
        "matched": [
            {"id": "dimension:launch_time", "name": "上市时间", "type": "dimension"},
        ],
        "references": [],
    }

    assert _detect_semantic_sql_conflicts(sql, semantic_trace) == []


def test_config_rate_blocks_eav_exists_distinct_slow_pattern() -> None:
    sql = """
    WITH eligible AS (
        SELECT DISTINCT vp.car_name
        FROM vehicle_params vp
        WHERE EXISTS (
            SELECT 1 FROM vehicle_params e
            WHERE e.car_name = vp.car_name
              AND e.type_name = '能源类型' AND e.type_value = '纯电'
        )
        AND EXISTS (
            SELECT 1 FROM vehicle_params t
            WHERE t.car_name = vp.car_name
              AND t.type_name = '上市时间' AND t.type_value LIKE '2026-%'
        )
        AND NOT EXISTS (
            SELECT 1 FROM vehicle_params l
            WHERE l.car_name = vp.car_name
              AND l.type_name = '级别' AND l.type_value = '皮卡'
        )
    )
    SELECT COUNT(DISTINCT eligible.car_name)
    FROM eligible
    """
    semantic_trace = {
        "matched": [
            {"id": "measure:config_rate", "name": "配置率", "type": "measure"},
            {"id": "dimension:launch_time", "name": "上市时间", "type": "dimension"},
            {"id": "dimension:energy_type", "name": "能源类型", "type": "dimension"},
        ],
        "references": [],
    }

    route = SimpleNamespace(table_names=["vehicle_params"])

    conflicts = _detect_semantic_sql_conflicts(sql, semantic_trace, route, question="配置率")

    assert conflicts
    assert "config_rate_no_exists_distinct" in conflicts[0]


def test_config_rate_allows_eav_flags_fallback_with_model_key_pattern() -> None:
    sql = """
    WITH car_flags AS (
      SELECT
        brand,
        serial_name,
        car_name,
        BOOL_OR(type_name = '上市时间' AND type_value >= '2026-01-01' AND type_value < '2027-01-01') AS is_2026_launch,
        BOOL_OR(type_name = '能源类型' AND type_value = '纯电') AS is_ev,
        BOOL_OR(type_name = '级别' AND type_value = '皮卡') AS is_pickup,
        BOOL_OR(type_name = '可调悬架种类' AND type_value LIKE '%空气悬架%') AS has_air_suspension
      FROM vehicle_params
      WHERE car_name IS NOT NULL
        AND brand IS NOT NULL
        AND serial_name IS NOT NULL
        AND type_name IN ('上市时间', '能源类型', '级别', '可调悬架种类')
      GROUP BY brand, serial_name, car_name
    )
    SELECT COUNT(*) FILTER (WHERE is_2026_launch AND is_ev AND NOT is_pickup) AS total_models
    FROM car_flags
    """
    semantic_trace = {
        "matched": [
            {"id": "measure:config_rate", "name": "配置率", "type": "measure"},
        ],
        "references": [],
    }

    assert _detect_semantic_sql_conflicts(sql, semantic_trace, question="配置率") == []


def test_config_rate_blocks_eav_denominator_when_model_base_table_is_routed() -> None:
    sql = """
    WITH car_flags AS (
      SELECT
        brand,
        serial_name,
        car_name,
        BOOL_OR(type_name = '上市时间' AND type_value >= '2026-01-01' AND type_value < '2027-01-01') AS is_target_launch,
        BOOL_OR(type_name = '能源类型' AND type_value = '纯电') AS is_target_energy,
        BOOL_OR(type_name = '级别' AND type_value = '皮卡') AS is_pickup,
        BOOL_OR(type_name = '可调悬架种类' AND type_value LIKE '%空气悬架%') AS has_air_suspension
      FROM vehicle_params
      WHERE car_name IS NOT NULL
        AND brand IS NOT NULL
        AND serial_name IS NOT NULL
        AND type_name IN ('上市时间', '能源类型', '级别', '可调悬架种类')
      GROUP BY brand, serial_name, car_name
    )
    SELECT
      COUNT(*) FILTER (WHERE is_target_launch AND is_target_energy AND NOT is_pickup) AS total_count,
      COUNT(*) FILTER (WHERE is_target_launch AND is_target_energy AND NOT is_pickup AND has_air_suspension) AS equipped_count
    FROM car_flags
    """
    semantic_trace = {
        "matched": [
            {"id": "measure:config_rate", "name": "配置率", "type": "measure"},
            {"id": "dimension:launch_time", "name": "上市时间", "type": "dimension"},
            {"id": "dimension:energy_type", "name": "能源类型", "type": "dimension"},
            {"id": "dimension:vehicle_level", "name": "车型级别", "type": "dimension"},
        ],
        "references": [],
    }
    route = SimpleNamespace(table_names=["vehicle_model_base", "vehicle_params"])

    conflicts = _detect_semantic_sql_conflicts(sql, semantic_trace, route, question="配置率")

    assert conflicts
    assert "vehicle_model_base" in conflicts[0]


def test_config_rate_blocks_eav_flags_grouped_only_by_car_name() -> None:
    sql = """
    WITH car_flags AS (
      SELECT
        car_name,
        BOOL_OR(type_name = '上市时间' AND type_value >= '2026-01-01' AND type_value < '2027-01-01') AS is_target_launch,
        BOOL_OR(type_name = '能源类型' AND type_value = '纯电') AS is_target_energy,
        BOOL_OR(type_name = '级别' AND type_value = '皮卡') AS is_pickup,
        BOOL_OR(type_name = '可调悬架种类' AND type_value LIKE '%空气悬架%') AS has_air_suspension
      FROM vehicle_params
      WHERE car_name IS NOT NULL
        AND type_name IN ('上市时间', '能源类型', '级别', '可调悬架种类')
      GROUP BY car_name
    )
    SELECT COUNT(*) FILTER (WHERE is_target_launch AND is_target_energy AND NOT is_pickup) AS total_count
    FROM car_flags
    """
    semantic_trace = {
        "matched": [
            {"id": "measure:config_rate", "name": "配置率", "type": "measure"},
        ],
        "references": [],
    }

    route = SimpleNamespace(table_names=["vehicle_params"])

    conflicts = _detect_semantic_sql_conflicts(sql, semantic_trace, route, question="配置率")

    assert conflicts
    assert "brand + serial_name + car_name" in conflicts[0]
