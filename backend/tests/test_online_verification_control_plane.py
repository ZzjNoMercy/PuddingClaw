from __future__ import annotations

import asyncio
import copy
import inspect
import multiprocessing
from pathlib import Path
from types import SimpleNamespace

import pytest
from deepagents import RubricMiddleware
from langchain_core.messages import AIMessage, HumanMessage

from graph.deepagents_manager import (
    DeepAgentsAgentManager,
    PuddingClawGraderState,
    PuddingClawRubricMiddleware,
    _build_rubric_grader_state,
    _prepare_rubric_grader_messages,
)
from graph.session_manager import SessionManager
from graph.verification.environment import (
    EnvironmentObservation,
    EnvironmentVerificationProfile,
    EnvironmentVerifier,
)
from graph.verification.models import (
    EvaluationInputSnapshot,
    EvaluationSubject,
    EvaluationSubjectKind,
    VerificationCriterionResult,
    VerificationInvalidation,
    VerificationMethod,
    VerificationRecordStatus,
    stable_digest,
)
from graph.verification.orchestrator import OnlineVerificationOrchestrator
from graph.verification.records import build_verification_record
from graph.verification.report_merger import merge_verification_records
from graph.verification.run_review import RunReviewOrchestrator
from graph.verification.snapshots import build_evaluation_snapshot
from graph.verification.transcript_projection import project_messages_for_grader
from harness.coordinators import HarnessRunCoordinator
from harness.models import (
    CriterionSource,
    GoalCompletionPolicy,
    GoalRecord,
    RunOutcome,
    RunRecord,
    RunReviewPolicy,
    RunStatus,
    RunVerificationContract,
    VerificationCriterion,
    VerifierKind,
)
from harness.rubric_compiler import RubricBuildContext, RunRubricCompiler


def _claim_review_operation_worker(
    base_dir: str,
    operation_id: str,
    owner: str,
    output: object,
) -> None:
    sessions = SessionManager()
    sessions.initialize(Path(base_dir))
    claimed = sessions.claim_verification_operation(
        "session-1",
        operation_id,
        owner=owner,
    )
    output.put(str((claimed or {}).get("owner") or ""))


def _sessions(tmp_path) -> SessionManager:
    sessions = SessionManager()
    sessions.initialize(tmp_path)
    sessions.create_session("session-1", metadata={"runtime_mode": "agent"})
    return sessions


def _contract(*criteria: VerificationCriterion) -> RunVerificationContract:
    return RunVerificationContract(
        contract_id="contract-1",
        task_type="test",
        criteria=list(criteria),
        rubric="semantic-only",
    )


def _snapshot(contract: RunVerificationContract | None = None) -> EvaluationInputSnapshot:
    subject = EvaluationSubject(
        kind=EvaluationSubjectKind.RUN_OUTPUT,
        session_id="session-1",
        run_id="run-1",
        query_id="query-1",
    )

    return build_evaluation_snapshot(
        subject=subject,
        contract=contract.model_dump(mode="json") if contract else {},
        transcript_projection=[HumanMessage(content="task"), AIMessage(content="answer")],
        candidate_message_id="query-1",
        candidate_content="answer",
        candidate_tool_calls=[],
        evidence_bindings=[],
        grader_policy={"version": "test-v1"},
    )
def _goal_verification_fixture(tmp_path, custom_rules: list[dict[str, object]]):
    """Create the persisted authority required by the Goal adapter tests."""

    sessions = _sessions(tmp_path)
    coordinator = HarnessRunCoordinator(sessions)
    run, goal = coordinator.start_run(
        session_id="session-1",
        query_id="query-1",
        objective="deliver",
        goal_mode=True,
        completion_policy=GoalCompletionPolicy.RUBRIC,
        custom_rubric_rules=custom_rules,
    )
    assert goal is not None and run.verification_contract is not None
    coordinator.transition(run, RunStatus.RUNNING)
    request = sessions.record_goal_completion_request(
        "session-1",
        goal_id=goal.goal_id,
        objective_revision=goal.objective_revision,
        run_id=run.run_id,
        tool_call_id="complete-1",
    )
    run = RunRecord.model_validate(sessions.get_run_state("session-1", run.run_id))
    goal = GoalRecord.model_validate(sessions.get_goal_state("session-1", goal.goal_id))
    orchestrator = OnlineVerificationOrchestrator(sessions)
    snapshot = orchestrator.freeze_goal_snapshot(
        run=run,
        goal=goal,
        final_state={
            "messages": [HumanMessage(content="deliver"), AIMessage(content="done")]
        },
        workspace_fingerprint="workspace-v1",
    )
    return sessions, orchestrator, run, goal, snapshot, request


def _deterministic_passes(run: RunRecord) -> list[dict[str, object]]:
    return [
        {"criterion_id": item.id, "passed": True}
        for item in run.verification_contract.criteria
        if item.verifier == VerifierKind.DETERMINISTIC
    ]


def _record(
    snapshot: EvaluationInputSnapshot,
    *,
    method: VerificationMethod,
    criteria: list[VerificationCriterionResult],
    status: VerificationRecordStatus = VerificationRecordStatus.SATISFIED,
    attempt: int = 0,
):
    return build_verification_record(
        snapshot_id=snapshot.snapshot_id,
        snapshot_input_digest=stable_digest(snapshot.model_dump(mode="json")),
        method=method,
        status=status,
        criteria=criteria,
        attempt_no=attempt,
        verifier_policy={"version": "test-v1", "method": method.value},
        error_kind=("test_error" if status == VerificationRecordStatus.GRADER_ERROR else None),
    )


def test_subject_prevents_ordinary_review_from_carrying_goal_authority() -> None:
    with pytest.raises(ValueError, match="cannot carry Goal"):
        EvaluationSubject(
            kind="run_output",
            session_id="s",
            run_id="r",
            query_id="q",
            goal_id="g",
        )


def test_snapshot_is_replayable_and_rejects_digest_tampering() -> None:
    snapshot = _snapshot()
    assert snapshot.transcript_projection[-1]["content"] == "answer"
    tampered = snapshot.model_dump(mode="json")
    tampered["transcript_projection"][-1]["content"] = "different"
    with pytest.raises(ValueError, match="transcript_digest"):
        EvaluationInputSnapshot.model_validate(tampered)


def test_projection_removes_control_messages_and_keeps_durable_objective() -> None:
    projected = project_messages_for_grader(
        [
            HumanMessage(
                content="internal",
                additional_kwargs={"lc_source": "puddingclaw_goal_continuation"},
            ),
            AIMessage(content="candidate"),
        ],
        run_query_id="missing",
        objective="durable objective",
    )
    assert [item.content for item in projected] == ["durable objective", "candidate"]


def test_merger_rejects_semantic_override_of_deterministic_failure() -> None:
    contract = _contract(
        VerificationCriterion(
            id="code_validation",
            statement="tests pass",
            source=CriterionSource.SYSTEM,
            verifier=VerifierKind.DETERMINISTIC,
        )
    )
    snapshot = _snapshot(contract)
    deterministic = _record(
        snapshot,
        method=VerificationMethod.DETERMINISTIC,
        status=VerificationRecordStatus.NEEDS_REVISION,
        criteria=[
            VerificationCriterionResult(
                criterion_id="code_validation",
                name="code_validation",
                passed=False,
                gap="tests failed",
            )
        ],
    )
    semantic = _record(
        snapshot,
        method=VerificationMethod.SEMANTIC_RUBRIC,
        criteria=[
            VerificationCriterionResult(
                criterion_id="code_validation",
                name="code_validation",
                passed=True,
            )
        ],
    )
    proposal = merge_verification_records(
        snapshot=snapshot,
        contract=contract,
        records=[deterministic, semantic],
    )
    assert proposal.status == VerificationRecordStatus.NEEDS_REVISION
    assert proposal.evaluations[0].passed is False
    assert "非权威" in proposal.evaluations[0].gap


def test_merger_uses_latest_terminal_attempt_and_ignores_invalidated_history() -> None:
    contract = _contract(
        VerificationCriterion(
            id="quality",
            statement="clear",
            source=CriterionSource.SYSTEM,
            verifier=VerifierKind.LLM_GRADER,
        )
    )
    snapshot = _snapshot(contract)
    old = _record(
        snapshot,
        method=VerificationMethod.SEMANTIC_RUBRIC,
        status=VerificationRecordStatus.NEEDS_REVISION,
        attempt=0,
        criteria=[
            VerificationCriterionResult(
                criterion_id="quality", name="quality", passed=False, gap="unclear"
            )
        ],
    )
    latest = _record(
        snapshot,
        method=VerificationMethod.SEMANTIC_RUBRIC,
        attempt=1,
        criteria=[VerificationCriterionResult(criterion_id="quality", name="quality", passed=True)],
    )
    invalidation = VerificationInvalidation(
        invalidation_id="invalidate-old",
        verification_id=old.verification_id,
        snapshot_id=snapshot.snapshot_id,
        reason="retry",
    )
    proposal = merge_verification_records(
        snapshot=snapshot,
        contract=contract,
        records=[old, latest],
        invalidations=[invalidation],
    )
    assert proposal.status == VerificationRecordStatus.SATISFIED
    assert proposal.verification_record_ids == [latest.verification_id]


def test_session_snapshot_record_and_invalidation_are_append_only(tmp_path) -> None:
    sessions = _sessions(tmp_path)
    coordinator = HarnessRunCoordinator(sessions)
    run, _ = coordinator.start_run(
        session_id="session-1",
        query_id="query-1",
        objective="answer",
        goal_mode=False,
        verification_enabled=False,
        run_review_policy=RunReviewPolicy.SHADOW,
    )
    snapshot = build_evaluation_snapshot(
        subject=EvaluationSubject(
            kind="run_output",
            session_id="session-1",
            run_id=run.run_id,
            query_id=run.query_id,
        ),
        contract={},
        transcript_projection=[HumanMessage(content="answer"), AIMessage(content="done")],
        candidate_message_id=run.query_id,
        candidate_content="done",
        candidate_tool_calls=[],
        evidence_bindings=[],
        grader_policy={"version": "test-v1"},
    )
    sessions.freeze_evaluation_snapshot("session-1", snapshot.model_dump(mode="json"))
    policy = {"version": "test-v1", "method": "semantic_rubric"}
    operation = sessions.reserve_verification_operation(
        "session-1",
        snapshot_id=snapshot.snapshot_id,
        method="semantic_rubric",
        verifier_policy_hash=stable_digest(policy),
    )
    record = build_verification_record(
        snapshot_id=snapshot.snapshot_id,
        snapshot_input_digest=stable_digest(snapshot.model_dump(mode="json")),
        method=VerificationMethod.SEMANTIC_RUBRIC,
        status=VerificationRecordStatus.SATISFIED,
        criteria=[],
        attempt_no=operation["attempt_no"],
        verifier_policy=policy,
    )
    sessions.record_verification_record("session-1", record.model_dump(mode="json"))
    original = copy.deepcopy(sessions.list_verification_records("session-1"))
    invalidations = sessions.mark_verification_records_stale(
        "session-1", snapshot_id=snapshot.snapshot_id, reason="candidate_changed"
    )
    assert invalidations[0]["verification_id"] == record.verification_id
    assert sessions.list_verification_records("session-1") == original


def test_completion_request_is_unique_and_evaluating_request_can_be_invalidated(tmp_path) -> None:
    sessions = _sessions(tmp_path)
    coordinator = HarnessRunCoordinator(sessions)
    run, goal = coordinator.start_run(
        session_id="session-1",
        query_id="query-1",
        objective="deliver",
        goal_mode=True,
        completion_policy=GoalCompletionPolicy.RUBRIC,
    )
    assert goal is not None
    coordinator.transition(run, RunStatus.RUNNING)
    request = sessions.record_goal_completion_request(
        "session-1",
        goal_id=goal.goal_id,
        objective_revision=goal.objective_revision,
        run_id=run.run_id,
        tool_call_id="complete-1",
    )
    with pytest.raises(ValueError, match="already has a live"):
        sessions.record_goal_completion_request(
            "session-1",
            goal_id=goal.goal_id,
            objective_revision=goal.objective_revision,
            run_id=run.run_id,
            tool_call_id="complete-2",
        )
    sessions.update_goal_completion_request_status(
        "session-1", request["request_id"], "evaluating"
    )
    invalidated = sessions.invalidate_goal_completion_request(
        "session-1",
        run_id=run.run_id,
        reason="post_completion_tool_call",
        expected_request_id=request["request_id"],
        expected_revision=goal.objective_revision,
    )
    assert invalidated is not None and invalidated["status"] == "invalidated"


def test_deepagents_0711_nested_grader_uses_public_hooks_and_structured_response() -> None:
    captured: dict[str, object] = {}

    class FakeNestedGrader:
        def invoke(self, payload, **kwargs):
            captured["payload"] = payload
            captured.update(kwargs)
            return {
                "structured_response": {
                    "result": "satisfied",
                    "explanation": "已完成",
                    "criteria": [{"name": "quality", "passed": True}],
                }
            }

    middleware = PuddingClawRubricMiddleware(
        model="test-grader",
        max_iterations=1,
        grader_state_schema=PuddingClawGraderState,
        prepare_messages_for_grader=_prepare_rubric_grader_messages,
        build_grader_state=_build_rubric_grader_state,
    )
    middleware._ensure_grader = lambda: FakeNestedGrader()  # type: ignore[method-assign]

    result = middleware._grade(
        {
            "rubric": "- [quality] clear",
            "messages": [
                HumanMessage(content="task"),
                HumanMessage(
                    content="internal revision",
                    name="rubric_grader",
                    additional_kwargs={"lc_source": "rubric_grader"},
                ),
                AIMessage(content="answer"),
            ],
            "_evaluation_snapshot_id": "snapshot-1",
            "_goal_verification_context": {"completion_request_id": "request-1"},
        },
        0,
        context={"run_id": "run-1"},
    )

    assert result.result == "satisfied"
    payload = captured["payload"]
    assert isinstance(payload, dict)
    assert payload["evaluation_snapshot_id"] == "snapshot-1"
    assert payload["completion_request_id"] == "request-1"
    assert captured["context"] == {"run_id": "run-1"}
    grader_message = payload["messages"][0]
    assert "task" in grader_message.content and "answer" in grader_message.content
    assert "internal revision" not in grader_message.content
    assert middleware._tools == []


def test_build_middlewares_wires_public_grader_hooks_and_no_tools(tmp_path) -> None:
    manager = DeepAgentsAgentManager()
    manager.initialize(tmp_path, user_root=tmp_path)

    middlewares = manager._build_middlewares(  # noqa: SLF001
        project_id=None,
        rubric_model=SimpleNamespace(),
        rubric_config={"enabled": True, "max_iterations": 1},
    )
    rubric = next(item for item in middlewares if isinstance(item, PuddingClawRubricMiddleware))

    assert rubric._prepare_messages_for_grader is _prepare_rubric_grader_messages  # noqa: SLF001
    assert rubric._build_grader_state is _build_rubric_grader_state  # noqa: SLF001
    assert rubric._grader_state_schema is PuddingClawGraderState  # noqa: SLF001
    assert rubric._tools == []  # noqa: SLF001


def test_rubric_middleware_rejects_direct_grader_tools() -> None:
    with pytest.raises(ValueError, match="does not permit grader tools"):
        PuddingClawRubricMiddleware(model="test-grader", tools=[object()])


def test_pudding_rubric_does_not_reintroduce_legacy_private_sdk_overrides() -> None:
    overridden = set(PuddingClawRubricMiddleware.__dict__) & {
        "_build_grader_payload",
        "_grader_input",
        "_parse_grader_response",
        "_reconcile_deterministic_grader_response",
        "_grade",
        "_agrade",
        "_compose_update",
    }
    assert overridden == set()


def test_goal_adapter_canonicalizes_statement_name_to_criterion_id(tmp_path) -> None:
    sessions, orchestrator, run, goal, snapshot, request = _goal_verification_fixture(
        tmp_path,
        [{"id": "quality", "statement": "clear", "verifier": "llm_grader"}],
    )

    report = orchestrator.materialize_goal_proposal_from_verifiers(
        run=run,
        goal=goal,
        snapshot=snapshot,
        deterministic_evaluations=_deterministic_passes(run),
        semantic_evaluation={
            "result": "satisfied",
            "criteria": [
                {"name": "task_fulfillment", "passed": True},
                {"name": "clear", "passed": True},
            ],
        },
    )

    assert report.status.value == "satisfied"
    assert report.verification_record_ids
    quality = next(item for item in report.evaluations if item.criterion_id == "quality")
    assert quality.name == "quality"
    assert sessions.get_harness_state("session-1")["completion_requests"][request["request_id"]]["status"] == "evaluating"


@pytest.mark.parametrize(
    "criteria",
    [
        [{"name": "not-in-contract", "passed": True}],
        [
            {"name": "quality", "passed": True},
            {"name": "quality", "passed": True},
        ],
    ],
)
def test_goal_adapter_rejects_unknown_or_duplicate_criterion_identity(tmp_path, criteria) -> None:
    sessions, orchestrator, run, goal, snapshot, _request = _goal_verification_fixture(
        tmp_path,
        [{"id": "quality", "statement": "clear", "verifier": "llm_grader"}],
    )

    report = orchestrator.materialize_goal_proposal_from_verifiers(
        run=run,
        goal=goal,
        snapshot=snapshot,
        deterministic_evaluations=_deterministic_passes(run),
        semantic_evaluation={"result": "satisfied", "criteria": criteria},
    )

    assert report.status.value == "grader_error"
    records = sessions.list_verification_records("session-1", snapshot_id=snapshot.snapshot_id)
    semantic = next(item for item in records if item["method"] == "semantic_rubric")
    assert semantic["status"] == "grader_error"
    assert semantic["error_kind"].startswith("criterion_identity:")


def test_goal_adapter_preserves_grader_runtime_error(tmp_path) -> None:
    sessions, orchestrator, run, goal, snapshot, _request = _goal_verification_fixture(
        tmp_path,
        [{"id": "quality", "statement": "clear", "verifier": "llm_grader"}],
    )

    report = orchestrator.materialize_goal_proposal_from_verifiers(
        run=run,
        goal=goal,
        snapshot=snapshot,
        deterministic_evaluations=_deterministic_passes(run),
        semantic_evaluation={
            "result": "grader_error",
            "explanation": "Grader raised APIConnectionError: Connection error.",
            "criteria": [],
        },
    )

    assert report.status == VerificationRecordStatus.GRADER_ERROR
    assert report.explanation == "语义 grader 调用失败，未形成业务裁决。"
    records = sessions.list_verification_records("session-1", snapshot_id=snapshot.snapshot_id)
    semantic = next(item for item in records if item["method"] == "semantic_rubric")
    assert semantic["error_kind"] == "grader_runtime:APIConnectionError"


def test_goal_adapter_never_fakes_environment_pass_without_read_only_observation(tmp_path) -> None:
    sessions, orchestrator, run, goal, snapshot, _request = _goal_verification_fixture(
        tmp_path,
        [
            {"id": "quality", "statement": "clear", "verifier": "llm_grader"},
            {"id": "artifact", "statement": "file exists", "verifier": "environment"},
        ],
    )

    report = orchestrator.materialize_goal_proposal_from_verifiers(
        run=run,
        goal=goal,
        snapshot=snapshot,
        deterministic_evaluations=_deterministic_passes(run),
        semantic_evaluation={
            "result": "satisfied",
            "criteria": [
                {"name": "task_fulfillment", "passed": True},
                {"name": "quality", "passed": True},
            ],
        },
    )

    assert report.status.value == "needs_revision"
    artifact = next(item for item in report.evaluations if item.criterion_id == "artifact")
    assert artifact.passed is None
    assert artifact.failure_kind is None or artifact.failure_kind.value == "environment_verifier_not_run"
    environment = next(
        item
        for item in sessions.list_verification_records("session-1", snapshot_id=snapshot.snapshot_id)
        if item["method"] == "environment"
    )
    assert environment["status"] == "not_evaluated"
    assert environment["criteria"][0]["passed"] is None


def test_environment_observation_requires_enforced_read_only_capability() -> None:
    with pytest.raises(ValueError, match="read-only capability boundary"):
        EnvironmentObservation(
            criterion_id="artifact",
            snapshot_id="snapshot-1",
            input_digest="digest-1",
            passed=True,
            capability_profile=EnvironmentVerificationProfile.ENVIRONMENT_VERIFIED.value,
            read_only_enforced=False,
        )


def test_environment_verifier_fail_closes_forged_non_read_only_observation() -> None:
    contract = _contract(
        VerificationCriterion(
            id="artifact",
            statement="file exists",
            source=CriterionSource.SYSTEM,
            verifier=VerifierKind.ENVIRONMENT,
        )
    )
    snapshot = _snapshot(contract)
    record = EnvironmentVerifier().verify(
        snapshot=snapshot,
        contract=contract,
        context={
            "observations": [
                {
                    "criterion_id": "artifact",
                    "snapshot_id": snapshot.snapshot_id,
                    "input_digest": stable_digest(snapshot.model_dump(mode="json")),
                    "passed": True,
                    "capability_profile": "environment_verified",
                    "read_only_enforced": False,
                }
            ]
        },
        profile=EnvironmentVerificationProfile.ENVIRONMENT_VERIFIED,
    )

    assert record is not None
    assert record.status == VerificationRecordStatus.INFRASTRUCTURE_ERROR
    assert record.criteria[0].passed is None
    assert record.error_kind.startswith("environment_observation_protocol:")


def test_deepagents_0711_rubric_hook_signatures_are_pinned() -> None:
    grade = inspect.signature(RubricMiddleware._grade)
    agrade = inspect.signature(RubricMiddleware._agrade)
    compose = inspect.signature(RubricMiddleware._compose_update)
    payload = inspect.signature(RubricMiddleware._build_grader_payload)

    assert grade.parameters["context"].kind == inspect.Parameter.KEYWORD_ONLY
    assert agrade.parameters["context"].kind == inspect.Parameter.KEYWORD_ONLY
    assert list(compose.parameters) == ["self", "state", "evaluation"]
    assert list(payload.parameters) == ["self", "state", "iteration", "correction"]


def test_compiled_rubric_contains_semantic_criteria_only() -> None:
    contract = RunRubricCompiler.compile(RubricBuildContext(user_message="生成销量报告"))

    assert contract is not None
    semantic_ids = {
        item.id for item in contract.criteria if item.verifier == VerifierKind.LLM_GRADER
    }
    deterministic_ids = {
        item.id for item in contract.criteria if item.verifier == VerifierKind.DETERMINISTIC
    }
    assert semantic_ids
    assert all(f"[{criterion_id}]" in contract.rubric for criterion_id in semantic_ids)
    assert all(f"[{criterion_id}]" not in contract.rubric for criterion_id in deterministic_ids)


def test_shadow_run_review_persists_report_without_changing_run_outcome(tmp_path, monkeypatch) -> None:
    sessions = _sessions(tmp_path)
    coordinator = HarnessRunCoordinator(sessions)
    run, goal = coordinator.start_run(
        session_id="session-1",
        query_id="query-1",
        objective="answer",
        goal_mode=False,
        verification_enabled=True,
        run_review_policy=RunReviewPolicy.SHADOW,
    )
    assert goal is None and run.verification_contract is not None
    coordinator.transition(run, RunStatus.RUNNING)
    run.finish(RunOutcome.COMPLETED)
    sessions.terminalize_run_state("session-1", run.run_id, run.model_dump(mode="json"))
    sessions.upsert_assistant_message(
        "session-1",
        query_id=run.query_id,
        content="done",
        status="completed",
    )
    run = type(run).model_validate(sessions.get_run_state("session-1", run.run_id))
    state = {
        "messages": [
            HumanMessage(
                content="answer",
                additional_kwargs={"puddingclaw_query_id": run.query_id},
            ),
            AIMessage(content="done"),
        ],
        "_harness_context": {"todos": [], "final_content": "done"},
    }
    reviewer = RunReviewOrchestrator(sessions)
    snapshot, operation = reviewer.prepare(
        run=run,
        final_state=state,
        workspace_fingerprint="workspace-v1",
        policy=RunReviewPolicy.SHADOW,
    )

    async def fake_after_agent(self, _state, _runtime):
        return {
            "_rubric_evaluations": [
                {
                    "result": "satisfied",
                    "explanation": "done",
                    "criteria": [{"name": "task_fulfillment", "passed": True}],
                }
            ]
        }

    monkeypatch.setattr(RubricMiddleware, "aafter_agent", fake_after_agent)
    report = asyncio.run(
        reviewer.execute(
            run=run,
            snapshot=snapshot,
            operation=operation,
            final_state=state,
            model=object(),
            policy=RunReviewPolicy.SHADOW,
        )
    )
    persisted = sessions.get_run_state("session-1", run.run_id)
    assert report.status == VerificationRecordStatus.SATISFIED
    assert report.published_before_review is True
    assert persisted["outcome"] == RunOutcome.COMPLETED.value
    assert persisted["run_review_report_id"] == report.report_id
    assert sessions.get_harness_state("session-1").get("completion_requests", {}) == {}


def test_manual_review_runtime_error_is_retryable_without_becoming_protocol_error(
    tmp_path,
    monkeypatch,
) -> None:
    sessions = _sessions(tmp_path)
    run = _completed_review_run(sessions)
    state = {
        "messages": [HumanMessage(content=run.objective), AIMessage(content="done")],
        "_harness_context": {"todos": [], "final_content": "done"},
    }
    reviewer = RunReviewOrchestrator(sessions)
    snapshot, operation = reviewer.prepare(
        run=run,
        final_state=state,
        workspace_fingerprint="workspace-runtime-error",
        policy=RunReviewPolicy.SHADOW,
        manual=True,
    )

    async def fake_after_agent(self, _state, _runtime):
        return {
            "_rubric_evaluations": [
                {
                    "result": "grader_error",
                    "explanation": "Grader raised APIConnectionError: Connection error.",
                    "criteria": [],
                }
            ]
        }

    monkeypatch.setattr(RubricMiddleware, "aafter_agent", fake_after_agent)
    report = asyncio.run(
        reviewer.execute(
            run=run,
            snapshot=snapshot,
            operation=operation,
            final_state=state,
            model=object(),
            policy=RunReviewPolicy.SHADOW,
            manual=True,
        )
    )

    assert report.status == VerificationRecordStatus.GRADER_ERROR
    assert report.summary == "语义 grader 调用失败，未形成业务裁决。"
    semantic = next(
        item
        for item in sessions.list_verification_records(
            "session-1", snapshot_id=snapshot.snapshot_id
        )
        if item["method"] == VerificationMethod.SEMANTIC_RUBRIC.value
    )
    assert semantic["error_kind"] == "grader_runtime:APIConnectionError"

    refreshed = RunRecord.model_validate(sessions.get_run_state("session-1", run.run_id))
    retry_snapshot, retry_operation = reviewer.prepare(
        run=refreshed,
        final_state=state,
        workspace_fingerprint="workspace-runtime-error",
        policy=RunReviewPolicy.SHADOW,
        manual=True,
        force_new_attempt=True,
    )
    assert retry_snapshot.snapshot_id == snapshot.snapshot_id
    assert retry_operation["operation_id"] != operation["operation_id"]
    assert retry_operation["attempt_no"] == operation["attempt_no"] + 1


def _completed_review_run(sessions: SessionManager):
    coordinator = HarnessRunCoordinator(sessions)
    run, goal = coordinator.start_run(
        session_id="session-1",
        query_id="query-restart",
        objective="restart-safe review",
        goal_mode=False,
        verification_enabled=True,
        run_review_policy=RunReviewPolicy.SHADOW,
    )
    assert goal is None and run.verification_contract is not None
    coordinator.transition(run, RunStatus.RUNNING)
    run.finish(RunOutcome.COMPLETED)
    sessions.terminalize_run_state("session-1", run.run_id, run.model_dump(mode="json"))
    sessions.upsert_assistant_message(
        "session-1", query_id=run.query_id, content="done", status="completed"
    )
    return type(run).model_validate(sessions.get_run_state("session-1", run.run_id))


def test_ordinary_review_operation_is_reused_across_manager_restart(tmp_path) -> None:
    sessions = _sessions(tmp_path)
    run = _completed_review_run(sessions)
    state = {"messages": [HumanMessage(content="restart-safe review"), AIMessage(content="done")]}
    reviewer = RunReviewOrchestrator(sessions)
    snapshot, operation = reviewer.prepare(
        run=run,
        final_state=state,
        workspace_fingerprint="workspace-restart",
        policy=RunReviewPolicy.SHADOW,
    )
    claimed = sessions.claim_verification_operation(
        "session-1", operation["operation_id"], owner="worker-a"
    )
    assert claimed and claimed["status"] == "running"

    restarted = SessionManager()
    restarted.initialize(tmp_path)
    restarted_run = type(run).model_validate(
        restarted.get_run_state("session-1", run.run_id)
    )
    snapshot2, operation2 = RunReviewOrchestrator(restarted).prepare(
        run=restarted_run,
        final_state=state,
        workspace_fingerprint="workspace-restart",
        policy=RunReviewPolicy.SHADOW,
    )
    assert snapshot2.snapshot_id == snapshot.snapshot_id
    assert operation2["operation_id"] == operation["operation_id"]
    other_claim = restarted.claim_verification_operation(
        "session-1", operation["operation_id"], owner="worker-b"
    )
    assert other_claim and other_claim["owner"] == "worker-a"
    assert restarted.list_pending_run_reviews()[0]["operation_id"] == operation["operation_id"]


def test_review_claim_is_serialized_across_processes(tmp_path) -> None:
    sessions = _sessions(tmp_path)
    run = _completed_review_run(sessions)
    state = {"messages": [HumanMessage(content=run.objective), AIMessage(content="done")]}
    _snapshot_value, operation = RunReviewOrchestrator(sessions).prepare(
        run=run,
        final_state=state,
        workspace_fingerprint="workspace-process-lock",
        policy=RunReviewPolicy.SHADOW,
    )
    context = multiprocessing.get_context("spawn")
    output = context.Queue()
    workers = [
        context.Process(
            target=_claim_review_operation_worker,
            args=(str(tmp_path), operation["operation_id"], owner, output),
        )
        for owner in ("worker-a", "worker-b")
    ]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(timeout=15)
        assert worker.exitcode == 0
    owners = [output.get(timeout=2) for _worker in workers]
    assert len(set(owners)) == 1
    persisted = sessions.get_harness_state("session-1")["verification_operations"][
        operation["operation_id"]
    ]
    assert persisted["owner"] == owners[0]


def test_completed_review_operation_can_replay_report_without_second_grader(tmp_path, monkeypatch) -> None:
    sessions = _sessions(tmp_path)
    run = _completed_review_run(sessions)
    state = {"messages": [HumanMessage(content="restart-safe review"), AIMessage(content="done")]}
    reviewer = RunReviewOrchestrator(sessions)
    snapshot, operation = reviewer.prepare(
        run=run,
        final_state=state,
        workspace_fingerprint="workspace-restart",
        policy=RunReviewPolicy.SHADOW,
    )
    policy = {
        "version": "ordinary-run-review-deepagents-0.7.11-v1",
        "policy": "shadow",
        "tools": [],
        "max_iterations": 1,
        "manual": False,
    }
    record = build_verification_record(
        snapshot_id=snapshot.snapshot_id,
        snapshot_input_digest=stable_digest(snapshot.model_dump(mode="json")),
        method=VerificationMethod.SEMANTIC_RUBRIC,
        status=VerificationRecordStatus.SATISFIED,
        criteria=[
            VerificationCriterionResult(
                criterion_id=run.verification_contract.criteria[0].id,
                name=run.verification_contract.criteria[0].id,
                passed=True,
            )
        ],
        attempt_no=operation["attempt_no"],
        verifier_policy=policy,
    )
    sessions.record_verification_record("session-1", record.model_dump(mode="json"))

    async def fail_if_called(*_args, **_kwargs):
        raise AssertionError("recovery must not call the grader twice")

    monkeypatch.setattr(RubricMiddleware, "aafter_agent", fail_if_called)
    report = asyncio.run(
        reviewer.execute(
            run=run,
            snapshot=snapshot,
            operation=operation,
            final_state=state,
            model=object(),
            policy=RunReviewPolicy.SHADOW,
        )
    )
    assert record.verification_id in report.verification_record_ids
    assert sessions.record_run_review_report("session-1", report.model_dump(mode="json")) == report.model_dump(
        mode="json"
    )
    assert sessions.get_harness_state("session-1")["verification_operations"][
        operation["operation_id"]
    ]["status"] == "completed"


def test_needs_revision_request_can_freeze_a_new_snapshot_attempt(tmp_path) -> None:
    sessions = _sessions(tmp_path)
    coordinator = HarnessRunCoordinator(sessions)
    run, goal = coordinator.start_run(
        session_id="session-1",
        query_id="query-1",
        objective="deliver",
        goal_mode=True,
        completion_policy=GoalCompletionPolicy.RUBRIC,
    )
    assert goal is not None and run.verification_contract is not None
    coordinator.transition(run, RunStatus.RUNNING)
    request = sessions.record_goal_completion_request(
        "session-1",
        goal_id=goal.goal_id,
        objective_revision=goal.objective_revision,
        run_id=run.run_id,
        tool_call_id="complete-1",
    )
    sessions.update_goal_completion_request_status(
        "session-1", request["request_id"], "needs_revision"
    )
    sessions.update_goal_completion_request_status(
        "session-1", request["request_id"], "evaluating"
    )
    saved = sessions.get_harness_state("session-1")["completion_requests"][request["request_id"]]
    assert saved["status"] == "evaluating"
    assert saved["decided_at"] is None


def test_goal_hook_freezes_snapshot_before_sdk_grader_and_persists_proposal(
    tmp_path, monkeypatch
) -> None:
    import graph.deepagents_manager as manager_module

    sessions = _sessions(tmp_path)
    coordinator = HarnessRunCoordinator(sessions)
    run, goal = coordinator.start_run(
        session_id="session-1",
        query_id="query-1",
        objective="deliver",
        goal_mode=True,
        completion_policy=GoalCompletionPolicy.RUBRIC,
    )
    assert goal is not None and run.verification_contract is not None
    coordinator.transition(run, RunStatus.RUNNING)
    request = sessions.record_goal_completion_request(
        "session-1",
        goal_id=goal.goal_id,
        objective_revision=goal.objective_revision,
        run_id=run.run_id,
        tool_call_id="complete-1",
    )
    monkeypatch.setattr(manager_module, "session_manager", sessions)
    observed: dict[str, str] = {}

    def fake_sdk_after_agent(self, state, _runtime):
        snapshot_id = str(state.get("_evaluation_snapshot_id") or "")
        assert sessions.get_evaluation_snapshot("session-1", snapshot_id) is not None
        observed["snapshot_id"] = snapshot_id
        return {
            "_rubric_status": "satisfied",
            "_rubric_evaluations": [
                {
                    "result": "satisfied",
                    "explanation": "done",
                    "criteria": [{"name": "task_fulfillment", "passed": True}],
                }
            ],
        }

    monkeypatch.setattr(RubricMiddleware, "after_agent", fake_sdk_after_agent)
    middleware = manager_module.PuddingClawRubricMiddleware(model=object())
    runtime = type(
        "Runtime",
        (),
        {
            "context": {
                "session_id": "session-1",
                "run_id": run.run_id,
                "query_id": run.query_id,
                "workspace_path": str(tmp_path),
            },
            "stream_writer": None,
        },
    )()
    update = middleware.after_agent(
        {
            "messages": [
                HumanMessage(
                    content="deliver",
                    additional_kwargs={"puddingclaw_query_id": run.query_id},
                ),
                AIMessage(content="done"),
            ],
            "todos": [],
            "rubric": run.verification_contract.rubric,
            "verification_contract": run.verification_contract.model_dump(mode="json"),
        },
        runtime,
    )
    assert update is not None
    report = update["_verification_proposal_report"]
    assert report["source_format"] == "verification_records_v1"
    assert report["snapshot_id"] == observed["snapshot_id"]
    assert report["status"] == "satisfied"
    harness = sessions.get_harness_state("session-1")
    assert harness["completion_requests"][request["request_id"]]["status"] == "evaluating"
    assert harness["verification_proposals"][report["report_id"]]["status"] == "satisfied"
