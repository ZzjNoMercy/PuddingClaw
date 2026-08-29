"""Durable ordinary-Run review orchestration with no Goal write authority."""

from __future__ import annotations

import logging
import time
import uuid
from types import SimpleNamespace
from typing import Any

from deepagents import RubricMiddleware
from deepagents.middleware.rubric import GraderResponse
from langchain.agents import AgentState
from typing_extensions import NotRequired

from graph.verification.environment import EnvironmentVerificationProfile
from graph.verification.models import (
    EvaluationInputSnapshot,
    EvaluationSubjectKind,
    RunReviewReport,
    VerificationCriterionResult,
    VerificationMethod,
    VerificationRecord,
    VerificationRecordStatus,
    stable_digest,
)
from graph.verification.orchestrator import OnlineVerificationOrchestrator
from graph.verification.records import build_verification_record
from graph.verification.report_merger import merge_verification_records
from graph.verification.transcript_projection import project_messages_for_grader
from harness.deterministic_checks import evaluate_deterministic_criteria
from harness.models import RunRecord, RunReviewPolicy, VerificationFailureKind, VerifierKind
from observability import emit_harness_metric

_GRADER_POLICY_VERSION = "ordinary-run-review-deepagents-0.7.11-v1"
logger = logging.getLogger(__name__)


class RunReviewGraderState(AgentState[GraderResponse]):
    evaluation_snapshot_id: NotRequired[str]
    verification_operation_id: NotRequired[str]


def _criterion_payload(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return dict(raw)
    if hasattr(raw, "model_dump"):
        return raw.model_dump(mode="json")
    return {}


class RunReviewOrchestrator:
    """Freeze, verify and persist a review while leaving Run outcome untouched."""

    def __init__(self, sessions: Any) -> None:
        self._sessions = sessions
        self._snapshots = OnlineVerificationOrchestrator(sessions)

    def prepare(
        self,
        *,
        run: RunRecord,
        final_state: dict[str, Any],
        workspace_fingerprint: str | None,
        policy: RunReviewPolicy,
        manual: bool = False,
    ) -> tuple[EvaluationInputSnapshot, dict[str, Any]]:
        if policy == RunReviewPolicy.OFF or (run.run_review_policy != policy and not manual):
            raise ValueError("Run review policy is not enabled for this immutable Run")
        grader_policy = {
            "version": _GRADER_POLICY_VERSION,
            "policy": policy.value,
            "tools": [],
            "max_iterations": 1,
            "manual": manual,
        }
        snapshot: EvaluationInputSnapshot | None = None
        if run.evaluation_snapshot_id:
            existing_snapshot = self._sessions.get_evaluation_snapshot(
                run.session_id,
                run.evaluation_snapshot_id,
            )
            if isinstance(existing_snapshot, dict):
                parsed = EvaluationInputSnapshot.model_validate(existing_snapshot)
                if (
                    parsed.subject.kind == EvaluationSubjectKind.RUN_OUTPUT
                    and parsed.subject.run_id == run.run_id
                    and parsed.grader_policy_hash == stable_digest(grader_policy)
                ):
                    operations = self._sessions.get_harness_state(run.session_id).get(
                        "verification_operations",
                        {},
                    )
                    live_or_recoverable = [
                        item
                        for item in operations.values()
                        if isinstance(item, dict)
                        and item.get("snapshot_id") == parsed.snapshot_id
                        and item.get("method") == VerificationMethod.SEMANTIC_RUBRIC.value
                        and item.get("verifier_policy_hash") == stable_digest(grader_policy)
                        and item.get("status") in {"pending", "running", "completed"}
                    ] if isinstance(operations, dict) else []
                    if live_or_recoverable:
                        return parsed, max(
                            live_or_recoverable,
                            key=lambda item: int(item.get("attempt_no") or 0),
                        )
                    snapshot = parsed
        if snapshot is None:
            snapshot = self._snapshots.freeze_run_snapshot(
                run=run,
                final_state=final_state,
                workspace_fingerprint=workspace_fingerprint,
                grader_policy=grader_policy,
            )
        operation = self._sessions.reserve_verification_operation(
            run.session_id,
            snapshot_id=snapshot.snapshot_id,
            method=VerificationMethod.SEMANTIC_RUBRIC.value,
            verifier_policy_hash=stable_digest(grader_policy),
            operation_metadata={"review_policy": policy.value, "manual": manual},
        )
        return snapshot, operation

    async def execute(
        self,
        *,
        run: RunRecord,
        snapshot: EvaluationInputSnapshot,
        operation: dict[str, Any],
        final_state: dict[str, Any],
        model: Any,
        policy: RunReviewPolicy,
        manual: bool = False,
    ) -> RunReviewReport:
        review_started = time.time()
        contract = run.verification_contract
        if contract is None:
            raise ValueError("Run review contract is missing")
        records: list[VerificationRecord] = []
        snapshot_digest = stable_digest(snapshot.model_dump(mode="json"))

        deterministic = evaluate_deterministic_criteria(contract, final_state)
        deterministic_results = [
            VerificationCriterionResult(
                criterion_id=item.criterion_id,
                name=item.criterion_id,
                passed=item.passed,
                evidence=item.evidence,
                gap=item.gap,
                failure_kind=item.failure_kind.value if item.failure_kind else None,
            )
            for item in deterministic
        ]
        if deterministic_results:
            records.append(
                self._persist_fresh_record(
                    run=run,
                    snapshot=snapshot,
                    snapshot_digest=snapshot_digest,
                    method=VerificationMethod.DETERMINISTIC,
                    status=(
                        VerificationRecordStatus.SATISFIED
                        if all(item.passed is True and not item.gap for item in deterministic_results)
                        else VerificationRecordStatus.INFRASTRUCTURE_ERROR
                        if any(
                            item.failure_kind == VerificationFailureKind.INFRASTRUCTURE_ERROR.value
                            for item in deterministic_results
                        )
                        else VerificationRecordStatus.NEEDS_REVISION
                    ),
                    criteria=deterministic_results,
                    policy={"version": "deterministic-v1", "contract_hash": snapshot.contract_hash},
                )
            )

        raw_review_config = (
            run.config_snapshot.get("completion", {}).get("run_review", {})
            if isinstance(run.config_snapshot, dict)
            else {}
        )
        environment_profile = EnvironmentVerificationProfile(
            raw_review_config.get("environment_profile")
            or EnvironmentVerificationProfile.NONE.value
        )
        if environment_profile not in {
            EnvironmentVerificationProfile.NONE,
            EnvironmentVerificationProfile.DETERMINISTIC_ONLY,
        }:
            environment_record = self._snapshots.run_environment_verifier(
                run=run,
                snapshot=snapshot,
                observations=list(final_state.get("_environment_observations") or []),
                profile=environment_profile,
            )
            if environment_record is not None:
                records.append(environment_record)

        semantic_criteria = [item for item in contract.criteria if item.verifier == VerifierKind.LLM_GRADER]
        if semantic_criteria:
            semantic_record = await self._semantic_record(
                run=run,
                snapshot=snapshot,
                snapshot_digest=snapshot_digest,
                operation=operation,
                final_state=final_state,
                model=model,
                criteria=semantic_criteria,
                policy=policy,
                manual=manual,
            )
            records.append(semantic_record)

        stored_records = [
            VerificationRecord.model_validate(item)
            for item in self._sessions.list_verification_records(
                run.session_id,
                snapshot_id=snapshot.snapshot_id,
            )
        ]
        proposal = merge_verification_records(
            snapshot=snapshot,
            contract=contract,
            records=stored_records,
        )
        report = RunReviewReport(
            report_id=f"run-review-{uuid.uuid4().hex[:20]}",
            run_id=run.run_id,
            snapshot_id=snapshot.snapshot_id,
            policy=policy.value,
            manual=manual,
            status=proposal.status,
            verification_record_ids=proposal.verification_record_ids,
            operation_id=str(operation["operation_id"]),
            attempt_no=int(operation["attempt_no"]),
            summary=proposal.explanation,
            published_before_review=(policy == RunReviewPolicy.SHADOW),
            completed_at=time.time(),
            error_kind=(
                "review_control_error"
                if proposal.status
                in {
                    VerificationRecordStatus.GRADER_ERROR,
                    VerificationRecordStatus.INFRASTRUCTURE_ERROR,
                }
                else None
            ),
        )
        self._sessions.record_run_review_report(
            run.session_id,
            report.model_dump(mode="json"),
        )
        emit_harness_metric(
            logger,
            "run_review_completed_total",
            session_id=run.session_id,
            run_id=run.run_id,
            policy=policy.value,
            manual=manual,
            status=report.status.value,
            attempt_no=report.attempt_no,
            published_before_review=report.published_before_review,
            latency_ms=round((time.time() - review_started) * 1000),
        )
        return report

    async def _semantic_record(
        self,
        *,
        run: RunRecord,
        snapshot: EvaluationInputSnapshot,
        snapshot_digest: str,
        operation: dict[str, Any],
        final_state: dict[str, Any],
        model: Any,
        criteria: list[Any],
        policy: RunReviewPolicy,
        manual: bool,
    ) -> VerificationRecord:
        # If the process died after publishing the immutable semantic record
        # but before publishing the review report, replay only the report
        # merge.  Never call the grader again for the same snapshot/operation.
        existing_records = self._sessions.list_verification_records(
            run.session_id,
            snapshot_id=snapshot.snapshot_id,
        )
        for raw_record in existing_records:
            if (
                isinstance(raw_record, dict)
                and raw_record.get("operation_id") == operation.get("operation_id")
                and raw_record.get("method") == VerificationMethod.SEMANTIC_RUBRIC.value
            ):
                return VerificationRecord.model_validate(raw_record)
        projected = project_messages_for_grader(
            final_state.get("messages") or [],
            run_query_id=run.query_id,
            objective=run.objective,
        )
        middleware = RubricMiddleware(
            model=model,
            tools=[],
            max_iterations=1,
            grader_middleware=[],
            grader_state_schema=RunReviewGraderState,
            prepare_messages_for_grader=lambda messages: list(messages),
            build_grader_state=lambda _state, _iteration: {
                "evaluation_snapshot_id": snapshot.snapshot_id,
                "verification_operation_id": str(operation["operation_id"]),
            },
        )
        started = time.time()
        try:
            update = await middleware.aafter_agent(
                {"messages": projected, "rubric": run.verification_contract.rubric},
                SimpleNamespace(context={"run_id": run.run_id}, stream_writer=None),
            )
            evaluations = update.get("_rubric_evaluations") if isinstance(update, dict) else None
            evaluation = (
                dict(evaluations[-1])
                if isinstance(evaluations, list) and evaluations and isinstance(evaluations[-1], dict)
                else {"result": "grader_error", "criteria": []}
            )
            by_name = {
                name: criterion
                for criterion in criteria
                for name in (criterion.id, criterion.statement)
            }
            seen: set[str] = set()
            results: list[VerificationCriterionResult] = []
            protocol_errors: list[str] = []
            for raw in evaluation.get("criteria") or []:
                item = _criterion_payload(raw)
                name = str(item.get("name") or "")
                criterion = by_name.get(name)
                if criterion is None:
                    protocol_errors.append(f"unknown:{name or '<empty>'}")
                    continue
                if criterion.id in seen:
                    protocol_errors.append(f"duplicate:{criterion.id}")
                    continue
                seen.add(criterion.id)
                results.append(
                    VerificationCriterionResult(
                        criterion_id=criterion.id,
                        name=criterion.id,
                        passed=item.get("passed") if isinstance(item.get("passed"), bool) else None,
                        gap=str(item.get("gap") or "") or None,
                    )
                )
            protocol_errors.extend(f"missing:{item.id}" for item in criteria if item.id not in seen)
            raw_result = str(evaluation.get("result") or "")
            if protocol_errors:
                status = VerificationRecordStatus.GRADER_ERROR
                error_kind = "criterion_identity:" + ",".join(protocol_errors)
            elif raw_result == "satisfied" and all(item.passed is True for item in results):
                status = VerificationRecordStatus.SATISFIED
                error_kind = None
            elif raw_result in {"needs_revision", "failed"}:
                status = (
                    VerificationRecordStatus.NEEDS_REVISION
                    if raw_result == "needs_revision"
                    else VerificationRecordStatus.FAILED
                )
                error_kind = None
            else:
                status = VerificationRecordStatus.GRADER_ERROR
                error_kind = "invalid_grader_result"
        except Exception as exc:  # review errors never rewrite the completed Run
            results = []
            status = VerificationRecordStatus.GRADER_ERROR
            error_kind = f"grader_exception:{type(exc).__name__}"
        grader_policy = {
            "version": _GRADER_POLICY_VERSION,
            "policy": policy.value,
            "tools": [],
            "max_iterations": 1,
            "manual": manual,
        }
        record = build_verification_record(
            snapshot_id=snapshot.snapshot_id,
            snapshot_input_digest=snapshot_digest,
            method=VerificationMethod.SEMANTIC_RUBRIC,
            status=status,
            criteria=results,
            attempt_no=int(operation["attempt_no"]),
            verifier_policy=grader_policy,
            verifier_model=str(getattr(model, "model_name", None) or getattr(model, "model", None) or ""),
            latency_ms=round((time.time() - started) * 1000),
            error_kind=error_kind,
        )
        if record.operation_id != operation["operation_id"]:
            raise ValueError("Run review operation identity drifted")
        self._sessions.record_verification_record(run.session_id, record.model_dump(mode="json"))
        return record

    def _persist_fresh_record(
        self,
        *,
        run: RunRecord,
        snapshot: EvaluationInputSnapshot,
        snapshot_digest: str,
        method: VerificationMethod,
        status: VerificationRecordStatus,
        criteria: list[VerificationCriterionResult],
        policy: dict[str, Any],
    ) -> VerificationRecord:
        operation = self._sessions.reserve_verification_operation(
            run.session_id,
            snapshot_id=snapshot.snapshot_id,
            method=method.value,
            verifier_policy_hash=stable_digest(policy),
        )
        record = build_verification_record(
            snapshot_id=snapshot.snapshot_id,
            snapshot_input_digest=snapshot_digest,
            method=method,
            status=status,
            criteria=criteria,
            attempt_no=int(operation["attempt_no"]),
            verifier_policy=policy,
            error_kind=(
                "deterministic_infrastructure"
                if status == VerificationRecordStatus.INFRASTRUCTURE_ERROR
                else None
            ),
        )
        if record.operation_id != operation["operation_id"]:
            raise ValueError("Run review operation identity drifted")
        self._sessions.record_verification_record(run.session_id, record.model_dump(mode="json"))
        return record
