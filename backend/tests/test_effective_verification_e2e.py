from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from deepagents import create_deep_agent
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.tools import tool
from pydantic import PrivateAttr

from graph.deepagents_manager import (
    PuddingClawAgentState,
    PuddingClawRubricMiddleware,
)
from graph.session_manager import session_manager
from harness.coordinators import HarnessRunCoordinator
from harness.models import RunStatus
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

    return (
        f"query completed: {question}\n"
        "database_source_id: db-sales\n"
        "result_id: result-sales-1\n"
        "rows: 12"
    )


@tool("fetch_url")
def fake_fetch_url(url: str) -> str:
    """Return deterministic web evidence."""

    return f"page fetched: {url}"


def _grader_model(*criterion_ids: str) -> ScriptedModel:
    criteria = ",".join(
        f'{{"name":"{criterion_id}","passed":true}}'
        for criterion_id in criterion_ids
    )
    return ScriptedModel(
        [
            AIMessage(
                content=(
                    '{"result":"satisfied","explanation":"全部必需项通过",'
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
        goal_mode=False,
        analytics_model_id=analytics_model_id,
    )
    coordinator.transition(run, RunStatus.RUNNING)
    return coordinator, run, goal


def test_runtime_database_tool_upgrades_contract_before_grader(tmp_path):
    coordinator, run, goal = _start_run(tmp_path, objective="继续处理")
    assert run.verification_contract is None
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
    grader_prompt = "\n".join(
        str(getattr(message, "content", ""))
        for message in grader_model._received_messages[0]
    )
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
    assert run.verification_contract is None
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
    assert "metric_consistency" not in {
        item.id for item in completed.verification_contract.criteria
    }
    assert report.status.value == "satisfied"


def test_manager_does_not_inject_full_selected_model_context_into_general_run(
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

    assert "ANALYTICS_FULL_CONTEXT" not in captured["system_prompt"]
    assert captured["subagents"]
    assert all(
        any(
            isinstance(item, VerificationActivationMiddleware)
            for item in subagent.get("middleware", [])
        )
        for subagent in captured["subagents"]
    )
    assert persisted is not None
    assert persisted["task_profile"]["primary_intent"] == "general"
    assert persisted["verification_contract"] is None


def test_manager_runtime_database_action_persists_effective_contract(
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
                        "args": {
                            "file_path": "/skills/database-analysis/SKILL.md"
                        },
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
    assert persisted["verification_report"]["status"] == "satisfied"
    assert "analytics" in persisted["verification_contract"]["verification_packs"]
    assert persisted["verification_activations"][0]["tool_call_id"] == (
        "call-db-manager"
    )
    evidence = persisted["verification_activations"][0]["evidence_refs"]
    assert any(item.get("kind") == "tool_result" for item in evidence)
    event_payload = "\n".join(
        f"{event.get('event')}:{event.get('data')}" for event in events
    )
    activation_index = event_payload.index("verification_activation_recorded")
    contract_index = event_payload.index("verification_contract_updated")
    report_index = event_payload.index("verification_report:")
    assert activation_index < contract_index < report_index
    grader_prompt = "\n".join(
        str(getattr(message, "content", ""))
        for message in grader_model._received_messages[0]
    )
    assert "[analytics_evidence_traceability]" in grader_prompt
