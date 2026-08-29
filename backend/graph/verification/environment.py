"""Receipt-only environment verification.

Environment observation is executed by a sandbox/read-only backend outside this
adapter. This module accepts no Python callbacks or mutation tools; it only
validates immutable observations bound to the frozen snapshot.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field, model_validator

from graph.verification.models import (
    EvaluationInputSnapshot,
    VerificationCriterionResult,
    VerificationMethod,
    VerificationRecord,
    VerificationRecordStatus,
    stable_digest,
)
from graph.verification.records import build_verification_record
from harness.models import RunVerificationContract, VerifierKind


class EnvironmentVerificationProfile(StrEnum):
    NONE = "none"
    DETERMINISTIC_ONLY = "deterministic_only"
    INDEPENDENT_EVIDENCE_REVIEW = "independent_evidence_review"
    ENVIRONMENT_VERIFIED = "environment_verified"


class EnvironmentObservation(BaseModel):
    """Immutable output of an independently enforced read-only capability."""

    criterion_id: str = Field(min_length=1)
    snapshot_id: str = Field(min_length=1)
    input_digest: str = Field(min_length=1)
    passed: bool | None
    evidence: list[dict[str, Any]] = Field(default_factory=list)
    tool_receipt_ids: list[str] = Field(default_factory=list)
    gap: str | None = None
    error_kind: str | None = None
    capability_profile: str = Field(min_length=1)
    read_only_enforced: bool = False

    @model_validator(mode="after")
    def require_enforced_read_only_boundary(self) -> EnvironmentObservation:
        if not self.read_only_enforced:
            raise ValueError("Environment observation lacks an enforced read-only capability boundary")
        return self


class EnvironmentVerifier:
    """Validate receipt-bound observations; never execute application callbacks."""

    def verify(
        self,
        *,
        snapshot: EvaluationInputSnapshot,
        contract: RunVerificationContract,
        context: dict[str, Any],
        profile: EnvironmentVerificationProfile,
        attempt_no: int = 0,
    ) -> VerificationRecord | None:
        criteria = [
            item
            for item in contract.criteria
            if item.verifier in {VerifierKind.ENVIRONMENT, VerifierKind.ANALYTICS}
        ]
        if not criteria or profile in {
            EnvironmentVerificationProfile.NONE,
            EnvironmentVerificationProfile.DETERMINISTIC_ONLY,
        }:
            return None

        expected_digest = stable_digest(snapshot.model_dump(mode="json"))
        raw_observations = context.get("observations")
        observations: dict[str, EnvironmentObservation] = {}
        protocol_errors: list[str] = []
        for raw in raw_observations if isinstance(raw_observations, list) else []:
            try:
                observation = EnvironmentObservation.model_validate(raw)
            except Exception as exc:
                protocol_errors.append(f"invalid_observation:{type(exc).__name__}")
                continue
            if observation.snapshot_id != snapshot.snapshot_id or observation.input_digest != expected_digest:
                protocol_errors.append(f"stale_observation:{observation.criterion_id}")
                continue
            if observation.criterion_id in observations:
                protocol_errors.append(f"duplicate_observation:{observation.criterion_id}")
                continue
            observations[observation.criterion_id] = observation

        results: list[VerificationCriterionResult] = []
        infrastructure_error = bool(protocol_errors)
        receipt_ids: set[str] = set()
        for criterion in criteria:
            observation = observations.get(criterion.id)
            if observation is None:
                results.append(
                    VerificationCriterionResult(
                        criterion_id=criterion.id,
                        name=criterion.id,
                        passed=None,
                        failure_kind="environment_observation_missing",
                    )
                )
                infrastructure_error = True
                continue
            strict_missing_receipt = (
                profile == EnvironmentVerificationProfile.ENVIRONMENT_VERIFIED
                and not observation.tool_receipt_ids
            )
            if strict_missing_receipt:
                results.append(
                    VerificationCriterionResult(
                        criterion_id=criterion.id,
                        name=criterion.id,
                        passed=None,
                        failure_kind="environment_receipt_missing",
                    )
                )
                infrastructure_error = True
                continue
            receipt_ids.update(observation.tool_receipt_ids)
            results.append(
                VerificationCriterionResult(
                    criterion_id=criterion.id,
                    name=criterion.id,
                    passed=observation.passed,
                    evidence=observation.evidence,
                    gap=observation.gap,
                    failure_kind=observation.error_kind,
                )
            )

        if infrastructure_error:
            status = VerificationRecordStatus.INFRASTRUCTURE_ERROR
            error_kind = "environment_observation_protocol:" + ",".join(protocol_errors or ["missing"])
        elif any(item.passed is not True or item.gap for item in results):
            status = VerificationRecordStatus.NEEDS_REVISION
            error_kind = None
        else:
            status = VerificationRecordStatus.SATISFIED
            error_kind = None
        return build_verification_record(
            snapshot_id=snapshot.snapshot_id,
            snapshot_input_digest=expected_digest,
            method=VerificationMethod.ENVIRONMENT,
            status=status,
            criteria=results,
            attempt_no=attempt_no,
            verifier_policy={
                "version": "environment-receipts-v1",
                "profile": profile.value,
                "callback_execution": False,
            },
            tool_receipt_ids=sorted(receipt_ids),
            error_kind=error_kind,
        )

