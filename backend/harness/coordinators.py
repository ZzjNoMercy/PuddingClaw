"""Coordinators for Run lifecycle, Run verification, and optional Goals."""

from __future__ import annotations

import hashlib
import re
import time
import uuid
from typing import Any

from graph.session_manager import SessionManager
from harness.artifact_paths import resolve_declared_artifact_targets
from harness.deterministic_checks import evaluate_deterministic_criteria
from harness.evidence_ledger import EvidenceRef, is_evidence_ref, ref_key
from harness.models import (
    CriterionEvaluation,
    GoalCompletionPolicy,
    GoalCompletionRequest,
    GoalCompletionRequestStatus,
    GoalRecord,
    GoalRevision,
    GoalStatus,
    GoalTurnIntent,
    GoalVerificationDecision,
    HarnessStateError,
    RubricEvaluationReport,
    RunKind,
    RunOutcome,
    RunRecord,
    RunStatus,
    RunTaskProfile,
    RunVerificationContract,
    VerificationFailureKind,
    VerificationMode,
    VerificationStatus,
    VerifierKind,
)
from harness.rubric_compiler import RubricBuildContext, RunRubricCompiler
from harness.task_profiles import TaskProfileClassifier


class GoalActivationError(HarnessStateError):
    """Raised when Goal identifiers and the explicit Goal Mode disagree."""


def _evidence_origin_run_ids(value: Any) -> set[str]:
    """Collect explicit Run identity from nested structured evidence."""

    found: set[str] = set()
    if isinstance(value, dict):
        for key, item in value.items():
            if key in {"origin_run_id", "run_id"} and str(item or "").strip():
                found.add(str(item).strip())
            elif isinstance(item, (dict, list)):
                found.update(_evidence_origin_run_ids(item))
    elif isinstance(value, list):
        for item in value:
            found.update(_evidence_origin_run_ids(item))
    return found


def _delta_repair_policy(objective: str) -> tuple[str, int]:
    """Choose a bounded repair policy without granting additional capability."""

    text = str(objective or "").lower()
    data_signal = re.search(
        r"(?:数据|矩阵|重算|补算|查询|统计|sql|database|dataset|recompute|refresh\s+data)",
        text,
    )
    presentation_signal = re.search(
        r"(?:下拉|选项|控件|按钮|默认年份|显示|html|selector|dropdown|option|ui)",
        text,
    )
    if presentation_signal and not data_signal:
        return "presentation_only", 6
    if data_signal:
        return "data_refresh", 12
    return "bounded_unknown", 12


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
        completion_policy: GoalCompletionPolicy = GoalCompletionPolicy.STANDARD,
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
            return self._migrate_declared_contract(goal)

        active = self._sessions.get_active_goal_state(session_id)
        if active:
            goal = GoalRecord.model_validate(active)
            if goal.status == GoalStatus.ACTIVE:
                return self._migrate_declared_contract(goal)

        goal = GoalRecord(
            goal_id=f"goal-{uuid.uuid4().hex[:16]}",
            session_id=session_id,
            objective=objective.strip(),
            goal_contract=goal_contract,
            completion_policy=completion_policy,
            max_rounds=max_rounds,
            revisions=[
                GoalRevision(
                    revision=1,
                    objective=objective.strip(),
                    contract_id=(goal_contract.contract_id if goal_contract else None),
                )
            ],
        )
        return goal

    def _migrate_declared_contract(self, goal: GoalRecord) -> GoalRecord:
        contract = goal.goal_contract
        if contract is None or contract.version == RunRubricCompiler.VERSION:
            return goal
        goal.goal_contract = RunRubricCompiler.rebuild_declared_contract(
            message=goal.objective,
            legacy_contract=contract,
        )
        for revision in goal.revisions:
            if revision.revision == goal.objective_revision:
                revision.contract_id = goal.goal_contract.contract_id
        goal.updated_at = time.time()
        saved = self._sessions.upsert_goal_state(
            goal.session_id,
            goal.model_dump(mode="json"),
        )
        return GoalRecord.model_validate(saved)

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
        if any(not is_evidence_ref(item) for item in goal.evidence_refs):
            self._sessions.migrate_goal_evidence_refs(goal.session_id, goal.goal_id)
        authoritative = self._load_goal(goal.session_id, goal.goal_id)
        if run.goal_revision != authoritative.objective_revision:
            return self._release_superseded_run(authoritative, run)
        goal = authoritative
        # ``goal_contract`` is the immutable declared Goal contract.  A Run's
        # material Tool work belongs only to its effective contract and must
        # not monotonically pollute every later continuation.
        goal.evidence_refs = self._merge_artifact_evidence(
            goal.evidence_refs,
            run.verification_activations,
            session_id=goal.session_id,
            goal_id=goal.goal_id,
            goal_revision=goal.objective_revision,
        )
        goal.latest_verification_report_id = report.report_id
        previous_decision = goal.latest_goal_decision
        provenance_by_criterion: dict[str, dict[str, Any]] = {}
        if (
            previous_decision is not None
            and previous_decision.objective_revision == goal.objective_revision
        ):
            for item in previous_decision.criterion_provenance:
                criterion_id = str(item.get("criterion_id") or "").strip()
                if criterion_id:
                    provenance_by_criterion[criterion_id] = dict(item)
        for evaluation in report.evaluations:
            evidence_refs = [
                EvidenceRef.model_validate(item).model_dump(mode="json")
                for item in evaluation.evidence
                if is_evidence_ref(item)
            ]
            if evaluation.passed is True and not evidence_refs:
                allowed_types = {
                    "analytics_evidence_traceability": {
                        "analytics_result",
                        "sql_generation",
                        "sql_validation",
                    },
                    "metric_consistency": {
                        "analytics_result",
                        "sql_generation",
                        "sql_validation",
                    },
                    "artifact_delivery": {"artifact", "external_mutation"},
                    "code_validation": {"validation_receipt", "artifact"},
                    "web_evidence_traceability": {"web_source"},
                }.get(evaluation.criterion_id, set())
                evidence_refs = [
                    dict(item)
                    for item in goal.evidence_refs
                    if is_evidence_ref(item) and item.get("type") in allowed_types
                ]
            evidence_origin_run_ids: set[str] = set()
            for evidence_ref in evidence_refs:
                resolved = self._sessions.resolve_evidence_ref(
                    goal.session_id,
                    evidence_ref,
                    goal_id=goal.goal_id,
                    goal_revision=goal.objective_revision,
                )
                if resolved is not None and resolved.get("source_run_id"):
                    evidence_origin_run_ids.add(str(resolved["source_run_id"]))
            previous_provenance = provenance_by_criterion.get(evaluation.criterion_id)
            if (
                evaluation.passed is True
                and isinstance(previous_provenance, dict)
                and previous_provenance.get("passed") is True
                and not evidence_refs
            ):
                evidence_origin_run_ids.update(
                    str(item)
                    for item in previous_provenance.get("evidence_origin_run_ids") or []
                    if str(item)
                )
                prior_evidence = previous_provenance.get("evidence_refs")
                if isinstance(prior_evidence, list):
                    evidence_refs = [dict(item) for item in prior_evidence if isinstance(item, dict)]
            supporting_run_ids = sorted({run.run_id, *evidence_origin_run_ids})
            provenance_by_criterion[evaluation.criterion_id] = {
                "criterion_id": evaluation.criterion_id,
                "name": evaluation.name,
                "passed": evaluation.passed,
                "verifier": evaluation.verifier.value,
                "evaluated_in_run_id": run.run_id,
                "evidence_origin_run_ids": sorted(evidence_origin_run_ids),
                "supporting_run_ids": supporting_run_ids,
                "evidence_refs": evidence_refs,
                "gap": evaluation.gap,
                "report_id": report.report_id,
            }
        criterion_provenance = list(provenance_by_criterion.values())
        supporting_run_ids = sorted(
            {
                str(run_id)
                for item in criterion_provenance
                for run_id in item.get("supporting_run_ids") or []
                if str(run_id)
            }
        )
        unresolved_provenance = [
            item
            for item in criterion_provenance
            if item.get("passed") is not True or bool(item.get("gap"))
        ]
        aggregate_gaps = list(
            dict.fromkeys(
                [
                    *report.gaps,
                    *[
                        str(item.get("gap") or f"标准 {item.get('criterion_id')} 尚未通过。")
                        for item in unresolved_provenance
                    ],
                ]
            )
        )
        decision_accepted = bool(
            report.status in {VerificationStatus.SATISFIED, VerificationStatus.NOT_REQUIRED}
            and not unresolved_provenance
            and goal.requested_status not in {GoalStatus.PAUSED, GoalStatus.CANCELLED}
        )
        aggregate_status = (
            report.status if decision_accepted else VerificationStatus.NEEDS_REVISION
            if report.status in {VerificationStatus.SATISFIED, VerificationStatus.NOT_REQUIRED}
            else report.status
        )
        goal.latest_goal_decision = GoalVerificationDecision(
            decision_id=f"goal-decision-{uuid.uuid4().hex[:16]}",
            goal_id=goal.goal_id,
            objective_revision=goal.objective_revision,
            status=aggregate_status,
            accepted=decision_accepted,
            supporting_run_ids=supporting_run_ids,
            criterion_provenance=criterion_provenance,
            evidence_ref_count=len(goal.evidence_refs),
            gaps=aggregate_gaps,
            accepted_run_id=(run.run_id if decision_accepted else None),
            report_id=report.report_id,
        )
        goal.current_run_id = None
        goal.model_call_count += max(0, run.model_call_count)
        requested_status = goal.requested_status
        if requested_status in {GoalStatus.PAUSED, GoalStatus.CANCELLED}:
            goal.requested_status = None
            goal.transition(requested_status)
            saved = self._sessions.finalize_goal_run_state(
                goal.session_id,
                goal.model_dump(mode="json"),
                run_id=run.run_id,
            )
            return GoalRecord.model_validate(saved)
        if report.status in {
            VerificationStatus.INCOMPLETE,
            VerificationStatus.GRADER_ERROR,
            VerificationStatus.INFRASTRUCTURE_ERROR,
        }:
            # An internal verification lifecycle failure is not a business
            # correction attempt. Refund the business round, but track a
            # separate failure fingerprint so autonomous retries remain finite.
            fingerprint_payload = "|".join(
                [report.status.value, *sorted(str(item) for item in report.gaps)]
            )
            fingerprint = hashlib.sha256(
                fingerprint_payload.encode("utf-8")
            ).hexdigest()[:20]
            if fingerprint == goal.last_control_failure_fingerprint:
                goal.consecutive_control_failure_count += 1
            else:
                goal.last_control_failure_fingerprint = fingerprint
                goal.consecutive_control_failure_count = 1
            goal.total_control_retry_count += 1
            goal.round = max(0, goal.round - 1)
            for notice in report.gaps:
                if notice and notice not in goal.control_notices:
                    goal.control_notices.append(notice)
            if (
                goal.consecutive_control_failure_count >= goal.max_control_retries
                or goal.total_control_retry_count >= goal.max_total_control_retries
            ):
                goal.transition(GoalStatus.BLOCKED)
            else:
                goal.updated_at = time.time()
            saved = self._sessions.finalize_goal_run_state(
                goal.session_id,
                goal.model_dump(mode="json"),
                run_id=run.run_id,
            )
            return GoalRecord.model_validate(saved)
        goal.consecutive_control_failure_count = 0
        goal.last_control_failure_fingerprint = None
        if report.status == VerificationStatus.BUDGET_EXCEEDED:
            for notice in report.gaps:
                if notice and notice not in goal.control_notices:
                    goal.control_notices.append(notice)
        else:
            goal.gaps = aggregate_gaps
        aggregate_status = goal.latest_goal_decision.status
        if outcome == RunOutcome.COMPLETED and aggregate_status == VerificationStatus.SATISFIED:
            goal.transition(GoalStatus.COMPLETED)
        elif (
            outcome == RunOutcome.COMPLETED
            and report.status == VerificationStatus.NOT_REQUIRED
            and goal.goal_contract is None
        ):
            goal.transition(GoalStatus.COMPLETED)
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
        saved = self._sessions.finalize_goal_run_state(
            goal.session_id,
            goal.model_dump(mode="json"),
            run_id=run.run_id,
        )
        return GoalRecord.model_validate(saved)

    def release_run(
        self,
        goal: GoalRecord,
        *,
        run: RunRecord | None = None,
        gap: str | None = None,
    ) -> GoalRecord:
        """Detach a cancelled/failed Run without cancelling the Goal."""

        if any(not is_evidence_ref(item) for item in goal.evidence_refs):
            self._sessions.migrate_goal_evidence_refs(goal.session_id, goal.goal_id)
        authoritative = self._load_goal(goal.session_id, goal.goal_id)
        superseded = (
            run is not None
            and run.goal_revision != authoritative.objective_revision
        )
        goal = authoritative
        attached_run_id = run.run_id if run is not None else str(goal.current_run_id or "")
        if run is not None and not superseded:
            goal.evidence_refs = self._merge_artifact_evidence(
                goal.evidence_refs,
                run.verification_activations,
                session_id=goal.session_id,
                goal_id=goal.goal_id,
                goal_revision=goal.objective_revision,
            )
            goal.model_call_count += max(0, run.model_call_count)
        goal.current_run_id = None
        if gap and gap not in goal.control_notices:
            goal.control_notices.append(gap)
        requested_status = goal.requested_status
        goal.requested_status = None
        if requested_status in {GoalStatus.PAUSED, GoalStatus.CANCELLED}:
            goal.transition(requested_status)
        goal.updated_at = time.time()
        if not attached_run_id:
            raise HarnessStateError("Goal has no attached Run to release")
        saved = self._sessions.finalize_goal_run_state(
            goal.session_id,
            goal.model_dump(mode="json"),
            run_id=attached_run_id,
        )
        return GoalRecord.model_validate(saved)

    def _merge_artifact_evidence(
        self,
        existing: list[dict[str, Any]],
        activations: list[Any],
        *,
        session_id: str,
        goal_id: str,
        goal_revision: int,
    ) -> list[dict[str, Any]]:
        """Persist only resolved, revision-bound stable refs across Goal Runs."""

        merged: dict[str, dict[str, Any]] = {}
        for ref in existing:
            if not is_evidence_ref(ref):
                continue
            resolved = self._sessions.resolve_evidence_ref(
                session_id,
                ref,
                goal_id=goal_id,
                goal_revision=goal_revision,
            )
            if resolved is None:
                continue
            parsed = EvidenceRef.model_validate(ref)
            merged[ref_key(parsed)] = parsed.model_dump(mode="json")
        for activation in activations:
            payload = (
                activation.model_dump(mode="json")
                if hasattr(activation, "model_dump")
                else activation
            )
            if not isinstance(payload, dict) or payload.get("status") != "succeeded":
                continue
            for ref in payload.get("stable_evidence_refs") or []:
                if not is_evidence_ref(ref):
                    continue
                resolved = self._sessions.resolve_evidence_ref(
                    session_id,
                    ref,
                    goal_id=goal_id,
                    goal_revision=goal_revision,
                )
                if resolved is None:
                    continue
                parsed = EvidenceRef.model_validate(ref)
                merged[ref_key(parsed)] = parsed.model_dump(mode="json")
        return list(merged.values())

    def update_objective(
        self,
        session_id: str,
        goal_id: str,
        *,
        objective: str,
        expected_revision: int,
    ) -> GoalRecord:
        """Create an auditable Goal revision and rebuild its acceptance contract."""

        normalized = objective.strip()
        if not normalized:
            raise HarnessStateError("Goal objective cannot be empty.")
        if len(normalized) > 20_000:
            raise HarnessStateError("Goal objective is too long (maximum 20000 characters).")
        goal = self._load_goal(session_id, goal_id)
        if goal.terminal:
            raise HarnessStateError(
                f"Goal {goal_id} is already terminal ({goal.status}); create a new Goal instead."
            )
        if goal.round >= goal.max_rounds:
            raise HarnessStateError(f"Goal {goal_id} has no remaining Runs.")
        if goal.objective_revision != expected_revision:
            raise HarnessStateError(
                f"Goal revision conflict: expected {expected_revision}, "
                f"current {goal.objective_revision}."
            )
        if normalized == goal.objective:
            return goal

        latest_run = self._sessions.get_run_state(
            session_id,
            goal.current_run_id or (goal.run_ids[-1] if goal.run_ids else None),
        )
        project_id = latest_run.get("project_id") if isinstance(latest_run, dict) else None
        analytics_model_id = (
            latest_run.get("analytics_model_id") if isinstance(latest_run, dict) else None
        )
        snapshot = latest_run.get("config_snapshot") if isinstance(latest_run, dict) else {}
        completion = snapshot.get("completion") if isinstance(snapshot, dict) else {}
        rubric = completion.get("rubric") if isinstance(completion, dict) else {}
        custom_rules = (
            list(rubric.get("custom_rules") or [])
            if isinstance(rubric, dict) and rubric.get("custom_rules_enabled", False)
            else []
        )
        profile = TaskProfileClassifier.classify(
            message=normalized,
            analytics_model_id=(str(analytics_model_id) if analytics_model_id else None),
        )
        contract = CompletionVerificationCoordinator.compile_contract(
            user_message=normalized,
            analytics_model_id=(str(analytics_model_id) if analytics_model_id else None),
            project_id=(str(project_id) if project_id else None),
            custom_rules=custom_rules,
            force_required=True,
            task_profile=profile,
        )
        try:
            saved = self._sessions.update_goal_objective(
                session_id,
                goal_id,
                objective=normalized,
                expected_revision=expected_revision,
                contract=(contract.model_dump(mode="json") if contract is not None else None),
            )
        except ValueError as exc:
            raise HarnessStateError(str(exc)) from exc
        return GoalRecord.model_validate(saved)

    def _release_superseded_run(self, goal: GoalRecord, run: RunRecord) -> GoalRecord:
        """Close an old-revision Run without letting it satisfy the revised Goal."""

        goal.current_run_id = None
        goal.model_call_count += max(0, run.model_call_count)
        goal.pending_revision = True
        revision_notice = "目标描述已更新，将按最新版本进入下一 Run。"
        if revision_notice not in goal.control_notices:
            goal.control_notices.append(revision_notice)
        # Old-revision acceptance gaps cannot be used to judge the new Goal.
        goal.gaps = []
        goal.updated_at = time.time()
        saved = self._sessions.finalize_goal_run_state(
            goal.session_id,
            goal.model_dump(mode="json"),
            run_id=run.run_id,
        )
        return GoalRecord.model_validate(saved)

    def pause(self, session_id: str, goal_id: str) -> GoalRecord:
        try:
            saved = self._sessions.request_goal_control(
                session_id,
                goal_id,
                GoalStatus.PAUSED.value,
            )
        except ValueError as exc:
            raise HarnessStateError(str(exc)) from exc
        return GoalRecord.model_validate(saved)

    def resume(self, session_id: str, goal_id: str) -> GoalRecord:
        goal = self._load_goal(session_id, goal_id)
        if goal.requested_status is not None or goal.current_run_id is not None:
            raise HarnessStateError(
                "当前 Run 正在收尾，需等待暂停生效后再恢复 Goal。"
            )
        return self._transition(session_id, goal_id, GoalStatus.ACTIVE)

    def extend_budget(
        self,
        session_id: str,
        goal_id: str,
        *,
        additional_rounds: int,
    ) -> GoalRecord:
        """Reopen an exhausted Goal with an explicit user-granted Run budget."""

        try:
            saved = self._sessions.extend_goal_budget(
                session_id,
                goal_id,
                additional_rounds=additional_rounds,
            )
        except ValueError as exc:
            raise HarnessStateError(str(exc)) from exc
        return GoalRecord.model_validate(saved)

    def cancel(self, session_id: str, goal_id: str) -> GoalRecord:
        try:
            saved = self._sessions.request_goal_control(
                session_id,
                goal_id,
                GoalStatus.CANCELLED.value,
            )
        except ValueError as exc:
            raise HarnessStateError(str(exc)) from exc
        return GoalRecord.model_validate(saved)

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
        "infrastructure_error": VerificationStatus.INFRASTRUCTURE_ERROR,
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
        unknown_grader_criteria: list[str] = []
        known_managed_criteria = {
            "task_fulfillment",
            "todo_reconciliation",
            "tool_protocol_integrity",
            "web_evidence_traceability",
            "metric_consistency",
            "analytics_evidence_traceability",
            "artifact_delivery",
            "code_validation",
            "time_scope",
            "report_integrity",
        }
        for raw in latest_criteria:
            if not isinstance(raw, dict):
                continue
            name = str(raw.get("name") or "").strip()
            configured = criteria_by_id.get(name) or criteria_by_statement.get(name)
            if configured is None:
                if name not in known_managed_criteria:
                    unknown_grader_criteria.append(name or "未命名标准")
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
                not_evaluated = False
                deterministic_gate_status = str(
                    state.get("_completion_gate_status") or ""
                )
                if (
                    configured.verifier == VerifierKind.LLM_GRADER
                    and (
                        raw_status
                        in {"needs_revision", "max_iterations_reached"}
                        or deterministic_gate_status
                        in {
                            "needs_revision",
                            "failed",
                            VerificationStatus.INFRASTRUCTURE_ERROR.value,
                        }
                    )
                    and not evaluations_payload
                ):
                    not_evaluated = True
                    gap = (
                        f"确定性检查尚未通过，标准 {configured.id} "
                        "尚未进入模型评审。"
                    )
                elif status == VerificationStatus.INCOMPLETE:
                    not_evaluated = True
                    gap = (
                        f"验收流程在形成终态判定前结束，标准 {configured.id} "
                        "尚未完成评审。"
                    )
                elif status == VerificationStatus.GRADER_ERROR:
                    not_evaluated = True
                    gap = f"模型验收器执行异常，标准 {configured.id} 未完成评审。"
                else:
                    gap = f"验收器未返回必需标准 {configured.id} 的判定。"
                evaluations.append(
                    CriterionEvaluation(
                        criterion_id=configured.id,
                        name=configured.id,
                        passed=None if not_evaluated else False,
                        verifier=configured.verifier,
                        evidence=(
                            [{"kind": "criterion_state", "status": "not_evaluated"}]
                            if not_evaluated
                            else []
                        ),
                        gap=gap,
                    )
                )
                if configured.required and not not_evaluated:
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
        if status == VerificationStatus.GRADER_ERROR and not gaps:
            control_gap = "模型验收器执行异常，业务标准尚未评审。"
            gaps.append(control_gap)
            explanation = explanation or control_gap
        if unknown_grader_criteria:
            unknown_gap = (
                "模型验收器返回了契约外标准："
                + "、".join(dict.fromkeys(unknown_grader_criteria))
                + "。Harness 未采纳该结果。"
            )
            gaps.append(unknown_gap)
            status = VerificationStatus.GRADER_ERROR
            explanation = unknown_gap
        required_by_id = {item.id: item.required for item in contract.criteria}
        if (
            any(
                (item.passed is False or (item.passed is not None and bool(item.gap)))
                and required_by_id.get(item.criterion_id, True)
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
            status = VerificationStatus.INCOMPLETE
            explanation = (
                "模型总体判定与逐项结果存在冲突；Harness 没有猜测通过，"
                "已将本次验收标记为流程异常。"
            )
        infrastructure_gaps = [
            item.gap
            for item in evaluations
            if item.failure_kind == VerificationFailureKind.INFRASTRUCTURE_ERROR
            and item.gap
        ]
        if infrastructure_gaps:
            status = VerificationStatus.INFRASTRUCTURE_ERROR
            explanation = "验收基础设施异常：" + "；".join(infrastructure_gaps)
        elif status == VerificationStatus.GRADER_ERROR:
            control_gap = "模型验收器执行异常，业务标准尚未评审。"
            gaps = [control_gap]
            explanation = control_gap
        elif status != VerificationStatus.SATISFIED and gaps:
            # The merged deterministic result is authoritative. Do not retain a
            # contradictory grader explanation such as “所有标准均满足”.
            deterministic_gaps = [
                item.gap
                for item in evaluations
                if item.verifier == VerifierKind.DETERMINISTIC
                and item.passed is False
                and item.gap
            ]
            explanation = (
                "确定性检查失败：" + "；".join(dict.fromkeys(deterministic_gaps))
                if deterministic_gaps
                else "验收未通过：" + "；".join(dict.fromkeys(gaps))
            )
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
        if report.status == VerificationStatus.INFRASTRUCTURE_ERROR:
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
        completion_policy: GoalCompletionPolicy | str = GoalCompletionPolicy.STANDARD,
        goal_max_rounds: int = 8,
        custom_rubric_rules: list[dict[str, Any]] | None = None,
        task_profile: RunTaskProfile | None = None,
        run_kind: RunKind | str | None = None,
        context_goal_id: str | None = None,
        context_goal_revision: int | None = None,
        goal_turn_intent: GoalTurnIntent | str | None = None,
        goal_turn_confidence: float | None = None,
        goal_turn_classifier: str | None = None,
    ) -> tuple[RunRecord, GoalRecord | None]:
        resolved_run_kind = RunKind(
            run_kind
            or (RunKind.GOAL_EXECUTION if goal_mode else RunKind.STANDALONE)
        )
        if goal_id and resolved_run_kind != RunKind.GOAL_EXECUTION:
            raise GoalActivationError("goal_id was supplied while goal_mode is disabled.")
        if resolved_run_kind == RunKind.GOAL_EXECUTION and not goal_mode:
            raise GoalActivationError("goal_execution requires goal_mode=true")
        if resolved_run_kind == RunKind.GOAL_INSPECTION and goal_mode:
            raise GoalActivationError("goal_inspection cannot own a Goal")
        if resolved_run_kind == RunKind.GOAL_INSPECTION and not context_goal_id:
            raise GoalActivationError("goal_inspection requires context_goal_id")
        supplied_task_profile = task_profile is not None
        task_profile = (
            task_profile.model_copy(deep=True)
            if task_profile is not None
            else TaskProfileClassifier.classify(
                message=objective,
                analytics_model_id=analytics_model_id,
            )
        )
        resolved_completion_policy = GoalCompletionPolicy(completion_policy)
        contract = (
            self.verification.compile_contract(
                user_message=objective,
                analytics_model_id=analytics_model_id,
                project_id=project_id,
                custom_rules=custom_rubric_rules,
                force_required=(resolved_run_kind == RunKind.GOAL_EXECUTION),
                task_profile=task_profile,
            )
            if verification_enabled and resolved_completion_policy == GoalCompletionPolicy.RUBRIC
            else None
        )
        goal = (
            self.goals.resolve_for_run(
                session_id=session_id,
                objective=objective,
                goal_mode=True,
                goal_id=goal_id,
                goal_contract=contract,
                completion_policy=resolved_completion_policy,
                max_rounds=goal_max_rounds,
            )
            if resolved_run_kind == RunKind.GOAL_EXECUTION
            else None
        )
        # A continuation inherits the Goal's frozen completion policy.  The
        # caller's default is only meaningful while creating a new Goal; it
        # must not silently downgrade an existing Rubric Goal to Agent mode.
        if goal is not None:
            resolved_completion_policy = goal.completion_policy
        effective_objective = goal.objective if goal is not None else objective
        if effective_objective != objective and not supplied_task_profile:
            task_profile = TaskProfileClassifier.classify(
                message=effective_objective,
                analytics_model_id=analytics_model_id,
            )
        # An explicit Goal freezes its acceptance contract. Follow-up prompts
        # such as “继续” or “确认后完成” must not weaken or bypass the original
        # Rubric merely because the new message contains fewer task keywords.
        if goal is not None and goal.goal_contract is not None:
            contract = goal.goal_contract.model_copy(deep=True)
            latest_decision = goal.latest_goal_decision
            if (
                latest_decision is not None
                and latest_decision.objective_revision == goal.objective_revision
                and not latest_decision.accepted
            ):
                unresolved_ids = {
                    str(item.get("criterion_id") or "")
                    for item in latest_decision.criterion_provenance
                    if item.get("passed") is not True or bool(item.get("gap"))
                }
                task_profile.initial_packs = RunRubricCompiler._normalize_packs(
                    [
                        *task_profile.initial_packs,
                        *RunRubricCompiler.packs_for_criteria(unresolved_ids),
                    ]
                )
        effective_contract = (
            RunRubricCompiler.expand_for_activations(
                contract=contract,
                profile=task_profile,
                message=effective_objective,
                activations=[],
            )
            if verification_enabled
            else None
        )
        follow_up_artifacts = (
            self._sessions.resolve_follow_up_artifacts(session_id, objective)
            if goal is None
            else []
        )
        follow_up_artifact_ids = [
            str(item.get("artifact_id") or "")
            for item in follow_up_artifacts
            if str(item.get("artifact_id") or "")
        ]
        follow_up_goal_ids = {
            str(item.get("source_goal_id") or "")
            for item in follow_up_artifacts
            if str(item.get("source_goal_id") or "")
        }
        delta_repair = bool(follow_up_artifact_ids) and bool(
            re.search(
                r"(?:还没|没有更新|没更新|补上|修复|不对|有误|改一下|调整|纠正|刷新)",
                objective,
            )
        )
        delta_repair_kind, delta_repair_tool_budget = (
            _delta_repair_policy(objective) if delta_repair else (None, None)
        )
        run = RunRecord(
            run_id=f"run-{uuid.uuid4().hex[:16]}",
            query_id=query_id,
            session_id=session_id,
            objective=effective_objective,
            declared_artifact_targets=resolve_declared_artifact_targets(effective_objective),
            declared_artifact_targets_version=2,
            run_kind=resolved_run_kind,
            goal_id=goal.goal_id if goal else None,
            context_goal_id=context_goal_id,
            context_goal_revision=context_goal_revision,
            goal_revision=goal.objective_revision if goal else None,
            goal_turn_intent=(
                GoalTurnIntent(goal_turn_intent) if goal_turn_intent is not None else None
            ),
            goal_turn_confidence=goal_turn_confidence,
            goal_turn_classifier=goal_turn_classifier,
            follow_up_of_goal_id=(
                next(iter(follow_up_goal_ids))
                if len(follow_up_goal_ids) == 1
                else None
            ),
            follow_up_of_artifact_ids=follow_up_artifact_ids,
            execution_mode="delta_repair" if delta_repair else "native",
            delta_repair_kind=delta_repair_kind,
            delta_repair_tool_budget=delta_repair_tool_budget,
            project_id=project_id,
            analytics_model_id=analytics_model_id,
            verification_enabled=verification_enabled,
            verification_mode=(
                VerificationMode.RUBRIC
                if resolved_completion_policy == GoalCompletionPolicy.RUBRIC
                and verification_enabled
                and resolved_run_kind == RunKind.GOAL_EXECUTION
                and goal is not None
                else VerificationMode.AGENT
            ),
            task_profile=task_profile,
            declared_verification_contract=(contract.model_copy(deep=True) if contract is not None else None),
            verification_contract=effective_contract,
            config_snapshot=dict(config_snapshot or {}),
        )
        if goal is not None:
            goal.pending_revision = False
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
    ) -> tuple[RunRecord, GoalRecord | None, RubricEvaluationReport | None]:
        self._refresh_runtime_fields(run)
        # A standard Goal is accepted only after the Agent made an explicit,
        # still-live completion declaration.  Natural termination completes
        # this Run but never silently completes the cross-Run Goal.
        if goal is not None and goal.completion_policy == GoalCompletionPolicy.STANDARD:
            state = self._sessions.get_harness_state(run.session_id)
            raw_requests = state.get("completion_requests") or {}
            raw_request = (
                raw_requests.get(run.completion_request_id)
                if isinstance(raw_requests, dict) and run.completion_request_id
                else None
            )
            request = GoalCompletionRequest.model_validate(raw_request) if isinstance(raw_request, dict) else None
            if (
                request is not None
                and request.status == GoalCompletionRequestStatus.REQUESTED
                and request.goal_id == goal.goal_id
                and request.run_id == run.run_id
                and request.objective_revision == goal.objective_revision == run.goal_revision
            ):
                run.finish(RunOutcome.COMPLETED)
                goal.model_call_count += max(0, run.model_call_count)
                goal.transition(GoalStatus.COMPLETED)
                goal.latest_completion_request_id = request.request_id
                return run, goal, None
            run.finish(RunOutcome.COMPLETED)
            saved = self._sessions.terminalize_run_state(
                run.session_id, run.run_id, run.model_dump(mode="json")
            )
            self._replace_run(run, RunRecord.model_validate(saved))
            goal = self.goals.release_run(goal, run=run)
            return run, goal, None
        if goal is not None and goal.completion_policy == GoalCompletionPolicy.RUBRIC:
            state = self._sessions.get_harness_state(run.session_id)
            raw_requests = state.get("completion_requests") or {}
            raw_request = (
                raw_requests.get(run.completion_request_id)
                if isinstance(raw_requests, dict) and run.completion_request_id
                else None
            )
            request = GoalCompletionRequest.model_validate(raw_request) if isinstance(raw_request, dict) else None
            if not (
                request is not None
                and request.status == GoalCompletionRequestStatus.REQUESTED
                and request.policy == GoalCompletionPolicy.RUBRIC
                and request.goal_id == goal.goal_id
                and request.run_id == run.run_id
                and request.objective_revision == goal.objective_revision == run.goal_revision
            ):
                run.finish(RunOutcome.COMPLETED)
                saved = self._sessions.terminalize_run_state(
                    run.session_id, run.run_id, run.model_dump(mode="json")
                )
                self._replace_run(run, RunRecord.model_validate(saved))
                goal = self.goals.release_run(goal, run=run)
                return run, goal, None
            self._sessions.update_goal_completion_request_status(
                run.session_id,
                request.request_id,
                GoalCompletionRequestStatus.EVALUATING.value,
            )
        if run.status == RunStatus.RUNNING and run.requires_goal_verification:
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
        if goal is not None:
            self._sessions.backfill_goal_declared_artifact_writes(
                run.session_id,
                goal.goal_id,
                int(run.goal_revision or 1),
            )
            self._sessions.restore_goal_artifact_evidence(
                run.session_id,
                goal.goal_id,
            )
        authoritative_goal = (
            self._sessions.get_goal_state(run.session_id, goal.goal_id)
            if goal is not None
            else None
        )
        if (
            goal is not None
            and isinstance(authoritative_goal, dict)
            and any(
                not is_evidence_ref(item)
                for item in authoritative_goal.get("evidence_refs") or []
            )
        ):
            self._sessions.migrate_goal_evidence_refs(run.session_id, goal.goal_id)
            authoritative_goal = self._sessions.get_goal_state(
                run.session_id,
                goal.goal_id,
            )
        authoritative_revision = (
            authoritative_goal.get("objective_revision")
            if isinstance(authoritative_goal, dict)
            else None
        )
        raw_goal_evidence = (
            authoritative_goal.get("evidence_refs")
            if isinstance(authoritative_goal, dict)
            and authoritative_revision == run.goal_revision
            else []
        )
        harness_context["goal_evidence_refs"] = [
            EvidenceRef.model_validate(item).model_dump(mode="json")
            for item in (raw_goal_evidence if isinstance(raw_goal_evidence, list) else [])
            if is_evidence_ref(item)
        ]
        harness_context["goal_evidence_records"] = []
        for evidence_ref in harness_context["goal_evidence_refs"]:
            resolved = self._sessions.resolve_evidence_ref(
                run.session_id,
                evidence_ref,
                goal_id=run.goal_id,
                goal_revision=run.goal_revision,
                allow_artifact_revision_inheritance=True,
            )
            if resolved is None:
                continue
            payload = resolved.get("payload")
            record = dict(payload) if isinstance(payload, dict) else {}
            record.update(
                {
                    "evidence_ref": dict(evidence_ref),
                    "evidence_type": resolved.get("kind"),
                    "evidence_id": resolved.get("id"),
                    "verification_pack": resolved.get("verification_pack"),
                    "origin_run_id": resolved.get("source_run_id"),
                    "run_id": resolved.get("source_run_id"),
                    "tool_call_id": resolved.get("origin_tool_call_id"),
                    "output_digest": resolved.get("output_digest"),
                    "result_id": resolved.get("result_id"),
                    "query_trace_id": resolved.get("query_trace_id"),
                    "generation_id": resolved.get("generation_id"),
                    "sql_validation_receipt_id": resolved.get(
                        "sql_validation_receipt_id"
                    ),
                    "source_goal_revision": resolved.get("goal_revision"),
                    "revision_inherited": bool(
                        run.goal_revision is not None
                        and resolved.get("goal_revision") is not None
                        and int(resolved["goal_revision"]) < int(run.goal_revision)
                    ),
                }
            )
            harness_context["goal_evidence_records"].append(record)
        execution = (
            run.config_snapshot.get("execution", {})
            if isinstance(run.config_snapshot, dict)
            else {}
        )
        harness_context.update(
            {
                "run_id": run.run_id,
                "goal_id": run.goal_id or "",
                "goal_revision": run.goal_revision,
                "workspace_id": str(execution.get("workspace_id") or ""),
                "backend_id": str(execution.get("backend_id") or ""),
                "declared_artifact_targets": list(run.declared_artifact_targets),
                "active_permission_grant_ids": [
                    str(item.get("id"))
                    for item in self._sessions.list_permission_grants(run.session_id)
                    if item.get("id")
                ],
                "permission_grants_authoritative": True,
            }
        )
        state["_harness_context"] = harness_context
        report = self.verification.report_from_final_state(
            run_id=run.run_id,
            contract=(
                run.verification_contract
                if run.requires_goal_verification
                else None
            ),
            final_state=state,
        )
        if goal is not None:
            report.verification_scope = "goal_aggregate"
            report.supporting_run_ids = list(dict.fromkeys([*goal.run_ids, run.run_id]))
            report.goal_revision = run.goal_revision
            report.accepted_for_goal_revision = False
        else:
            report.accepted_for_goal_revision = (
                report.status == VerificationStatus.SATISFIED
            )
        outcome = self.verification.outcome_for_report(report)
        run.verification_report = report
        # Successful Rubric results remain a proposal until the candidate
        # final response is available.  Do not terminalize either authority
        # here: SessionManager commits request, report, Run, Goal and message
        # together in one write.
        if (
            goal is not None
            and goal.completion_policy == GoalCompletionPolicy.RUBRIC
            and outcome == RunOutcome.COMPLETED
            and report.status == VerificationStatus.SATISFIED
        ):
            report.accepted_for_goal_revision = True
            report.goal_revision = run.goal_revision
            report.verification_scope = "goal_aggregate"
            report.supporting_run_ids = list(dict.fromkeys([*goal.run_ids, run.run_id]))
            run.verification_report = report
            run.finish(outcome)
            goal.latest_verification_report_id = report.report_id
            goal.transition(GoalStatus.COMPLETED)
            return run, goal, report
        run.finish(outcome)
        saved = self._sessions.terminalize_run_state(
            run.session_id,
            run.run_id,
            run.model_dump(mode="json"),
        )
        self._replace_run(run, RunRecord.model_validate(saved))
        if goal is not None:
            goal = self.goals.apply_run_report(goal, run, report, outcome)
            if run.completion_request_id:
                request_status = (
                    GoalCompletionRequestStatus.EVALUATING
                    if goal.status == GoalStatus.COMPLETED
                    else GoalCompletionRequestStatus.NEEDS_REVISION
                    if report.status in {
                        VerificationStatus.NEEDS_REVISION,
                        VerificationStatus.FAILED,
                        VerificationStatus.MAX_ITERATIONS_REACHED,
                    }
                    else GoalCompletionRequestStatus.REJECTED
                )
                self._sessions.update_goal_completion_request_status(
                    run.session_id,
                    run.completion_request_id,
                    request_status.value,
                    verification_report_id=report.report_id,
                )
            decision = goal.latest_goal_decision
            if decision is not None and decision.objective_revision == run.goal_revision:
                report.supporting_run_ids = list(decision.supporting_run_ids)
            report.accepted_for_goal_revision = bool(
                goal.status == GoalStatus.COMPLETED
                and goal.objective_revision == run.goal_revision
                and (
                    report.status == VerificationStatus.NOT_REQUIRED
                    or (
                        decision is not None
                        and decision.objective_revision == run.goal_revision
                        and decision.accepted
                        and decision.accepted_run_id == run.run_id
                    )
                )
            )
            run.verification_report = report
            saved = self._sessions.update_terminal_run_verification_report(
                run.session_id,
                run.run_id,
                report.model_dump(mode="json"),
            )
            self._replace_run(run, RunRecord.model_validate(saved))
        return run, goal, report

    def _refresh_runtime_fields(self, run: RunRecord) -> None:
        """Refresh fields that Tool middleware may persist during a live Run.

        The graph holds a RunRecord created before Tool execution.  Trusted
        middleware such as ``update_goal`` writes completion authority directly
        to Session state, so the in-memory record must observe those fields
        before terminal completion is decided.
        """

        persisted = self._sessions.get_run_state(run.session_id, run.run_id)
        if not isinstance(persisted, dict):
            return
        current = RunRecord.model_validate(persisted)
        run.task_profile = current.task_profile
        run.declared_verification_contract = current.declared_verification_contract
        run.verification_activations = list(current.verification_activations)
        run.completion_request_id = current.completion_request_id
        run.completion_requested_at = current.completion_requested_at
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
            contract=run.declared_verification_contract,
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
            verification_scope="goal_aggregate" if goal is not None else "run",
            supporting_run_ids=list(goal.run_ids) if goal is not None else [],
            goal_revision=run.goal_revision,
            accepted_for_goal_revision=False,
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
            goal = self.goals.apply_run_report(
                goal,
                run,
                report,
                RunOutcome.BUDGET_EXCEEDED,
            )
            run.verification_report = report
            saved = self._sessions.update_terminal_run_verification_report(
                run.session_id,
                run.run_id,
                report.model_dump(mode="json"),
            )
            self._replace_run(run, RunRecord.model_validate(saved))
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
