"""Coordinators for Run lifecycle, Run verification, and optional Goals."""

from __future__ import annotations

import time
import uuid
from typing import Any

from graph.session_manager import SessionManager
from harness.deterministic_checks import evaluate_deterministic_criteria
from harness.models import (
    CriterionEvaluation,
    GoalRecord,
    GoalStatus,
    HarnessStateError,
    RubricEvaluationReport,
    RunOutcome,
    RunRecord,
    RunStatus,
    RunVerificationContract,
    VerificationStatus,
    VerifierKind,
)
from harness.rubric_compiler import RubricBuildContext, RunRubricCompiler


class GoalActivationError(HarnessStateError):
    """Raised when Goal identifiers and the explicit Goal Mode disagree."""


class GoalCoordinator:
    """Own Goal creation and cross-Run advancement.

    This coordinator is never invoked for the default non-Goal request path.
    """

    def __init__(self, sessions: SessionManager) -> None:
        self._sessions = sessions

    def resolve_for_run(
        self,
        *,
        session_id: str,
        objective: str,
        goal_mode: bool,
        goal_id: str | None,
        goal_contract: RunVerificationContract | None,
        max_rounds: int = 8,
    ) -> GoalRecord | None:
        if not goal_mode:
            if goal_id:
                raise GoalActivationError(
                    "goal_id was supplied while goal_mode is disabled."
                )
            return None

        if goal_id:
            goal = self._load_goal(session_id, goal_id)
            if goal.status != GoalStatus.ACTIVE:
                raise GoalActivationError(
                    f"Goal {goal_id} is {goal.status}; resume it before starting a Run."
                )
            return goal

        active = self._sessions.get_active_goal_state(session_id)
        if active:
            goal = GoalRecord.model_validate(active)
            if goal.status == GoalStatus.ACTIVE:
                return goal

        goal = GoalRecord(
            goal_id=f"goal-{uuid.uuid4().hex[:16]}",
            session_id=session_id,
            objective=objective.strip(),
            goal_contract=goal_contract,
            max_rounds=max_rounds,
        )
        return goal

    def attach_run(self, goal: GoalRecord, run_id: str) -> GoalRecord:
        goal.attach_run(run_id)
        self._sessions.upsert_goal_state(
            goal.session_id,
            goal.model_dump(mode="json"),
        )
        return goal

    def apply_run_report(
        self,
        goal: GoalRecord,
        run: RunRecord,
        report: RubricEvaluationReport,
        outcome: RunOutcome,
    ) -> GoalRecord:
        goal.latest_verification_report_id = report.report_id
        goal.gaps = list(report.gaps)
        goal.current_run_id = None
        goal.model_call_count += max(0, run.model_call_count)
        if (
            outcome == RunOutcome.COMPLETED
            and report.status == VerificationStatus.SATISFIED
        ):
            goal.transition(GoalStatus.ACHIEVED)
        elif (
            outcome == RunOutcome.COMPLETED
            and report.status == VerificationStatus.NOT_REQUIRED
            and goal.goal_contract is None
        ):
            goal.transition(GoalStatus.ACHIEVED)
        elif (
            outcome == RunOutcome.BUDGET_EXCEEDED
            and run.budget_exhaustion_reason
            and run.budget_exhaustion_reason.startswith("goal_")
        ):
            goal.budget_exhaustion_reason = run.budget_exhaustion_reason
            goal.transition(GoalStatus.BUDGET_EXCEEDED)
        elif goal.round >= goal.max_rounds:
            goal.budget_exhaustion_reason = "goal_max_runs"
            goal.transition(GoalStatus.BUDGET_EXCEEDED)
        else:
            goal.updated_at = time.time()
        self._sessions.upsert_goal_state(
            goal.session_id,
            goal.model_dump(mode="json"),
        )
        return goal

    def release_run(
        self,
        goal: GoalRecord,
        *,
        gap: str | None = None,
    ) -> GoalRecord:
        """Detach a cancelled/failed Run without cancelling the Goal."""

        goal.current_run_id = None
        if gap:
            goal.gaps = [gap]
        goal.updated_at = time.time()
        self._sessions.upsert_goal_state(
            goal.session_id,
            goal.model_dump(mode="json"),
        )
        return goal

    def pause(self, session_id: str, goal_id: str) -> GoalRecord:
        goal = self._load_goal(session_id, goal_id)
        if goal.current_run_id:
            raise HarnessStateError(
                f"Goal {goal_id} has running Run {goal.current_run_id}; "
                "stop the Run before pausing the Goal."
            )
        return self._transition(session_id, goal_id, GoalStatus.PAUSED)

    def resume(self, session_id: str, goal_id: str) -> GoalRecord:
        return self._transition(session_id, goal_id, GoalStatus.ACTIVE)

    def cancel(self, session_id: str, goal_id: str) -> GoalRecord:
        goal = self._load_goal(session_id, goal_id)
        if goal.current_run_id:
            raise HarnessStateError(
                f"Goal {goal_id} has running Run {goal.current_run_id}; "
                "stop the Run before cancelling the Goal."
            )
        return self._transition(session_id, goal_id, GoalStatus.CANCELLED)

    def _transition(
        self,
        session_id: str,
        goal_id: str,
        status: GoalStatus,
    ) -> GoalRecord:
        goal = self._load_goal(session_id, goal_id)
        goal.transition(status)
        self._sessions.upsert_goal_state(session_id, goal.model_dump(mode="json"))
        return goal

    def _load_goal(self, session_id: str, goal_id: str) -> GoalRecord:
        payload = self._sessions.get_goal_state(session_id, goal_id)
        if not payload:
            raise GoalActivationError(
                f"Goal {goal_id} does not exist in session {session_id}."
            )
        goal = GoalRecord.model_validate(payload)
        if goal.session_id != session_id:
            raise GoalActivationError(
                f"Goal {goal_id} belongs to a different session."
            )
        return goal


class CompletionVerificationCoordinator:
    """Translate DeepAgents Rubric terminal state into a Run-owned report."""

    _STATUS_MAP = {
        "satisfied": VerificationStatus.SATISFIED,
        "needs_revision": VerificationStatus.NEEDS_REVISION,
        "failed": VerificationStatus.FAILED,
        "max_iterations_reached": VerificationStatus.MAX_ITERATIONS_REACHED,
        "grader_error": VerificationStatus.GRADER_ERROR,
    }

    @staticmethod
    def compile_contract(
        *,
        user_message: str,
        analytics_model_id: str | None,
        project_id: str | None,
        custom_rules: list[dict[str, Any]] | None = None,
        force_required: bool = False,
    ) -> RunVerificationContract | None:
        return RunRubricCompiler.compile(
            RubricBuildContext(
                user_message=user_message,
                analytics_model_id=analytics_model_id,
                project_id=project_id,
                custom_rules=tuple(custom_rules or ()),
                force_required=force_required,
            )
        )

    @classmethod
    def report_from_final_state(
        cls,
        *,
        run_id: str,
        contract: RunVerificationContract | None,
        final_state: dict[str, Any] | None,
    ) -> RubricEvaluationReport:
        if contract is None:
            return RubricEvaluationReport(
                report_id=f"verification-{uuid.uuid4().hex[:16]}",
                run_id=run_id,
                status=VerificationStatus.NOT_REQUIRED,
            )

        state = final_state or {}
        raw_status = str(state.get("_rubric_status") or "")
        status = cls._STATUS_MAP.get(raw_status, VerificationStatus.GRADER_ERROR)
        raw_evaluations = state.get("_rubric_evaluations")
        evaluations_payload = (
            list(raw_evaluations) if isinstance(raw_evaluations, list) else []
        )
        latest = evaluations_payload[-1] if evaluations_payload else {}
        latest_criteria = (
            latest.get("criteria")
            if isinstance(latest, dict) and isinstance(latest.get("criteria"), list)
            else []
        )
        criteria_by_statement = {
            item.statement: item for item in contract.criteria
        }
        criteria_by_id = {item.id: item for item in contract.criteria}
        evaluations: list[CriterionEvaluation] = []
        gaps: list[str] = []
        for index, raw in enumerate(latest_criteria):
            if not isinstance(raw, dict):
                continue
            name = str(raw.get("name") or f"criterion_{index + 1}")
            configured = criteria_by_id.get(name) or criteria_by_statement.get(name)
            criterion_id = configured.id if configured else name
            gap = str(raw.get("gap") or "").strip() or None
            if gap:
                gaps.append(gap)
            evaluations.append(
                CriterionEvaluation(
                    criterion_id=criterion_id,
                    name=name,
                    passed=bool(raw.get("passed")),
                    verifier=(
                        configured.verifier
                        if configured is not None
                        else VerifierKind.LLM_GRADER
                    ),
                    gap=gap,
                )
            )
        explanation = (
            str(latest.get("explanation") or "")
            if isinstance(latest, dict)
            else ""
        )
        deterministic_evaluations = evaluate_deterministic_criteria(
            contract,
            state,
        )
        if deterministic_evaluations:
            deterministic_by_id = {
                item.criterion_id: item for item in deterministic_evaluations
            }
            evaluations = [
                deterministic_by_id.pop(item.criterion_id, item)
                for item in evaluations
            ]
            evaluations.extend(deterministic_by_id.values())
            deterministic_gaps = [
                item.gap
                for item in deterministic_evaluations
                if not item.passed and item.gap
            ]
            if deterministic_gaps:
                gaps = [*deterministic_gaps, *gaps]
                if status == VerificationStatus.SATISFIED:
                    status = VerificationStatus.NEEDS_REVISION
        if status != VerificationStatus.SATISFIED and not gaps:
            gaps.append(
                explanation
                or f"Run verification ended with status {status.value}."
            )
        return RubricEvaluationReport(
            report_id=f"verification-{uuid.uuid4().hex[:16]}",
            run_id=run_id,
            status=status,
            contract_id=contract.contract_id,
            contract_version=contract.version,
            evaluations=evaluations,
            gaps=gaps,
            explanation=explanation,
            iteration_count=len(evaluations_payload),
        )

    @staticmethod
    def outcome_for_report(report: RubricEvaluationReport) -> RunOutcome:
        if report.status in {
            VerificationStatus.NOT_REQUIRED,
            VerificationStatus.SATISFIED,
        }:
            return RunOutcome.COMPLETED
        return RunOutcome.VERIFICATION_FAILED


class HarnessRunCoordinator:
    """Single write path for product-level Run and optional Goal state."""

    def __init__(self, sessions: SessionManager) -> None:
        self._sessions = sessions
        self.goals = GoalCoordinator(sessions)
        self.verification = CompletionVerificationCoordinator()

    def start_run(
        self,
        *,
        session_id: str,
        query_id: str,
        objective: str,
        goal_mode: bool,
        goal_id: str | None = None,
        project_id: str | None = None,
        analytics_model_id: str | None = None,
        config_snapshot: dict[str, Any] | None = None,
        verification_enabled: bool = True,
        goal_max_rounds: int = 8,
        custom_rubric_rules: list[dict[str, Any]] | None = None,
    ) -> tuple[RunRecord, GoalRecord | None]:
        contract = (
            self.verification.compile_contract(
                user_message=objective,
                analytics_model_id=analytics_model_id,
                project_id=project_id,
                custom_rules=custom_rubric_rules,
                force_required=goal_mode,
            )
            if verification_enabled
            else None
        )
        goal = self.goals.resolve_for_run(
            session_id=session_id,
            objective=objective,
            goal_mode=goal_mode,
            goal_id=goal_id,
            goal_contract=contract,
            max_rounds=goal_max_rounds,
        )
        # An explicit Goal freezes its acceptance contract. Follow-up prompts
        # such as “继续” or “确认后完成” must not weaken or bypass the original
        # Rubric merely because the new message contains fewer task keywords.
        if goal is not None and goal.goal_contract is not None:
            contract = goal.goal_contract.model_copy(deep=True)
        run = RunRecord(
            run_id=f"run-{uuid.uuid4().hex[:16]}",
            query_id=query_id,
            session_id=session_id,
            objective=objective,
            goal_id=goal.goal_id if goal else None,
            project_id=project_id,
            analytics_model_id=analytics_model_id,
            verification_contract=contract,
            config_snapshot=dict(config_snapshot or {}),
        )
        if goal is not None:
            goal.attach_run(run.run_id)
        self._sessions.start_harness_run(
            session_id,
            run.model_dump(mode="json"),
            goal.model_dump(mode="json") if goal is not None else None,
        )
        return run, goal

    def transition(self, run: RunRecord, status: RunStatus) -> RunRecord:
        run.transition(status)
        self._sessions.upsert_run_state(
            run.session_id,
            run.model_dump(mode="json"),
        )
        return run

    def complete_from_final_state(
        self,
        run: RunRecord,
        goal: GoalRecord | None,
        final_state: dict[str, Any] | None,
    ) -> tuple[RunRecord, GoalRecord | None, RubricEvaluationReport]:
        if run.status == RunStatus.RUNNING:
            self.transition(run, RunStatus.EVALUATING)
        report = self.verification.report_from_final_state(
            run_id=run.run_id,
            contract=run.verification_contract,
            final_state=final_state,
        )
        outcome = self.verification.outcome_for_report(report)
        run.verification_report = report
        run.finish(outcome)
        self._sessions.upsert_run_state(
            run.session_id,
            run.model_dump(mode="json"),
        )
        if goal is not None:
            goal = self.goals.apply_run_report(goal, run, report, outcome)
        return run, goal, report

    def complete_budget_exceeded(
        self,
        run: RunRecord,
        goal: GoalRecord | None,
        *,
        reason: str,
        model_call_count: int,
        detail: str,
    ) -> tuple[RunRecord, GoalRecord | None, RubricEvaluationReport]:
        """Finish a Run whose runtime circuit breaker stopped execution."""

        if run.status == RunStatus.RUNNING:
            self.transition(run, RunStatus.EVALUATING)
        report = RubricEvaluationReport(
            report_id=f"verification-{uuid.uuid4().hex[:16]}",
            run_id=run.run_id,
            status=VerificationStatus.BUDGET_EXCEEDED,
            contract_id=(
                run.verification_contract.contract_id
                if run.verification_contract is not None
                else None
            ),
            contract_version=(
                run.verification_contract.version
                if run.verification_contract is not None
                else None
            ),
            gaps=[detail],
            explanation=detail,
        )
        run.model_call_count = max(0, model_call_count)
        run.budget_exhaustion_reason = reason
        run.verification_report = report
        run.finish(RunOutcome.BUDGET_EXCEEDED, error=detail)
        self._sessions.upsert_run_state(
            run.session_id,
            run.model_dump(mode="json"),
        )
        if goal is not None:
            goal = self.goals.apply_run_report(
                goal,
                run,
                report,
                RunOutcome.BUDGET_EXCEEDED,
            )
        return run, goal, report

    def fail(
        self,
        run: RunRecord,
        *,
        outcome: RunOutcome,
        error: str | None = None,
    ) -> RunRecord:
        run.finish(outcome, error=error)
        self._sessions.upsert_run_state(
            run.session_id,
            run.model_dump(mode="json"),
        )
        return run
