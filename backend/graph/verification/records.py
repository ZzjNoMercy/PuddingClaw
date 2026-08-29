"""Factories for immutable, content-addressed verification records."""

from __future__ import annotations

import time
import uuid
from typing import Any

from graph.verification.models import (
    VerificationCriterionResult,
    VerificationMethod,
    VerificationRecord,
    VerificationRecordStatus,
    stable_digest,
    verification_operation_id,
)
from harness.evidence_ledger import EvidenceRef


def build_verification_record(
    *,
    snapshot_id: str,
    snapshot_input_digest: str,
    method: VerificationMethod,
    status: VerificationRecordStatus,
    criteria: list[VerificationCriterionResult],
    attempt_no: int,
    verifier_policy: dict[str, Any],
    evidence_refs: list[EvidenceRef] | None = None,
    verifier_model: str | None = None,
    tool_receipt_ids: list[str] | None = None,
    latency_ms: int | None = None,
    usage: dict[str, Any] | None = None,
    error_kind: str | None = None,
    started_at: float | None = None,
) -> VerificationRecord:
    policy_hash = stable_digest(verifier_policy)
    operation_id = verification_operation_id(snapshot_id, method, attempt_no, policy_hash)
    completed_at = time.time()
    payload: dict[str, Any] = {
        "verification_id": f"verification-{uuid.uuid4().hex[:20]}",
        "snapshot_id": snapshot_id,
        "method": method,
        "status": status,
        "criteria": criteria,
        "evidence_refs": evidence_refs or [],
        "operation_id": operation_id,
        "attempt_no": attempt_no,
        "input_digest": snapshot_input_digest,
        "verifier_model": verifier_model,
        "verifier_policy_version": str(verifier_policy.get("version") or "1"),
        "verifier_policy_hash": policy_hash,
        "tool_receipt_ids": tool_receipt_ids or [],
        "latency_ms": latency_ms,
        "usage": usage or {},
        "error_kind": error_kind,
        "started_at": started_at if started_at is not None else completed_at,
        "completed_at": completed_at,
    }
    provisional = VerificationRecord.model_construct(**payload, result_digest="pending")
    payload["result_digest"] = stable_digest(
        provisional.model_dump(mode="json", exclude={"result_digest"}, exclude_none=True)
    )
    return VerificationRecord.model_validate(payload)
