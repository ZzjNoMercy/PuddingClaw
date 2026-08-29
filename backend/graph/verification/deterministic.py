"""Adapter from registered deterministic checks to immutable records."""

from __future__ import annotations

from typing import Any

from graph.verification.models import (
    EvaluationInputSnapshot,
    VerificationCriterionResult,
    VerificationMethod,
    VerificationRecord,
    VerificationRecordStatus,
    stable_digest,
)
from graph.verification.records import build_verification_record
from harness.deterministic_checks import evaluate_deterministic_criteria
from harness.models import RunVerificationContract, VerificationFailureKind


def run_deterministic_verifier(
    *,
    snapshot: EvaluationInputSnapshot,
    contract: RunVerificationContract,
    final_state: dict[str, Any],
    attempt_no: int = 0,
) -> VerificationRecord | None:
    evaluations = evaluate_deterministic_criteria(contract, final_state)
    if not evaluations:
        return None
    criteria = [
        VerificationCriterionResult(
            criterion_id=item.criterion_id,
            name=item.name,
            passed=item.passed,
            evidence=item.evidence,
            gap=item.gap,
            failure_kind=item.failure_kind.value if item.failure_kind else None,
        )
        for item in evaluations
    ]
    if any(item.failure_kind == VerificationFailureKind.INFRASTRUCTURE_ERROR for item in evaluations):
        status = VerificationRecordStatus.INFRASTRUCTURE_ERROR
        error_kind = "deterministic_verifier_infrastructure"
    elif any(item.passed is not True for item in evaluations):
        status = VerificationRecordStatus.NEEDS_REVISION
        error_kind = None
    else:
        status = VerificationRecordStatus.SATISFIED
        error_kind = None
    return build_verification_record(
        snapshot_id=snapshot.snapshot_id,
        snapshot_input_digest=stable_digest(snapshot.model_dump(mode="json")),
        method=VerificationMethod.DETERMINISTIC,
        status=status,
        criteria=criteria,
        attempt_no=attempt_no,
        verifier_policy={"version": "deterministic-v1", "contract_hash": snapshot.contract_hash},
        error_kind=error_kind,
    )
