"""Deterministic completion checks that can override an LLM grader.

These checks intentionally operate on typed Run state and concrete workspace
evidence. A textual Rubric cannot make a check deterministic by itself.
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import Any

from harness.artifact_paths import artifact_path_matches
from harness.models import (
    ArtifactReference,
    ArtifactRole,
    ArtifactScope,
    CriterionEvaluation,
    RunVerificationContract,
    VerificationFailureKind,
    VerifierKind,
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
    current_goal_id = str(harness_context.get("goal_id") or "")
    current_goal_revision = harness_context.get("goal_revision")
    current_run_id = str(harness_context.get("run_id") or "")
    todos = [
        item
        for item in todos
        if isinstance(item, dict)
        and (
            (
                current_goal_id
                and str(item.get("goal_id") or "") == current_goal_id
                and int(item.get("goal_revision") or 1) == int(current_goal_revision or 1)
            )
            or (
                not current_goal_id
                and str(item.get("created_run_id") or current_run_id) == current_run_id
            )
        )
    ]
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
            "goal_id": current_goal_id or None,
            "goal_revision": current_goal_revision,
            "run_id": current_run_id,
            "items": [
                {
                    "id": item.get("id"),
                    "status": item.get("status"),
                    "last_changed_run_id": item.get("last_changed_run_id"),
                }
                for item in todos
            ],
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
    workspace_raw = str(harness_context.get("workspace_path") or "").strip()
    workspace = Path(workspace_raw).expanduser().resolve() if workspace_raw else None
    activations = [
        item
        for item in _verification_activations(harness_context, {})
        if item.get("pack") == "artifact"
    ]
    current_refs = [
        ref
        for activation in activations
        for ref in activation.get("evidence_refs") or []
        if isinstance(ref, dict) and ref.get("kind") == "artifact_write"
    ]
    inherited_raw = harness_context.get("goal_evidence_refs")
    inherited_refs = [
        ref
        for ref in (inherited_raw if isinstance(inherited_raw, list) else [])
        if isinstance(ref, dict) and ref.get("kind") == "artifact_write"
    ]
    current_goal_id = str(harness_context.get("goal_id") or "")
    current_goal_revision = harness_context.get("goal_revision")
    current_workspace_id = str(harness_context.get("workspace_id") or "")
    active_grant_ids = {
        str(item)
        for item in (harness_context.get("active_permission_grant_ids") or [])
        if item
    }
    grants_authoritative = bool(harness_context.get("permission_grants_authoritative"))
    refs_by_id: dict[str, dict[str, Any]] = {}
    malformed: list[dict[str, Any]] = []
    for raw in [*inherited_refs, *current_refs]:
        try:
            parsed = ArtifactReference.model_validate(raw)
        except Exception:
            malformed.append({"path": str(raw.get("path") or ""), "reason": "invalid_artifact_reference"})
            continue
        if current_goal_id and parsed.goal_id and parsed.goal_id != current_goal_id:
            continue
        if (
            current_goal_revision is not None
            and parsed.goal_revision is not None
            and parsed.goal_revision != int(current_goal_revision)
        ):
            continue
        refs_by_id[parsed.artifact_id] = {"kind": "artifact_write", **parsed.model_dump(mode="json")}

    target_refs = [
        ref for ref in refs_by_id.values()
        if ref.get("role") == ArtifactRole.TARGET.value
    ]
    declared_targets = [
        str(item) for item in (harness_context.get("declared_artifact_targets") or []) if item
    ]
    uncovered_targets = [
        declared
        for declared in declared_targets
        if not any(
            artifact_path_matches(str(ref.get(field) or ""), declared)
            for ref in target_refs
            for field in ("path", "host_path", "virtual_path")
            if ref.get(field)
        )
    ]
    current_candidate_refs = [
        ref for ref in refs_by_id.values()
        if ref.get("run_id") == harness_context.get("run_id")
        and ref.get("role") != ArtifactRole.TEMPORARY.value
    ]
    deliverable_refs = [
        ref for ref in refs_by_id.values()
        if ref.get("role") != ArtifactRole.TEMPORARY.value
    ]
    # Explicit objective targets are authoritative and all must remain valid.
    # For an open-ended artifact task, validate the newest current-Run receipt,
    # or the newest inherited receipt when a Goal continuation did not rewrite it.
    selected_refs = target_refs or sorted(
        current_candidate_refs or deliverable_refs,
        key=lambda item: float(item.get("written_at") or 0),
    )[-1:]

    existing: list[dict[str, Any]] = []
    invalid: list[dict[str, Any]] = list(malformed)
    missing: list[dict[str, Any]] = []
    changed: list[dict[str, Any]] = []
    for ref in selected_refs:
        parsed = ArtifactReference.model_validate(ref)
        if parsed.scope == ArtifactScope.EXTERNAL and (
            not parsed.authorized
            or not parsed.permission_grant_id
            or (grants_authoritative and parsed.permission_grant_id not in active_grant_ids)
        ):
            invalid.append({
                "artifact_id": parsed.artifact_id,
                "path": parsed.path,
                "reason": "external_artifact_missing_write_grant",
            })
            continue
        if (
            current_workspace_id
            and parsed.workspace_id
            and parsed.workspace_id != current_workspace_id
        ):
            invalid.append({
                "artifact_id": parsed.artifact_id,
                "path": parsed.path,
                "reason": "workspace_identity_mismatch",
            })
            continue
        host_path = parsed.host_path
        if parsed.scope == ArtifactScope.WORKSPACE:
            relative = parsed.workspace_relative_path
            if workspace is None or not relative or not host_path:
                invalid.append({
                    "artifact_id": parsed.artifact_id,
                    "path": parsed.path,
                    "reason": "workspace_mapping_unavailable",
                })
                continue
            resolved = (workspace / relative).resolve()
            try:
                resolved.relative_to(workspace)
            except ValueError:
                invalid.append({
                    "artifact_id": parsed.artifact_id,
                    "path": parsed.path,
                    "reason": "workspace_path_escape",
                })
                continue
            if Path(host_path).expanduser().resolve() != resolved:
                invalid.append({
                    "artifact_id": parsed.artifact_id,
                    "path": parsed.path,
                    "reason": "workspace_receipt_path_mismatch",
                })
                continue
            host_path = str(resolved)
        if not host_path:
            invalid.append({
                "artifact_id": parsed.artifact_id,
                "path": parsed.path,
                "reason": "host_path_unavailable",
            })
            continue
        artifact_path = Path(host_path)
        if not artifact_path.is_file():
            missing.append({
                "artifact_id": parsed.artifact_id,
                "path": parsed.path,
                "host_path": host_path,
                "scope": parsed.scope.value,
            })
            continue
        if parsed.receipt_version >= 2 and not parsed.content_sha256:
            invalid.append({
                "artifact_id": parsed.artifact_id,
                "path": parsed.path,
                "reason": "artifact_content_digest_missing",
            })
            continue
        hasher = hashlib.sha256()
        with artifact_path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                hasher.update(chunk)
        current_digest = f"sha256:{hasher.hexdigest()}"
        if parsed.content_sha256 and current_digest != parsed.content_sha256:
            changed.append({
                "artifact_id": parsed.artifact_id,
                "path": parsed.path,
                "expected_sha256": parsed.content_sha256,
                "actual_sha256": current_digest,
            })
            continue
        existing.append({
            **ref,
            "verified_host_path": host_path,
            "verified_content_sha256": current_digest,
            "verified_size_bytes": artifact_path.stat().st_size,
        })

    evidence = [{
        "kind": "artifact_registry",
        "current_run_count": len(current_refs),
        "inherited_count": len(inherited_refs),
        "artifacts": list(refs_by_id.values()),
        "selected_artifact_ids": [str(item.get("artifact_id") or "") for item in selected_refs],
        "declared_targets": declared_targets,
        "uncovered_targets": uncovered_targets,
        "existing": existing,
        "missing": missing,
        "changed": changed,
        "invalid": invalid,
    }]
    if (
        selected_refs
        and not uncovered_targets
        and len(existing) == len(selected_refs)
        and not missing
        and not changed
        and not invalid
    ):
        return CriterionEvaluation(
            criterion_id=criterion_id,
            name=criterion_id,
            passed=True,
            verifier=VerifierKind.DETERMINISTIC,
            evidence=evidence,
        )
    if uncovered_targets:
        gap = (
            "尚未写入用户明确指定的交付目标："
            + "；".join(uncovered_targets[:5])
            + "。workspace 副本不能替代该目标。"
        )
        failure_kind = VerificationFailureKind.TASK_GAP
    elif invalid:
        gap = "Artifact 结构化证据或权限映射异常，已停止自动续跑。"
        failure_kind = VerificationFailureKind.INFRASTRUCTURE_ERROR
    elif missing:
        paths = [str(item.get("host_path") or item.get("path") or "") for item in missing[:5]]
        gap = f"工具报告写入成功，但验收时产物不存在：{'；'.join(paths)}"
        failure_kind = VerificationFailureKind.TASK_GAP
    elif changed:
        paths = [str(item.get("path") or "") for item in changed[:5]]
        gap = f"产物在写入后发生变化，需要重新完成并登记：{'；'.join(paths)}"
        failure_kind = VerificationFailureKind.TASK_GAP
    else:
        gap = "本次 Goal 尚无成功写入的结构化产物证据。"
        failure_kind = VerificationFailureKind.TASK_GAP
    return CriterionEvaluation(
        criterion_id=criterion_id,
        name=criterion_id,
        passed=False,
        verifier=VerifierKind.DETERMINISTIC,
        evidence=evidence,
        gap=gap,
        failure_kind=failure_kind,
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
    inherited_raw = harness_context.get("goal_evidence_refs")
    inherited = [
        item
        for item in (inherited_raw if isinstance(inherited_raw, list) else [])
        if isinstance(item, dict)
        and item.get("verification_pack") == required_pack
        and item.get("material", True) is not False
        and item.get("kind") != "tool_execution"
    ]
    evidence = [*inherited, *evidence]
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
                    "当前 Goal 修订版没有形成可验证的来源引用，或最终回答没有引用"
                    "真实 source_id / 网页链接。"
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
                    "分析 pack 已激活，但缺少当前 Goal 修订版查询结果的输出摘要、"
                    "result_id、query trace 或数据源引用。"
                ),
            )
    if (activations or inherited) and evidence:
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
            f"缺少当前 Goal 修订版的结构化 {pack_label} 工具证据；"
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
