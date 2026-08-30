"""Application-owned orchestration between verifier engines and settlement."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from graph.verification.environment import (
    EnvironmentVerificationProfile,
    EnvironmentVerifier,
)
from graph.verification.models import (
    ArtifactFingerprint,
    EvaluationInputSnapshot,
    EvaluationSubject,
    EvaluationSubjectKind,
    EvidenceBinding,
    VerificationCriterionResult,
    VerificationInvalidation,
    VerificationMethod,
    VerificationRecord,
    VerificationRecordStatus,
    stable_digest,
)
from graph.verification.records import build_verification_record
from graph.verification.report_merger import (
    merge_verification_records,
    proposal_to_rubric_report,
)
from graph.verification.snapshots import build_evaluation_snapshot
from graph.verification.transcript_projection import (
    candidate_from_projected_messages,
    project_messages_for_grader,
)
from harness.evidence_ledger import is_evidence_ref
from harness.models import (
    GoalRecord,
    RubricEvaluationReport,
    RunRecord,
    VerificationFailureKind,
    VerificationStatus,
    VerifierKind,
)


def _walk_dicts(value: Any) -> Iterable[dict[str, Any]]:
    if isinstance(value, dict):
        yield value
        for item in value.values():
            yield from _walk_dicts(item)
    elif isinstance(value, list):
        for item in value:
            yield from _walk_dicts(item)


def artifact_fingerprints_from_run(run: RunRecord) -> list[ArtifactFingerprint]:
    by_id: dict[str, ArtifactFingerprint] = {}
    for item in _walk_dicts(
        [
            activation.model_dump(mode="json")
            if hasattr(activation, "model_dump")
            else activation
            for activation in run.verification_activations
        ]
    ):
        artifact_id = str(item.get("artifact_id") or "")
        content_sha256 = str(item.get("content_sha256") or "")
        if not artifact_id or not content_sha256:
            continue
        by_id[artifact_id] = ArtifactFingerprint(
            artifact_id=artifact_id,
            content_sha256=content_sha256,
            scope=str(item.get("scope") or "") or None,
            path=str(item.get("host_path") or item.get("path") or "") or None,
            workspace_relative_path=str(item.get("workspace_relative_path") or "") or None,
            mtime_ns=item.get("mtime_ns") if isinstance(item.get("mtime_ns"), int) else None,
            size_bytes=item.get("size_bytes") if isinstance(item.get("size_bytes"), int) else None,
            version_token=str(item.get("version_token") or "") or None,
            source_receipt_id=str(
                item.get("mutation_receipt_id")
                or item.get("validation_receipt_id")
                or item.get("receipt_id")
                or ""
            )
            or None,
            workspace_id=str(item.get("workspace_id") or "") or None,
            backend_id=str(item.get("backend_id") or "") or None,
            permission_grant_id=str(item.get("permission_grant_id") or "") or None,
        )
    return sorted(by_id.values(), key=lambda item: item.artifact_id)


def _record_status(
    criteria: list[VerificationCriterionResult],
    *,
    overall: VerificationStatus,
    method: VerificationMethod,
) -> tuple[VerificationRecordStatus, str | None]:
    if overall == VerificationStatus.INFRASTRUCTURE_ERROR:
        return VerificationRecordStatus.INFRASTRUCTURE_ERROR, f"{method.value}_infrastructure"
    if method == VerificationMethod.SEMANTIC_RUBRIC and overall == VerificationStatus.GRADER_ERROR:
        return VerificationRecordStatus.GRADER_ERROR, "semantic_grader_protocol"
    if any(item.failure_kind == VerificationFailureKind.INFRASTRUCTURE_ERROR.value for item in criteria):
        return VerificationRecordStatus.INFRASTRUCTURE_ERROR, f"{method.value}_infrastructure"
    if any(item.passed is not True or item.gap for item in criteria):
        return VerificationRecordStatus.NEEDS_REVISION, None
    return VerificationRecordStatus.SATISFIED, None


class OnlineVerificationOrchestrator:
    """Builds immutable evidence and proposals; never transitions a Goal."""

    def __init__(self, sessions: Any) -> None:
        self._sessions = sessions

    def _evidence_bindings(self, run: RunRecord, goal: GoalRecord) -> list[EvidenceBinding]:
        state = self._sessions.get_harness_state(run.session_id)
        raw_goal = state.get("goals", {}).get(goal.goal_id, {})
        bindings: list[EvidenceBinding] = []
        for raw_ref in raw_goal.get("evidence_refs") or []:
            resolved = self._sessions.resolve_evidence_ref(
                run.session_id,
                raw_ref,
                goal_id=goal.goal_id,
                goal_revision=goal.objective_revision,
                allow_artifact_revision_inheritance=True,
            )
            if not isinstance(resolved, dict):
                raise ValueError("Goal evidence became stale before evaluation snapshot freeze")
            bindings.append(
                EvidenceBinding(
                    ref=raw_ref,
                    record_digest=stable_digest(resolved),
                )
            )
        return bindings

    def _run_evidence_bindings(self, run: RunRecord) -> list[EvidenceBinding]:
        refs: list[dict[str, Any]] = []
        seen: set[str] = set()
        for activation in run.verification_activations:
            payload = (
                activation.model_dump(mode="json")
                if hasattr(activation, "model_dump")
                else activation
            )
            if not isinstance(payload, dict):
                continue
            for raw_ref in [
                *(payload.get("stable_evidence_refs") or []),
                *(payload.get("evidence_refs") or []),
            ]:
                if not is_evidence_ref(raw_ref):
                    continue
                key = stable_digest(raw_ref)
                if key in seen:
                    continue
                seen.add(key)
                refs.append(raw_ref)
        bindings: list[EvidenceBinding] = []
        for raw_ref in refs:
            resolved = self._sessions.resolve_evidence_ref(
                run.session_id,
                raw_ref,
                require_inheritable=False,
            )
            if not isinstance(resolved, dict):
                raise ValueError("Run evidence became stale before review snapshot freeze")
            bindings.append(EvidenceBinding(ref=raw_ref, record_digest=stable_digest(resolved)))
        return bindings

    def freeze_run_snapshot(
        self,
        *,
        run: RunRecord,
        final_state: dict[str, Any],
        workspace_fingerprint: str | None,
        grader_policy: dict[str, Any],
        permission_epoch: int = 1,
    ) -> EvaluationInputSnapshot:
        """Freeze an ordinary Run output without creating Goal authority."""

        if run.goal_id is not None or run.completion_request_id is not None:
            raise ValueError("Ordinary Run review cannot carry Goal completion authority")
        if run.verification_contract is None:
            raise ValueError("Ordinary Run review requires a frozen verification contract")
        subject = EvaluationSubject(
            kind=EvaluationSubjectKind.RUN_OUTPUT,
            session_id=run.session_id,
            run_id=run.run_id,
            query_id=run.query_id,
        )
        projected = project_messages_for_grader(
            final_state.get("messages") or [],
            run_query_id=run.query_id,
            objective=run.objective,
        )
        candidate_content, candidate_tool_calls = candidate_from_projected_messages(projected)
        snapshot = build_evaluation_snapshot(
            subject=subject,
            contract=run.verification_contract.model_dump(mode="json"),
            transcript_projection=projected,
            candidate_message_id=run.query_id,
            candidate_content=candidate_content,
            candidate_tool_calls=candidate_tool_calls,
            evidence_bindings=self._run_evidence_bindings(run),
            artifact_fingerprints=artifact_fingerprints_from_run(run),
            workspace_fingerprint=workspace_fingerprint,
            grader_policy=grader_policy,
            permission_epoch=permission_epoch,
        )
        saved_snapshot = self._sessions.freeze_evaluation_snapshot(
            run.session_id,
            snapshot.model_dump(mode="json"),
        )
        snapshot = EvaluationInputSnapshot.model_validate(saved_snapshot)
        return snapshot

    def freeze_goal_snapshot(
        self,
        *,
        run: RunRecord,
        goal: GoalRecord,
        final_state: dict[str, Any],
        workspace_fingerprint: str | None,
        permission_epoch: int = 1,
    ) -> EvaluationInputSnapshot:
        """Freeze server-owned candidate state before any verifier runs."""

        if run.verification_contract is None or not run.completion_request_id:
            raise ValueError("Goal verification snapshot requires contract and completion request")
        subject = EvaluationSubject(
            kind=EvaluationSubjectKind.GOAL_COMPLETION_REQUEST,
            session_id=run.session_id,
            run_id=run.run_id,
            query_id=run.query_id,
            goal_id=goal.goal_id,
            goal_revision=goal.objective_revision,
            completion_request_id=run.completion_request_id,
        )
        projected = project_messages_for_grader(
            final_state.get("messages") or [],
            run_query_id=run.query_id,
            objective=run.objective,
        )
        candidate_content, candidate_tool_calls = candidate_from_projected_messages(projected)
        snapshot = build_evaluation_snapshot(
            subject=subject,
            contract=run.verification_contract.model_dump(mode="json"),
            transcript_projection=projected,
            candidate_message_id=run.query_id,
            candidate_content=candidate_content,
            candidate_tool_calls=candidate_tool_calls,
            evidence_bindings=self._evidence_bindings(run, goal),
            artifact_fingerprints=artifact_fingerprints_from_run(run),
            workspace_fingerprint=workspace_fingerprint,
            grader_policy={"version": "deepagents-0.7.11-hooks-v1"},
            permission_epoch=permission_epoch,
        )
        saved_snapshot = self._sessions.freeze_evaluation_snapshot(
            run.session_id,
            snapshot.model_dump(mode="json"),
        )
        snapshot = EvaluationInputSnapshot.model_validate(saved_snapshot)
        run.evaluation_snapshot_id = snapshot.snapshot_id
        return snapshot

    @staticmethod
    def _criterion_payload(raw: Any) -> dict[str, Any]:
        if isinstance(raw, dict):
            return dict(raw)
        if hasattr(raw, "model_dump"):
            return raw.model_dump(mode="json")
        return {}

    def materialize_goal_proposal_from_verifiers(
        self,
        *,
        run: RunRecord,
        goal: GoalRecord,
        snapshot: Any,
        deterministic_evaluations: list[dict[str, Any]],
        semantic_evaluation: dict[str, Any] | None,
    ) -> RubricEvaluationReport:
        """Persist records produced after the frozen snapshot, then merge them."""

        contract = run.verification_contract
        if contract is None:
            raise ValueError("Verification records require a frozen contract")
        snapshot_digest = stable_digest(snapshot.model_dump(mode="json"))
        records: list[VerificationRecord] = []

        deterministic_ids = {
            item.id for item in contract.criteria if item.verifier == VerifierKind.DETERMINISTIC
        }
        deterministic_results = [
            VerificationCriterionResult(
                criterion_id=str(item.get("criterion_id") or ""),
                name=str(item.get("name") or item.get("criterion_id") or ""),
                passed=item.get("passed") if isinstance(item.get("passed"), bool) else None,
                evidence=[value for value in item.get("evidence") or [] if isinstance(value, dict)],
                gap=str(item.get("gap") or "") or None,
                failure_kind=str(item.get("failure_kind") or "") or None,
            )
            for item in deterministic_evaluations
            if isinstance(item, dict) and str(item.get("criterion_id") or "") in deterministic_ids
        ]
        if deterministic_results:
            status, error_kind = _record_status(
                deterministic_results,
                overall=VerificationStatus.SATISFIED,
                method=VerificationMethod.DETERMINISTIC,
            )
            records.append(
                self._persist_record(
                    run=run,
                    snapshot=snapshot,
                    snapshot_digest=snapshot_digest,
                    method=VerificationMethod.DETERMINISTIC,
                    status=status,
                    criteria=deterministic_results,
                    policy={"version": "deterministic-v1", "contract_hash": snapshot.contract_hash},
                    error_kind=error_kind,
                )
            )

        semantic_contract = [
            item for item in contract.criteria if item.verifier == VerifierKind.LLM_GRADER
        ]
        if semantic_contract and semantic_evaluation is not None:
            by_name = {
                name: criterion
                for criterion in semantic_contract
                for name in (criterion.id, criterion.statement)
            }
            seen: set[str] = set()
            semantic_results: list[VerificationCriterionResult] = []
            protocol_errors: list[str] = []
            for raw in semantic_evaluation.get("criteria") or []:
                item = self._criterion_payload(raw)
                name = str(item.get("name") or "")
                criterion = by_name.get(name)
                if criterion is None:
                    protocol_errors.append(f"unknown:{name or '<empty>'}")
                    continue
                if criterion.id in seen:
                    protocol_errors.append(f"duplicate:{criterion.id}")
                    continue
                seen.add(criterion.id)
                semantic_results.append(
                    VerificationCriterionResult(
                        criterion_id=criterion.id,
                        name=criterion.id,
                        passed=item.get("passed") if isinstance(item.get("passed"), bool) else None,
                        gap=str(item.get("gap") or "") or None,
                    )
                )
            missing = [item.id for item in semantic_contract if item.id not in seen]
            protocol_errors.extend(f"missing:{item}" for item in missing)
            raw_result = str(semantic_evaluation.get("result") or "")
            if raw_result == "grader_error":
                status = VerificationRecordStatus.GRADER_ERROR
                explanation = str(semantic_evaluation.get("explanation") or "")
                raised_detail = explanation.partition("Grader raised ")[2].split(maxsplit=1)
                raised_type = raised_detail[0].rstrip(":") if raised_detail else "unknown"
                error_kind = f"grader_runtime:{raised_type or 'unknown'}"
                semantic_results = []
            elif protocol_errors:
                status = VerificationRecordStatus.GRADER_ERROR
                error_kind = "criterion_identity:" + ",".join(protocol_errors)
            elif raw_result == "satisfied" and all(item.passed is True for item in semantic_results):
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
            records.append(
                self._persist_record(
                    run=run,
                    snapshot=snapshot,
                    snapshot_digest=snapshot_digest,
                    method=VerificationMethod.SEMANTIC_RUBRIC,
                    status=status,
                    criteria=semantic_results,
                    policy={"version": "deepagents-0.7.11-hooks-v1", "contract_hash": snapshot.contract_hash},
                    error_kind=error_kind,
                )
            )

        environment_criteria = [
            item
            for item in contract.criteria
            if item.verifier in {VerifierKind.ENVIRONMENT, VerifierKind.ANALYTICS}
        ]
        existing_environment = any(
            item.get("method") == VerificationMethod.ENVIRONMENT.value
            for item in self._sessions.list_verification_records(
                run.session_id,
                snapshot_id=snapshot.snapshot_id,
            )
            if isinstance(item, dict)
        )
        if environment_criteria and not existing_environment:
            environment_results = [
                VerificationCriterionResult(
                    criterion_id=item.id,
                    name=item.id,
                    passed=None,
                    failure_kind="environment_verifier_not_run",
                )
                for item in environment_criteria
            ]
            records.append(
                self._persist_record(
                    run=run,
                    snapshot=snapshot,
                    snapshot_digest=snapshot_digest,
                    method=VerificationMethod.ENVIRONMENT,
                    status=VerificationRecordStatus.NOT_EVALUATED,
                    criteria=environment_results,
                    policy={"version": "environment-v1", "profile": "none"},
                )
            )

        stored_records = [
            VerificationRecord.model_validate(item)
            for item in self._sessions.list_verification_records(
                run.session_id, snapshot_id=snapshot.snapshot_id
            )
        ]
        invalidations = [
            VerificationInvalidation.model_validate(item)
            for item in self._sessions.list_verification_invalidations(
                run.session_id, snapshot_id=snapshot.snapshot_id
            )
        ]
        proposal = merge_verification_records(
            snapshot=snapshot,
            contract=contract,
            records=stored_records,
            invalidations=invalidations,
        )
        self._sessions.record_verification_proposal(
            run.session_id, proposal.model_dump(mode="json")
        )
        report = proposal_to_rubric_report(
            proposal,
            contract=contract,
            goal_revision=goal.objective_revision,
        )
        report.verification_scope = "goal_aggregate"
        report.supporting_run_ids = list(dict.fromkeys([*goal.run_ids, run.run_id]))
        self._sessions.update_goal_completion_request_status(
            run.session_id,
            run.completion_request_id or "",
            "evaluating",
            verification_report_id=report.report_id,
        )
        return report

    def run_environment_verifier(
        self,
        *,
        run: RunRecord,
        snapshot: EvaluationInputSnapshot,
        observations: list[dict[str, Any]],
        profile: EnvironmentVerificationProfile,
    ) -> VerificationRecord | None:
        """Persist a receipt-only environment record for the frozen snapshot."""

        contract = run.verification_contract
        if contract is None:
            raise ValueError("Environment verification requires a contract")
        policy = {
            "version": "environment-receipts-v1",
            "profile": profile.value,
            "callback_execution": False,
        }
        operation = self._sessions.reserve_verification_operation(
            run.session_id,
            snapshot_id=snapshot.snapshot_id,
            method=VerificationMethod.ENVIRONMENT.value,
            verifier_policy_hash=stable_digest(policy),
        )
        record = EnvironmentVerifier().verify(
            snapshot=snapshot,
            contract=contract,
            context={"observations": observations},
            profile=profile,
            attempt_no=int(operation["attempt_no"]),
        )
        if record is None:
            return None
        if record.operation_id != operation["operation_id"]:
            raise ValueError("Environment verification operation identity drifted")
        self._sessions.record_verification_record(
            run.session_id,
            record.model_dump(mode="json"),
        )
        return record

    def _persist_record(
        self,
        *,
        run: RunRecord,
        snapshot: Any,
        snapshot_digest: str,
        method: VerificationMethod,
        status: VerificationRecordStatus,
        criteria: list[VerificationCriterionResult],
        policy: dict[str, Any],
        error_kind: str | None = None,
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
            evidence_refs=list(snapshot.evidence_refs),
            error_kind=error_kind,
        )
        if record.operation_id != operation["operation_id"]:
            raise ValueError("Reserved verification operation identity drifted")
        self._sessions.record_verification_record(
            run.session_id, record.model_dump(mode="json")
        )
        return record
