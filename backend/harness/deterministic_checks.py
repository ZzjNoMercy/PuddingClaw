"""Deterministic completion checks that can override an LLM grader.

These checks intentionally operate on typed Run state and concrete workspace
evidence. A textual Rubric cannot make a check deterministic by itself.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from harness.models import (
    CriterionEvaluation,
    RunVerificationContract,
    VerifierKind,
)

_WORKSPACE_PATH_PATTERN = re.compile(
    r"(?<![\w/])(/workspace/[^\s`'\"<>\\|)]+)"
)


def evaluate_deterministic_criteria(
    contract: RunVerificationContract,
    final_state: dict[str, Any],
) -> list[CriterionEvaluation]:
    """Evaluate every registered deterministic criterion fail-closed."""

    context = final_state.get("_harness_context")
    harness_context = context if isinstance(context, dict) else {}
    evaluations: list[CriterionEvaluation] = []
    for criterion in contract.criteria:
        if criterion.verifier != VerifierKind.DETERMINISTIC:
            continue
        if criterion.id == "todo_reconciliation":
            evaluations.append(_evaluate_todos(criterion.id, harness_context, final_state))
        elif criterion.id == "artifact_delivery":
            evaluations.append(_evaluate_artifact_delivery(criterion.id, harness_context))
        else:
            evaluations.append(
                CriterionEvaluation(
                    criterion_id=criterion.id,
                    name=criterion.id,
                    passed=False,
                    verifier=VerifierKind.DETERMINISTIC,
                    gap=(
                        "该规则声明为 deterministic，但没有注册对应的代码验证器；"
                        "Harness 按 fail-closed 处理。"
                    ),
                )
            )
    return evaluations


def _evaluate_todos(
    criterion_id: str,
    harness_context: dict[str, Any],
    final_state: dict[str, Any],
) -> CriterionEvaluation:
    raw_todos = harness_context.get("todos", final_state.get("todos", []))
    todos = raw_todos if isinstance(raw_todos, list) else []
    incomplete = [
        item
        for item in todos
        if isinstance(item, dict)
        and str(item.get("status") or "").lower() not in {"completed", "cancelled"}
    ]
    evidence = [
        {
            "kind": "todo_state",
            "total": len(todos),
            "incomplete": len(incomplete),
        }
    ]
    if not incomplete:
        return CriterionEvaluation(
            criterion_id=criterion_id,
            name=criterion_id,
            passed=True,
            verifier=VerifierKind.DETERMINISTIC,
            evidence=evidence,
        )
    labels = [
        str(item.get("content") or item.get("title") or item.get("id") or "未命名 Todo")
        for item in incomplete[:5]
    ]
    return CriterionEvaluation(
        criterion_id=criterion_id,
        name=criterion_id,
        passed=False,
        verifier=VerifierKind.DETERMINISTIC,
        evidence=evidence,
        gap=f"仍有 {len(incomplete)} 个未收口 Todo：{'；'.join(labels)}",
    )


def _evaluate_artifact_delivery(
    criterion_id: str,
    harness_context: dict[str, Any],
) -> CriterionEvaluation:
    content = str(harness_context.get("final_content") or "")
    workspace_raw = str(harness_context.get("workspace_path") or "").strip()
    workspace = Path(workspace_raw).expanduser().resolve() if workspace_raw else None
    mentioned: list[str] = []
    existing: list[str] = []
    missing: list[str] = []
    for match in _WORKSPACE_PATH_PATTERN.finditer(content):
        virtual_path = match.group(1).rstrip("，。；：、,.!?]")
        if virtual_path in mentioned:
            continue
        mentioned.append(virtual_path)
        relative = virtual_path.removeprefix("/workspace/").lstrip("/")
        if workspace is None or not relative or ".." in Path(relative).parts:
            missing.append(virtual_path)
            continue
        local_path = (workspace / relative).resolve()
        try:
            local_path.relative_to(workspace)
        except ValueError:
            missing.append(virtual_path)
            continue
        if local_path.exists():
            existing.append(str(local_path))
        else:
            missing.append(str(local_path))

    evidence = [
        {
            "kind": "workspace_artifact",
            "mentioned": mentioned,
            "existing": existing,
            "missing": missing,
        }
    ]
    if mentioned and existing and not missing:
        return CriterionEvaluation(
            criterion_id=criterion_id,
            name=criterion_id,
            passed=True,
            verifier=VerifierKind.DETERMINISTIC,
            evidence=evidence,
        )
    if missing:
        gap = f"最终回答引用的产物不存在：{'；'.join(missing[:5])}"
    else:
        gap = "最终回答没有给出可验证的 /workspace/ 产物路径。"
    return CriterionEvaluation(
        criterion_id=criterion_id,
        name=criterion_id,
        passed=False,
        verifier=VerifierKind.DETERMINISTIC,
        evidence=evidence,
        gap=gap,
    )
