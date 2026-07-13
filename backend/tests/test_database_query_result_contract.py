"""Regression tests for database query result completeness contracts."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import analytics.nl2sql.result_store as result_store_module
import tools.database.sql_execute_tool as sql_execute_module
from analytics.nl2sql.schemas import DatabaseSqlGenerationResult, SqlExecutionResult, TableRoute
from analytics.nl2sql.service import DatabaseKnowledgeQueryError
from analytics.nl2sql.sql_runner import (
    _compact_rows,
    _estimate_tokens,
    _profile_from_rows,
    extract_sql,
    validate_readonly_sql,
)
from graph.database_sql_revision_resume import database_sql_revision_resume_registry
from knowledge.models import Base
from tools.database.sql_execute_tool import DatabaseSqlExecuteTool
from tools.database_knowledge_tool import _format_query_error


class _FakeSessionMaker:
    def __call__(self) -> _FakeSessionMaker:
        return self

    async def __aenter__(self) -> object:
        return object()

    async def __aexit__(self, *_args: object) -> None:
        return None


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
            "artifact_path": "backend/data/database-query-results/qr-pagination.jsonl",
            "expires_at": "2026-07-20T00:00:00+00:00",
            "ttl_hours": 168,
        }

    monkeypatch.setattr(sql_execute_module, "resolve_database_source_scope", fake_resolve)
    monkeypatch.setattr(sql_execute_module, "run_readonly_sql", fake_run)
    monkeypatch.setattr(sql_execute_module, "get_sessionmaker", lambda: _FakeSessionMaker())
    monkeypatch.setattr(result_store_module, "persist_query_result", fake_persist)

    output = await DatabaseSqlExecuteTool(session_id="session-pagination")._arun(
        sql=generation.result.sql,
        generation_id=generation.id,
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
        monkeypatch.setattr(result_store_module, "BASE_DIR", tmp_path)
        monkeypatch.setattr(result_store_module, "RESULT_DIR", tmp_path / "data" / "database-query-results")
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

        assert len(page_1["rows"]) == 100
        assert page_1["has_next"] is True
        assert len(page_2["rows"]) == 100
        assert page_2["has_next"] is True
        assert len(page_3["rows"]) == 6
        assert page_3["has_next"] is False
        assert [row["row_number"] for row in page_1["rows"] + page_2["rows"] + page_3["rows"]] == list(
            range(206)
        )
    finally:
        await engine.dispose()
