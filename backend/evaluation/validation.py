"""Publish-time validation for reproducible evaluation datasets."""

from __future__ import annotations

from .contracts import DatasetValidation, EvalDataset, ValidationIssue
from .privacy import find_plaintext_secrets


def _has_executable_expectation(case: object) -> bool:
    expectations = case.expectations
    available_dimensions = set(case.dimensions)
    explicit_evaluators = {binding.evaluator_id for binding in case.evaluator_bindings}

    def selected(dimension: str, evaluator_id: str) -> bool:
        return (not available_dimensions or dimension in available_dimensions) and (
            not explicit_evaluators or evaluator_id in explicit_evaluators
        )

    output_contract = any(
        [
            expectations.exact_output is not None,
            bool(expectations.contains_all),
            bool(expectations.contains_any),
            bool(expectations.excludes),
        ]
    )
    tool_contract = bool(expectations.required_tools or expectations.forbidden_tools) or (
        expectations.max_tool_calls is not None
    )
    return any(
        [
            output_contract and selected("task_completion", "task_completion.v1"),
            tool_contract and selected("tool_use", "tool_use.v1"),
            bool(expectations.tool_order) and selected("trajectory", "trajectory.v1"),
        ]
    )


def validate_dataset(dataset: EvalDataset) -> DatasetValidation:
    issues: list[ValidationIssue] = []
    for finding in find_plaintext_secrets(dataset.model_dump(mode="json")):
        issues.append(
            ValidationIssue(
                severity="error",
                code="plaintext_secret_detected",
                message=f"检测到不可发布的明文凭证类型：{finding.kind}",
                path=finding.path,
            )
        )
    dataset_classification = dataset.metadata.get("data_classification")
    if dataset_classification is not None and str(dataset_classification).lower() not in {
        "public",
        "internal",
        "sensitive",
        "restricted",
    }:
        issues.append(
            ValidationIssue(
                severity="error",
                code="invalid_data_classification",
                message="metadata.data_classification 必须是 public/internal/sensitive/restricted",
                path="metadata.data_classification",
            )
        )
    if not dataset.cases:
        issues.append(ValidationIssue(severity="error", code="empty_dataset", message="Dataset 至少需要一个 Case"))
    elif not any(case.enabled for case in dataset.cases):
        issues.append(
            ValidationIssue(
                severity="error",
                code="no_enabled_cases",
                message="Dataset 至少需要一个 enabled Case，避免空执行集被误判",
                path="cases",
            )
        )

    seen_names: set[str] = set()
    for case in dataset.cases:
        if case.name in seen_names:
            issues.append(
                ValidationIssue(
                    severity="warning",
                    code="duplicate_case_name",
                    message=f"Case 名称重复：{case.name}",
                    case_id=case.case_id,
                    path="name",
                )
            )
        seen_names.add(case.name)
        if not case.setup.reproducible:
            issues.append(
                ValidationIssue(
                    severity="error",
                    code="non_reproducible",
                    message="正式回归 Case 必须声明为可复现",
                    case_id=case.case_id,
                    path="setup.reproducible",
                )
            )
        if case.repetitions != 1:
            issues.append(
                ValidationIssue(
                    severity="error",
                    code="case_repetitions_not_supported",
                    message="Phase 1 统一由 Experiment 控制重复次数；Case repetitions 必须为 1",
                    case_id=case.case_id,
                    path="repetitions",
                )
            )
        unsupported_turn = next(
            (turn for turn in case.input.turns if turn.role not in {"user", "assistant"}), None
        )
        if unsupported_turn is not None:
            issues.append(
                ValidationIssue(
                    severity="error",
                    code="scripted_turn_adapter_missing",
                    message=f"Phase 1 尚不支持 {unsupported_turn.role} scripted turn",
                    case_id=case.case_id,
                    path="input.turns",
                )
            )
        if case.setup.allow_side_effects:
            issues.append(
                ValidationIssue(
                    severity="error",
                    code="side_effects_not_isolated",
                    message="第一阶段不允许真实外部副作用",
                    case_id=case.case_id,
                    path="setup.allow_side_effects",
                )
            )
        if case.setup.allow_network:
            issues.append(
                ValidationIssue(
                    severity="error",
                    code="network_capability_not_supported",
                    message="Phase 1 不开放网络能力；allow_network 必须为 false",
                    case_id=case.case_id,
                    path="setup.allow_network",
                )
            )
        for fixture in case.setup.fixtures:
            if fixture.kind != "inline":
                issues.append(
                    ValidationIssue(
                        severity="error",
                        code="fixture_adapter_missing",
                        message=f"Phase 1 仅支持 inline Fixture：{fixture.fixture_id}",
                        case_id=case.case_id,
                        path="setup.fixtures",
                    )
                )
            if fixture.read_only:
                issues.append(
                    ValidationIssue(
                        severity="error",
                        code="readonly_fixture_not_supported",
                        message=f"Phase 1 尚未提供不可绕过的只读挂载：{fixture.fixture_id}",
                        case_id=case.case_id,
                        path="setup.fixtures",
                    )
                )
            if fixture.reset_strategy != "recreate":
                issues.append(
                    ValidationIssue(
                        severity="error",
                        code="fixture_reset_strategy_not_supported",
                        message=f"Phase 1 仅支持 recreate reset_strategy：{fixture.fixture_id}",
                        case_id=case.case_id,
                        path="setup.fixtures",
                    )
                )
            if fixture.kind != "inline" and not fixture.checksum:
                issues.append(
                    ValidationIssue(
                        severity="error",
                        code="fixture_checksum_missing",
                        message=f"Fixture {fixture.fixture_id} 缺少 checksum",
                        case_id=case.case_id,
                        path="setup.fixtures",
                    )
                )
        if "time_sensitive" in case.tags or case.setup.clock is not None:
            issues.append(
                ValidationIssue(
                    severity="error",
                    code="clock_provider_not_supported",
                    message="Phase 1 尚无可约束 Agent、工具和 middleware 的 Clock Provider",
                    case_id=case.case_id,
                    path="setup.clock",
                )
            )
        if case.enabled and not _has_executable_expectation(case):
            issues.append(
                ValidationIssue(
                    severity="error",
                    code="no_executable_evaluator",
                    message="启用的 Case 至少需要一个 Phase 1 可执行 expectation，并被 dimensions/bindings 选中",
                    case_id=case.case_id,
                    path="expectations",
                )
            )

    return DatasetValidation(
        valid=not any(issue.severity == "error" for issue in issues),
        reproducible=not any(issue.code == "non_reproducible" for issue in issues),
        issues=issues,
    )
