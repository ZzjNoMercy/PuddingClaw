import evaluation.evaluators as evaluators_module
from evaluation.contracts import (
    AgentRunEnvelope,
    EvalCase,
    EvalExpectations,
    EvalInput,
    ToolCallEvidence,
    TraceEvidence,
)
from evaluation.evaluators import evaluator_code_hash, evaluator_registry


def test_general_profile_scores_only_supported_metrics_and_preserves_critical_gate():
    case = EvalCase(
        name="safe tool",
        input=EvalInput(message="look up"),
        expectations=EvalExpectations(contains_all=["answer"], required_tools=["search"], forbidden_tools=["delete"]),
        criticality="critical",
    )
    run = AgentRunEnvelope(
        case_id=case.case_id,
        experiment_id="exp",
        session_id="session",
        response="answer",
        tool_calls=[ToolCallEvidence(name="delete", sequence=0, succeeded=True)],
    )
    evidence = TraceEvidence(
        available_kinds={"final_output", "tool_name", "tool_order", "tool_status"},
        tool_calls=run.tool_calls,
        trajectory=["delete"],
        metadata={"offered_tools": ["search", "delete"]},
    )
    results = evaluator_registry.run_profile("general_agent@1", case, run, evidence)
    summary = evaluator_registry.summarize(case, results)
    assert len(results) == 7
    assert summary["critical_failure"] is True
    assert summary["verdict"] == "fail"
    assert next(item for item in results if item.evaluator_id == "grounding.v1").outcome == "not_applicable"


def test_missing_tool_evidence_is_not_a_false_pass():
    case = EvalCase(
        name="tool",
        input=EvalInput(message="use it"),
        expectations=EvalExpectations(required_tools=["search"]),
    )
    run = AgentRunEnvelope(case_id=case.case_id, experiment_id="exp", session_id="session")
    result = next(
        item
        for item in evaluator_registry.run_profile("general_agent@1", case, run, TraceEvidence())
        if item.evaluator_id == "tool_use.v1"
    )
    assert result.outcome == "error"
    assert result.error_type == "evidence_missing"
    assert result.score is None


def test_evaluator_hash_includes_shared_artifact_dependencies(monkeypatch):
    spec, evaluator = evaluator_registry.get_registered("task_completion.v1")
    original = evaluator_code_hash(spec, evaluator)
    monkeypatch.setattr(
        evaluators_module,
        "_evaluator_artifact_source",
        lambda: "changed shared helper or contract",
    )
    assert evaluator_code_hash(spec, evaluator) != original


def test_incomplete_tool_sequence_cannot_produce_trajectory_pass():
    case = EvalCase(
        name="order",
        input=EvalInput(message="run"),
        expectations=EvalExpectations(tool_order=["a", "b"]),
    )
    run = AgentRunEnvelope(case_id=case.case_id, experiment_id="exp", session_id="session")
    evidence = TraceEvidence(
        available_kinds={"tool_order"},
        trajectory=["a", "b"],
        metadata={"tool_sequence_complete": False},
    )
    result = next(
        item
        for item in evaluator_registry.run_profile("general_agent@1", case, run, evidence)
        if item.evaluator_id == "trajectory.v1"
    )
    assert result.outcome == "not_evaluated"
