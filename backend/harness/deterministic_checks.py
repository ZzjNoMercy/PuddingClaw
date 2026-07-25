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


def _latest_artifact_versions(
    refs: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Return only the latest hash-bound version for each artifact path."""

    latest: dict[str, dict[str, Any]] = {}
    for ref in refs:
        path = str(
            ref.get("host_path")
            or ref.get("path")
            or ref.get("virtual_path")
            or ref.get("artifact_id")
            or ""
        )
        if not path:
            continue
        current = latest.get(path)
        timestamp = float(
            ref.get("written_at")
            or ref.get("_activation_created_at")
            or 0
        )
        current_timestamp = float(
            (current or {}).get("written_at")
            or (current or {}).get("_activation_created_at")
            or 0
        )
        if current is None or timestamp >= current_timestamp:
            latest[path] = ref
    return list(latest.values())


def _artifact_acceptance_set(
    refs: list[dict[str, Any]],
    *,
    run_id: str,
    declared_targets: list[str] | None = None,
) -> list[dict[str, Any]]:
    """Select the same final delivery set for delivery and code validation."""

    latest = _latest_artifact_versions(refs)
    declared = [str(item) for item in declared_targets or [] if item]
    if declared:
        matched = [
            item
            for item in latest
            if any(
                artifact_path_matches(str(item.get(field) or ""), target)
                for target in declared
                for field in ("path", "host_path", "virtual_path")
                if item.get(field)
            )
        ]
        if matched:
            return matched
    if not any(str(item.get("role") or "") for item in latest):
        # Legacy receipts predate role metadata. Preserve their complete set;
        # silently selecting one would weaken an existing multi-file contract.
        return latest
    targets = [
        item
        for item in latest
        if str(item.get("role") or "") == ArtifactRole.TARGET.value
    ]
    if targets:
        return targets
    current_candidates = [
        item
        for item in latest
        if str(item.get("run_id") or "") == run_id
        and str(item.get("role") or "") != ArtifactRole.TEMPORARY.value
    ]
    deliverable = [
        item
        for item in latest
        if str(item.get("role") or "") != ArtifactRole.TEMPORARY.value
    ]
    # Open-ended multi-file deliveries are a bundle, not "whichever file was
    # written last". Temporary/scratch roles are already excluded above.
    return current_candidates or deliverable


def evaluate_deterministic_criteria(
    contract: RunVerificationContract,
    final_state: dict[str, Any],
) -> list[CriterionEvaluation]:
    """Evaluate every registered deterministic criterion fail-closed."""

    context = final_state.get("_harness_context")
    harness_context = dict(context) if isinstance(context, dict) else {}
    harness_context["browser_e2e_required"] = bool(
        contract.browser_e2e_required
    )
    evaluations: list[CriterionEvaluation] = []
    for criterion in contract.criteria:
        if criterion.verifier != VerifierKind.DETERMINISTIC:
            continue
        if criterion.id == "todo_reconciliation":
            evaluations.append(_evaluate_todos(criterion.id, harness_context, final_state))
        elif criterion.id == "tool_protocol_integrity":
            evaluations.append(_evaluate_tool_protocol(criterion.id, final_state))
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
        elif criterion.id == "analytics_model_invariants":
            evaluations.append(
                _evaluate_analytics_model_invariants(
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


def _evaluate_analytics_model_invariants(
    criterion_id: str,
    harness_context: dict[str, Any],
    final_state: dict[str, Any],
) -> CriterionEvaluation:
    from harness.analytics_invariants import evaluate_model_invariants

    model_id = str(
        final_state.get("analytics_model_id")
        or harness_context.get("analytics_model_id")
        or ""
    ).strip()
    if not model_id:
        # 与缺证据惯例一致 fail-closed：编译期能绑定模型，执行期取不到 id 属异常。
        return CriterionEvaluation(
            criterion_id=criterion_id,
            name=criterion_id,
            passed=False,
            verifier=VerifierKind.DETERMINISTIC,
            evidence=[{"kind": "analytics_model_invariants", "analytics_model_id": ""}],
            gap="契约包含分析模型验收不变量，但最终状态缺少 analytics_model_id。",
            failure_kind=VerificationFailureKind.INFRASTRUCTURE_ERROR,
        )
    try:
        violations = evaluate_model_invariants(model_id, final_state)
    except Exception as exc:
        return CriterionEvaluation(
            criterion_id=criterion_id,
            name=criterion_id,
            passed=False,
            verifier=VerifierKind.DETERMINISTIC,
            evidence=[{"kind": "analytics_model_invariants", "analytics_model_id": model_id}],
            gap=f"分析模型验收不变量执行异常：{exc}",
            failure_kind=VerificationFailureKind.INFRASTRUCTURE_ERROR,
        )
    evidence = [
        {
            "kind": "analytics_model_invariants",
            "analytics_model_id": model_id,
            "violations": violations,
        }
    ]
    return CriterionEvaluation(
        criterion_id=criterion_id,
        name=criterion_id,
        passed=not violations,
        verifier=VerifierKind.DETERMINISTIC,
        evidence=evidence,
        gap="；".join(violations[:5]) if violations else None,
        failure_kind=VerificationFailureKind.TASK_GAP if violations else None,
    )


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


def _evaluate_tool_protocol(
    criterion_id: str,
    final_state: dict[str, Any],
) -> CriterionEvaluation:
    requested: list[str] = []
    completed: list[str] = []
    pending: set[str] = set()
    missing: list[str] = []
    duplicate_ids: list[str] = []
    seen_ids: set[str] = set()
    for message in final_state.get("messages") or []:
        if isinstance(message, dict):
            role = str(message.get("role") or message.get("type") or "").lower()
            tool_calls = message.get("tool_calls") or []
            tool_call_id = message.get("tool_call_id")
        else:
            role = str(getattr(message, "type", "") or "").lower()
            tool_calls = getattr(message, "tool_calls", None) or []
            tool_call_id = getattr(message, "tool_call_id", None)
        if role in {"tool", "toolmessage"}:
            call_id = str(tool_call_id or "")
            if call_id and call_id in pending:
                pending.remove(call_id)
                completed.append(call_id)
            continue
        # A ToolMessage must immediately close every parsed call before the
        # transcript advances to another Human/AI message. A later response
        # with the same id cannot retroactively repair the protocol boundary.
        if pending:
            missing.extend(sorted(pending))
            pending.clear()
        # Only executable, parsed tool calls create a protocol obligation.
        # Provider ``invalid_tool_calls`` never entered the tools node; the
        # last model-boundary protocol middleware represents those as an
        # explicit synthetic error in the next model request.
        for call in tool_calls:
            call_id = (
                call.get("id") if isinstance(call, dict) else getattr(call, "id", None)
            )
            if call_id:
                normalized_id = str(call_id)
                requested.append(normalized_id)
                if normalized_id in seen_ids:
                    duplicate_ids.append(normalized_id)
                seen_ids.add(normalized_id)
                pending.add(normalized_id)
    missing.extend(sorted(pending))
    missing = sorted(dict.fromkeys(missing))
    duplicate_ids = sorted(dict.fromkeys(duplicate_ids))
    evidence = [{
        "kind": "tool_protocol",
        "requested_call_ids": requested,
        "completed_call_ids": completed,
        "missing_call_ids": missing,
        "duplicate_call_ids": duplicate_ids,
    }]
    return CriterionEvaluation(
        criterion_id=criterion_id,
        name=criterion_id,
        passed=not missing and not duplicate_ids,
        verifier=VerifierKind.DETERMINISTIC,
        evidence=evidence,
        gap=(
            "存在缺失 ToolMessage 的工具调用：" + "、".join(missing[:10])
            if missing
            else "存在重复使用 tool_call_id 的工具调用：" + "、".join(duplicate_ids[:10])
            if duplicate_ids
            else None
        ),
    )


def _evaluate_artifact_delivery(
    criterion_id: str,
    harness_context: dict[str, Any],
) -> CriterionEvaluation:
    evaluation_phase = str(harness_context.get("evaluation_phase") or "terminal")
    enforce_publication_reference = evaluation_phase == "terminal"
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
    inherited_raw = harness_context.get("goal_evidence_records")
    inherited_refs = [
        ref
        for ref in (inherited_raw if isinstance(inherited_raw, list) else [])
        if isinstance(ref, dict) and ref.get("kind") == "artifact_write"
    ]
    mutation_records = [
        ref
        for activation in activations
        for ref in activation.get("evidence_refs") or []
        if isinstance(ref, dict)
        and ref.get("kind") == "external_mutation_completed"
    ]
    mutation_records.extend(
        ref
        for ref in (inherited_raw if isinstance(inherited_raw, list) else [])
        if isinstance(ref, dict)
        and ref.get("kind") == "external_mutation_completed"
    )
    mutations_by_id = {
        str(item.get("receipt_id") or ""): item
        for item in mutation_records
        if item.get("receipt_id")
    }
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
            and raw.get("revision_inherited") is not True
        ):
            continue
        refs_by_id[parsed.artifact_id] = {"kind": "artifact_write", **parsed.model_dump(mode="json")}

    latest_refs = _latest_artifact_versions(list(refs_by_id.values()))
    declared_targets = [
        str(item) for item in (harness_context.get("declared_artifact_targets") or []) if item
    ]
    uncovered_targets = [
        declared
        for declared in declared_targets
        if not any(
            artifact_path_matches(str(ref.get(field) or ""), declared)
            # The declared path is the acceptance authority. Older Runs may
            # have recorded the correct write as ``candidate`` because their
            # inferred target list was wrong; exact path/hash evidence repairs
            # that metadata without weakening the contract.
            for ref in latest_refs
            for field in ("path", "host_path", "virtual_path")
            if ref.get(field)
        )
    ]
    # Explicit objective targets are authoritative and all must remain valid.
    # For an open-ended artifact task, validate the newest current-Run receipt,
    # or the newest inherited receipt when a Goal continuation did not rewrite it.
    selected_refs = _artifact_acceptance_set(
        list(refs_by_id.values()),
        run_id=str(harness_context.get("run_id") or ""),
        declared_targets=declared_targets,
    )

    existing: list[dict[str, Any]] = []
    invalid: list[dict[str, Any]] = list(malformed)
    missing: list[dict[str, Any]] = []
    changed: list[dict[str, Any]] = []
    final_content = str(harness_context.get("final_content") or "")
    unreferenced: list[dict[str, Any]] = []
    for ref in selected_refs:
        parsed = ArtifactReference.model_validate(ref)
        if parsed.scope == ArtifactScope.EXTERNAL:
            mutation = (
                mutations_by_id.get(parsed.mutation_receipt_id)
                if parsed.mutation_receipt_id
                else None
            )
            mutation_matches = bool(
                isinstance(mutation, dict)
                and str(mutation.get("canonical_path") or "")
                == str(parsed.host_path or parsed.path)
                and str(mutation.get("after_sha256") or "")
                == str(parsed.content_sha256 or "")
                and str(mutation.get("permission_grant_id") or "")
                == str(parsed.permission_grant_id or "")
                and str(mutation.get("run_id") or "")
                == str(parsed.run_id or "")
            )
            declared_target_matches = any(
                artifact_path_matches(
                    str(parsed.host_path or parsed.path),
                    declared,
                )
                for declared in declared_targets
            )
            declared_authority = bool(
                parsed.authorized
                and parsed.authority_kind == "declared_artifact"
                and parsed.permission_grant_id
                == f"declared-artifact:{parsed.run_id}"
                and mutation_matches
                and declared_target_matches
            )
            legacy_authority = bool(
                parsed.authorized
                and parsed.authority_kind
                == "legacy_declared_artifact_backfill"
                and str(parsed.mutation_receipt_id or "").startswith(
                    "legacy-write-backfill:"
                )
                and parsed.permission_grant_id
                == f"declared-artifact:{parsed.run_id}"
                and declared_target_matches
            )
            persistent_grant_authority = bool(
                parsed.authorized
                and parsed.permission_grant_id
                and (
                    not grants_authoritative
                    or parsed.permission_grant_id in active_grant_ids
                )
            )
            if not (
                persistent_grant_authority
                or declared_authority
                or legacy_authority
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
        if parsed.receipt_version >= 2 and (
            not parsed.content_sha256 or parsed.size_bytes is None
        ):
            invalid.append({
                "artifact_id": parsed.artifact_id,
                "path": parsed.path,
                "reason": "artifact_content_identity_missing",
            })
            continue
        hasher = hashlib.sha256()
        with artifact_path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                hasher.update(chunk)
        current_size = artifact_path.stat().st_size
        current_digest = f"sha256:{hasher.hexdigest()}"
        if parsed.size_bytes is not None and current_size != parsed.size_bytes:
            changed.append({
                "artifact_id": parsed.artifact_id,
                "path": parsed.path,
                "expected_size_bytes": parsed.size_bytes,
                "actual_size_bytes": current_size,
            })
            continue
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
            "verified_size_bytes": current_size,
        })
        reference_candidates = {
            str(value)
            for value in (
                parsed.path,
                parsed.virtual_path,
                parsed.host_path,
                Path(parsed.path).name if parsed.path else "",
            )
            if value
        }
        if enforce_publication_reference and (
            not final_content
            or not any(value in final_content for value in reference_candidates)
        ):
            unreferenced.append({
                "artifact_id": parsed.artifact_id,
                "path": parsed.path,
                "reference_candidates": sorted(reference_candidates),
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
        "unreferenced": unreferenced,
        "publication_reference_checked": enforce_publication_reference,
        "evaluation_phase": evaluation_phase,
    }]
    if (
        selected_refs
        and not uncovered_targets
        and len(existing) == len(selected_refs)
        and not missing
        and not changed
        and not invalid
        and not unreferenced
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
    elif unreferenced:
        paths = [str(item.get("path") or "") for item in unreferenced[:5]]
        gap = f"最终回答尚未引用已交付产物：{'；'.join(paths)}"
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
    inherited_raw = harness_context.get("goal_evidence_records")
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
        tool_result_call_ids = {
            str(item.get("tool_call_id") or "")
            for item in evidence
            if item.get("kind") == "tool_result" and item.get("tool_call_id")
        }
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
        has_structured_citations = bool(cited_source_ids)
        explicitly_cited = bool(evidence_source_ids & cited_source_ids) or any(
            url in final_content for url in evidence_urls
        )
        unique_sources: dict[str, dict[str, Any]] = {}
        for source in sources:
            identity = str(source.get("uri") or source.get("source_id") or "")
            if identity:
                unique_sources.setdefault(identity, source)
        source_call_ids = {
            str(source.get("tool_call_id") or "")
            for source in unique_sources.values()
            if source.get("tool_call_id")
        }
        # One source joined to one successful Tool result is already
        # unambiguous in the evidence graph. Inline citations add no lineage
        # information in that topology.
        # With multiple sources, an explicit citation remains necessary to map
        # the published conclusion to the relevant source.
        unambiguous_tool_lineage = (
            len(unique_sources) == 1
            and len(source_call_ids) == 1
            and source_call_ids.issubset(tool_result_call_ids)
            and not has_structured_citations
        )
        if not sources or not (explicitly_cited or unambiguous_tool_lineage):
            return CriterionEvaluation(
                criterion_id=criterion_id,
                name=criterion_id,
                passed=False,
                verifier=VerifierKind.DETERMINISTIC,
                evidence=evidence,
                gap=(
                    "当前 Goal 修订版没有形成可验证的来源链路，或多来源回答没有"
                    "显式引用真实 source_id / 网页链接。"
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
    browser_e2e_required = bool(harness_context.get("browser_e2e_required"))

    def artifact_identity(item: dict[str, Any]) -> tuple[str, str]:
        raw_path = str(item.get("path") or "").strip()
        identity = str(Path(raw_path)) if raw_path else str(item.get("artifact_id") or "")
        return identity, str(item.get("content_sha256") or "")

    run_id = str(harness_context.get("run_id") or "")
    declared_targets = [
        str(item)
        for item in (harness_context.get("declared_artifact_targets") or [])
        if item
    ]
    raw_activations = harness_context.get("verification_activations", [])
    activations = [
        item
        for item in (raw_activations if isinstance(raw_activations, list) else [])
        if isinstance(item, dict)
        and item.get("pack") == "code"
        and item.get("status") in {"succeeded", "failed"}
    ]
    current_write_refs = [
        {
            **evidence_ref,
            "_source_current": True,
            "_activation_created_at": float(
                activation.get("created_at") or 0
            ),
        }
        for activation in activations
        for evidence_ref in activation.get("evidence_refs") or []
        if isinstance(evidence_ref, dict)
        and evidence_ref.get("kind") == "artifact_write"
        and evidence_ref.get("material") is True
    ]
    inherited_raw = harness_context.get("goal_evidence_records")
    inherited = [
        item
        for item in (inherited_raw if isinstance(inherited_raw, list) else [])
        if isinstance(item, dict) and item.get("material") is True
    ]
    inherited_write_refs = [
        {
            **item,
            "_source_inherited": True,
            "_activation_created_at": float(
                item.get("written_at") or item.get("created_at") or 0
            ),
        }
        for item in inherited
        if item.get("kind") == "artifact_write"
    ]
    acceptance_candidates = _artifact_acceptance_set(
        [*current_write_refs, *inherited_write_refs],
        run_id=run_id,
        declared_targets=declared_targets,
    )
    invalid_inherited_writes = [
        item
        for item in acceptance_candidates
        if item.get("_source_inherited")
        and not _artifact_evidence_matches_current_bytes(item, harness_context)
    ]
    acceptance_writes = [
        item
        for item in acceptance_candidates
        if not item.get("_source_inherited")
        or _artifact_evidence_matches_current_bytes(item, harness_context)
    ]
    acceptance_identity = {
        artifact_identity(item)
        for item in acceptance_writes
        if (item.get("path") or item.get("artifact_id"))
        and item.get("content_sha256")
    }
    uncovered_declared = [
        target
        for target in declared_targets
        if not any(
            artifact_path_matches(str(item.get(field) or ""), target)
            for item in acceptance_writes
            for field in ("path", "host_path", "virtual_path")
            if item.get(field)
        )
    ]
    latest_write_at = max(
        (
            float(
                item.get("written_at")
                or item.get("_activation_created_at")
                or 0
            )
            for item in acceptance_writes
        ),
        default=0.0,
    )
    raw_receipts = [
        {
            **evidence_ref,
            "_source_current": True,
            "_activation_created_at": float(
                activation.get("created_at") or 0
            ),
        }
        for activation in activations
        for evidence_ref in activation.get("evidence_refs") or []
        if isinstance(evidence_ref, dict)
        and evidence_ref.get("kind") == "validation_receipt"
        and evidence_ref.get("material") is True
    ]
    raw_receipts.extend(
        {
            **item,
            "_source_inherited": True,
            "_activation_created_at": float(item.get("created_at") or 0),
        }
        for item in inherited
        if item.get("kind") == "validation_receipt"
        and item.get("verification_pack") == "code"
    )
    acceptance_write_timestamps = {
        artifact_identity(item): float(
            item.get("written_at")
            or item.get("_activation_created_at")
            or 0
        )
        for item in acceptance_writes
        if (item.get("path") or item.get("artifact_id"))
        and item.get("content_sha256")
    }

    def _attempted_acceptance_identities(
        activation: dict[str, Any],
    ) -> set[tuple[str, str]]:
        structured = {
            artifact_identity(item)
            for ref in activation.get("evidence_refs") or []
            if isinstance(ref, dict) and ref.get("kind") == "tool_execution"
            for item in ref.get("attempted_artifact_refs") or []
            if isinstance(item, dict)
        }
        structured &= acceptance_identity
        if structured:
            return structured
        input_text = "\n".join(
            str(ref.get("input_preview") or "")
            for ref in activation.get("evidence_refs") or []
            if isinstance(ref, dict) and ref.get("kind") == "tool_execution"
        )
        matched: set[tuple[str, str]] = set()
        for item in acceptance_writes:
            aliases = {
                str(item.get(field) or "").strip()
                for field in ("path", "host_path", "virtual_path")
                if item.get(field)
            }
            if any(alias in input_text for alias in aliases):
                matched.add(artifact_identity(item))
        return matched

    def _attempt_is_current(activation: dict[str, Any]) -> bool:
        created_at = float(activation.get("created_at") or 0)
        identities = _attempted_acceptance_identities(activation)
        if identities:
            return any(
                created_at >= acceptance_write_timestamps.get(identity, 0)
                for identity in identities
            )
        # An unbound attempt cannot prove coverage, but retain a recent one so
        # the control plane reports a protocol error instead of looping.
        return created_at >= latest_write_at

    validation_attempts = [
        evidence_ref
        for activation in activations
        if activation.get("tool_name")
        in {
            "execute",
            "terminal",
            "execute_external_directory",
            "validate_html_report",
        }
        and _attempt_is_current(activation)
        for evidence_ref in activation.get("evidence_refs") or []
        if isinstance(evidence_ref, dict)
        and evidence_ref.get("kind") == "tool_execution"
        and evidence_ref.get("material") is True
    ]
    unreceipted_successful_attempts: list[dict[str, Any]] = []
    for activation in activations:
        if (
            activation.get("tool_name")
            not in {
                "execute",
                "terminal",
                "execute_external_directory",
                "validate_html_report",
            }
            or not _attempt_is_current(activation)
            or not any(
                isinstance(ref, dict) and ref.get("kind") == "tool_result"
                for ref in activation.get("evidence_refs") or []
            )
            or any(
                isinstance(ref, dict) and ref.get("kind") == "validation_receipt"
                for ref in activation.get("evidence_refs") or []
            )
        ):
            continue
        attempted_identities = _attempted_acceptance_identities(activation)
        unreceipted_successful_attempts.extend(
            {
                **execution,
                "activation_created_at": float(
                    activation.get("created_at") or 0
                ),
                "_attempted_artifact_identities": [
                    list(identity) for identity in sorted(attempted_identities)
                ],
            }
            for execution in activation.get("evidence_refs") or []
            if isinstance(execution, dict)
            and execution.get("kind") == "tool_execution"
            and execution.get("material") is True
        )

    relevant_receipts: list[dict[str, Any]] = []
    for receipt in raw_receipts:
        bound_refs = [
            item
            for item in receipt.get("artifact_refs") or []
            if isinstance(item, dict)
            and (item.get("path") or item.get("artifact_id"))
            and item.get("content_sha256")
        ]
        bound_identities = {artifact_identity(item) for item in bound_refs}
        if bound_identities:
            coverage = bound_identities & acceptance_identity
            extra_refs = [
                item
                for item in bound_refs
                if artifact_identity(item) not in acceptance_identity
            ]
            if coverage and all(
                _artifact_evidence_matches_current_bytes(item, harness_context)
                for item in extra_refs
            ):
                relevant_receipts.append(receipt)
            continue
        if (
            receipt.get("_source_current")
            and float(receipt.get("_activation_created_at") or 0)
            >= latest_write_at
        ):
            relevant_receipts.append(receipt)

    passed_receipts = [
        receipt
        for receipt in relevant_receipts
        if str(receipt.get("status") or "passed") == "passed"
        and int(receipt.get("exit_code", -1)) == 0
        and int(receipt.get("checks_failed") or 0) == 0
    ]
    failed_receipts = [
        receipt
        for receipt in relevant_receipts
        if bool(receipt.get("blocking", True))
        and (
            str(receipt.get("status") or "passed") == "failed"
            or int(receipt.get("exit_code", -1)) != 0
            or int(receipt.get("checks_failed") or 0) > 0
        )
    ]
    artifact_failure_receipts = [
        receipt
        for receipt in failed_receipts
        if str(receipt.get("failure_class") or "artifact_failure")
        == "artifact_failure"
        and receipt.get("content_observed") is True
    ]
    control_failure_receipts = [
        receipt
        for receipt in failed_receipts
        if (
            str(receipt.get("failure_class") or "")
            in {"invocation_failure", "infrastructure_failure"}
            or (
                str(receipt.get("failure_class") or "artifact_failure")
                == "artifact_failure"
                and receipt.get("content_observed") is not True
            )
        )
    ]

    def _same_obligation(
        failure: dict[str, Any],
        success: dict[str, Any],
    ) -> bool:
        failure_key = str(
            failure.get("obligation_key")
            or f"{failure.get('validator_kind')}:{failure.get('validator_version')}"
        )
        success_key = str(
            success.get("obligation_key")
            or f"{success.get('validator_kind')}:{success.get('validator_version')}"
        )
        if failure_key != success_key:
            return False
        failure_identities = {
            artifact_identity(item)
            for item in failure.get("artifact_refs") or []
            if isinstance(item, dict)
        }
        success_identities = {
            artifact_identity(item)
            for item in success.get("artifact_refs") or []
            if isinstance(item, dict)
        }
        return (
            not failure_identities
            or failure_identities.issubset(success_identities)
        ) and float(success.get("created_at") or 0) >= float(
            failure.get("created_at") or 0
        )

    unsuperseded_control_failures = [
        failure
        for failure in control_failure_receipts
        if not any(
            _same_obligation(failure, success)
            for success in passed_receipts
        )
    ]

    def _validator_matches_artifact(
        receipt: dict[str, Any],
        artifact: dict[str, Any],
    ) -> bool:
        suffix = Path(str(artifact.get("path") or "")).suffix.lower()
        kind = str(receipt.get("validator_kind") or "")
        if suffix in {".js", ".mjs", ".cjs", ".ts", ".tsx"}:
            return kind in {
                "javascript_syntax",
                "project_test",
                "static_check",
                "browser_runtime",
                "artifact_ui_contract",
            }
        if suffix in {".html", ".htm"}:
            return kind in {
                "html_structure",
                "project_test",
                "static_check",
                "browser_runtime",
                "artifact_ui_contract",
            }
        return kind in {"project_test", "static_check", "json_structure"}

    receipt_identities = {
        artifact_identity(item)
        for receipt in passed_receipts
        for item in receipt.get("artifact_refs") or []
        if isinstance(item, dict)
        and _validator_matches_artifact(receipt, item)
    }
    browser_validated_identities = {
        artifact_identity(item)
        for receipt in passed_receipts
        if receipt.get("validator_kind")
        in {"browser_runtime", "artifact_ui_contract"}
        for item in receipt.get("artifact_refs") or []
        if isinstance(item, dict)
    }
    html_identity = {
        artifact_identity(item)
        for item in acceptance_writes
        if Path(str(item.get("path") or "")).suffix.lower() in {".html", ".htm"}
    }
    missing_validation = acceptance_identity - receipt_identities
    missing_browser = html_identity - browser_validated_identities

    if acceptance_writes and artifact_failure_receipts:
        return CriterionEvaluation(
            criterion_id=criterion_id,
            name=criterion_id,
            passed=False,
            verifier=VerifierKind.DETERMINISTIC,
            evidence=[*acceptance_writes, *artifact_failure_receipts],
            gap=(
                "目标产物存在与当前 hash 绑定的真实内容失败；"
                "后续同 hash 成功不能覆盖该失败，必须修复产物并验证新 hash。"
            ),
            failure_kind=VerificationFailureKind.TASK_GAP,
        )
    if invalid_inherited_writes:
        return CriterionEvaluation(
            criterion_id=criterion_id,
            name=criterion_id,
            passed=False,
            verifier=VerifierKind.DETERMINISTIC,
            evidence=invalid_inherited_writes,
            gap="代码产物 hash 已变化，前序 Run 的验证证据失效，必须重新验证。",
        )
    if uncovered_declared:
        return CriterionEvaluation(
            criterion_id=criterion_id,
            name=criterion_id,
            passed=False,
            verifier=VerifierKind.DETERMINISTIC,
            evidence=acceptance_writes,
            gap=(
                "尚未形成全部声明目标的代码产物证据："
                + "；".join(uncovered_declared)
            ),
        )
    if (
        acceptance_writes
        and not missing_validation
        and (not browser_e2e_required or not missing_browser)
    ):
        return CriterionEvaluation(
            criterion_id=criterion_id,
            name=criterion_id,
            passed=True,
            verifier=VerifierKind.DETERMINISTIC,
            evidence=[*acceptance_writes, *passed_receipts],
        )
    unreceipted_missing_attempts = [
        item
        for item in unreceipted_successful_attempts
        if not item.get("_attempted_artifact_identities")
        or any(
            tuple(identity) in missing_validation
            for identity in item.get("_attempted_artifact_identities") or []
        )
    ]
    if acceptance_writes and unreceipted_missing_attempts:
        return CriterionEvaluation(
            criterion_id=criterion_id,
            name=criterion_id,
            passed=False,
            verifier=VerifierKind.DETERMINISTIC,
            evidence=[
                *acceptance_writes,
                *control_failure_receipts,
                *unreceipted_missing_attempts,
            ],
            gap=(
                "校验命令已经成功执行，但 Harness 未能为目标产物生成 ValidationReceipt。"
                "这是验证协议错误，不应继续修改业务产物。"
            ),
            failure_kind=VerificationFailureKind.VALIDATOR_PROTOCOL_ERROR,
        )
    if acceptance_writes and unsuperseded_control_failures:
        has_infrastructure_failure = any(
            str(item.get("failure_class") or "") == "infrastructure_failure"
            for item in unsuperseded_control_failures
        )
        return CriterionEvaluation(
            criterion_id=criterion_id,
            name=criterion_id,
            passed=False,
            verifier=VerifierKind.DETERMINISTIC,
            evidence=[*acceptance_writes, *unsuperseded_control_failures],
            gap=(
                "验收基础设施执行失败；业务产物不应被要求修改。"
                if has_infrastructure_failure
                else "验收调用参数、路径映射或内容读取证明无效；"
                "请修复验证调用，不要修改业务产物。"
            ),
            failure_kind=(
                VerificationFailureKind.INFRASTRUCTURE_ERROR
                if has_infrastructure_failure
                else VerificationFailureKind.VALIDATOR_PROTOCOL_ERROR
            ),
        )
    if acceptance_writes and validation_attempts and not relevant_receipts:
        return CriterionEvaluation(
            criterion_id=criterion_id,
            name=criterion_id,
            passed=False,
            verifier=VerifierKind.DETERMINISTIC,
            evidence=[*acceptance_writes, *validation_attempts],
            gap=(
                "校验尝试没有形成可判定的 ValidationReceipt。"
                "这是验证协议错误，不应继续修改业务产物。"
            ),
            failure_kind=VerificationFailureKind.VALIDATOR_PROTOCOL_ERROR,
        )
    if acceptance_writes:
        if browser_e2e_required and missing_browser:
            gap = (
                "当前 Run 修改了 HTML 报告，但尚未形成真实浏览器运行验证。"
                "请在目标目录运行 node "
                "/opt/puddingclaw/bin/validate-html-report-e2e.mjs <report.html>，"
                "并保持产物 hash 不再变化。"
            )
        else:
            gap = (
                "尚未成功完成与全部当前产物 hash 绑定的测试、构建或静态检查。"
                "请运行与产物匹配的可执行验证，例如 pytest/ruff、"
                "npm run build 或 node --check <file>。"
            )
    else:
        gap = (
            "未发现与当前代码产物绑定的成功测试、构建或静态检查证据。"
            "可使用 pytest/ruff、npm run build、node --check <file>，"
            "或命名含 validate/check/test 的 Python 校验脚本闭合。"
        )
    return CriterionEvaluation(
        criterion_id=criterion_id,
        name=criterion_id,
        passed=False,
        verifier=VerifierKind.DETERMINISTIC,
        evidence=[*acceptance_writes, *relevant_receipts],
        gap=gap,
    )


def _artifact_evidence_matches_current_bytes(
    ref: dict[str, Any],
    harness_context: dict[str, Any],
) -> bool:
    digest = str(ref.get("content_sha256") or "")
    if not digest.startswith("sha256:"):
        return False
    raw_path = str(ref.get("host_path") or ref.get("path") or "").strip()
    if not raw_path and ref.get("workspace_relative_path"):
        workspace_raw = str(harness_context.get("workspace_path") or "").strip()
        if workspace_raw:
            raw_path = str(
                Path(workspace_raw).expanduser().resolve()
                / str(ref.get("workspace_relative_path"))
            )
    if not raw_path:
        return False
    path = Path(raw_path).expanduser()
    if not path.is_file():
        return False
    expected_size = ref.get("size_bytes")
    if isinstance(expected_size, int) and path.stat().st_size != expected_size:
        return False
    hasher = hashlib.sha256()
    try:
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                hasher.update(chunk)
    except OSError:
        return False
    return f"sha256:{hasher.hexdigest()}" == digest
