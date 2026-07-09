"""Regression tests for database query result completeness contracts."""

from __future__ import annotations

from analytics.nl2sql.service import DatabaseKnowledgeQueryError
from analytics.nl2sql.sql_runner import _compact_rows, _estimate_tokens, _profile_from_rows, extract_sql, validate_readonly_sql
from tools.database_knowledge_tool import _format_query_error


def test_profile_fixture_keeps_omitted_tengshi_group_visible() -> None:
    rows = [
        {"车型名称": f"比亚迪车型{i}", "品牌": "比亚迪", "上市日期": "2026-06-01", "价格": "10.00"}
        for i in range(20)
    ] + [
        {"车型名称": "腾势车型1", "品牌": "腾势", "上市日期": "2026-06-23", "价格": "31.98"},
        {"车型名称": "腾势车型2", "品牌": "腾势", "上市日期": "2026-06-23", "价格": "34.98"},
    ]
    columns = ["车型名称", "品牌", "上市日期", "价格"]

    profile = _profile_from_rows(rows, columns)

    assert profile["group_counts"]["品牌"] == {"比亚迪": 20, "腾势": 2}
    assert profile["date_ranges"]["上市日期"] == {"min": "2026-06-01", "max": "2026-06-23"}
    assert profile["numeric_ranges"]["价格"] == {"min": 10.0, "max": 34.98}


def test_budget_fixture_marks_preview_only_with_omitted_count() -> None:
    rows = [
        {"车型名称": f"比亚迪车型{i}", "品牌": "比亚迪", "上市日期": "2026-06-01", "价格": "10.00"}
        for i in range(20)
    ] + [
        {"车型名称": "腾势车型1", "品牌": "腾势", "上市日期": "2026-06-23", "价格": "31.98"},
        {"车型名称": "腾势车型2", "品牌": "腾势", "上市日期": "2026-06-23", "价格": "34.98"},
    ]
    columns = ["车型名称", "品牌", "上市日期", "价格"]

    compact_rows = _compact_rows(rows, columns, max_cell_chars=500)
    estimated_tokens = _estimate_tokens({"columns": columns, "rows": compact_rows})
    preview_rows = compact_rows[:20]
    omitted_count = len(rows) - len(preview_rows)

    assert estimated_tokens > 0
    assert len(preview_rows) == 20
    assert omitted_count == 2

    profile = _profile_from_rows(rows, columns)
    assert profile["group_counts"]["品牌"]["腾势"] == 2


def test_extract_sql_repairs_scalar_subquery_list() -> None:
    raw_sql = """
    (SELECT COUNT(DISTINCT car_name)
     FROM vehicle_params
     WHERE type_name = '上市时间'
       AND type_value LIKE '2026-%'
    ) AS total_count,
    (SELECT COUNT(DISTINCT v.car_name)
     FROM vehicle_params v
     WHERE v.type_name = '可调悬架种类'
       AND v.type_value LIKE '%空气悬架%'
    ) AS air_count
    """

    sql = extract_sql(raw_sql)

    assert sql.startswith("SELECT (SELECT COUNT")
    assert "AS total_count" in sql
    assert "AS air_count" in sql
    assert sql.count("(") == sql.count(")")


def test_extract_sql_keeps_normal_select_unchanged() -> None:
    raw_sql = "SELECT brand, COUNT(*) AS count FROM vehicle_params GROUP BY brand"

    assert extract_sql(raw_sql) == raw_sql


def test_validate_readonly_sql_does_not_treat_extract_from_to_date_as_table() -> None:
    sql = """
    SELECT EXTRACT(YEAR FROM to_date(type_value, 'YYYY-MM-DD')) AS launch_year,
           COUNT(DISTINCT car_name) AS car_count
    FROM vehicle_params
    WHERE type_name = '上市时间'
    GROUP BY launch_year
    """

    clean_sql = validate_readonly_sql(sql, allowed_tables=["vehicle_params"])

    assert "to_date" in clean_sql


def test_database_query_error_format_includes_generated_sql() -> None:
    sql = "SELECT COUNT(*) FROM vehicle_params"
    message = _format_query_error(DatabaseKnowledgeQueryError("SQL 引用了未授权数据表：foo", sql=sql))

    assert "数据库问数失败" in message
    assert "生成 SQL" in message
    assert sql in message
