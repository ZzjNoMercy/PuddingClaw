"""Regression tests for generated-SQL provenance and natural-language revision HITL."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any

import pytest
from langchain_core.messages import HumanMessage

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
    for tool in (
        DatabaseSqlGenerateTool(),
        DatabaseSqlValidateTool(),
        DatabaseSqlExecuteTool(),
    ):
        assert "runtime" in tool.get_input_schema().model_fields
        assert "runtime" not in tool.tool_call_schema.model_fields


def test_sql_authority_is_scoped_to_run_or_same_goal_revision() -> None:
    registry = DatabaseSqlRevisionResumeRegistry()
    run_generation = registry.register_generation(
        session_id="session-scope",
        query_id="query-a",
        run_id="run-a",
        result=_result("Run scoped"),
        request={"question": "Run scoped"},
    )
    run_receipt = registry.register_validation_receipt(
        generation=run_generation,
        database_source_id="source-1",
        allowed_tables=["vehicle_params"],
    )

    assert registry.get_generation(
        run_generation.id,
        session_id="session-scope",
        run_id="run-a",
    ) is run_generation
    assert registry.get_generation(
        run_generation.id,
        session_id="session-scope",
        run_id="run-b",
    ) is None
    assert registry.get_validation_receipt(
        run_receipt.id,
        session_id="session-scope",
        run_id="run-b",
    ) is None

    goal_generation = registry.register_generation(
        session_id="session-scope",
        query_id="query-goal-a",
        run_id="run-goal-a",
        goal_id="goal-1",
        goal_revision=3,
        result=_result("Goal scoped"),
        request={"question": "Goal scoped"},
    )
    goal_receipt = registry.register_validation_receipt(
        generation=goal_generation,
        database_source_id="source-1",
        allowed_tables=["vehicle_params"],
    )

    assert registry.get_generation(
        goal_generation.id,
        session_id="session-scope",
        run_id="run-goal-b",
        goal_id="goal-1",
        goal_revision=3,
    ) is goal_generation
    assert registry.get_validation_receipt(
        goal_receipt.id,
        session_id="session-scope",
        run_id="run-goal-b",
        goal_id="goal-1",
        goal_revision=3,
    ) is goal_receipt
    assert registry.get_generation(
        goal_generation.id,
        session_id="session-scope",
        run_id="run-goal-c",
        goal_id="goal-1",
        goal_revision=4,
    ) is None
    assert registry.get_generation(
        goal_generation.id,
        session_id="session-scope",
        run_id="run-goal-b",
        goal_id="goal-2",
        goal_revision=3,
    ) is None


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
async def test_goal_business_subquery_is_allowed_without_physical_guidance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import tools.database.sql_generate_tool as module

    requests: list[Any] = []

    async def fake_generate(_session: object, request: Any) -> DatabaseSqlGenerationResult:
        requests.append(request)
        return _result(request.question)

    monkeypatch.setattr(module, "get_sessionmaker", lambda: _FakeSessionMaker())
    monkeypatch.setattr(module, "generate_database_sql", fake_generate)
    objective = "刷新2021到2026年产品配置报告中的全部图表"
    runtime = SimpleNamespace(
        state={
            "_run_objective": objective,
            "messages": [HumanMessage(content=objective)],
        }
    )

    output = await DatabaseSqlGenerateTool()._arun(
        question="统计2021至2026年L2级驾驶辅助的款型配置率，按上市年份分组并排除皮卡",
        runtime=runtime,
    )

    assert len(requests) == 1
    assert requests[0].question.startswith("统计2021至2026年L2级驾驶辅助")
    assert "SQL 生成结果" in output


@pytest.mark.asyncio
async def test_goal_subquery_rejects_agent_invented_l2_physical_mapping(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import tools.database.sql_generate_tool as module

    called = False

    async def fake_generate(_session: object, request: Any) -> DatabaseSqlGenerationResult:
        nonlocal called
        called = True
        return _result(request.question)

    monkeypatch.setattr(module, "get_sessionmaker", lambda: _FakeSessionMaker())
    monkeypatch.setattr(module, "generate_database_sql", fake_generate)
    objective = "刷新2021到2026年产品配置报告中的全部图表"
    runtime = SimpleNamespace(
        state={
            "_run_objective": objective,
            "messages": [HumanMessage(content=objective)],
        }
    )

    output = await DatabaseSqlGenerateTool()._arun(
        question=(
            "统计2021年到2026年每年L2级辅助驾驶的款型配备率。"
            "L2判断依据：vehicle_params中type_name='辅助驾驶系统'且type_value不为空。"
        ),
        runtime=runtime,
    )

    assert called is False
    assert "Agent 在业务子任务中新增了用户未指定的物理实现" in output
    assert "vehicle_params" in output
    assert "type_name" in output
    assert "仅保留业务问题后重新调用" in output


@pytest.mark.asyncio
async def test_agent_read_template_state_authorizes_declared_enum_scope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import tools.database.sql_generate_tool as module

    requests: list[Any] = []

    async def fake_generate(_session: object, request: Any) -> DatabaseSqlGenerationResult:
        requests.append(request)
        return _result(request.question)

    monkeypatch.setattr(module, "get_sessionmaker", lambda: _FakeSessionMaker())
    monkeypatch.setattr(module, "generate_database_sql", fake_generate)
    active_template = {
        "model_id": "产品配置分析",
        "template_id": "monthly_product_config_report",
        "semantic_scope": {
            "enum_filters": {
                "dimension:energy_type": {
                    "members": ["纯电", "插电混合", "增程式纯电动"],
                    "classifications": ["新能源", "传统能源"],
                }
            }
        },
    }
    runtime = SimpleNamespace(
        state={
            "analytics_model_id": "产品配置分析",
            "allowed_semantic_asset_ids": [
                "measure:launch_update_count",
                "measure:launch_cycle",
                "dimension:launch_time",
                "dimension:energy_type",
                "grain:car_model",
            ],
            "_active_analysis_template": active_template,
            "messages": [HumanMessage(content="Agent delegated: 统计纯电车型")],
        },
        context={
            "run_objective": "刷新月报",
        },
    )

    output = await DatabaseSqlGenerateTool()._arun(
        question=(
            "统计2021年至2026年中国狭义乘用车（排除皮卡）的新车迭代情况，"
            "按能源类型（新能源和传统能源）和年份分组。对每年每个能源类型统计："
            "更新次数和平均更新周期天数。2026年只统计2026-01-01至2026-06-30。"
        ),
        selected_semantic_asset_ids=[
            "measure:launch_update_count",
            "measure:launch_cycle",
            "dimension:launch_time",
            "dimension:energy_type",
            "grain:car_model",
        ],
        runtime=runtime,
    )

    assert len(requests) == 1
    assert requests[0].measure_ids == [
        "measure:launch_update_count",
        "measure:launch_cycle",
        "dimension:launch_time",
        "dimension:energy_type",
        "grain:car_model",
    ]
    assert "SQL 生成结果" in output


@pytest.mark.asyncio
async def test_agent_cannot_bypass_enum_guard_by_omitting_asset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import tools.database.sql_generate_tool as module

    called = False

    async def fake_generate(_session: object, request: Any) -> DatabaseSqlGenerationResult:
        nonlocal called
        called = True
        return _result(request.question)

    monkeypatch.setattr(module, "get_sessionmaker", lambda: _FakeSessionMaker())
    monkeypatch.setattr(module, "generate_database_sql", fake_generate)
    runtime = SimpleNamespace(
        state={
            "analytics_model_id": "产品配置分析",
            "allowed_semantic_asset_ids": ["dimension:energy_type"],
            "messages": [HumanMessage(content="刷新产品配置报告")],
        },
        context={"run_objective": "刷新产品配置报告"},
    )

    output = await DatabaseSqlGenerateTool()._arun(
        question="仅统计纯电车型",
        selected_semantic_asset_ids=[],
        runtime=runtime,
    )

    assert called is False
    assert "口径枚举" in output
    assert "纯电" in output


@pytest.mark.asyncio
async def test_delegated_human_message_does_not_authorize_physical_guidance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import tools.database.sql_generate_tool as module

    called = False

    async def fake_generate(_session: object, request: Any) -> DatabaseSqlGenerationResult:
        nonlocal called
        called = True
        return _result(request.question)

    monkeypatch.setattr(module, "get_sessionmaker", lambda: _FakeSessionMaker())
    monkeypatch.setattr(module, "generate_database_sql", fake_generate)
    runtime = SimpleNamespace(
        state={
            "analytics_model_id": "产品配置分析",
            "allowed_semantic_asset_ids": ["dimension:energy_type"],
            "_run_objective": "刷新月报",
            "messages": [
                HumanMessage(content="使用vehicle_params的type_name和type_value统计纯电车型")
            ],
        }
    )

    output = await DatabaseSqlGenerateTool()._arun(
        question="使用vehicle_params的type_name和type_value统计纯电车型",
        runtime=runtime,
    )

    assert called is False
    assert "vehicle_params" in output
    assert "type_name" in output


@pytest.mark.asyncio
async def test_runtime_context_cannot_inject_template_authorization_without_guide_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import tools.database.sql_generate_tool as module

    called = False

    async def fake_generate(_session: object, request: Any) -> DatabaseSqlGenerationResult:
        nonlocal called
        called = True
        return _result(request.question)

    monkeypatch.setattr(module, "get_sessionmaker", lambda: _FakeSessionMaker())
    monkeypatch.setattr(module, "generate_database_sql", fake_generate)
    server_suggested_template = {
        "model_id": "产品配置分析",
        "template_id": "monthly_product_config_report",
        "semantic_scope": {
            "enum_filters": {
                "dimension:energy_type": {"members": ["纯电"], "classifications": []}
            }
        },
    }
    runtime = SimpleNamespace(
        state={
            "analytics_model_id": "产品配置分析",
            "allowed_semantic_asset_ids": ["dimension:energy_type"],
            "_run_objective": "查询空气悬架",
        },
        context={
            "run_objective": "查询空气悬架",
            "active_analysis_template": server_suggested_template,
        },
    )

    output = await DatabaseSqlGenerateTool()._arun(
        question="仅统计纯电车型的空气悬架",
        runtime=runtime,
    )

    assert called is False
    assert "纯电" in output


@pytest.mark.asyncio
async def test_user_supplied_physical_mapping_remains_authorized(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import tools.database.sql_generate_tool as module

    requests: list[Any] = []

    async def fake_generate(_session: object, request: Any) -> DatabaseSqlGenerationResult:
        requests.append(request)
        return _result(request.question)

    monkeypatch.setattr(module, "get_sessionmaker", lambda: _FakeSessionMaker())
    monkeypatch.setattr(module, "generate_database_sql", fake_generate)
    user_question = (
        "按vehicle_params中type_name='驾驶辅助级别'且type_value为L2，"
        "统计2021至2026年款型配置率"
    )
    runtime = SimpleNamespace(
        state={
            "_run_objective": user_question,
            "messages": [HumanMessage(content=user_question)],
        }
    )

    output = await DatabaseSqlGenerateTool()._arun(
        question=user_question,
        table_names=["vehicle_params"],
        runtime=runtime,
    )

    assert len(requests) == 1
    assert requests[0].table_names == ["vehicle_params"]
    assert "SQL 生成结果" in output


@pytest.mark.asyncio
async def test_old_user_turn_does_not_authorize_new_goal_physical_guidance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import tools.database.sql_generate_tool as module

    called = False

    async def fake_generate(_session: object, request: Any) -> DatabaseSqlGenerationResult:
        nonlocal called
        called = True
        return _result(request.question)

    monkeypatch.setattr(module, "get_sessionmaker", lambda: _FakeSessionMaker())
    monkeypatch.setattr(module, "generate_database_sql", fake_generate)
    current_goal = "刷新产品配置报告"
    runtime = SimpleNamespace(
        state={
            "_run_objective": current_goal,
            "_run_query_id": "query-current",
            "messages": [
                HumanMessage(
                    content="旧任务要求使用vehicle_params表",
                    additional_kwargs={"puddingclaw_query_id": "query-old"},
                ),
                HumanMessage(
                    content=current_goal,
                    additional_kwargs={"puddingclaw_query_id": "query-current"},
                ),
            ],
        }
    )

    output = await DatabaseSqlGenerateTool()._arun(
        question="统计L2配置率，使用vehicle_params表中的配置项",
        table_names=["vehicle_params"],
        runtime=runtime,
    )

    assert called is False
    assert "用户未指定的物理实现" in output
    assert "vehicle_params" in output


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


@pytest.mark.asyncio
async def test_revision_request_normalizes_nested_datetime_metadata() -> None:
    registry = DatabaseSqlRevisionResumeRegistry()
    result = _result("空气悬架配置率")
    result.semantic_assets = {
        "matched": [
            {
                "id": "dimension:launch_time",
                "indexed_at": datetime(2026, 7, 29, 13, 42, tzinfo=timezone.utc),
            }
        ],
        "references": [],
    }
    generation = registry.register_generation(
        session_id="session-json",
        query_id="query-json",
        result=result,
        request={"question": "空气悬架配置率"},
    )

    request = registry.create_revision_request(
        generation=generation,
        proposed_revision_instruction="修改统计口径",
        tool_call_id="tool-json",
    )

    assert request["semantic_assets"]["matched"][0]["indexed_at"] == (
        "2026-07-29T13:42:00+00:00"
    )
    json.dumps(request, ensure_ascii=False)


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

    requests: list[Any] = []

    async def fake_generate(_session: object, request: Any) -> DatabaseSqlGenerationResult:
        requests.append(request)
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
    assert requests[1].question == f"原始问题：\n空气悬架配置率\n\n用户确认的本次口径补充：\n{expected_instruction}"
    assert requests[1].semantic_question == requests[1].question
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
@pytest.mark.parametrize(
    "revision_instruction",
    [
        "上一版 SQL 执行超时，数据库返回 statement timeout。",
        (
            'SQL执行失败：column "wb_order" does not exist。VALUES 子句中定义了 '
            "AS wb(ord, seg)，但后续引用了 wb.wb_order 和 pw.pwr_order 列名，"
            "与实际别名 ord 不匹配。"
        ),
    ],
)
async def test_technical_sql_repair_regenerates_without_business_hitl(
    monkeypatch: pytest.MonkeyPatch,
    revision_instruction: str,
) -> None:
    import tools.database.sql_generate_tool as module

    requests: list[Any] = []

    async def fake_generate(_session: object, request: Any) -> DatabaseSqlGenerationResult:
        requests.append(request)
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
        revision_instruction=revision_instruction,
    )

    assert len(requests) == 2
    assert "原始业务问题（业务语义不可改变）" in requests[1].question
    assert "SQL 技术修复反馈" in requests[1].question
    assert requests[0].semantic_question == "查询 2020 到 2026 年高速 NOA 配置率"
    assert requests[1].semantic_question == requests[0].semantic_question
    assert "技术修复已自动重生成" in repaired
    assert "无需业务口径确认" in repaired
    assert "SELECT 2" in repaired


@pytest.mark.asyncio
async def test_prescriptive_technical_revision_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import tools.database.sql_generate_tool as module

    requests: list[str] = []

    async def fake_generate(_session: object, request: Any) -> DatabaseSqlGenerationResult:
        requests.append(request.question)
        return _result(request.question)

    monkeypatch.setattr(module, "get_sessionmaker", lambda: _FakeSessionMaker())
    monkeypatch.setattr(module, "generate_database_sql", fake_generate)
    tool = DatabaseSqlGenerateTool(session_id="session-prescriptive", query_id="query-prescriptive")

    original = await tool._arun(question="查询 L2 配置率")
    generation_id = next(
        line.split("：", 1)[1]
        for line in original.splitlines()
        if line.startswith("- generation_id：")
    )
    rejected = await tool._arun(
        question="ignored",
        parent_generation_id=generation_id,
        revision_instruction="请改用 LEFT JOIN，并使用 type_name='辅助驾驶系统'。",
    )

    assert len(requests) == 1
    assert "只能反馈已观察到的问题" in rejected
    assert "不能指导使用哪个字段、表、实体或 SQL 实现" in rejected


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
            "revision_instruction": "分母改为车系粒度",
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
        revision_instruction="分母改为车系粒度",
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
    receipt_id = next(
        line.split("：", 1)[1]
        for line in validate_output.splitlines()
        if line.startswith("- validation_receipt_id：")
    )
    execute_output = await DatabaseSqlExecuteTool(session_id="session-block")._arun(
        generation_id=generation.id,
        validation_receipt_id=receipt_id,
    )

    assert "SQL 校验通过" in validate_output
    assert "SELECT 1" in validate_output
    assert "SELECT 2" not in validate_output
    assert "SQL 执行结果" in execute_output
    assert executed_sql == ["SELECT 1"]


@pytest.mark.asyncio
async def test_validator_replays_semantic_guardrails_and_withholds_receipt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import tools.database.sql_validate_tool as validate_module
    from graph.database_sql_revision_resume import database_sql_revision_resume_registry

    semantic_result = _result(
        "高压平台配置率",
        sql=(
            "SELECT type_value, COUNT(*) FROM vehicle_params "
            "WHERE type_name = '高压快充平台' AND type_value = '400V' GROUP BY type_value"
        ),
    )
    semantic_result.semantic_assets = {
        "matched": [{"id": "measure:config_rate", "name": "配置率", "type": "measure"}],
        "references": [],
    }
    generation = database_sql_revision_resume_registry.register_generation(
        session_id="session-semantic-validator",
        query_id="query-semantic-validator",
        result=semantic_result,
        request={"question": "高压平台配置率", "table_names": ["vehicle_params"]},
    )

    async def fake_resolve(_source_id: str | None, _tables: list[str] | None) -> tuple[dict, dict, list[str]]:
        return {}, {"id": "source-1", "name": "测试库", "database": "test"}, ["vehicle_params"]

    monkeypatch.setattr(validate_module, "resolve_database_source_scope", fake_resolve)

    output = await DatabaseSqlValidateTool(session_id="session-semantic-validator")._arun(
        generation_id=generation.id,
    )

    assert "SQL 语义校验失败" in output
    assert "voltage_platform_400v_physical_value" in output
    assert "未签发 Receipt" in output
    assert "parent_generation_id" in output


@pytest.mark.asyncio
async def test_agent_mode_execute_refuses_missing_cross_session_and_mismatched_receipts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import tools.database.sql_execute_tool as execute_module
    from graph.database_sql_revision_resume import database_sql_revision_resume_registry

    generation = database_sql_revision_resume_registry.register_generation(
        session_id="session-receipt-a",
        query_id="query-receipt-a",
        result=_result("原问题", sql="SELECT 1"),
        request={"question": "原问题", "table_names": ["vehicle_params"]},
    )
    other_generation = database_sql_revision_resume_registry.register_generation(
        session_id="session-receipt-a",
        query_id="query-receipt-b",
        result=_result("另一个问题", sql="SELECT 2"),
        request={"question": "另一个问题", "table_names": ["vehicle_params"]},
    )
    mismatched_receipt = database_sql_revision_resume_registry.register_validation_receipt(
        generation=other_generation,
        database_source_id="source-1",
        allowed_tables=["vehicle_params"],
    )
    cross_session_generation = database_sql_revision_resume_registry.register_generation(
        session_id="session-receipt-b",
        query_id="query-receipt-c",
        result=_result("跨会话问题", sql="SELECT 3"),
        request={"question": "跨会话问题", "table_names": ["vehicle_params"]},
    )
    cross_session_receipt = database_sql_revision_resume_registry.register_validation_receipt(
        generation=cross_session_generation,
        database_source_id="source-1",
        allowed_tables=["vehicle_params"],
    )

    async def unexpected_resolve(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("invalid receipts must be refused before database access")

    monkeypatch.setattr(execute_module, "resolve_database_source_scope", unexpected_resolve)
    tool = DatabaseSqlExecuteTool(session_id="session-receipt-a")

    missing = await tool._arun(generation_id=generation.id)
    mismatched = await tool._arun(
        generation_id=generation.id,
        validation_receipt_id=mismatched_receipt.id,
    )
    cross_session = await tool._arun(
        generation_id=generation.id,
        validation_receipt_id=cross_session_receipt.id,
    )

    assert "缺少当前会话有效" in missing
    assert "不匹配" in mismatched
    assert "缺少当前会话有效" in cross_session


def test_sql_generation_and_validation_receipt_survive_registry_memory_reset(
    tmp_path,
) -> None:
    from graph.database_sql_revision_resume import database_sql_revision_resume_registry
    from graph.session_manager import session_manager

    session_manager.initialize(tmp_path)
    session_manager.create_session("session-sql-ledger")
    generation = database_sql_revision_resume_registry.register_generation(
        session_id="session-sql-ledger",
        query_id="query-ledger",
        result=_result("原问题", sql="SELECT 1"),
        request={"question": "原问题", "table_names": ["vehicle_params"]},
    )
    receipt = database_sql_revision_resume_registry.register_validation_receipt(
        generation=generation,
        database_source_id="source-1",
        allowed_tables=["vehicle_params"],
    )
    database_sql_revision_resume_registry._generations.pop(generation.id)
    database_sql_revision_resume_registry._validation_receipts.pop(receipt.id)

    restored_generation = database_sql_revision_resume_registry.get_generation(
        generation.id,
        session_id="session-sql-ledger",
    )
    restored_receipt = database_sql_revision_resume_registry.get_validation_receipt(
        receipt.id,
        session_id="session-sql-ledger",
    )

    assert restored_generation is not None
    assert restored_generation.result.sql == "SELECT 1"
    assert restored_generation.sql_sha256 == generation.sql_sha256
    assert restored_receipt is not None
    assert restored_receipt.generation_id == generation.id
    assert restored_receipt.sql_sha256 == generation.sql_sha256
    assert restored_receipt.semantic_validation_status == "passed"
    assert restored_receipt.validator_version == "readonly+semantic-guardrails/v2"


def test_legacy_validation_receipt_is_not_silently_upgraded(tmp_path) -> None:
    from graph.session_manager import session_manager

    session_manager.initialize(tmp_path)
    session_manager.create_session("session-legacy-receipt")
    session_manager.record_sql_validation_receipt(
        "session-legacy-receipt",
        "sql-validation-legacy",
        {
            "id": "sql-validation-legacy",
            "session_id": "session-legacy-receipt",
            "query_id": "query-legacy",
            "generation_id": "sql-gen-legacy",
            "sql_sha256": "sha256:legacy",
            "database_source_id": "source-1",
            "allowed_tables": ["vehicle_params"],
            "validator_version": "readonly-sql/v1",
            "created_at": 1.0,
        },
    )
    registry = DatabaseSqlRevisionResumeRegistry()

    restored = registry.get_validation_receipt(
        "sql-validation-legacy",
        session_id="session-legacy-receipt",
    )

    assert restored is not None
    assert restored.semantic_validation_status == "legacy_unverified"
    assert restored.validator_version == "readonly-sql/v1"
