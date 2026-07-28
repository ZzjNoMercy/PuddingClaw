from __future__ import annotations

import pytest

from analytics.nl2sql.guardrail_runtime import detector_failed, validate_rules
from analytics.nl2sql.guardrails import DETECTORS, GuardrailRule


@pytest.mark.parametrize(
    ("rule", "sql"),
    [
        ({"id": "p", "name": "p", "type": "forbid_sql_pattern", "params": {"pattern": "DROP"}}, "DROP TABLE t"),
        (
            {"id": "c", "name": "c", "type": "require_sql_contains", "params": {"contains": "brand"}},
            "SELECT sales FROM t",
        ),
        (
            {
                "id": "t",
                "name": "t",
                "type": "require_table_when_available",
                "params": {"required_table": "base", "fallback_table": "eav"},
            },
            "SELECT * FROM eav",
        ),
        (
            {"id": "g", "name": "g", "type": "require_group_by", "params": {"forbidden_columns_only": ["car_name"]}},
            "SELECT count(*) FROM t GROUP BY car_name",
        ),
        (
            {
                "id": "e",
                "name": "e",
                "type": "forbid_exists_distinct_pattern",
                "params": {"table": "eav", "distinct_column": "car_name", "min_exists_count": 2},
            },
            "SELECT count(distinct x) FROM eav WHERE EXISTS (SELECT DISTINCT car_name FROM eav) AND NOT EXISTS (SELECT 1 FROM eav)",
        ),
    ],
)
def test_platform_detectors_share_the_portable_runtime(rule: dict, sql: str) -> None:
    payload = {
        "enabled": True,
        "scope": {},
        "action": {"type": "block", "message": "blocked"},
        **rule,
    }
    platform_rule = GuardrailRule.model_validate(payload)
    assert detector_failed(sql, payload) is True
    assert DETECTORS[platform_rule.type](sql, platform_rule) is not None


def test_portable_runtime_requires_scope_context_and_warn_is_non_blocking() -> None:
    scoped = {
        "id": "scoped",
        "name": "scoped",
        "enabled": True,
        "type": "require_sql_contains",
        "scope": {"semantic_assets": ["measure:sales"], "intent_any": ["销量"]},
        "params": {"contains": "brand"},
        "action": {"type": "block", "message": "missing brand"},
    }
    missing_context = validate_rules("SELECT sales FROM t", [scoped])
    assert missing_context["passed"] is False
    assert missing_context["not_evaluated"][0]["reason"] == "scope"

    warning = {**scoped, "scope": {}, "action": {"type": "warn", "message": "missing brand"}}
    result = validate_rules("SELECT sales FROM t", [warning])
    assert result["passed"] is True
    assert result["warnings"][0]["id"] == "scoped"
