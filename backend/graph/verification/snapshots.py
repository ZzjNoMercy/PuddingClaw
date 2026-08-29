"""Frozen input construction and freshness checks for online verification."""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any

from graph.verification.models import (
    ArtifactFingerprint,
    EvaluationInputSnapshot,
    EvaluationSubject,
    EvidenceBinding,
    stable_digest,
)
from graph.verification.transcript_projection import serialize_projected_messages


def snapshot_id_for(
    subject: EvaluationSubject,
    *,
    contract_hash: str,
    transcript_digest: str,
    evidence_digest: str,
    artifact_fingerprints: Iterable[ArtifactFingerprint],
    workspace_fingerprint: str | None,
    transcript_projection_version: str,
    grader_policy_version: str,
    grader_policy_hash: str,
    candidate_message_id: str,
    candidate_content_digest: str,
    candidate_tool_calls_digest: str,
    permission_epoch: int,
) -> str:
    payload = {
        "subject": subject.model_dump(mode="json"),
        "contract_hash": contract_hash,
        "transcript_digest": transcript_digest,
        "evidence_digest": evidence_digest,
        "artifact_fingerprints": [item.model_dump(mode="json") for item in artifact_fingerprints],
        "workspace_fingerprint": workspace_fingerprint,
        "transcript_projection_version": transcript_projection_version,
        "grader_policy_version": grader_policy_version,
        "grader_policy_hash": grader_policy_hash,
        "candidate_message_id": candidate_message_id,
        "candidate_content_digest": candidate_content_digest,
        "candidate_tool_calls_digest": candidate_tool_calls_digest,
        "permission_epoch": permission_epoch,
    }
    return "evaluation-snapshot-" + stable_digest(payload).removeprefix("sha256:")[:24]


def build_evaluation_snapshot(
    *,
    subject: EvaluationSubject,
    contract: dict[str, Any] | None,
    transcript_projection: Any,
    candidate_message_id: str,
    candidate_content: str,
    candidate_tool_calls: Iterable[dict[str, Any]],
    evidence_bindings: Iterable[EvidenceBinding | dict[str, Any]],
    artifact_fingerprints: Iterable[ArtifactFingerprint | dict[str, Any]] = (),
    workspace_fingerprint: str | None = None,
    transcript_projection_version: str = "puddingclaw-grader-transcript-v1",
    grader_policy_version: str = "puddingclaw-online-verification-v1",
    grader_policy: dict[str, Any] | None = None,
    permission_epoch: int = 1,
) -> EvaluationInputSnapshot:
    parsed_bindings = [
        item if isinstance(item, EvidenceBinding) else EvidenceBinding.model_validate(item)
        for item in evidence_bindings
    ]
    parsed_evidence = [item.ref for item in parsed_bindings]
    parsed_artifacts = [
        item if isinstance(item, ArtifactFingerprint) else ArtifactFingerprint.model_validate(item)
        for item in artifact_fingerprints
    ]
    contract_payload = contract or {}
    contract_hash = stable_digest(contract_payload)
    serialized_projection = serialize_projected_messages(transcript_projection)
    transcript_digest = stable_digest(serialized_projection)
    evidence_digest = stable_digest([item.model_dump(mode="json") for item in parsed_bindings])
    candidate_content_digest = stable_digest(candidate_content)
    candidate_tool_calls_payload = list(candidate_tool_calls)
    candidate_tool_calls_digest = stable_digest(candidate_tool_calls_payload)
    grader_policy_hash = stable_digest(grader_policy or {"version": grader_policy_version})
    snapshot_id = snapshot_id_for(
        subject,
        contract_hash=contract_hash,
        transcript_digest=transcript_digest,
        evidence_digest=evidence_digest,
        artifact_fingerprints=parsed_artifacts,
        workspace_fingerprint=workspace_fingerprint,
        transcript_projection_version=transcript_projection_version,
        grader_policy_version=grader_policy_version,
        grader_policy_hash=grader_policy_hash,
        candidate_message_id=candidate_message_id,
        candidate_content_digest=candidate_content_digest,
        candidate_tool_calls_digest=candidate_tool_calls_digest,
        permission_epoch=permission_epoch,
    )
    return EvaluationInputSnapshot(
        snapshot_id=snapshot_id,
        subject=subject,
        contract_id=str(contract_payload.get("contract_id") or "") or None,
        contract_version=str(contract_payload.get("version") or "") or None,
        contract_hash=contract_hash,
        transcript_projection_version=transcript_projection_version,
        transcript_projection=serialized_projection,
        transcript_digest=transcript_digest,
        candidate_message_id=candidate_message_id,
        candidate_content_digest=candidate_content_digest,
        candidate_tool_calls_digest=candidate_tool_calls_digest,
        evidence_refs=parsed_evidence,
        evidence_bindings=parsed_bindings,
        evidence_digest=evidence_digest,
        artifact_fingerprints=parsed_artifacts,
        workspace_fingerprint=workspace_fingerprint,
        grader_policy_version=grader_policy_version,
        grader_policy_hash=grader_policy_hash,
        permission_epoch=permission_epoch,
    )


def file_sha256(path: str | Path) -> str | None:
    try:
        hasher = hashlib.sha256()
        with Path(path).open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                hasher.update(chunk)
    except (OSError, RuntimeError, ValueError):
        return None
    return f"sha256:{hasher.hexdigest()}"


def stale_artifact_reasons(
    snapshot: EvaluationInputSnapshot,
    *,
    digest_resolver: Callable[[ArtifactFingerprint], str | None] | None = None,
) -> list[str]:
    resolver = digest_resolver or (lambda item: file_sha256(item.path) if item.path else None)
    reasons: list[str] = []
    for artifact in snapshot.artifact_fingerprints:
        observed = resolver(artifact)
        if observed is None:
            reasons.append(f"artifact_unobservable:{artifact.artifact_id}")
        elif observed != artifact.content_sha256:
            reasons.append(f"artifact_changed:{artifact.artifact_id}")
    return reasons
