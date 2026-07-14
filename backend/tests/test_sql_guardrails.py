"""Tests for configurable SQL guardrails."""

from __future__ import annotations

from types import SimpleNamespace

from analytics.nl2sql import guardrails as guardrail_module
from analytics.nl2sql.guardrails import (
    GuardrailRule,
    detect_guardrail_conflicts,
    list_guardrail_rules,
    reset_guardrail_rules,
    scope_matches,
    upsert_guardrail_rule,
)


def test_scope_matches_semantic_asset_and_table() -> None:
    rule = GuardrailRule.model_validate(
        {
            "id": "config_rate_model_key_group",
            "name": "配置率款型颗粒度分组",
            "type": "require_group_by",
            "scope": {
                "table_scope": {"mode": "any", "values": ["vehicle_params"]},
                "semantic_assets": ["measure:config_rate"],
            },
            "params": {
                "forbidden_columns_only": ["car_name"],
            },
        }
    )
    route = SimpleNamespace(table_names=["vehicle_params"])
    semantic_trace = {"matched": [{"id": "measure:config_rate"}], "references": []}

    assert scope_matches(rule, source_name="insight_data", route=route, semantic_trace=semantic_trace)


def test_scope_requires_metric_intent_when_configured() -> None:
    rule = GuardrailRule.model_validate(
        {
            "id": "config_rate_use_model_base_denominator",
            "name": "配置率优先使用款型基础表分母",
            "type": "require_table_when_available",
            "scope": {
                "table_scope": {"mode": "all", "values": ["vehicle_params", "vehicle_model_base"]},
                "semantic_assets": ["measure:config_rate"],
                "intent_any": ["配置率", "搭载率", "渗透率", "配备率", "占比"],
            },
            "params": {"required_table": "vehicle_model_base", "fallback_table": "vehicle_params"},
        }
    )
    route = SimpleNamespace(table_names=["vehicle_params", "vehicle_model_base"])
    semantic_trace = {"matched": [{"id": "measure:config_rate"}], "references": []}

    assert not scope_matches(
        rule,
        source_name="insight_data",
        route=route,
        semantic_trace=semantic_trace,
        question="列出汉新上市车型有哪些配置",
    )
    assert scope_matches(
        rule,
        source_name="insight_data",
        route=route,
        semantic_trace=semantic_trace,
        question="2026年空气悬架配置率是多少",
    )


def test_require_group_by_rule_blocks_car_name_only() -> None:
    rule = GuardrailRule.model_validate(
        {
            "id": "config_rate_model_key_group",
            "name": "配置率款型颗粒度分组",
            "type": "require_group_by",
            "scope": {
                "table_scope": {"mode": "any", "values": ["vehicle_params"]},
                "semantic_assets": ["measure:config_rate"],
            },
            "params": {
                "forbidden_columns_only": ["car_name"],
            },
            "action": {"type": "rewrite", "message": "默认款型颗粒度必须按 brand + serial_name + car_name 分组。"},
        }
    )
    sql = """
    WITH car_flags AS (
      SELECT car_name, BOOL_OR(type_name = '级别' AND type_value = '皮卡') AS is_pickup
      FROM vehicle_params
      GROUP BY car_name
    )
    SELECT COUNT(*) FROM car_flags
    """
    route = SimpleNamespace(table_names=["vehicle_params"])
    semantic_trace = {"matched": [{"id": "measure:config_rate"}], "references": []}

    conflicts = detect_guardrail_conflicts(
        sql,
        source_name="insight_data",
        route=route,
        semantic_trace=semantic_trace,
        rules=[rule],
    )

    assert len(conflicts) == 1
    assert conflicts[0].rule_id == "config_rate_model_key_group"
    assert conflicts[0].action == "rewrite"


def test_require_group_by_rule_allows_model_key_group() -> None:
    rule = GuardrailRule.model_validate(
        {
            "id": "config_rate_model_key_group",
            "name": "配置率款型颗粒度分组",
            "type": "require_group_by",
            "scope": {
                "table_scope": {"mode": "any", "values": ["vehicle_params"]},
                "semantic_assets": ["measure:config_rate"],
            },
            "params": {
                "forbidden_columns_only": ["car_name"],
            },
        }
    )
    sql = """
    WITH car_flags AS (
      SELECT brand, serial_name, car_name, BOOL_OR(type_name = '级别' AND type_value = '皮卡') AS is_pickup
      FROM vehicle_params
      GROUP BY brand, serial_name, car_name
    )
    SELECT COUNT(*) FROM car_flags
    """
    route = SimpleNamespace(table_names=["vehicle_params"])
    semantic_trace = {"matched": [{"id": "measure:config_rate"}], "references": []}

    conflicts = detect_guardrail_conflicts(
        sql,
        source_name="insight_data",
        route=route,
        semantic_trace=semantic_trace,
        rules=[rule],
    )

    assert conflicts == []


def test_require_group_by_rule_allows_year_rollup_after_model_key_ctes() -> None:
    rule = GuardrailRule.model_validate(
        {
            "id": "config_rate_model_key_group",
            "name": "配置率款型颗粒度分组",
            "type": "require_group_by",
            "scope": {
                "table_scope": {"mode": "any", "values": ["vehicle_params"]},
                "semantic_assets": ["measure:config_rate"],
            },
            "params": {
                "forbidden_columns_only": ["car_name"],
            },
        }
    )
    sql = """
    WITH denominator AS (
      SELECT DISTINCT brand, serial_name, car_name, launch_year
      FROM vehicle_model_base
      WHERE launch_year BETWEEN 2020 AND 2026
    ),
    numerator AS (
      SELECT DISTINCT d.brand, d.serial_name, d.car_name, d.launch_year
      FROM denominator d
      JOIN vehicle_params vp
        ON vp.brand = d.brand
       AND vp.serial_name = d.serial_name
       AND vp.car_name = d.car_name
      WHERE vp.type_name = '激光雷达数量'
    ),
    models_with_flag AS (
      SELECT
        d.launch_year,
        d.brand,
        d.serial_name,
        d.car_name,
        CASE WHEN n.car_name IS NOT NULL THEN 1 ELSE 0 END AS has_lidar
      FROM denominator d
      LEFT JOIN numerator n
        ON d.brand = n.brand
       AND d.serial_name = n.serial_name
       AND d.car_name = n.car_name
    )
    SELECT
      launch_year,
      COUNT(*) AS total_models,
      SUM(has_lidar) AS equipped_models
    FROM models_with_flag
    GROUP BY launch_year
    ORDER BY launch_year
    """
    route = SimpleNamespace(table_names=["vehicle_model_base", "vehicle_params"])
    semantic_trace = {"matched": [{"id": "measure:config_rate"}], "references": []}

    conflicts = detect_guardrail_conflicts(
        sql,
        source_name="insight_data",
        route=route,
        semantic_trace=semantic_trace,
        rules=[rule],
    )

    assert conflicts == []


def test_global_guardrail_blocks_count_distinct_nullable_tuple_after_left_join() -> None:
    rule = GuardrailRule.model_validate(
        {
            "id": "postgres_count_distinct_nullable_tuple_after_left_join",
            "name": "PostgreSQL LEFT JOIN 后禁止直接 COUNT DISTINCT 右表 nullable tuple",
            "type": "forbid_sql_pattern",
            "scope": {"table_scope": {"mode": "any", "values": []}, "semantic_assets": []},
            "params": {
                "pattern": r"(?=[\s\S]*\bLEFT\s+JOIN\b)(?=[\s\S]*\bCOUNT\s*\(\s*DISTINCT\s*\([^)]*\.[^)]*,[^)]*\)\s*\))",
            },
            "action": {"type": "rewrite", "message": "LEFT JOIN 后不要直接 COUNT nullable tuple。"},
        }
    )
    sql = """
    SELECT
      COUNT(DISTINCT (r.brand, r.serial_name, r.car_name)) AS equipped_models
    FROM denominator d
    LEFT JOIN numerator r
      ON r.brand = d.brand
     AND r.serial_name = d.serial_name
     AND r.car_name = d.car_name
    """
    route = SimpleNamespace(table_names=["vehicle_model_base", "vehicle_params"])
    semantic_trace = {"matched": [], "references": []}

    conflicts = detect_guardrail_conflicts(
        sql,
        source_name="insight_data",
        route=route,
        semantic_trace=semantic_trace,
        rules=[rule],
    )

    assert len(conflicts) == 1
    assert conflicts[0].rule_id == "postgres_count_distinct_nullable_tuple_after_left_join"


def test_global_guardrail_allows_left_join_with_filtered_tuple_count_distinct() -> None:
    rule = GuardrailRule.model_validate(
        {
            "id": "postgres_count_distinct_nullable_tuple_after_left_join",
            "name": "PostgreSQL LEFT JOIN 后禁止直接 COUNT DISTINCT 右表 nullable tuple",
            "type": "forbid_sql_pattern",
            "scope": {"table_scope": {"mode": "any", "values": []}, "semantic_assets": []},
            "params": {
                "pattern": r"(?=[\s\S]*\bLEFT\s+JOIN\b)(?=[\s\S]*\bCOUNT\s*\(\s*DISTINCT\s*\([^)]*\.[^)]*,[^)]*\)\s*\))",
                "unless_pattern": (
                    r"\bCOUNT\s*\(\s*DISTINCT\s*\([^)]*\.[^)]*,[^)]*\)\s*\)\s*"
                    r"FILTER\s*\(\s*WHERE\s+[^)]*\.[A-Za-z_][\w]*\s+IS\s+NOT\s+NULL\s*\)"
                ),
            },
        }
    )
    sql = """
    SELECT
      COUNT(DISTINCT (r.brand, r.serial_name, r.car_name))
        FILTER (WHERE r.brand IS NOT NULL) AS equipped_models
    FROM denominator d
    LEFT JOIN numerator r
      ON r.brand = d.brand
     AND r.serial_name = d.serial_name
     AND r.car_name = d.car_name
    """
    route = SimpleNamespace(table_names=["vehicle_model_base", "vehicle_params"])
    semantic_trace = {"matched": [], "references": []}

    conflicts = detect_guardrail_conflicts(
        sql,
        source_name="insight_data",
        route=route,
        semantic_trace=semantic_trace,
        rules=[rule],
    )

    assert conflicts == []


def test_guardrail_rules_are_loaded_from_markdown_assets(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(guardrail_module, "BASE_DIR", tmp_path)
    monkeypatch.setattr(guardrail_module, "GUARDRAILS_ROOT", tmp_path / "sql-guardrails")
    monkeypatch.setattr(guardrail_module, "GUARDRAILS_RULES_DIR", tmp_path / "sql-guardrails" / "rules")
    monkeypatch.setattr(guardrail_module, "GUARDRAILS_DRAFTS_DIR", tmp_path / "sql-guardrails" / "drafts")

    payload = reset_guardrail_rules()

    assert len(payload["guardrails"]) == 6
    doc_path = tmp_path / "sql-guardrails" / "rules" / "config_rate_model_key_group" / "guardrail.md"
    assert doc_path.exists()
    text = doc_path.read_text(encoding="utf-8")
    assert "formatter: sql-guardrail" in text
    assert "type: require_group_by" in text

    loaded = guardrail_module.load_guardrail_rules()
    assert sorted(rule.id for rule in loaded.guardrails) == sorted(item["id"] for item in payload["guardrails"])
    assert loaded.diagnostics == []

    listed = list_guardrail_rules()
    first = listed["guardrails"][0]
    assert first["document_path"].endswith("/guardrail.md")
    assert first["document_body"].startswith("# ")
    assert first["document_content"].startswith("---")


def test_guardrail_raw_markdown_upsert(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(guardrail_module, "BASE_DIR", tmp_path)
    monkeypatch.setattr(guardrail_module, "GUARDRAILS_ROOT", tmp_path / "sql-guardrails")
    monkeypatch.setattr(guardrail_module, "GUARDRAILS_RULES_DIR", tmp_path / "sql-guardrails" / "rules")
    monkeypatch.setattr(guardrail_module, "GUARDRAILS_DRAFTS_DIR", tmp_path / "sql-guardrails" / "drafts")
    content = """---
formatter: sql-guardrail
id: raw_rule
name: Raw Rule
enabled: true
type: require_sql_contains
scope:
  table_scope:
    mode: any
    values: []
  semantic_assets: []
params:
  contains: "select"
action:
  type: warn
  message: test
---

# Raw Rule

用户编辑的原始 Markdown 正文。
"""

    saved = upsert_guardrail_rule({"id": "raw_rule", "document_content": content})

    assert saved["id"] == "raw_rule"
    assert saved["document_body"].startswith("# Raw Rule")
    assert (tmp_path / "sql-guardrails" / "rules" / "raw_rule" / "guardrail.md").exists()
