"""Contract tests for Run verification and explicit cross-Run Goal mode."""

from __future__ import annotations

from pathlib import Path

import pytest
from langchain_core.messages import ToolMessage

from graph.session_manager import SessionManager
from harness.coordinators import GoalActivationError, HarnessRunCoordinator
from harness.models import (
    GoalRecord,
    GoalStatus,
    HarnessStateError,
    RunOutcome,
    RunRecord,
    RunStatus,
    VerificationStatus,
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


def test_explicit_goal_always_freezes_a_verification_contract(tmp_path):
    sessions = _sessions(tmp_path)
    coordinator = HarnessRunCoordinator(sessions)

    run, goal = coordinator.start_run(
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

    run, goal, report = coordinator.complete_from_final_state(run, goal, {})

    assert report.status == VerificationStatus.NOT_REQUIRED
    assert report.accepted_for_goal_revision is True
    assert goal is not None and goal.status == GoalStatus.ACHIEVED
    assert goal.latest_goal_decision is not None
    assert goal.latest_goal_decision.accepted is True
    assert goal.latest_goal_decision.accepted_run_id == run.run_id


def test_deterministic_revision_state_is_not_reported_as_grader_error(tmp_path):
    sessions = _sessions(tmp_path)
    coordinator = HarnessRunCoordinator(sessions)
    run, goal = coordinator.start_run(
        session_id="session-1",
        query_id="query-deterministic-revision",
        objective="生成销量报告并提供文件",
        goal_mode=False,
    )
    coordinator.transition(run, RunStatus.RUNNING)

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
    _persist_satisfied_evidence(sessions, run, tmp_path)
    completed, completed_goal, report = coordinator.complete_from_final_state(
        run,
        goal,
        _satisfied_final_state(tmp_path),
    )

    assert completed_goal is None
    assert report.status == VerificationStatus.SATISFIED, report.model_dump(mode="json")
    assert completed.status == RunStatus.COMPLETED
    assert completed.outcome == RunOutcome.COMPLETED
    harness = sessions.get_harness_state("session-1")
    assert len(harness["runs"]) == 1
    assert harness["goals"] == {}


def test_non_goal_verification_failure_does_not_create_followup_run(tmp_path):
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

    assert completed.outcome == RunOutcome.VERIFICATION_FAILED
    assert report.status == VerificationStatus.MAX_ITERATIONS_REACHED
    assert report.gaps
    harness = sessions.get_harness_state("session-1")
    assert len(harness["runs"]) == 1
    assert harness["goals"] == {}


def test_deterministic_todo_gate_overrides_satisfied_grader(tmp_path):
    sessions = _sessions(tmp_path)
    coordinator = HarnessRunCoordinator(sessions)
    run, goal = coordinator.start_run(
        session_id="session-1",
        query_id="query-1",
        objective="分析 6 月销量并给出结论",
        goal_mode=False,
    )
    coordinator.transition(run, RunStatus.RUNNING)
    state = _satisfied_final_state(tmp_path)
    state["_harness_context"]["todos"] = [{"id": "todo-1", "content": "补齐数据来源", "status": "in_progress"}]

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
    run, goal = coordinator.start_run(
        session_id="session-1",
        query_id="query-1",
        objective="生成 6 月销量报告",
        goal_mode=False,
    )
    coordinator.transition(run, RunStatus.RUNNING)
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
    run, goal = coordinator.start_run(
        session_id="session-1",
        query_id="query-external-target",
        objective=f"{external} 刷新这个报告到 2026 年",
        goal_mode=False,
    )
    coordinator.transition(run, RunStatus.RUNNING)
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

    first, goal = coordinator.start_run(
        session_id="session-external-goal",
        query_id="query-1",
        objective=f"更新外部报告 {external}",
        goal_mode=True,
    )
    assert goal is not None
    coordinator.transition(first, RunStatus.RUNNING)
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
            args={"command": "pytest -q"},
            result=validation_result,
        )
        if item.pack == "code"
    )
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
        "_harness_context": {"workspace_path": str(workspace), "todos": []},
    }
    second, goal, report = coordinator.complete_from_final_state(second, goal, satisfied_state)

    artifact = next(item for item in report.evaluations if item.criterion_id == "artifact_delivery")
    assert artifact.passed is True
    assert artifact.evidence[0]["current_run_count"] == 0
    assert artifact.evidence[0]["inherited_count"] == 1
    assert report.status == VerificationStatus.SATISFIED, report.model_dump(mode="json")
    assert second.outcome == RunOutcome.COMPLETED
    assert goal is not None and goal.status == GoalStatus.ACHIEVED


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


def test_incomplete_verification_does_not_consume_goal_business_round(tmp_path):
    sessions = _sessions(tmp_path)
    coordinator = HarnessRunCoordinator(sessions)
    run, goal = coordinator.start_run(
        session_id="session-1",
        query_id="query-incomplete",
        objective="持续生成销量报告",
        goal_mode=True,
        goal_max_rounds=1,
    )
    assert goal is not None and goal.round == 1
    coordinator.transition(run, RunStatus.RUNNING)

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
    run, goal = coordinator.start_run(
        session_id="session-1",
        query_id="query-grader-error",
        objective="持续生成销量报告",
        goal_mode=True,
        goal_max_rounds=1,
    )
    assert goal is not None and goal.round == 1
    coordinator.transition(run, RunStatus.RUNNING)

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
        run, goal = coordinator.start_run(
            session_id="session-1",
            query_id=f"query-{attempt}",
            objective="持续生成销量报告",
            goal_mode=True,
            goal_id=goal.goal_id if goal is not None else None,
            goal_max_rounds=1,
        )
        assert goal is not None
        coordinator.transition(run, RunStatus.RUNNING)
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
    first_run, goal = coordinator.start_run(
        session_id="session-1",
        query_id="query-1",
        objective="持续完成销量下降根因分析，直到证据完整",
        goal_mode=True,
        analytics_model_id="汽车行业综合分析",
    )
    assert goal is not None
    assert goal.status == GoalStatus.ACTIVE
    coordinator.transition(first_run, RunStatus.RUNNING)

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
    _persist_satisfied_evidence(sessions, second_run, tmp_path)
    second_run, achieved_goal, second_report = coordinator.complete_from_final_state(
        second_run,
        resumed_goal,
        _satisfied_final_state(tmp_path),
    )

    assert second_report.status == VerificationStatus.SATISFIED
    assert second_run.outcome == RunOutcome.COMPLETED
    assert achieved_goal is not None
    assert achieved_goal.status == GoalStatus.ACHIEVED
    assert achieved_goal.run_ids == [first_run.run_id, second_run.run_id]
    assert achieved_goal.latest_goal_decision is not None
    assert achieved_goal.latest_goal_decision.accepted is True
    assert achieved_goal.latest_goal_decision.criterion_provenance
    assert second_run.run_id in achieved_goal.latest_goal_decision.supporting_run_ids
    assert sessions.get_active_goal_state("session-1") is None


def test_editing_running_goal_supersedes_old_run_and_next_run_uses_revision(tmp_path):
    sessions = _sessions(tmp_path)
    coordinator = HarnessRunCoordinator(sessions)
    first_run, goal = coordinator.start_run(
        session_id="session-1",
        query_id="query-old-revision",
        objective="生成 2025 年报告",
        goal_mode=True,
    )
    assert goal is not None
    assert first_run.goal_revision == 1
    coordinator.transition(first_run, RunStatus.RUNNING)
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

    assert report.status == VerificationStatus.SATISFIED
    assert report.accepted_for_goal_revision is False
    persisted_old_run = sessions.get_run_state("session-1", first_run.run_id)
    assert persisted_old_run["verification_report"]["accepted_for_goal_revision"] is False
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
    first_run, goal = coordinator.start_run(
        session_id="session-1",
        query_id="query-1",
        objective="生成销量报告文件",
        goal_mode=True,
    )
    assert goal is not None
    assert first_run.verification_contract is not None
    coordinator.transition(first_run, RunStatus.RUNNING)
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


def test_goal_contract_monotonically_inherits_runtime_analytics_pack(tmp_path):
    sessions = _sessions(tmp_path)
    coordinator = HarnessRunCoordinator(sessions)
    first_run, goal = coordinator.start_run(
        session_id="session-1",
        query_id="query-1",
        objective="持续完成这项任务",
        goal_mode=True,
    )
    assert goal is not None
    assert first_run.verification_contract is not None
    assert "analytics" not in first_run.verification_contract.verification_packs
    coordinator.transition(first_run, RunStatus.RUNNING)
    activation = build_verification_activations(
        run_id=first_run.run_id,
        query_id=first_run.query_id,
        tool_call_id="call-db",
        tool_name="database_sql_execute",
        args={"question": "查询销量"},
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
    assert "analytics" in goal.goal_contract.verification_packs

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


def test_cancelled_goal_run_preserves_successful_runtime_pack(tmp_path):
    sessions = _sessions(tmp_path)
    coordinator = HarnessRunCoordinator(sessions)
    run, goal = coordinator.start_run(
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
    assert "analytics" in goal.goal_contract.verification_packs
    persisted = sessions.get_goal_state(goal.session_id, goal.goal_id)
    assert persisted is not None
    assert "analytics" in persisted["goal_contract"]["verification_packs"]


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
        "strict",
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
        "backend_mode": "restricted_host",
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
    stale_completion.transition(GoalStatus.ACHIEVED)

    sessions.request_goal_control("session-1", goal.goal_id, "paused")
    saved = sessions.finalize_goal_run_state(
        "session-1",
        stale_completion.model_dump(mode="json"),
        run_id=run.run_id,
    )

    assert saved["status"] == "paused"
    assert saved["requested_status"] is None
    assert saved["current_run_id"] is None


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


def test_goal_max_rounds_is_enforced_before_persistence(tmp_path):
    sessions = _sessions(tmp_path)
    coordinator = HarnessRunCoordinator(sessions)
    first_run, goal = coordinator.start_run(
        session_id="session-1",
        query_id="query-1",
        objective="销量分析最多一轮",
        goal_mode=True,
        goal_max_rounds=1,
    )
    assert goal is not None
    coordinator.transition(first_run, RunStatus.RUNNING)
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
            "backend_mode": "restricted_host",
            "backend_id": "restricted-host:test",
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
            "backend_mode": "restricted_host",
            "backend_id": "restricted-host:test",
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
    goal.transition(GoalStatus.ACHIEVED)
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
