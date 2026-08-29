"""Fail-closed merge of verifier-owned criterion results."""

from __future__ import annotations

import time
import uuid
from typing import Any

from graph.verification.models import (
    EvaluationInputSnapshot,
    VerificationCriterionResult,
    VerificationInvalidation,
    VerificationMethod,
    VerificationProposal,
    VerificationRecord,
    VerificationRecordStatus,
    stable_digest,
)
from harness.models import (
    CriterionEvaluation,
    RubricEvaluationReport,
    RunVerificationContract,
    VerificationFailureKind,
    VerificationStatus,
    VerifierKind,
)

_OWNER_METHODS = {
    VerifierKind.DETERMINISTIC: {VerificationMethod.DETERMINISTIC},
    VerifierKind.ENVIRONMENT: {VerificationMethod.ENVIRONMENT},
    VerifierKind.ANALYTICS: {VerificationMethod.ENVIRONMENT},
    VerifierKind.LLM_GRADER: {VerificationMethod.SEMANTIC_RUBRIC},
}


def merge_verification_records(
    *,
    snapshot: EvaluationInputSnapshot,
    contract: RunVerificationContract,
    records: list[VerificationRecord],
    invalidations: list[VerificationInvalidation] | None = None,
) -> VerificationProposal:
    invalidated_ids = {item.verification_id for item in invalidations or []}
    expected_input_digest = stable_digest(snapshot.model_dump(mode="json"))
    candidates_by_method: dict[VerificationMethod, list[VerificationRecord]] = {}
    control_errors: list[VerificationRecord] = []
    for record in records:
        if record.snapshot_id != snapshot.snapshot_id:
            raise ValueError("Cannot merge verification records from different snapshots")
        if record.input_digest != expected_input_digest:
            raise ValueError("Verification record input digest does not match snapshot")
        if record.verification_id in invalidated_ids:
            continue
        candidates_by_method.setdefault(record.method, []).append(record)
    active: list[VerificationRecord] = []
    flow_pending = False
    for method_records in candidates_by_method.values():
        latest = max(
            method_records,
            key=lambda item: (item.attempt_no, item.completed_at or item.started_at),
        )
        if latest.status in {
            VerificationRecordStatus.PENDING,
            VerificationRecordStatus.RUNNING,
        }:
            flow_pending = True
            continue
        active.append(latest)
        if latest.status in {
            VerificationRecordStatus.GRADER_ERROR,
            VerificationRecordStatus.INFRASTRUCTURE_ERROR,
        }:
            control_errors.append(latest)

    results_by_id: dict[str, list[tuple[VerificationMethod, VerificationCriterionResult]]] = {}
    for record in active:
        for result in record.criteria:
            results_by_id.setdefault(result.criterion_id, []).append((record.method, result))

    evaluations: list[VerificationCriterionResult] = []
    gaps: list[str] = []
    for criterion in contract.criteria:
        candidates = [
            pair
            for pair in results_by_id.get(criterion.id, [])
            if pair[0] in _OWNER_METHODS[criterion.verifier]
        ]
        foreign = [
            pair
            for pair in results_by_id.get(criterion.id, [])
            if pair[0] not in _OWNER_METHODS[criterion.verifier]
        ]
        if foreign:
            gap = f"标准 {criterion.id} 被非权威 verifier 返回，结果已拒绝。"
            result = VerificationCriterionResult(
                criterion_id=criterion.id,
                name=criterion.id,
                passed=False,
                gap=gap,
                failure_kind="verifier_ownership_conflict",
            )
        elif len(candidates) != 1:
            gap = (
                f"必需标准 {criterion.id} 没有权威判定。"
                if not candidates
                else f"标准 {criterion.id} 存在重复权威判定。"
            )
            result = VerificationCriterionResult(
                criterion_id=criterion.id,
                name=criterion.id,
                passed=False if criterion.required else None,
                gap=gap,
                failure_kind="criterion_coverage_error",
            )
        else:
            result = candidates[0][1]
        evaluations.append(result)
        if criterion.required and (result.passed is not True or result.gap):
            gaps.append(result.gap or f"标准 {criterion.id} 未通过。")

    known_ids = {item.id for item in contract.criteria}
    foreign_ids = sorted(set(results_by_id) - known_ids)
    if foreign_ids:
        gaps.append("Verifier 返回契约外标准：" + "、".join(foreign_ids))

    if flow_pending:
        status = VerificationRecordStatus.NOT_EVALUATED
        explanation = "验收仍在执行，尚未形成可结算的终态。"
    elif any(item.status == VerificationRecordStatus.INFRASTRUCTURE_ERROR for item in control_errors):
        status = VerificationRecordStatus.INFRASTRUCTURE_ERROR
        explanation = "验收基础设施异常，未将其解释为任务不合格。"
    elif any(item.status == VerificationRecordStatus.GRADER_ERROR for item in control_errors) or foreign_ids:
        status = VerificationRecordStatus.GRADER_ERROR
        explanation = "语义 grader 协议异常，未形成业务裁决。"
    elif gaps:
        status = VerificationRecordStatus.NEEDS_REVISION
        explanation = "验收未通过：" + "；".join(dict.fromkeys(gaps))
    else:
        status = VerificationRecordStatus.SATISFIED
        explanation = "所有必需标准均由其权威 verifier 判定通过。"
    payload: dict[str, Any] = {
        "proposal_id": f"verification-proposal-{uuid.uuid4().hex[:20]}",
        "snapshot_id": snapshot.snapshot_id,
        "run_id": snapshot.subject.run_id,
        "status": status,
        "verification_record_ids": [item.verification_id for item in active],
        "evaluations": evaluations,
        "gaps": list(dict.fromkeys(gaps)),
        "explanation": explanation,
        "created_at": time.time(),
    }
    provisional = VerificationProposal.model_construct(**payload, proposal_digest="pending")
    payload["proposal_digest"] = stable_digest(
        provisional.model_dump(mode="json", exclude={"proposal_digest"}, exclude_none=True)
    )
    return VerificationProposal.model_validate(payload)


def proposal_to_rubric_report(
    proposal: VerificationProposal,
    *,
    contract: RunVerificationContract,
    goal_revision: int | None,
) -> RubricEvaluationReport:
    status_map = {
        VerificationRecordStatus.SATISFIED: VerificationStatus.SATISFIED,
        VerificationRecordStatus.NEEDS_REVISION: VerificationStatus.NEEDS_REVISION,
        VerificationRecordStatus.FAILED: VerificationStatus.FAILED,
        VerificationRecordStatus.GRADER_ERROR: VerificationStatus.GRADER_ERROR,
        VerificationRecordStatus.INFRASTRUCTURE_ERROR: VerificationStatus.INFRASTRUCTURE_ERROR,
    }
    return RubricEvaluationReport(
        report_id=proposal.proposal_id,
        run_id=proposal.run_id,
        status=status_map.get(proposal.status, VerificationStatus.INCOMPLETE),
        contract_id=contract.contract_id,
        contract_version=contract.version,
        evaluations=[
            CriterionEvaluation(
                criterion_id=item.criterion_id,
                name=item.name,
                passed=item.passed,
                verifier=next(
                    criterion.verifier
                    for criterion in contract.criteria
                    if criterion.id == item.criterion_id
                ),
                evidence=item.evidence,
                gap=item.gap,
                failure_kind=(
                    VerificationFailureKind(item.failure_kind)
                    if item.failure_kind in {value.value for value in VerificationFailureKind}
                    else None
                ),
            )
            for item in proposal.evaluations
        ],
        gaps=proposal.gaps,
        explanation=proposal.explanation,
        goal_revision=goal_revision,
        accepted_for_goal_revision=None,
        snapshot_id=proposal.snapshot_id,
        verification_record_ids=proposal.verification_record_ids,
        source_format="verification_records_v1",
    )
