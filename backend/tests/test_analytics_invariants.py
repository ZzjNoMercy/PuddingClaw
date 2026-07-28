"""分析模型 acceptance.invariants 验收引擎与编译/执行接入测试。"""

import pytest
from langchain_core.messages import AIMessage

import harness.analytics_invariants as analytics_invariants
from harness.analytics_invariants import evaluate_model_invariants
from harness.deterministic_checks import evaluate_deterministic_criteria
from harness.models import (
    CriterionSource,
    EvidenceScope,
    RunVerificationContract,
    VerificationCriterion,
    VerifierKind,
)
from harness.rubric_compiler import RubricBuildContext, RunRubricCompiler

_ENERGY_FRONTMATTER = {
    "classifications": {
        "传统能源": ["汽油", "汽油+48V轻混系统", "油电混合", "汽油电驱", "汽油+24V轻混系统"],
        "新能源": ["纯电", "插电混合", "增程式纯电动"],
    },
    "enum_universe": [
        "纯电", "插电混合", "增程式纯电动", "油电混合", "汽油", "汽油电驱",
        "汽油+48V轻混系统", "汽油+24V轻混系统", "汽油+天然气", "柴油",
        "柴油+48V轻混系统", "氢燃料", "天然气",
    ],
}

_ACCEPTANCE_FRONTMATTER = {
    "acceptance": {
        "invariants": [
            {
                "type": "classification_mapping_declaration",
                "target": "dimension:energy_type",
            }
        ]
    }
}


@pytest.fixture
def acceptance_model(monkeypatch):
    monkeypatch.setattr(
        analytics_invariants, "_model_frontmatter", lambda model_id: _ACCEPTANCE_FRONTMATTER
    )
    monkeypatch.setattr(
        analytics_invariants, "_asset_frontmatter", lambda target: _ENERGY_FRONTMATTER
    )


def _state(answer: str, model_id: str = "产品配置分析") -> dict:
    return {"analytics_model_id": model_id, "messages": [AIMessage(content=answer)]}


def test_diesel_grouped_into_traditional_energy_violates(acceptance_model) -> None:
    violations = evaluate_model_invariants(
        "产品配置分析", _state("传统能源包括汽油、柴油等类型。")
    )
    assert len(violations) == 1
    assert "柴油" in violations[0]
    assert "传统能源" in violations[0]


def test_correct_mapping_passes(acceptance_model) -> None:
    violations = evaluate_model_invariants(
        "产品配置分析",
        _state("传统能源包括汽油、汽油+48V轻混系统、油电混合、汽油电驱、汽油+24V轻混系统。"),
    )
    assert violations == []


def test_answer_without_label_passes(acceptance_model) -> None:
    violations = evaluate_model_invariants(
        "产品配置分析", _state("本次分析了柴油与纯电车型的配置率差异。")
    )
    assert violations == []


def test_negated_containment_is_not_a_violation(acceptance_model) -> None:
    # 「传统能源不含柴油」与资产声明一致,否定表述不得误判为归类。
    violations = evaluate_model_invariants(
        "产品配置分析", _state("传统能源不含柴油。传统能源不包括天然气。")
    )
    assert violations == []


def test_model_without_acceptance_passes(monkeypatch) -> None:
    monkeypatch.setattr(analytics_invariants, "_model_frontmatter", lambda model_id: {})
    assert evaluate_model_invariants("任意模型", _state("传统能源包括柴油。")) == []


def test_unregistered_invariant_type_skipped(monkeypatch) -> None:
    monkeypatch.setattr(
        analytics_invariants,
        "_model_frontmatter",
        lambda model_id: {
            "acceptance": {"invariants": [{"type": "not_a_real_type", "target": "x"}]}
        },
    )
    assert evaluate_model_invariants("任意模型", _state("任何答复")) == []


def test_compiler_appends_invariants_criterion(acceptance_model) -> None:
    contract = RunRubricCompiler.compile(
        RubricBuildContext(
            user_message="完成任务",
            analytics_model_id="产品配置分析",
            force_required=True,
        )
    )
    assert contract is not None
    criterion = next(
        item for item in contract.criteria if item.id == "analytics_model_invariants"
    )
    assert criterion.verifier == VerifierKind.DETERMINISTIC


def test_compiler_skips_model_without_acceptance(monkeypatch) -> None:
    monkeypatch.setattr(analytics_invariants, "_model_frontmatter", lambda model_id: {})
    contract = RunRubricCompiler.compile(
        RubricBuildContext(
            user_message="完成任务",
            analytics_model_id="产品配置分析",
            force_required=True,
        )
    )
    assert contract is not None
    assert "analytics_model_invariants" not in {
        item.id for item in contract.criteria
    }


def _contract() -> RunVerificationContract:
    return RunVerificationContract(
        contract_id="run-contract-test",
        task_type="analytics",
        criteria=[
            VerificationCriterion(
                id="analytics_model_invariants",
                statement="分析模型声明的验收不变量（acceptance.invariants）必须全部满足。",
                source=CriterionSource.SYSTEM,
                verifier=VerifierKind.DETERMINISTIC,
                evidence_scope=EvidenceScope.RUN_ONLY,
            )
        ],
        verification_packs=["core", "analytics"],
    )


def test_deterministic_dispatch_reports_violation(acceptance_model) -> None:
    evaluations = evaluate_deterministic_criteria(
        _contract(), _state("传统能源：汽油、柴油。")
    )
    assert len(evaluations) == 1
    assert evaluations[0].passed is False
    assert "柴油" in (evaluations[0].gap or "")


def test_deterministic_dispatch_passes_clean_answer(acceptance_model) -> None:
    evaluations = evaluate_deterministic_criteria(
        _contract(), _state("传统能源包括汽油、油电混合。")
    )
    assert evaluations[0].passed is True


def test_deterministic_dispatch_fails_closed_without_model_id(acceptance_model) -> None:
    state = _state("传统能源包括汽油。")
    del state["analytics_model_id"]
    evaluations = evaluate_deterministic_criteria(_contract(), state)
    assert evaluations[0].passed is False
    assert "analytics_model_id" in (evaluations[0].gap or "")
