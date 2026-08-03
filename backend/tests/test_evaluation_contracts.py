import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from evaluation.contracts import (
    AgentRunEnvelope,
    DatasetBundle,
    EvalCase,
    EvalDataset,
    EvalInput,
    EvaluationOutcome,
    EvaluationResult,
    TraceEvidence,
    protocol_json_schemas,
)


def test_example_bundle_round_trips_without_provider_sdk_types():
    path = Path(__file__).resolve().parent.parent / "evaluation" / "examples" / "general-agent-core.bundle.json"
    bundle = DatasetBundle.model_validate_json(path.read_text(encoding="utf-8"))
    restored = DatasetBundle.model_validate_json(bundle.model_dump_json())
    assert restored.dataset.name == bundle.dataset.name
    assert len(restored.dataset.cases) == 2
    contract_source = (path.parent.parent / "contracts.py").read_text(encoding="utf-8")
    assert "import langsmith" not in contract_source.lower()


def test_protocol_models_reject_unknown_fields_and_invalid_input_shapes():
    with pytest.raises(ValidationError):
        EvalInput(message="hello", turns=[{"role": "user", "content": "also hello"}])
    with pytest.raises(ValidationError):
        EvalDataset(name="x", undocumented_field=True)


def test_evaluation_result_distinguishes_missing_evidence_from_not_applicable():
    missing = EvaluationResult(
        evaluator_id="tool_use.v1",
        evaluator_version="1",
        dimension="tool_use",
        outcome=EvaluationOutcome.ERROR,
        error_type="evidence_missing",
        reason="missing",
    )
    not_applicable = EvaluationResult(
        evaluator_id="tool_use.v1",
        evaluator_version="1",
        dimension="tool_use",
        outcome=EvaluationOutcome.NOT_APPLICABLE,
        reason="no contract",
    )
    assert missing.outcome != not_applicable.outcome
    with pytest.raises(ValidationError):
        EvaluationResult(
            evaluator_id="x",
            evaluator_version="1",
            dimension="safety",
            outcome="not_applicable",
            score=1,
            passed=True,
            reason="invalid green",
        )


def test_primary_protocol_models_have_json_schemas():
    schemas = protocol_json_schemas()
    assert {"EvalDataset", "EvalCase", "DatasetBundle", "EvalExperiment", "AgentRunEnvelope", "TraceEvidence", "EvaluationResult"} <= set(schemas)
    case = EvalCase(name="case", input=EvalInput(message="hello"))
    run = AgentRunEnvelope(case_id=case.case_id, experiment_id="exp", session_id="session")
    evidence = TraceEvidence()
    assert AgentRunEnvelope.model_validate_json(run.model_dump_json()) == run
    assert TraceEvidence.model_validate_json(evidence.model_dump_json()) == evidence


def test_checked_in_protocol_schema_matches_runtime_generation():
    path = Path(__file__).resolve().parent.parent / "evaluation" / "schemas" / "protocol-1.0.json"
    checked_in = json.loads(path.read_text(encoding="utf-8"))
    assert checked_in == {"protocol_version": "1.0", "schemas": protocol_json_schemas()}
