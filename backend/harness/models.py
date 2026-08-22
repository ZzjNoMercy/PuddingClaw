"""Typed Run, Goal, and verification contracts.

Session JSON remains the cross-Run source of truth. These models define the
legal product states written into that file; they do not replace LangGraph's
same-Run checkpoint or the trace sidecar.
"""

from __future__ import annotations

import time
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator

from harness.evidence_ledger import EvidenceRef


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
    INFRASTRUCTURE_ERROR = "infrastructure_error"
    BLOCKED = "blocked"
    BUDGET_EXCEEDED = "budget_exceeded"
    VERIFICATION_FAILED = "verification_failed"


class GoalStatus(StrEnum):
    ACTIVE = "active"
    PAUSED = "paused"
    BLOCKED = "blocked"
    COMPLETED = "completed"
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


class VerificationMode(StrEnum):
    """Run verification ownership.

    ``agent`` is the default for ordinary read-only work. A successful
    mutation upgrades it monotonically to ``proportional``. Only an explicit
    Goal (or a future explicit strict-verification control) may use ``goal``
    and enter the independent reviewer/repair loop.
    """

    AGENT = "agent"
    PROPORTIONAL = "proportional"
    RUBRIC = "rubric"


class GoalCompletionPolicy(StrEnum):
    """Who accepts an Agent's explicit Goal completion declaration."""

    STANDARD = "standard"
    RUBRIC = "rubric"


class GoalCompletionRequestStatus(StrEnum):
    REQUESTED = "requested"
    EVALUATING = "evaluating"
    NEEDS_REVISION = "needs_revision"
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    INVALIDATED = "invalidated"


class GoalTurnIntent(StrEnum):
    """How the current user turn relates to a standing Goal."""

    INSPECT_GOAL = "inspect_goal"
    CONTINUE_GOAL = "continue_goal"
    REVISE_GOAL = "revise_goal"
    CONTROL_GOAL = "control_goal"
    STANDALONE_TASK = "standalone_task"
    CLARIFY = "clarify"


class RunKind(StrEnum):
    """Execution ownership is distinct from read-only Goal context."""

    GOAL_EXECUTION = "goal_execution"
    GOAL_INSPECTION = "goal_inspection"
    STANDALONE = "standalone"


class VerificationFailureKind(StrEnum):
    TASK_GAP = "task_gap"
    INFRASTRUCTURE_ERROR = "infrastructure_error"
    VALIDATOR_PROTOCOL_ERROR = "validator_protocol_error"


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


class EvidenceScope(StrEnum):
    """When evidence for one criterion remains valid.

    The scope is part of the contract rather than an implicit property of a
    verifier implementation.  This makes cross-Run reuse auditable and keeps a
    Goal continuation from applying a different inheritance rule per pack.
    """

    RUN_ONLY = "run_only"
    GOAL_INHERITABLE = "goal_inheritable"
    ARTIFACT_BOUND = "artifact_bound"
    FRESHNESS_BOUND = "freshness_bound"


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
        GoalStatus.COMPLETED,
        GoalStatus.CANCELLED,
        GoalStatus.BUDGET_EXCEEDED,
    }
)

RUN_STATUS_FOR_OUTCOME = {
    RunOutcome.COMPLETED: RunStatus.COMPLETED,
    RunOutcome.CANCELLED: RunStatus.CANCELLED,
    RunOutcome.FAILED: RunStatus.FAILED,
    RunOutcome.INFRASTRUCTURE_ERROR: RunStatus.FAILED,
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
            GoalStatus.COMPLETED,
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
    evidence_scope: EvidenceScope = EvidenceScope.RUN_ONLY


class SkillCandidate(BaseModel):
    """One installed Skill selected by the task-understanding router."""

    skill_id: str
    confidence: float = Field(ge=0.0, le=1.0)
    evidence: str = ""
    explicit: bool = False
    required: bool = False


class SkillActivation(BaseModel):
    """A verified SKILL.md read scoped to one Run or Goal revision."""

    activation_id: str
    skill_id: str
    scope: Literal["run", "goal"] = "run"
    run_id: str
    goal_id: str | None = None
    goal_revision: int | None = None
    skill_content_sha256: str
    toolsets: list[str] = Field(default_factory=list)
    unlocked_tools: list[str] = Field(default_factory=list)
    source_tool_call_id: str
    policy_epoch: int = Field(default=1, ge=1)
    created_at: float = Field(default_factory=time.time)


class SkillCacheEntry(BaseModel):
    """Hash-bound Skill instructions cached inside one Session."""

    skill_id: str
    skill_content_sha256: str
    content: str
    toolsets: list[str] = Field(default_factory=list)
    policy_epoch: int = Field(default=1, ge=1)
    source_run_id: str
    source_goal_id: str | None = None
    source_goal_revision: int | None = None
    created_at: float = Field(default_factory=time.time)
    last_used_at: float = Field(default_factory=time.time)


class SkillRecommendation(BaseModel):
    """A relevant installed Skill that remains inactive until SKILL.md is read."""

    skill_id: str
    confidence: float = Field(ge=0.0, le=1.0)
    evidence: str = ""
    source: str = "task_profile"
    activation_instruction: str = ""


class CapabilityManifest(BaseModel):
    """Single authority used for model prompt, Tool Schema and Trace."""

    manifest_id: str
    run_id: str
    active_skill_ids: list[str] = Field(default_factory=list)
    recommended_inactive_skills: list[SkillRecommendation] = Field(default_factory=list)
    enabled_toolsets: list[str] = Field(default_factory=list)
    allowed_tool_names: list[str] = Field(default_factory=list)
    unavailable_tools: list[dict[str, Any]] = Field(default_factory=list)
    tool_schema_hash: str
    created_at: float = Field(default_factory=time.time)


class PermissionManifest(BaseModel):
    """Model-visible permission state, separate from tool capability state."""

    manifest_id: str
    run_id: str
    approval_mode: Literal["strict", "smart"]
    backend_mode: str = ""
    filesystem_mode: Literal["restricted", "unrestricted"] = "restricted"
    allowed: list[dict[str, Any]] = Field(default_factory=list)
    runtime_evaluated: list[dict[str, Any]] = Field(default_factory=list)
    hitl_required: list[dict[str, Any]] = Field(default_factory=list)
    blocked: list[dict[str, Any]] = Field(default_factory=list)
    recent_decisions: list[dict[str, Any]] = Field(default_factory=list)
    policy_epoch: int = 1
    policy_version: str = ""
    created_at: float = Field(default_factory=time.time)


class DelegationLimits(BaseModel):
    """Bounded resources for one subagent invocation."""

    wall_clock_seconds: int = Field(default=600, ge=1)
    model_calls: int = Field(default=12, ge=1)
    tool_calls: int = Field(default=30, ge=1)
    idle_seconds: int = Field(default=90, ge=1)


class DelegationContract(BaseModel):
    """Server-authored authority for one native task delegation."""

    subagent_run_id: str
    parent_run_id: str
    parent_tool_call_id: str
    session_id: str
    goal_id: str | None = None
    goal_revision: int | None = None
    subagent_type: str
    objective: str
    todo_slice: list[str] = Field(default_factory=list)
    selected_analytics_model: str | None = None
    semantic_context_refs: list[str] = Field(default_factory=list)
    allowed_skill_activations: list[str] = Field(default_factory=list)
    allowed_toolsets: list[str] = Field(default_factory=list)
    permission_context: dict[str, Any] = Field(default_factory=dict)
    declared_artifact_targets: list[str] = Field(default_factory=list)
    expected_output_schema: str = "DelegationResultEnvelope/v1"
    completion_conditions: list[str] = Field(default_factory=list)
    limits: DelegationLimits = Field(default_factory=DelegationLimits)
    created_at: float = Field(default_factory=time.time)


class DelegationResultEnvelope(BaseModel):
    """Machine-readable subagent handoff consumed by the parent Agent."""

    status: Literal["completed", "blocked", "timed_out", "failed", "cancelled"]
    subagent_run_id: str
    summary: str = ""
    content_trust: Literal["trusted_tool_result", "untrusted_attachment_content"] = "trusted_tool_result"
    completed_todo_ids: list[str] = Field(default_factory=list)
    remaining_todo_ids: list[str] = Field(default_factory=list)
    evidence_refs: list[str] = Field(default_factory=list)
    sql_generation_ids: list[str] = Field(default_factory=list)
    validation_receipt_ids: list[str] = Field(default_factory=list)
    artifact_refs: list[dict[str, Any]] = Field(default_factory=list)
    question_for_parent: str | None = None
    last_successful_action: str | None = None
    blocking_or_timeout_reason: str | None = None
    recommended_parent_action: Literal[
        "continue_directly",
        "ask_user",
        "accept_result",
        "revise_delegation",
    ] = "accept_result"
    retry_same_delegation_allowed: bool = False
    created_at: float = Field(default_factory=time.time)


class RunTaskProfile(BaseModel):
    """Run-local task classification independent from selected model context."""

    primary_intent: str = "general"
    intents: list[str] = Field(default_factory=list)
    work_natures: list[str] = Field(default_factory=list)
    delivery_forms: list[str] = Field(default_factory=list)
    verification_intents: list[str] = Field(default_factory=list)
    skill_candidates: list[SkillCandidate] = Field(default_factory=list)
    missing_explicit_skill_ids: list[str] = Field(default_factory=list)
    execution_route: Literal["skill_first", "native", "missing_skill"] = "native"
    native_fallback: bool = True
    initial_packs: list[str] = Field(default_factory=list)
    available_context_refs: list[str] = Field(default_factory=list)
    classification_evidence: dict[str, list[str]] = Field(default_factory=dict)
    classifier: str = "deterministic_fallback"
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
    stable_evidence_refs: list[EvidenceRef] = Field(default_factory=list)
    created_at: float = Field(default_factory=time.time)


class ValidationArtifactRef(BaseModel):
    artifact_id: str
    content_sha256: str
    path: str | None = None
    observed_path: str | None = None


class ValidationReceipt(BaseModel):
    """Artifact-bound immutable result produced by one validator attempt."""

    validation_receipt_id: str
    run_id: str
    goal_id: str | None = None
    goal_revision: int | None = None
    validator_kind: Literal[
        "html_structure",
        "browser_runtime",
        "javascript_syntax",
        "artifact_ui_contract",
        "project_test",
        "static_check",
    ]
    validator_version: str = "v1"
    artifact_refs: list[ValidationArtifactRef]
    command_evidence_ref: str
    exit_code: int
    checks_passed: int | None = None
    checks_failed: int = 0
    status: Literal["passed", "failed"] = "passed"
    # A failed validator attempt must say whether the artifact bytes failed,
    # the invocation was malformed, or the validator infrastructure failed.
    # Only artifact failures are sticky for the same artifact hash.
    failure_class: (
        Literal[
            "artifact_failure",
            "invocation_failure",
            "infrastructure_failure",
        ]
        | None
    ) = None
    # True only when the validator is known to have consumed the exact bytes
    # identified by artifact_refs. A path mention plus a non-zero exit code is
    # not proof of a content failure.
    content_observed: bool | None = None
    blocking: bool = True
    # Completion evidence and commit authority are deliberately separate.
    # A free-form command may still be useful evidence for the rubric, but it
    # must not authorize publishing bytes merely because an artifact path was
    # present in argv.  Only Harness-controlled validator adapters set this.
    commit_authority: bool = False
    # Stable semantic identity of the validation obligation.  Success can
    # supersede failure only within the same obligation; a UI contract must
    # never wash away a syntax failure (or vice versa).
    obligation_key: str | None = None
    created_at: float = Field(default_factory=time.time)


class DeliveredArtifact(BaseModel):
    """Latest durable identity for one formally committed artifact target."""

    artifact_id: str
    target_path: str
    content_sha256: str
    role: Literal["delivered"] = "delivered"
    status: Literal["active", "deleted", "stale"] = "active"
    deleted_at: float | None = None
    stale_reason: str | None = None
    delivery_receipt_id: str
    related_artifact_ids: list[str] = Field(default_factory=list)
    contract_ids: list[str] = Field(default_factory=list)
    validation_receipt_ids: list[str] = Field(default_factory=list)
    source_skill_ids: list[str] = Field(default_factory=list)
    source_run_id: str
    source_query_id: str
    source_goal_id: str | None = None
    source_goal_revision: int | None = None
    created_at: float = Field(default_factory=time.time)
    updated_at: float = Field(default_factory=time.time)


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
    # External writes performed by HostFileBroker carry the immutable
    # mutation authority that existed at commit time. This prevents a later
    # projection from losing declared-target authority merely because it was
    # not represented as a persistent user Grant.
    mutation_receipt_id: str | None = None
    authority_kind: (
        Literal[
            "workspace",
            "permission_grant",
            "declared_artifact",
            "legacy_declared_artifact_backfill",
        ]
        | None
    ) = None
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
    browser_e2e_required: bool = False
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


class RunHandoffSummary(BaseModel):
    """Bounded cross-Run continuity without replaying private execution logs."""

    source_run_id: str
    goal_id: str | None = None
    goal_revision: int | None = None
    terminal_status: str
    objective: str
    completed_todos: list[dict[str, Any]] = Field(default_factory=list)
    durable_facts: list[str] = Field(default_factory=list)
    evidence_refs: list[dict[str, Any]] = Field(default_factory=list)
    artifact_refs: list[dict[str, Any]] = Field(default_factory=list)
    sql_generation_refs: list[dict[str, Any]] = Field(default_factory=list)
    unresolved_gaps: list[str] = Field(default_factory=list)
    created_at: float = Field(default_factory=time.time)


class RunRecord(BaseModel):
    run_id: str
    query_id: str
    session_id: str
    objective: str
    declared_artifact_targets: list[str] = Field(default_factory=list)
    declared_artifact_targets_version: int = Field(default=2, ge=1)
    run_kind: RunKind = RunKind.STANDALONE
    goal_id: str | None = None
    context_goal_id: str | None = None
    context_goal_revision: int | None = None
    goal_revision: int | None = None
    goal_turn_intent: GoalTurnIntent | None = None
    goal_turn_confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    goal_turn_classifier: str | None = None
    follow_up_of_goal_id: str | None = None
    follow_up_of_artifact_ids: list[str] = Field(default_factory=list)
    execution_mode: Literal["native", "delta_repair"] = "native"
    delta_repair_kind: Literal["presentation_only", "data_refresh", "bounded_unknown"] | None = None
    delta_repair_tool_budget: int | None = Field(default=None, ge=1)
    delta_repair_tool_call_ids: list[str] = Field(default_factory=list)
    project_id: str | None = None
    analytics_model_id: str | None = None
    verification_enabled: bool = True
    verification_mode: VerificationMode = VerificationMode.AGENT
    task_profile: RunTaskProfile = Field(default_factory=RunTaskProfile)
    skill_activations: list[SkillActivation] = Field(default_factory=list)
    capability_manifest: CapabilityManifest | None = None
    permission_manifest: PermissionManifest | None = None
    delegation_contracts: list[DelegationContract] = Field(default_factory=list)
    delegation_results: list[DelegationResultEnvelope] = Field(default_factory=list)
    delegation_events: list[dict[str, Any]] = Field(default_factory=list)
    attachment_instruction_promotions: list[dict[str, Any]] = Field(default_factory=list)
    status: RunStatus = RunStatus.PREPARING
    outcome: RunOutcome | None = None
    declared_verification_contract: RunVerificationContract | None = None
    verification_contract: RunVerificationContract | None = None
    verification_activations: list[VerificationActivation] = Field(default_factory=list)
    verification_report: RubricEvaluationReport | None = None
    completion_requested_at: float | None = None
    completion_request_id: str | None = None
    handoff_summary: RunHandoffSummary | None = None
    model_call_count: int = 0
    budget_exhaustion_reason: str | None = None
    error: str | None = None
    failure_detail: dict[str, Any] | None = None
    next_action: dict[str, Any] | None = None
    model_termination: dict[str, Any] | None = None
    config_snapshot: dict[str, Any] = Field(default_factory=dict)
    created_at: float = Field(default_factory=time.time)
    updated_at: float = Field(default_factory=time.time)
    completed_at: float | None = None

    @model_validator(mode="before")
    @classmethod
    def _migrate_verification_mode(cls, value: Any) -> Any:
        """Preserve strict semantics for Goal Runs persisted before v3."""

        if not isinstance(value, dict):
            return value
        migrated = dict(value)
        if "declared_artifact_targets_version" not in migrated:
            migrated["declared_artifact_targets_version"] = 1
        if not migrated.get("run_kind"):
            migrated["run_kind"] = RunKind.GOAL_EXECUTION.value if migrated.get("goal_id") else RunKind.STANDALONE.value
        if not migrated.get("verification_mode"):
            migrated["verification_mode"] = (
                VerificationMode.RUBRIC.value
                if migrated.get("verification_enabled", True)
                and migrated.get("run_kind") == RunKind.GOAL_EXECUTION.value
                and migrated.get("goal_id")
                else VerificationMode.AGENT.value
            )
        return migrated

    @property
    def terminal(self) -> bool:
        return self.status in TERMINAL_RUN_STATUSES

    @property
    def requires_goal_verification(self) -> bool:
        """Whether this Run may invoke the reviewer and repair loop."""

        return (
            self.run_kind == RunKind.GOAL_EXECUTION
            and self.goal_id is not None
            and self.verification_enabled
            and self.verification_mode == VerificationMode.RUBRIC
        )

    @property
    def executes_goal(self) -> bool:
        return self.run_kind == RunKind.GOAL_EXECUTION and self.goal_id is not None

    def transition(self, next_status: RunStatus, *, now: float | None = None) -> None:
        timestamp = now if now is not None else time.time()
        if next_status == self.status:
            return
        if self.terminal:
            raise HarnessStateError(
                f"Run {self.run_id} is already terminal ({self.status}); cannot transition to {next_status}."
            )
        allowed = _RUN_TRANSITIONS.get(self.status, frozenset())
        if next_status not in allowed:
            raise HarnessStateError(f"Illegal Run transition {self.status} -> {next_status} for {self.run_id}.")
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


class GoalCompletionRequest(BaseModel):
    """Immutable, idempotent Agent declaration for one Goal revision.

    This is deliberately independent from a Rubric report: standard Goals use
    the same request as their sole completion authority.
    """

    request_id: str
    goal_id: str
    objective_revision: int
    run_id: str
    tool_call_id: str
    completed: Literal[True] = True
    policy: GoalCompletionPolicy
    status: GoalCompletionRequestStatus = GoalCompletionRequestStatus.REQUESTED
    message: str = ""
    invalidated_reason: str | None = None
    acceptance_snapshot_id: str | None = None
    verification_report_id: str | None = None
    requested_at: float = Field(default_factory=time.time)
    decided_at: float | None = None


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
    completion_policy: GoalCompletionPolicy = GoalCompletionPolicy.STANDARD
    latest_completion_request_id: str | None = None
    goal_contract: RunVerificationContract | None = None
    gaps: list[str] = Field(default_factory=list)
    control_notices: list[str] = Field(default_factory=list)
    evidence_refs: list[dict[str, Any]] = Field(default_factory=list)
    skill_activations: list[SkillActivation] = Field(default_factory=list)
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
                f"Goal {self.goal_id} is already terminal ({self.status}); cannot attach another Run."
            )
        if self.status != GoalStatus.ACTIVE:
            raise HarnessStateError(
                f"Goal {self.goal_id} must be active before attaching a Run; current status is {self.status}."
            )
        if run_id not in self.run_ids and self.round >= self.max_rounds:
            raise HarnessStateError(f"Goal {self.goal_id} exhausted max_rounds={self.max_rounds}.")
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
                f"Goal {self.goal_id} is already terminal ({self.status}); cannot transition to {next_status}."
            )
        allowed = _GOAL_TRANSITIONS.get(self.status, frozenset())
        if next_status not in allowed:
            raise HarnessStateError(f"Illegal Goal transition {self.status} -> {next_status} for {self.goal_id}.")
        self.status = next_status
        self.updated_at = timestamp
        if next_status in TERMINAL_GOAL_STATUSES:
            self.completed_at = timestamp
            self.current_run_id = None
