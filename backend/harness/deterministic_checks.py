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
_SOURCE_CITATION_PATTERN = re.compile(r"\[\^src_[A-Za-z0-9_-]+\]")


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
        elif criterion.id == "web_evidence_traceability":
            evaluations.append(
                _evaluate_evidence_traceability(
                    criterion.id,
                    "web_research",
                    harness_context,
                    final_state,
                )
            )
        elif criterion.id == "analytics_evidence_traceability":
            evaluations.append(
                _evaluate_evidence_traceability(
                    criterion.id,
                    "analytics",
                    harness_context,
                    final_state,
                )
            )
        elif criterion.id == "code_validation":
            evaluations.append(
                _evaluate_code_validation(
                    criterion.id,
                    harness_context,
                    final_state,
                )
            )
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
    activations = [
        item
        for item in _verification_activations(harness_context, {})
        if item.get("pack") == "artifact"
    ]
    written_paths = {
        str(ref.get("path") or "")
        for activation in activations
        for ref in activation.get("evidence_refs") or []
        if isinstance(ref, dict) and ref.get("kind") == "artifact_write"
    }
    current_run_paths = {
        virtual_path
        for virtual_path in mentioned
        if virtual_path in written_paths
    }
    evidence[0]["current_run_written_paths"] = sorted(current_run_paths)
    if mentioned and existing and not missing and current_run_paths == set(mentioned):
        return CriterionEvaluation(
            criterion_id=criterion_id,
            name=criterion_id,
            passed=True,
            verifier=VerifierKind.DETERMINISTIC,
            evidence=evidence,
        )
    if missing:
        gap = f"最终回答引用的产物不存在：{'；'.join(missing[:5])}"
    elif mentioned:
        gap = "产物虽然存在，但缺少当前 Run 创建或修改该路径的结构化写入证据。"
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


def _verification_activations(
    harness_context: dict[str, Any],
    final_state: dict[str, Any],
) -> list[dict[str, Any]]:
    raw = harness_context.get("verification_activations", [])
    if not isinstance(raw, list):
        return []
    return [
        item
        for item in raw
        if isinstance(item, dict) and item.get("status") == "succeeded"
    ]


def _evaluate_evidence_traceability(
    criterion_id: str,
    required_pack: str,
    harness_context: dict[str, Any],
    final_state: dict[str, Any],
) -> CriterionEvaluation:
    activations = [
        item
        for item in _verification_activations(harness_context, final_state)
        if item.get("pack") == required_pack
    ]
    evidence = [
        evidence_ref
        for activation in activations
        for evidence_ref in activation.get("evidence_refs") or []
        if isinstance(evidence_ref, dict)
        and evidence_ref.get("material", True) is not False
        and evidence_ref.get("kind") != "tool_execution"
    ]
    if required_pack == "web_research":
        sources = [item for item in evidence if item.get("kind") == "source"]
        final_content = str(
            harness_context.get(
                "final_content",
                final_state.get("final_content", ""),
            )
            or ""
        )
        evidence_urls = {
            str(item.get("uri") or "").rstrip("}),]")
            for item in sources
            if item.get("uri")
        }
        evidence_source_ids = {
            str(item.get("source_id") or "")
            for item in sources
            if item.get("source_id")
        }
        cited_source_ids = {
            match.group(0)[2:-1]
            for match in _SOURCE_CITATION_PATTERN.finditer(final_content)
        }
        cited = bool(evidence_source_ids & cited_source_ids) or any(
            url in final_content for url in evidence_urls
        )
        if not sources or not cited:
            return CriterionEvaluation(
                criterion_id=criterion_id,
                name=criterion_id,
                passed=False,
                verifier=VerifierKind.DETERMINISTIC,
                evidence=evidence,
                gap=(
                    "当前 Run 没有形成可验证的来源引用，或最终回答没有引用"
                    "本轮真实 source_id / 网页链接。"
                ),
            )
    if required_pack == "analytics":
        result_evidence = [
            item
            for item in evidence
            if item.get("kind") in {"tool_result", "analytics_result"}
            and item.get("tool_call_id")
        ]
        if not result_evidence:
            return CriterionEvaluation(
                criterion_id=criterion_id,
                name=criterion_id,
                passed=False,
                verifier=VerifierKind.DETERMINISTIC,
                evidence=evidence,
                gap=(
                    "分析 pack 已激活，但缺少当前 Run 查询结果的输出摘要、"
                    "result_id、query trace 或数据源引用。"
                ),
            )
    if activations and evidence:
        return CriterionEvaluation(
            criterion_id=criterion_id,
            name=criterion_id,
            passed=True,
            verifier=VerifierKind.DETERMINISTIC,
            evidence=evidence,
        )
    pack_label = "网页研究" if required_pack == "web_research" else "数据分析"
    return CriterionEvaluation(
        criterion_id=criterion_id,
        name=criterion_id,
        passed=False,
        verifier=VerifierKind.DETERMINISTIC,
        evidence=evidence,
        gap=(
            f"缺少当前 Run 的结构化 {pack_label} 工具证据；"
            "不能仅凭模型声称“有来源”判定可追溯。"
        ),
    )


def _evaluate_code_validation(
    criterion_id: str,
    harness_context: dict[str, Any],
    final_state: dict[str, Any],
) -> CriterionEvaluation:
    activations = [
        item
        for item in _verification_activations(harness_context, final_state)
        if item.get("pack") == "code"
    ]
    evidence = [
        evidence_ref
        for activation in activations
        for evidence_ref in activation.get("evidence_refs") or []
        if isinstance(evidence_ref, dict)
        and evidence_ref.get("kind") == "tool_result"
        and evidence_ref.get("material", True) is not False
    ]
    if activations and evidence:
        return CriterionEvaluation(
            criterion_id=criterion_id,
            name=criterion_id,
            passed=True,
            verifier=VerifierKind.DETERMINISTIC,
            evidence=evidence,
        )
    return CriterionEvaluation(
        criterion_id=criterion_id,
        name=criterion_id,
        passed=False,
        verifier=VerifierKind.DETERMINISTIC,
        evidence=[],
        gap="未发现当前 Run 成功完成的测试、构建或静态检查命令。",
    )
