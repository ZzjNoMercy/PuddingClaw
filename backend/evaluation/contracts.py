"""Versioned, provider-neutral evaluation protocol models."""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from datetime import datetime, timezone
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

PROTOCOL_VERSION = "1.0"
_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


class ProtocolModel(BaseModel):
    model_config = ConfigDict(extra="forbid", use_enum_values=True)


class DatasetStatus(StrEnum):
    DRAFT = "draft"
    PUBLISHED = "published"
    ARCHIVED = "archived"


class ExperimentStatus(StrEnum):
    QUEUED = "queued"
    SYNCING = "syncing"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCEL_REQUESTED = "cancel_requested"
    CANCELLED = "cancelled"


class EvaluationDimension(StrEnum):
    TASK_COMPLETION = "task_completion"
    TOOL_USE = "tool_use"
    TRAJECTORY = "trajectory"
    GROUNDING = "grounding"
    MULTI_TURN = "multi_turn"
    SAFETY = "safety"
    ROBUSTNESS = "robustness"


class Criticality(StrEnum):
    NORMAL = "normal"
    HIGH = "high"
    CRITICAL = "critical"


class DataClassification(StrEnum):
    PUBLIC = "public"
    INTERNAL = "internal"
    SENSITIVE = "sensitive"
    RESTRICTED = "restricted"


class EvaluationOutcome(StrEnum):
    PASS = "pass"
    FAIL = "fail"
    NOT_APPLICABLE = "not_applicable"
    NOT_EVALUATED = "not_evaluated"
    ERROR = "error"


class EvalError(ProtocolModel):
    code: str
    message: str
    retryable: bool = False
    details: dict[str, Any] = Field(default_factory=dict)


class EvalTurn(ProtocolModel):
    role: Literal["system", "user", "assistant", "tool"]
    content: str = Field(min_length=1)
    name: str | None = None


class EvalInput(ProtocolModel):
    message: str | None = None
    turns: list[EvalTurn] = Field(default_factory=list)

    @model_validator(mode="after")
    def require_one_input_shape(self) -> EvalInput:
        has_message = bool((self.message or "").strip())
        if has_message == bool(self.turns):
            raise ValueError("exactly one of message or turns is required")
        if self.turns and not any(turn.role == "user" for turn in self.turns):
            raise ValueError("multi-turn input must contain at least one user turn")
        return self


class FixtureReference(ProtocolModel):
    fixture_id: str
    kind: Literal["file", "directory", "database", "connector", "inline"]
    source: str | None = None
    target: str | None = None
    checksum: str | None = None
    read_only: bool = True
    payload: dict[str, Any] | None = None
    reset_strategy: Literal["recreate", "rollback", "restore_snapshot", "none"] = "recreate"
    resource_lock: Literal["read", "write", "exclusive"] = "read"


class EvalSetup(ProtocolModel):
    clock: datetime | None = None
    timezone: str = "UTC"
    fixtures: list[FixtureReference] = Field(default_factory=list)
    resource_group: str | None = None
    allow_network: bool = False
    allow_side_effects: bool = False
    reproducible: bool = True


class EvalExpectations(ProtocolModel):
    exact_output: str | None = None
    reference_answer: str | None = None
    contains_all: list[str] = Field(default_factory=list)
    contains_any: list[str] = Field(default_factory=list)
    excludes: list[str] = Field(default_factory=list)
    required_tools: list[str] = Field(default_factory=list)
    forbidden_tools: list[str] = Field(default_factory=list)
    tool_order: list[str] = Field(default_factory=list)
    max_tool_calls: int | None = Field(default=None, ge=0)
    required_steps: list[str] = Field(default_factory=list)
    forbidden_actions: list[str] = Field(default_factory=list)
    expected_state: dict[str, Any] = Field(default_factory=dict)
    rubric: str | None = None


class CodeVerificationCommand(ProtocolModel):
    command_id: str = Field(min_length=1, max_length=128)
    command: str = Field(min_length=1, max_length=4_000)
    runner: Literal["python_callable_json"] = "python_callable_json"
    timeout_seconds: int = Field(default=120, ge=1, le=900)
    expected_exit_code: int = Field(default=0, ge=0, le=255)

    @model_validator(mode="after")
    def reject_infrastructure_exit_codes(self) -> CodeVerificationCommand:
        if self.expected_exit_code in {124, 125, 126, 127} or self.expected_exit_code >= 128:
            raise ValueError("expected_exit_code cannot use timeout, runner, or signal exit codes")
        normalized = self.command.replace("\\", "/")
        parts = normalized.split("/")
        if (
            normalized.startswith("/")
            or not normalized.endswith(".json")
            or any(part in {"", ".", ".."} for part in parts)
            or parts[0] == ".git"
        ):
            raise ValueError("python_callable_json command must be one hidden relative .json case path")
        if self.expected_exit_code != 0:
            raise ValueError("python_callable_json verification requires expected_exit_code=0")
        return self


class SWEbenchReference(ProtocolModel):
    dataset_name: str = Field(min_length=1, max_length=200)
    split: str = Field(default="test", min_length=1, max_length=64)
    instance_id: str = Field(min_length=1, max_length=200)
    repo: str = Field(pattern=r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
    base_commit: str = Field(pattern=r"^[0-9a-fA-F]{7,64}$")
    version: str | None = Field(default=None, max_length=100)
    environment_setup_commit: str | None = Field(default=None, max_length=64)
    test_patch: str = Field(default="", max_length=2_000_000)
    fail_to_pass: list[str] = Field(default_factory=list, max_length=10_000)
    pass_to_pass: list[str] = Field(default_factory=list, max_length=10_000)

    @model_validator(mode="after")
    def validate_instance_path_identity(self) -> SWEbenchReference:
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", self.instance_id):
            raise ValueError("SWE-bench instance_id must be one safe path/container identifier")
        return self


class CodeRepositorySpec(ProtocolModel):
    kind: Literal["inline", "swebench"] = "inline"
    files: dict[str, str] = Field(default_factory=dict)
    swebench: SWEbenchReference | None = None

    @model_validator(mode="after")
    def validate_repository_shape(self) -> CodeRepositorySpec:
        if self.kind == "inline" and not self.files:
            raise ValueError("inline code repository requires at least one file")
        if self.kind == "swebench" and self.swebench is None:
            raise ValueError("swebench repository requires a SWE-bench reference")
        if self.kind == "inline" and self.swebench is not None:
            raise ValueError("inline code repository cannot include a SWE-bench reference")
        return self


class CodeVerificationSpec(ProtocolModel):
    mode: Literal["commands", "swebench"] = "commands"
    commands: list[CodeVerificationCommand] = Field(default_factory=list, max_length=20)
    hidden_files: dict[str, str] = Field(default_factory=dict)
    require_patch: bool = True

    @model_validator(mode="after")
    def validate_verification_shape(self) -> CodeVerificationSpec:
        if self.mode == "commands" and not self.commands:
            raise ValueError("command verification requires at least one command")
        if self.mode == "swebench" and (self.commands or self.hidden_files):
            raise ValueError("SWE-bench verification is delegated to the official Harness")
        return self


class CodeEvaluationSpec(ProtocolModel):
    schema_version: Literal["1"] = "1"
    repository: CodeRepositorySpec
    verification: CodeVerificationSpec


class EvaluatorBinding(ProtocolModel):
    evaluator_id: str
    version: str = "1"
    required: bool = False
    config: dict[str, Any] = Field(default_factory=dict)


class ResolvedEvaluatorBinding(EvaluatorBinding):
    code_hash: str


class EvalCase(ProtocolModel):
    protocol_version: Literal["1.0"] = PROTOCOL_VERSION
    case_id: str = Field(default_factory=lambda: new_id("case"))
    revision_id: str = Field(default_factory=lambda: new_id("rev"))
    name: str = Field(min_length=1, max_length=200)
    description: str = ""
    enabled: bool = True
    repetitions: int = Field(default=1, ge=1, le=20)
    dimensions: list[EvaluationDimension] = Field(default_factory=list)
    input: EvalInput
    setup: EvalSetup = Field(default_factory=EvalSetup)
    expectations: EvalExpectations = Field(default_factory=EvalExpectations)
    code: CodeEvaluationSpec | None = None
    evaluator_bindings: list[EvaluatorBinding] = Field(default_factory=list)
    resolved_evaluator_bindings: list[ResolvedEvaluatorBinding] = Field(default_factory=list)
    criticality: Criticality = Criticality.NORMAL
    data_classification: DataClassification = DataClassification.INTERNAL
    tags: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)

    @model_validator(mode="after")
    def validate_case_id(self) -> EvalCase:
        if not _ID_PATTERN.fullmatch(self.case_id):
            raise ValueError("case_id contains unsupported characters")
        return self


class EvalDataset(ProtocolModel):
    protocol_version: Literal["1.0"] = PROTOCOL_VERSION
    dataset_id: str = Field(default_factory=lambda: new_id("ds"))
    name: str = Field(min_length=1, max_length=200)
    description: str = ""
    status: DatasetStatus = DatasetStatus.DRAFT
    current_version: int = Field(default=0, ge=0)
    current_version_id: str | None = None
    revision: int = Field(default=1, ge=1)
    default_profile: str = "general_agent@1"
    tags: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)
    cases: list[EvalCase] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)

    @model_validator(mode="after")
    def validate_dataset(self) -> EvalDataset:
        if not _ID_PATTERN.fullmatch(self.dataset_id):
            raise ValueError("dataset_id contains unsupported characters")
        ids = [case.case_id for case in self.cases]
        if len(ids) != len(set(ids)):
            raise ValueError("case_id must be unique within a dataset")
        return self


class DatasetBundle(ProtocolModel):
    protocol_version: Literal["1.0"] = PROTOCOL_VERSION
    exported_at: datetime = Field(default_factory=utc_now)
    dataset: EvalDataset
    version_id: str | None = None
    checksum: str | None = None

    def content_checksum(self) -> str:
        payload = self.dataset.model_dump(mode="json")
        raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()


class ExperimentCandidate(ProtocolModel):
    protocol_version: Literal["1.0"] = PROTOCOL_VERSION
    candidate_id: str = Field(default_factory=lambda: new_id("candidate"))
    name: str = Field(min_length=1, max_length=200)
    target: Literal["puddingclaw_agent"] = "puddingclaw_agent"
    llm_model_id: str | None = None
    thinking_level: Literal["low", "high", "max"] | None = None
    credential_name: str | None = None
    project_id: str | None = None
    analytics_model_id: str | None = None
    config: dict[str, Any] = Field(default_factory=dict)
    fingerprint: str | None = None
    fingerprint_status: Literal["partial", "complete"] = "partial"

    def with_fingerprint(self) -> ExperimentCandidate:
        if self.fingerprint:
            return self
        payload = self.model_dump(mode="json", exclude={"fingerprint"})
        digest = hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()[:20]
        return self.model_copy(update={"fingerprint": digest})


class ExecutionPolicy(ProtocolModel):
    repetitions: int = Field(default=1, ge=1, le=20)
    max_concurrency: int = Field(default=1, ge=1, le=16)
    timeout_seconds: int = Field(default=300, ge=1, le=3600)
    preserve_workspaces: bool = False


class EvalExperiment(ProtocolModel):
    protocol_version: Literal["1.0"] = PROTOCOL_VERSION
    experiment_id: str = Field(default_factory=lambda: new_id("exp"))
    name: str = Field(min_length=1, max_length=200)
    dataset_id: str
    dataset_version: int = Field(ge=1)
    dataset_version_id: str
    dataset_content_hash: str
    candidate: ExperimentCandidate
    profile_id: str = "general_agent@1"
    execution: ExecutionPolicy = Field(default_factory=ExecutionPolicy)
    backend: Literal["langsmith"] = "langsmith"
    status: ExperimentStatus = ExperimentStatus.QUEUED
    verdict: Literal["pending", "pass", "fail", "indeterminate"] = "pending"
    remote_experiment_id: str | None = None
    remote_url: str | None = None
    summary: dict[str, Any] = Field(default_factory=dict)
    error: EvalError | None = None
    created_at: datetime = Field(default_factory=utc_now)
    started_at: datetime | None = None
    finished_at: datetime | None = None


class ToolCallEvidence(ProtocolModel):
    name: str
    arguments: dict[str, Any] = Field(default_factory=dict)
    output_summary: str | None = None
    succeeded: bool | None = None
    sequence: int = Field(ge=0)


class TokenUsage(ProtocolModel):
    input_tokens: int | None = Field(default=None, ge=0)
    output_tokens: int | None = Field(default=None, ge=0)
    total_tokens: int | None = Field(default=None, ge=0)
    estimated_cost_usd: float | None = Field(default=None, ge=0)


class RunTiming(ProtocolModel):
    latency_ms: float | None = Field(default=None, ge=0)
    time_to_first_token_ms: float | None = Field(default=None, ge=0)


class TraceReference(ProtocolModel):
    provider: Literal["langsmith", "puddingclaw_trace"]
    trace_id: str | None = None
    url: str | None = None


class AgentRunEnvelope(ProtocolModel):
    protocol_version: Literal["1.0"] = PROTOCOL_VERSION
    eval_run_id: str = Field(default_factory=lambda: new_id("evalrun"))
    case_id: str
    experiment_id: str
    candidate_id: str | None = None
    repetition: int = Field(default=0, ge=0)
    input: EvalInput | None = None
    session_id: str
    run_id: str | None = None
    response: str = ""
    structured_output: Any = None
    tool_calls: list[ToolCallEvidence] = Field(default_factory=list)
    state_before: dict[str, Any] = Field(default_factory=dict)
    state_after: dict[str, Any] = Field(default_factory=dict)
    state_diff: dict[str, Any] = Field(default_factory=dict)
    state: dict[str, Any] = Field(default_factory=dict)
    citations: list[dict[str, Any]] = Field(default_factory=list)
    artifacts: list[dict[str, Any]] = Field(default_factory=list)
    usage: TokenUsage = Field(default_factory=TokenUsage)
    timing: RunTiming = Field(default_factory=RunTiming)
    outcome: Literal["completed", "failed", "cancelled", "interrupted"] = "completed"
    trace_refs: list[TraceReference] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)
    started_at: datetime = Field(default_factory=utc_now)
    finished_at: datetime = Field(default_factory=utc_now)
    error: EvalError | None = None


class EvidenceReference(ProtocolModel):
    kind: str
    locator: str | None = None
    summary: str = ""
    metadata: dict[str, Any] = Field(default_factory=dict)


class TraceEvidence(ProtocolModel):
    protocol_version: Literal["1.0"] = PROTOCOL_VERSION
    provider: Literal["envelope", "langsmith", "puddingclaw_trace"] = "envelope"
    run_id: str | None = None
    trace_url: str | None = None
    available_kinds: set[str] = Field(default_factory=set)
    tool_calls: list[ToolCallEvidence] = Field(default_factory=list)
    trajectory: list[str] = Field(default_factory=list)
    grounding: list[EvidenceReference] = Field(default_factory=list)
    model_inputs: list[EvidenceReference] = Field(default_factory=list)
    state_changes: list[EvidenceReference] = Field(default_factory=list)
    permissions: list[EvidenceReference] = Field(default_factory=list)
    retrieval: list[EvidenceReference] = Field(default_factory=list)
    errors: list[EvidenceReference] = Field(default_factory=list)
    safety: list[EvidenceReference] = Field(default_factory=list)
    artifacts: list[EvidenceReference] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


class EvaluationResult(ProtocolModel):
    protocol_version: Literal["1.0"] = PROTOCOL_VERSION
    evaluator_id: str
    evaluator_version: str
    dimension: EvaluationDimension
    outcome: EvaluationOutcome
    error_type: Literal["agent_error", "evaluator_error", "evidence_missing", "verifier_error"] | None = None
    score: float | None = Field(default=None, ge=0, le=1)
    passed: bool | None = None
    reason: str
    evidence: list[EvidenceReference] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_applicability(self) -> EvaluationResult:
        scored = self.outcome in {EvaluationOutcome.PASS, EvaluationOutcome.FAIL}
        if scored and (self.score is None or self.passed is None):
            raise ValueError("pass/fail results require score and passed")
        if not scored and (self.score is not None or self.passed is not None):
            raise ValueError("unscored outcomes cannot contain score or passed")
        if self.outcome == EvaluationOutcome.ERROR and self.error_type is None:
            raise ValueError("error outcomes require error_type")
        if self.outcome != EvaluationOutcome.ERROR and self.error_type is not None:
            raise ValueError("error_type is only valid for error outcomes")
        return self

    @property
    def applicable(self) -> bool:
        return self.outcome not in {
            EvaluationOutcome.NOT_APPLICABLE,
            EvaluationOutcome.NOT_EVALUATED,
        }


class EvaluatorSpec(ProtocolModel):
    evaluator_id: str
    version: str
    dimension: EvaluationDimension
    description: str
    requires: list[str] = Field(default_factory=list)
    scope: Literal["attempt", "case_aggregate", "experiment", "pairwise"] = "attempt"


class EvaluationProfile(ProtocolModel):
    profile_id: str
    version: str
    name: str
    evaluator_ids: list[str]
    dimension_weights: dict[EvaluationDimension, float]
    critical_failure_score: float = Field(default=0.5, ge=0, le=1)


class ValidationIssue(ProtocolModel):
    severity: Literal["error", "warning"]
    code: str
    message: str
    case_id: str | None = None
    path: str | None = None


class DatasetValidation(ProtocolModel):
    valid: bool
    reproducible: bool
    issues: list[ValidationIssue] = Field(default_factory=list)


def protocol_json_schemas() -> dict[str, dict[str, Any]]:
    models = [
        EvalDataset,
        EvalCase,
        DatasetBundle,
        ExperimentCandidate,
        EvalExperiment,
        AgentRunEnvelope,
        TraceEvidence,
        EvaluationResult,
    ]
    return {model.__name__: model.model_json_schema() for model in models}
