"""Typed Run, Goal, and verification contracts.

Session JSON remains the cross-Run source of truth. These models define the
legal product states written into that file; they do not replace LangGraph's
same-Run checkpoint or the trace sidecar.
"""

from __future__ import annotations

import time
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, Field


class HarnessStateError(ValueError):
    """Raised when a caller attempts an illegal Harness state transition."""


class RunStatus(StrEnum):
    PREPARING = "preparing"
    RUNNING = "running"
    WAITING_HITL = "waiting_hitl"
    EVALUATING = "evaluating"
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    FAILED = "failed"
    BLOCKED = "blocked"
    BUDGET_EXCEEDED = "budget_exceeded"
    VERIFICATION_FAILED = "verification_failed"


class RunOutcome(StrEnum):
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    FAILED = "failed"
    BLOCKED = "blocked"
    BUDGET_EXCEEDED = "budget_exceeded"
    VERIFICATION_FAILED = "verification_failed"


class GoalStatus(StrEnum):
    ACTIVE = "active"
    PAUSED = "paused"
    BLOCKED = "blocked"
    ACHIEVED = "achieved"
    CANCELLED = "cancelled"
    BUDGET_EXCEEDED = "budget_exceeded"


class VerificationStatus(StrEnum):
    NOT_REQUIRED = "not_required"
    PENDING = "pending"
    EVALUATING = "evaluating"
    SATISFIED = "satisfied"
    NEEDS_REVISION = "needs_revision"
    FAILED = "failed"
    MAX_ITERATIONS_REACHED = "max_iterations_reached"
    INCOMPLETE = "verification_incomplete"
    GRADER_ERROR = "grader_error"
    INFRASTRUCTURE_ERROR = "infrastructure_error"
    BUDGET_EXCEEDED = "budget_exceeded"


class VerificationFailureKind(StrEnum):
    TASK_GAP = "task_gap"
    INFRASTRUCTURE_ERROR = "infrastructure_error"


class ArtifactScope(StrEnum):
    WORKSPACE = "workspace"
    EXTERNAL = "external"
    SCRATCH = "scratch"
    ATTACHMENT = "attachment"


class ArtifactRole(StrEnum):
    """How one write relates to the current Run objective."""

    TARGET = "target"
    CANDIDATE = "candidate"
    TEMPORARY = "temporary"


class CriterionSource(StrEnum):
    SYSTEM = "system"
    MANAGED = "managed"
    SETTINGS = "settings"
    USER = "user"
    TASK = "task"


class VerifierKind(StrEnum):
    DETERMINISTIC = "deterministic"
    ANALYTICS = "analytics"
    LLM_GRADER = "llm_grader"


TERMINAL_RUN_STATUSES = frozenset(
    {
        RunStatus.COMPLETED,
        RunStatus.CANCELLED,
        RunStatus.FAILED,
        RunStatus.BLOCKED,
        RunStatus.BUDGET_EXCEEDED,
        RunStatus.VERIFICATION_FAILED,
    }
)

TERMINAL_GOAL_STATUSES = frozenset(
    {
        GoalStatus.ACHIEVED,
        GoalStatus.CANCELLED,
        GoalStatus.BUDGET_EXCEEDED,
    }
)

RUN_STATUS_FOR_OUTCOME = {
    RunOutcome.COMPLETED: RunStatus.COMPLETED,
    RunOutcome.CANCELLED: RunStatus.CANCELLED,
    RunOutcome.FAILED: RunStatus.FAILED,
    RunOutcome.BLOCKED: RunStatus.BLOCKED,
    RunOutcome.BUDGET_EXCEEDED: RunStatus.BUDGET_EXCEEDED,
    RunOutcome.VERIFICATION_FAILED: RunStatus.VERIFICATION_FAILED,
}

_RUN_TRANSITIONS: dict[RunStatus, frozenset[RunStatus]] = {
    RunStatus.PREPARING: frozenset(
        {
            RunStatus.RUNNING,
            RunStatus.CANCELLED,
            RunStatus.FAILED,
            RunStatus.BLOCKED,
        }
    ),
    RunStatus.RUNNING: frozenset(
        {
            RunStatus.WAITING_HITL,
            RunStatus.EVALUATING,
            RunStatus.COMPLETED,
            RunStatus.CANCELLED,
            RunStatus.FAILED,
            RunStatus.BLOCKED,
            RunStatus.BUDGET_EXCEEDED,
        }
    ),
    RunStatus.WAITING_HITL: frozenset(
        {
            RunStatus.RUNNING,
            RunStatus.CANCELLED,
            RunStatus.FAILED,
            RunStatus.BLOCKED,
            RunStatus.BUDGET_EXCEEDED,
        }
    ),
    RunStatus.EVALUATING: frozenset(
        {
            RunStatus.RUNNING,
            RunStatus.COMPLETED,
            RunStatus.CANCELLED,
            RunStatus.FAILED,
            RunStatus.BUDGET_EXCEEDED,
            RunStatus.VERIFICATION_FAILED,
        }
    ),
}

_GOAL_TRANSITIONS: dict[GoalStatus, frozenset[GoalStatus]] = {
    GoalStatus.ACTIVE: frozenset(
        {
            GoalStatus.PAUSED,
            GoalStatus.BLOCKED,
            GoalStatus.ACHIEVED,
            GoalStatus.CANCELLED,
            GoalStatus.BUDGET_EXCEEDED,
        }
    ),
    GoalStatus.PAUSED: frozenset(
        {
            GoalStatus.ACTIVE,
            GoalStatus.CANCELLED,
            GoalStatus.BUDGET_EXCEEDED,
        }
    ),
    GoalStatus.BLOCKED: frozenset(
        {
            GoalStatus.ACTIVE,
            GoalStatus.PAUSED,
            GoalStatus.CANCELLED,
            GoalStatus.BUDGET_EXCEEDED,
        }
    ),
}


class VerificationCriterion(BaseModel):
    id: str
    statement: str
    source: CriterionSource
    verifier: VerifierKind
    required: bool = True


class RunTaskProfile(BaseModel):
    """Run-local task classification independent from selected model context."""

    primary_intent: str = "general"
    intents: list[str] = Field(default_factory=list)
    initial_packs: list[str] = Field(default_factory=list)
    available_context_refs: list[str] = Field(default_factory=list)
    reasons: list[str] = Field(default_factory=list)


class VerificationActivation(BaseModel):
    """One successful current-Run action that activates a verification pack."""

    activation_id: str
    run_id: str
    query_id: str
    tool_call_id: str
    tool_name: str
    pack: str
    source: str = "tool"
    status: str = "succeeded"
    evidence_refs: list[dict[str, Any]] = Field(default_factory=list)
    created_at: float = Field(default_factory=time.time)


class ArtifactReference(BaseModel):
    """Canonical identity for one Tool-produced artifact.

    ``path`` is the user-visible canonical path. Workspace artifacts also carry
    their stable ``/workspace`` virtual path; explicitly approved external
    artifacts keep their real host path and grant identity instead of being
    rewritten into a fake workspace path.
    """

    receipt_version: int = 2
    artifact_id: str
    scope: ArtifactScope
    role: ArtifactRole = ArtifactRole.CANDIDATE
    path: str
    host_path: str | None = None
    virtual_path: str | None = None
    workspace_relative_path: str | None = None
    authorized: bool = True
    permission_grant_id: str | None = None
    run_id: str | None = None
    query_id: str | None = None
    goal_id: str | None = None
    goal_revision: int | None = None
    backend_id: str | None = None
    workspace_id: str | None = None
    tool_call_id: str
    output_digest: str | None = None
    content_sha256: str | None = None
    size_bytes: int | None = None
    mtime_ns: int | None = None
    written_at: float = Field(default_factory=time.time)


class CriterionEvaluation(BaseModel):
    criterion_id: str
    name: str
    # None means the criterion was not evaluated because the verification
    # control flow itself did not reach a valid terminal verdict.
    passed: bool | None
    verifier: VerifierKind = VerifierKind.LLM_GRADER
    evidence: list[dict[str, Any]] = Field(default_factory=list)
    gap: str | None = None
    failure_kind: VerificationFailureKind | None = None


class RunVerificationContract(BaseModel):
    contract_id: str
    version: str = "1"
    task_type: str
    criteria: list[VerificationCriterion] = Field(default_factory=list)
    rubric: str = ""
    verification_packs: list[str] = Field(default_factory=list)
    activation_reasons: dict[str, list[str]] = Field(default_factory=dict)
    base_contract_id: str | None = None
    created_at: float = Field(default_factory=time.time)

    @property
    def required(self) -> bool:
        return bool(self.criteria and self.rubric.strip())


class RubricEvaluationReport(BaseModel):
    report_id: str
    run_id: str
    status: VerificationStatus
    contract_id: str | None = None
    contract_version: str | None = None
    evaluations: list[CriterionEvaluation] = Field(default_factory=list)
    gaps: list[str] = Field(default_factory=list)
    explanation: str = ""
    iteration_count: int = 0
    verification_scope: Literal["run", "goal_aggregate"] = "run"
    supporting_run_ids: list[str] = Field(default_factory=list)
    goal_revision: int | None = None
    accepted_for_goal_revision: bool | None = None
    created_at: float = Field(default_factory=time.time)


class RunRecord(BaseModel):
    run_id: str
    query_id: str
    session_id: str
    objective: str
    declared_artifact_targets: list[str] = Field(default_factory=list)
    goal_id: str | None = None
    goal_revision: int | None = None
    project_id: str | None = None
    analytics_model_id: str | None = None
    verification_enabled: bool = True
    task_profile: RunTaskProfile = Field(default_factory=RunTaskProfile)
    status: RunStatus = RunStatus.PREPARING
    outcome: RunOutcome | None = None
    declared_verification_contract: RunVerificationContract | None = None
    verification_contract: RunVerificationContract | None = None
    verification_activations: list[VerificationActivation] = Field(default_factory=list)
    verification_report: RubricEvaluationReport | None = None
    model_call_count: int = 0
    budget_exhaustion_reason: str | None = None
    error: str | None = None
    config_snapshot: dict[str, Any] = Field(default_factory=dict)
    created_at: float = Field(default_factory=time.time)
    updated_at: float = Field(default_factory=time.time)
    completed_at: float | None = None

    @property
    def terminal(self) -> bool:
        return self.status in TERMINAL_RUN_STATUSES

    def transition(self, next_status: RunStatus, *, now: float | None = None) -> None:
        timestamp = now if now is not None else time.time()
        if next_status == self.status:
            return
        if self.terminal:
            raise HarnessStateError(
                f"Run {self.run_id} is already terminal ({self.status}); "
                f"cannot transition to {next_status}."
            )
        allowed = _RUN_TRANSITIONS.get(self.status, frozenset())
        if next_status not in allowed:
            raise HarnessStateError(
                f"Illegal Run transition {self.status} -> {next_status} "
                f"for {self.run_id}."
            )
        self.status = next_status
        self.updated_at = timestamp
        if next_status in TERMINAL_RUN_STATUSES:
            self.completed_at = timestamp

    def finish(
        self,
        outcome: RunOutcome,
        *,
        error: str | None = None,
        now: float | None = None,
    ) -> None:
        next_status = RUN_STATUS_FOR_OUTCOME[outcome]
        self.transition(next_status, now=now)
        self.outcome = outcome
        self.error = error


class GoalRevision(BaseModel):
    revision: int
    objective: str
    contract_id: str | None = None
    created_at: float = Field(default_factory=time.time)


class GoalVerificationDecision(BaseModel):
    """Cross-Run acceptance decision for one immutable Goal revision."""

    decision_id: str
    goal_id: str
    objective_revision: int
    status: VerificationStatus
    accepted: bool = False
    supporting_run_ids: list[str] = Field(default_factory=list)
    criterion_provenance: list[dict[str, Any]] = Field(default_factory=list)
    evidence_ref_count: int = 0
    gaps: list[str] = Field(default_factory=list)
    accepted_run_id: str | None = None
    report_id: str | None = None
    created_at: float = Field(default_factory=time.time)


class GoalRecord(BaseModel):
    goal_id: str
    session_id: str
    objective: str
    objective_revision: int = 1
    revisions: list[GoalRevision] = Field(default_factory=list)
    pending_revision: bool = False
    status: GoalStatus = GoalStatus.ACTIVE
    requested_status: GoalStatus | None = None
    current_run_id: str | None = None
    run_ids: list[str] = Field(default_factory=list)
    goal_contract: RunVerificationContract | None = None
    gaps: list[str] = Field(default_factory=list)
    control_notices: list[str] = Field(default_factory=list)
    evidence_refs: list[dict[str, Any]] = Field(default_factory=list)
    latest_verification_report_id: str | None = None
    latest_goal_decision: GoalVerificationDecision | None = None
    max_rounds: int = 8
    round: int = 0
    model_call_count: int = 0
    budget_exhaustion_reason: str | None = None
    consecutive_control_failure_count: int = 0
    total_control_retry_count: int = 0
    last_control_failure_fingerprint: str | None = None
    max_control_retries: int = 2
    max_total_control_retries: int = 4
    created_at: float = Field(default_factory=time.time)
    updated_at: float = Field(default_factory=time.time)
    completed_at: float | None = None

    @property
    def terminal(self) -> bool:
        return self.status in TERMINAL_GOAL_STATUSES

    def attach_run(self, run_id: str, *, now: float | None = None) -> None:
        if self.terminal:
            raise HarnessStateError(
                f"Goal {self.goal_id} is already terminal ({self.status}); "
                "cannot attach another Run."
            )
        if self.status != GoalStatus.ACTIVE:
            raise HarnessStateError(
                f"Goal {self.goal_id} must be active before attaching a Run; "
                f"current status is {self.status}."
            )
        if run_id not in self.run_ids and self.round >= self.max_rounds:
            raise HarnessStateError(
                f"Goal {self.goal_id} exhausted max_rounds={self.max_rounds}."
            )
        if run_id not in self.run_ids:
            self.run_ids.append(run_id)
            self.round += 1
        self.current_run_id = run_id
        self.updated_at = now if now is not None else time.time()

    def transition(self, next_status: GoalStatus, *, now: float | None = None) -> None:
        timestamp = now if now is not None else time.time()
        if next_status == self.status:
            return
        if self.terminal:
            raise HarnessStateError(
                f"Goal {self.goal_id} is already terminal ({self.status}); "
                f"cannot transition to {next_status}."
            )
        allowed = _GOAL_TRANSITIONS.get(self.status, frozenset())
        if next_status not in allowed:
            raise HarnessStateError(
                f"Illegal Goal transition {self.status} -> {next_status} "
                f"for {self.goal_id}."
            )
        self.status = next_status
        self.updated_at = timestamp
        if next_status in TERMINAL_GOAL_STATUSES:
            self.completed_at = timestamp
            self.current_run_id = None
