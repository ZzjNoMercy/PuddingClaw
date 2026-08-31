"""Regression coverage for the explicit Goal completion protocol."""

from __future__ import annotations

from graph.middlewares.goal_completion import GoalCompletionMiddleware
from graph.session_manager import SessionManager
from harness.coordinators import HarnessRunCoordinator
from harness.models import (
    GoalCompletionPolicy,
    GoalCompletionRequestStatus,
    GoalStatus,
    RubricEvaluationReport,
    RunOutcome,
    RunStatus,
)


def _sessions(tmp_path) -> SessionManager:
    sessions = SessionManager()
    sessions.initialize(tmp_path)
    sessions.create_session("session-1", metadata={"runtime_mode": "agent"})
    return sessions


def test_standard_goal_requires_request_then_commits_atomically(tmp_path) -> None:
    sessions = _sessions(tmp_path)
    coordinator = HarnessRunCoordinator(sessions)
    run, goal = coordinator.start_run(
        session_id="session-1",
        query_id="query-1",
        objective="完成交付",
        goal_mode=True,
        completion_policy=GoalCompletionPolicy.STANDARD,
    )
    assert goal is not None
    coordinator.transition(run, RunStatus.RUNNING)

    request = sessions.record_goal_completion_request(
        "session-1",
        goal_id=goal.goal_id,
        objective_revision=goal.objective_revision,
        run_id=run.run_id,
        tool_call_id="call-complete",
        message="已执行测试",
    )
    duplicate = sessions.record_goal_completion_request(
        "session-1",
        goal_id=goal.goal_id,
        objective_revision=goal.objective_revision,
        run_id=run.run_id,
        tool_call_id="call-complete",
    )
    assert duplicate["request_id"] == request["request_id"]

    # Keep the graph's original in-memory objects. The completion Tool writes
    # authority directly to Session state, so coordinator finalization must
    # refresh it instead of relying on a caller-side reload.
    run.model_call_count = 7
    completed_run, completed_goal, report = coordinator.complete_from_final_state(run, goal, {})
    assert report is None
    assert completed_goal is not None and completed_goal.status == GoalStatus.COMPLETED
    assert completed_run.completion_request_id == request["request_id"]
    assert completed_goal.model_call_count == 7
    # Before final text exists, the persistent authority is still live.
    assert sessions.get_goal_state("session-1", goal.goal_id)["status"] == "active"

    sessions.commit_accepted_completion(
        "session-1",
        run=completed_run.model_dump(mode="json"),
        goal=completed_goal.model_dump(mode="json"),
        query_id="query-1",
        content="交付完成。",
    )
    saved = sessions.get_harness_state("session-1")
    assert saved["goals"][goal.goal_id]["status"] == "completed"
    assert saved["runs"][run.run_id]["status"] == "completed"
    assert saved["completion_requests"][request["request_id"]]["status"] == "accepted"
    assert sessions.load_session("session-1")[-1]["content"] == "交付完成。"


def test_natural_stop_keeps_standard_goal_active(tmp_path) -> None:
    sessions = _sessions(tmp_path)
    coordinator = HarnessRunCoordinator(sessions)
    run, goal = coordinator.start_run(
        session_id="session-1",
        query_id="query-1",
        objective="完成交付",
        goal_mode=True,
        completion_policy=GoalCompletionPolicy.STANDARD,
    )
    assert goal is not None
    coordinator.transition(run, RunStatus.RUNNING)
    run.model_call_count = 3
    completed_run, active_goal, report = coordinator.complete_from_final_state(run, goal, {})
    assert completed_run.status == RunStatus.COMPLETED
    assert active_goal is not None and active_goal.status == GoalStatus.ACTIVE
    assert active_goal.model_call_count == 3
    assert report is None


def test_standard_goal_natural_stop_gets_one_structured_completion_reminder(tmp_path) -> None:
    sessions = _sessions(tmp_path)
    coordinator = HarnessRunCoordinator(sessions)
    run, goal = coordinator.start_run(
        session_id="session-1",
        query_id="query-1",
        objective="完成交付",
        goal_mode=True,
        completion_policy=GoalCompletionPolicy.STANDARD,
    )
    assert goal is not None
    coordinator.transition(run, RunStatus.RUNNING)
    update = GoalCompletionMiddleware._completion_reminder_update(
        {},
        persisted_run=sessions.get_run_state("session-1", run.run_id),
        persisted_goal=sessions.get_goal_state("session-1", goal.goal_id),
    )

    assert update is not None and update["jump_to"] == "model"
    assert update["_goal_completion_reminder_count"] == 1
    assert "update_goal(completed=true)" in update["messages"][0].content

    stale_run = sessions.get_run_state("session-1", run.run_id)
    assert stale_run is not None
    stale_run["status"] = "completed"
    assert (
        GoalCompletionMiddleware._completion_reminder_update(
            {},
            persisted_run=stale_run,
            persisted_goal=sessions.get_goal_state("session-1", goal.goal_id),
        )
        is None
    )

    superseded_goal = sessions.get_goal_state("session-1", goal.goal_id)
    assert superseded_goal is not None
    superseded_goal["current_run_id"] = "run-newer"
    assert (
        GoalCompletionMiddleware._completion_reminder_update(
            {},
            persisted_run=sessions.get_run_state("session-1", run.run_id),
            persisted_goal=superseded_goal,
        )
        is None
    )

    assert (
        GoalCompletionMiddleware._completion_reminder_update(
            {"_goal_completion_reminder_count": 1},
            persisted_run=sessions.get_run_state("session-1", run.run_id),
            persisted_goal=sessions.get_goal_state("session-1", goal.goal_id),
        )
        is None
    )

    request = sessions.record_goal_completion_request(
        "session-1",
        goal_id=goal.goal_id,
        objective_revision=goal.objective_revision,
        run_id=run.run_id,
        tool_call_id="call-complete",
    )
    assert (
        GoalCompletionMiddleware._completion_reminder_update(
            {},
            persisted_run=sessions.get_run_state("session-1", run.run_id),
            persisted_goal=sessions.get_goal_state("session-1", goal.goal_id),
            completion_request=request,
        )
        is None
    )


def test_post_request_work_invalidates_request(tmp_path) -> None:
    sessions = _sessions(tmp_path)
    coordinator = HarnessRunCoordinator(sessions)
    run, goal = coordinator.start_run(
        session_id="session-1",
        query_id="query-1",
        objective="完成交付",
        goal_mode=True,
    )
    assert goal is not None
    coordinator.transition(run, RunStatus.RUNNING)
    request = sessions.record_goal_completion_request(
        "session-1", goal_id=goal.goal_id, objective_revision=1, run_id=run.run_id, tool_call_id="call-1"
    )
    invalidated = sessions.invalidate_goal_completion_request(
        "session-1", run_id=run.run_id, reason="post_completion_tool:write_file"
    )
    assert invalidated is not None
    assert invalidated["status"] == GoalCompletionRequestStatus.INVALIDATED.value
    assert invalidated["request_id"] == request["request_id"]


def test_rubric_acceptance_remains_atomic_until_candidate_message(tmp_path) -> None:
    sessions = _sessions(tmp_path)
    coordinator = HarnessRunCoordinator(sessions)
    run, goal = coordinator.start_run(
        session_id="session-1",
        query_id="query-1",
        objective="关键交付",
        goal_mode=True,
        completion_policy=GoalCompletionPolicy.RUBRIC,
    )
    assert goal is not None
    coordinator.transition(run, RunStatus.RUNNING)
    request = sessions.record_goal_completion_request(
        "session-1", goal_id=goal.goal_id, objective_revision=1, run_id=run.run_id, tool_call_id="call-1"
    )
    coordinator.transition(run, RunStatus.EVALUATING)
    run = type(run).model_validate(sessions.get_run_state("session-1", run.run_id))
    report = RubricEvaluationReport(
        report_id="report-1",
        run_id=run.run_id,
        status="satisfied",
        accepted_for_goal_revision=True,
        goal_revision=1,
    )
    run.verification_report = report
    run.finish(RunOutcome.COMPLETED)
    goal.transition(GoalStatus.COMPLETED)

    sessions.commit_accepted_completion(
        "session-1",
        run=run.model_dump(mode="json"),
        goal=goal.model_dump(mode="json"),
        query_id="query-1",
        content="已通过复核。",
        usage_summary={
            "run_id": run.run_id,
            "query_id": run.query_id,
            "rounds": 7,
        },
    )
    saved = sessions.get_harness_state("session-1")
    assert saved["goals"][goal.goal_id]["status"] == "completed"
    assert saved["runs"][run.run_id]["model_call_count"] == 7
    assert saved["goals"][goal.goal_id]["model_call_count"] == 7
    assert saved["completion_requests"][request["request_id"]]["status"] == "accepted"


def test_legacy_rubric_session_recovers_run_and_goal_model_calls() -> None:
    data = {
        "messages": [
            {
                "role": "assistant",
                "query_id": "query-1",
                "usage_summary": {
                    "run_id": "run-1",
                    "query_id": "query-1",
                    "rounds": 20,
                    "observed_calls": 11,
                },
            }
        ],
        "harness": {
            "runs": {
                "run-1": {
                    "run_id": "run-1",
                    "query_id": "query-1",
                    "status": "completed",
                    "model_call_count": 0,
                }
            },
            "goals": {
                "goal-1": {
                    "goal_id": "goal-1",
                    "status": "completed",
                    "run_ids": ["run-1"],
                    "model_call_count": 0,
                }
            },
        },
    }

    assert SessionManager._repair_legacy_model_call_counts(data) is True
    assert data["harness"]["runs"]["run-1"]["model_call_count"] == 11
    assert data["harness"]["goals"]["goal-1"]["model_call_count"] == 11
