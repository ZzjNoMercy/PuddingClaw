from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

from deepagents import create_deep_agent
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.tools import tool
from pydantic import PrivateAttr

from graph.deepagents_manager import (
    PuddingClawAgentState,
    PuddingClawRubricMiddleware,
)
from graph.middlewares.tool_protocol import ToolProtocolIntegrityMiddleware
from graph.session_manager import session_manager
from harness.coordinators import HarnessRunCoordinator
from harness.models import GoalCompletionPolicy, RunStatus
from harness.verification_activations import VerificationActivationMiddleware


class ScriptedModel(BaseChatModel):
    _responses: list[AIMessage] = PrivateAttr()
    _calls: int = PrivateAttr(default=0)
    _received_messages: list[list[Any]] = PrivateAttr(default_factory=list)

    def __init__(self, responses: list[AIMessage]) -> None:
        super().__init__()
        self._responses = responses

    @property
    def _llm_type(self) -> str:
        return "effective_verification_scripted"

    def bind_tools(self, tools: list[Any], **kwargs: Any) -> ScriptedModel:
        return self

    def _generate(
        self,
        messages: list[Any],
        stop: list[str] | None = None,
        run_manager: Any = None,
        **kwargs: Any,
    ) -> ChatResult:
        self._received_messages.append(list(messages))
        try:
            message = self._responses[self._calls]
        except IndexError:
            message = AIMessage(content=f"UNSCRIPTED_{self._calls + 1}")
        self._calls += 1
        return ChatResult(generations=[ChatGeneration(message=message)])


@tool("database_sql_execute")
def fake_database_sql_execute(question: str) -> str:
    """Return deterministic analytics evidence."""

    return f"query completed: {question}\ndatabase_source_id: db-sales\nresult_id: result-sales-1\nrows: 12"


@tool("fetch_url")
def fake_fetch_url(url: str) -> str:
    """Return deterministic web evidence."""

    return f"page fetched: {url}"


def _grader_model(
    *criterion_ids: str,
    explanation: str = "已核对最终交付及其实际证据，本次启用的验收项均有结果支持。",
) -> ScriptedModel:
    criteria = ",".join(f'{{"name":"{criterion_id}","passed":true}}' for criterion_id in criterion_ids)
    return ScriptedModel(
        [
            AIMessage(
                content=(
                    f'{{"result":"satisfied","explanation":"{explanation}",'
                    f'"criteria":[{criteria}]}}'
                )
            )
        ]
    )


def _start_run(tmp_path, *, objective: str, analytics_model_id: str | None = None):
    session_manager.initialize(tmp_path)
    session_manager.create_session("e2e-session")
    coordinator = HarnessRunCoordinator(session_manager)
    run, goal = coordinator.start_run(
        session_id="e2e-session",
        query_id="query-current",
        objective=objective,
        goal_mode=True,
        analytics_model_id=analytics_model_id,
        completion_policy=GoalCompletionPolicy.RUBRIC,
    )
    coordinator.transition(run, RunStatus.RUNNING)
    assert goal is not None
    session_manager.record_goal_completion_request(
        run.session_id,
        goal_id=goal.goal_id,
        objective_revision=goal.objective_revision,
        run_id=run.run_id,
        tool_call_id=f"complete-{run.run_id}",
    )
    return coordinator, run, goal


def _rubric_context(
    tmp_path,
    *,
    objective: str,
    session_id: str,
    query_id: str,
) -> dict[str, str]:
    session_manager.initialize(tmp_path)
    session_manager.create_session(session_id)
    coordinator = HarnessRunCoordinator(session_manager)
    run, goal = coordinator.start_run(
        session_id=session_id,
        query_id=query_id,
        objective=objective,
        goal_mode=True,
        completion_policy=GoalCompletionPolicy.RUBRIC,
    )
    assert goal is not None
    coordinator.transition(run, RunStatus.RUNNING)
    session_manager.record_goal_completion_request(
        session_id,
        goal_id=goal.goal_id,
        objective_revision=goal.objective_revision,
        run_id=run.run_id,
        tool_call_id=f"complete-{run.run_id}",
    )
    return {
        "session_id": session_id,
        "run_id": run.run_id,
        "query_id": query_id,
        "workspace_path": str(tmp_path),
        "run_objective": objective,
    }


def test_runtime_context_scopes_grader_to_current_run(tmp_path, monkeypatch):
    from harness.rubric_compiler import RubricBuildContext, RunRubricCompiler

    runtime_context = _rubric_context(
        tmp_path,
        objective="L6 年度改款多少钱？",
        session_id="scope-session",
        query_id="scope-query",
    )
    contract = RunRubricCompiler.compile(RubricBuildContext(user_message="L6 年度改款多少钱？", force_required=True))
    assert contract is not None
    main_model = ScriptedModel([AIMessage(content="L6 售价 24.98 万元。")])
    grader_model = _grader_model("task_fulfillment", "todo_reconciliation")
    monkeypatch.setattr(
        PuddingClawRubricMiddleware,
        "_effective_contract_update",
        staticmethod(lambda _state, _runtime: {}),
    )
    agent = create_deep_agent(
        model=main_model,
        tools=[],
        middleware=[PuddingClawRubricMiddleware(model=grader_model, max_iterations=2)],
        state_schema=PuddingClawAgentState,
    )
    messages = [
        HumanMessage(content="aihot技能有更新吗？帮我重新安装一次"),
        AIMessage(content="aihot 更新完成"),
        HumanMessage(
            content="L6 年度改款多少钱？",
            additional_kwargs={"puddingclaw_query_id": "scope-query"},
        ),
    ]

    asyncio.run(
        agent.ainvoke(
            {
                "messages": messages,
                "rubric": contract.rubric,
                "verification_contract": contract.model_dump(mode="json"),
            },
            context=runtime_context,
        )
    )

    main_prompt = "\n".join(str(getattr(message, "content", "")) for message in main_model._received_messages[0])
    grader_prompt = "\n".join(str(getattr(message, "content", "")) for message in grader_model._received_messages[0])
    assert "aihot技能有更新吗" in main_prompt
    assert "L6 年度改款多少钱" in grader_prompt
    assert "L6 售价 24.98 万元" in grader_prompt
    assert "aihot技能有更新吗" not in grader_prompt
    assert "aihot 更新完成" not in grader_prompt


def test_runtime_context_restores_objective_after_user_turn_is_summarized(
    tmp_path,
    monkeypatch,
):
    from harness.rubric_compiler import RubricBuildContext, RunRubricCompiler

    runtime_context = _rubric_context(
        tmp_path,
        objective="L6 年度改款多少钱？",
        session_id="summarized-scope-session",
        query_id="summarized-scope-query",
    )
    contract = RunRubricCompiler.compile(RubricBuildContext(user_message="L6 年度改款多少钱？", force_required=True))
    assert contract is not None
    main_model = ScriptedModel([AIMessage(content="L6 售价 24.98 万元。")])
    grader_model = _grader_model("task_fulfillment", "todo_reconciliation")
    monkeypatch.setattr(
        PuddingClawRubricMiddleware,
        "_effective_contract_update",
        staticmethod(lambda _state, _runtime: {}),
    )
    agent = create_deep_agent(
        model=main_model,
        tools=[],
        middleware=[PuddingClawRubricMiddleware(model=grader_model, max_iterations=2)],
        state_schema=PuddingClawAgentState,
    )

    asyncio.run(
        agent.ainvoke(
            {
                "messages": [
                    HumanMessage(
                        content="摘要中包含旧任务：重新安装 aihot",
                        additional_kwargs={"lc_source": "summarization"},
                    )
                ],
                "rubric": contract.rubric,
                "verification_contract": contract.model_dump(mode="json"),
            },
            context=runtime_context,
        )
    )

    grader_prompt = "\n".join(str(getattr(message, "content", "")) for message in grader_model._received_messages[0])
    assert "L6 年度改款多少钱" in grader_prompt
    assert "L6 售价 24.98 万元" in grader_prompt
    assert "重新安装 aihot" not in grader_prompt


def test_run_scope_survives_one_explicit_rubric_revision(tmp_path, monkeypatch):
    from harness.rubric_compiler import RubricBuildContext, RunRubricCompiler

    runtime_context = _rubric_context(
        tmp_path,
        objective="L6 年度改款多少钱？",
        session_id="revision-session",
        query_id="revision-query",
    )
    contract = RunRubricCompiler.compile(RubricBuildContext(user_message="L6 年度改款多少钱？", force_required=True))
    assert contract is not None
    main_model = ScriptedModel(
        [
            AIMessage(content="L6 售价 24.98 万元。"),
            AIMessage(content="L6 售价 24.98 万元，置换 23.48 万元起。"),
        ]
    )
    grader_model = ScriptedModel(
        [
            AIMessage(
                content=(
                    '{"result":"needs_revision","explanation":"补充置换价",'
                    '"criteria":[{"name":"task_fulfillment","passed":false,'
                    '"gap":"缺少置换价格"},{"name":"todo_reconciliation",'
                    '"passed":true}]}'
                )
            ),
            AIMessage(
                content=(
                    '{"result":"satisfied","explanation":"已完成",'
                    '"criteria":[{"name":"task_fulfillment","passed":true},'
                    '{"name":"todo_reconciliation","passed":true}]}'
                )
            ),
        ]
    )
    monkeypatch.setattr(
        PuddingClawRubricMiddleware,
        "_effective_contract_update",
        staticmethod(lambda _state, _runtime: {}),
    )
    agent = create_deep_agent(
        model=main_model,
        tools=[],
        middleware=[PuddingClawRubricMiddleware(model=grader_model, max_iterations=3)],
        state_schema=PuddingClawAgentState,
    )

    asyncio.run(
        agent.ainvoke(
            {
                "messages": [
                    HumanMessage(content="aihot技能有更新吗？帮我重新安装一次"),
                    AIMessage(content="aihot 更新完成"),
                    HumanMessage(
                        content="L6 年度改款多少钱？",
                        additional_kwargs={"puddingclaw_query_id": "revision-query"},
                    ),
                ],
                "rubric": contract.rubric,
                "verification_contract": contract.model_dump(mode="json"),
            },
            context=runtime_context,
        )
    )

    # A rejected request gets one repair jump. The repaired answer must submit
    # a new completion request before grading can run again.
    assert len(grader_model._received_messages) == 1
    for received in grader_model._received_messages:
        prompt = "\n".join(str(getattr(message, "content", "")) for message in received)
        assert "L6 年度改款多少钱" in prompt
        assert "aihot技能有更新吗" not in prompt
        assert "aihot 更新完成" not in prompt
    request_id = session_manager.get_run_state(
        runtime_context["session_id"], runtime_context["run_id"]
    )["completion_request_id"]
    request = session_manager.get_harness_state(runtime_context["session_id"])[
        "completion_requests"
    ][request_id]
    assert request["status"] == "needs_revision"


def test_rubric_revision_requires_a_new_completion_request(tmp_path, monkeypatch):
    """A repair response alone cannot silently resubmit completion."""

    from harness.rubric_compiler import RubricBuildContext, RunRubricCompiler

    runtime_context = _rubric_context(
        tmp_path,
        objective="完成这个回答",
        session_id="iteration-session",
        query_id="iteration-query",
    )
    contract = RunRubricCompiler.compile(
        RubricBuildContext(user_message="完成这个回答", force_required=True)
    )
    assert contract is not None
    main_model = ScriptedModel(
        [
            AIMessage(content="第一次申请完成。"),
            AIMessage(content="第二次申请完成。"),
            AIMessage(content="最终完成。"),
        ]
    )
    grader_model = ScriptedModel(
        [
            AIMessage(content=(
                '{"result":"needs_revision","explanation":"缺口一",'
                '"criteria":[{"name":"task_fulfillment","passed":false,"gap":"缺口一"},'
                '{"name":"todo_reconciliation","passed":true}]}'
            )),
            AIMessage(content=(
                '{"result":"failed","explanation":"缺口二",'
                '"criteria":[{"name":"task_fulfillment","passed":false,"gap":"缺口二"},'
                '{"name":"todo_reconciliation","passed":true}]}'
            )),
            AIMessage(content=(
                '{"result":"satisfied","explanation":"通过",'
                '"criteria":[{"name":"task_fulfillment","passed":true},'
                '{"name":"todo_reconciliation","passed":true}]}'
            )),
        ]
    )
    monkeypatch.setattr(
        PuddingClawRubricMiddleware,
        "_effective_contract_update",
        staticmethod(lambda _state, _runtime: {}),
    )
    evaluations = []
    agent = create_deep_agent(
        model=main_model,
        tools=[],
        middleware=[PuddingClawRubricMiddleware(
            model=grader_model,
            max_iterations=1,
            on_evaluation=evaluations.append,
        )],
        state_schema=PuddingClawAgentState,
    )

    final_state = asyncio.run(
        agent.ainvoke(
            {
                "messages": [HumanMessage(content="完成这个回答")],
                "rubric": contract.rubric,
                "verification_contract": contract.model_dump(mode="json"),
            },
            context=runtime_context,
        )
    )

    assert main_model._calls == 2
    assert grader_model._calls == 1
    assert [item["result"] for item in evaluations] == ["needs_revision"]
    assert final_state["messages"][-1].content == "第二次申请完成。"


def test_rubric_revision_jump_repairs_hidden_provider_tool_call(tmp_path, monkeypatch):
    """The Rubric jump must not bypass the last model-boundary protocol guard."""

    from harness.rubric_compiler import RubricBuildContext, RunRubricCompiler

    runtime_context = _rubric_context(
        tmp_path,
        objective="修改报告",
        session_id="protocol-session",
        query_id="protocol-query",
    )
    contract = RunRubricCompiler.compile(RubricBuildContext(user_message="修改报告", force_required=True))
    assert contract is not None
    main_model = ScriptedModel(
        [
            AIMessage(
                content="",
                invalid_tool_calls=[
                    {
                        "id": "call-hidden",
                        "name": "patch_file",
                        "args": "{broken",
                        "error": "invalid json",
                        "type": "invalid_tool_call",
                    }
                ],
            ),
            AIMessage(content="报告已修正。"),
        ]
    )
    grader_model = ScriptedModel(
        [
            AIMessage(
                content=(
                    '{"result":"needs_revision","explanation":"继续修正",'
                    '"criteria":[{"name":"task_fulfillment","passed":false,'
                    '"gap":"尚未完成"},{"name":"todo_reconciliation","passed":true}]}'
                )
            ),
            AIMessage(
                content=(
                    '{"result":"satisfied","explanation":"已完成",'
                    '"criteria":[{"name":"task_fulfillment","passed":true},'
                    '{"name":"todo_reconciliation","passed":true}]}'
                )
            ),
        ]
    )
    monkeypatch.setattr(
        PuddingClawRubricMiddleware,
        "_effective_contract_update",
        staticmethod(lambda _state, _runtime: {}),
    )
    agent = create_deep_agent(
        model=main_model,
        tools=[],
        middleware=[
            PuddingClawRubricMiddleware(model=grader_model, max_iterations=3),
            ToolProtocolIntegrityMiddleware(emit_context_usage=False),
        ],
        state_schema=PuddingClawAgentState,
    )

    asyncio.run(
        agent.ainvoke(
            {
                "messages": [HumanMessage(content="修改报告")],
                "rubric": contract.rubric,
                "verification_contract": contract.model_dump(mode="json"),
            },
            context=runtime_context,
        )
    )

    assert len(main_model._received_messages) == 2
    second_request = main_model._received_messages[1]
    hidden_index = next(
        index
        for index, message in enumerate(second_request)
        if isinstance(message, AIMessage) and message.invalid_tool_calls
    )
    assert isinstance(second_request[hidden_index + 1], ToolMessage)
    assert second_request[hidden_index + 1].tool_call_id == "call-hidden"
    assert isinstance(second_request[hidden_index + 2], HumanMessage)
    assert second_request[hidden_index + 2].name == "rubric_grader"


def test_runtime_database_tool_upgrades_contract_before_grader(tmp_path):
    coordinator, run, goal = _start_run(tmp_path, objective="继续处理")
    assert run.verification_contract is not None
    assert run.verification_contract.verification_packs == ["core"]
    main_model = ScriptedModel(
        [
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "database_sql_execute",
                        "args": {"question": "查询销量"},
                        "id": "call-db",
                    }
                ],
            ),
            AIMessage(content="销量查询已完成。"),
        ]
    )
    evaluations = []
    grader_model = _grader_model(
        "task_fulfillment",
        "todo_reconciliation",
        "metric_consistency",
        "analytics_evidence_traceability",
        explanation=(
            "已核对最终回答确实返回 12 行销量结果；数据库查询成功完成，"
            "结果可追溯到已登记的数据源。"
        ),
    )
    agent = create_deep_agent(
        model=main_model,
        tools=[fake_database_sql_execute],
        middleware=[
            VerificationActivationMiddleware(),
            PuddingClawRubricMiddleware(
                model=grader_model,
                max_iterations=2,
                on_evaluation=evaluations.append,
            ),
        ],
        state_schema=PuddingClawAgentState,
    )

    final_state = asyncio.run(
        agent.ainvoke(
            {
                "messages": [{"role": "user", "content": "继续处理"}],
                "task_profile": run.task_profile.model_dump(mode="json"),
            },
            context={
                "session_id": run.session_id,
                "run_id": run.run_id,
                "query_id": run.query_id,
                "workspace_path": str(tmp_path),
            },
        )
    )
    assert evaluations
    grader_prompt = "\n".join(str(getattr(message, "content", "")) for message in grader_model._received_messages[0])
    assert "[metric_consistency]" in grader_prompt
    assert "[analytics_evidence_traceability]" in grader_prompt
    final_state["_rubric_status"] = evaluations[-1]["result"]
    final_state["_rubric_evaluations"] = evaluations
    completed, _, report = coordinator.complete_from_final_state(
        run,
        goal,
        final_state,
    )

    assert completed.verification_contract is not None
    assert "analytics" in completed.verification_contract.verification_packs
    assert {
        "metric_consistency",
        "analytics_evidence_traceability",
    } <= {item.id for item in completed.verification_contract.criteria}
    assert report.status.value == "satisfied"
    assert completed.verification_activations[0].tool_call_id == "call-db"


def test_runtime_fetch_url_activates_web_without_selected_model_pollution(tmp_path):
    coordinator, run, goal = _start_run(
        tmp_path,
        objective="继续整理",
        analytics_model_id="selected-analytics-model",
    )
    assert run.verification_contract is not None
    assert run.verification_contract.verification_packs == ["core"]
    main_model = ScriptedModel(
        [
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "fetch_url",
                        "args": {"url": "https://example.com/news"},
                        "id": "call-web",
                    }
                ],
            ),
            AIMessage(content="今天有一条新闻：https://example.com/news"),
        ]
    )
    evaluations = []
    agent = create_deep_agent(
        model=main_model,
        tools=[fake_fetch_url],
        middleware=[
            VerificationActivationMiddleware(),
            PuddingClawRubricMiddleware(
                model=_grader_model(
                    "task_fulfillment",
                    "todo_reconciliation",
                    "web_evidence_traceability",
                ),
                max_iterations=2,
                on_evaluation=evaluations.append,
            ),
        ],
        state_schema=PuddingClawAgentState,
    )

    final_state = asyncio.run(
        agent.ainvoke(
            {
                "messages": [{"role": "user", "content": run.objective}],
                "task_profile": run.task_profile.model_dump(mode="json"),
            },
            context={
                "session_id": run.session_id,
                "run_id": run.run_id,
                "query_id": run.query_id,
                "workspace_path": str(tmp_path),
            },
        )
    )
    assert evaluations
    final_state["_rubric_status"] = evaluations[-1]["result"]
    final_state["_rubric_evaluations"] = evaluations
    completed, _, report = coordinator.complete_from_final_state(
        run,
        goal,
        final_state,
    )

    assert completed.verification_contract is not None
    assert "web_research" in completed.verification_contract.verification_packs
    assert "analytics" not in completed.verification_contract.verification_packs
    assert "metric_consistency" not in {item.id for item in completed.verification_contract.criteria}
    assert report.status.value == "satisfied"


def test_manager_loads_selected_model_context_without_enabling_analytics_verification(
    tmp_path,
    monkeypatch,
):
    from graph import deepagents_manager as manager_module
    from projects.registry import project_registry

    session_manager.initialize(tmp_path)
    project_registry.initialize(tmp_path)
    session_manager.create_session("manager-context-session")
    captured = {}

    class FakeDeepAgent:
        async def astream(self, *_args, **_kwargs):
            yield ("values", {"messages": [AIMessage(content="这是通用解释。")]})

    def fake_create_deep_agent(**kwargs):
        captured.update(kwargs)
        return FakeDeepAgent()

    monkeypatch.setattr(manager_module, "create_deep_agent", fake_create_deep_agent)
    passive_model = ScriptedModel([AIMessage(content="unused")])

    def fake_model_client(*_args, **kwargs):
        return passive_model

    monkeypatch.setattr(manager_module, "ModelClientChatModel", fake_model_client)
    monkeypatch.setattr(
        manager_module.DeepAgentsAgentManager,
        "_analytics_model_context",
        lambda self, model_id: (
            "\n\nANALYTICS_FULL_CONTEXT",
            {"id": model_id},
        ),
    )

    async def no_title(_session_id: str):
        return None

    monkeypatch.setattr(manager_module, "_generate_title", no_title)
    runtime = manager_module.DeepAgentsAgentManager()
    runtime.initialize(Path(tmp_path))

    async def collect():
        return [
            event
            async for event in runtime.astream(
                message="解释一下 RubricMiddleware 是什么",
                session_id="manager-context-session",
                analytics_model_id="selected-model",
                project_id=None,
                user_id="test-user",
            )
        ]

    asyncio.run(collect())
    persisted = session_manager.get_run_state("manager-context-session")

    assert "ANALYTICS_FULL_CONTEXT" in captured["system_prompt"]
    assert captured["subagents"]
    assert all(
        any(isinstance(item, VerificationActivationMiddleware) for item in subagent.get("middleware", []))
        for subagent in captured["subagents"]
    )
    assert persisted is not None
    assert persisted["task_profile"]["primary_intent"] == "general"
    assert persisted["task_profile"]["classifier"] == "deterministic_fallback"
    assert persisted["verification_contract"] is None


def test_manager_standard_run_persists_activation_without_rubric_contract(
    tmp_path,
    monkeypatch,
):
    from graph import deepagents_manager as manager_module
    from projects.registry import project_registry

    session_manager.initialize(tmp_path)
    project_registry.initialize(tmp_path)
    session_manager.create_session("manager-dynamic-session")
    skill_dir = Path(tmp_path) / "skills" / "database-analysis"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        (
            "---\n"
            "name: database-analysis\n"
            "description: Query configured relational data.\n"
            "toolsets:\n"
            "  - database_analysis\n"
            "---\n\n"
            "# Database Analysis\n"
        ),
        encoding="utf-8",
    )
    main_model = ScriptedModel(
        [
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "read_file",
                        "args": {"file_path": "/skills/database-analysis/SKILL.md"},
                        "id": "call-read-skill",
                    }
                ],
            ),
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "database_sql_execute",
                        "args": {"question": "查询销量"},
                        "id": "call-db-manager",
                    }
                ],
            ),
            AIMessage(content="销量查询完成，结果为 12 行。"),
        ]
    )
    grader_model = _grader_model(
        "task_fulfillment",
        "todo_reconciliation",
        "metric_consistency",
        "analytics_evidence_traceability",
        explanation=(
            "已核对最终回答确实返回 12 行销量结果；数据库查询成功完成，"
            "结果可追溯到已登记的数据源。"
        ),
    )

    def fake_model_client(*_args, **kwargs):
        return grader_model if kwargs.get("role") == "rubric" else main_model

    async def no_title(_session_id: str):
        return None

    monkeypatch.setattr(
        manager_module,
        "ModelClientChatModel",
        fake_model_client,
    )
    monkeypatch.setattr(
        manager_module.DeepAgentsAgentManager,
        "_build_tools",
        lambda self, *_args, **_kwargs: [fake_database_sql_execute],
    )
    monkeypatch.setattr(
        manager_module,
        "_build_subagents",
        lambda *_args, **_kwargs: [],
    )
    monkeypatch.setattr(manager_module, "_generate_title", no_title)
    runtime = manager_module.DeepAgentsAgentManager()
    runtime.initialize(Path(tmp_path))

    async def collect():
        return [
            event
            async for event in runtime.astream(
                message="继续处理",
                session_id="manager-dynamic-session",
                analytics_model_id="selected-model",
                project_id=None,
                user_id="test-user",
            )
        ]

    events = asyncio.run(collect())
    persisted = session_manager.get_run_state("manager-dynamic-session")

    assert events
    assert persisted is not None
    assert persisted["verification_activations"], persisted
    assert persisted["outcome"] == "completed"
    assert persisted["verification_report"]["status"] == "not_required"
    assert persisted["declared_verification_contract"] is None
    assert persisted["verification_contract"] is None
    assert persisted["verification_activations"][0]["tool_call_id"] == ("call-db-manager")
    evidence = persisted["verification_activations"][0]["evidence_refs"]
    assert any(item.get("kind") == "tool_result" for item in evidence)
    event_payload = "\n".join(f"{event.get('event')}:{event.get('data')}" for event in events)
    assert "verification_activation_recorded" in event_payload
    assert "rubric_evaluation_start" not in event_payload
    event_names = [event.get("event") for event in events]
    assert "token" in event_names
    assert event_names.count("final_response") == 1
    assert event_names.index("final_response") < event_names.index("done")
    final_payload = json.loads(
        next(event["data"] for event in events if event["event"] == "final_response")
    )
    assert not final_payload.get("verification_summary")
    session = session_manager.load_session("manager-dynamic-session")
    assistant = next(
        item
        for item in reversed(session)
        if item.get("role") == "assistant"
    )
    assert assistant["content"] == "销量查询完成，结果为 12 行。"
    assert not assistant.get("verification_summary")
    assert "verification_state" not in assistant["segments"][-1]
    assert not any(
        item.get("type") == "activity"
        and item.get("label") in {"正在核对完成质量", "完成质量检查通过"}
        for item in assistant["timeline"]
    )
    assert grader_model._received_messages == []
