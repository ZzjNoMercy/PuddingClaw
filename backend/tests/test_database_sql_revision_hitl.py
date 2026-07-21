"""Regression tests for generated-SQL provenance and natural-language revision HITL."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from analytics.nl2sql.schemas import DatabaseSqlGenerationResult, SqlExecutionResult, TableRoute
from graph.database_sql_revision_resume import DatabaseSqlRevisionResumeRegistry
from tools.database.sql_execute_tool import DatabaseSqlExecuteTool
from tools.database.sql_generate_tool import DatabaseSqlGenerateTool
from tools.database.sql_validate_tool import DatabaseSqlValidateTool


def _result(question: str, sql: str = "SELECT 1") -> DatabaseSqlGenerationResult:
    return DatabaseSqlGenerationResult(
        question=question,
        sql=sql,
        source={"id": "source-1", "name": "测试库"},
        route=TableRoute(
            database_source_id="source-1",
            source_name="测试库",
            database="test",
            dialect="postgresql",
            table_names=["vehicle_params"],
            available_tables=["vehicle_params"],
            candidates=[],
            confidence=1.0,
            reason="test",
            prompt_context="",
        ),
        semantic_assets={"matched": [], "references": []},
    )


def test_database_sql_generate_runtime_is_hidden_from_llm_schema() -> None:
    tool = DatabaseSqlGenerateTool()

    assert "runtime" in tool.get_input_schema().model_fields
    assert "runtime" not in tool.tool_call_schema.model_fields


@pytest.mark.asyncio
async def test_database_sql_generate_uses_selected_model_from_runtime_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import tools.database.sql_generate_tool as module

    requests: list[Any] = []

    async def fake_generate(_session: object, request: Any) -> DatabaseSqlGenerationResult:
        requests.append(request)
        return _result(request.question)

    monkeypatch.setattr(module, "get_sessionmaker", lambda: _FakeSessionMaker())
    monkeypatch.setattr(module, "generate_database_sql", fake_generate)
    runtime = SimpleNamespace(
        state={
            "analytics_model_id": "产品配置分析",
            "allowed_semantic_asset_ids": ["measure:launch_cycle"],
        }
    )

    await DatabaseSqlGenerateTool()._arun(
        question="上市周期",
        model_id="Agent误传的模型",
        selected_semantic_asset_ids=["measure:launch_cycle"],
        runtime=runtime,
    )

    assert requests[0].model_id == "产品配置分析"
    assert requests[0].measure_ids == ["measure:launch_cycle"]


@pytest.mark.asyncio
async def test_database_sql_generate_rejects_asset_outside_selected_model() -> None:
    runtime = SimpleNamespace(
        state={
            "analytics_model_id": "产品配置分析",
            "allowed_semantic_asset_ids": ["dimension:motor_power"],
        }
    )

    result = await DatabaseSqlGenerateTool()._arun(
        question="电机功率趋势",
        selected_semantic_asset_ids=["dimension:price_band"],
        runtime=runtime,
    )

    assert "不属于当前分析模型" in result
    assert "dimension:price_band" in result


@pytest.mark.asyncio
async def test_database_sql_generate_normalizes_unique_bare_asset_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import tools.database.sql_generate_tool as module

    requests: list[Any] = []

    async def fake_generate(_session: object, request: Any) -> DatabaseSqlGenerationResult:
        requests.append(request)
        return _result(request.question)

    monkeypatch.setattr(module, "get_sessionmaker", lambda: _FakeSessionMaker())
    monkeypatch.setattr(module, "generate_database_sql", fake_generate)
    runtime = SimpleNamespace(
        state={
            "analytics_model_id": "产品配置分析",
            "allowed_semantic_asset_ids": ["measure:launch_cycle", "dimension:launch_time"],
        }
    )

    await DatabaseSqlGenerateTool()._arun(
        question="上市周期",
        selected_semantic_asset_ids=["launch_cycle"],
        runtime=runtime,
    )

    assert requests[0].measure_ids == ["measure:launch_cycle"]


@pytest.mark.asyncio
async def test_database_sql_generate_rejects_ambiguous_bare_asset_id() -> None:
    runtime = SimpleNamespace(
        state={
            "analytics_model_id": "产品配置分析",
            "allowed_semantic_asset_ids": ["measure:launch", "dimension:launch"],
        }
    )

    result = await DatabaseSqlGenerateTool()._arun(
        question="上市情况",
        selected_semantic_asset_ids=["launch"],
        runtime=runtime,
    )

    assert "存在多个候选" in result
    assert "measure:launch" in result
    assert "dimension:launch" in result


@pytest.mark.asyncio
async def test_revision_registry_has_exactly_three_natural_language_decisions() -> None:
    registry = DatabaseSqlRevisionResumeRegistry()
    generation = registry.register_generation(
        session_id="session-1",
        query_id="query-1",
        result=_result("空气悬架配置率"),
        request={"question": "空气悬架配置率"},
    )

    agree_request = registry.create_revision_request(
        generation=generation,
        proposed_revision_instruction="包含皮卡",
        tool_call_id="tool-1",
    )
    assert agree_request["original_sql"] == "SELECT 1"
    assert registry.resolve(agree_request["id"], {"action": "agree"}) == {
        "action": "agree",
        "revision_instruction": "包含皮卡",
    }

    reject_request = registry.create_revision_request(
        generation=generation,
        proposed_revision_instruction="包含皮卡",
        tool_call_id="tool-2",
    )
    assert registry.resolve(reject_request["id"], {"action": "reject"}) == {"action": "reject"}

    modify_request = registry.create_revision_request(
        generation=generation,
        proposed_revision_instruction="包含皮卡",
        tool_call_id="tool-3",
    )
    assert registry.resolve(
        modify_request["id"],
        {"action": "modify", "revision_instruction": "只统计皮卡"},
    ) == {"action": "modify", "revision_instruction": "只统计皮卡"}


class _FakeSessionMaker:
    def __call__(self) -> _FakeSessionMaker:
        return self

    async def __aenter__(self) -> object:
        return object()

    async def __aexit__(self, *_args: Any) -> None:
        return None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("decision", "expected_instruction"),
    [
        ({"action": "agree", "revision_instruction": "包含皮卡"}, "包含皮卡"),
        ({"action": "modify", "revision_instruction": "只统计皮卡"}, "只统计皮卡"),
    ],
)
async def test_approved_or_modified_revision_is_regenerated_from_natural_language(
    monkeypatch: pytest.MonkeyPatch,
    decision: dict[str, str],
    expected_instruction: str,
) -> None:
    import tools.database.sql_generate_tool as module

    requests: list[str] = []

    async def fake_generate(_session: object, request: Any) -> DatabaseSqlGenerationResult:
        requests.append(request.question)
        return _result(request.question, sql=f"SELECT {len(requests)}")

    monkeypatch.setattr(module, "get_sessionmaker", lambda: _FakeSessionMaker())
    monkeypatch.setattr(module, "generate_database_sql", fake_generate)
    monkeypatch.setattr(module, "interrupt", lambda _payload: decision)
    tool = DatabaseSqlGenerateTool(session_id="session-hitl", query_id="query-hitl")

    original = await tool._arun(question="空气悬架配置率")
    generation_id = next(line.split("：", 1)[1] for line in original.splitlines() if line.startswith("- generation_id："))
    revised = await tool._arun(
        question="Agent 不得替换的文字",
        parent_generation_id=generation_id,
        revision_instruction="包含皮卡",
    )

    assert len(requests) == 2
    assert requests[1] == f"原始问题：\n空气悬架配置率\n\n用户确认的本次口径补充：\n{expected_instruction}"
    assert "重新生成 SQL" in revised
    assert "SELECT 2" in revised


@pytest.mark.asyncio
async def test_rejected_revision_reuses_original_generation_without_regeneration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import tools.database.sql_generate_tool as module

    calls = 0

    async def fake_generate(_session: object, request: Any) -> DatabaseSqlGenerationResult:
        nonlocal calls
        calls += 1
        return _result(request.question, sql="SELECT original")

    monkeypatch.setattr(module, "get_sessionmaker", lambda: _FakeSessionMaker())
    monkeypatch.setattr(module, "generate_database_sql", fake_generate)
    monkeypatch.setattr(module, "interrupt", lambda _payload: {"action": "reject"})
    tool = DatabaseSqlGenerateTool(session_id="session-reject", query_id="query-reject")

    original = await tool._arun(question="空气悬架配置率")
    generation_id = next(line.split("：", 1)[1] for line in original.splitlines() if line.startswith("- generation_id："))
    rejected = await tool._arun(
        question="ignored",
        parent_generation_id=generation_id,
        revision_instruction="包含皮卡",
    )

    assert calls == 1
    assert generation_id in rejected
    assert "用户拒绝修改" in rejected
    assert "HITL 状态：已完成（resolved）" in rejected
    assert "不要再次询问用户选择" in rejected
    assert "database_sql_validate" in rejected
    assert "database_sql_execute" in rejected
    assert "SELECT original" in rejected


@pytest.mark.asyncio
async def test_technical_sql_repair_regenerates_without_business_hitl(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import tools.database.sql_generate_tool as module

    requests: list[str] = []

    async def fake_generate(_session: object, request: Any) -> DatabaseSqlGenerationResult:
        requests.append(request.question)
        return _result(request.question, sql=f"SELECT {len(requests)}")

    def unexpected_interrupt(_payload: object) -> object:
        raise AssertionError("technical SQL repair must not open business HITL")

    monkeypatch.setattr(module, "get_sessionmaker", lambda: _FakeSessionMaker())
    monkeypatch.setattr(module, "generate_database_sql", fake_generate)
    monkeypatch.setattr(module, "interrupt", unexpected_interrupt)
    tool = DatabaseSqlGenerateTool(session_id="session-technical", query_id="query-technical")

    original = await tool._arun(question="查询 2020 到 2026 年高速 NOA 配置率")
    generation_id = next(
        line.split("：", 1)[1]
        for line in original.splitlines()
        if line.startswith("- generation_id：")
    )
    repaired = await tool._arun(
        question="ignored",
        parent_generation_id=generation_id,
        revision_instruction=(
            "原始 SQL 的相关 EXISTS 子查询超时，请改用 LEFT JOIN、DISTINCT 标志表和 COUNT FILTER 聚合。"
        ),
    )

    assert len(requests) == 2
    assert "原始业务问题（业务语义不可改变）" in requests[1]
    assert "SQL 技术修复反馈" in requests[1]
    assert "技术修复已自动重生成" in repaired
    assert "无需业务口径确认" in repaired
    assert "SELECT 2" in repaired


@pytest.mark.asyncio
async def test_mixed_business_and_technical_revision_still_requires_hitl(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import tools.database.sql_generate_tool as module

    requests: list[str] = []
    interrupted: list[dict[str, Any]] = []

    async def fake_generate(_session: object, request: Any) -> DatabaseSqlGenerationResult:
        requests.append(request.question)
        return _result(request.question, sql=f"SELECT {len(requests)}")

    def approve(payload: dict[str, Any]) -> dict[str, str]:
        interrupted.append(payload)
        return {
            "action": "agree",
            "revision_instruction": "分母改为车系粒度，并使用 LEFT JOIN",
        }

    monkeypatch.setattr(module, "get_sessionmaker", lambda: _FakeSessionMaker())
    monkeypatch.setattr(module, "generate_database_sql", fake_generate)
    monkeypatch.setattr(module, "interrupt", approve)
    tool = DatabaseSqlGenerateTool(session_id="session-mixed", query_id="query-mixed")

    original = await tool._arun(question="高速 NOA 配置率")
    generation_id = next(
        line.split("：", 1)[1]
        for line in original.splitlines()
        if line.startswith("- generation_id：")
    )
    await tool._arun(
        question="ignored",
        parent_generation_id=generation_id,
        revision_instruction="分母改为车系粒度，并使用 LEFT JOIN",
    )

    assert len(interrupted) == 1
    assert interrupted[0]["type"] == "database_sql_revision_request"


@pytest.mark.asyncio
async def test_agent_mode_validate_and_execute_use_registered_sql(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import tools.database.sql_execute_tool as execute_module
    import tools.database.sql_validate_tool as validate_module
    from graph.database_sql_revision_resume import database_sql_revision_resume_registry

    generation = database_sql_revision_resume_registry.register_generation(
        session_id="session-block",
        query_id="query-block",
        result=_result("原问题", sql="SELECT 1"),
        request={"question": "原问题", "table_names": ["vehicle_params"]},
    )
    executed_sql: list[str] = []

    async def fake_resolve(_source_id: str | None, _tables: list[str] | None) -> tuple[dict, dict, list[str]]:
        return {}, {"id": "source-1", "name": "测试库", "database": "test"}, ["vehicle_params"]

    async def fake_run(_source: object, sql: str, **_kwargs: object) -> SqlExecutionResult:
        executed_sql.append(sql)
        return SqlExecutionResult(columns=[], rows=[], row_count=0, limited=False)

    monkeypatch.setattr(validate_module, "resolve_database_source_scope", fake_resolve)
    monkeypatch.setattr(execute_module, "resolve_database_source_scope", fake_resolve)
    monkeypatch.setattr(execute_module, "run_readonly_sql", fake_run)

    validate_output = await DatabaseSqlValidateTool(session_id="session-block")._arun(
        sql="SELECT 2",
        generation_id=generation.id,
    )
    execute_output = await DatabaseSqlExecuteTool(session_id="session-block")._arun(
        generation_id=generation.id,
    )

    assert "SQL 校验通过" in validate_output
    assert "SELECT 1" in validate_output
    assert "SELECT 2" not in validate_output
    assert "SQL 执行结果" in execute_output
    assert executed_sql == ["SELECT 1"]
