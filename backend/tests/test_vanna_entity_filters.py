from __future__ import annotations

import pytest

from analytics.nl2sql.training import (
    VannaTrainingError,
    _entity_filter_where_sql,
    _normalize_entity_filters,
)


def test_entity_filters_normalize_selected_values_and_build_bound_sql() -> None:
    filters = _normalize_entity_filters(
        [
            {
                "column": " category ",
                "operator": "not_in",
                "values": [" 选配包 ", "选配包", "特色配置"],
            },
            {
                "column": "brand",
                "operator": "in",
                "values": ["O'Reilly') OR 1=1 --"],
            },
        ],
        available_columns={"type_name", "category", "brand"},
    )

    assert filters == [
        {"column": "category", "operator": "not_in", "values": ["选配包", "特色配置"]},
        {"column": "brand", "operator": "in", "values": ["O'Reilly') OR 1=1 --"]},
    ]

    where_sql, params = _entity_filter_where_sql("type_name", filters)

    assert '"type_name" IS NOT NULL' in where_sql
    assert '"category" IS NULL OR BTRIM("category"::text) NOT IN' in where_sql
    assert 'BTRIM("brand"::text) IN' in where_sql
    assert "O'Reilly" not in where_sql
    assert params == {
        "filter_0_value_0": "选配包",
        "filter_0_value_1": "特色配置",
        "filter_1_value_0": "O'Reilly') OR 1=1 --",
    }


@pytest.mark.parametrize(
    ("raw_filter", "message"),
    [
        ({"column": "missing", "operator": "in", "values": ["x"]}, "筛选字段不存在"),
        ({"column": "category", "operator": "contains", "values": ["x"]}, "不支持的筛选操作符"),
        ({"column": "category", "operator": "in", "values": []}, "没有选择已有值"),
    ],
)
def test_entity_filters_reject_untrusted_shape(raw_filter: dict[str, object], message: str) -> None:
    with pytest.raises(VannaTrainingError, match=message):
        _normalize_entity_filters([raw_filter], available_columns={"type_name", "category"})

