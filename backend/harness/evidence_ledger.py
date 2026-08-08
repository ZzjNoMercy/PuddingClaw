"""Stable evidence identities and authoritative Session-local records.

Run-local activations may retain rich evidence payloads for Trace and current-
Run checks.  Cross-Run state must carry only ``{type, id}`` references and
resolve them against this ledger.  This keeps compaction and handoff from
silently dropping lineage fields.
"""

from __future__ import annotations

import hashlib
import json
import time
from copy import deepcopy
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field


class EvidenceRef(BaseModel):
    """The only evidence shape legal in Goal/Handoff/compact state."""

    type: str = Field(min_length=1)
    id: str = Field(min_length=1)


class EvidenceRecord(BaseModel):
    """Backend-authored, immutable-lineage evidence stored in Session JSON."""

    id: str
    kind: str
    source_run_id: str
    source_query_id: str
    origin_tool_call_id: str
    origin_tool_name: str = ""
    verification_pack: str = ""
    goal_id: str | None = None
    goal_revision: int | None = None
    output_digest: str
    result_id: str | None = None
    query_trace_id: str | None = None
    generation_id: str | None = None
    sql_submission_id: str | None = None
    evidence_search_id: str | None = None
    sql_validation_receipt_id: str | None = None
    artifact_id: str | None = None
    validation_receipt_id: str | None = None
    receipt_id: str | None = None
    content_sha256: str | None = None
    profile_revisions: list[str] = Field(default_factory=list)
    source_id: str | None = None
    uri: str | None = None
    status: Literal["active", "stale", "revoked", "deleted"] = "active"
    inheritable: bool = False
    non_inheritable_reason: str | None = None
    payload: dict[str, Any] = Field(default_factory=dict)
    created_at: float = Field(default_factory=time.time)

    @property
    def ref(self) -> EvidenceRef:
        return EvidenceRef(type=self.kind, id=self.id)


def is_evidence_ref(value: Any) -> bool:
    return (
        isinstance(value, dict)
        and set(value) == {"type", "id"}
        and bool(str(value.get("type") or "").strip())
        and bool(str(value.get("id") or "").strip())
    )


def ref_key(ref: EvidenceRef | dict[str, Any]) -> str:
    parsed = ref if isinstance(ref, EvidenceRef) else EvidenceRef.model_validate(ref)
    return f"{parsed.type}:{parsed.id}"


def _digest(payload: dict[str, Any]) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"


def _file_digest(path: Path) -> str | None:
    try:
        hasher = hashlib.sha256()
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                hasher.update(chunk)
    except (OSError, ValueError):
        return None
    return f"sha256:{hasher.hexdigest()}"


def _first(values: list[dict[str, Any]], *keys: str) -> str | None:
    for item in values:
        for key in keys:
            value = str(item.get(key) or "").strip()
            if value:
                return value
    return None


def _identity(raw: dict[str, Any]) -> tuple[str, str] | None:
    kind = str(raw.get("kind") or "")
    # Wrapper receipts may carry nested validation ids for audit convenience.
    # The explicit kind owns the identity; incidental fields must not steal it.
    if kind == "external_mutation_completed" and raw.get("receipt_id"):
        return "external_mutation", str(raw["receipt_id"])
    if kind == "validation_receipt" and raw.get("validation_receipt_id"):
        return "validation_receipt", str(raw["validation_receipt_id"])
    if raw.get("result_id"):
        return "analytics_result", str(raw["result_id"])
    if kind == "analytics_result" and raw.get("ref"):
        # Legacy adapters did not retain whether ``ref`` was a result, trace,
        # or source id.  Keep it auditable but never silently promote it to a
        # reusable analytics result.
        return "legacy_evidence", str(raw["ref"])
    if raw.get("sql_validation_receipt_id"):
        return "sql_validation", str(raw["sql_validation_receipt_id"])
    if raw.get("evidence_search_id"):
        return "database_evidence", str(raw["evidence_search_id"])
    if raw.get("sql_submission_id"):
        return "sql_submission", str(raw["sql_submission_id"])
    if raw.get("validation_receipt_id"):
        return "validation_receipt", str(raw["validation_receipt_id"])
    if raw.get("artifact_id"):
        return "artifact", str(raw["artifact_id"])
    if raw.get("generation_id"):
        return "sql_generation", str(raw["generation_id"])
    if raw.get("source_id"):
        return "web_source", str(raw["source_id"])
    if kind in {"tool_result", "tool_execution"} and raw.get("tool_call_id"):
        return "tool_result", str(raw["tool_call_id"])
    return None


def _record_for_raw(
    *,
    raw: dict[str, Any],
    evidence: list[dict[str, Any]],
    run: dict[str, Any],
    activation: dict[str, Any],
) -> EvidenceRecord | None:
    identity = _identity(raw)
    if identity is None:
        return None
    kind, evidence_id = identity
    tool_call_id = str(raw.get("tool_call_id") or activation.get("tool_call_id") or "")
    output_digest = str(raw.get("output_digest") or _first(evidence, "output_digest") or "")
    if not output_digest:
        output_digest = _digest(raw)
    result_id = str(raw.get("result_id") or "") or None
    generation_id = str(raw.get("generation_id") or _first(evidence, "generation_id") or "") or None
    sql_submission_id = str(raw.get("sql_submission_id") or _first(evidence, "sql_submission_id") or "") or None
    evidence_search_id = str(raw.get("evidence_search_id") or _first(evidence, "evidence_search_id") or "") or None
    sql_validation_receipt_id = (
        str(raw.get("sql_validation_receipt_id") or _first(evidence, "sql_validation_receipt_id") or "") or None
    )
    explicit_trace_id = (
        str(raw.get("query_trace_id") or raw.get("trace_id") or _first(evidence, "query_trace_id", "trace_id") or "")
        or None
    )
    # Every successful Tool activation has a server-authored execution trace,
    # even when an upstream database adapter did not expose its own trace id.
    query_trace_id = explicit_trace_id or (
        "tool-trace-" + hashlib.sha256(f"{run.get('run_id')}:{tool_call_id}:{output_digest}".encode()).hexdigest()[:24]
        if tool_call_id
        else None
    )
    material = bool(
        raw.get("material", True) is not False and raw.get("role") != "temporary" and raw.get("scope") != "scratch"
    )
    base_lineage = bool(
        material
        and run.get("run_id")
        and run.get("query_id")
        and tool_call_id
        and output_digest
        and activation.get("status") == "succeeded"
    )
    inheritable = base_lineage
    reason: str | None = None
    if kind == "analytics_result":
        inheritable = bool(base_lineage and result_id and query_trace_id)
        if not inheritable:
            reason = "analytics_lineage_incomplete"
    elif kind == "legacy_evidence":
        inheritable = False
        reason = "legacy_reference_kind_unknown"
    elif kind == "artifact":
        inheritable = bool(base_lineage and raw.get("content_sha256"))
        if not inheritable:
            reason = "artifact_digest_missing"
    elif kind == "validation_receipt":
        inheritable = bool(base_lineage and raw.get("artifact_refs"))
        if not inheritable:
            reason = "validation_artifact_binding_missing"
    elif kind == "web_source":
        inheritable = bool(base_lineage and raw.get("uri"))
        if not inheritable:
            reason = "web_source_uri_missing"
    elif not inheritable:
        reason = "origin_lineage_incomplete"
    return EvidenceRecord(
        id=evidence_id,
        kind=kind,
        source_run_id=str(run.get("run_id") or ""),
        source_query_id=str(run.get("query_id") or ""),
        origin_tool_call_id=tool_call_id,
        origin_tool_name=str(activation.get("tool_name") or raw.get("tool_name") or ""),
        verification_pack=str(activation.get("pack") or ""),
        goal_id=str(run.get("goal_id") or "") or None,
        goal_revision=(int(run.get("goal_revision")) if run.get("goal_revision") is not None else None),
        output_digest=output_digest,
        result_id=result_id,
        query_trace_id=query_trace_id,
        generation_id=generation_id,
        sql_submission_id=sql_submission_id,
        evidence_search_id=evidence_search_id,
        sql_validation_receipt_id=sql_validation_receipt_id,
        artifact_id=str(raw.get("artifact_id") or "") or None,
        validation_receipt_id=str(raw.get("validation_receipt_id") or "") or None,
        receipt_id=str(raw.get("receipt_id") or "") or None,
        content_sha256=str(raw.get("content_sha256") or raw.get("after_sha256") or "") or None,
        profile_revisions=[str(item) for item in raw.get("profile_revisions") or [] if str(item)],
        source_id=str(raw.get("source_id") or "") or None,
        uri=str(raw.get("uri") or "") or None,
        status=(
            str(raw.get("status"))
            if str(raw.get("status") or "") in {"active", "stale", "revoked", "deleted"}
            else "active"
        ),
        inheritable=inheritable,
        non_inheritable_reason=reason,
        payload=deepcopy(raw),
    )


def register_activation_evidence(
    data: dict[str, Any],
    *,
    run: dict[str, Any],
    activation: dict[str, Any],
) -> list[dict[str, str]]:
    """Register rich activation evidence and return stable references."""

    evidence = [dict(item) for item in activation.get("evidence_refs") or [] if isinstance(item, dict)]
    ledger = data.setdefault("evidence_ledger", {})
    conflicts = data.setdefault("evidence_ledger_conflicts", [])
    stable: dict[str, dict[str, str]] = {}
    for raw in evidence:
        record = _record_for_raw(
            raw=raw,
            evidence=evidence,
            run=run,
            activation=activation,
        )
        if record is None:
            continue
        key = ref_key(record.ref)
        existing = ledger.get(key)
        if isinstance(existing, dict):
            prior = EvidenceRecord.model_validate(existing)
            immutable_identity = (
                prior.source_run_id,
                prior.origin_tool_call_id,
                prior.output_digest,
            )
            incoming_identity = (
                record.source_run_id,
                record.origin_tool_call_id,
                record.output_digest,
            )
            if immutable_identity != incoming_identity:
                conflicts.append(
                    {
                        "key": key,
                        "reason": "immutable_identity_conflict",
                        "existing": list(immutable_identity),
                        "incoming": list(incoming_identity),
                        "observed_at": time.time(),
                    }
                )
                continue
            if str(prior.payload.get("kind") or "") == "tool_execution" and str(raw.get("kind") or "") == "tool_result":
                # ``_identity`` keys the attempt wrapper (tool_execution) and
                # the real result (tool_result) under the same stable
                # ``tool_result:<call_id>`` key, and the wrapper is emitted
                # first.  Upgrade in place: evaluators need the result's
                # output summary, not the attempt envelope.
                ledger[key] = record.model_dump(mode="json")
            else:
                record = prior
        else:
            ledger[key] = record.model_dump(mode="json")
        if raw.get("material", True) is not False:
            stable[key] = record.ref.model_dump(mode="json")
    return list(stable.values())


def repair_legacy_validation_wrapper_records(data: dict[str, Any]) -> bool:
    """Repair receipts whose mutation wrapper stole a validation identity.

    Older sessions registered ``external_mutation_completed`` before its
    nested ``validation_receipt`` and keyed the wrapper as
    ``validation_receipt:<id>``. Rebuild that entry from the authoritative
    activation payload so cross-Run inheritance sees the real artifact refs.
    """

    ledger = data.get("evidence_ledger")
    harness = data.get("harness")
    runs = harness.get("runs") if isinstance(harness, dict) else None
    if not isinstance(ledger, dict) or not isinstance(runs, dict):
        return False
    changed = False
    for key, stored in list(ledger.items()):
        if not (
            key.startswith("validation_receipt:")
            and isinstance(stored, dict)
            and isinstance(stored.get("payload"), dict)
            and stored["payload"].get("kind") == "external_mutation_completed"
        ):
            continue
        validation_id = key.split(":", 1)[1]
        source_run = runs.get(str(stored.get("source_run_id") or ""))
        if not isinstance(source_run, dict):
            continue
        matching_activations = [
            item
            for item in source_run.get("verification_activations") or []
            if isinstance(item, dict)
            and item.get("status") == "succeeded"
            and str(item.get("tool_call_id") or "") == str(stored.get("origin_tool_call_id") or "")
            and any(
                isinstance(ref, dict)
                and ref.get("kind") == "validation_receipt"
                and str(ref.get("validation_receipt_id") or "") == validation_id
                for ref in item.get("evidence_refs") or []
            )
        ]
        activation = min(
            matching_activations,
            key=lambda item: (
                item.get("pack") != "code",
                item.get("pack") != stored.get("verification_pack"),
            ),
            default=None,
        )
        if not isinstance(activation, dict):
            continue
        evidence = [dict(item) for item in activation.get("evidence_refs") or [] if isinstance(item, dict)]
        nested = next(
            (
                item
                for item in evidence
                if item.get("kind") == "validation_receipt"
                and str(item.get("validation_receipt_id") or "") == validation_id
            ),
            None,
        )
        if nested is None:
            continue
        rebuilt = _record_for_raw(
            raw=nested,
            evidence=evidence,
            run=source_run,
            activation=activation,
        )
        if rebuilt is None:
            continue
        rebuilt.created_at = float(stored.get("created_at") or rebuilt.created_at)
        ledger[key] = rebuilt.model_dump(mode="json")
        mutation: EvidenceRecord | None = None
        stored_wrapper = stored.get("payload")
        mutation_raw = (
            dict(stored_wrapper)
            if isinstance(stored_wrapper, dict)
            and stored_wrapper.get("kind") == "external_mutation_completed"
            and str(stored_wrapper.get("validation_receipt_id") or "") == validation_id
            else next(
                (
                    item
                    for item in evidence
                    if item.get("kind") == "external_mutation_completed"
                    and item.get("receipt_id")
                    and str(item.get("validation_receipt_id") or "") == validation_id
                ),
                None,
            )
        )
        if mutation_raw is not None:
            mutation = _record_for_raw(
                raw=mutation_raw,
                evidence=evidence,
                run=source_run,
                activation=activation,
            )
            if mutation is not None:
                ledger.setdefault(
                    ref_key(mutation.ref),
                    mutation.model_dump(mode="json"),
                )
        stable_refs = [item for item in activation.get("stable_evidence_refs") or [] if is_evidence_ref(item)]
        for ref in (
            rebuilt.ref,
            mutation.ref if mutation is not None else None,
        ):
            if ref is not None:
                stable_refs.append(ref.model_dump(mode="json"))
        activation["stable_evidence_refs"] = list(
            {ref_key(item): EvidenceRef.model_validate(item).model_dump(mode="json") for item in stable_refs}.values()
        )
        changed = True
    return changed


def repair_legacy_tool_execution_records(data: dict[str, Any]) -> bool:
    """Replace attempt wrappers that shadowed the authoritative Tool result.

    ``tool_execution`` and ``tool_result`` intentionally share one stable
    ``tool_result:<call_id>`` identity.  Older registration kept the first
    record, which is the attempt wrapper, and silently discarded the following
    result envelope.  Rebuild those entries from the originating activation so
    cross-Run evaluators recover the output summary and analytics lineage.
    """

    ledger = data.get("evidence_ledger")
    harness = data.get("harness")
    runs = harness.get("runs") if isinstance(harness, dict) else None
    if not isinstance(ledger, dict) or not isinstance(runs, dict):
        return False

    changed = False
    for key, stored in list(ledger.items()):
        if not (
            key.startswith("tool_result:")
            and isinstance(stored, dict)
            and isinstance(stored.get("payload"), dict)
            and stored["payload"].get("kind") == "tool_execution"
        ):
            continue

        tool_call_id = key.split(":", 1)[1]
        source_run = runs.get(str(stored.get("source_run_id") or ""))
        if not isinstance(source_run, dict):
            continue
        matching_activations = [
            item
            for item in source_run.get("verification_activations") or []
            if isinstance(item, dict)
            and item.get("status") == "succeeded"
            and str(item.get("tool_call_id") or "") == tool_call_id
            and any(
                isinstance(ref, dict)
                and ref.get("kind") == "tool_result"
                and str(ref.get("tool_call_id") or "") == tool_call_id
                for ref in item.get("evidence_refs") or []
            )
        ]
        activation = min(
            matching_activations,
            key=lambda item: item.get("pack") != stored.get("verification_pack"),
            default=None,
        )
        if not isinstance(activation, dict):
            continue
        evidence = [dict(item) for item in activation.get("evidence_refs") or [] if isinstance(item, dict)]
        result_raw = next(
            (
                item
                for item in evidence
                if item.get("kind") == "tool_result" and str(item.get("tool_call_id") or "") == tool_call_id
            ),
            None,
        )
        if result_raw is None:
            continue
        rebuilt = _record_for_raw(
            raw=result_raw,
            evidence=evidence,
            run=source_run,
            activation=activation,
        )
        if rebuilt is None or ref_key(rebuilt.ref) != key:
            continue
        rebuilt.created_at = float(stored.get("created_at") or rebuilt.created_at)
        ledger[key] = rebuilt.model_dump(mode="json")
        stable_refs = [item for item in activation.get("stable_evidence_refs") or [] if is_evidence_ref(item)]
        stable_refs.append(rebuilt.ref.model_dump(mode="json"))
        activation["stable_evidence_refs"] = list(
            {ref_key(item): EvidenceRef.model_validate(item).model_dump(mode="json") for item in stable_refs}.values()
        )
        changed = True
    return changed


def resolve_evidence_ref(
    data: dict[str, Any],
    ref: EvidenceRef | dict[str, Any],
    *,
    goal_id: str | None = None,
    goal_revision: int | None = None,
    require_inheritable: bool = True,
    allow_artifact_revision_inheritance: bool = False,
) -> EvidenceRecord | None:
    """Resolve and re-validate one stable reference against current authority."""

    parsed = ref if isinstance(ref, EvidenceRef) else EvidenceRef.model_validate(ref)
    ledger = data.get("evidence_ledger")
    raw = ledger.get(ref_key(parsed)) if isinstance(ledger, dict) else None
    if not isinstance(raw, dict):
        return None
    record = EvidenceRecord.model_validate(raw)
    if record.status != "active" or (require_inheritable and not record.inheritable):
        return None
    if goal_id is not None and record.goal_id != goal_id:
        return None
    if goal_revision is not None and record.goal_revision != goal_revision:
        if not (
            allow_artifact_revision_inheritance
            and record.kind in {"artifact", "validation_receipt", "external_mutation"}
            and record.goal_revision is not None
            and record.goal_revision < goal_revision
        ):
            return None
    harness = data.get("harness")
    runs = harness.get("runs") if isinstance(harness, dict) else None
    source_run = runs.get(record.source_run_id) if isinstance(runs, dict) else None
    if not isinstance(source_run, dict) or record.source_query_id != str(source_run.get("query_id") or ""):
        return None
    activations = source_run.get("verification_activations")
    origin = next(
        (
            item
            for item in (activations if isinstance(activations, list) else [])
            if isinstance(item, dict)
            if item.get("status") == "succeeded" and str(item.get("tool_call_id") or "") == record.origin_tool_call_id
        ),
        None,
    )
    if not isinstance(origin, dict):
        return None
    stable_refs = origin.get("stable_evidence_refs")
    if not isinstance(stable_refs, list) or parsed.model_dump(mode="json") not in stable_refs:
        return None
    if record.kind == "artifact":
        registry = data.get("delivered_artifacts")
        artifacts = list(registry.values()) if isinstance(registry, dict) else []
        target_path = str(record.payload.get("host_path") or record.payload.get("path") or "")
        artifact = next(
            (
                item
                for item in artifacts
                if isinstance(item, dict)
                and (
                    str(item.get("artifact_id") or "") == str(record.artifact_id or "")
                    or (
                        target_path
                        and str(item.get("target_path") or "") == target_path
                        and item.get("content_sha256") == record.content_sha256
                    )
                )
            ),
            None,
        )
        if isinstance(artifact, dict):
            if str(artifact.get("status") or "active") != "active":
                return None
            if record.content_sha256 and artifact.get("content_sha256") != record.content_sha256:
                return None
        else:
            # HostFileBroker writes are already committed directly to their
            # authorized target and therefore do not pass through the legacy
            # delivered-artifact registry. Re-resolve those receipts against
            # the current target bytes instead of forcing a staging lease back
            # into the architecture.
            authorized = record.payload.get("authorized") is True
            grant_id = str(record.payload.get("permission_grant_id") or "")
            # Workspace writes carry no permission grant: the workspace
            # backend itself is the write authority, recorded as
            # ``authority_kind == "workspace"`` at write time. Requiring a
            # grant id here silently dropped every workspace-authored
            # artifact from goal evidence, which made the code-validation
            # acceptance set permanently empty.
            workspace_authority = str(record.payload.get("authority_kind") or "") == "workspace"
            if not authorized or not (grant_id or workspace_authority) or not target_path:
                return None
            current_digest = _file_digest(Path(target_path).expanduser())
            if not current_digest or current_digest != record.content_sha256:
                return None
    return record


def migrate_legacy_refs(
    data: dict[str, Any],
    refs: list[Any],
    *,
    goal_id: str,
    goal_revision: int,
) -> list[dict[str, str]]:
    """Migrate complete legacy payloads; audit incomplete records as non-inheritable."""

    migrated: dict[str, dict[str, str]] = {}
    audit = data.setdefault("evidence_migration_audit", [])
    harness = data.get("harness")
    runs = harness.get("runs") if isinstance(harness, dict) else None
    for raw in refs:
        if is_evidence_ref(raw):
            resolved = resolve_evidence_ref(
                data,
                raw,
                goal_id=goal_id,
                goal_revision=goal_revision,
            )
            if resolved is not None:
                migrated[ref_key(raw)] = EvidenceRef.model_validate(raw).model_dump(mode="json")
            continue
        if not isinstance(raw, dict):
            continue
        source_run_id = str(raw.get("origin_run_id") or raw.get("run_id") or "")
        source_run = runs.get(source_run_id) if isinstance(runs, dict) else None
        tool_call_id = str(raw.get("tool_call_id") or "")
        migrated_before = len(migrated)
        if isinstance(source_run, dict) and tool_call_id:
            activation = next(
                (
                    item
                    for item in source_run.get("verification_activations") or []
                    if isinstance(item, dict)
                    and str(item.get("tool_call_id") or "") == tool_call_id
                    and item.get("status") == "succeeded"
                ),
                None,
            )
            if isinstance(activation, dict):
                candidate = dict(activation)
                candidate["evidence_refs"] = [raw]
                registered = register_activation_evidence(
                    data,
                    run=source_run,
                    activation=candidate,
                )
                existing_stable = [
                    item for item in activation.get("stable_evidence_refs") or [] if is_evidence_ref(item)
                ]
                activation["stable_evidence_refs"] = list(
                    {
                        ref_key(item): EvidenceRef.model_validate(item).model_dump(mode="json")
                        for item in [*existing_stable, *registered]
                    }.values()
                )
                for ref in registered:
                    resolved = resolve_evidence_ref(
                        data,
                        ref,
                        goal_id=goal_id,
                        goal_revision=goal_revision,
                    )
                    if resolved is not None:
                        migrated[ref_key(ref)] = ref
        if len(migrated) == migrated_before:
            audit.append(
                {
                    "status": "non_inheritable",
                    "reason": "legacy_origin_lineage_incomplete",
                    "legacy_digest": _digest(raw),
                    "goal_id": goal_id,
                    "goal_revision": goal_revision,
                    "observed_at": time.time(),
                }
            )
    return list(migrated.values())


__all__ = [
    "EvidenceRecord",
    "EvidenceRef",
    "is_evidence_ref",
    "migrate_legacy_refs",
    "repair_legacy_tool_execution_records",
    "repair_legacy_validation_wrapper_records",
    "ref_key",
    "register_activation_evidence",
    "resolve_evidence_ref",
]
