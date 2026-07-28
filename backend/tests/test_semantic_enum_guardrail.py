"""semantic_enum_consistency detector: diesel regression and friends."""

import time

from analytics.nl2sql.guardrails import (
    GuardrailAction,
    GuardrailRule,
    GuardrailScope,
    _detect_semantic_enum_consistency,
    detect_guardrail_conflicts,
)

_RULE = GuardrailRule(
    id="semantic_enum_consistency",
    name="语义资产枚举一致性",
    type="semantic_enum_consistency",
    scope=GuardrailScope(),
    params={},
    action=GuardrailAction(type="rewrite", message="口径不一致"),
)

_TRACE_ENERGY = {
    "matched": [{"id": "dimension:energy_type"}],
    "references": [],
}

_DIESEL_CASE_SQL = """
WITH model_details AS (
    SELECT brand, serial_name, car_name,
        MAX(CASE WHEN type_name = '上市时间' THEN type_value END) AS launch_time_str,
        MAX(CASE WHEN type_name = '能源类型' THEN type_value END) AS energy_type_val,
        MAX(CASE WHEN type_name = '级别' THEN type_value END) AS vehicle_level_val
    FROM vehicle_params
    WHERE type_name IN ('上市时间', '能源类型', '级别')
    GROUP BY brand, serial_name, car_name
),
classified AS (
    SELECT brand, serial_name, launch_time_str,
        CASE
            WHEN energy_type_val IN ('纯电', '插电混合', '增程式纯电动') THEN '新能源'
            WHEN energy_type_val IN ('汽油', '汽油+48V轻混系统', '油电混合', '汽油电驱', '汽油+24V轻混系统', '柴油', '柴油+48V轻混系统') THEN '传统能源'
        END AS energy_group
    FROM model_details
    WHERE vehicle_level_val IS DISTINCT FROM '皮卡'
)
SELECT * FROM classified
"""

_ELSE_SQL = """
SELECT CASE
    WHEN energy_type IN ('纯电', '插电混合', '增程式纯电动') THEN '新能源'
    ELSE '传统能源'
END AS energy_group, launch_date
FROM vehicle_model_base
"""

_CLEAN_SQL = """
SELECT CASE
    WHEN energy_type IN ('纯电', '插电混合', '增程式纯电动') THEN '新能源'
    WHEN energy_type IN ('汽油', '汽油+48V轻混系统', '油电混合', '汽油电驱', '汽油+24V轻混系统') THEN '传统能源'
END AS energy_group, launch_date
FROM vehicle_model_base
WHERE vehicle_level IS DISTINCT FROM '皮卡'
"""


def test_diesel_in_explicit_enum_is_rejected_with_diff():
    conflict = _detect_semantic_enum_consistency(
        _DIESEL_CASE_SQL, _RULE, semantic_trace=_TRACE_ENERGY
    )
    assert conflict is not None
    assert "柴油" in conflict.message
    assert "传统能源" in conflict.message


def test_else_arm_into_classification_is_rejected():
    conflict = _detect_semantic_enum_consistency(_ELSE_SQL, _RULE, semantic_trace=_TRACE_ENERGY)
    assert conflict is not None
    assert "ELSE" in conflict.message


def test_mapping_compliant_sql_passes():
    conflict = _detect_semantic_enum_consistency(_CLEAN_SQL, _RULE, semantic_trace=_TRACE_ENERGY)
    assert conflict is None


def test_unknown_literal_outside_universe_is_rejected():
    sql = "SELECT * FROM vehicle_model_base WHERE energy_type IN ('纯电动', '汽油')"
    conflict = _detect_semantic_enum_consistency(sql, _RULE, semantic_trace=_TRACE_ENERGY)
    assert conflict is not None
    assert "纯电动" in conflict.message


def test_fuzzy_like_forbidden_pattern_is_rejected():
    sql = "SELECT * FROM vehicle_model_base WHERE energy_type LIKE '%纯电%'"
    conflict = _detect_semantic_enum_consistency(sql, _RULE, semantic_trace=_TRACE_ENERGY)
    assert conflict is not None
    assert "禁止模式" in conflict.message


def test_fuzzy_like_eav_pinned_is_rejected():
    sql = (
        "SELECT * FROM vehicle_params "
        "WHERE type_name = '能源类型' AND type_value LIKE '%纯电%'"
    )
    conflict = _detect_semantic_enum_consistency(sql, _RULE, semantic_trace=_TRACE_ENERGY)
    assert conflict is not None
    assert "禁止模式" in conflict.message


def test_ungoverned_sql_escapes():
    sql = "SELECT wheelbase_mm, COUNT(*) FROM vehicle_model_base GROUP BY wheelbase_mm"
    conflict = _detect_semantic_enum_consistency(sql, _RULE, semantic_trace=_TRACE_ENERGY)
    assert conflict is None


def test_fallback_catches_governed_column_without_resolved_trace():
    # semantic_trace empty: the governed column in the SQL must still trigger
    # the declaration-bearing asset check.
    conflict = _detect_semantic_enum_consistency(_DIESEL_CASE_SQL, _RULE, semantic_trace={})
    assert conflict is not None
    assert "柴油" in conflict.message


def test_eav_literals_from_other_domains_do_not_false_positive():
    # '皮卡' belongs to 级别, not 能源类型; a top-level type_name IN (...)
    # filter must not pin it into the energy domain.
    sql = """
    WITH car_flags AS (
      SELECT brand, serial_name, car_name,
        BOOL_OR(type_name = '能源类型' AND type_value = '纯电') AS is_ev,
        BOOL_OR(type_name = '级别' AND type_value = '皮卡') AS is_pickup
      FROM vehicle_params
      WHERE type_name IN ('上市时间', '能源类型', '级别')
      GROUP BY brand, serial_name, car_name
    )
    SELECT COUNT(*) FROM car_flags WHERE is_ev AND NOT is_pickup
    """
    conflict = _detect_semantic_enum_consistency(sql, _RULE, semantic_trace=_TRACE_ENERGY)
    assert conflict is None


def test_question_driven_plain_where_bypass_is_closed():
    # No CASE labels at all; the question text names 传统能源 while the SQL
    # sneaks diesel into a plain WHERE — this was the S1 bypass.
    sql = (
        "SELECT brand, car_name FROM vehicle_model_base "
        "WHERE energy_type IN ('汽油','汽油+48V轻混系统','油电混合','汽油电驱','汽油+24V轻混系统','柴油')"
    )
    conflict = _detect_semantic_enum_consistency(
        sql, _RULE, semantic_trace=_TRACE_ENERGY, question="每年传统能源和新能源的上市更新次数"
    )
    assert conflict is not None
    assert "柴油" in conflict.message


def test_subset_narrowing_is_legitimate():
    sql = (
        "SELECT CASE WHEN energy_type = '纯电' THEN '新能源' "
        "WHEN energy_type = '汽油' THEN '传统能源' END AS grp, COUNT(*) "
        "FROM vehicle_model_base GROUP BY 1"
    )
    conflict = _detect_semantic_enum_consistency(sql, _RULE, semantic_trace=_TRACE_ENERGY)
    assert conflict is None


def test_unrelated_label_column_is_not_judged():
    sql = (
        "SELECT CASE WHEN serial_name = '轩逸' THEN '传统能源' ELSE '新能源' END AS grp "
        "FROM vehicle_model_base"
    )
    conflict = _detect_semantic_enum_consistency(sql, _RULE, semantic_trace=_TRACE_ENERGY)
    assert conflict is None


def test_not_in_exclusion_is_not_inclusion():
    sql = "SELECT * FROM vehicle_model_base WHERE energy_type NOT IN ('纯电动','插电混动')"
    conflict = _detect_semantic_enum_consistency(sql, _RULE, semantic_trace=_TRACE_ENERGY)
    assert conflict is None


def test_like_on_ungoverned_column_is_not_forbidden():
    sql = "SELECT * FROM vehicle_model_base WHERE car_name LIKE '%纯电%'"
    conflict = _detect_semantic_enum_consistency(sql, _RULE, semantic_trace=_TRACE_ENERGY)
    assert conflict is None


def test_type_name_pin_stops_at_subquery_boundary():
    sql = (
        "SELECT * FROM vehicle_params a "
        "WHERE a.type_name = '能源类型' AND a.type_value = '汽油' "
        "AND EXISTS (SELECT 1 FROM vehicle_params b "
        "WHERE b.car_name = a.car_name AND b.type_value = '5座')"
    )
    conflict = _detect_semantic_enum_consistency(sql, _RULE, semantic_trace=_TRACE_ENERGY)
    assert conflict is None


def test_table_qualified_column_skips_cte_alias():
    sql = (
        "WITH x AS (SELECT energy_type AS et FROM vehicle_model_base) "
        "SELECT b.et FROM other_table b WHERE b.et = '定制版'"
    )
    conflict = _detect_semantic_enum_consistency(sql, _RULE, semantic_trace=_TRACE_ENERGY)
    assert conflict is None


def test_end_to_end_through_detect_guardrail_conflicts():
    conflicts = detect_guardrail_conflicts(
        _DIESEL_CASE_SQL,
        source_name="insight_data",
        route=None,
        semantic_trace=_TRACE_ENERGY,
        rules=[_RULE],
    )
    assert conflicts
    assert conflicts[0].action == "rewrite"


def test_extraction_performance_on_nested_cte():
    sql = _DIESEL_CASE_SQL * 20
    started = time.perf_counter()
    _detect_semantic_enum_consistency(sql, _RULE, semantic_trace=_TRACE_ENERGY)
    elapsed = time.perf_counter() - started
    assert elapsed < 1.0


# 2026-07-25 incident: the rewritten, asset-compliant SQL was blocked because
# the CASE-derived label column (energy_group) was alias-mapped to the
# governed column, leaking '传统能源'/'新能源' into its literal set.
_INCIDENT_SQL = """
WITH model_energy AS (
  SELECT
    brand,
    serial_name,
    car_name,
    launch_date,
    launch_year,
    CASE
      WHEN energy_type IN ('汽油', '汽油+48V轻混系统', '油电混合', '汽油电驱', '汽油+24V轻混系统') THEN '传统能源'
      WHEN energy_type IN ('纯电', '插电混合', '增程式纯电动') THEN '新能源'
    END AS energy_group
  FROM vehicle_model_base
  WHERE launch_year BETWEEN 2021 AND 2026
    AND vehicle_level IS DISTINCT FROM '皮卡'
    AND launch_date IS NOT NULL
    AND energy_type IN (
      '汽油', '汽油+48V轻混系统', '油电混合', '汽油电驱', '汽油+24V轻混系统',
      '纯电', '插电混合', '增程式纯电动'
    )
),
events AS (
  SELECT DISTINCT
    brand,
    serial_name,
    launch_date,
    launch_year,
    energy_group
  FROM model_energy
  WHERE energy_group IS NOT NULL
)
SELECT
  launch_year,
  COUNT(*) FILTER (WHERE energy_group = '传统能源') AS traditional_events,
  COUNT(*) FILTER (WHERE energy_group = '新能源') AS new_energy_events
FROM events
GROUP BY launch_year
ORDER BY launch_year
"""


def test_case_label_alias_predicates_are_not_attributed_to_governed_column():
    conflict = _detect_semantic_enum_consistency(
        _INCIDENT_SQL,
        _RULE,
        semantic_trace=_TRACE_ENERGY,
        question="2021年到2026年，每年传统能源和新能源各有多少次上市更新事件？排除皮卡。",
    )
    assert conflict is None


def test_case_label_alias_with_diesel_is_still_rejected():
    # Same shape, but the 传统能源 arm smuggles diesel in: the alias fix must
    # not weaken classification checks on the CASE conditions themselves.
    sql = _INCIDENT_SQL.replace(
        "'汽油+24V轻混系统') THEN '传统能源'",
        "'汽油+24V轻混系统', '柴油', '柴油+48V轻混系统') THEN '传统能源'",
    )
    conflict = _detect_semantic_enum_consistency(
        sql,
        _RULE,
        semantic_trace=_TRACE_ENERGY,
        question="2021年到2026年，每年传统能源和新能源各有多少次上市更新事件？排除皮卡。",
    )
    assert conflict is not None
    assert "柴油" in conflict.message


# ---------------------------------------------------------------------------
# P2c: classification bypass — question names classifications but the SQL
# materializes none of the mapping (raw breakdown dodge, 2026-07-25).
# ---------------------------------------------------------------------------

_RAW_BREAKDOWN_SQL = """
SELECT launch_year, energy_type, COUNT(DISTINCT (brand, serial_name, launch_date)) AS update_count
FROM vehicle_model_base
WHERE launch_year BETWEEN 2021 AND 2026
  AND vehicle_level IS DISTINCT FROM '皮卡'
GROUP BY launch_year, energy_type
ORDER BY launch_year, energy_type
"""


def test_classification_bypass_via_raw_breakdown_is_rejected():
    conflict = _detect_semantic_enum_consistency(
        _RAW_BREAKDOWN_SQL,
        _RULE,
        semantic_trace=_TRACE_ENERGY,
        question="2021年到2026年，每年传统能源和新能源各有多少次上市更新事件？排除皮卡。",
    )
    assert conflict is not None
    assert "未物化任何分类结构" in conflict.message


def test_raw_breakdown_without_classification_question_escapes():
    # 用户就是要原始能源类型分布：未点名分类标签,不下移检测。
    conflict = _detect_semantic_enum_consistency(
        _RAW_BREAKDOWN_SQL,
        _RULE,
        semantic_trace=_TRACE_ENERGY,
        question="2021年到2026年，每年各能源类型的上市更新次数是多少？排除皮卡。",
    )
    assert conflict is None


def test_named_classification_materialized_as_filter_passes():
    sql = (
        "SELECT launch_year, COUNT(*) FROM vehicle_model_base "
        "WHERE energy_type IN ('汽油', '汽油+48V轻混系统', '油电混合', '汽油电驱', '汽油+24V轻混系统') "
        "GROUP BY launch_year"
    )
    conflict = _detect_semantic_enum_consistency(
        sql,
        _RULE,
        semantic_trace=_TRACE_ENERGY,
        question="2021年到2026年，每年传统能源有多少次上市更新事件？",
    )
    assert conflict is None


# ---------------------------------------------------------------------------
# P2a: question-channel enum caliber injection (question vs trusted user text)
# ---------------------------------------------------------------------------


def test_enum_caliber_injection_beyond_user_scope_detected():
    from tools.database.sql_generate_tool import _agent_added_enum_caliber

    findings = _agent_added_enum_caliber(
        question=(
            "每年传统能源和新能源各有多少次上市更新事件？"
            "传统能源包括汽油、汽油+48V轻混系统、油电混合、汽油电驱、汽油+24V轻混系统、"
            "柴油、柴油+48V轻混系统；新能源包括纯电、插电混合、增程式纯电动。"
        ),
        selected_asset_ids=["dimension:energy_type"],
        trusted_text="2021年到2026年，每年传统能源和新能源各有多少次上市更新事件？排除皮卡。",
    )
    assert "柴油" in findings
    assert "柴油+48V轻混系统" in findings
    assert "汽油" in findings


def test_enum_caliber_user_stated_values_pass():
    from tools.database.sql_generate_tool import _agent_added_enum_caliber

    findings = _agent_added_enum_caliber(
        question="传统能源（含柴油）的上市更新次数？",
        selected_asset_ids=["dimension:energy_type"],
        trusted_text="传统能源（含柴油）的上市更新次数？",
    )
    assert findings == []


def test_enum_caliber_classification_label_is_governed():
    from tools.database.sql_generate_tool import _agent_added_enum_caliber

    findings = _agent_added_enum_caliber(
        question="仅统计新能源车型",
        selected_asset_ids=["dimension:energy_type"],
        trusted_text="刷新产品配置报告",
    )

    assert "新能源" in findings


def test_enum_caliber_allows_only_server_routed_template_terms():
    from tools.database.sql_generate_tool import _agent_added_enum_caliber

    findings = _agent_added_enum_caliber(
        question="分别统计纯电、新能源、柴油和传统能源车型",
        selected_asset_ids=["dimension:energy_type"],
        trusted_text="刷新月报",
        authorized_terms_by_asset={
            "dimension:energy_type": {"纯电", "新能源"},
        },
    )

    assert "纯电" not in findings
    assert "新能源" not in findings
    assert "柴油" in findings
    assert "传统能源" in findings


def test_enum_caliber_without_trusted_text_escapes():
    from tools.database.sql_generate_tool import _agent_added_enum_caliber

    findings = _agent_added_enum_caliber(
        question="传统能源包括柴油的上市更新次数？",
        selected_asset_ids=["dimension:energy_type"],
        trusted_text="",
    )
    assert findings == []


# ---------------------------------------------------------------------------
# Asset-id references must not be misread as bare physical identifiers.
# ---------------------------------------------------------------------------


def test_asset_id_reference_is_not_flagged_as_physical_identifier(monkeypatch):
    import tools.database.sql_generate_tool as gen_tool

    monkeypatch.setattr(
        gen_tool, "_trusted_user_scope_text", lambda runtime: "每年传统能源和新能源各有多少次上市更新事件？"
    )
    findings = gen_tool._agent_added_physical_guidance(
        question="传统能源和新能源的分类以语义资产 dimension:energy_type 的定义为准。",
        table_names=[],
        runtime=None,
    )
    assert findings == []


def test_bare_column_name_is_still_flagged(monkeypatch):
    import tools.database.sql_generate_tool as gen_tool

    monkeypatch.setattr(
        gen_tool, "_trusted_user_scope_text", lambda runtime: "每年传统能源和新能源各有多少次上市更新事件？"
    )
    findings = gen_tool._agent_added_physical_guidance(
        question="按 energy_type 分组统计传统能源和新能源。",
        table_names=[],
        runtime=None,
    )
    assert "energy_type" in findings
