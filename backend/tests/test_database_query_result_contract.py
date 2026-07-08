"""Regression tests for database query result completeness contracts."""

from __future__ import annotations

from analytics.nl2sql.sql_runner import _compact_rows, _estimate_tokens, _profile_from_rows


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
