"""Regression tests for database query result completeness contracts."""

from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import analytics.nl2sql.result_store as result_store_module
import analytics.nl2sql.service as nl2sql_service
import tools.database.result_source_tool as result_source_tool_module
import tools.database.sql_execute_tool as sql_execute_module
from analytics.nl2sql.schemas import (
    DatabaseQueryRequest,
    DatabaseSqlGenerationResult,
    SqlExecutionResult,
    TableRoute,
)
from analytics.nl2sql.service import DatabaseKnowledgeQueryError
from analytics.nl2sql.sql_runner import (
    SqlRunnerError,
    _compact_rows,
    _estimate_tokens,
    _profile_from_rows,
    _referenced_tables,
    _trim_profile_to_token_budget,
    extract_sql,
    validate_readonly_sql,
)
from analytics.semantic_assets.resolver import format_semantic_assets_for_prompt
from graph.database_sql_revision_resume import database_sql_revision_resume_registry
from knowledge.models import AnalyticsQueryResult, Base, utcnow
from tools.database.formatting import format_actions
from tools.database.result_source_tool import DatabaseQueryResultSourceTool
from tools.database.sql_execute_tool import DatabaseSqlExecuteTool
from tools.database_knowledge_tool import _format_query_error


@pytest.fixture
def user_semantic_definitions(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Build user-owned semantic fixtures without restoring package assets."""

    home = tmp_path / "puddingclaw-home"
    definitions = home / "definitions"
    model = definitions / "analytics-models" / "产品配置分析" / "model.md"
    measure = definitions / "semantic-assets" / "measures" / "config_rate" / "measure.md"
    reference = measure.parent / "references" / "air_suspension.md"
    price_band = definitions / "semantic-assets" / "dimensions" / "price_band" / "dimension.md"
    for target in (model, measure, reference, price_band):
        target.parent.mkdir(parents=True, exist_ok=True)
    model.write_text(
        """---
formatter: analytics-model
id: 产品配置分析
name: 产品配置分析
semantic_assets:
  measures: [measure:config_rate]
  dimensions: [dimension:price_band]
---
# 产品配置分析

## 默认分析范围

- 默认分析中国狭义乘用车。
- 用户未明确要求包含皮卡时，必须排除车型级别为 `皮卡` 的车型。
- 回退到 EAV 表时读取 `type_name = '级别'`。
""",
        encoding="utf-8",
    )
    measure.write_text(
        """---
formatter: semantic-asset
name: 配置率
type: measure
description: 统计目标配置的配备率。
aliases: [搭载率, 配备率]
---
# 配置率

按目标统计对象计算配置率。
""",
        encoding="utf-8",
    )
    reference.write_text(
        """# 空气悬架配置率口径

适用于空气悬架、空悬和空气悬架配置率；使用 `type_name = '可调悬架种类'`。
""",
        encoding="utf-8",
    )
    price_band.write_text(
        """---
formatter: semantic-asset
name: 价格段
type: dimension
aliases: [价格区间, 价位]
---
# 价格段

- 5万元以下
- 5-10万元
- 10-15万元
- 15-20万元
- 20-30万元
- 30-40万元
- 40-50万元
- 50万元以上
- 未定价

`未定价` 只包含 `price IS NULL OR price <= 0`。

```sql
CASE
  WHEN price IS NULL OR price <= 0 THEN '未定价'
  WHEN price < 5 THEN '5万元以下'
  WHEN price < 10 THEN '5-10万元'
  WHEN price < 15 THEN '10-15万元'
  WHEN price < 20 THEN '15-20万元'
  WHEN price < 30 THEN '20-30万元'
  WHEN price < 40 THEN '30-40万元'
  WHEN price < 50 THEN '40-50万元'
  ELSE '50万元以上'
END
```
""",
        encoding="utf-8",
    )
    monkeypatch.setenv("PUDDINGCLAW_HOME", str(home))
    return definitions


def test_sql_generator_loads_selected_analytics_model_body(monkeypatch) -> None:
    class _Registry:
        @staticmethod
        def get_model_context(model_id: str) -> dict[str, Any]:
            assert model_id == "产品配置分析"
            return {
                "id": model_id,
                "name": model_id,
                "version": "0.2.0",
                "path": "analytics-models/产品配置分析/model.md",
                "frontmatter": {"data_assets": {"tables": ["db.vehicle_params"]}},
                "body": "## 默认分析范围\n\n- 默认排除车型级别为 `皮卡` 的车型。",
            }

    monkeypatch.setattr(nl2sql_service, "get_analytics_model_registry", lambda: _Registry())

    prompt, trace = nl2sql_service._format_analytics_model_for_sql_prompt("产品配置分析")

    assert "默认排除车型级别为 `皮卡`" in prompt
    assert "用户明确要求 > 具体 Measure/Reference > 模型全局规则" in prompt
    assert trace["id"] == "产品配置分析"
    assert trace["path"] == "analytics-models/产品配置分析/model.md"


def test_product_configuration_model_declares_default_pickup_exclusion(
    user_semantic_definitions: Path,
) -> None:
    prompt, _ = nl2sql_service._format_analytics_model_for_sql_prompt("产品配置分析")

    assert "默认分析中国狭义乘用车" in prompt
    assert "必须排除车型级别为 `皮卡`" in prompt
    assert "type_name = '级别'" in prompt


def test_price_band_semantic_asset_is_exhaustive_and_keeps_unpriced_separate(
    user_semantic_definitions: Path,
) -> None:
    asset = (
        user_semantic_definitions
        / "semantic-assets"
        / "dimensions"
        / "price_band"
        / "dimension.md"
    ).read_text(encoding="utf-8")

    assert "5万元以下" in asset
    assert "50万元以上" in asset
    assert "50万以下" not in asset
    assert "`未定价` 只包含 `price IS NULL OR price <= 0`" in asset
    assert "WHEN price < 5 THEN '5万元以下'" in asset


@pytest.mark.asyncio
async def test_result_source_rejects_generation_id_with_actionable_guidance() -> None:
    result = await DatabaseQueryResultSourceTool(session_id="source-adapter-session")._arun(
        result_id="sql-gen-not-a-result",
        runtime=SimpleNamespace(context={"session_id": "source-adapter-session"}),
    )

    payload = json.loads(result)
    assert payload["status"] == "error"
    assert payload["error_code"] == "invalid_result_id_format"
    assert payload["retry_same_result_id"] is False
    assert "qr_*" in payload["error"]
    assert "generation_id" in payload["error"]


@pytest.mark.asyncio
async def test_result_store_row_cap_failure_is_actionable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        result_store_module,
        "get_database_qa_config",
        lambda: {
            "result_store_enabled": True,
            "result_materialization_row_cap": 5000,
        },
    )
    execution = SqlExecutionResult(
        columns=["id"],
        rows=[{"id": 1}],
        row_count=5905,
        limited=True,
        total_row_count=5905,
        preview_count=1,
        omitted_count=5904,
        is_complete=False,
        materialized_rows=[{"id": 1}],
        materialized_all=False,
    )

    persisted = await result_store_module.attach_persisted_query_result(
        SimpleNamespace(),
        execution,
        question="大明细",
        sql="SELECT id FROM vehicle_params",
    )

    assert persisted is False
    assert execution.result_id is None
    assert execution.actions == [
        {
            "type": "fetch_page",
            "available": False,
            "reason": "result_exceeds_materialization_row_cap",
            "row_count": 5905,
            "materialization_row_cap": 5000,
            "next_action": (
                "narrow_or_aggregate_the_query_or_raise_result_materialization_row_cap_then_rerun_database_query"
            ),
        }
    ]
    guidance = "\n".join(format_actions(execution.actions))
    assert "超过持久化上限 5000 行" in guidance
    assert "未生成 result_id" in guidance
    assert "重新执行数据库查询" in guidance


def test_semantic_resolution_uses_strict_match_then_generalizes_on_real_miss(
    user_semantic_definitions: Path,
) -> None:
    matched = nl2sql_service._resolve_request_semantic_assets(
        DatabaseQueryRequest(
            question="按价格段统计空气悬架配置率",
            model_id="产品配置分析",
        )
    )
    assert matched["resolution_mode"] == "model_scoped_fuzzy"
    assert any(item.id == "measure:config_rate" for item in matched["matched"])
    assert any("air_suspension" in item.id for item in matched["references"])

    generalized = nl2sql_service._resolve_request_semantic_assets(
        DatabaseQueryRequest(
            question="统计杯架氛围灯开关颜色组合",
            model_id="产品配置分析",
        )
    )
    assert generalized["resolution_mode"] == "generalized"
    assert generalized["matched"] == []
    prompt = format_semantic_assets_for_prompt(generalized)
    assert "这不是失败条件" in prompt
    assert "泛化 SQL" in prompt


def test_explicit_semantic_asset_selection_remains_authoritative(
    user_semantic_definitions: Path,
) -> None:
    selected = nl2sql_service._resolve_request_semantic_assets(
        DatabaseQueryRequest(
            question="统计配置情况",
            model_id="产品配置分析",
            measure_ids=["measure:config_rate"],
        )
    )
    assert selected["resolution_mode"] == "selected_ids"
    assert [item.id for item in selected["matched"]] == ["measure:config_rate"]


def test_explicit_semantic_asset_cannot_cross_selected_model(monkeypatch) -> None:
    class _Registry:
        @staticmethod
        def get_model_context(_model_id: str) -> dict[str, Any]:
            return {"semantic_assets": [{"id": "measure:other"}]}

    monkeypatch.setattr(nl2sql_service, "get_analytics_model_registry", lambda: _Registry())
    selected = nl2sql_service._resolve_request_semantic_assets(
        DatabaseQueryRequest(
            question="统计配置率",
            model_id="model-without-config-rate",
            measure_ids=["measure:config_rate"],
        )
    )

    assert selected["matched"] == []
    assert selected["unmatched_requested_ids"] == ["measure:config_rate"]


@pytest.mark.asyncio
async def test_grounded_sql_keeps_long_l2_question_raw_for_vanna_retrieval() -> None:
    question = (
        "查询2020年到2026年每年L2级及以上智能驾驶辅助系统的车系维度和款型维度配备率。"
        "同时查询每年各价格段（10万以下、10-15万、15-20万、20-30万、30万以上）的"
        "款型维度L2+配备率。排除皮卡车型。"
    )
    calls: list[tuple[str, str]] = []

    class _FakeVanna:
        config = {"model": "fake-model"}

        @staticmethod
        def get_all_entities() -> list[dict[str, str]]:
            return [
                {
                    "entity_type": "配置名称",
                    "table_column": "public.vehicle_params.type_name",
                }
            ]

        @staticmethod
        def get_related_entities(query: str, **_kwargs: Any) -> list[dict[str, Any]]:
            calls.append(("entities", query))
            if query != question:
                return []
            return [
                {
                    "entity_type": "配置名称",
                    "canonical_name": "驾驶辅助级别",
                    "aliases": ["自动驾驶级别", "智驾级别", "L2+"],
                    "table_column": "public.vehicle_params.type_name",
                    "score": 0.99,
                }
            ]

        @staticmethod
        def get_related_ddl(query: str, **_kwargs: Any) -> list[str]:
            calls.append(("ddl", query))
            return ["CREATE TABLE vehicle_params (type_name text, type_value text);"]

        @staticmethod
        def get_related_documentation(query: str, **_kwargs: Any) -> list[str]:
            calls.append(("documentation", query))
            return []

        @staticmethod
        def get_similar_question_sql(query: str, **_kwargs: Any) -> list[dict[str, str]]:
            calls.append(("sql_examples", query))
            return []

        @staticmethod
        def generate_sql(*, question: str, entity_list: list[dict[str, Any]], **_kwargs: Any) -> str:
            calls.append(("generate_sql", question))
            assert entity_list[0]["canonical_name"] == "驾驶辅助级别"
            return "SELECT COUNT(*) FROM vehicle_params WHERE type_name = '自动驾驶级别'"

        @staticmethod
        def submit_prompt(prompt: list[dict[str, str]], **_kwargs: Any) -> str:
            calls.append(("refine", prompt[1]["content"]))
            assert 'canonical_name": "驾驶辅助级别' in prompt[1]["content"]
            assert "语义资产中的自然语言概念“自动驾驶级别”" in prompt[1]["content"]
            assert "数据库实体证据在物理名称或存储值上冲突时" in prompt[0]["content"]
            return "SELECT COUNT(*) FROM vehicle_params WHERE type_name = '驾驶辅助级别'"

    route = TableRoute(
        database_source_id="source-1",
        source_name="测试库",
        database="test",
        dialect="PostgreSQL",
        table_names=["vehicle_params"],
        available_tables=["vehicle_params"],
        candidates=[],
        confidence=1.0,
        reason="test",
        prompt_context="只允许使用 vehicle_params。",
    )
    timings: dict[str, float] = {}

    sql, references, _guardrail_note, generation = await nl2sql_service._generate_grounded_sql(
        request=DatabaseQueryRequest(question=question),
        route=route,
        semantic_context="语义资产中的自然语言概念“自动驾驶级别”表示 L2 及以上。",
        semantic_trace={},
        vanna=_FakeVanna(),
        stage_timings=timings,
    )

    retrieval_calls = [
        value for name, value in calls if name in {"ddl", "documentation", "sql_examples", "generate_sql"}
    ]
    assert retrieval_calls and all(value == question for value in retrieval_calls)
    # A second, typed entity lookup is intentionally scoped to the unsupported
    # EAV literal. It discovers candidates but never changes business intent.
    assert ("entities", "自动驾驶级别") in calls
    assert references["entities"]["groups"][0]["items"][0]["name"] == "驾驶辅助级别"
    assert "type_name = '驾驶辅助级别'" in sql
    assert "type_name = '自动驾驶级别'" not in sql
    assert generation["candidate_sql"].endswith("type_name = '自动驾驶级别'")
    assert generation["final_sql"] == sql
    assert generation["entity_authority"] == "database_entity_evidence_for_physical_facts"
    assert timings["sql_candidate_generation_ms"] >= 0
    assert timings["sql_semantic_refinement_ms"] >= 0


class _FakeSessionMaker:
    def __call__(self) -> _FakeSessionMaker:
        return self

    async def __aenter__(self) -> object:
        return object()

    async def __aexit__(self, *_args: object) -> None:
        return None


@pytest.mark.asyncio
async def test_legacy_database_tool_forwards_strict_result_owner_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    async def fake_query(_session, _request, **kwargs):
        captured.update(kwargs)
        return object()

    monkeypatch.setattr(
        result_source_tool_module,
        "get_sessionmaker",
        lambda: _FakeSessionMaker(),
    )
    import tools.database.legacy_query_tool as legacy_query_tool_module

    monkeypatch.setattr(
        legacy_query_tool_module,
        "get_sessionmaker",
        lambda: _FakeSessionMaker(),
    )
    monkeypatch.setattr(
        legacy_query_tool_module,
        "query_database_knowledge",
        fake_query,
    )
    monkeypatch.setattr(
        legacy_query_tool_module,
        "emit_trace_spans",
        lambda _result: None,
    )
    monkeypatch.setattr(
        legacy_query_tool_module,
        "format_result",
        lambda _result: "ok",
    )
    tool = legacy_query_tool_module.DatabaseKnowledgeQueryTool()
    result = await tool._arun(
        question="查询配置率",
        runtime=SimpleNamespace(
            tool_call_id="call-legacy-db",
            context={
                "session_id": "session-legacy-db",
                "query_id": "query-legacy-db",
                "run_id": "run-legacy-db",
            },
        ),
    )

    assert result == "ok"
    assert captured == {
        "session_id": "session-legacy-db",
        "tool_call_id": "call-legacy-db",
        "source_query_id": "query-legacy-db",
        "source_run_id": "run-legacy-db",
    }


def test_profile_fixture_keeps_omitted_tengshi_group_visible() -> None:
    rows = [
        {"车型名称": f"比亚迪车型{i}", "品牌": "比亚迪", "上市日期": "2026-06-01", "价格": "10.00"} for i in range(20)
    ] + [
        {"车型名称": "腾势车型1", "品牌": "腾势", "上市日期": "2026-06-23", "价格": "31.98"},
        {"车型名称": "腾势车型2", "品牌": "腾势", "上市日期": "2026-06-23", "价格": "34.98"},
    ]
    columns = ["车型名称", "品牌", "上市日期", "价格"]

    profile = _profile_from_rows(rows, columns)

    assert profile["group_counts"]["品牌"] == {"比亚迪": 20, "腾势": 2}
    assert profile["date_ranges"]["上市日期"] == {"min": "2026-06-01", "max": "2026-06-23"}
    assert profile["numeric_ranges"]["价格"] == {"min": 10.0, "max": 34.98}


def test_profile_token_budget_trims_distribution_evidence() -> None:
    profile = {
        "group_counts": {
            "品牌": {f"品牌{i}": 100 - i for i in range(30)},
        },
        "date_ranges": {
            "上市日期": {"min": "2021-01-01", "max": "2026-12-31"},
        },
    }

    trimmed = _trim_profile_to_token_budget(profile, token_budget=60)

    assert _estimate_tokens(trimmed) <= 60
    assert trimmed["group_counts"]["品牌"]["品牌0"] == 100
    assert len(trimmed["group_counts"]["品牌"]) < 30


def test_budget_fixture_marks_preview_only_with_omitted_count() -> None:
    rows = [
        {"车型名称": f"比亚迪车型{i}", "品牌": "比亚迪", "上市日期": "2026-06-01", "价格": "10.00"} for i in range(20)
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


def test_validate_readonly_sql_does_not_treat_is_not_distinct_from_column_as_table() -> None:
    sql = """WITH model_latest AS (
      SELECT MAX(launch_date) AS latest_launch_date_by_model
      FROM vehicle_model_base
    ),
    config_latest AS (
      SELECT MAX(launch_date) AS latest_launch_date_by_config
      FROM vehicle_params
    )
    SELECT ml.latest_launch_date_by_model IS NOT DISTINCT FROM cl.latest_launch_date_by_config
    FROM model_latest ml
    CROSS JOIN config_latest cl"""

    clean_sql = validate_readonly_sql(
        sql,
        allowed_tables=["vehicle_model_base", "vehicle_params"],
    )

    assert "IS NOT DISTINCT FROM" in clean_sql
    assert _referenced_tables(sql) == {"vehicle_model_base", "vehicle_params"}


def test_validate_readonly_sql_does_not_treat_is_distinct_from_column_as_table() -> None:
    sql = "SELECT left_row.value IS DISTINCT FROM right_row.value FROM vehicle_params left_row JOIN vehicle_model_base right_row ON TRUE"

    assert (
        validate_readonly_sql(
            sql,
            allowed_tables=["vehicle_params", "vehicle_model_base"],
        )
        == sql
    )


def test_validate_readonly_sql_allows_postgres_btrim_builtin() -> None:
    sql = "SELECT btrim(type_value) AS value FROM vehicle_params"

    assert validate_readonly_sql(sql, allowed_tables=["vehicle_params"]) == sql


def test_validate_readonly_sql_allows_static_values_relation() -> None:
    sql = """WITH bands(label, sort_order) AS (
      SELECT * FROM (VALUES ('5-10万元', 1), ('10-15万元', 2)) AS value_rows(label, sort_order)
    )
    SELECT bands.label FROM bands ORDER BY bands.sort_order"""

    assert validate_readonly_sql(sql, allowed_tables=["vehicle_params"]) == sql
    assert _referenced_tables(sql) == set()


def test_static_values_relation_does_not_hide_unauthorized_scalar_subquery() -> None:
    sql = "SELECT * FROM (VALUES ((SELECT secret FROM private.secret_models))) AS value_rows(value)"

    with pytest.raises(SqlRunnerError, match="private.secret_models"):
        validate_readonly_sql(sql, allowed_tables=["vehicle_params"])


def test_validate_readonly_sql_preserves_schema_qualified_table_scope() -> None:
    sql = 'SELECT vp.car_name FROM "public"."vehicle_params" vp'

    assert _referenced_tables(sql) == {"public.vehicle_params"}
    assert validate_readonly_sql(sql, allowed_tables=["public.vehicle_params"]) == sql


def test_validate_readonly_sql_rejects_unauthorized_table_inside_cte() -> None:
    sql = """WITH hidden AS (
      SELECT * FROM private.secret_models
    )
    SELECT * FROM hidden"""

    with pytest.raises(SqlRunnerError, match="private.secret_models"):
        validate_readonly_sql(sql, allowed_tables=["vehicle_params"])


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT * FROM dblink('connection', 'SELECT * FROM secret') AS t(id int)",
        "SELECT * FROM generate_series(1, 3) AS n",
        "SELECT * FROM unnest(ARRAY[1, 2]) AS n",
    ],
)
def test_validate_readonly_sql_fails_closed_for_table_functions(sql: str) -> None:
    with pytest.raises(SqlRunnerError, match=r"未授权的(?:表函数|关系或系统读取函数)"):
        validate_readonly_sql(sql, allowed_tables=["vehicle_params"])


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT query_to_xml('SELECT * FROM private.secret', true, false, '')",
        "SELECT table_to_xml('private.secret'::regclass, true, false, '')",
        "SELECT pg_read_file('/etc/passwd')",
    ],
)
def test_validate_readonly_sql_fails_closed_for_dynamic_relation_readers(sql: str) -> None:
    with pytest.raises(SqlRunnerError, match="未授权的关系或系统读取函数"):
        validate_readonly_sql(sql, allowed_tables=["vehicle_params"])


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT secret_reader()",
        "SELECT public.secret_reader()",
        "SELECT public.jsonb_agg(car_name) FROM vehicle_params",
        "SELECT convert_from(lo_get(1234), 'UTF8')",
        "SELECT loread(1234, 4096)",
    ],
)
def test_validate_readonly_sql_rejects_unregistered_scalar_relation_readers(sql: str) -> None:
    with pytest.raises(SqlRunnerError, match="未授权的"):
        validate_readonly_sql(sql, allowed_tables=["vehicle_params"])


def test_validate_readonly_sql_allows_explicit_safe_postgres_aggregate_exception() -> None:
    sql = "SELECT jsonb_agg(car_name) FROM vehicle_params"

    assert validate_readonly_sql(sql, allowed_tables=["vehicle_params"]) == sql


def test_validate_readonly_sql_recognizes_commented_chained_ctes() -> None:
    sql = """WITH model_base AS (
      SELECT brand FROM vehicle_params
    ),
    -- 筛选有效款型
    valid_models AS (
      SELECT brand FROM model_base
    ),
    /* 去重上市事件 */
    events AS (
      SELECT DISTINCT brand FROM valid_models
    ),
    event_sequence AS (
      SELECT brand FROM events
    )
    SELECT brand FROM event_sequence"""

    clean_sql = validate_readonly_sql(sql, allowed_tables=["vehicle_params"])

    assert clean_sql == sql


def test_validate_readonly_sql_ignores_table_like_text_inside_comments() -> None:
    sql = """WITH events AS (
      SELECT brand FROM vehicle_params
      -- FROM unauthorized_debug_table
    )
    SELECT brand FROM events"""

    clean_sql = validate_readonly_sql(sql, allowed_tables=["vehicle_params"])

    assert clean_sql == sql


def test_validate_readonly_sql_reports_incomplete_cte_shape_before_table_scope() -> None:
    sql = """SELECT brand FROM vehicle_params
    ),
    car_launch AS (
      SELECT brand FROM model_base
    )
    SELECT brand FROM car_launch"""

    with pytest.raises(SqlRunnerError, match="SQL 结构不完整：括号不平衡"):
        validate_readonly_sql(sql, allowed_tables=["vehicle_params"])


def test_database_query_error_format_includes_generated_sql() -> None:
    sql = "SELECT COUNT(*) FROM vehicle_params"
    message = _format_query_error(DatabaseKnowledgeQueryError("SQL 引用了未授权数据表：foo", sql=sql))

    assert "数据库问数失败" in message
    assert "生成 SQL" in message
    assert sql in message


@pytest.mark.asyncio
async def test_explicit_sql_execute_persists_complete_rows_for_pagination(monkeypatch: pytest.MonkeyPatch) -> None:
    rows = [{"row_number": index, "serial_name": f"车系{index % 41}"} for index in range(206)]
    execution = SqlExecutionResult(
        columns=["row_number", "serial_name"],
        rows=rows[:80],
        row_count=206,
        limited=True,
        total_row_count=206,
        preview_count=80,
        omitted_count=126,
        is_complete=False,
        materialized_rows=rows,
        materialized_all=True,
    )
    route = TableRoute(
        database_source_id="source-1",
        source_name="测试库",
        database="test",
        dialect="PostgreSQL",
        table_names=["vehicle_params"],
        available_tables=["vehicle_params"],
        candidates=[],
        confidence=1.0,
        reason="test",
        prompt_context="",
    )
    generation = database_sql_revision_resume_registry.register_generation(
        session_id="session-pagination",
        query_id="query-pagination",
        result=DatabaseSqlGenerationResult(
            question="测试完整结果分页",
            sql="SELECT * FROM vehicle_params",
            source={"id": "source-1", "name": "测试库"},
            route=route,
        ),
        request={"question": "测试完整结果分页", "table_names": ["vehicle_params"]},
    )
    persisted: dict[str, Any] = {}

    async def fake_resolve(_source_id: str | None, _tables: list[str] | None) -> tuple[dict, dict, list[str]]:
        return {}, {"id": "source-1", "name": "测试库", "database": "test"}, ["vehicle_params"]

    async def fake_run(*_args: object, **_kwargs: object) -> SqlExecutionResult:
        return execution

    async def fake_persist(_session: object, **kwargs: Any) -> dict[str, Any]:
        persisted.update(kwargs)
        return {
            "result_id": "qr-pagination",
            "artifact_path": "data/query-results/qr-pagination.jsonl",
            "expires_at": "2026-07-20T00:00:00+00:00",
            "ttl_hours": 168,
        }

    monkeypatch.setattr(sql_execute_module, "resolve_database_source_scope", fake_resolve)
    monkeypatch.setattr(sql_execute_module, "run_readonly_sql", fake_run)
    monkeypatch.setattr(sql_execute_module, "get_sessionmaker", lambda: _FakeSessionMaker())
    monkeypatch.setattr(result_store_module, "persist_query_result", fake_persist)
    receipt = database_sql_revision_resume_registry.register_validation_receipt(
        generation=generation,
        database_source_id="source-1",
        allowed_tables=["vehicle_params"],
    )

    output = await DatabaseSqlExecuteTool(session_id="session-pagination")._arun(
        sql=generation.result.sql,
        generation_id=generation.id,
        validation_receipt_id=receipt.id,
        limit=5000,
    )

    assert persisted["question"] == "测试完整结果分页"
    assert persisted["rows"] == rows
    assert persisted["session_id"] == "session-pagination"
    assert "结果：206 行（展示 80 行，省略 126 行）" in output
    assert "result_id：qr-pagination" in output
    assert "database_query_result_page(result_id, page, page_size)" in output
    assert "fetch_page: 可用" in output


@pytest.mark.asyncio
async def test_persisted_execution_reads_all_206_rows_across_pages(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        sessionmaker = async_sessionmaker(engine, expire_on_commit=False)
        monkeypatch.setattr(result_store_module, "RESULT_DIR", tmp_path / "data" / "query-results")
        rows = [{"row_number": index} for index in range(206)]
        execution = SqlExecutionResult(
            columns=["row_number"],
            rows=rows[:80],
            row_count=206,
            limited=True,
            total_row_count=206,
            preview_count=80,
            omitted_count=126,
            is_complete=False,
            materialized_rows=rows,
            materialized_all=True,
        )

        async with sessionmaker() as session:
            persisted = await result_store_module.attach_persisted_query_result(
                session,
                execution,
                question="206 行分页测试",
                sql="SELECT row_number FROM test_rows",
                session_id="session-pagination-store",
            )
            assert persisted is True
            assert execution.result_id

            page_1 = await result_store_module.get_query_result_page(
                session, execution.result_id, page=1, page_size=100
            )
            page_2 = await result_store_module.get_query_result_page(
                session, execution.result_id, page=2, page_size=100
            )
            page_3 = await result_store_module.get_query_result_page(
                session, execution.result_id, page=3, page_size=100
            )
            with pytest.raises(
                result_store_module.QueryResultStoreError,
                match="不属于当前 Session",
            ):
                await result_store_module.get_query_result_page(
                    session,
                    execution.result_id,
                    page=1,
                    page_size=100,
                    session_id="another-session",
                )

        assert len(page_1["rows"]) == 100
        assert page_1["has_next"] is True
        assert len(page_2["rows"]) == 100
        assert page_2["has_next"] is True
        assert len(page_3["rows"]) == 6
        assert page_3["has_next"] is False
        assert [row["row_number"] for row in page_1["rows"] + page_2["rows"] + page_3["rows"]] == list(range(206))
        artifact = result_store_module.RESULT_DIR / f"{execution.result_id}.jsonl"
        artifact.write_text('{"row_number":999}\n', encoding="utf-8")
        async with sessionmaker() as session:
            with pytest.raises(
                result_store_module.QueryResultStoreError,
                match="hash 不一致",
            ):
                await result_store_module.get_query_result_page(
                    session,
                    execution.result_id,
                )
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_database_result_adapts_to_generic_source_reference_without_rows(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from graph.session_manager import session_manager

    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        sessionmaker = async_sessionmaker(engine, expire_on_commit=False)
        monkeypatch.setattr(
            result_store_module,
            "RESULT_DIR",
            tmp_path / "data" / "query-results",
        )
        monkeypatch.setattr(
            result_source_tool_module,
            "get_sessionmaker",
            lambda: sessionmaker,
        )
        state = tmp_path / "state"
        state.mkdir()
        session_manager.initialize(state)
        session_manager.create_session("source-adapter-session")
        rows = [{"year": 2021 + index % 6, "config": f"配置-{index}"} for index in range(337)]
        execution = SqlExecutionResult(
            columns=["year", "config"],
            rows=rows[:20],
            row_count=337,
            limited=True,
            total_row_count=337,
            preview_count=20,
            omitted_count=317,
            is_complete=False,
            materialized_rows=rows,
            materialized_all=True,
        )
        async with sessionmaker() as session:
            await result_store_module.attach_persisted_query_result(
                session,
                execution,
                question="配置结果直写",
                sql="SELECT year, config FROM vehicle_params",
                session_id="source-adapter-session",
                tool_call_id="call-sql",
                source_query_id="query-sql",
                source_run_id="run-sql",
                producer_receipt_ids=["sql-validation-receipt"],
            )

        result = await DatabaseQueryResultSourceTool(session_id="source-adapter-session")._arun(
            result_id=str(execution.result_id),
            runtime=SimpleNamespace(
                context={
                    "session_id": "source-adapter-session",
                    "run_id": "run-sql",
                }
            ),
        )
        payload = json.loads(result)
        source = payload["source"]

        assert payload["status"] == "completed"
        assert source["kind"] == "database_result"
        assert source["row_count"] == 337
        assert "locator" not in source
        assert "配置-0" not in result
        persisted_source = session_manager.get_source_reference(
            "source-adapter-session",
            source["source_ref"],
        )
        assert persisted_source is not None
        assert persisted_source["producer_receipt_ids"] == [
            f"result-store:{execution.result_id}",
            "sql-validation-receipt",
        ]
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_backfill_query_result_catalog_recovers_legacy_owner(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from graph.session_manager import session_manager

    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        sessionmaker = async_sessionmaker(engine, expire_on_commit=False)
        monkeypatch.setattr(
            result_store_module,
            "RESULT_DIR",
            tmp_path / "data" / "query-results",
        )
        session_manager.initialize(tmp_path)
        session_manager.create_session("legacy-result-session")
        session_manager.upsert_assistant_message(
            "legacy-result-session",
            query_id="query-legacy",
            content="done",
            tool_calls=[
                {
                    "tool": "database_sql_execute",
                    "id": "call-legacy-owner",
                    "output": "result_id：qr-legacy-owner",
                }
            ],
        )
        artifact = result_store_module.RESULT_DIR / "qr-legacy-owner.jsonl"
        artifact.parent.mkdir(parents=True)
        artifact.write_text('{"id":1}\n', encoding="utf-8")
        now = utcnow()
        async with sessionmaker() as session:
            session.add(
                AnalyticsQueryResult(
                    id="qr-legacy-owner",
                    session_id="legacy-result-session",
                    tool_call_id="",
                    question="legacy",
                    sql="SELECT 1",
                    columns=["id"],
                    row_count=1,
                    profile_json={},
                    artifact_path="qr-legacy-owner.jsonl",
                    artifact_format="jsonl",
                    status="ready",
                    created_at=now,
                    expires_at=now + timedelta(hours=1),
                )
            )
            await session.commit()

            assert await result_store_module.backfill_query_result_catalogs(session) == 1
            record = await session.get(AnalyticsQueryResult, "qr-legacy-owner")
            assert record is not None
            assert record.tool_call_id == "call-legacy-owner"

        catalog = json.loads(
            (result_store_module.RESULT_DIR / ".catalog" / "qr-legacy-owner.json").read_text(encoding="utf-8")
        )
        assert catalog["session_id"] == "legacy-result-session"
        assert catalog["tool_call_id"] == "call-legacy-owner"
        assert catalog["artifact_sha256"].startswith("sha256:")
        immutable_hash = catalog["artifact_sha256"]
        artifact.write_text('{"id":999}\n', encoding="utf-8")
        async with sessionmaker() as session:
            assert await result_store_module.backfill_query_result_catalogs(session) == 0
        catalog_after_restart = json.loads(
            (result_store_module.RESULT_DIR / ".catalog" / "qr-legacy-owner.json").read_text(encoding="utf-8")
        )
        assert catalog_after_restart["artifact_sha256"] == immutable_hash
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_expired_result_cleanup_retains_retryable_tombstone_on_unlink_failure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        sessionmaker = async_sessionmaker(engine, expire_on_commit=False)
        result_dir = tmp_path / "data" / "query-results"
        monkeypatch.setattr(result_store_module, "RESULT_DIR", result_dir)
        artifact = result_dir / "qr-expired.jsonl"
        catalog = result_dir / ".catalog" / "qr-expired.json"
        catalog.parent.mkdir(parents=True)
        artifact.write_text('{"id":1}\n', encoding="utf-8")
        catalog.write_text('{"result_id":"qr-expired"}', encoding="utf-8")
        now = utcnow()
        async with sessionmaker() as session:
            session.add(
                AnalyticsQueryResult(
                    id="qr-expired",
                    session_id="",
                    tool_call_id="",
                    question="expired",
                    sql="SELECT 1",
                    columns=["id"],
                    row_count=1,
                    profile_json={},
                    artifact_path=artifact.name,
                    artifact_format="jsonl",
                    status="ready",
                    created_at=now - timedelta(hours=2),
                    expires_at=now - timedelta(hours=1),
                )
            )
            await session.commit()

        original_unlink = Path.unlink
        failed_once = False

        def flaky_unlink(
            path: Path,
            missing_ok: bool = False,
        ) -> None:
            nonlocal failed_once
            if path.name == "qr-expired.json" and not failed_once:
                failed_once = True
                raise OSError("simulated catalog unlink failure")
            original_unlink(path, missing_ok=missing_ok)

        monkeypatch.setattr(Path, "unlink", flaky_unlink)
        async with sessionmaker() as session:
            assert await result_store_module.cleanup_expired_query_results(session) == 0
            record = await session.get(AnalyticsQueryResult, "qr-expired")
            assert record is not None
            assert record.status == "deleting"
        assert not artifact.exists()
        assert catalog.exists()

        async with sessionmaker() as session:
            assert await result_store_module.cleanup_expired_query_results(session) == 1
            assert await session.get(AnalyticsQueryResult, "qr-expired") is None
        assert not catalog.exists()
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_orphan_scavenger_uses_database_ownership_and_grace_window(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        sessionmaker = async_sessionmaker(engine, expire_on_commit=False)
        result_dir = tmp_path / "data" / "query-results"
        catalog_dir = result_dir / ".catalog"
        catalog_dir.mkdir(parents=True)
        monkeypatch.setattr(result_store_module, "RESULT_DIR", result_dir)

        owned_artifact = result_dir / "qr-owned.jsonl"
        owned_artifact.write_text('{"id":1}\n', encoding="utf-8")
        orphan_artifact = result_dir / "qr_orphan.jsonl"
        orphan_catalog = catalog_dir / "qr_orphan.json"
        orphan_artifact.write_text('{"id":2}\n', encoding="utf-8")
        orphan_catalog.write_text('{"result_id":"qr-orphan"}', encoding="utf-8")
        fresh_artifact = result_dir / "qr-fresh.jsonl"
        fresh_artifact.write_text('{"id":3}\n', encoding="utf-8")
        now = utcnow()
        async with sessionmaker() as session:
            session.add(
                AnalyticsQueryResult(
                    id="qr-owned",
                    session_id="",
                    tool_call_id="",
                    question="owned",
                    sql="SELECT 1",
                    columns=["id"],
                    row_count=1,
                    profile_json={},
                    artifact_path=owned_artifact.name,
                    artifact_format="jsonl",
                    status="ready",
                    created_at=now,
                    expires_at=now + timedelta(hours=1),
                )
            )
            await session.commit()

            removed = await result_store_module.scavenge_orphaned_query_result_files(
                session,
                grace_seconds=1,
            )
            assert removed == 0
            assert orphan_artifact.exists()
            assert fresh_artifact.exists()

            old_timestamp = orphan_artifact.stat().st_mtime - 10
            orphan_artifact.touch()
            orphan_catalog.touch()
            import os

            os.utime(orphan_artifact, (old_timestamp, old_timestamp))
            os.utime(orphan_catalog, (old_timestamp, old_timestamp))
            removed = await result_store_module.scavenge_orphaned_query_result_files(
                session,
                grace_seconds=1,
            )

        assert removed == 1
        assert owned_artifact.exists()
        assert fresh_artifact.exists()
        assert not orphan_artifact.exists()
        assert not orphan_catalog.exists()
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_persist_registers_creating_owner_before_publishing_final_files(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        sessionmaker = async_sessionmaker(engine, expire_on_commit=False)
        result_dir = tmp_path / "data" / "query-results"
        monkeypatch.setattr(result_store_module, "RESULT_DIR", result_dir)
        monkeypatch.setattr(
            result_store_module,
            "get_database_qa_config",
            lambda: {"result_store_ttl_hours": 168},
        )

        async with sessionmaker() as session:
            original_commit = session.commit
            committed_states: list[tuple[str, bool, bool]] = []

            async def tracked_commit() -> None:
                await original_commit()
                result = await session.execute(select(AnalyticsQueryResult))
                record = result.scalars().first()
                if record is not None:
                    committed_states.append(
                        (
                            record.status,
                            (result_dir / f"{record.id}.jsonl").exists(),
                            (result_dir / ".catalog" / f"{record.id}.json").exists(),
                        )
                    )

            monkeypatch.setattr(session, "commit", tracked_commit)
            stored = await result_store_module.persist_query_result(
                session,
                question="two phase",
                sql="SELECT 1",
                columns=["id"],
                rows=[{"id": 1}],
                profile={},
            )

        assert stored["result_id"].startswith("qr_")
        assert committed_states[0] == ("creating", False, False)
        assert committed_states[-1] == ("ready", True, True)
    finally:
        await engine.dispose()
