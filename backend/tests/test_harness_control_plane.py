"""Contract tests for Run verification and explicit cross-Run Goal mode."""

from __future__ import annotations

from pathlib import Path

import pytest

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
    return {
        "_harness_context": _verification_context(workspace),
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
                    {"name": "evidence_traceability", "passed": True},
                    {"name": "time_scope", "passed": True},
                    {"name": "artifact_delivery", "passed": True},
                    {"name": "report_integrity", "passed": True},
                ],
            }
        ],
    }


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
                        "name": "evidence_traceability",
                        "passed": False,
                        "gap": "关键销量数据缺少来源。",
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
        "evidence_traceability",
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
    criterion = next(
        item for item in contract.criteria if item.id == "quantified_impact"
    )
    assert criterion.source.value == "settings"
    assert criterion.verifier.value == "analytics"


def test_plain_chat_does_not_pay_rubric_tax():
    assert (
        RunRubricCompiler.compile(
            RubricBuildContext(user_message="你好，介绍一下你自己")
        )
        is None
    )


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
    } <= {
        item.id for item in run.verification_contract.criteria
    }


def test_create_workspace_artifact_compiles_artifact_delivery():
    contract = RunRubricCompiler.compile(
        RubricBuildContext(
            user_message="创建 /workspace/result.md 并引用该路径"
        )
    )

    assert contract is not None
    assert contract.task_type == "artifact"
    assert "artifact_delivery" in {
        item.id for item in contract.criteria
    }


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
    assert report.status == VerificationStatus.SATISFIED
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
    assert report.gaps == ["关键销量数据缺少来源。"]
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
    state["_harness_context"]["todos"] = [
        {"id": "todo-1", "content": "补齐数据来源", "status": "in_progress"}
    ]

    completed, _, report = coordinator.complete_from_final_state(run, goal, state)

    assert completed.outcome == RunOutcome.VERIFICATION_FAILED
    assert report.status == VerificationStatus.NEEDS_REVISION
    todo_evaluation = next(
        item
        for item in report.evaluations
        if item.criterion_id == "todo_reconciliation"
    )
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
    state = _satisfied_final_state(tmp_path)
    state["_harness_context"]["final_content"] = "已生成：`/workspace/missing.md`"

    completed, _, report = coordinator.complete_from_final_state(run, goal, state)

    assert completed.outcome == RunOutcome.VERIFICATION_FAILED
    artifact_evaluation = next(
        item for item in report.evaluations if item.criterion_id == "artifact_delivery"
    )
    assert artifact_evaluation.passed is False
    assert "missing.md" in str(artifact_evaluation.gap)


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
    assert sessions.get_active_goal_state("session-1") is None


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
    assert (
        followup.verification_contract.contract_id
        == goal.goal_contract.contract_id
    )


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


def test_goal_cannot_pause_or_cancel_while_run_is_attached(tmp_path):
    sessions = _sessions(tmp_path)
    coordinator = HarnessRunCoordinator(sessions)
    _, goal = coordinator.start_run(
        session_id="session-1",
        query_id="query-1",
        objective="跨 Run 目标",
        goal_mode=True,
    )
    assert goal is not None

    with pytest.raises(HarnessStateError, match="stop the Run before pausing"):
        coordinator.goals.pause("session-1", goal.goal_id)
    with pytest.raises(HarnessStateError, match="stop the Run before cancelling"):
        coordinator.goals.cancel("session-1", goal.goal_id)


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
        status=RunStatus.RUNNING,
    )
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
