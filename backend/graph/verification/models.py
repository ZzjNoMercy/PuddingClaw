"""Stable schemas for online verification inputs and results."""

from __future__ import annotations

import hashlib
import json
import time
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator

from harness.evidence_ledger import EvidenceRef


def stable_digest(value: Any) -> str:
    """Return a deterministic digest for a JSON-compatible value."""

    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"


class EvaluationSubjectKind(StrEnum):
    RUN_OUTPUT = "run_output"
    GOAL_COMPLETION_REQUEST = "goal_completion_request"


class EvaluationSubject(BaseModel):
    kind: EvaluationSubjectKind
    session_id: str = Field(min_length=1)
    run_id: str = Field(min_length=1)
    query_id: str = Field(min_length=1)
    goal_id: str | None = None
    goal_revision: int | None = Field(default=None, ge=1)
    completion_request_id: str | None = None

    @model_validator(mode="after")
    def validate_authority(self) -> EvaluationSubject:
        if self.kind == EvaluationSubjectKind.GOAL_COMPLETION_REQUEST:
            if not self.goal_id or self.goal_revision is None or not self.completion_request_id:
                raise ValueError("Goal completion evaluation requires goal, revision and completion request identity")
        elif any((self.goal_id, self.goal_revision, self.completion_request_id)):
            raise ValueError("Ordinary Run output evaluation cannot carry Goal completion authority")
        return self


class ArtifactFingerprint(BaseModel):
    artifact_id: str = Field(min_length=1)
    content_sha256: str = Field(min_length=1)
    scope: str | None = None
    path: str | None = None
    workspace_relative_path: str | None = None
    mtime_ns: int | None = Field(default=None, ge=0)
    size_bytes: int | None = Field(default=None, ge=0)
    version_token: str | None = None
    source_receipt_id: str | None = None
    workspace_id: str | None = None
    backend_id: str | None = None
    permission_grant_id: str | None = None


class EvidenceBinding(BaseModel):
    ref: EvidenceRef
    record_digest: str = Field(min_length=1)


class EvaluationInputSnapshot(BaseModel):
    schema_version: str = "1"
    snapshot_id: str = Field(min_length=1)
    subject: EvaluationSubject
    contract_id: str | None = None
    contract_version: str | None = None
    contract_hash: str = Field(min_length=1)
    transcript_projection_version: str = "puddingclaw-grader-transcript-v1"
    transcript_projection: list[dict[str, Any]] = Field(default_factory=list)
    transcript_digest: str = Field(min_length=1)
    candidate_message_id: str = Field(min_length=1)
    candidate_content_digest: str = Field(min_length=1)
    candidate_tool_calls_digest: str = Field(min_length=1)
    evidence_refs: list[EvidenceRef] = Field(default_factory=list)
    evidence_bindings: list[EvidenceBinding] = Field(default_factory=list)
    evidence_digest: str = Field(min_length=1)
    artifact_fingerprints: list[ArtifactFingerprint] = Field(default_factory=list)
    workspace_fingerprint: str | None = None
    grader_policy_version: str = "puddingclaw-online-verification-v1"
    grader_policy_hash: str = Field(min_length=1)
    permission_epoch: int = Field(default=1, ge=1)
    created_at: float = Field(default_factory=time.time)

    @model_validator(mode="after")
    def validate_digests(self) -> EvaluationInputSnapshot:
        expected_evidence = stable_digest(
            [item.model_dump(mode="json") for item in self.evidence_bindings]
        )
        if self.evidence_digest != expected_evidence:
            raise ValueError("Evaluation snapshot evidence_digest does not match evidence_refs")
        if self.transcript_digest != stable_digest(self.transcript_projection):
            raise ValueError("Evaluation snapshot transcript_digest does not match projection")
        if self.candidate_message_id != self.subject.query_id:
            raise ValueError("Evaluation snapshot candidate identity must match its subject query")
        candidate_content = ""
        candidate_tool_calls: list[dict[str, Any]] = []
        for message in self.transcript_projection:
            role = str(message.get("role") or message.get("type") or "").lower()
            if role in {"ai", "assistant"}:
                content = message.get("content")
                candidate_content = content if isinstance(content, str) else str(content or "")
            calls = message.get("tool_calls")
            if isinstance(calls, list):
                candidate_tool_calls.extend(
                    dict(item) for item in calls if isinstance(item, dict)
                )
        if self.candidate_content_digest != stable_digest(candidate_content):
            raise ValueError("Evaluation snapshot candidate content is not the projected candidate")
        if self.candidate_tool_calls_digest != stable_digest(candidate_tool_calls):
            raise ValueError("Evaluation snapshot candidate tool calls are not the projected candidate calls")
        refs = [item.model_dump_json() for item in self.evidence_refs]
        bound_refs = [item.ref.model_dump_json() for item in self.evidence_bindings]
        if len(refs) != len(set(refs)):
            raise ValueError("Evaluation snapshot contains duplicate evidence refs")
        if sorted(bound_refs) != sorted(refs):
            raise ValueError("Every evidence ref must have exactly one frozen evidence binding")
        return self


class VerificationMethod(StrEnum):
    DETERMINISTIC = "deterministic"
    ENVIRONMENT = "environment"
    SEMANTIC_RUBRIC = "semantic_rubric"


class VerificationRecordStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    SATISFIED = "satisfied"
    NEEDS_REVISION = "needs_revision"
    FAILED = "failed"
    NOT_EVALUATED = "not_evaluated"
    GRADER_ERROR = "grader_error"
    INFRASTRUCTURE_ERROR = "infrastructure_error"
    STALE = "stale"


class VerificationCriterionResult(BaseModel):
    criterion_id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    passed: bool | None
    evidence_refs: list[EvidenceRef] = Field(default_factory=list)
    evidence: list[dict[str, Any]] = Field(default_factory=list)
    gap: str | None = None
    failure_kind: str | None = None


class VerificationRecord(BaseModel):
    verification_id: str = Field(min_length=1)
    snapshot_id: str = Field(min_length=1)
    method: VerificationMethod
    status: VerificationRecordStatus
    criteria: list[VerificationCriterionResult] = Field(default_factory=list)
    evidence_refs: list[EvidenceRef] = Field(default_factory=list)
    operation_id: str = Field(min_length=1)
    attempt_no: int = Field(ge=0)
    input_digest: str = Field(min_length=1)
    verifier_model: str | None = None
    verifier_policy_version: str = "puddingclaw-online-verification-v1"
    verifier_policy_hash: str = Field(min_length=1)
    tool_receipt_ids: list[str] = Field(default_factory=list)
    latency_ms: int | None = Field(default=None, ge=0)
    usage: dict[str, Any] = Field(default_factory=dict)
    error_kind: str | None = None
    stale_reason: str | None = None
    started_at: float = Field(default_factory=time.time)
    completed_at: float | None = None
    result_digest: str = Field(min_length=1)

    @model_validator(mode="after")
    def validate_terminal_shape(self) -> VerificationRecord:
        if self.status == VerificationRecordStatus.STALE and not self.stale_reason:
            raise ValueError("A stale verification record requires stale_reason")
        if self.status in {
            VerificationRecordStatus.GRADER_ERROR,
            VerificationRecordStatus.INFRASTRUCTURE_ERROR,
        } and not self.error_kind:
            raise ValueError("Control-plane verification errors require error_kind")
        canonical = self.model_dump(mode="json", exclude={"result_digest"}, exclude_none=True)
        if self.result_digest != stable_digest(canonical):
            raise ValueError("Verification record result_digest does not match its immutable payload")
        return self


class VerificationInvalidation(BaseModel):
    """Append-only marker that prevents reuse of an immutable verification."""

    invalidation_id: str = Field(min_length=1)
    verification_id: str = Field(min_length=1)
    snapshot_id: str = Field(min_length=1)
    reason: str = Field(min_length=1, max_length=500)
    invalidated_at: float = Field(default_factory=time.time)


class VerificationProposal(BaseModel):
    """Merged verifier verdict. It is evidence, never Goal write authority."""

    proposal_id: str = Field(min_length=1)
    snapshot_id: str = Field(min_length=1)
    run_id: str = Field(min_length=1)
    status: VerificationRecordStatus
    verification_record_ids: list[str] = Field(min_length=1)
    evaluations: list[VerificationCriterionResult] = Field(default_factory=list)
    gaps: list[str] = Field(default_factory=list)
    explanation: str = ""
    proposal_digest: str = Field(min_length=1)
    created_at: float = Field(default_factory=time.time)

    @model_validator(mode="after")
    def validate_digest(self) -> VerificationProposal:
        canonical = self.model_dump(mode="json", exclude={"proposal_digest"}, exclude_none=True)
        if self.proposal_digest != stable_digest(canonical):
            raise ValueError("Verification proposal digest does not match its payload")
        return self


class RunReviewReport(BaseModel):
    report_id: str = Field(min_length=1)
    run_id: str = Field(min_length=1)
    snapshot_id: str = Field(min_length=1)
    policy: Literal["shadow", "blocking_one_shot"]
    manual: bool = False
    status: VerificationRecordStatus
    verification_record_ids: list[str] = Field(default_factory=list)
    operation_id: str = Field(min_length=1)
    attempt_no: int = Field(default=0, ge=0)
    summary: str = ""
    published_before_review: bool = False
    created_at: float = Field(default_factory=time.time)
    completed_at: float | None = None
    error_kind: str | None = None

    @model_validator(mode="after")
    def validate_publication_order(self) -> RunReviewReport:
        if self.policy == "shadow" and not self.published_before_review:
            raise ValueError("Shadow review must be recorded after publication")
        if self.policy == "blocking_one_shot" and self.published_before_review:
            raise ValueError("Blocking review cannot publish the candidate before review")
        return self


def verification_operation_id(
    snapshot_id: str,
    method: VerificationMethod,
    attempt: int,
    verifier_policy_hash: str,
) -> str:
    if attempt < 0:
        raise ValueError("verification attempt cannot be negative")
    digest = hashlib.sha256(
        f"{snapshot_id}\0{method.value}\0{attempt}\0{verifier_policy_hash}".encode()
    ).hexdigest()
    return f"verification-operation-{digest[:24]}"
