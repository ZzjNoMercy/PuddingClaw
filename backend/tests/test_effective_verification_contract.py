from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from langchain.agents.middleware.types import ToolCallRequest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langgraph.types import Command

from graph.session_manager import SessionManager
from harness.coordinators import CompletionVerificationCoordinator, HarnessRunCoordinator
from harness.deterministic_checks import evaluate_deterministic_criteria
from harness.models import (
    RunOutcome,
    RunRecord,
    RunStatus,
    RunTaskProfile,
    VerificationActivation,
    VerificationStatus,
)
from harness.rubric_compiler import RubricBuildContext, RunRubricCompiler
from harness.task_profiles import SemanticTaskProfileClassifier, TaskProfileClassifier
from harness.verification_activations import (
    VerificationActivationMiddleware,
    build_verification_activations,
    tool_result_succeeded,
    verification_packs_for_tool,
)


def _criterion_ids(contract) -> set[str]:
    return {item.id for item in contract.criteria} if contract else set()


def test_tool_protocol_readiness_requires_immediate_matching_tool_message():
    contract = RunRubricCompiler.compile(
        RubricBuildContext(user_message="完成任务", force_required=True)
    )
    assert contract is not None
    tool_call = {
        "name": "read_file",
        "args": {"file_path": "/workspace/report.md"},
        "id": "call-read",
    }
    proper = evaluate_deterministic_criteria(
        contract,
        {
            "messages": [
                AIMessage(content="", tool_calls=[tool_call]),
                ToolMessage(content="ok", tool_call_id="call-read"),
            ]
        },
    )
    late = evaluate_deterministic_criteria(
        contract,
        {
            "messages": [
                AIMessage(content="", tool_calls=[tool_call]),
                HumanMessage(content="continue"),
                ToolMessage(content="ok", tool_call_id="call-read"),
            ]
        },
    )

    assert next(item for item in proper if item.criterion_id == "tool_protocol_integrity").passed
    late_result = next(item for item in late if item.criterion_id == "tool_protocol_integrity")
    assert not late_result.passed
    assert "call-read" in str(late_result.gap)


@pytest.mark.parametrize(
    ("message", "intent", "expected_pack"),
    [
        ("分析今天 OpenAI 和 Anthropic 的新闻，附来源", "ai_insights", "web_research"),
        ("修改数据结构代码并运行 pytest", "code", "code"),
        ("解释一下 RubricMiddleware 是什么", "general", None),
    ],
)
def test_selected_analytics_model_is_context_not_task_intent(
    message,
    intent,
    expected_pack,
):
    profile = TaskProfileClassifier.classify(
        message=message,
        analytics_model_id="汽车行业分析模型",
    )

    assert profile.primary_intent == intent
    assert "analytics" not in profile.initial_packs
    assert profile.available_context_refs == ["analytics_model:汽车行业分析模型"]
    if expected_pack:
        assert expected_pack in profile.initial_packs


class _TaskClassifierModel:
    def __init__(self, content: str | None = None, error: Exception | None = None):
        self.content = content
        self.error = error
        self.received_messages = []

    async def ainvoke(self, messages):
        self.received_messages.append(messages)
        if self.error is not None:
            raise self.error
        return AIMessage(content=self.content or "")


@pytest.mark.asyncio
async def test_semantic_classifier_separates_work_nature_from_delivery_form():
    profile = await SemanticTaskProfileClassifier.classify(
        message=(
            "刷新产品配置分析报告到 2026 年，重算所有年份数据，并同步更新图表趋势。"
        ),
        analytics_model_id="产品配置分析",
        model=_TaskClassifierModel(
            json.dumps(
                {
                    "work_natures": ["重算业务指标并刷新分析报告"],
                    "delivery_forms": ["artifact"],
                    "verification_intents": ["database_analysis", "artifact"],
                    "evidence": {
                        "database_analysis": ["重算所有年份数据"],
                        "artifact": ["刷新产品配置分析报告到 2026 年"],
                    },
                    "skill_candidates": [
                        {
                            "skill_id": "database-analysis",
                            "confidence": 0.93,
                            "evidence": "重算所有年份数据",
                        }
                    ],
                    "explicit_skill_requests": [],
                },
                ensure_ascii=False,
            )
        ),
        skill_catalog=[
            {
                "skill_id": "database-analysis",
                "name": "database-analysis",
                "description": "Analyze relational data.",
            }
        ],
    )

    assert profile.primary_intent == "database_analysis"
    assert profile.work_natures == ["重算业务指标并刷新分析报告"]
    assert profile.verification_intents == ["database_analysis", "artifact"]
    assert profile.delivery_forms == ["artifact"]
    assert profile.intents == ["database_analysis", "artifact"]
    assert {"core", "analytics", "artifact"} <= set(profile.initial_packs)
    assert profile.available_context_refs == ["analytics_model:产品配置分析"]
    assert profile.classifier == "llm_semantic"
    assert [item.skill_id for item in profile.skill_candidates] == [
        "database-analysis"
    ]
    assert profile.execution_route == "skill_first"


@pytest.mark.asyncio
async def test_semantic_classifier_does_not_treat_selected_model_as_analytics_intent():
    profile = await SemanticTaskProfileClassifier.classify(
        message="解释一下 RubricMiddleware 是什么",
        analytics_model_id="产品配置分析",
        model=_TaskClassifierModel(
            '{"work_natures":["解释概念"],"delivery_forms":["answer"],'
            '"verification_intents":[],"evidence":{},"skill_candidates":[],'
            '"explicit_skill_requests":[]}'
        ),
        skill_catalog=[],
    )

    assert profile.primary_intent == "general"
    assert profile.delivery_forms == ["answer"]
    assert "analytics" not in profile.initial_packs
    assert profile.available_context_refs == ["analytics_model:产品配置分析"]


@pytest.mark.asyncio
async def test_semantic_classifier_falls_back_when_model_output_is_invalid():
    profile = await SemanticTaskProfileClassifier.classify(
        message="查询数据库中的月销量",
        analytics_model_id=None,
        model=_TaskClassifierModel("not-json"),
        skill_catalog=[],
    )

    assert profile.primary_intent == "database_analysis"
    assert profile.classifier == "deterministic_fallback"


@pytest.mark.asyncio
async def test_semantic_classifier_fallback_preserves_explicit_skill_request():
    profile = await SemanticTaskProfileClassifier.classify(
        message="使用 aihot Skill 和 missing-news Skill",
        analytics_model_id=None,
        model=_TaskClassifierModel(error=RuntimeError("provider unavailable")),
        skill_catalog=[
            {
                "skill_id": "aihot",
                "name": "aihot",
                "description": "Query current AI news.",
            }
        ],
    )

    assert [item.skill_id for item in profile.skill_candidates] == ["aihot"]
    assert profile.missing_explicit_skill_ids == ["missing-news"]


@pytest.mark.asyncio
async def test_semantic_classifier_routes_new_installed_skill_without_registry_entry():
    model = _TaskClassifierModel(
        json.dumps(
            {
                "work_natures": ["整理会议决策"],
                "delivery_forms": ["artifact"],
                "verification_intents": ["artifact"],
                "evidence": {"artifact": ["整理成决策日志"]},
                "skill_candidates": [
                    {
                        "skill_id": "decision-log",
                        "confidence": 0.91,
                        "evidence": "整理成决策日志",
                    },
                    {
                        "skill_id": "weak-match",
                        "confidence": 0.31,
                        "evidence": "会议记录",
                    },
                ],
                "explicit_skill_requests": [],
            },
            ensure_ascii=False,
        )
    )
    profile = await SemanticTaskProfileClassifier.classify(
        message="把这段会议记录整理成决策日志",
        analytics_model_id=None,
        model=model,
        skill_catalog=[
            {
                "skill_id": "decision-log",
                "name": "decision-log",
                "description": "Turn meeting notes into decision logs.",
            },
            {
                "skill_id": "weak-match",
                "name": "weak-match",
                "description": "A weakly related workflow.",
            },
        ],
    )

    assert [item.skill_id for item in profile.skill_candidates] == ["decision-log"]
    assert profile.execution_route == "skill_first"
    assert "decision-log" in str(model.received_messages[0][0].content)
    assert "Turn meeting notes into decision logs" in str(
        model.received_messages[0][0].content
    )


@pytest.mark.asyncio
async def test_semantic_classifier_prioritizes_explicit_skill_and_reports_missing():
    profile = await SemanticTaskProfileClassifier.classify(
        message="使用 aihot Skill，并同时使用 missing-news Skill",
        analytics_model_id=None,
        model=_TaskClassifierModel(
            json.dumps(
                {
                    "work_natures": ["查询 AI 新闻"],
                    "delivery_forms": ["answer"],
                    "verification_intents": ["ai_insights"],
                    "evidence": {"ai_insights": ["AI 新闻"]},
                    "skill_candidates": [],
                    "explicit_skill_requests": ["aihot", "missing-news"],
                },
                ensure_ascii=False,
            )
        ),
        skill_catalog=[
            {
                "skill_id": "aihot",
                "name": "aihot",
                "description": "Query current AI news.",
            }
        ],
    )

    assert profile.skill_candidates[0].skill_id == "aihot"
    assert profile.skill_candidates[0].explicit is True
    assert profile.missing_explicit_skill_ids == ["missing-news"]
    assert profile.native_fallback is False


def test_semantic_router_enhancement_only_adds_to_deterministic_baseline():
    baseline = TaskProfileClassifier.classify(
        message="更新报告",
        analytics_model_id="model-1",
        skill_catalog=[],
    )
    semantic = TaskProfileClassifier.profile_from_dimensions(
        work_natures=["重算产品配置指标"],
        delivery_forms=["answer"],
        verification_intents=["database_analysis"],
        skill_candidates=[
            {
                "skill_id": "database-analysis",
                "confidence": 0.92,
                "evidence": "重算产品配置指标",
            }
        ],
        analytics_model_id="model-1",
        classifier="llm_semantic",
    )

    merged = TaskProfileClassifier.merge_semantic_enhancement(
        baseline,
        semantic,
        analytics_model_id="model-1",
    )

    assert {"artifact", "database_analysis"} <= set(merged.intents)
    assert {"artifact", "analytics"} <= set(merged.initial_packs)
    assert [item.skill_id for item in merged.skill_candidates] == [
        "database-analysis"
    ]
    assert merged.available_context_refs == ["analytics_model:model-1"]
    assert merged.classifier == "llm_semantic"


def test_async_router_can_enhance_preparing_run_but_not_running_contract(tmp_path):
    sessions = SessionManager()
    sessions.initialize(tmp_path)
    sessions.create_session("router-enhancement-session")
    coordinator = HarnessRunCoordinator(sessions)
    baseline = TaskProfileClassifier.classify(message="更新报告")
    run, _ = coordinator.start_run(
        session_id="router-enhancement-session",
        query_id="query-router-enhancement",
        objective="更新报告并重算配置指标",
        goal_mode=False,
        task_profile=baseline,
    )
    semantic = TaskProfileClassifier.profile_from_dimensions(
        work_natures=["重算配置指标"],
        delivery_forms=["artifact"],
        verification_intents=["database_analysis"],
        classifier="llm_semantic",
    )

    saved, applied = sessions.enhance_run_task_profile(
        run.session_id,
        run.run_id,
        semantic.model_dump(mode="json"),
    )

    assert applied is True
    assert {"artifact", "analytics"} <= set(
        saved["verification_contract"]["verification_packs"]
    )

    persisted_run = sessions.get_run_state(run.session_id, run.run_id)
    assert persisted_run is not None
    live_run = RunRecord.model_validate(persisted_run)
    coordinator.transition(live_run, RunStatus.RUNNING)
    later = TaskProfileClassifier.profile_from_dimensions(
        work_natures=["联网研究"],
        delivery_forms=["answer"],
        verification_intents=["web_research"],
        classifier="llm_semantic",
    )
    unchanged, applied_late = sessions.enhance_run_task_profile(
        live_run.session_id,
        live_run.run_id,
        later.model_dump(mode="json"),
    )

    assert applied_late is False
    assert "web_research" not in unchanged["verification_contract"][
        "verification_packs"
    ]


def test_run_coordinator_accepts_semantic_task_profile(tmp_path):
    from graph.session_manager import SessionManager

    sessions = SessionManager()
    sessions.initialize(tmp_path)
    sessions.create_session("semantic-profile-session")
    profile = TaskProfileClassifier.profile_from_dimensions(
        work_natures=["database_analysis"],
        delivery_forms=["artifact"],
        analytics_model_id="产品配置分析",
        classifier="llm_semantic",
    )

    run, _goal = HarnessRunCoordinator(sessions).start_run(
        session_id="semantic-profile-session",
        query_id="query-semantic-profile",
        objective="刷新报告并重算历史数据",
        goal_mode=False,
        analytics_model_id="产品配置分析",
        task_profile=profile,
    )

    assert run.task_profile.classifier == "llm_semantic"
    assert run.verification_contract is not None
    assert {"analytics", "artifact"} <= set(
        run.verification_contract.verification_packs
    )


def test_selected_model_does_not_change_contract_semantics():
    message = "分析今天 AI 新闻并附来源"
    without_model = RunRubricCompiler.compile(
        RubricBuildContext(user_message=message)
    )
    with_model = RunRubricCompiler.compile(
        RubricBuildContext(
            user_message=message,
            analytics_model_id="model-1",
        )
    )

    assert without_model is not None
    assert with_model is not None
    assert with_model.verification_packs == without_model.verification_packs
    assert _criterion_ids(with_model) == _criterion_ids(without_model)
    assert with_model.contract_id == without_model.contract_id
    assert "metric_consistency" not in _criterion_ids(with_model)


def test_negated_analytics_intent_does_not_activate_analytics():
    profile = TaskProfileClassifier.classify(
        message="不要查询数据库，也不要分析数据，只总结这段新闻",
        analytics_model_id="selected-model",
    )

    assert "analytics" not in profile.initial_packs


def test_explicit_analytics_intent_does_not_require_selected_model():
    contract = RunRubricCompiler.compile(
        RubricBuildContext(
            user_message="查询 6 月销量同比下降原因，并明确计算口径"
        )
    )

    assert contract is not None
    assert "analytics" in contract.verification_packs
    assert {"metric_consistency", "analytics_evidence_traceability"} <= _criterion_ids(
        contract
    )


@pytest.mark.parametrize(
    "tool_name",
    [
        "database_sql_execute",
        "pandas_knowledge_query",
        "semantic_entity_lookup",
    ],
)
def test_successful_analytics_tools_activate_analytics(tool_name):
    profile = TaskProfileClassifier.classify(message="继续处理这个任务")
    activations = build_verification_activations(
        run_id="run-1",
        query_id="query-1",
        tool_call_id="call-1",
        tool_name=tool_name,
        args={},
        result=ToolMessage(
            content="result_id: result-1\ndatabase_source_id: db-1",
            tool_call_id="call-1",
            name=tool_name,
        ),
    )

    effective = RunRubricCompiler.expand_for_activations(
        contract=None,
        profile=profile,
        message="继续处理这个任务",
        activations=activations,
    )

    assert effective is not None
    assert "analytics" in effective.verification_packs
    assert {"metric_consistency", "analytics_evidence_traceability"} <= _criterion_ids(
        effective
    )


def test_fetch_url_activates_web_research_only():
    profile = TaskProfileClassifier.classify(message="继续整理")
    effective = RunRubricCompiler.expand_for_activations(
        contract=None,
        profile=profile,
        message="继续整理",
        activations=build_verification_activations(
            run_id="run-1",
            query_id="query-1",
            tool_call_id="call-web",
            tool_name="fetch_url",
            args={"url": "https://example.com/news"},
            result=ToolMessage(
                content="page fetched: https://example.com/news",
                tool_call_id="call-web",
                name="fetch_url",
            ),
        ),
    )

    assert effective is not None
    assert "web_research" in effective.verification_packs
    assert "analytics" not in effective.verification_packs
    assert "metric_consistency" not in _criterion_ids(effective)


def test_aihot_skill_command_activates_web_research():
    assert verification_packs_for_tool(
        "execute",
        {"command": "python3 /skills/aihot/scripts/aihot_query.py --limit 10"},
    ) == ["web_research"]


def test_aihot_curl_json_creates_material_web_sources():
    result = ToolMessage(
        content=json.dumps(
            {
                "items": [
                    {
                        "id": "news-1",
                        "title": "OpenAI 发布新模型",
                        "url": "https://example.com/original",
                        "permalink": "https://aihot.virxact.com/items/news-1",
                        "summary": "模型能力更新。",
                    }
                ]
            },
            ensure_ascii=False,
        ),
        tool_call_id="call-aihot",
        name="execute",
    )

    activations = build_verification_activations(
        run_id="run-aihot",
        query_id="query-aihot",
        tool_call_id="call-aihot",
        tool_name="execute",
        args={
            "command": (
                'curl -sS -H "User-Agent: aihot-skill/0.3.6" '
                '"https://aihot.virxact.com/api/public/items?mode=selected"'
            )
        },
        result=result,
    )

    assert [activation.pack for activation in activations] == ["web_research"]
    refs = activations[0].evidence_refs
    assert all(ref["material"] is True for ref in refs)
    source = next(ref for ref in refs if ref["kind"] == "source")
    assert source["uri"] == "https://aihot.virxact.com/items/news-1"
    assert source["source_id"].startswith("src_")


def test_arbitrary_skill_source_dynamically_activates_web_research():
    activations = build_verification_activations(
        run_id="run-skill",
        query_id="query-skill",
        tool_call_id="call-skill",
        tool_name="execute_skill",
        args={"skill_name": "third-party-news", "user_query": "最近有什么更新"},
        result=ToolMessage(
            content=(
                "技能：third-party-news\n\n执行结果：\n"
                "[官方更新](https://example.com/updates)"
            ),
            tool_call_id="call-skill",
            name="execute_skill",
        ),
    )

    assert [activation.pack for activation in activations] == ["web_research"]
    source = next(
        ref
        for ref in activations[0].evidence_refs
        if ref.get("kind") == "source"
    )
    assert source["material"] is True
    assert source["uri"] == "https://example.com/updates"


@pytest.mark.parametrize(
    "command",
    [
        "cd /skills/aihot && python3 scripts/aihot_query.py --limit 10",
        "PYTHONUNBUFFERED=1 python3 /skills/aihot/scripts/aihot_query.py --limit 10",
        "timeout 30 python3 /skills/aihot/scripts/aihot_query.py --limit 10",
        "bash -lc 'cd /skills/aihot && python3 scripts/aihot_query.py --limit 10'",
    ],
)
def test_wrapped_aihot_skill_commands_activate_web_research(command):
    assert verification_packs_for_tool(
        "execute",
        {"command": command},
    ) == ["web_research"]


def test_mentioning_aihot_path_without_executing_entrypoint_does_not_activate_web():
    assert verification_packs_for_tool(
        "execute",
        {
            "command": (
                "python3 -c \"import hashlib; "
                "print(hashlib.sha256(open('/skills/aihot/SKILL.md','rb').read()).hexdigest())\""
            )
        },
    ) == []


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("/workspace/app.py", {"code"}),
        ("/workspace/report.md", {"artifact"}),
        ("/workspace/dashboard.html", {"code", "artifact"}),
    ],
)
def test_successful_workspace_writes_activate_matching_packs(path, expected):
    assert set(
        verification_packs_for_tool("write_file", {"file_path": path})
    ) == expected


def test_external_write_keeps_real_authorized_path_with_spaces(tmp_path):
    from graph.session_manager import session_manager

    session_manager.initialize(tmp_path)
    session_manager.create_session("session-external")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    external = tmp_path / "outside" / "产品配置分析模型模板 v2.html"
    external.parent.mkdir()
    external.write_text("<html></html>", encoding="utf-8")
    grant = session_manager.add_permission_grant(
        "session-external",
        grant_type="external_file_write",
        target_kind="exact_file",
        target=str(external.resolve()),
        capabilities=["write", "external_path"],
    )
    result = ToolMessage(
        content=f"Updated file {external}",
        tool_call_id="call-external",
        name="edit_file",
        status="success",
    )

    activation = next(
        item
        for item in build_verification_activations(
            run_id="run-external",
            query_id="query-external",
            tool_call_id="call-external",
            tool_name="edit_file",
            args={
                "file_path": str(external),
                "old_string": "old",
                "new_string": "new",
            },
            result=result,
            session_id="session-external",
            workspace_path=str(workspace),
        )
        if item.pack == "artifact"
    )
    ref = next(item for item in activation.evidence_refs if item.get("kind") == "artifact_write")

    assert ref["scope"] == "external"
    assert ref["path"] == str(external.resolve())
    assert ref["host_path"] == str(external.resolve())
    assert ref["virtual_path"] is None
    assert ref["authorized"] is True
    assert ref["permission_grant_id"] == grant["id"]
    assert ref["content_sha256"].startswith("sha256:")
    assert ref["size_bytes"] == external.stat().st_size
    assert "/workspace/Users/" not in ref["path"]


def test_artifact_delivery_rejects_receipt_size_identity_mismatch(tmp_path):
    from graph.session_manager import session_manager
    from harness.deterministic_checks import _evaluate_artifact_delivery

    session_manager.initialize(tmp_path)
    session_manager.create_session("session-artifact-size")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    artifact = workspace / "report.md"
    artifact.write_text("# report\n", encoding="utf-8")
    activation = next(
        item
        for item in build_verification_activations(
            run_id="run-artifact-size",
            query_id="query-artifact-size",
            tool_call_id="call-artifact-size",
            tool_name="write_file",
            args={"file_path": "/workspace/report.md", "content": "# report\n"},
            result=ToolMessage(
                content="Updated /workspace/report.md",
                tool_call_id="call-artifact-size",
                name="write_file",
                status="success",
            ),
            workspace_path=str(workspace),
        )
        if item.pack == "artifact"
    )
    payload = activation.model_dump(mode="json")
    unreferenced = _evaluate_artifact_delivery(
        "artifact_delivery",
        {
            "workspace_path": str(workspace),
            "run_id": "run-artifact-size",
            "final_content": "",
            "verification_activations": [activation.model_dump(mode="json")],
        },
    )
    artifact_ref = next(
        item for item in payload["evidence_refs"] if item.get("kind") == "artifact_write"
    )
    artifact_ref["size_bytes"] += 1

    evaluation = _evaluate_artifact_delivery(
        "artifact_delivery",
        {
            "workspace_path": str(workspace),
            "run_id": "run-artifact-size",
            "final_content": "报告：`/workspace/report.md`",
            "verification_activations": [payload],
        },
    )

    assert not unreferenced.passed
    assert "尚未引用" in str(unreferenced.gap)
    assert not evaluation.passed
    assert "发生变化" in str(evaluation.gap)


def test_scratch_write_is_temporary_and_cannot_satisfy_artifact_delivery(tmp_path):
    from graph.session_manager import session_manager
    from harness.deterministic_checks import _evaluate_artifact_delivery

    session_manager.initialize(tmp_path)
    session_manager.create_session("session-scratch")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    scratch = tmp_path / "scratch" / "session-scratch" / "query-scratch"
    scratch.mkdir(parents=True)
    run, _goal = HarnessRunCoordinator(session_manager).start_run(
        session_id="session-scratch",
        query_id="query-scratch",
        objective="生成一个 HTML 报告",
        goal_mode=False,
    )
    session_manager.bind_run_execution_snapshot(
        "session-scratch",
        run.run_id,
        {
            "backend_mode": "restricted_host",
            "backend_id": "host:test",
            "workspace_id": "workspace:test",
            "scratch_host_path": str(scratch),
        },
    )
    scratch_file = scratch / "report.html"
    scratch_file.write_text("<html></html>", encoding="utf-8")
    activation = next(
        item
        for item in build_verification_activations(
            run_id=run.run_id,
            query_id="query-scratch",
            tool_call_id="call-scratch",
            tool_name="write_file",
            args={"file_path": "/scratch/report.html", "content": "<html></html>"},
            result=ToolMessage(
                content="Updated /scratch/report.html",
                tool_call_id="call-scratch",
                name="write_file",
                status="success",
            ),
            session_id="session-scratch",
            workspace_path=str(workspace),
        )
        if item.pack == "artifact"
    )
    ref = next(item for item in activation.evidence_refs if item.get("kind") == "artifact_write")
    assert ref["scope"] == "scratch"
    assert ref["role"] == "temporary"

    evaluation = _evaluate_artifact_delivery(
        "artifact_delivery",
        {
            "workspace_path": str(workspace),
            "run_id": run.run_id,
            "verification_activations": [activation.model_dump(mode="json")],
            "declared_artifact_targets": [],
        },
    )
    assert evaluation.passed is False


def test_sql_generation_activates_analytics_but_is_not_material_evidence():
    activation = build_verification_activations(
        run_id="run-1",
        query_id="query-1",
        tool_call_id="call-generate",
        tool_name="database_sql_generate",
        args={"question": "查询销量"},
    )[0]

    assert activation.pack == "analytics"
    assert activation.evidence_refs[0]["material"] is False


def test_non_material_aihot_inspection_does_not_widen_persisted_contract(
    tmp_path,
):
    from types import SimpleNamespace

    from langchain.agents.middleware.types import ToolCallRequest

    from graph.session_manager import session_manager
    from harness.verification_activations import VerificationActivationMiddleware

    session_manager.initialize(tmp_path)
    session_manager.create_session("session-material")
    coordinator = HarnessRunCoordinator(session_manager)
    run, _goal = coordinator.start_run(
        session_id="session-material",
        query_id="query-material",
        objective="检查本地 Skill 哈希",
        goal_mode=False,
    )
    coordinator.transition(run, RunStatus.RUNNING)
    request = ToolCallRequest(
        tool_call={
            "id": "call-hash",
            "name": "execute",
            "args": {
                "command": (
                    "python3 -c \"import hashlib; "
                    "print(hashlib.sha256(open('/skills/aihot/SKILL.md','rb').read()).hexdigest())\""
                )
            },
        },
        tool=None,
        state={},
        runtime=SimpleNamespace(
            context={
                "session_id": "session-material",
                "run_id": run.run_id,
                "query_id": "query-material",
            },
            stream_writer=None,
        ),
    )
    result = ToolMessage(
        content="abc123\n\n[Command succeeded with exit code 0]",
        tool_call_id="call-hash",
        name="execute",
    )

    VerificationActivationMiddleware._record(request, result)

    persisted = session_manager.get_run_state("session-material", run.run_id)
    assert persisted is not None
    assert persisted["verification_activations"] == []


@pytest.mark.parametrize(
    ("tool_name", "args", "content", "expected_pack", "expected_material"),
    [
        (
            "execute",
            {"command": "curl -fsSL https://example.com/news"},
            "news body\n\n[Command succeeded with exit code 0]",
            "web_research",
            True,
        ),
        (
            "database_sql_generate",
            {"question": "查询销量"},
            "SELECT SUM(amount) FROM sales",
            "analytics",
            False,
        ),
    ],
)
def test_semantic_activation_materiality_follows_result_evidence(
    tmp_path,
    tool_name,
    args,
    content,
    expected_pack,
    expected_material,
):
    from types import SimpleNamespace

    from langchain.agents.middleware.types import ToolCallRequest

    from graph.session_manager import session_manager
    from harness.verification_activations import VerificationActivationMiddleware

    session_manager.initialize(tmp_path)
    session_manager.create_session("session-non-material")
    coordinator = HarnessRunCoordinator(session_manager)
    run, _goal = coordinator.start_run(
        session_id="session-non-material",
        query_id="query-non-material",
        objective="继续处理",
        goal_mode=False,
    )
    coordinator.transition(run, RunStatus.RUNNING)
    request = ToolCallRequest(
        tool_call={"id": "call-non-material", "name": tool_name, "args": args},
        tool=None,
        state={},
        runtime=SimpleNamespace(
            context={
                "session_id": "session-non-material",
                "run_id": run.run_id,
                "query_id": "query-non-material",
            },
            stream_writer=None,
        ),
    )
    result = ToolMessage(
        content=content,
        tool_call_id="call-non-material",
        name=tool_name,
    )

    VerificationActivationMiddleware._record(request, result)

    persisted = session_manager.get_run_state("session-non-material", run.run_id)
    assert persisted is not None
    activation = persisted["verification_activations"][0]
    assert activation["pack"] == expected_pack
    assert all(
        item.get("material") is expected_material
        for item in activation["evidence_refs"]
    )
    effective = RunRubricCompiler.expand_for_activations(
        contract=None,
        profile=TaskProfileClassifier.classify(message="继续处理"),
        message="继续处理",
        activations=[VerificationActivation.model_validate(activation)],
    )
    if expected_material:
        assert effective is not None
        assert expected_pack in effective.verification_packs
    else:
        assert effective is None


def test_effective_contract_is_order_independent_and_idempotent():
    profile = TaskProfileClassifier.classify(message="继续")
    db = build_verification_activations(
        run_id="run-1",
        query_id="query-1",
        tool_call_id="call-db",
        tool_name="database_sql_execute",
        args={},
        result=ToolMessage(
            content="result_id: result-1\ndatabase_source_id: db-1",
            tool_call_id="call-db",
            name="database_sql_execute",
        ),
    )[0]
    web = build_verification_activations(
        run_id="run-1",
        query_id="query-1",
        tool_call_id="call-web",
        tool_name="fetch_url",
        args={"url": "https://example.com"},
        result=ToolMessage(
            content="page fetched: https://example.com",
            tool_call_id="call-web",
            name="fetch_url",
        ),
    )[0]

    first = RunRubricCompiler.expand_for_activations(
        contract=None,
        profile=profile,
        message="继续",
        activations=[db, web, db],
    )
    second = RunRubricCompiler.expand_for_activations(
        contract=None,
        profile=profile,
        message="继续",
        activations=[web, db],
    )

    assert first is not None and second is not None
    assert set(first.verification_packs) == set(second.verification_packs)
    assert _criterion_ids(first) == _criterion_ids(second)
    assert first.contract_id == second.contract_id
    assert first.activation_reasons == second.activation_reasons
    assert len(_criterion_ids(first)) == len(first.criteria)


@pytest.mark.parametrize(
    "tool_name",
    ["database_sql_execute_fake", "my_database_sql_execute", "fetch_url_v2", ""],
)
def test_similar_or_unknown_tool_names_do_not_activate(tool_name):
    assert verification_packs_for_tool(tool_name, {}) == []


@pytest.mark.parametrize(
    "message",
    [
        ToolMessage(
            content="Error: database connection refused",
            tool_call_id="call-1",
            name="database_sql_execute",
            status="error",
        ),
        ToolMessage(
            content="Traceback: failed",
            tool_call_id="call-1",
            name="database_sql_execute",
        ),
    ],
)
def test_unsuccessful_tool_results_do_not_activate(message):
    assert tool_result_succeeded(message) is False


def test_activation_ledger_is_current_run_scoped_and_idempotent(tmp_path):
    sessions = SessionManager()
    sessions.initialize(tmp_path)
    sessions.create_session("session-1")
    run = {
        "run_id": "run-1",
        "query_id": "query-current",
        "session_id": "session-1",
        "objective": "继续",
        "task_profile": {},
        "verification_activations": [],
    }
    sessions.start_harness_run("session-1", run)
    activation = build_verification_activations(
        run_id="run-1",
        query_id="query-current",
        tool_call_id="call-1",
        tool_name="database_sql_execute",
        args={},
    )[0]

    _, created = sessions.append_run_verification_activation(
        "session-1",
        "run-1",
        activation.model_dump(mode="json"),
    )
    _, replay_created = sessions.append_run_verification_activation(
        "session-1",
        "run-1",
        activation.model_dump(mode="json"),
    )

    assert created is True
    assert replay_created is False
    forged = activation.model_copy(update={"query_id": "query-old"})
    with pytest.raises(ValueError, match="query_id mismatch"):
        sessions.append_run_verification_activation(
            "session-1",
            "run-1",
            forged.model_dump(mode="json"),
        )

    sessions.update_run_verification_contract(
        "session-1",
        "run-1",
        {"contract_id": "effective-contract"},
    )
    persisted = sessions.get_run_state("session-1", "run-1")
    assert persisted is not None
    assert persisted["verification_contract"]["contract_id"] == "effective-contract"
    assert len(persisted["verification_activations"]) == 1


def test_stale_run_upsert_cannot_erase_concurrent_activation(tmp_path):
    sessions = SessionManager()
    sessions.initialize(tmp_path)
    sessions.create_session("session-1")
    stale = {
        "run_id": "run-1",
        "query_id": "query-current",
        "session_id": "session-1",
        "objective": "继续",
        "status": "preparing",
        "task_profile": {},
        "verification_activations": [],
    }
    sessions.start_harness_run("session-1", stale)
    sessions.transition_run_status("session-1", "run-1", "running")
    stale["status"] = "running"
    activation = build_verification_activations(
        run_id="run-1",
        query_id="query-current",
        tool_call_id="call-1",
        tool_name="database_sql_execute",
        args={},
    )[0]
    sessions.append_run_verification_activation(
        "session-1",
        "run-1",
        activation.model_dump(mode="json"),
    )

    stale["status"] = "waiting_hitl"
    sessions.upsert_run_state("session-1", stale)
    persisted = sessions.get_run_state("session-1", "run-1")

    assert persisted is not None
    assert len(persisted["verification_activations"]) == 1


def test_evaluation_freezes_activation_ledger(tmp_path):
    from harness.coordinators import HarnessRunCoordinator

    sessions = SessionManager()
    sessions.initialize(tmp_path)
    sessions.create_session("session-1")
    coordinator = HarnessRunCoordinator(sessions)
    run, _ = coordinator.start_run(
        session_id="session-1",
        query_id="query-current",
        objective="继续",
        goal_mode=False,
    )
    coordinator.transition(run, RunStatus.RUNNING)
    coordinator.transition(run, RunStatus.EVALUATING)
    activation = build_verification_activations(
        run_id=run.run_id,
        query_id=run.query_id,
        tool_call_id="call-late",
        tool_name="database_sql_execute",
        args={},
    )[0]

    with pytest.raises(ValueError, match="cannot accept"):
        sessions.append_run_verification_activation(
            run.session_id,
            run.run_id,
            activation.model_dump(mode="json"),
        )


def test_middleware_does_not_record_failed_tool(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(
        "harness.verification_activations.session_manager.append_run_verification_activation",
        lambda *args, **kwargs: calls.append((args, kwargs)),
    )
    request = ToolCallRequest(
        tool_call={
            "id": "call-1",
            "name": "database_sql_execute",
            "args": {},
        },
        tool=None,
        state={},
        runtime=SimpleNamespace(
            context={
                "session_id": "session-1",
                "run_id": "run-1",
                "query_id": "query-1",
            },
            stream_writer=None,
        ),
    )

    result = VerificationActivationMiddleware().wrap_tool_call(
        request,
        lambda _request: ToolMessage(
            content="Error: refused",
            tool_call_id="call-1",
            name="database_sql_execute",
            status="error",
        ),
    )

    assert result.status == "error"
    assert calls == []


def test_general_subagent_inherits_verification_activation_middleware():
    from graph.deepagents_manager import _build_subagents

    subagents = _build_subagents(
        default_tools=[],
        default_skills=[],
        middleware_factory=lambda: [VerificationActivationMiddleware()],
    )

    assert subagents
    assert any(
        isinstance(item, VerificationActivationMiddleware)
        for item in subagents[0].get("middleware", [])
    )


def test_evidence_traceability_fails_closed_without_structured_evidence():
    contract = RunRubricCompiler.compile(
        RubricBuildContext(user_message="分析 6 月销量下降原因")
    )
    assert contract is not None
    report = CompletionVerificationCoordinator.report_from_final_state(
        run_id="run-1",
        contract=contract,
        final_state={
            "_rubric_status": "satisfied",
            "_rubric_evaluations": [
                {
                    "result": "satisfied",
                    "criteria": [
                        {"name": "task_fulfillment", "passed": True},
                        {"name": "metric_consistency", "passed": True},
                        {
                            "name": "analytics_evidence_traceability",
                            "passed": True,
                        },
                    ],
                }
            ],
            "_harness_context": {
                "todos": [],
                "verification_activations": [],
            },
        },
    )

    evidence = next(
        item
        for item in report.evaluations
        if item.criterion_id == "analytics_evidence_traceability"
    )
    assert evidence.passed is False
    assert evidence.evidence == []
    assert "查询结果" in str(evidence.gap)
    assert report.status == VerificationStatus.NEEDS_REVISION


def test_single_web_source_is_traceable_through_tool_lineage():
    contract = RunRubricCompiler.compile(
        RubricBuildContext(user_message="搜索最新 AI 新闻并附来源")
    )
    assert contract is not None
    activation = build_verification_activations(
        run_id="run-1",
        query_id="query-1",
        tool_call_id="call-web",
        tool_name="fetch_url",
        args={"url": "https://example.com/news"},
        result=ToolMessage(
            content="# News\nA new model launched.",
            tool_call_id="call-web",
            name="fetch_url",
            status="success",
        ),
    )[0].model_dump(mode="json")

    report = CompletionVerificationCoordinator.report_from_final_state(
        run_id="run-1",
        contract=contract,
        final_state={
            "_rubric_status": "satisfied",
            "_rubric_evaluations": [
                {
                    "result": "satisfied",
                    "criteria": [
                        {"name": "task_fulfillment", "passed": True},
                        {"name": "web_evidence_traceability", "passed": True},
                        {"name": "time_scope", "passed": True},
                    ],
                }
            ],
            "_harness_context": {
                "todos": [],
                "final_content": "今天发布了一个新模型。",
                "verification_activations": [activation],
            },
        },
    )

    evidence = next(
        item
        for item in report.evaluations
        if item.criterion_id == "web_evidence_traceability"
    )
    assert evidence.passed is True
    assert evidence.gap is None


def test_multiple_web_sources_require_explicit_citation():
    contract = RunRubricCompiler.compile(
        RubricBuildContext(user_message="搜索最新 AI 新闻并附来源")
    )
    assert contract is not None
    activation = VerificationActivation(
        activation_id="activation-web-multiple",
        run_id="run-1",
        query_id="query-1",
        tool_call_id="call-web",
        tool_name="tavily_search",
        pack="web_research",
        evidence_refs=[
            {"kind": "tool_result", "tool_call_id": "call-web", "material": True},
            {
                "kind": "source",
                "tool_call_id": "call-web",
                "source_id": "src_one",
                "uri": "https://example.com/one",
                "material": True,
            },
            {
                "kind": "source",
                "tool_call_id": "call-web",
                "source_id": "src_two",
                "uri": "https://example.com/two",
                "material": True,
            },
        ],
    ).model_dump(mode="json")

    report = CompletionVerificationCoordinator.report_from_final_state(
        run_id="run-1",
        contract=contract,
        final_state={
            "_rubric_status": "satisfied",
            "_rubric_evaluations": [
                {
                    "result": "satisfied",
                    "criteria": [
                        {"name": "task_fulfillment", "passed": True},
                        {"name": "web_evidence_traceability", "passed": True},
                        {"name": "time_scope", "passed": True},
                    ],
                }
            ],
            "_harness_context": {
                "todos": [],
                "final_content": "今天发布了两个新模型。",
                "verification_activations": [activation],
            },
        },
    )

    evidence = next(
        item
        for item in report.evaluations
        if item.criterion_id == "web_evidence_traceability"
    )
    assert evidence.passed is False
    assert "多来源回答" in str(evidence.gap)


def test_grader_cannot_omit_required_criteria_and_claim_satisfied():
    contract = RunRubricCompiler.compile(
        RubricBuildContext(user_message="分析 6 月销量下降原因")
    )
    assert contract is not None
    activation = VerificationActivation(
        activation_id="activation-1",
        run_id="run-1",
        query_id="query-1",
        tool_call_id="call-db",
        tool_name="database_sql_execute",
        pack="analytics",
        evidence_refs=[{"kind": "tool_execution", "tool_call_id": "call-db"}],
    ).model_dump(mode="json")

    report = CompletionVerificationCoordinator.report_from_final_state(
        run_id="run-1",
        contract=contract,
        final_state={
            "_rubric_status": "satisfied",
            "_rubric_evaluations": [
                {
                    "result": "satisfied",
                    "criteria": [
                        {"name": "task_fulfillment", "passed": True},
                    ],
                }
            ],
            "_harness_context": {
                "todos": [],
                "verification_activations": [activation],
            },
        },
    )

    metric = next(
        item
        for item in report.evaluations
        if item.criterion_id == "metric_consistency"
    )
    assert metric.passed is False
    assert "未返回" in str(metric.gap)
    assert report.status == VerificationStatus.NEEDS_REVISION


def test_coordinator_ignores_forged_final_state_contract_and_activations(tmp_path):
    from graph.session_manager import SessionManager
    from harness.coordinators import HarnessRunCoordinator
    from harness.models import RunStatus

    sessions = SessionManager()
    sessions.initialize(tmp_path)
    sessions.create_session("session-1")
    coordinator = HarnessRunCoordinator(sessions)
    run, goal = coordinator.start_run(
        session_id="session-1",
        query_id="query-current",
        objective="解释一下这个概念",
        goal_mode=False,
    )
    coordinator.transition(run, RunStatus.RUNNING)
    forged = build_verification_activations(
        run_id=run.run_id,
        query_id=run.query_id,
        tool_call_id="call-forged",
        tool_name="database_sql_execute",
        args={},
        result=ToolMessage(
            content="result_id: forged-result\ndatabase_source_id: forged-db",
            tool_call_id="call-forged",
            name="database_sql_execute",
        ),
    )[0].model_dump(mode="json")
    forged_contract = RunRubricCompiler.expand_for_activations(
        contract=None,
        profile=run.task_profile,
        message=run.objective,
        activations=[forged],
    )
    assert forged_contract is not None

    completed, _, report = coordinator.complete_from_final_state(
        run,
        goal,
        {
            "verification_contract": forged_contract.model_dump(mode="json"),
            "verification_activations": [forged],
            "_rubric_status": "satisfied",
        },
    )

    assert completed.verification_contract is None
    assert completed.verification_activations == []
    assert report.status == VerificationStatus.NOT_REQUIRED


def test_stale_run_state_cannot_shrink_persisted_contract(tmp_path):
    from harness.coordinators import HarnessRunCoordinator

    sessions = SessionManager()
    sessions.initialize(tmp_path)
    sessions.create_session("session-1")
    coordinator = HarnessRunCoordinator(sessions)
    run, _ = coordinator.start_run(
        session_id="session-1",
        query_id="query-current",
        objective="查询 6 月销量并分析原因",
        goal_mode=False,
    )
    assert run.verification_contract is not None
    assert "analytics" in run.verification_contract.verification_packs
    stale = run.model_dump(mode="json")
    stale["verification_contract"] = RunRubricCompiler.compile(
        RubricBuildContext(
            user_message="解释这个概念",
            force_required=True,
        )
    ).model_dump(mode="json")

    sessions.upsert_run_state(run.session_id, stale)
    persisted = sessions.get_run_state(run.session_id, run.run_id)

    assert persisted is not None
    assert "analytics" in persisted["verification_contract"]["verification_packs"]


@pytest.mark.parametrize(
    "content",
    [
        "🧮 SQL 执行失败：database unavailable",
        "📊 PandasQueryEngine 查询失败：invalid table",
        "未找到相关内容。",
        "command not found: pytest\n\n[Command failed with exit code 127]",
    ],
)
def test_business_failure_outputs_cannot_activate_verification(content):
    message = ToolMessage(
        content=content,
        tool_call_id="call-current",
        name="database_sql_execute",
        status="success",
    )

    assert (
        tool_result_succeeded(message, expected_call_id="call-current") is False
    )


def test_unrelated_success_message_cannot_mask_current_tool_failure():
    result = Command(
        update={
            "messages": [
                ToolMessage(
                    content="Error: current call failed",
                    tool_call_id="call-current",
                    name="database_sql_execute",
                    status="error",
                ),
                ToolMessage(
                    content="query completed",
                    tool_call_id="call-other",
                    name="database_sql_execute",
                    status="success",
                ),
            ]
        }
    )

    assert (
        tool_result_succeeded(result, expected_call_id="call-current") is False
    )


@pytest.mark.parametrize(
    "command",
    [
        "pytest tests/test_pandas_knowledge.py",
        "rg pandas requirements.txt",
        "ls fixtures/demo.csv",
        "rg 'select x from y' docs",
        "echo pytest",
        "pytest nonexistent || true",
    ],
)
def test_non_analytics_or_fake_validation_commands_do_not_overactivate(command):
    packs = verification_packs_for_tool("execute", {"command": command})

    assert "analytics" not in packs
    if command in {"echo pytest", "pytest nonexistent || true"}:
        assert "code" not in packs


@pytest.mark.parametrize(
    "message",
    [
        "分析 Python 的数据结构实现",
        "修改销量页面的 CSS 代码",
        "解释 SQL 注入漏洞，不查询数据库",
        "生成一段 SQL 示例，不执行",
        "写一个网页页面",
    ],
)
def test_task_profile_does_not_confuse_domain_words_with_analytics(message):
    profile = TaskProfileClassifier.classify(
        message=message,
        analytics_model_id="selected-model",
    )

    assert "analytics" not in profile.initial_packs


def test_required_grader_gap_forces_needs_revision():
    contract = RunRubricCompiler.compile(
        RubricBuildContext(user_message="解释 RubricMiddleware")
    )
    assert contract is None
    contract = RunRubricCompiler.compile(
        RubricBuildContext(
            user_message="解释 RubricMiddleware",
            force_required=True,
        )
    )
    assert contract is not None

    report = CompletionVerificationCoordinator.report_from_final_state(
        run_id="run-1",
        contract=contract,
        final_state={
            "_rubric_status": "satisfied",
            "_rubric_evaluations": [
                {
                    "result": "satisfied",
                    "criteria": [
                        {
                            "name": "task_fulfillment",
                            "passed": True,
                            "gap": "尚未解释 verify 触发时机。",
                        },
                        {"name": "todo_reconciliation", "passed": True},
                    ],
                }
            ],
            "_harness_context": {"todos": []},
        },
    )

    task = next(
        item
        for item in report.evaluations
        if item.criterion_id == "task_fulfillment"
    )
    assert task.passed is False
    assert report.status == VerificationStatus.NEEDS_REVISION


def test_nonterminal_needs_revision_cannot_be_terminalized_as_satisfied():
    contract = RunRubricCompiler.compile(
        RubricBuildContext(user_message="搜索最新 AI 新闻并附来源")
    )
    assert contract is not None
    result = ToolMessage(
        content="# News\nA new model launched.",
        tool_call_id="call-web",
        name="fetch_url",
        status="success",
    )
    activation = build_verification_activations(
        run_id="run-1",
        query_id="query-1",
        tool_call_id="call-web",
        tool_name="fetch_url",
        args={"url": "https://example.com/news"},
        result=result,
    )[0]
    source = next(
        item for item in activation.evidence_refs if item.get("kind") == "source"
    )

    report = CompletionVerificationCoordinator.report_from_final_state(
        run_id="run-1",
        contract=contract,
        final_state={
            "_rubric_status": "needs_revision",
            "_rubric_evaluations": [
                {
                    "result": "needs_revision",
                    "explanation": "模型误认为来源验收未通过。",
                    "criteria": [
                        {"name": "task_fulfillment", "passed": True},
                        {"name": "time_scope", "passed": True},
                        {
                            "name": "web_evidence_traceability",
                            "passed": False,
                            "gap": "模型未识别引用。",
                        },
                    ],
                }
            ],
            "_harness_context": {
                "todos": [],
                "final_content": f"新闻结论。[^{source['source_id']}]",
                "verification_activations": [
                    activation.model_dump(mode="json")
                ],
            },
        },
    )

    assert all(item.passed for item in report.evaluations)
    assert report.status == VerificationStatus.INCOMPLETE
    assert any("未形成合法终态" in gap for gap in report.gaps)


def _web_report(final_content: str, *, cited_source_id: str | None = None):
    contract = RunRubricCompiler.compile(
        RubricBuildContext(user_message="搜索最新 AI 新闻并附来源")
    )
    assert contract is not None
    result = ToolMessage(
        content="# News\nA new model launched.",
        tool_call_id="call-web",
        name="fetch_url",
        status="success",
    )
    activation = build_verification_activations(
        run_id="run-1",
        query_id="query-1",
        tool_call_id="call-web",
        tool_name="fetch_url",
        args={"url": "https://example.com/news"},
        result=result,
    )[0]
    source_ref = next(
        item
        for item in activation.evidence_refs
        if item.get("kind") == "source"
    )
    if cited_source_id is None:
        cited_source_id = str(source_ref["source_id"])
    report = CompletionVerificationCoordinator.report_from_final_state(
        run_id="run-1",
        contract=contract,
        final_state={
            "_rubric_status": "satisfied",
            "_rubric_evaluations": [
                {
                    "result": "satisfied",
                    "criteria": [
                        {"name": "task_fulfillment", "passed": True},
                        {"name": "todo_reconciliation", "passed": True},
                        {"name": "web_evidence_traceability", "passed": True},
                    ],
                }
            ],
            "_harness_context": {
                "todos": [],
                "final_content": final_content.format(source_id=cited_source_id),
                "verification_activations": [
                    activation.model_dump(mode="json")
                ],
            },
        },
    )
    return report


def test_current_run_verified_citation_passes():
    report = _web_report("新闻结论。[^{source_id}]")

    evidence = next(
        item
        for item in report.evaluations
        if item.criterion_id == "web_evidence_traceability"
    )
    assert evidence.passed is True
    assert report.status == VerificationStatus.SATISFIED


def test_forged_source_id_is_rejected():
    report = _web_report("新闻结论。[^{source_id}]", cited_source_id="src_fake")

    evidence = next(
        item
        for item in report.evaluations
        if item.criterion_id == "web_evidence_traceability"
    )
    assert evidence.passed is False
    assert report.status == VerificationStatus.NEEDS_REVISION


def test_web_activation_cannot_satisfy_analytics_evidence():
    contract = RunRubricCompiler.compile(
        RubricBuildContext(
            user_message="搜索行业新闻，并查询销量分析变化原因"
        )
    )
    assert contract is not None
    result = ToolMessage(
        content="# News\nA new model launched.",
        tool_call_id="call-web",
        name="fetch_url",
        status="success",
    )
    activation = build_verification_activations(
        run_id="run-1",
        query_id="query-1",
        tool_call_id="call-web",
        tool_name="fetch_url",
        args={"url": "https://example.com/news"},
        result=result,
    )[0]
    report = CompletionVerificationCoordinator.report_from_final_state(
        run_id="run-1",
        contract=contract,
        final_state={
            "_rubric_status": "satisfied",
            "_rubric_evaluations": [
                {
                    "result": "satisfied",
                    "criteria": [
                        {"name": item.id, "passed": True}
                        for item in contract.criteria
                    ],
                }
            ],
            "_harness_context": {
                "todos": [],
                "final_content": "行业新闻。https://example.com/news",
                "verification_activations": [
                    activation.model_dump(mode="json")
                ],
            },
        },
    )

    analytics = next(
        item
        for item in report.evaluations
        if item.criterion_id == "analytics_evidence_traceability"
    )
    assert analytics.passed is False
    assert report.status == VerificationStatus.NEEDS_REVISION


def test_failed_run_terminalization_preserves_authoritative_activation(tmp_path):
    from harness.coordinators import HarnessRunCoordinator

    sessions = SessionManager()
    sessions.initialize(tmp_path)
    sessions.create_session("session-1")
    coordinator = HarnessRunCoordinator(sessions)
    run, _ = coordinator.start_run(
        session_id="session-1",
        query_id="query-1",
        objective="继续处理",
        goal_mode=False,
    )
    coordinator.transition(run, RunStatus.RUNNING)
    result = ToolMessage(
        content="database_source_id: db-sales\nresult_id: result-1\nrows: 3",
        tool_call_id="call-db",
        name="database_sql_execute",
        status="success",
    )
    activation = build_verification_activations(
        run_id=run.run_id,
        query_id=run.query_id,
        tool_call_id="call-db",
        tool_name="database_sql_execute",
        args={"question": "查询销量"},
        result=result,
    )[0]
    sessions.append_run_verification_activation(
        run.session_id,
        run.run_id,
        activation.model_dump(mode="json"),
    )

    coordinator.fail(run, outcome=RunOutcome.FAILED, error="boom")
    persisted = sessions.get_run_state(run.session_id, run.run_id)

    assert persisted is not None
    assert len(persisted["verification_activations"]) == 1
    assert "analytics" in persisted["verification_contract"]["verification_packs"]


def test_contract_id_changes_when_custom_rule_semantics_change():
    first = RunRubricCompiler.compile(
        RubricBuildContext(
            user_message="解释这个概念",
            force_required=True,
            custom_rules=(
                {
                    "id": "custom_rule",
                    "statement": "必须解释状态边界。",
                    "required": True,
                },
            ),
        )
    )
    second = RunRubricCompiler.compile(
        RubricBuildContext(
            user_message="解释这个概念",
            force_required=True,
            custom_rules=(
                {
                    "id": "custom_rule",
                    "statement": "可以不解释状态边界。",
                    "required": False,
                },
            ),
        )
    )

    assert first is not None and second is not None
    assert first.contract_id != second.contract_id


def test_managed_criteria_declare_explicit_evidence_scope():
    contract = RunRubricCompiler.compile(
        RubricBuildContext(
            user_message="搜索资料、分析数据并修改代码报告",
            force_required=True,
            task_profile=RunTaskProfile(
                primary_intent="mixed",
                initial_packs=["web_research", "analytics", "artifact", "code"],
            ),
        )
    )
    assert contract is not None
    scopes = {item.id: item.evidence_scope.value for item in contract.criteria}

    assert scopes["todo_reconciliation"] == "goal_inheritable"
    assert scopes["web_evidence_traceability"] == "goal_inheritable"
    assert scopes["analytics_evidence_traceability"] == "goal_inheritable"
    assert scopes["artifact_delivery"] == "artifact_bound"
    assert scopes["code_validation"] == "artifact_bound"
    assert scopes["task_fulfillment"] == "run_only"


def test_code_validation_inheritance_is_invalidated_by_artifact_hash_change(tmp_path):
    from harness.deterministic_checks import _evaluate_code_validation

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    source = workspace / "app.py"
    source.write_text("VALUE = 1\n", encoding="utf-8")
    write_activation = next(
        item
        for item in build_verification_activations(
            run_id="run-1",
            query_id="query-1",
            tool_call_id="call-write",
            tool_name="write_file",
            args={"file_path": "/workspace/app.py", "content": "VALUE = 1\n"},
            result=ToolMessage(
                content="Updated /workspace/app.py",
                tool_call_id="call-write",
                name="write_file",
            ),
            workspace_path=str(workspace),
        )
        if item.pack == "code"
    )
    validation_activation = next(
        item
        for item in build_verification_activations(
            run_id="run-1",
            query_id="query-1",
            tool_call_id="call-test",
            tool_name="execute",
            args={"command": "pytest -q"},
            result=ToolMessage(
                content="1 passed\n[Command succeeded with exit code 0]",
                tool_call_id="call-test",
                name="execute",
            ),
        )
        if item.pack == "code"
    )
    inherited = [
        {
            **ref,
            "verification_pack": "code",
            "origin_run_id": activation.run_id,
        }
        for activation in (write_activation, validation_activation)
        for ref in activation.evidence_refs
        if ref.get("material") is True
    ]
    context = {
        "workspace_path": str(workspace),
        "run_id": "run-2",
        "verification_activations": [],
        "goal_evidence_refs": inherited,
    }

    assert _evaluate_code_validation("code_validation", context, {}).passed is True
    source.write_text("VALUE = 2\n", encoding="utf-8")
    changed = _evaluate_code_validation("code_validation", context, {})
    assert changed.passed is False
    assert "hash 已变化" in str(changed.gap)


def test_runtime_pack_expansion_preserves_custom_rules():
    profile = TaskProfileClassifier.classify(message="解释这个概念")
    declared = RunRubricCompiler.compile(
        RubricBuildContext(
            user_message="解释这个概念",
            force_required=True,
            task_profile=profile,
            custom_rules=(
                {
                    "id": "advanced_custom_rule",
                    "statement": "必须说明权威状态边界。",
                    "required": True,
                    "verifier": "llm_grader",
                    "source": "settings",
                },
            ),
        )
    )
    assert declared is not None
    activation = build_verification_activations(
        run_id="run-1",
        query_id="query-1",
        tool_call_id="call-db",
        tool_name="database_sql_execute",
        args={},
    )[0]

    effective = RunRubricCompiler.expand_for_activations(
        contract=declared,
        profile=profile,
        message="解释这个概念",
        activations=[activation],
    )

    assert effective is not None
    custom = next(
        item for item in effective.criteria if item.id == "advanced_custom_rule"
    )
    assert custom.statement == "必须说明权威状态边界。"
    assert custom.required is True
    assert custom.source.value == "settings"
