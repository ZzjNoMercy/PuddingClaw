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
    RunTaskProfile,
    RunVerificationContract,
    VerificationStatus,
    VerifierKind,
)
from harness.rubric_compiler import RubricBuildContext, RunRubricCompiler
from harness.task_profiles import TaskProfileClassifier


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
                raise GoalActivationError("goal_id was supplied while goal_mode is disabled.")
            return None

        if goal_id:
            goal = self._load_goal(session_id, goal_id)
            if goal.status != GoalStatus.ACTIVE:
                raise GoalActivationError(f"Goal {goal_id} is {goal.status}; resume it before starting a Run.")
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
        goal.current_run_id = None
        goal.model_call_count += max(0, run.model_call_count)
        if report.status in {
            VerificationStatus.INCOMPLETE,
            VerificationStatus.GRADER_ERROR,
        }:
            # An internal verification lifecycle failure is not a business
            # correction attempt. Keep the Run for audit, but refund the Goal
            # round so retrying cannot exhaust max_rounds.
            goal.round = max(0, goal.round - 1)
            goal.updated_at = time.time()
            self._sessions.upsert_goal_state(
                goal.session_id,
                goal.model_dump(mode="json"),
            )
            return goal
        goal.gaps = list(report.gaps)
        if outcome == RunOutcome.COMPLETED and report.status == VerificationStatus.SATISFIED:
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
        run: RunRecord | None = None,
        gap: str | None = None,
    ) -> GoalRecord:
        """Detach a cancelled/failed Run without cancelling the Goal."""

        if run is not None:
            goal.goal_contract = RunRubricCompiler.merge_contracts(
                base=goal.goal_contract,
                expanded=run.verification_contract,
                profile=run.task_profile,
                message=goal.objective,
            )
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
                f"Goal {goal_id} has running Run {goal.current_run_id}; stop the Run before pausing the Goal."
            )
        return self._transition(session_id, goal_id, GoalStatus.PAUSED)

    def resume(self, session_id: str, goal_id: str) -> GoalRecord:
        return self._transition(session_id, goal_id, GoalStatus.ACTIVE)

    def cancel(self, session_id: str, goal_id: str) -> GoalRecord:
        goal = self._load_goal(session_id, goal_id)
        if goal.current_run_id:
            raise HarnessStateError(
                f"Goal {goal_id} has running Run {goal.current_run_id}; stop the Run before cancelling the Goal."
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
            raise GoalActivationError(f"Goal {goal_id} does not exist in session {session_id}.")
        goal = GoalRecord.model_validate(payload)
        if goal.session_id != session_id:
            raise GoalActivationError(f"Goal {goal_id} belongs to a different session.")
        return goal


class CompletionVerificationCoordinator:
    """Translate DeepAgents Rubric terminal state into a Run-owned report."""

    _STATUS_MAP = {
        "satisfied": VerificationStatus.SATISFIED,
        "needs_revision": VerificationStatus.NEEDS_REVISION,
        "failed": VerificationStatus.FAILED,
        "max_iterations_reached": VerificationStatus.MAX_ITERATIONS_REACHED,
        "verification_incomplete": VerificationStatus.INCOMPLETE,
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
        task_profile: RunTaskProfile | None = None,
    ) -> RunVerificationContract | None:
        return RunRubricCompiler.compile(
            RubricBuildContext(
                user_message=user_message,
                analytics_model_id=analytics_model_id,
                project_id=project_id,
                custom_rules=tuple(custom_rules or ()),
                force_required=force_required,
                task_profile=task_profile,
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
        raw_status = str(
            state.get("_rubric_status")
            or state.get("_completion_gate_status")
            or ""
        )
        # An absent terminal verdict means the verification lifecycle did not
        # finish.  It is not evidence that the model grader itself failed.
        status = (
            VerificationStatus.INCOMPLETE
            if raw_status in {"", "pending", "evaluating", "needs_revision"}
            else cls._STATUS_MAP.get(raw_status, VerificationStatus.INCOMPLETE)
        )
        raw_evaluations = state.get("_rubric_evaluations")
        evaluations_payload = list(raw_evaluations) if isinstance(raw_evaluations, list) else []
        latest = evaluations_payload[-1] if evaluations_payload else {}
        latest_criteria = (
            latest.get("criteria") if isinstance(latest, dict) and isinstance(latest.get("criteria"), list) else []
        )
        criteria_by_statement = {item.statement: item for item in contract.criteria}
        criteria_by_id = {item.id: item for item in contract.criteria}
        raw_by_id: dict[str, list[dict[str, Any]]] = {}
        for raw in latest_criteria:
            if not isinstance(raw, dict):
                continue
            name = str(raw.get("name") or "").strip()
            configured = criteria_by_id.get(name) or criteria_by_statement.get(name)
            if configured is None:
                continue
            raw_by_id.setdefault(configured.id, []).append(raw)

        deterministic_evaluations = evaluate_deterministic_criteria(
            contract,
            state,
        )
        deterministic_by_id = {item.criterion_id: item for item in deterministic_evaluations}
        context = state.get("_harness_context")
        harness_context = context if isinstance(context, dict) else {}
        raw_activations = harness_context.get(
            "verification_activations",
            state.get("verification_activations", []),
        )
        activations = raw_activations if isinstance(raw_activations, list) else []
        analytics_evidence = [
            evidence
            for activation in activations
            if isinstance(activation, dict)
            and activation.get("status") == "succeeded"
            and activation.get("pack") == "analytics"
            for evidence in activation.get("evidence_refs") or []
            if isinstance(evidence, dict) and evidence.get("material", True) is not False
        ]

        evaluations: list[CriterionEvaluation] = []
        gaps: list[str] = []
        for configured in contract.criteria:
            deterministic = deterministic_by_id.get(configured.id)
            if deterministic is not None:
                evaluations.append(deterministic)
                if deterministic.gap:
                    gaps.append(deterministic.gap)
                continue
            matches = raw_by_id.get(configured.id, [])
            if not matches:
                if (
                    configured.verifier == VerifierKind.LLM_GRADER
                    and raw_status in {"needs_revision", "max_iterations_reached"}
                    and not evaluations_payload
                ):
                    gap = (
                        f"确定性检查尚未通过，标准 {configured.id} "
                        "尚未进入模型评审。"
                    )
                elif status == VerificationStatus.INCOMPLETE:
                    gap = (
                        f"验收流程在形成终态判定前结束，标准 {configured.id} "
                        "尚未完成评审。"
                    )
                elif status == VerificationStatus.GRADER_ERROR:
                    gap = f"模型验收器执行异常，标准 {configured.id} 未完成评审。"
                else:
                    gap = f"验收器未返回必需标准 {configured.id} 的判定。"
                evaluations.append(
                    CriterionEvaluation(
                        criterion_id=configured.id,
                        name=configured.id,
                        passed=(
                            None
                            if status in {VerificationStatus.INCOMPLETE, VerificationStatus.GRADER_ERROR}
                            else False
                        ),
                        verifier=configured.verifier,
                        gap=gap,
                    )
                )
                if configured.required:
                    gaps.append(gap)
                continue
            if len(matches) > 1:
                gap = f"验收器重复返回标准 {configured.id}，Harness 按 fail-closed 处理。"
                evaluations.append(
                    CriterionEvaluation(
                        criterion_id=configured.id,
                        name=configured.id,
                        passed=False,
                        verifier=configured.verifier,
                        gap=gap,
                    )
                )
                if configured.required:
                    gaps.append(gap)
                continue
            raw = matches[0]
            gap = str(raw.get("gap") or "").strip() or None
            raw_evidence = raw.get("evidence")
            evidence = (
                [item for item in raw_evidence if isinstance(item, dict)] if isinstance(raw_evidence, list) else []
            )
            if configured.id == "metric_consistency":
                evidence = [*evidence, *analytics_evidence]
            passed = bool(raw.get("passed"))
            if configured.verifier == VerifierKind.ANALYTICS and not evidence:
                passed = False
                gap = gap or (f"标准 {configured.id} 没有当前 Run 的结构化分析证据，不能仅凭模型判定通过。")
            if configured.required and gap:
                passed = False
            evaluations.append(
                CriterionEvaluation(
                    criterion_id=configured.id,
                    name=str(raw.get("name") or configured.id),
                    passed=passed,
                    verifier=configured.verifier,
                    evidence=evidence,
                    gap=gap,
                )
            )
            if configured.required and (not passed or gap):
                gaps.append(gap or f"标准 {configured.id} 未通过。")
        explanation = str(latest.get("explanation") or "") if isinstance(latest, dict) else ""
        required_by_id = {item.id: item.required for item in contract.criteria}
        if (
            any(
                (not item.passed or bool(item.gap)) and required_by_id.get(item.criterion_id, True)
                for item in evaluations
            )
            and status == VerificationStatus.SATISFIED
        ):
            status = VerificationStatus.NEEDS_REVISION
        elif status == VerificationStatus.NEEDS_REVISION and all(
            (item.passed and not item.gap) or not required_by_id.get(item.criterion_id, True) for item in evaluations
        ):
            # Deterministic checks are authoritative for their criteria.  A
            # grader may still return an overall needs_revision verdict for a
            # criterion that was subsequently replaced by a passing
            # deterministic evaluation.  Derive the effective aggregate from
            # the merged per-criterion results so the report cannot show all
            # green criteria with a contradictory terminal status.
            status = VerificationStatus.SATISFIED
        if status == VerificationStatus.INCOMPLETE:
            gaps.append("验收控制流程未形成合法终态，请重试本 Run 或查看运行日志。")
        elif status != VerificationStatus.SATISFIED and not gaps:
            gaps.append(explanation or f"Run verification ended with status {status.value}.")
        return RubricEvaluationReport(
            report_id=f"verification-{uuid.uuid4().hex[:16]}",
            run_id=run_id,
            status=status,
            contract_id=contract.contract_id,
            contract_version=contract.version,
            evaluations=evaluations,
            gaps=list(dict.fromkeys(gaps)),
            explanation=explanation,
            iteration_count=max(
                len(evaluations_payload),
                int(state.get("_verification_attempts") or 0),
                int(state.get("_completion_gate_iterations") or 0),
            ),
        )

    @staticmethod
    def outcome_for_report(report: RubricEvaluationReport) -> RunOutcome:
        if report.status in {
            VerificationStatus.NOT_REQUIRED,
            VerificationStatus.SATISFIED,
        }:
            return RunOutcome.COMPLETED
        if report.status == VerificationStatus.INCOMPLETE:
            return RunOutcome.FAILED
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
        task_profile = TaskProfileClassifier.classify(
            message=objective,
            analytics_model_id=analytics_model_id,
        )
        contract = (
            self.verification.compile_contract(
                user_message=objective,
                analytics_model_id=analytics_model_id,
                project_id=project_id,
                custom_rules=custom_rubric_rules,
                force_required=goal_mode,
                task_profile=task_profile,
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
            verification_enabled=verification_enabled,
            task_profile=task_profile,
            declared_verification_contract=(contract.model_copy(deep=True) if contract is not None else None),
            verification_contract=contract,
            config_snapshot=dict(config_snapshot or {}),
        )
        if goal is not None:
            goal.attach_run(run.run_id)
        saved_run, saved_goal = self._sessions.start_harness_run(
            session_id,
            run.model_dump(mode="json"),
            goal.model_dump(mode="json") if goal is not None else None,
        )
        run = RunRecord.model_validate(saved_run)
        goal = GoalRecord.model_validate(saved_goal) if saved_goal is not None else None
        return run, goal

    def bind_execution_snapshot(
        self,
        run: RunRecord,
        execution: dict[str, Any],
    ) -> RunRecord:
        """Finish PREPARING by freezing the effective backend identity."""

        saved = self._sessions.bind_run_execution_snapshot(
            run.session_id,
            run.run_id,
            execution,
        )
        self._replace_run(run, RunRecord.model_validate(saved))
        return run

    def transition(
        self,
        run: RunRecord,
        status: RunStatus,
        *,
        refresh_runtime: bool = True,
    ) -> RunRecord:
        if refresh_runtime:
            self._refresh_runtime_fields(run)
        if status == RunStatus.EVALUATING:
            saved = self._sessions.prepare_run_evaluation(
                run.session_id,
                run.run_id,
            )
            self._replace_run(run, RunRecord.model_validate(saved))
            return run
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
        self._refresh_runtime_fields(run)
        if run.status == RunStatus.RUNNING:
            self.transition(
                run,
                RunStatus.EVALUATING,
                refresh_runtime=False,
            )
        state = dict(final_state or {})
        context = state.get("_harness_context")
        harness_context = dict(context) if isinstance(context, dict) else {}
        if not str(harness_context.get("final_content") or "").strip():
            harness_context["final_content"] = self._last_ai_content(state.get("messages"))
        harness_context["verification_activations"] = [
            item.model_dump(mode="json") if hasattr(item, "model_dump") else dict(item)
            for item in run.verification_activations
            if isinstance(item, dict) or hasattr(item, "model_dump")
        ]
        state["_harness_context"] = harness_context
        report = self.verification.report_from_final_state(
            run_id=run.run_id,
            contract=run.verification_contract,
            final_state=state,
        )
        outcome = self.verification.outcome_for_report(report)
        run.verification_report = report
        run.finish(outcome)
        saved = self._sessions.terminalize_run_state(
            run.session_id,
            run.run_id,
            run.model_dump(mode="json"),
        )
        self._replace_run(run, RunRecord.model_validate(saved))
        if goal is not None:
            goal.goal_contract = RunRubricCompiler.merge_contracts(
                base=goal.goal_contract,
                expanded=run.verification_contract,
                profile=run.task_profile,
                message=goal.objective,
            )
            goal = self.goals.apply_run_report(goal, run, report, outcome)
        return run, goal, report

    def _refresh_runtime_fields(self, run: RunRecord) -> None:
        persisted = self._sessions.get_run_state(run.session_id, run.run_id)
        if not isinstance(persisted, dict):
            return
        current = RunRecord.model_validate(persisted)
        run.verification_activations = list(current.verification_activations)
        if current.verification_contract is not None:
            run.verification_contract = current.verification_contract

    @staticmethod
    def _replace_run(target: RunRecord, source: RunRecord) -> None:
        for field_name in RunRecord.model_fields:
            setattr(target, field_name, getattr(source, field_name))

    @staticmethod
    def _last_ai_content(raw_messages: Any) -> str:
        messages = raw_messages if isinstance(raw_messages, list) else []
        for message in reversed(messages):
            if isinstance(message, dict):
                role = str(message.get("role") or message.get("type") or "").lower()
                if role not in {"ai", "assistant"}:
                    continue
                content = message.get("content")
            else:
                message_type = str(getattr(message, "type", "") or "").lower()
                if message_type not in {"ai", "assistant"}:
                    continue
                content = getattr(message, "content", "")
            if isinstance(content, str):
                return content
            if isinstance(content, list):
                return "\n".join(
                    str(item.get("text") or item.get("content") or "") if isinstance(item, dict) else str(item)
                    for item in content
                )
        return ""

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
        run.verification_contract = RunRubricCompiler.expand_for_activations(
            contract=run.verification_contract,
            profile=run.task_profile,
            message=run.objective,
            activations=list(run.verification_activations),
        )
        report = RubricEvaluationReport(
            report_id=f"verification-{uuid.uuid4().hex[:16]}",
            run_id=run.run_id,
            status=VerificationStatus.BUDGET_EXCEEDED,
            contract_id=(run.verification_contract.contract_id if run.verification_contract is not None else None),
            contract_version=(run.verification_contract.version if run.verification_contract is not None else None),
            gaps=[detail],
            explanation=detail,
        )
        run.model_call_count = max(0, model_call_count)
        run.budget_exhaustion_reason = reason
        run.verification_report = report
        run.finish(RunOutcome.BUDGET_EXCEEDED, error=detail)
        saved = self._sessions.terminalize_run_state(
            run.session_id,
            run.run_id,
            run.model_dump(mode="json"),
        )
        self._replace_run(run, RunRecord.model_validate(saved))
        if goal is not None:
            goal.goal_contract = RunRubricCompiler.merge_contracts(
                base=goal.goal_contract,
                expanded=run.verification_contract,
                profile=run.task_profile,
                message=goal.objective,
            )
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
        self._refresh_runtime_fields(run)
        run.finish(outcome, error=error)
        saved = self._sessions.terminalize_run_state(
            run.session_id,
            run.run_id,
            run.model_dump(mode="json"),
        )
        self._replace_run(run, RunRecord.model_validate(saved))
        return run
