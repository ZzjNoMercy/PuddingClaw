"""Contract tests for Run verification and explicit cross-Run Goal mode."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from graph.session_manager import SessionManager
from graph.verification.models import stable_digest
from graph.verification.orchestrator import OnlineVerificationOrchestrator
from harness.coordinators import GoalActivationError, HarnessRunCoordinator
from harness.deterministic_checks import evaluate_deterministic_criteria
from harness.models import (
    GoalCompletionPolicy,
    GoalRecord,
    GoalStatus,
    GoalTurnIntent,
    HarnessStateError,
    RunKind,
    RunOutcome,
    RunRecord,
    RunStatus,
    RunTaskProfile,
    SkillActivation,
    SkillCandidate,
    VerificationMode,
    VerificationStatus,
    VerifierKind,
)
from harness.rubric_compiler import RubricBuildContext, RunRubricCompiler
from harness.verification_activations import build_verification_activations


def _sessions(tmp_path) -> SessionManager:
    sessions = SessionManager()
    sessions.initialize(tmp_path)
    sessions.create_session("session-1", metadata={"runtime_mode": "agent"})
    return sessions


def _verification_context(workspace: Path) -> dict:
    artifact = workspace / "report.md"
    artifact.write_text("# report\n", encoding="utf-8")
    return {
        "todos": [],
        "final_content": "报告已生成：`/workspace/report.md`",
        "workspace_path": str(workspace),
    }


def _start_rubric_goal(
    coordinator: HarnessRunCoordinator,
    **kwargs,
):
    """Start or continue a Goal under the explicit Rubric policy."""

    return coordinator.start_run(
        **kwargs,
        completion_policy=GoalCompletionPolicy.RUBRIC,
    )


def _request_goal_completion(
    sessions: SessionManager,
    run: RunRecord,
    goal: GoalRecord,
) -> None:
    """Create the explicit completion declaration required before grading."""

    sessions.record_goal_completion_request(
        run.session_id,
        goal_id=goal.goal_id,
        objective_revision=goal.objective_revision,
        run_id=run.run_id,
        tool_call_id=f"complete-{run.run_id}",
    )


def _materialize_current_proposal(
    sessions: SessionManager,
    coordinator: HarnessRunCoordinator,
    run: RunRecord,
    goal: GoalRecord,
    final_state: dict,
) -> dict:
    """Simulate the post-freeze verifier boundary for coordinator unit tests."""

    coordinator._refresh_runtime_fields(run)
    context = dict(final_state.get("_harness_context") or {})
    persisted_goal = sessions.get_goal_state(run.session_id, goal.goal_id) or {}
    goal_refs = [item for item in persisted_goal.get("evidence_refs") or [] if isinstance(item, dict)]
    goal_records = []
    for evidence_ref in goal_refs:
        resolved = sessions.resolve_evidence_ref(
            run.session_id,
            evidence_ref,
            goal_id=goal.goal_id,
            goal_revision=goal.objective_revision,
            allow_artifact_revision_inheritance=True,
        )
        if not isinstance(resolved, dict):
            continue
        record = dict(resolved.get("payload") or {})
        record.update(
            {
                "evidence_ref": evidence_ref,
                "evidence_type": resolved.get("kind"),
                "evidence_id": resolved.get("id"),
                "verification_pack": resolved.get("verification_pack"),
                "origin_run_id": resolved.get("source_run_id"),
                "run_id": resolved.get("source_run_id"),
                "tool_call_id": resolved.get("origin_tool_call_id"),
                "output_digest": resolved.get("output_digest"),
                "source_goal_revision": resolved.get("goal_revision"),
                "revision_inherited": bool(
                    resolved.get("goal_revision") is not None
                    and int(resolved["goal_revision"]) < goal.objective_revision
                ),
            }
        )
        goal_records.append(record)
    context.update(
        {
            "verification_activations": [
                item.model_dump(mode="json") for item in run.verification_activations
            ],
            "goal_evidence_refs": goal_refs,
            "goal_evidence_records": goal_records,
            "run_id": run.run_id,
            "goal_id": goal.goal_id,
            "goal_revision": goal.objective_revision,
            "declared_artifact_targets": list(run.declared_artifact_targets),
            "active_permission_grant_ids": [
                str(item.get("id"))
                for item in sessions.list_permission_grants(run.session_id)
                if item.get("id")
            ],
            "permission_grants_authoritative": True,
        }
    )
    candidate = str(context.get("final_content") or "completed")
    verifier_state = {
        **final_state,
        "_harness_context": context,
        "messages": [HumanMessage(content=run.objective), AIMessage(content=candidate)],
    }
    orchestrator = OnlineVerificationOrchestrator(sessions)
    snapshot = orchestrator.freeze_goal_snapshot(
        run=run,
        goal=goal,
        final_state=verifier_state,
        workspace_fingerprint=stable_digest(
            {"workspace_path": str(context.get("workspace_path") or "")}
        ),
    )
    deterministic = [
        item.model_dump(mode="json")
        for item in evaluate_deterministic_criteria(run.verification_contract, verifier_state)
    ]
    semantic_ids = {
        item.id
        for item in run.verification_contract.criteria
        if item.verifier == VerifierKind.LLM_GRADER
    }
    raw_evaluations = final_state.get("_rubric_evaluations") or []
    semantic = dict(raw_evaluations[-1]) if raw_evaluations else None
    if semantic is not None:
        semantic["criteria"] = [
            item
            for item in semantic.get("criteria") or []
            if str(item.get("name") or "") in semantic_ids
        ]
    report = orchestrator.materialize_goal_proposal_from_verifiers(
        run=run,
        goal=goal,
        snapshot=snapshot,
        deterministic_evaluations=deterministic,
        semantic_evaluation=semantic,
    )
    return {
        **verifier_state,
        "_evaluation_snapshot_id": snapshot.snapshot_id,
        "_verification_proposal_report": report.model_dump(mode="json"),
    }


def test_run_verification_mode_is_owned_by_explicit_goal_state(tmp_path: Path) -> None:
    sessions = _sessions(tmp_path)
    coordinator = HarnessRunCoordinator(sessions)

    ordinary, ordinary_goal = coordinator.start_run(
        session_id="session-1",
        query_id="query-ordinary",
        objective="HTML 中 HUD 数据是多少？",
        goal_mode=False,
    )
    assert ordinary_goal is None
    assert ordinary.verification_mode == VerificationMode.AGENT
    assert ordinary.requires_goal_verification is False
    coordinator.fail(ordinary, outcome=RunOutcome.CANCELLED, error="test boundary")

    strict, goal = _start_rubric_goal(
        coordinator,
        session_id="session-1",
        query_id="query-goal",
        objective="刷新完整分析报告并严格验收",
        goal_mode=True,
    )
    assert goal is not None
    assert strict.verification_mode == VerificationMode.RUBRIC
    assert strict.requires_goal_verification is True


def test_goal_inspection_references_context_without_owning_goal(tmp_path: Path) -> None:
    sessions = _sessions(tmp_path)
    coordinator = HarnessRunCoordinator(sessions)
    execution, goal = coordinator.start_run(
        session_id="session-1",
        query_id="query-goal-owner",
        objective="刷新完整报告",
        goal_mode=True,
    )
    assert goal is not None
    coordinator.fail(execution, outcome=RunOutcome.CANCELLED, error="manual stop")
    goal = coordinator.goals.release_run(goal, run=execution, gap="stopped")

    inspection, attached_goal = coordinator.start_run(
        session_id="session-1",
        query_id="query-inspection",
        objective="总结一下已经完成的工作",
        goal_mode=False,
        run_kind=RunKind.GOAL_INSPECTION,
        context_goal_id=goal.goal_id,
        context_goal_revision=goal.objective_revision,
        goal_turn_intent=GoalTurnIntent.INSPECT_GOAL,
        goal_turn_confidence=0.99,
        goal_turn_classifier="deterministic",
    )

    assert attached_goal is None
    assert inspection.run_kind == RunKind.GOAL_INSPECTION
    assert inspection.goal_id is None
    assert inspection.context_goal_id == goal.goal_id
    assert inspection.verification_mode == VerificationMode.AGENT
    assert inspection.requires_goal_verification is False
    authoritative_goal = sessions.get_goal_state("session-1", goal.goal_id)
    assert authoritative_goal is not None
    assert inspection.run_id not in authoritative_goal["run_ids"]


def test_historical_goal_evidence_does_not_activate_standalone_run(tmp_path: Path) -> None:
    sessions = _sessions(tmp_path)
    coordinator = HarnessRunCoordinator(sessions)
    goal_run, goal = coordinator.start_run(
        session_id="session-1",
        query_id="query-goal-evidence",
        objective="严格查询并刷新报告",
        goal_mode=True,
    )
    assert goal is not None
    coordinator.transition(goal_run, RunStatus.RUNNING)
    activation = build_verification_activations(
        run_id=goal_run.run_id,
        query_id=goal_run.query_id,
        tool_call_id="call-db-history",
        tool_name="database_sql_execute",
        args={"question": "查询 HUD 配置率"},
        result=ToolMessage(
            content=(
                "database_source_id: db-products\n"
                "result_id: result-history\n"
                "query_trace_id: trace-history\n"
                "rows: 7"
            ),
            tool_call_id="call-db-history",
            name="database_sql_execute",
            status="success",
        ),
    )[0]
    sessions.append_run_verification_activation(
        goal_run.session_id,
        goal_run.run_id,
        activation.model_dump(mode="json"),
    )
    coordinator.fail(
        goal_run,
        outcome=RunOutcome.CANCELLED,
        error="test goal handoff",
    )
    goal = coordinator.goals.release_run(
        goal,
        run=goal_run,
        gap="test handoff",
    )
    assert any(ref.get("type") == "analytics_result" for ref in goal.evidence_refs)
    goal.transition(GoalStatus.COMPLETED)
    sessions.upsert_goal_state("session-1", goal.model_dump(mode="json"))

    ordinary, ordinary_goal = coordinator.start_run(
        session_id="session-1",
        query_id="query-plain-after-goal",
        objective="HTML 中 HUD 数据是多少，用哪个 JS？",
        goal_mode=False,
    )

    assert ordinary_goal is None
    assert ordinary.goal_id is None
    assert ordinary.verification_mode == VerificationMode.AGENT
    assert ordinary.verification_activations == []
    assert ordinary.verification_contract is None
    assert ordinary.declared_verification_contract is None


def test_standalone_artifact_follow_up_uses_delta_repair_without_reopening_goal(
    tmp_path: Path,
) -> None:
    sessions = _sessions(tmp_path)
    target = tmp_path / "report.html"
    target.write_text("report", encoding="utf-8")
    delivered = sessions.register_delivered_artifact(
        "session-1",
        target_path=str(target),
        content_sha256="sha256:"
        + hashlib.sha256(target.read_bytes()).hexdigest(),
        source_run_id="run-old",
        source_query_id="query-old",
        source_goal_id="goal-achieved",
        source_goal_revision=1,
    )

    run, goal = HarnessRunCoordinator(sessions).start_run(
        session_id="session-1",
        query_id="query-followup",
        objective="report.html 里的年份还没有更新，请修复",
        goal_mode=False,
        verification_enabled=False,
    )

    assert goal is None
    assert run.goal_id is None
    assert run.follow_up_of_goal_id == "goal-achieved"
    assert run.follow_up_of_artifact_ids == [delivered["artifact_id"]]
    assert run.execution_mode == "delta_repair"
    assert run.delta_repair_kind == "presentation_only"
    assert run.delta_repair_tool_budget == 6


def test_ui_only_follow_up_gets_six_call_presentation_policy(tmp_path: Path) -> None:
    sessions = _sessions(tmp_path)
    target = tmp_path / "report.html"
    target.write_text("<select><option>2024</option></select>", encoding="utf-8")
    delivered = sessions.register_delivered_artifact(
        "session-1",
        target_path=str(target),
        content_sha256="sha256:" + hashlib.sha256(target.read_bytes()).hexdigest(),
        source_run_id="run-old",
        source_query_id="query-old",
    )

    run, _goal = HarnessRunCoordinator(sessions).start_run(
        session_id="session-1",
        query_id="query-ui-followup",
        objective=f"{target.name} 的下拉选项只到 2024，请修复显示",
        goal_mode=False,
        verification_enabled=False,
    )

    assert run.follow_up_of_artifact_ids == [delivered["artifact_id"]]
    assert run.delta_repair_kind == "presentation_only"
    assert run.delta_repair_tool_budget == 6


def _satisfied_final_state(workspace: Path) -> dict:
    activation = {
        "activation_id": "verification-activation-test",
        "run_id": "run-test",
        "query_id": "query-1",
        "tool_call_id": "call-db",
        "tool_name": "database_sql_execute",
        "pack": "analytics",
        "source": "tool",
        "status": "succeeded",
        "evidence_refs": [
            {
                "kind": "tool_execution",
                "tool_call_id": "call-db",
                "tool_name": "database_sql_execute",
            }
        ],
    }
    return {
        "_harness_context": {
            **_verification_context(workspace),
            "verification_activations": [activation],
        },
        "verification_activations": [activation],
        "_rubric_status": "satisfied",
        "_rubric_evaluations": [
            {
                "grading_run_id": "grading-1",
                "iteration": 0,
                "result": "satisfied",
                "explanation": "全部标准均有证据。",
                "criteria": [
                    {"name": "task_fulfillment", "passed": True},
                    {"name": "metric_consistency", "passed": True},
                    {
                        "name": "analytics_evidence_traceability",
                        "passed": True,
                    },
                    {"name": "time_scope", "passed": True},
                    {"name": "artifact_delivery", "passed": True},
                    {"name": "report_integrity", "passed": True},
                ],
            }
        ],
    }


def _persist_satisfied_evidence(
    sessions: SessionManager,
    run: RunRecord,
    workspace: Path,
) -> None:
    artifact = workspace / "report.md"
    artifact.write_text("# report\n", encoding="utf-8")
    cases = [
        (
            "call-db",
            "database_sql_execute",
            {"question": "查询销量"},
            "database_source_id: db-sales\nresult_id: result-sales-1\nrows: 12",
        ),
        (
            "call-write",
            "write_file",
            {"file_path": "/workspace/report.md", "content": "# report"},
            "Wrote file /workspace/report.md",
        ),
    ]
    for tool_call_id, tool_name, args, content in cases:
        result = ToolMessage(
            content=content,
            tool_call_id=tool_call_id,
            name=tool_name,
            status="success",
        )
        for activation in build_verification_activations(
            run_id=run.run_id,
            query_id=run.query_id,
            tool_call_id=tool_call_id,
            tool_name=tool_name,
            args=args,
            result=result,
            session_id=run.session_id,
            workspace_path=str(workspace),
        ):
            sessions.append_run_verification_activation(
                run.session_id,
                run.run_id,
                activation.model_dump(mode="json"),
            )


def _exhausted_final_state(workspace: Path) -> dict:
    return {
        "_harness_context": _verification_context(workspace),
        "_rubric_status": "max_iterations_reached",
        "_rubric_evaluations": [
            {
                "grading_run_id": "grading-1",
                "iteration": 1,
                "result": "needs_revision",
                "explanation": "报告没有给出数据来源。",
                "criteria": [
                    {
                        "name": "metric_consistency",
                        "passed": False,
                        "gap": "关键销量指标口径不一致。",
                    }
                ],
            }
        ],
    }


def test_run_rubric_is_independent_from_goal_mode():
    contract = RunRubricCompiler.compile(
        RubricBuildContext(
            user_message="刷新 6 月的销量分析报告模板",
            analytics_model_id="汽车行业综合分析",
        )
    )

    assert contract is not None
    assert contract.task_type == "analytics_artifact"
    assert {
        "task_fulfillment",
        "metric_consistency",
        "analytics_evidence_traceability",
        "time_scope",
        "artifact_delivery",
        "report_integrity",
    } <= {criterion.id for criterion in contract.criteria}
    assert "[time_scope]" in contract.rubric


def test_harness_custom_rule_is_compiled_with_settings_source():
    contract = RunRubricCompiler.compile(
        RubricBuildContext(
            user_message="分析 6 月销量下降原因",
            custom_rules=(
                {
                    "id": "quantified_impact",
                    "enabled": True,
                    "statement": "主要原因必须给出影响量级",
                    "required": True,
                    "verifier": "analytics",
                },
            ),
        )
    )

    assert contract is not None
    criterion = next(item for item in contract.criteria if item.id == "quantified_impact")
    assert criterion.source.value == "settings"
    assert criterion.verifier.value == "analytics"


def test_plain_chat_does_not_pay_rubric_tax():
    assert RunRubricCompiler.compile(RubricBuildContext(user_message="你好，介绍一下你自己")) is None


def test_explicit_rubric_goal_freezes_a_verification_contract(tmp_path):
    sessions = _sessions(tmp_path)
    coordinator = HarnessRunCoordinator(sessions)

    run, goal = _start_rubric_goal(
        coordinator,
        session_id="session-1",
        query_id="query-goal-contract",
        objective="完成这个目标",
        goal_mode=True,
    )

    assert goal is not None
    assert run.verification_contract is not None
    assert goal.goal_contract is not None
    assert run.verification_contract.contract_id == goal.goal_contract.contract_id
    assert {
        "task_fulfillment",
        "todo_reconciliation",
    } <= {item.id for item in run.verification_contract.criteria}


def test_create_workspace_artifact_compiles_artifact_delivery():
    contract = RunRubricCompiler.compile(RubricBuildContext(user_message="创建 /workspace/result.md 并引用该路径"))

    assert contract is not None
    assert contract.task_type == "artifact"
    assert "artifact_delivery" in {item.id for item in contract.criteria}


def test_verification_can_be_disabled_without_false_grader_error(tmp_path):
    sessions = _sessions(tmp_path)
    coordinator = HarnessRunCoordinator(sessions)
    run, goal = coordinator.start_run(
        session_id="session-1",
        query_id="query-1",
        objective="刷新 6 月报告",
        goal_mode=False,
        verification_enabled=False,
    )
    coordinator.transition(run, RunStatus.RUNNING)

    run, _, report = coordinator.complete_from_final_state(run, goal, {})

    assert run.outcome == RunOutcome.COMPLETED
    assert report.status == VerificationStatus.NOT_REQUIRED


def test_goal_without_verification_keeps_decision_and_run_acceptance_consistent(tmp_path):
    sessions = _sessions(tmp_path)
    coordinator = HarnessRunCoordinator(sessions)
    run, goal = coordinator.start_run(
        session_id="session-1",
        query_id="query-goal-no-verification",
        objective="完成一个无需 rubric 的目标",
        goal_mode=True,
        verification_enabled=False,
    )
    assert goal is not None
    coordinator.transition(run, RunStatus.RUNNING)
    _request_goal_completion(sessions, run, goal)

    run, goal, report = coordinator.complete_from_final_state(run, goal, {})

    assert report is None
    assert goal is not None and goal.status == GoalStatus.COMPLETED
    assert goal.latest_completion_request_id == run.completion_request_id
    assert goal.latest_goal_decision is None


def test_deterministic_revision_state_is_not_reported_as_grader_error(tmp_path):
    sessions = _sessions(tmp_path)
    coordinator = HarnessRunCoordinator(sessions)
    run, goal = _start_rubric_goal(
        coordinator,
        session_id="session-1",
        query_id="query-deterministic-revision",
        objective="生成销量报告并提供文件",
        goal_mode=True,
    )
    coordinator.transition(run, RunStatus.RUNNING)
    assert goal is not None
    _request_goal_completion(sessions, run, goal)

    completed, _, report = coordinator.complete_from_final_state(
        run,
        goal,
        {
            "_completion_gate_status": "needs_revision",
            "_harness_context": {
                "todos": [{"id": "todo-1", "content": "完成任务", "status": "in_progress"}],
                "final_content": "尚未完成",
                "workspace_path": str(tmp_path),
            },
        },
    )

    assert completed.outcome == RunOutcome.FAILED
    assert report.status == VerificationStatus.INCOMPLETE
    assert any(item.passed is None for item in report.evaluations)


def test_missing_terminal_verdict_is_reported_as_incomplete(tmp_path):
    sessions = SessionManager()
    sessions.initialize(tmp_path)
    coordinator = HarnessRunCoordinator(sessions)
    contract = coordinator.verification.compile_contract(
        user_message="生成销量报告并提供文件",
        analytics_model_id=None,
        project_id="project-1",
    )

    assert contract is not None
    report = coordinator.verification.report_from_final_state(
        run_id="run-incomplete",
        contract=contract,
        final_state={"_completion_gate_iterations": 1},
    )

    assert report.status == VerificationStatus.INCOMPLETE
    assert report.iteration_count == 1
    assert all("验收器未返回" not in gap for gap in report.gaps)
    assert all("验收器异常" not in gap for gap in report.gaps)


def test_default_request_creates_one_run_without_goal(tmp_path):
    sessions = _sessions(tmp_path)
    coordinator = HarnessRunCoordinator(sessions)

    run, goal = coordinator.start_run(
        session_id="session-1",
        query_id="query-1",
        objective="刷新 6 月的销量分析报告模板",
        goal_mode=False,
        analytics_model_id="汽车行业综合分析",
    )

    assert goal is None
    assert run.goal_id is None
    assert run.verification_contract is not None
    assert sessions.get_active_goal_state("session-1") is None
    assert sessions.get_harness_state("session-1")["run_order"] == [run.run_id]

    coordinator.transition(run, RunStatus.RUNNING)
    completed, completed_goal, report = coordinator.complete_from_final_state(
        run,
        goal,
        _satisfied_final_state(tmp_path),
    )

    assert completed_goal is None
    assert completed.verification_mode == VerificationMode.AGENT
    assert report.status == VerificationStatus.NOT_REQUIRED, report.model_dump(mode="json")
    assert completed.status == RunStatus.COMPLETED
    assert completed.outcome == RunOutcome.COMPLETED
    harness = sessions.get_harness_state("session-1")
    assert len(harness["runs"]) == 1
    assert harness["goals"] == {}


def test_non_goal_run_does_not_enter_completion_repair_loop(tmp_path):
    sessions = _sessions(tmp_path)
    coordinator = HarnessRunCoordinator(sessions)
    run, goal = coordinator.start_run(
        session_id="session-1",
        query_id="query-1",
        objective="刷新 6 月的销量分析报告模板",
        goal_mode=False,
    )
    coordinator.transition(run, RunStatus.RUNNING)

    completed, _, report = coordinator.complete_from_final_state(
        run,
        goal,
        _exhausted_final_state(tmp_path),
    )

    assert completed.outcome == RunOutcome.COMPLETED
    assert completed.verification_mode == VerificationMode.AGENT
    assert report.status == VerificationStatus.NOT_REQUIRED
    harness = sessions.get_harness_state("session-1")
    assert len(harness["runs"]) == 1
    assert harness["goals"] == {}


def test_deterministic_todo_gate_overrides_satisfied_grader(tmp_path):
    sessions = _sessions(tmp_path)
    coordinator = HarnessRunCoordinator(sessions)
    run, goal = _start_rubric_goal(
        coordinator,
        session_id="session-1",
        query_id="query-1",
        objective="分析 6 月销量并给出结论",
        goal_mode=True,
    )
    coordinator.transition(run, RunStatus.RUNNING)
    assert goal is not None
    _request_goal_completion(sessions, run, goal)
    state = _satisfied_final_state(tmp_path)
    pending_todos = [{
        "id": "todo-1",
        "content": "补齐数据来源",
        "status": "in_progress",
        "goal_id": run.goal_id,
        "goal_revision": run.goal_revision,
        "created_run_id": run.run_id,
    }]
    sessions.update_todos(
        run.session_id,
        pending_todos,
        goal_id=run.goal_id,
        goal_revision=run.goal_revision,
    )
    state["_harness_context"]["todos"] = pending_todos

    completed, _, report = coordinator.complete_from_final_state(run, goal, state)

    assert completed.outcome == RunOutcome.VERIFICATION_FAILED
    assert report.status == VerificationStatus.NEEDS_REVISION
    assert report.explanation.startswith("确定性检查失败：")
    todo_evaluation = next(item for item in report.evaluations if item.criterion_id == "todo_reconciliation")
    assert todo_evaluation.passed is False
    assert "补齐数据来源" in str(todo_evaluation.gap)


def test_missing_artifact_overrides_satisfied_grader(tmp_path):
    sessions = _sessions(tmp_path)
    coordinator = HarnessRunCoordinator(sessions)
    run, goal = _start_rubric_goal(
        coordinator,
        session_id="session-1",
        query_id="query-1",
        objective="生成 6 月销量报告",
        goal_mode=True,
    )
    coordinator.transition(run, RunStatus.RUNNING)
    assert goal is not None
    _request_goal_completion(sessions, run, goal)
    result = ToolMessage(
        content="Updated file /workspace/missing.md",
        tool_call_id="call-missing",
        name="write_file",
        status="success",
    )
    activation = next(
        item
        for item in build_verification_activations(
            run_id=run.run_id,
            query_id=run.query_id,
            tool_call_id="call-missing",
            tool_name="write_file",
            args={"file_path": "/workspace/missing.md", "content": "missing"},
            result=result,
            workspace_path=str(tmp_path),
        )
        if item.pack == "artifact"
    )
    sessions.append_run_verification_activation(
        run.session_id,
        run.run_id,
        activation.model_dump(mode="json"),
    )
    state = _satisfied_final_state(tmp_path)
    state["_harness_context"]["final_content"] = "已生成：`/workspace/missing.md`"

    completed, _, report = coordinator.complete_from_final_state(run, goal, state)

    assert completed.outcome == RunOutcome.VERIFICATION_FAILED
    assert report.status == VerificationStatus.NEEDS_REVISION
    artifact_evaluation = next(item for item in report.evaluations if item.criterion_id == "artifact_delivery")
    assert artifact_evaluation.passed is False
    assert "missing.md" in str(artifact_evaluation.gap)


def test_workspace_copy_cannot_replace_declared_external_target(tmp_path):
    sessions = _sessions(tmp_path)
    coordinator = HarnessRunCoordinator(sessions)
    external = tmp_path / "outside" / "报告 模板 v2.html"
    run, goal = _start_rubric_goal(
        coordinator,
        session_id="session-1",
        query_id="query-external-target",
        objective=f"{external} 刷新这个报告到 2026 年",
        goal_mode=True,
    )
    coordinator.transition(run, RunStatus.RUNNING)
    assert goal is not None
    _request_goal_completion(sessions, run, goal)
    workspace_copy = tmp_path / "报告 模板 v2.html"
    workspace_copy.write_text("<html>2026</html>", encoding="utf-8")
    result = ToolMessage(
        content="Updated file /workspace/报告 模板 v2.html",
        tool_call_id="call-workspace-copy",
        name="write_file",
        status="success",
    )
    activation = next(
        item
        for item in build_verification_activations(
            run_id=run.run_id,
            query_id=run.query_id,
            tool_call_id="call-workspace-copy",
            tool_name="write_file",
            args={"file_path": "/workspace/报告 模板 v2.html", "content": "2026"},
            result=result,
            workspace_path=str(tmp_path),
        )
        if item.pack == "artifact"
    )
    sessions.append_run_verification_activation(
        run.session_id,
        run.run_id,
        activation.model_dump(mode="json"),
    )
    state = _satisfied_final_state(tmp_path)
    completed, _, report = coordinator.complete_from_final_state(run, goal, state)

    artifact_evaluation = next(
        item for item in report.evaluations if item.criterion_id == "artifact_delivery"
    )
    assert completed.outcome == RunOutcome.VERIFICATION_FAILED
    assert artifact_evaluation.passed is False
    assert "workspace 副本不能替代" in str(artifact_evaluation.gap)
    assert str(external) in str(artifact_evaluation.gap)


def test_goal_inherits_authorized_external_artifact_across_runs(tmp_path):
    from graph.session_manager import session_manager

    session_manager.initialize(tmp_path)
    session_manager.create_session("session-external-goal", metadata={"runtime_mode": "agent"})
    coordinator = HarnessRunCoordinator(session_manager)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    external = tmp_path / "outside" / "报告 模板 v2.md"
    external.parent.mkdir()
    external.write_text("# 2026\n", encoding="utf-8")
    session_manager.add_permission_grant(
        "session-external-goal",
        grant_type="external_file_write",
        target_kind="exact_file",
        target=str(external.resolve()),
        capabilities=["write", "external_path"],
    )

    first, goal = _start_rubric_goal(
        coordinator,
        session_id="session-external-goal",
        query_id="query-1",
        objective=f"更新外部报告 {external}",
        goal_mode=True,
    )
    assert goal is not None
    coordinator.transition(first, RunStatus.RUNNING)
    _request_goal_completion(session_manager, first, goal)
    result = ToolMessage(
        content=f"Updated file {external}",
        tool_call_id="call-write",
        name="edit_file",
        status="success",
    )
    activation = next(
        item
        for item in build_verification_activations(
            run_id=first.run_id,
            query_id=first.query_id,
            tool_call_id="call-write",
            tool_name="edit_file",
            args={"file_path": str(external), "old_string": "2025", "new_string": "2026"},
            result=result,
            session_id=first.session_id,
            workspace_path=str(workspace),
        )
        if item.pack == "artifact"
    )
    artifact_receipt = next(
        item for item in activation.evidence_refs if item.get("kind") == "artifact_write"
    )
    assert artifact_receipt["role"] == "target"
    assert artifact_receipt["goal_id"] == goal.goal_id
    assert artifact_receipt["goal_revision"] == 1
    assert artifact_receipt["content_sha256"].startswith("sha256:")
    session_manager.append_run_verification_activation(
        first.session_id,
        first.run_id,
        activation.model_dump(mode="json"),
    )
    failed_state = {
        "_rubric_status": "max_iterations_reached",
        "_rubric_evaluations": [{
            "result": "max_iterations_reached",
            "criteria": [
                {"name": "task_fulfillment", "passed": False, "gap": "仍需补充总结。"},
                {"name": "todo_reconciliation", "passed": True},
                {"name": "artifact_delivery", "passed": True},
                {"name": "code_validation", "passed": True},
                {"name": "report_integrity", "passed": True},
            ],
        }],
        "_harness_context": {"workspace_path": str(workspace), "todos": []},
    }
    first, goal, first_report = coordinator.complete_from_final_state(first, goal, failed_state)
    assert first_report.status == VerificationStatus.MAX_ITERATIONS_REACHED
    assert goal is not None and goal.evidence_refs

    second, goal = coordinator.start_run(
        session_id="session-external-goal",
        query_id="query-2",
        objective="继续完成",
        goal_mode=True,
        goal_id=goal.goal_id,
    )
    coordinator.transition(second, RunStatus.RUNNING)
    assert goal is not None
    _request_goal_completion(session_manager, second, goal)
    validation_result = ToolMessage(
        content="1 passed\n[Command succeeded with exit code 0]",
        tool_call_id="call-test",
        name="execute",
        status="success",
    )
    validation = next(
        item
        for item in build_verification_activations(
            run_id=second.run_id,
            query_id=second.query_id,
            tool_call_id="call-test",
            tool_name="execute",
            # A model-authored script name is not verifier authority. Use a
            # recognized project-test runner, which the Harness can sign as a
            # completion receipt without granting artifact commit authority.
            args={"command": "pytest tests/test_report.py"},
            result=validation_result,
            session_id=second.session_id,
            workspace_path=str(workspace),
            goal_id=second.goal_id,
            goal_revision=second.goal_revision,
        )
        if item.pack == "code"
    )
    assert any(
        item.get("kind") == "validation_receipt"
        for item in validation.evidence_refs
    ), validation.model_dump(mode="json")
    session_manager.append_run_verification_activation(
        second.session_id,
        second.run_id,
        validation.model_dump(mode="json"),
    )
    satisfied_state = {
        "_rubric_status": "satisfied",
        "_rubric_evaluations": [{
            "result": "satisfied",
            "criteria": [
                {"name": "task_fulfillment", "passed": True},
                {"name": "todo_reconciliation", "passed": True},
                {"name": "artifact_delivery", "passed": True},
                {"name": "code_validation", "passed": True},
                {"name": "report_integrity", "passed": True},
            ],
        }],
        "_harness_context": {
            "workspace_path": str(workspace),
            "todos": [],
            "final_content": f"已更新外部报告：`{external}`",
        },
    }
    satisfied_state = _materialize_current_proposal(
        session_manager,
        coordinator,
        second,
        goal,
        satisfied_state,
    )
    second, goal, report = coordinator.complete_from_final_state(second, goal, satisfied_state)

    artifact = next(item for item in report.evaluations if item.criterion_id == "artifact_delivery")
    assert artifact.passed is True
    assert artifact.evidence[0]["current_run_count"] == 0
    assert artifact.evidence[0]["inherited_count"] == 1
    assert report.status == VerificationStatus.SATISFIED, report.model_dump(mode="json")
    assert second.outcome == RunOutcome.COMPLETED
    assert goal is not None and goal.status == GoalStatus.COMPLETED


def test_run_model_call_limit_does_not_exhaust_goal_budget_early(tmp_path):
    sessions = _sessions(tmp_path)
    coordinator = HarnessRunCoordinator(sessions)
    run, goal = coordinator.start_run(
        session_id="session-1",
        query_id="query-1",
        objective="跨 Run 完成分析",
        goal_mode=True,
        goal_max_rounds=3,
    )
    assert goal is not None
    coordinator.transition(run, RunStatus.RUNNING)

    run, goal, report = coordinator.complete_budget_exceeded(
        run,
        goal,
        reason="run_model_call_limit",
        model_call_count=10,
        detail="run limit 10/10",
    )

    assert run.outcome == RunOutcome.BUDGET_EXCEEDED
    assert run.budget_exhaustion_reason == "run_model_call_limit"
    assert report.status == VerificationStatus.BUDGET_EXCEEDED
    assert goal is not None
    assert goal.status == GoalStatus.ACTIVE
    assert goal.model_call_count == 10
    assert goal.current_run_id is None


def test_goal_skill_activation_inherits_across_same_revision_without_router_candidate(tmp_path):
    sessions = _sessions(tmp_path)
    coordinator = HarnessRunCoordinator(sessions)
    profile = RunTaskProfile(
        skill_candidates=[
            SkillCandidate(
                skill_id="database-analysis",
                confidence=0.95,
                evidence="数据库分析任务",
            )
        ]
    )
    first, goal = coordinator.start_run(
        session_id="session-1",
        query_id="query-skill-1",
        objective="分析数据库",
        goal_mode=True,
        task_profile=profile,
    )
    assert goal is not None
    coordinator.transition(first, RunStatus.RUNNING)
    sessions.record_run_skill_activation(
        first.session_id,
        first.run_id,
        {
            "activation_id": "skill-activation-db",
            "skill_id": "database-analysis",
            "run_id": first.run_id,
            "skill_content_sha256": "sha256:abc",
            "toolsets": ["database_analysis"],
            "unlocked_tools": ["database_sql_generate"],
            "source_tool_call_id": "read-skill",
        },
    )
    first, goal, _ = coordinator.complete_budget_exceeded(
        first,
        goal,
        reason="run_model_call_limit",
        model_call_count=10,
        detail="run limit",
    )
    assert goal is not None

    followup, same_goal = coordinator.start_run(
        session_id="session-1",
        query_id="query-skill-2",
        objective="继续",
        goal_mode=True,
        goal_id=goal.goal_id,
        task_profile=profile,
    )
    inherited = sessions.get_effective_run_skill_activations(
        followup.session_id,
        followup.run_id,
    )
    assert [item["skill_id"] for item in inherited] == ["database-analysis"]

    assert same_goal is not None
    coordinator.transition(followup, RunStatus.RUNNING)
    _, same_goal, _ = coordinator.complete_budget_exceeded(
        followup,
        same_goal,
        reason="run_model_call_limit",
        model_call_count=20,
        detail="run limit",
    )
    assert same_goal is not None
    unrelated, _ = coordinator.start_run(
        session_id="session-1",
        query_id="query-skill-3",
        objective="继续",
        goal_mode=True,
        goal_id=same_goal.goal_id,
        task_profile=RunTaskProfile(),
    )
    # The verified Goal-revision activation is capability authority. A soft
    # router miss on a short continuation must not silently revoke it.
    inherited_without_candidate = sessions.get_effective_run_skill_activations(
        unrelated.session_id,
        unrelated.run_id,
    )
    assert [item["skill_id"] for item in inherited_without_candidate] == [
        "database-analysis"
    ]


def test_achieved_goal_skill_activation_does_not_leak_to_standalone_run(tmp_path):
    sessions = _sessions(tmp_path)
    achieved = GoalRecord(
        goal_id="goal-achieved",
        session_id="session-1",
        objective="完成数据库报告",
        status=GoalStatus.COMPLETED,
        skill_activations=[
            SkillActivation(
                activation_id="skill-activation-old-goal",
                skill_id="database-analysis",
                scope="goal",
                run_id="run-old",
                goal_id="goal-achieved",
                goal_revision=1,
                skill_content_sha256="sha256:abc",
                toolsets=["database_analysis"],
                unlocked_tools=["database_sql_generate"],
                source_tool_call_id="read-skill",
            )
        ],
    )
    sessions.upsert_goal_state("session-1", achieved.model_dump(mode="json"))
    standalone = RunRecord(
        run_id="run-standalone",
        query_id="query-standalone",
        session_id="session-1",
        objective="问一个新问题",
        task_profile=RunTaskProfile(
            skill_candidates=[
                SkillCandidate(
                    skill_id="database-analysis",
                    confidence=0.9,
                    evidence="仅作为推荐",
                )
            ]
        ),
    )
    sessions.upsert_run_state("session-1", standalone.model_dump(mode="json"))

    assert sessions.get_effective_run_skill_activations(
        "session-1", standalone.run_id
    ) == []


def test_incomplete_verification_does_not_consume_goal_business_round(tmp_path):
    sessions = _sessions(tmp_path)
    coordinator = HarnessRunCoordinator(sessions)
    run, goal = _start_rubric_goal(
        coordinator,
        session_id="session-1",
        query_id="query-incomplete",
        objective="持续生成销量报告",
        goal_mode=True,
        goal_max_rounds=1,
    )
    assert goal is not None and goal.round == 1
    coordinator.transition(run, RunStatus.RUNNING)
    _request_goal_completion(sessions, run, goal)

    completed, goal, report = coordinator.complete_from_final_state(
        run,
        goal,
        {"_rubric_status": "needs_revision"},
    )

    assert completed.outcome == RunOutcome.FAILED
    assert report.status == VerificationStatus.INCOMPLETE
    assert goal is not None
    assert goal.status == GoalStatus.ACTIVE
    assert goal.round == 0
    assert goal.gaps == []
    assert any(
        "未完成评审" in notice or "未形成合法终态" in notice
        for notice in goal.control_notices
    )
    retry, retried_goal = coordinator.start_run(
        session_id="session-1",
        query_id="query-retry",
        objective="重试",
        goal_mode=True,
        goal_id=goal.goal_id,
    )
    assert retry.goal_id == goal.goal_id
    assert retried_goal is not None and retried_goal.round == 1


def test_grader_error_does_not_consume_goal_business_round(tmp_path):
    sessions = _sessions(tmp_path)
    coordinator = HarnessRunCoordinator(sessions)
    run, goal = _start_rubric_goal(
        coordinator,
        session_id="session-1",
        query_id="query-grader-error",
        objective="持续生成销量报告",
        goal_mode=True,
        goal_max_rounds=1,
    )
    assert goal is not None and goal.round == 1
    coordinator.transition(run, RunStatus.RUNNING)
    _request_goal_completion(sessions, run, goal)

    completed, goal, report = coordinator.complete_from_final_state(
        run,
        goal,
        {"_rubric_status": "grader_error", "_rubric_error": "model unavailable"},
    )

    assert completed.outcome == RunOutcome.VERIFICATION_FAILED
    assert report.status == VerificationStatus.GRADER_ERROR
    assert goal is not None
    assert goal.status == GoalStatus.ACTIVE
    assert goal.round == 0
    assert goal.gaps == []
    assert any("模型验收器执行异常" in notice for notice in goal.control_notices)


def test_varying_control_failure_fingerprints_still_hit_total_retry_budget(tmp_path):
    sessions = _sessions(tmp_path)
    coordinator = HarnessRunCoordinator(sessions)
    goal = None

    statuses = [
        "grader_error",
        "infrastructure_error",
        "grader_error",
        "infrastructure_error",
    ]
    for attempt, status in enumerate(statuses):
        run, goal = _start_rubric_goal(
            coordinator,
            session_id="session-1",
            query_id=f"query-{attempt}",
            objective="持续生成销量报告",
            goal_mode=True,
            goal_id=goal.goal_id if goal is not None else None,
            goal_max_rounds=1,
        )
        assert goal is not None
        coordinator.transition(run, RunStatus.RUNNING)
        _request_goal_completion(sessions, run, goal)
        _, goal, report = coordinator.complete_from_final_state(
            run,
            goal,
            {
                "_rubric_status": status,
                "_rubric_error": f"request-{attempt}-unavailable",
            },
        )
        assert report.status in {
            VerificationStatus.GRADER_ERROR,
            VerificationStatus.INFRASTRUCTURE_ERROR,
        }

    assert goal is not None
    assert goal.total_control_retry_count == 4
    assert goal.status == GoalStatus.BLOCKED
    assert goal.round == 0


def test_explicit_goal_can_advance_across_runs(tmp_path):
    sessions = _sessions(tmp_path)
    coordinator = HarnessRunCoordinator(sessions)
    first_run, goal = _start_rubric_goal(
        coordinator,
        session_id="session-1",
        query_id="query-1",
        objective="持续完成销量下降根因分析，直到证据完整",
        goal_mode=True,
        analytics_model_id="汽车行业综合分析",
    )
    assert goal is not None
    assert goal.status == GoalStatus.ACTIVE
    coordinator.transition(first_run, RunStatus.RUNNING)
    _request_goal_completion(sessions, first_run, goal)

    first_run, goal, first_report = coordinator.complete_from_final_state(
        first_run,
        goal,
        _exhausted_final_state(tmp_path),
    )
    assert first_run.outcome == RunOutcome.VERIFICATION_FAILED
    assert first_report.gaps
    assert goal is not None
    assert goal.status == GoalStatus.ACTIVE
    assert sessions.get_active_goal_state("session-1")["goal_id"] == goal.goal_id

    second_run, resumed_goal = coordinator.start_run(
        session_id="session-1",
        query_id="query-2",
        objective="补齐来源并完成目标",
        goal_mode=True,
        goal_id=goal.goal_id,
        analytics_model_id="汽车行业综合分析",
    )
    assert resumed_goal is not None
    assert second_run.goal_id == goal.goal_id
    coordinator.transition(second_run, RunStatus.RUNNING)
    _request_goal_completion(sessions, second_run, resumed_goal)
    _persist_satisfied_evidence(sessions, second_run, tmp_path)
    satisfied_state = _materialize_current_proposal(
        sessions,
        coordinator,
        second_run,
        resumed_goal,
        _satisfied_final_state(tmp_path),
    )
    coordinator.transition(second_run, RunStatus.EVALUATING)
    second_run.model_call_count = 11
    second_run, achieved_goal, second_report = coordinator.complete_from_final_state(
        second_run,
        resumed_goal,
        satisfied_state,
    )

    assert second_report.status == VerificationStatus.SATISFIED
    assert second_run.outcome == RunOutcome.COMPLETED
    assert achieved_goal is not None
    assert achieved_goal.status == GoalStatus.COMPLETED
    assert achieved_goal.model_call_count == 11
    assert achieved_goal.run_ids == [first_run.run_id, second_run.run_id]
    assert second_report.accepted_for_goal_revision is None
    assert second_run.run_id in second_report.supporting_run_ids
    # Acceptance is a candidate until the final assistant message is ready;
    # commit all authorities and the message in one Session write.
    assert sessions.get_active_goal_state("session-1") is not None
    sessions.commit_accepted_completion(
        "session-1",
        run=second_run.model_dump(mode="json"),
        goal=achieved_goal.model_dump(mode="json"),
        query_id=second_run.query_id,
        content=satisfied_state["messages"][-1].content,
        verified_candidate_content=satisfied_state["messages"][-1].content,
        verified_candidate_tool_calls=[],
    )
    assert sessions.get_active_goal_state("session-1") is None


def test_editing_running_goal_supersedes_old_run_and_next_run_uses_revision(tmp_path):
    sessions = _sessions(tmp_path)
    coordinator = HarnessRunCoordinator(sessions)
    first_run, goal = _start_rubric_goal(
        coordinator,
        session_id="session-1",
        query_id="query-old-revision",
        objective="生成 2025 年报告",
        goal_mode=True,
    )
    assert goal is not None
    assert first_run.goal_revision == 1
    coordinator.transition(first_run, RunStatus.RUNNING)
    _request_goal_completion(sessions, first_run, goal)
    original_request_id = sessions.get_run_state(
        "session-1", first_run.run_id
    )["completion_request_id"]
    goal.evidence_refs = [{
        "kind": "artifact_write",
        "artifact_id": "artifact-old-revision",
        "goal_id": goal.goal_id,
        "goal_revision": 1,
        "path": "/workspace/old-report.md",
    }]
    sessions.upsert_goal_state("session-1", goal.model_dump(mode="json"))

    revised = coordinator.goals.update_objective(
        "session-1",
        goal.goal_id,
        objective="生成 2026 年报告，并更新趋势总结",
        expected_revision=1,
    )
    assert revised.objective_revision == 2
    assert revised.pending_revision is True
    assert revised.current_run_id == first_run.run_id
    assert revised.evidence_refs == []

    _persist_satisfied_evidence(sessions, first_run, tmp_path)
    first_run, still_active, report = coordinator.complete_from_final_state(
        first_run,
        goal,
        _satisfied_final_state(tmp_path),
    )

    assert report is None
    persisted_old_run = sessions.get_run_state("session-1", first_run.run_id)
    request = sessions.get_harness_state("session-1")["completion_requests"][
        original_request_id
    ]
    assert request["status"] == "invalidated"
    assert request["invalidated_reason"] == "goal_revision_superseded"
    assert persisted_old_run["verification_report"] is None
    assert first_run.outcome == RunOutcome.COMPLETED
    assert still_active is not None
    assert still_active.status == GoalStatus.ACTIVE
    assert still_active.objective_revision == 2
    assert still_active.pending_revision is True
    assert still_active.latest_verification_report_id is None
    assert still_active.current_run_id is None

    next_run, next_goal = coordinator.start_run(
        session_id="session-1",
        query_id="query-new-revision",
        objective="继续",
        goal_mode=True,
        goal_id=goal.goal_id,
    )
    assert next_goal is not None
    assert next_goal.pending_revision is False
    assert next_run.goal_revision == 2
    assert next_run.objective == "生成 2026 年报告，并更新趋势总结"
    assert next_run.verification_contract is not None
    assert next_run.verification_contract.contract_id == next_goal.goal_contract.contract_id


def test_goal_followup_inherits_frozen_contract(tmp_path):
    sessions = _sessions(tmp_path)
    coordinator = HarnessRunCoordinator(sessions)
    first_run, goal = _start_rubric_goal(
        coordinator,
        session_id="session-1",
        query_id="query-1",
        objective="生成销量报告文件",
        goal_mode=True,
    )
    assert goal is not None
    assert first_run.verification_contract is not None
    coordinator.transition(first_run, RunStatus.RUNNING)
    _request_goal_completion(sessions, first_run, goal)
    first_run, goal, _ = coordinator.complete_from_final_state(
        first_run,
        goal,
        _exhausted_final_state(tmp_path),
    )
    assert goal is not None
    assert goal.status == GoalStatus.ACTIVE

    followup, same_goal = coordinator.start_run(
        session_id="session-1",
        query_id="query-2",
        objective="继续并完成",
        goal_mode=True,
        goal_id=goal.goal_id,
    )

    assert same_goal is not None
    assert followup.verification_contract is not None
    assert followup.verification_contract.contract_id == goal.goal_contract.contract_id


def test_goal_contract_stays_declared_while_unresolved_pack_enters_next_run(tmp_path):
    sessions = _sessions(tmp_path)
    coordinator = HarnessRunCoordinator(sessions)
    first_run, goal = _start_rubric_goal(
        coordinator,
        session_id="session-1",
        query_id="query-1",
        objective="持续完成这项任务",
        goal_mode=True,
    )
    assert goal is not None
    assert first_run.verification_contract is not None
    assert "analytics" not in first_run.verification_contract.verification_packs
    coordinator.transition(first_run, RunStatus.RUNNING)
    _request_goal_completion(sessions, first_run, goal)
    activation = build_verification_activations(
        run_id=first_run.run_id,
        query_id=first_run.query_id,
        tool_call_id="call-db",
        tool_name="database_sql_execute",
        args={"question": "查询销量"},
        result=ToolMessage(
            content="database_source_id: db-sales\nresult_id: result-1",
            tool_call_id="call-db",
            name="database_sql_execute",
            status="success",
        ),
    )[0]
    sessions.append_run_verification_activation(
        first_run.session_id,
        first_run.run_id,
        activation.model_dump(mode="json"),
    )
    effective = RunRubricCompiler.expand_for_activations(
        contract=first_run.verification_contract,
        profile=first_run.task_profile,
        message=first_run.objective,
        activations=[activation],
    )
    assert effective is not None
    state = _exhausted_final_state(tmp_path)

    first_run, goal, report = coordinator.complete_from_final_state(
        first_run,
        goal,
        state,
    )

    assert report.status == VerificationStatus.MAX_ITERATIONS_REACHED
    assert goal is not None
    assert goal.status == GoalStatus.ACTIVE
    assert goal.goal_contract is not None
    assert "analytics" not in goal.goal_contract.verification_packs

    followup, same_goal = coordinator.start_run(
        session_id="session-1",
        query_id="query-2",
        objective="继续并完成",
        goal_mode=True,
        goal_id=goal.goal_id,
    )

    assert same_goal is not None
    assert followup.verification_contract is not None
    assert "analytics" in followup.verification_contract.verification_packs
    assert "metric_consistency" in {item.id for item in followup.verification_contract.criteria}


def test_legacy_polluted_goal_contract_is_rebuilt_from_goal_objective(tmp_path):
    sessions = _sessions(tmp_path)
    polluted = RunRubricCompiler.compile(
        RubricBuildContext(
            user_message="检索网页并生成报告到 /workspace/report.md",
            force_required=True,
        )
    )
    assert polluted is not None
    assert "web_research" in polluted.verification_packs
    polluted = polluted.model_copy(
        update={"version": "run-task-profile-v2", "contract_id": "legacy-polluted"}
    )
    legacy_goal = GoalRecord(
        goal_id="goal-legacy",
        session_id="session-1",
        objective=(
            "生成报告到 /workspace/report.md，并在交付前执行 E2E 测试"
        ),
        goal_contract=polluted,
    )
    sessions.upsert_goal_state(
        "session-1",
        legacy_goal.model_dump(mode="json"),
    )

    run, migrated = HarnessRunCoordinator(sessions).start_run(
        session_id="session-1",
        query_id="query-migrated",
        objective="继续",
        goal_mode=True,
        goal_id=legacy_goal.goal_id,
    )

    assert migrated is not None and migrated.goal_contract is not None
    assert migrated.goal_contract.version == RunRubricCompiler.VERSION
    assert "artifact" in migrated.goal_contract.verification_packs
    assert "web_research" not in migrated.goal_contract.verification_packs
    assert migrated.goal_contract.browser_e2e_required is True
    assert run.declared_verification_contract == migrated.goal_contract


def test_cancelled_goal_run_preserves_successful_runtime_pack(tmp_path):
    sessions = _sessions(tmp_path)
    coordinator = HarnessRunCoordinator(sessions)
    run, goal = _start_rubric_goal(
        coordinator,
        session_id="session-1",
        query_id="query-1",
        objective="持续完成这项任务",
        goal_mode=True,
    )
    assert goal is not None
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

    coordinator.fail(
        run,
        outcome=RunOutcome.CANCELLED,
        error="client_cancelled",
    )
    goal = coordinator.goals.release_run(
        goal,
        run=run,
        gap="本 Run 已停止。",
    )

    assert goal.goal_contract is not None
    assert "analytics" not in goal.goal_contract.verification_packs
    analytics_ref = next(
        item for item in goal.evidence_refs if item.get("type") == "analytics_result"
    )
    assert set(analytics_ref) == {"type", "id"}
    resolved = sessions.resolve_evidence_ref(
        goal.session_id,
        analytics_ref,
        goal_id=goal.goal_id,
        goal_revision=goal.objective_revision,
    )
    assert resolved is not None
    assert resolved["verification_pack"] == "analytics"
    assert resolved["result_id"] == "result-1"
    assert resolved["source_run_id"] == run.run_id
    assert resolved["query_trace_id"]
    persisted = sessions.get_goal_state(goal.session_id, goal.goal_id)
    assert persisted is not None
    assert "analytics" not in persisted["goal_contract"]["verification_packs"]


def test_goal_id_is_rejected_when_goal_mode_is_off(tmp_path):
    coordinator = HarnessRunCoordinator(_sessions(tmp_path))

    with pytest.raises(GoalActivationError, match="goal_mode is disabled"):
        coordinator.start_run(
            session_id="session-1",
            query_id="query-1",
            objective="不要隐式开启目标",
            goal_mode=False,
            goal_id="goal-forged",
        )


def test_session_rejects_two_active_goals(tmp_path):
    sessions = _sessions(tmp_path)
    first = GoalRecord(
        goal_id="goal-1",
        session_id="session-1",
        objective="first",
    )
    second = GoalRecord(
        goal_id="goal-2",
        session_id="session-1",
        objective="second",
    )
    sessions.upsert_goal_state("session-1", first.model_dump(mode="json"))

    with pytest.raises(ValueError, match="already has active Goal"):
        sessions.upsert_goal_state("session-1", second.model_dump(mode="json"))


def test_session_rejects_two_concurrent_runs(tmp_path):
    sessions = _sessions(tmp_path)
    coordinator = HarnessRunCoordinator(sessions)
    first, _ = coordinator.start_run(
        session_id="session-1",
        query_id="query-1",
        objective="第一个 Run",
        goal_mode=False,
    )

    with pytest.raises(ValueError, match=f"active Run {first.run_id}"):
        coordinator.start_run(
            session_id="session-1",
            query_id="query-2",
            objective="并发 Run",
            goal_mode=False,
        )


def test_run_freezes_session_permission_policy_and_execution_backend(tmp_path):
    sessions = _sessions(tmp_path)
    policy = sessions.set_approval_mode_if_idle("session-1", "smart")
    coordinator = HarnessRunCoordinator(sessions)

    run, _ = coordinator.start_run(
        session_id="session-1",
        query_id="query-permissions",
        objective="查公开网页",
        goal_mode=False,
    )

    assert run.config_snapshot["permissions"] == policy
    assert run.config_snapshot["permissions"]["approval_mode"] == "smart"
    coordinator.bind_execution_snapshot(
        run,
        {
            "backend_mode": "docker",
            "backend_id": "docker:project:spec",
            "workspace_id": "sha256:workspace",
        },
    )
    assert run.config_snapshot["execution"]["backend_mode"] == "docker"
    persisted = sessions.get_run_state("session-1", run.run_id)
    assert persisted is not None
    assert persisted["config_snapshot"] == run.config_snapshot


def test_mode_change_and_run_start_are_linearizable(tmp_path):
    """Whichever lock wins, the Run snapshot must match the accepted policy."""

    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    sessions = _sessions(tmp_path)
    coordinator = HarnessRunCoordinator(sessions)
    barrier = Barrier(2)

    def start_run():
        barrier.wait()
        return coordinator.start_run(
            session_id="session-1",
            query_id="query-race",
            objective="race",
            goal_mode=False,
        )[0]

    def change_mode():
        barrier.wait()
        try:
            return sessions.set_approval_mode_if_idle("session-1", "smart")
        except RuntimeError:
            return None

    with ThreadPoolExecutor(max_workers=2) as executor:
        run_future = executor.submit(start_run)
        mode_future = executor.submit(change_mode)
        run = run_future.result(timeout=5)
        changed = mode_future.result(timeout=5)

    frozen = run.config_snapshot["permissions"]
    if changed is None:
        assert frozen["approval_mode"] == "strict"
    else:
        assert changed["approval_mode"] == "smart"
        assert frozen == changed


def test_approval_mode_is_idempotent_and_blocked_by_active_run(tmp_path):
    sessions = _sessions(tmp_path)
    original = sessions.get_permission_policy("session-1")
    unchanged = sessions.set_approval_mode_if_idle(
        "session-1",
        "smart",
        expected_epoch=original["policy_epoch"],
    )
    assert unchanged == original

    coordinator = HarnessRunCoordinator(sessions)
    run, _ = coordinator.start_run(
        session_id="session-1",
        query_id="query-active",
        objective="active",
        goal_mode=False,
    )
    with pytest.raises(RuntimeError, match=f"active Run {run.run_id}"):
        sessions.set_approval_mode_if_idle("session-1", "smart")


def test_execution_snapshot_is_single_assignment_and_config_is_immutable(tmp_path):
    sessions = _sessions(tmp_path)
    coordinator = HarnessRunCoordinator(sessions)
    run, _ = coordinator.start_run(
        session_id="session-1",
        query_id="query-bind",
        objective="bind",
        goal_mode=False,
    )
    execution = {
        "backend_mode": "spawn",
        "backend_id": "host:one",
        "workspace_id": "sha256:one",
    }
    coordinator.bind_execution_snapshot(run, execution)
    coordinator.bind_execution_snapshot(run, execution)
    with pytest.raises(ValueError, match="already bound"):
        sessions.bind_run_execution_snapshot(
            "session-1",
            run.run_id,
            {**execution, "backend_id": "host:forged"},
        )

    stale = run.model_copy(deep=True)
    stale.config_snapshot = {"permissions": {"approval_mode": "strict"}}
    saved = sessions.upsert_run_state(
        "session-1",
        stale.model_dump(mode="json"),
    )
    assert saved["config_snapshot"] == run.config_snapshot


def test_goal_pause_or_cancel_request_stops_attached_run_then_wins(tmp_path):
    sessions = _sessions(tmp_path)
    coordinator = HarnessRunCoordinator(sessions)
    _, goal = coordinator.start_run(
        session_id="session-1",
        query_id="query-1",
        objective="跨 Run 目标",
        goal_mode=True,
    )
    assert goal is not None

    paused = coordinator.goals.pause("session-1", goal.goal_id)
    assert paused.status == GoalStatus.ACTIVE
    assert paused.requested_status == GoalStatus.PAUSED
    assert paused.current_run_id is not None
    with pytest.raises(HarnessStateError, match="等待暂停生效"):
        coordinator.goals.resume("session-1", goal.goal_id)

    cancelled = coordinator.goals.cancel("session-1", goal.goal_id)
    assert cancelled.status == GoalStatus.ACTIVE
    assert cancelled.requested_status == GoalStatus.CANCELLED

    released = coordinator.goals.release_run(cancelled, gap="当前 Run 已停止。")
    assert released.status == GoalStatus.CANCELLED
    assert released.requested_status is None
    assert released.current_run_id is None


def test_goal_finalize_atomically_consumes_concurrent_pause_request(tmp_path):
    sessions = _sessions(tmp_path)
    coordinator = HarnessRunCoordinator(sessions)
    run, goal = coordinator.start_run(
        session_id="session-1",
        query_id="query-race",
        objective="并发控制目标",
        goal_mode=True,
    )
    assert goal is not None
    stale_completion = goal.model_copy(deep=True)
    stale_completion.current_run_id = None
    stale_completion.transition(GoalStatus.COMPLETED)

    sessions.request_goal_control("session-1", goal.goal_id, "paused")
    saved = sessions.finalize_goal_run_state(
        "session-1",
        stale_completion.model_dump(mode="json"),
        run_id=run.run_id,
    )

    assert saved["status"] == "paused"
    assert saved["requested_status"] is None
    assert saved["current_run_id"] is None


def test_accepted_completion_commits_authority_and_message_in_one_write(
    tmp_path,
    monkeypatch,
):
    sessions = _sessions(tmp_path)
    coordinator = HarnessRunCoordinator(sessions)
    run, goal = coordinator.start_run(
        session_id="session-1",
        query_id="query-accepted-commit",
        objective="完成无需 rubric 的目标",
        goal_mode=True,
        verification_enabled=False,
    )
    assert goal is not None
    coordinator.transition(run, RunStatus.RUNNING)
    _request_goal_completion(sessions, run, goal)
    completed, achieved, report = coordinator.complete_from_final_state(run, goal, {})
    assert achieved is not None and achieved.status == GoalStatus.COMPLETED
    assert report is None

    writes: list[str] = []
    original_write = sessions._write_file

    def tracked_write(session_id: str, data: dict) -> None:
        writes.append(session_id)
        original_write(session_id, data)

    monkeypatch.setattr(sessions, "_write_file", tracked_write)
    sessions.commit_accepted_completion(
        "session-1",
        run=completed.model_dump(mode="json"),
        goal=achieved.model_dump(mode="json"),
        query_id=completed.query_id,
        content="最终答案",
        verification_summary="无需额外验证。",
    )

    assert writes == ["session-1"]
    state = sessions._read_file("session-1")
    saved_run = state["harness"]["runs"][completed.run_id]
    saved_goal = state["harness"]["goals"][achieved.goal_id]
    saved_message = next(
        item
        for item in state["messages"]
        if item.get("query_id") == completed.query_id
    )
    assert saved_run["outcome"] == RunOutcome.COMPLETED.value
    assert saved_goal["status"] == GoalStatus.COMPLETED.value
    assert saved_goal["latest_goal_decision"] is None
    assert saved_goal["latest_completion_request_id"] == completed.completion_request_id
    assert saved_message["content"] == "最终答案"
    assert saved_message["verification_summary"] == "无需额外验证。"


def test_superseded_run_finalize_consumes_concurrent_cancel_request(tmp_path):
    sessions = _sessions(tmp_path)
    coordinator = HarnessRunCoordinator(sessions)
    run, goal = coordinator.start_run(
        session_id="session-1",
        query_id="query-old-revision",
        objective="旧目标",
        goal_mode=True,
    )
    assert goal is not None
    stale_old_revision = goal.model_copy(deep=True)
    stale_old_revision.current_run_id = None
    sessions.update_goal_objective(
        "session-1",
        goal.goal_id,
        objective="新目标",
        expected_revision=1,
        contract=None,
    )
    sessions.request_goal_control("session-1", goal.goal_id, "cancelled")

    saved = sessions.finalize_goal_run_state(
        "session-1",
        stale_old_revision.model_dump(mode="json"),
        run_id=run.run_id,
    )

    assert saved["objective"] == "新目标"
    assert saved["objective_revision"] == 2
    assert saved["status"] == "cancelled"
    assert saved["requested_status"] is None
    assert saved["current_run_id"] is None


def test_goal_revision_supersedes_old_exact_and_directory_drafts(tmp_path):
    sessions = _sessions(tmp_path)
    _run, goal = HarnessRunCoordinator(sessions).start_run(
        session_id="session-1",
        query_id="query-old-revision",
        objective="旧目标",
        goal_mode=True,
        verification_enabled=False,
    )
    assert goal is not None
    owner = {
        "session_id": "session-1",
        "run_id": _run.run_id,
        "query_id": _run.query_id,
        "goal_id": goal.goal_id,
        "goal_revision": goal.objective_revision,
    }
    sessions.claim_external_draft(
        "session-1",
        lease_kind="exact_file",
        lease={
            **owner,
            "lease_id": "old-file",
            "target_path": str(tmp_path / "report.html"),
            "staged_path": "/scratch/external/old-file/report.html",
            "expires_at": 9_999_999_999,
        },
    )
    sessions.claim_external_draft(
        "session-1",
        lease_kind="exact_directory",
        lease={
            **owner,
            "lease_id": "old-directory",
            "directory_path": str(tmp_path / "project"),
            "staged_dir": "/scratch/external-directories/old-directory",
            "expires_at": 9_999_999_999,
        },
    )

    revised = sessions.update_goal_objective(
        "session-1",
        goal.goal_id,
        objective="新目标",
        expected_revision=goal.objective_revision,
        contract=None,
    )

    old_file = sessions.get_external_artifact_lease("session-1", "old-file")
    old_directory = sessions.get_external_directory_lease(
        "session-1", "old-directory"
    )
    assert old_file is not None and old_file["status"] == "abandoned"
    assert old_directory is not None and old_directory["status"] == "abandoned"
    assert old_file["abandoned_reason"] == "goal_revision_superseded"
    assert old_directory["abandoned_reason"] == "goal_revision_superseded"

    new_owner = {
        **owner,
        "goal_revision": revised["objective_revision"],
    }
    new_file = sessions.claim_external_draft(
        "session-1",
        lease_kind="exact_file",
        lease={
            **new_owner,
            "lease_id": "new-file",
            "target_path": str(tmp_path / "report.html"),
            "staged_path": "/scratch/external/new-file/report.html",
            "expires_at": 9_999_999_999,
        },
    )
    new_directory = sessions.claim_external_draft(
        "session-1",
        lease_kind="exact_directory",
        lease={
            **new_owner,
            "lease_id": "new-directory",
            "directory_path": str(tmp_path / "project"),
            "staged_dir": "/scratch/external-directories/new-directory",
            "expires_at": 9_999_999_999,
        },
    )
    assert new_file["status"] == "claiming"
    assert new_directory["status"] == "claiming"


def test_terminal_goal_run_expires_search_snapshots_but_keeps_goal_draft(tmp_path):
    sessions = _sessions(tmp_path)
    run, goal = HarnessRunCoordinator(sessions).start_run(
        session_id="session-1",
        query_id="query-search",
        objective="搜索并继续编辑",
        goal_mode=True,
        verification_enabled=False,
    )
    assert goal is not None
    HarnessRunCoordinator(sessions).transition(run, RunStatus.RUNNING)
    owner = {
        "session_id": "session-1",
        "run_id": run.run_id,
        "query_id": run.query_id,
        "goal_id": goal.goal_id,
        "goal_revision": goal.objective_revision,
    }
    sessions.claim_external_draft(
        "session-1",
        lease_kind="exact_file",
        lease={
            **owner,
            "lease_id": "goal-draft",
            "target_path": str(tmp_path / "report.html"),
            "staged_path": "/scratch/external/goal-draft/report.html",
            "expires_at": 9_999_999_999,
        },
    )
    sessions.upsert_external_artifact_lease(
        "session-1",
        {
            **owner,
            "lease_id": "file-search",
            "target_path": str(tmp_path / "source.txt"),
            "staged_path": "/scratch/external-search-files/file-search/source.txt",
            "status": "search_snapshot",
            "search_only": True,
        },
    )
    sessions.upsert_external_directory_lease(
        "session-1",
        {
            **owner,
            "lease_id": "directory-search",
            "directory_path": str(tmp_path / "source"),
            "staged_dir": "/scratch/external-directories/directory-search",
            "status": "search_snapshot",
            "search_only": True,
        },
    )

    incoming = run.model_copy(deep=True)
    incoming.finish(RunOutcome.COMPLETED)
    sessions.terminalize_run_state(
        "session-1",
        run.run_id,
        incoming.model_dump(mode="json"),
    )

    draft = sessions.get_external_artifact_lease("session-1", "goal-draft")
    file_search = sessions.get_external_artifact_lease("session-1", "file-search")
    directory_search = sessions.get_external_directory_lease(
        "session-1", "directory-search"
    )
    assert draft is not None and draft["status"] == "claiming"
    assert file_search is not None and file_search["status"] == "abandoned"
    assert directory_search is not None and directory_search["status"] == "abandoned"
    assert file_search["abandoned_reason"] == "run_search_snapshot_terminal"
    assert directory_search["abandoned_reason"] == "run_search_snapshot_terminal"
    assert sessions.resolve_terminal_scratch_reference(
        "session-1", file_search["staged_path"]
    )["status"] == "artifact_not_durable"
    assert sessions.resolve_terminal_scratch_reference(
        "session-1", f"{directory_search['staged_dir']}/nested.txt"
    )["status"] == "artifact_not_durable"
    assert sessions.resolve_terminal_scratch_reference(
        "session-1", directory_search["staged_dir"]
    )["status"] == "artifact_not_durable"


def test_goal_max_rounds_is_enforced_before_persistence(tmp_path):
    sessions = _sessions(tmp_path)
    coordinator = HarnessRunCoordinator(sessions)
    first_run, goal = _start_rubric_goal(
        coordinator,
        session_id="session-1",
        query_id="query-1",
        objective="销量分析最多一轮",
        goal_mode=True,
        goal_max_rounds=1,
    )
    assert goal is not None
    coordinator.transition(first_run, RunStatus.RUNNING)
    _request_goal_completion(sessions, first_run, goal)
    first_run, goal, _ = coordinator.complete_from_final_state(
        first_run,
        goal,
        _exhausted_final_state(tmp_path),
    )
    assert goal is not None
    assert goal.status == GoalStatus.BUDGET_EXCEEDED

    with pytest.raises(GoalActivationError, match="budget_exceeded"):
        coordinator.start_run(
            session_id="session-1",
            query_id="query-2",
            objective="不得创建第二轮",
            goal_mode=True,
            goal_id=goal.goal_id,
        )

    harness = sessions.get_harness_state("session-1")
    assert harness["run_order"] == [first_run.run_id]


def test_budget_exhausted_goal_can_be_explicitly_cancelled(tmp_path):
    sessions = _sessions(tmp_path)
    coordinator = HarnessRunCoordinator(sessions)
    first_run, goal = _start_rubric_goal(
        coordinator,
        session_id="session-1",
        query_id="query-close-budget-goal",
        objective="已完成的 V2 报告",
        goal_mode=True,
        goal_max_rounds=1,
    )
    assert goal is not None
    coordinator.transition(first_run, RunStatus.RUNNING)
    _request_goal_completion(sessions, first_run, goal)
    _, goal, _ = coordinator.complete_from_final_state(
        first_run,
        goal,
        _exhausted_final_state(tmp_path),
    )
    assert goal is not None and goal.status == GoalStatus.BUDGET_EXCEEDED

    cancelled = coordinator.goals.cancel("session-1", goal.goal_id)

    assert cancelled.status == GoalStatus.CANCELLED
    assert cancelled.current_run_id is None
    assert cancelled.completed_at is not None
    assert sessions.get_goal_state("session-1", goal.goal_id)["status"] == "cancelled"


def test_explicit_budget_extension_allows_a_new_goal_run(tmp_path):
    sessions = _sessions(tmp_path)
    coordinator = HarnessRunCoordinator(sessions)
    first_run, goal = _start_rubric_goal(
        coordinator,
        session_id="session-1",
        query_id="query-1",
        objective="销量分析最多一轮",
        goal_mode=True,
        goal_max_rounds=1,
    )
    assert goal is not None
    coordinator.transition(first_run, RunStatus.RUNNING)
    _request_goal_completion(sessions, first_run, goal)
    _, exhausted, _ = coordinator.complete_from_final_state(
        first_run,
        goal,
        _exhausted_final_state(tmp_path),
    )
    assert exhausted is not None
    assert exhausted.status == GoalStatus.BUDGET_EXCEEDED

    reopened = coordinator.goals.extend_budget(
        "session-1",
        exhausted.goal_id,
        additional_rounds=2,
    )
    assert reopened.status == GoalStatus.PAUSED
    assert reopened.round == 1
    assert reopened.max_rounds == 3
    resumed = coordinator.goals.resume("session-1", reopened.goal_id)
    assert resumed.status == GoalStatus.ACTIVE

    second_run, attached = coordinator.start_run(
        session_id="session-1",
        query_id="query-2",
        objective="继续销量分析",
        goal_mode=True,
        goal_id=reopened.goal_id,
    )
    assert attached is not None
    assert attached.round == 2
    assert attached.max_rounds == 3
    assert second_run.goal_id == reopened.goal_id


def test_first_terminal_run_outcome_is_authoritative(tmp_path):
    sessions = _sessions(tmp_path)
    run = RunRecord(
        run_id="run-1",
        query_id="query-1",
        session_id="session-1",
        objective="task",
    )
    sessions.upsert_run_state("session-1", run.model_dump(mode="json"))
    run.transition(RunStatus.RUNNING)
    sessions.upsert_run_state("session-1", run.model_dump(mode="json"))
    run.finish(RunOutcome.CANCELLED)
    sessions.upsert_run_state("session-1", run.model_dump(mode="json"))

    forged = run.model_copy(deep=True)
    forged.status = RunStatus.COMPLETED
    forged.outcome = RunOutcome.COMPLETED
    with pytest.raises(ValueError, match="already has terminal outcome"):
        sessions.upsert_run_state(
            "session-1",
            forged.model_dump(mode="json"),
        )


def test_terminal_run_contract_is_immutable_and_new_run_must_prepare(tmp_path):
    sessions = _sessions(tmp_path)
    invalid = RunRecord(
        run_id="run-invalid",
        query_id="query-invalid",
        session_id="session-1",
        objective="task",
        status=RunStatus.RUNNING,
    )
    with pytest.raises(ValueError, match="must start in preparing"):
        sessions.start_harness_run(
            "session-1",
            invalid.model_dump(mode="json"),
        )

    coordinator = HarnessRunCoordinator(sessions)
    run, _ = coordinator.start_run(
        session_id="session-1",
        query_id="query-1",
        objective="task",
        goal_mode=False,
    )
    coordinator.bind_execution_snapshot(
        run,
        {
            "backend_mode": "spawn",
            "backend_id": "spawn:test",
            "workspace_id": "workspace:test",
        },
    )
    coordinator.transition(run, RunStatus.RUNNING)
    run.finish(RunOutcome.CANCELLED)
    sessions.terminalize_run_state(
        "session-1",
        run.run_id,
        run.model_dump(mode="json"),
    )

    with pytest.raises(ValueError, match="Terminal Run"):
        sessions.update_run_verification_contract(
            "session-1",
            run.run_id,
            {"contract_id": "late"},
        )


def test_run_waiting_hitl_transition_round_trip(tmp_path):
    sessions = _sessions(tmp_path)
    coordinator = HarnessRunCoordinator(sessions)
    run, _ = coordinator.start_run(
        session_id="session-1",
        query_id="query-1",
        objective="task",
        goal_mode=False,
    )
    coordinator.bind_execution_snapshot(
        run,
        {
            "backend_mode": "spawn",
            "backend_id": "spawn:test",
            "workspace_id": "workspace:test",
        },
    )
    coordinator.transition(run, RunStatus.RUNNING)

    waiting = sessions.transition_run_status(
        "session-1",
        run.run_id,
        "waiting_hitl",
        expected_statuses={"running"},
    )
    resumed = sessions.transition_run_status(
        "session-1",
        run.run_id,
        "running",
        expected_statuses={"waiting_hitl"},
    )

    assert waiting["status"] == "waiting_hitl"
    assert resumed["status"] == "running"


def test_terminal_goal_cannot_be_rewritten_through_persistence(tmp_path):
    sessions = _sessions(tmp_path)
    goal = GoalRecord(
        goal_id="goal-1",
        session_id="session-1",
        objective="task",
    )
    goal.transition(GoalStatus.COMPLETED)
    sessions.upsert_goal_state("session-1", goal.model_dump(mode="json"))

    forged = goal.model_copy(deep=True)
    forged.objective = "forged"
    with pytest.raises(ValueError, match="already terminal"):
        sessions.upsert_goal_state(
            "session-1",
            forged.model_dump(mode="json"),
        )


def test_model_rejects_illegal_transition_after_terminal():
    run = RunRecord(
        run_id="run-1",
        query_id="query-1",
        session_id="session-1",
        objective="task",
        status=RunStatus.RUNNING,
    )
    run.finish(RunOutcome.CANCELLED)

    with pytest.raises(HarnessStateError, match="already terminal"):
        run.transition(RunStatus.RUNNING)


def test_clear_session_removes_invisible_goal_state(tmp_path):
    sessions = _sessions(tmp_path)
    goal = GoalRecord(
        goal_id="goal-1",
        session_id="session-1",
        objective="task",
    )
    sessions.upsert_goal_state("session-1", goal.model_dump(mode="json"))

    sessions.clear_messages("session-1")

    assert sessions.get_active_goal_state("session-1") is None
    assert "harness" not in sessions.get_raw_messages("session-1")
