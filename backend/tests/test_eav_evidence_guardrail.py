from __future__ import annotations

import hashlib
import json
import time
from typing import Any
from unittest.mock import AsyncMock

import pytest

from analytics.nl2sql import service as nl2sql_service
from analytics.nl2sql.eav_evidence import (
    EavEquivalenceBinding,
    bindings_from_semantic_trace,
    check_eav_evidence,
    eav_mapping_fingerprint,
    extract_eav_type_names,
    sql_business_fingerprint,
)
from analytics.nl2sql.schemas import DatabaseQueryRequest, TableRoute


def _route() -> TableRoute:
    return TableRoute(
        database_source_id="source-1",
        source_name="测试库",
        database="test",
        dialect="PostgreSQL",
        table_names=["vehicle_params"],
        available_tables=["vehicle_params"],
        candidates=[],
        confidence=1.0,
        reason="test",
        prompt_context="只允许 vehicle_params",
    )


def test_extracts_eav_literals_from_eq_reverse_eq_and_in() -> None:
    sql = """
    SELECT * FROM vehicle_params
    WHERE type_name = '电池电量[kWh]'
       OR '级别' = type_name
       OR type_name IN ('CLTC纯电续航[km]', 'CLTC纯电续航里程[km]')
    """
    assert extract_eav_type_names(sql) == {
        "电池电量[kWh]",
        "级别",
        "CLTC纯电续航[km]",
        "CLTC纯电续航里程[km]",
    }


def test_requires_live_existence_and_explicit_binding_completeness() -> None:
    binding = EavEquivalenceBinding(
        concept="cltc_range",
        type_names=("CLTC纯电续航[km]", "CLTC纯电续航里程[km]"),
    )
    sql = "SELECT * FROM vehicle_params WHERE type_name = 'CLTC纯电续航[km]'"
    result = check_eav_evidence(
        sql,
        live_type_names=binding.type_names,
        bindings=[binding],
    )
    assert not result.passed
    assert not result.unsupported
    assert result.incomplete_bindings == (binding,)


def test_multi_field_binding_rejects_sum_or_unordered_resolution() -> None:
    binding = EavEquivalenceBinding(
        concept="cltc_range",
        type_names=("CLTC纯电续航[km]", "CLTC纯电续航里程[km]"),
    )
    sql = """
    SELECT SUM(CASE
      WHEN type_name IN ('CLTC纯电续航[km]', 'CLTC纯电续航里程[km]')
      THEN type_value::numeric END)
    FROM vehicle_params
    """
    result = check_eav_evidence(sql, live_type_names=binding.type_names, bindings=[binding])
    assert not result.passed
    assert result.invalid_binding_resolutions == (binding,)


def test_eav_mapping_fingerprint_allows_only_physical_name_change() -> None:
    original = "SELECT * FROM vehicle_params WHERE type_name = '电池容量[kWh]' AND brand = '比亚迪'"
    repaired = "SELECT * FROM vehicle_params WHERE type_name = '电池电量[kWh]' AND brand = '比亚迪'"
    drifted = "SELECT * FROM vehicle_params WHERE type_name = '电池电量[kWh]' AND brand = '吉利银河'"
    group = [("电池容量[kWh]", "电池电量[kWh]")]
    assert eav_mapping_fingerprint(original, replacement_groups=group) == eav_mapping_fingerprint(
        repaired, replacement_groups=group
    )
    assert eav_mapping_fingerprint(original, replacement_groups=group) != eav_mapping_fingerprint(
        drifted, replacement_groups=group
    )
    binding = EavEquivalenceBinding(
        concept="battery_energy",
        type_names=("电池电量[kWh]",),
        aliases=("电池容量",),
    )
    assert eav_mapping_fingerprint(
        original, bindings=[binding], replacement_groups=group
    ) == eav_mapping_fingerprint(repaired, bindings=[binding], replacement_groups=group)


def test_eav_extractor_is_table_scoped_and_fails_closed_on_dynamic_predicates() -> None:
    sql = """
    SELECT * FROM vehicle_params vp
    JOIN other_table o ON o.id = vp.car_name
    WHERE o.type_name = 'not-eav'
      AND LOWER(vp.type_name) = 'dynamic'
    """
    result = check_eav_evidence(sql, live_type_names=[])
    assert not result.passed
    assert not result.used_type_names
    assert result.unprovable_predicates


def test_eav_extractor_resolves_aliases_per_union_scope_and_rejects_same_named_cte() -> None:
    union_sql = """
    SELECT vp.type_name FROM vehicle_params vp WHERE vp.type_name = '电池电量[kWh]'
    UNION ALL
    SELECT vp.type_name FROM other_table vp WHERE vp.type_name = '发动机型号'
    """
    assert extract_eav_type_names(union_sql) == {"电池电量[kWh]"}
    cte_sql = """
    WITH vehicle_params AS (SELECT type_name FROM other_table)
    SELECT * FROM vehicle_params WHERE type_name = '发动机型号'
    """
    assert extract_eav_type_names(cte_sql) == set()
    derived_eav_sql = """
    WITH vp AS (SELECT * FROM vehicle_params)
    SELECT * FROM vp WHERE type_name = '不存在字段'
    """
    result = check_eav_evidence(derived_eav_sql, live_type_names=[])
    assert not result.passed and result.unsupported == {"不存在字段"}
    union_derived_sql = """
    WITH vp AS (
      SELECT * FROM vehicle_params
      UNION ALL
      SELECT * FROM other_table
    )
    SELECT * FROM vp WHERE type_name = '不存在字段'
    """
    result = check_eav_evidence(union_derived_sql, live_type_names=[])
    assert not result.passed and result.unsupported == {"不存在字段"}


def test_vanna_alias_authority_does_not_treat_related_words_as_equivalent() -> None:
    entities = [{
        "canonical_name": "电池电量[kWh]",
        "aliases": ["电池容量"],
        "table_column": "public.vehicle_params.type_name",
    }]
    pairs = nl2sql_service._entity_authorized_replacement_pairs(
        {"电池容量[kWh]", "电池类型", "电池预加热"},
        entities,
    )
    assert pairs == {("电池容量[kWh]", "电池电量[kWh]")}
    unit_pairs = nl2sql_service._entity_authorized_replacement_pairs(
        {"CLTC续航[miles]", "CLTC续航[km]"},
        [{
            "canonical_name": "CLTC纯电续航[km]",
            "aliases": ["CLTC续航"],
            "table_column": "public.vehicle_params.type_name",
        }],
    )
    assert unit_pairs == {("CLTC续航[km]", "CLTC纯电续航[km]")}


def test_fuzzy_extra_assets_do_not_impose_unrelated_eav_bindings() -> None:
    trace = {
        "resolution_mode": "model_scoped_fuzzy",
        "matched": [
            {
                "id": "dimension:battery_energy",
                "frontmatter": {
                    "name": "电池电量",
                    "aliases": ["电池容量"],
                    "resolution": {
                        "bindings": [{"fields": {"type_name": "电池电量[kWh]"}}]
                    },
                },
            },
            {
                "id": "dimension:cltc_range",
                "frontmatter": {
                    "name": "CLTC纯电续航",
                    "aliases": ["CLTC续航"],
                    "eav_equivalence": [{
                        "concept": "cltc_range",
                        "type_names": ["CLTC纯电续航[km]", "CLTC纯电续航里程[km]"],
                    }],
                },
            },
        ],
    }
    bindings = bindings_from_semantic_trace(trace, question="查询车型电池容量")
    assert [item.concept for item in bindings] == ["dimension:battery_energy"]


def test_cltc_resolution_requires_reachable_type_value_lineage() -> None:
    binding = EavEquivalenceBinding(
        concept="cltc_range",
        type_names=("CLTC纯电续航[km]", "CLTC纯电续航里程[km]"),
    )
    price_sql = """
    SELECT COALESCE(
      MAX(CASE WHEN type_name = 'CLTC纯电续航[km]' THEN price END),
      MAX(CASE WHEN type_name = 'CLTC纯电续航里程[km]' THEN price END)
    ) FROM vehicle_params
    """
    result = check_eav_evidence(price_sql, live_type_names=binding.type_names, bindings=[binding])
    assert not result.passed and result.invalid_binding_resolutions == (binding,)

    disguised_price_sql = price_sql.replace("price END", "price + 0 * type_value::numeric END")
    result = check_eav_evidence(
        disguised_price_sql, live_type_names=binding.type_names, bindings=[binding]
    )
    assert not result.passed and result.invalid_binding_resolutions == (binding,)

    rowwise_sql = """
    SELECT COALESCE(
      CASE WHEN type_name = 'CLTC纯电续航[km]' THEN type_value END,
      CASE WHEN type_name = 'CLTC纯电续航里程[km]' THEN type_value END
    ) FROM vehicle_params
    """
    result = check_eav_evidence(rowwise_sql, live_type_names=binding.type_names, bindings=[binding])
    assert not result.passed and result.invalid_binding_resolutions == (binding,)

    extra_max_sql = """
    SELECT COALESCE(
      MAX(CASE WHEN type_name = 'CLTC纯电续航[km]' THEN type_value END) + MAX(price),
      MAX(CASE WHEN type_name = 'CLTC纯电续航里程[km]' THEN type_value END)
    ) FROM vehicle_params
    """
    result = check_eav_evidence(extra_max_sql, live_type_names=binding.type_names, bindings=[binding])
    assert not result.passed and result.invalid_binding_resolutions == (binding,)

    mixed_condition_sql = """
    SELECT COALESCE(
      MAX(CASE WHEN type_name = 'CLTC纯电续航[km]' OR type_name = 'CLTC纯电续航里程[km]'
        THEN type_value END),
      MAX(CASE WHEN type_name = 'CLTC纯电续航里程[km]' THEN type_value END)
    ) FROM vehicle_params
    """
    result = check_eav_evidence(
        mixed_condition_sql, live_type_names=binding.type_names, bindings=[binding]
    )
    assert not result.passed and result.invalid_binding_resolutions == (binding,)

    dead_cte_sql = """
    WITH dead AS (
      SELECT COALESCE(
        MAX(CASE WHEN type_name = 'CLTC纯电续航[km]' THEN type_value END),
        MAX(CASE WHEN type_name = 'CLTC纯电续航里程[km]' THEN type_value END)
      ) AS cltc FROM vehicle_params
    )
    SELECT SUM(type_value::numeric) FROM vehicle_params
    """
    result = check_eav_evidence(dead_cte_sql, live_type_names=binding.type_names, bindings=[binding])
    assert not result.passed and result.incomplete_bindings == (binding,)


def test_business_fingerprint_allows_cte_rewrite_but_rejects_semantic_drift() -> None:
    parent = """
    SELECT serial_name, COUNT(*) AS models
    FROM vehicle_params WHERE brand = '比亚迪'
    GROUP BY serial_name ORDER BY serial_name LIMIT 20
    """
    rewritten = """
    WITH filtered AS (SELECT * FROM vehicle_params)
    SELECT serial_name, COUNT(*) AS models
    FROM filtered WHERE brand = '比亚迪'
    GROUP BY serial_name ORDER BY serial_name LIMIT 20
    """
    drifted = rewritten.replace("比亚迪", "吉利")
    assert sql_business_fingerprint(parent) == sql_business_fingerprint(rewritten)
    assert sql_business_fingerprint(parent) != sql_business_fingerprint(drifted)

    sum_price = "SELECT SUM(price) AS total FROM vehicle_params WHERE brand='比亚迪'"
    sum_value = "SELECT SUM(type_value::numeric) AS total FROM vehicle_params WHERE brand='比亚迪'"
    assert sql_business_fingerprint(sum_price) != sql_business_fingerprint(sum_value)
    and_filter = "SELECT COUNT(*) FROM vehicle_params WHERE brand='比亚迪' AND serial_name='汉'"
    or_filter = and_filter.replace(" AND ", " OR ")
    assert sql_business_fingerprint(and_filter) != sql_business_fingerprint(or_filter)
    left_join = (
        "SELECT COUNT(*) FROM vehicle_params a LEFT JOIN vehicle_params b "
        "ON a.car_name=b.car_name"
    )
    inner_join = left_join.replace("LEFT JOIN", "INNER JOIN")
    assert sql_business_fingerprint(left_join) != sql_business_fingerprint(inner_join)
    qualified_sum_a = "SELECT SUM(a.price) FROM vehicle_params a JOIN vehicle_params b ON a.car_name=b.car_name"
    qualified_sum_b = qualified_sum_a.replace("SUM(a.price)", "SUM(b.price)")
    assert sql_business_fingerprint(qualified_sum_a) != sql_business_fingerprint(qualified_sum_b)
    union = "SELECT car_name FROM vehicle_params UNION SELECT car_name FROM vehicle_params"
    union_all = union.replace(" UNION SELECT", " UNION ALL SELECT")
    assert sql_business_fingerprint(union) != sql_business_fingerprint(union_all)
    offset_0 = "SELECT car_name FROM vehicle_params ORDER BY car_name OFFSET 0"
    offset_100 = offset_0.replace("OFFSET 0", "OFFSET 100")
    assert sql_business_fingerprint(offset_0) != sql_business_fingerprint(offset_100)
    source_a = "SELECT COUNT(*) FROM vehicle_params WHERE brand='A'"
    source_b = source_a.replace("vehicle_params", "vehicle_model_base")
    assert sql_business_fingerprint(source_a) != sql_business_fingerprint(source_b)
    union_filters = """
    SELECT car_name FROM vehicle_params WHERE brand='A'
    UNION ALL
    SELECT serial_name FROM vehicle_model_base WHERE brand='B'
    """
    swapped_filters = union_filters.replace("brand='A'", "brand='X'").replace(
        "brand='B'", "brand='A'"
    ).replace("brand='X'", "brand='B'")
    assert sql_business_fingerprint(union_filters) != sql_business_fingerprint(swapped_filters)


@pytest.mark.asyncio
async def test_top_k_miss_is_enriched_from_live_catalog_before_execution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    question = "查询车型电池容量"

    class FakeVanna:
        @staticmethod
        def get_all_entities() -> list[dict[str, Any]]:
            return [{"entity_type": "配置名称", "table_column": "public.vehicle_params.type_name"}]

        @staticmethod
        def get_related_entities(query: str, **_kwargs: Any) -> list[dict[str, Any]]:
            if "电池" not in query:
                return []
            return [{
                "entity_type": "配置名称",
                "canonical_name": "电池电量[kWh]",
                "aliases": ["电池容量"],
                "table_column": "public.vehicle_params.type_name",
            }]

        @staticmethod
        def get_related_ddl(*_args: Any, **_kwargs: Any) -> list[str]:
            return ["CREATE TABLE vehicle_params (type_name text, type_value text)"]

        @staticmethod
        def get_related_documentation(*_args: Any, **_kwargs: Any) -> list[str]:
            return []

        @staticmethod
        def get_similar_question_sql(*_args: Any, **_kwargs: Any) -> list[dict[str, str]]:
            return []

        @staticmethod
        def generate_sql(**_kwargs: Any) -> str:
            return "SELECT * FROM vehicle_params WHERE type_name = '电池容量[kWh]'"

        @staticmethod
        def submit_prompt(prompt: list[dict[str, str]], **_kwargs: Any) -> str:
            assert "电池电量[kWh]" in prompt[1]["content"]
            assert "conflict_model_count" in prompt[1]["content"]
            return "SELECT * FROM vehicle_params WHERE type_name = '电池电量[kWh]'"

    async def fake_inspect(**kwargs: Any) -> list[dict[str, Any]]:
        requested_names = kwargs["requested_names"]
        if "电池电量[kWh]" in requested_names:
            return [{"type_name": "电池电量[kWh]", "count": 9748}]
        # The post-candidate pass may re-check the model's alias.  The live
        # catalog is exact-match evidence, so an alias absent from the DB
        # returns no row; the already observed canonical name remains valid.
        return []

    monkeypatch.setattr(nl2sql_service, "_inspect_live_eav_type_names", fake_inspect)
    monkeypatch.setattr(
        nl2sql_service,
        "_inspect_live_eav_value_profiles",
        AsyncMock(
            return_value=[
                {
                    "type_name": "电池电量[kWh]",
                    "distinct_value_count": 12,
                    "conflict_model_count": 3,
                    "top_values": [{"value": "100", "row_count": 20, "model_count": 20}],
                }
            ]
        ),
    )
    sql, references, _note, generation = await nl2sql_service._generate_grounded_sql(
        request=DatabaseQueryRequest(question=question),
        route=_route(),
        semantic_context="",
        semantic_trace={},
        vanna=FakeVanna(),
        stage_timings={},
        source={"id": "source-1"},
    )

    assert "type_name = '电池电量[kWh]'" in sql
    assert generation["eav_evidence"]["automatic_repair"] is True
    assert generation["eav_evidence"]["complete"] is True
    assert references["eav_live_inspection"]["source"] == "live_database"
    assert generation["eav_value_profiles"]["items"][0]["conflict_model_count"] == 3


@pytest.mark.asyncio
async def test_explicit_cltc_binding_requires_all_physical_names(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    names = ["CLTC纯电续航[km]", "CLTC纯电续航里程[km]"]
    prompt_calls = 0

    class FakeVanna:
        @staticmethod
        def get_all_entities() -> list[dict[str, Any]]:
            return [{"entity_type": "配置名称", "table_column": "public.vehicle_params.type_name"}]

        @staticmethod
        def get_related_entities(*_args: Any, **_kwargs: Any) -> list[dict[str, Any]]:
            return []

        get_related_ddl = staticmethod(lambda *_args, **_kwargs: [])
        get_related_documentation = staticmethod(lambda *_args, **_kwargs: [])
        get_similar_question_sql = staticmethod(lambda *_args, **_kwargs: [])
        generate_sql = staticmethod(lambda **_kwargs: (
            "SELECT MAX(CASE WHEN type_name = 'CLTC纯电续航[km]' "
            "THEN type_value END) AS cltc FROM vehicle_params"
        ))

        @staticmethod
        def submit_prompt(prompt: list[dict[str, str]], **_kwargs: Any) -> str:
            nonlocal prompt_calls
            prompt_calls += 1
            if prompt_calls == 1:
                return (
                    "SELECT MAX(CASE WHEN type_name = 'CLTC纯电续航[km]' "
                    "THEN type_value END) AS cltc FROM vehicle_params"
                )
            assert "cltc_range" in prompt[1]["content"]
            return (
                "SELECT COALESCE("
                "MAX(CASE WHEN type_name = 'CLTC纯电续航[km]' THEN type_value END), "
                "MAX(CASE WHEN type_name = 'CLTC纯电续航里程[km]' THEN type_value END)"
                ") AS cltc FROM vehicle_params"
            )

    async def fake_inspect(**_kwargs: Any) -> list[dict[str, Any]]:
        return [{"type_name": name, "count": 1} for name in names]

    monkeypatch.setattr(nl2sql_service, "_inspect_live_eav_type_names", fake_inspect)
    monkeypatch.setattr(
        nl2sql_service,
        "_inspect_live_eav_value_profiles",
        AsyncMock(return_value=[]),
    )
    semantic_trace = {
        "matched": [
            {
                "id": "dimension:cltc_range",
                "frontmatter": {
                    "resolution": {
                        "eav_equivalence": [
                            {"concept": "cltc_range", "type_names": names, "match": "any"}
                        ]
                    }
                },
            }
        ],
        "references": [],
    }
    sql, _references, _note, generation = await nl2sql_service._generate_grounded_sql(
        request=DatabaseQueryRequest(question="CLTC续航"),
        route=_route(),
        semantic_context="",
        semantic_trace=semantic_trace,
        vanna=FakeVanna(),
        stage_timings={},
        source={"id": "source-1"},
    )
    assert all(name in sql for name in names)
    assert generation["eav_evidence"]["complete"] is True


@pytest.mark.asyncio
async def test_schema_receipt_repairs_exact_binding_without_business_drift(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parent_sql = (
        "SELECT MAX(CASE WHEN type_name = '电池容量[kWh]' THEN type_value END) "
        "AS battery FROM vehicle_params"
    )
    repaired_sql = parent_sql.replace("电池容量[kWh]", "电池电量[kWh]")
    evidence = {
        "database_source_id": "source-1",
        "table_name": "public.vehicle_params",
        "mode": "type_names",
        "search": "电池",
        "rows": [{"type_name": "电池电量[kWh]", "count": 10}],
        "parent_sql_sha256": f"sha256:{hashlib.sha256(parent_sql.encode()).hexdigest()}",
        "parent_type_names": ["电池容量[kWh]"],
    }
    digest = hashlib.sha256(
        json.dumps(evidence, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    receipt = {
        "id": "schema-evidence-test",
        "expires_at": time.time() + 60,
        "evidence": evidence,
        "sha256": f"sha256:{digest}",
    }
    prompt_calls = 0

    class FakeVanna:
        get_all_entities = staticmethod(lambda: [
            {"entity_type": "配置名称", "table_column": "public.vehicle_params.type_name"}
        ])

        @staticmethod
        def get_related_entities(*_args: Any, **_kwargs: Any) -> list[dict[str, Any]]:
            return [{
                "entity_type": "配置名称",
                "canonical_name": "电池电量[kWh]",
                "aliases": ["电池容量"],
                "table_column": "public.vehicle_params.type_name",
            }]

        get_related_ddl = staticmethod(lambda *_args, **_kwargs: [])
        get_related_documentation = staticmethod(lambda *_args, **_kwargs: [])
        get_similar_question_sql = staticmethod(lambda *_args, **_kwargs: [])
        generate_sql = staticmethod(lambda **_kwargs: "SELECT 1")

        @staticmethod
        def submit_prompt(*_args: Any, **_kwargs: Any) -> str:
            nonlocal prompt_calls
            prompt_calls += 1
            return parent_sql if prompt_calls == 1 else repaired_sql

    async def fake_inspect(**_kwargs: Any) -> list[dict[str, Any]]:
        return [{"type_name": "电池电量[kWh]", "count": 10}]

    monkeypatch.setattr(nl2sql_service, "_inspect_live_eav_type_names", fake_inspect)
    monkeypatch.setattr(
        nl2sql_service,
        "_inspect_live_eav_value_profiles",
        AsyncMock(return_value=[]),
    )
    semantic_trace = {
        "resolution_mode": "selected_ids",
        "matched": [{
            "id": "dimension:battery_energy",
            "frontmatter": {
                "name": "电池电量",
                "aliases": ["电池容量"],
                "resolution": {
                    "bindings": [{"fields": {"type_name": "电池电量[kWh]"}}]
                },
            },
        }],
    }
    sql, _refs, _note, generation = await nl2sql_service._generate_grounded_sql(
        request=DatabaseQueryRequest(
            question="查询车型电池容量",
            technical_evidence={
                "kind": "schema_evidence",
                "observed_problem_category": "schema_physical_mapping_mismatch",
                "parent_sql": parent_sql,
                "schema_receipt": receipt,
            },
        ),
        route=_route(),
        semantic_context="电池容量映射到电池电量[kWh]",
        semantic_trace=semantic_trace,
        vanna=FakeVanna(),
        stage_timings={},
        source={"id": "source-1"},
    )
    assert sql == repaired_sql
    assert generation["eav_evidence"]["complete"] is True


@pytest.mark.asyncio
async def test_observed_technical_repair_allows_cte_rewrite_but_blocks_business_drift() -> None:
    parent_sql = """
    SELECT serial_name, COUNT(*) AS models
    FROM vehicle_params WHERE brand = '比亚迪'
    GROUP BY serial_name ORDER BY serial_name LIMIT 20
    """
    rewritten = """
    WITH filtered AS (SELECT * FROM vehicle_params)
    SELECT serial_name, COUNT(*) AS models FROM filtered WHERE brand = '比亚迪'
    GROUP BY serial_name ORDER BY serial_name LIMIT 20
    """

    class FakeVanna:
        get_all_entities = staticmethod(lambda: [])
        get_related_entities = staticmethod(lambda *_args, **_kwargs: [])
        get_related_ddl = staticmethod(lambda *_args, **_kwargs: [])
        get_related_documentation = staticmethod(lambda *_args, **_kwargs: [])
        get_similar_question_sql = staticmethod(lambda *_args, **_kwargs: [])
        generate_sql = staticmethod(lambda **_kwargs: "SELECT 1")

        def __init__(self, final_sql: str) -> None:
            self.final_sql = final_sql
            self.calls = 0

        def submit_prompt(self, *_args: Any, **_kwargs: Any) -> str:
            self.calls += 1
            return parent_sql if self.calls == 1 else self.final_sql

    request = DatabaseQueryRequest(
        question="查询比亚迪各车系款型数",
        technical_evidence={
            "kind": "observed_sql_failure",
            "observed_problem_category": "performance_or_timeout",
            "parent_sql": parent_sql,
        },
    )
    sql, *_rest = await nl2sql_service._generate_grounded_sql(
        request=request,
        route=_route(),
        semantic_context="",
        semantic_trace={},
        vanna=FakeVanna(rewritten),
        stage_timings={},
    )
    assert "WITH filtered" in sql

    with pytest.raises(nl2sql_service.DatabaseKnowledgeQueryError, match="不变量"):
        await nl2sql_service._generate_grounded_sql(
            request=request,
            route=_route(),
            semantic_context="",
            semantic_trace={},
            vanna=FakeVanna(rewritten.replace("比亚迪", "吉利")),
            stage_timings={},
        )
