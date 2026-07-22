"""Optimistic, versioned file patch tools for DeepAgents."""

import hashlib
import json
import logging
import posixpath
import re
import time
import unicodedata
from pathlib import Path, PurePosixPath
from typing import Any

from langchain.agents.middleware.types import AgentMiddleware
from langchain.tools import ToolRuntime
from langchain_core.messages import ToolMessage
from langchain_core.tools import StructuredTool
from pydantic import BaseModel, Field, model_validator

from observability import emit_harness_metric

logger = logging.getLogger(__name__)

EXTERNAL_ARTIFACT_LEASE_TTL_SECONDS = 6 * 60 * 60


class ReplacementHunk(BaseModel):
    old_string: str
    new_string: str
    replace_all: bool = False

    @model_validator(mode="after")
    def strings_differ(self) -> "ReplacementHunk":
        if self.old_string == self.new_string:
            raise ValueError("old_string and new_string must differ")
        return self


class InspectFileVersionInput(BaseModel):
    file_path: str


class PatchFileInput(BaseModel):
    file_path: str
    expected_sha256: str = Field(
        description="sha256:<hex> returned by inspect_file_version for the exact source version"
    )
    replacements: list[ReplacementHunk] = Field(min_length=1, max_length=100)


class StageExternalArtifactInput(BaseModel):
    file_path: str = Field(description="Approved absolute external source path")


class CommitExternalArtifactInput(BaseModel):
    lease_id: str
    file_path: str = Field(description="Exact external target bound to the lease")
    expected_source_sha256: str | None = Field(
        default=None,
        description=(
            "Optional lease source hash. Omit it to use the immutable source hash recorded by the lease; "
            "this is not the edited staged-file hash."
        ),
    )
    expected_draft_sha256: str | None = Field(
        default=None,
        description=(
            "Edited staged-file hash. Required for code-like artifacts and must match the exact "
            "bytes covered by validation_receipt_ids."
        ),
    )
    validation_receipt_ids: list[str] = Field(
        default_factory=list,
        description="Server-persisted ValidationReceipt ids authorizing this exact target/draft hash.",
    )


class UpsertScratchFileInput(BaseModel):
    file_path: str = Field(description="Exact /scratch path to create or atomically replace")
    content: str
    expected_sha256: str | None = Field(
        default=None,
        description="Required when replacing an existing scratch file; omit only when creating it",
    )


class ValidateArtifactContractInput(BaseModel):
    contract_id: str = Field(description="Registered deterministic artifact contract id")
    html_file_path: str
    javascript_file_path: str


def _read_all(backend: Any, file_path: str) -> tuple[str | None, str | None]:
    chunks: list[str] = []
    offset = 0
    while True:
        result = backend.read(file_path, offset=offset, limit=2000)
        if result.error:
            if chunks and "exceeds file length" in result.error:
                break
            return None, result.error
        data = result.file_data or {}
        if data.get("encoding") != "utf-8":
            return None, f"Versioned patch only supports UTF-8 text files: {file_path}"
        chunk = str(data.get("content") or "")
        chunks.append(chunk)
        line_count = len(chunk.splitlines())
        if line_count < 2000:
            break
        offset += line_count
    return "".join(chunks), None


def _digest(content: str) -> str:
    return f"sha256:{hashlib.sha256(content.encode('utf-8')).hexdigest()}"


def _safe_staged_filename(value: str) -> str:
    """Preserve readable Unicode names while removing path/control syntax."""

    normalized = unicodedata.normalize("NFC", value).replace("/", "_").replace("\\", "_")
    normalized = "".join(character for character in normalized if not unicodedata.category(character).startswith("C"))
    return re.sub(r"[^\w.\-]+", "_", normalized, flags=re.UNICODE).strip("._") or "artifact.txt"


_CODE_LIKE_SUFFIXES = frozenset(
    {
        ".c", ".cc", ".cpp", ".cs", ".css", ".dart", ".go", ".h", ".hpp",
        ".htm", ".html", ".java", ".js", ".jsx", ".cjs", ".mjs", ".kt", ".kts",
        ".php", ".py", ".rb", ".rs", ".scss", ".sh", ".sql", ".swift", ".ts",
        ".tsx", ".cts", ".mts", ".vue",
    }
)


def _code_like_target(file_path: str) -> bool:
    name = PurePosixPath(file_path.replace("\\", "/")).name.lower()
    return any(name.endswith(suffix) for suffix in _CODE_LIKE_SUFFIXES)


def _receipt_passed(receipt: dict[str, Any]) -> bool:
    return (
        str(receipt.get("status") or "passed") == "passed"
        and int(receipt.get("exit_code", -1)) == 0
        and int(receipt.get("checks_failed") or 0) == 0
    )


def _receipt_authorizes_commit(receipt: dict[str, Any]) -> bool:
    """Return whether Harness vouches for this validator as a commit gate."""

    return bool(receipt.get("commit_authority")) and _receipt_passed(receipt)


def _receipt_obligation_key(receipt: dict[str, Any]) -> str:
    explicit = str(receipt.get("obligation_key") or "").strip()
    if explicit:
        return explicit
    return (
        f"{str(receipt.get('validator_kind') or 'unknown')}:"
        f"{str(receipt.get('validator_version') or 'unknown')}"
    )


def _accepted_receipts_for_target(
    receipts: list[dict[str, Any]],
    *,
    target_path: str,
    content_sha256: str,
    selected_receipt_ids: set[str],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Resolve accepted successes and still-active failures per obligation.

    Only Harness-controlled validators participate in the publication gate.
    A later success clears a failure only for the same semantic obligation;
    syntax, project tests and UI contracts remain independent facts.
    """

    matching = [
        receipt
        for receipt in receipts
        if bool(receipt.get("commit_authority"))
        and _receipt_matches_target_hash(
            receipt,
            target_path=target_path,
            content_sha256=content_sha256,
        )
    ]
    accepted = [
        receipt
        for receipt in matching
        if str(receipt.get("validation_receipt_id") or "") in selected_receipt_ids
        and _receipt_authorizes_commit(receipt)
    ]
    latest_success_by_obligation: dict[str, float] = {}
    latest_failure_by_obligation: dict[str, tuple[float, dict[str, Any]]] = {}
    for receipt in matching:
        key = _receipt_obligation_key(receipt)
        created_at = float(receipt.get("created_at") or 0)
        if _receipt_passed(receipt):
            latest_success_by_obligation[key] = max(
                latest_success_by_obligation.get(key, 0.0), created_at
            )
        elif bool(receipt.get("blocking", True)):
            previous = latest_failure_by_obligation.get(key)
            if previous is None or created_at >= previous[0]:
                latest_failure_by_obligation[key] = (created_at, receipt)
    blocking = [
        receipt
        for key, (failed_at, receipt) in latest_failure_by_obligation.items()
        if failed_at >= latest_success_by_obligation.get(key, 0.0)
    ]
    return accepted, blocking


def _receipt_matches_target_hash(
    receipt: dict[str, Any],
    *,
    target_path: str,
    content_sha256: str,
) -> bool:
    normalized_target = str(PurePosixPath(target_path.replace("\\", "/")))
    return any(
        isinstance(ref, dict)
        and str(ref.get("content_sha256") or "") == content_sha256
        and str(PurePosixPath(str(ref.get("path") or "").replace("\\", "/")))
        == normalized_target
        for ref in receipt.get("artifact_refs") or []
    )


def _persisted_validation_receipts(
    session_manager: Any,
    *,
    session_id: str,
    run_id: str,
    goal_id: str,
    goal_revision: Any,
) -> list[dict[str, Any]]:
    receipts: dict[str, dict[str, Any]] = {}

    def collect(refs: Any) -> None:
        for ref in refs if isinstance(refs, list) else []:
            if not isinstance(ref, dict) or ref.get("kind") != "validation_receipt":
                continue
            receipt_id = str(ref.get("validation_receipt_id") or "")
            if receipt_id:
                receipts[receipt_id] = ref

    run = session_manager.get_run_state(session_id, run_id)
    for activation in run.get("verification_activations") or [] if isinstance(run, dict) else []:
        if isinstance(activation, dict):
            collect(activation.get("evidence_refs"))
    if goal_id:
        goal = session_manager.get_goal_state(session_id, goal_id)
        if isinstance(goal, dict) and goal.get("objective_revision") == goal_revision:
            collect(goal.get("evidence_refs"))
    return list(receipts.values())


class VersionedPatchMiddleware(AgentMiddleware[Any, Any, Any]):
    """Add inspect+patch tools backed by optimistic source-version checks."""

    def __init__(self, backend: Any) -> None:
        super().__init__()
        self.backend = backend

        def inspect_file_version(
            file_path: str,
            runtime: ToolRuntime[Any, Any],
        ) -> ToolMessage:
            content, error = _read_all(self.backend, file_path)
            if error is not None or content is None:
                return ToolMessage(
                    content=f"Error: {error or 'unable to read file'}",
                    name="inspect_file_version",
                    tool_call_id=runtime.tool_call_id,
                    status="error",
                )
            return ToolMessage(
                content=(
                    f"file_path: {file_path}\nversion: {_digest(content)}\n"
                    f"size_chars: {len(content)}\ncontent:\n{content}"
                ),
                name="inspect_file_version",
                tool_call_id=runtime.tool_call_id,
                status="success",
            )

        def patch_file(
            file_path: str,
            expected_sha256: str,
            replacements: list[ReplacementHunk],
            runtime: ToolRuntime[Any, Any],
        ) -> ToolMessage:
            original, error = _read_all(self.backend, file_path)
            if error is not None or original is None:
                return ToolMessage(
                    content=f"Error: {error or 'unable to read file'}",
                    name="patch_file",
                    tool_call_id=runtime.tool_call_id,
                    status="error",
                )
            current_version = _digest(original)
            if expected_sha256 != current_version:
                return ToolMessage(
                    content=(
                        "Patch conflict: source version changed. "
                        f"expected={expected_sha256}, current={current_version}. "
                        "Call inspect_file_version again and rebase the patch."
                    ),
                    name="patch_file",
                    tool_call_id=runtime.tool_call_id,
                    status="error",
                )
            updated = original
            applied = 0
            for index, hunk in enumerate(replacements, start=1):
                occurrences = updated.count(hunk.old_string)
                if occurrences == 0:
                    return ToolMessage(
                        content=(
                            f"Patch conflict at hunk {index}: old_string is absent from "
                            f"version {current_version}. Re-inspect and rebase instead of retrying guesses."
                        ),
                        name="patch_file",
                        tool_call_id=runtime.tool_call_id,
                        status="error",
                    )
                if not hunk.replace_all and occurrences != 1:
                    return ToolMessage(
                        content=(
                            f"Patch conflict at hunk {index}: old_string occurs {occurrences} times; "
                            "make the hunk unique or set replace_all=true."
                        ),
                        name="patch_file",
                        tool_call_id=runtime.tool_call_id,
                        status="error",
                    )
                updated = updated.replace(
                    hunk.old_string,
                    hunk.new_string,
                    -1 if hunk.replace_all else 1,
                )
                applied += occurrences if hunk.replace_all else 1
            # Commit with the entire inspected source as the compare-and-swap
            # precondition. A concurrent write becomes an exact-match conflict.
            result = self.backend.edit(file_path, original, updated, replace_all=False)
            if result.error:
                return ToolMessage(
                    content=(
                        f"Patch commit conflict for {file_path}: {result.error}. "
                        "Call inspect_file_version again and rebase."
                    ),
                    name="patch_file",
                    tool_call_id=runtime.tool_call_id,
                    status="error",
                )
            return ToolMessage(
                content=(
                    f"Applied {applied} replacement(s) to {result.path or file_path}. "
                    f"previous_version={current_version}, new_version={_digest(updated)}"
                ),
                name="patch_file",
                tool_call_id=runtime.tool_call_id,
                status="success",
            )

        def stage_external_artifact(
            file_path: str,
            runtime: ToolRuntime[Any, Any],
        ) -> ToolMessage:
            from graph.session_manager import session_manager

            try:
                file_path = str(Path(file_path).expanduser().resolve(strict=True))
            except OSError as exc:
                return ToolMessage(
                    content=f"Error: unable to resolve exact external target: {exc}",
                    name="stage_external_artifact",
                    tool_call_id=runtime.tool_call_id,
                    status="error",
                )
            context = runtime.context if isinstance(runtime.context, dict) else {}
            session_id = str(context.get("session_id") or "")
            run_id = str(context.get("run_id") or "")
            query_id = str(context.get("query_id") or "")
            goal_id = str(context.get("goal_id") or "")
            goal_revision = context.get("goal_revision")
            original, error = _read_all(self.backend, file_path)
            if error is not None or original is None:
                return ToolMessage(
                    content=f"Error: {error or 'unable to read external artifact'}",
                    name="stage_external_artifact",
                    tool_call_id=runtime.tool_call_id,
                    status="error",
                )
            source_version = _digest(original)
            active_lease = session_manager.find_staged_external_artifact_lease(
                session_id,
                run_id=run_id,
                query_id=query_id,
                target_path=file_path,
                goal_id=goal_id,
                goal_revision=goal_revision,
            )
            if isinstance(active_lease, dict):
                if str(active_lease.get("expected_source_sha256") or "") != source_version:
                    return ToolMessage(
                        content=(
                            "Stage conflict: the authoritative external target changed after the active "
                            "lease was created. Commit or discard that lease before staging again."
                        ),
                        name="stage_external_artifact",
                        tool_call_id=runtime.tool_call_id,
                        status="error",
                    )
                staged_path = str(active_lease.get("staged_path") or "")
                _, staged_error = _read_all(self.backend, staged_path)
                rehydrated = staged_error is not None
                if staged_error is not None:
                    write_result = self.backend.write(staged_path, original)
                    if write_result.error:
                        return ToolMessage(
                            content=f"Error rehydrating staged artifact: {write_result.error}",
                            name="stage_external_artifact",
                            tool_call_id=runtime.tool_call_id,
                            status="error",
                        )
                now = time.time()
                validation_scratch = str(
                    active_lease.get("validation_scratch")
                    or f"/scratch/validation/{active_lease.get('lease_id')}"
                )
                migrated = not bool(active_lease.get("validation_scratch"))
                active_lease["validation_scratch"] = validation_scratch
                expired = float(active_lease.get("expires_at") or 0) < now
                rebound = (
                    str(active_lease.get("run_id") or "") != run_id
                    or str(active_lease.get("query_id") or "") != query_id
                )
                if expired or rebound or migrated:
                    active_lease.update(
                        {
                            "run_id": run_id,
                            "query_id": query_id,
                            "expires_at": now + EXTERNAL_ARTIFACT_LEASE_TTL_SECONDS,
                            "renewed_at": now,
                        }
                    )
                    session_manager.upsert_external_artifact_lease(
                        session_id,
                        active_lease,
                    )
                return ToolMessage(
                    content=(
                        "ExternalArtifactLease "
                        f"{'rehydrated from the current source; the previous uncommitted draft was unavailable' if rehydrated else 'rebound to this Run' if rebound else 'renewed after expiry' if expired else 'reused'} "
                        "for this Goal revision and exact target. "
                        f"lease_id={active_lease.get('lease_id')}; staged_path={staged_path}; "
                        f"expected_source_sha256={source_version}. Continue from the existing staged file; "
                        f"write temporary validators only under {validation_scratch}; "
                        "do not create or copy another workspace artifact."
                    ),
                    name="stage_external_artifact",
                    tool_call_id=runtime.tool_call_id,
                    status="success",
                )
            lease_seed = f"{session_id}:{run_id}:{runtime.tool_call_id}:{file_path}"
            lease_id = "artifact-lease-" + hashlib.sha256(
                lease_seed.encode("utf-8")
            ).hexdigest()[:16]
            safe_name = _safe_staged_filename(file_path.rsplit("/", 1)[-1])
            staged_path = f"/scratch/external/{lease_id}/{safe_name}"
            validation_scratch = f"/scratch/validation/{lease_id}"
            existing_lease = session_manager.get_external_artifact_lease(session_id, lease_id)
            if isinstance(existing_lease, dict):
                existing_lease.setdefault("validation_scratch", validation_scratch)
                staged_content, staged_error = _read_all(
                    self.backend,
                    str(existing_lease.get("staged_path") or ""),
                )
                replay_matches = (
                    existing_lease.get("status") == "staged"
                    and existing_lease.get("target_path") == file_path
                    and str(existing_lease.get("run_id") or "") == run_id
                    and str(existing_lease.get("query_id") or "") == query_id
                    and str(existing_lease.get("goal_id") or "") == goal_id
                    and existing_lease.get("goal_revision") == goal_revision
                    and existing_lease.get("expected_source_sha256") == source_version
                    and staged_error is None
                    and staged_content is not None
                    and _digest(staged_content) == source_version
                )
                if not replay_matches:
                    return ToolMessage(
                        content=(
                            "Stage conflict: this tool-call identity already owns a different "
                            "lease/source snapshot. Issue a new tool call and re-stage the latest target."
                        ),
                        name="stage_external_artifact",
                        tool_call_id=runtime.tool_call_id,
                        status="error",
                    )
                session_manager.upsert_external_artifact_lease(session_id, existing_lease)
                return ToolMessage(
                    content=(
                        f"ExternalArtifactLease already staged. lease_id={lease_id}; "
                        f"staged_path={staged_path}; expected_source_sha256={source_version}; "
                        f"validation_scratch={validation_scratch}."
                    ),
                    name="stage_external_artifact",
                    tool_call_id=runtime.tool_call_id,
                    status="success",
                )
            now = time.time()
            lease = {
                "lease_id": lease_id,
                "session_id": session_id,
                "run_id": run_id,
                "query_id": query_id,
                "goal_id": goal_id,
                "goal_revision": goal_revision,
                "target_path": file_path,
                "staged_path": staged_path,
                "validation_scratch": validation_scratch,
                "expected_source_sha256": source_version,
                "status": "claiming",
                "created_at": now,
                "expires_at": now + EXTERNAL_ARTIFACT_LEASE_TTL_SECONDS,
            }
            try:
                session_manager.claim_external_draft(
                    session_id,
                    lease_kind="exact_file",
                    lease=lease,
                )
            except RuntimeError as exc:
                return ToolMessage(
                    content=f"Stage conflict: {exc}",
                    name="stage_external_artifact",
                    tool_call_id=runtime.tool_call_id,
                    status="error",
                )
            write_result = self.backend.write(staged_path, original)
            if write_result.error:
                lease.update(
                    {
                        "status": "abandoned",
                        "abandoned_at": time.time(),
                        "abandoned_reason": "staging_write_failed",
                    }
                )
                session_manager.upsert_external_artifact_lease(session_id, lease)
                return ToolMessage(
                    content=f"Error staging external artifact: {write_result.error}",
                    name="stage_external_artifact",
                    tool_call_id=runtime.tool_call_id,
                    status="error",
                )
            lease["status"] = "staged"
            session_manager.upsert_external_artifact_lease(session_id, lease)
            return ToolMessage(
                content=(
                    f"ExternalArtifactLease created. lease_id={lease_id}; "
                    f"staged_path={staged_path}; expected_source_sha256={source_version}. "
                    f"Write temporary validation scripts only under {validation_scratch}; "
                    "edit the staged artifact at staged_path, then call commit_external_artifact."
                ),
                name="stage_external_artifact",
                tool_call_id=runtime.tool_call_id,
                status="success",
            )

        def commit_external_artifact(
            lease_id: str,
            file_path: str,
            runtime: ToolRuntime[Any, Any],
            expected_source_sha256: str | None = None,
            expected_draft_sha256: str | None = None,
            validation_receipt_ids: list[str] | None = None,
        ) -> ToolMessage:
            from graph.session_manager import session_manager

            context = runtime.context if isinstance(runtime.context, dict) else {}
            session_id = str(context.get("session_id") or "")
            run_id = str(context.get("run_id") or "")
            query_id = str(context.get("query_id") or "")
            goal_id = str(context.get("goal_id") or "")
            goal_revision = context.get("goal_revision")
            lease = session_manager.get_external_artifact_lease(session_id, lease_id)
            if not isinstance(lease, dict):
                return ToolMessage(
                    content=f"Error: unknown ExternalArtifactLease {lease_id}",
                    name="commit_external_artifact",
                    tool_call_id=runtime.tool_call_id,
                    status="error",
                )
            try:
                file_path = str(Path(file_path).expanduser().resolve(strict=True))
            except OSError:
                return ToolMessage(
                    content="Error: file_path does not match the exact target bound to this lease.",
                    name="commit_external_artifact",
                    tool_call_id=runtime.tool_call_id,
                    status="error",
                )
            if file_path != lease.get("target_path"):
                return ToolMessage(
                    content="Error: file_path does not match the exact target bound to this lease.",
                    name="commit_external_artifact",
                    tool_call_id=runtime.tool_call_id,
                    status="error",
                )
            lease_version = str(lease.get("expected_source_sha256") or "")
            supplied_version = str(expected_source_sha256 or lease_version)
            if supplied_version != lease_version:
                return ToolMessage(
                    content=(
                        f"Commit conflict: lease expects {lease_version}, request supplied "
                        f"{supplied_version}. expected_source_sha256 means the source hash captured at staging, "
                        "not the edited staged-file hash; omit the parameter to use the lease value."
                    ),
                    name="commit_external_artifact",
                    tool_call_id=runtime.tool_call_id,
                    status="error",
                )
            same_owner = (
                str(lease.get("goal_id") or "") == goal_id
                and lease.get("goal_revision") == goal_revision
                if goal_id
                else not str(lease.get("goal_id") or "")
                and str(lease.get("run_id") or "") == run_id
                and str(lease.get("query_id") or "") == query_id
            )
            if not same_owner:
                return ToolMessage(
                    content="Error: ExternalArtifactLease belongs to a different execution scope or Goal revision.",
                    name="commit_external_artifact",
                    tool_call_id=runtime.tool_call_id,
                    status="error",
                )
            if float(lease.get("expires_at") or 0) < time.time():
                return ToolMessage(
                    content="Error: ExternalArtifactLease expired; stage the current source again.",
                    name="commit_external_artifact",
                    tool_call_id=runtime.tool_call_id,
                    status="error",
                )
            if lease.get("status") == "committed":
                return ToolMessage(
                    content=(
                        f"ExternalArtifactLease already committed. lease_id={lease_id}; "
                        f"file_path={file_path}; content_sha256={lease.get('committed_sha256')}"
                    ),
                    name="commit_external_artifact",
                    tool_call_id=runtime.tool_call_id,
                    status="success",
                )
            if lease.get("status") != "staged":
                return ToolMessage(
                    content=f"Error: ExternalArtifactLease is not committable ({lease.get('status')}).",
                    name="commit_external_artifact",
                    tool_call_id=runtime.tool_call_id,
                    status="error",
                )
            current, current_error = _read_all(self.backend, file_path)
            staged, staged_error = _read_all(self.backend, str(lease.get("staged_path") or ""))
            if current_error or staged_error or current is None or staged is None:
                return ToolMessage(
                    content=f"Error: unable to read lease inputs: {current_error or staged_error}",
                    name="commit_external_artifact",
                    tool_call_id=runtime.tool_call_id,
                    status="error",
                )
            current_version = _digest(current)
            if current_version != lease_version:
                return ToolMessage(
                    content=(
                        f"Commit conflict: external target changed after staging. "
                        f"expected={lease_version}, current={current_version}. Create a new lease and rebase."
                    ),
                    name="commit_external_artifact",
                    tool_call_id=runtime.tool_call_id,
                    status="error",
                )
            draft_version = _digest(staged)
            if expected_draft_sha256 and expected_draft_sha256 != draft_version:
                return ToolMessage(
                    content=(
                        "Commit conflict: expected_draft_sha256 does not match the current staged draft. "
                        f"expected={expected_draft_sha256}, current={draft_version}. Re-inspect and revalidate."
                    ),
                    name="commit_external_artifact",
                    tool_call_id=runtime.tool_call_id,
                    status="error",
                )
            selected_receipt_ids = {
                str(item) for item in (validation_receipt_ids or []) if str(item)
            }
            if _code_like_target(file_path):
                if not expected_draft_sha256:
                    return ToolMessage(
                        content=(
                            "Error: code-like artifacts require expected_draft_sha256 and a successful "
                            "ValidationReceipt bound to this exact target/hash."
                        ),
                        name="commit_external_artifact",
                        tool_call_id=runtime.tool_call_id,
                        status="error",
                    )
                persisted_receipts = _persisted_validation_receipts(
                    session_manager,
                    session_id=session_id,
                    run_id=run_id,
                    goal_id=goal_id,
                    goal_revision=goal_revision,
                )
                selected_successes, blocking_failures = _accepted_receipts_for_target(
                    persisted_receipts,
                    target_path=file_path,
                    content_sha256=draft_version,
                    selected_receipt_ids=selected_receipt_ids,
                )
                if blocking_failures:
                    emit_harness_metric(
                        logger,
                        "commit_blocked_by_failed_validation_count",
                        session_id=session_id,
                        target=file_path,
                    )
                    failed_ids = [
                        str(item.get("validation_receipt_id") or "")
                        for item in blocking_failures
                    ]
                    return ToolMessage(
                        content=(
                            "Error: the staged draft has a blocking failed ValidationReceipt. "
                            f"failed_receipt_ids={failed_ids}. Fix the bytes and validate the new hash."
                        ),
                        name="commit_external_artifact",
                        tool_call_id=runtime.tool_call_id,
                        status="error",
                    )
                if not selected_successes:
                    emit_harness_metric(
                        logger,
                        "validation_receipt_target_mismatch_count",
                        session_id=session_id,
                        target=file_path,
                    )
                    return ToolMessage(
                        content=(
                            "Error: no supplied successful ValidationReceipt is bound to this exact "
                            f"target and draft hash ({draft_version})."
                        ),
                        name="commit_external_artifact",
                        tool_call_id=runtime.tool_call_id,
                        status="error",
                    )
                selected_receipt_ids = {
                    str(item.get("validation_receipt_id") or "")
                    for item in selected_successes
                    if str(item.get("validation_receipt_id") or "")
                }
            else:
                # Non-code deliveries do not claim validation lineage merely
                # because arbitrary receipt ids were supplied by the caller.
                selected_receipt_ids = set()
            commit_result = self.backend.edit(file_path, current, staged, replace_all=False)
            if commit_result.error:
                return ToolMessage(
                    content=f"Commit conflict: {commit_result.error}",
                    name="commit_external_artifact",
                    tool_call_id=runtime.tool_call_id,
                    status="error",
                )
            lease.update(
                {
                    "status": "committed",
                    "committed_at": time.time(),
                    "committed_sha256": _digest(staged),
                    "commit_tool_call_id": runtime.tool_call_id,
                    "validation_receipt_ids": sorted(selected_receipt_ids),
                }
            )
            try:
                delivery = session_manager.register_delivered_artifact(
                    session_id,
                    target_path=file_path,
                    content_sha256=draft_version,
                    source_run_id=run_id,
                    source_query_id=query_id,
                    source_goal_id=goal_id or None,
                    source_goal_revision=(
                        int(goal_revision) if goal_revision is not None else None
                    ),
                    validation_receipt_ids=sorted(selected_receipt_ids),
                )
            except Exception as exc:
                rollback = self.backend.edit(
                    file_path, staged, current, replace_all=False
                )
                lease.update(
                    {
                        "status": "staged",
                        "last_commit_error": f"artifact registry update failed: {exc}",
                    }
                )
                for key in (
                    "committed_at",
                    "committed_sha256",
                    "commit_tool_call_id",
                ):
                    lease.pop(key, None)
                session_manager.upsert_external_artifact_lease(session_id, lease)
                return ToolMessage(
                    content=(
                        "Error: artifact registry update failed after write-back; "
                        + (
                            "the target was rolled back and the staged lease remains retryable."
                            if not rollback.error
                            else f"rollback also failed: {rollback.error}"
                        )
                    ),
                    name="commit_external_artifact",
                    tool_call_id=runtime.tool_call_id,
                    status="error",
                )
            lease["delivered_artifact_id"] = delivery["artifact_id"]
            lease["delivery_receipt_id"] = delivery["delivery_receipt_id"]
            session_manager.upsert_external_artifact_lease(session_id, lease)
            return ToolMessage(
                content=(
                    f"External artifact committed. lease_id={lease_id}; file_path={file_path}; "
                    f"previous_sha256={current_version}; content_sha256={draft_version}; "
                    f"validation_receipt_ids={sorted(selected_receipt_ids)}; "
                    f"delivered_artifact_id={delivery['artifact_id']}"
                ),
                name="commit_external_artifact",
                tool_call_id=runtime.tool_call_id,
                status="success",
                artifact={"delivered_artifact": delivery},
            )

        def upsert_scratch_file(
            file_path: str,
            content: str,
            runtime: ToolRuntime[Any, Any],
            expected_sha256: str | None = None,
        ) -> ToolMessage:
            normalized = file_path.replace("\\", "/")
            parts = PurePosixPath(normalized).parts
            if (
                not normalized.startswith("/scratch/")
                or ".." in parts
                or "." in parts
                or "//" in normalized
            ):
                return ToolMessage(
                    content="Error: upsert_scratch_file is restricted to exact /scratch paths.",
                    name="upsert_scratch_file",
                    tool_call_id=runtime.tool_call_id,
                    status="error",
                )
            current, error = _read_all(self.backend, normalized)
            if current is None:
                if error and "not found" not in error.lower() and "does not exist" not in error.lower():
                    return ToolMessage(
                        content=f"Error reading scratch target: {error}",
                        name="upsert_scratch_file",
                        tool_call_id=runtime.tool_call_id,
                        status="error",
                    )
                if expected_sha256:
                    return ToolMessage(
                        content="Scratch replace conflict: target does not exist; omit expected_sha256 to create it.",
                        name="upsert_scratch_file",
                        tool_call_id=runtime.tool_call_id,
                        status="error",
                    )
                write_result = self.backend.write(normalized, content)
            else:
                current_sha = _digest(current)
                if not expected_sha256:
                    return ToolMessage(
                        content=(
                            f"Scratch target already exists at version {current_sha}. "
                            "Call inspect_file_version, then retry with expected_sha256 for atomic replacement."
                        ),
                        name="upsert_scratch_file",
                        tool_call_id=runtime.tool_call_id,
                        status="error",
                    )
                if expected_sha256 != current_sha:
                    return ToolMessage(
                        content=f"Scratch replace conflict: expected={expected_sha256}, current={current_sha}.",
                        name="upsert_scratch_file",
                        tool_call_id=runtime.tool_call_id,
                        status="error",
                    )
                write_result = self.backend.edit(normalized, current, content, replace_all=False)
            if write_result.error:
                return ToolMessage(
                    content=f"Error writing scratch file: {write_result.error}",
                    name="upsert_scratch_file",
                    tool_call_id=runtime.tool_call_id,
                    status="error",
                )
            return ToolMessage(
                content=f"Scratch file upserted: {normalized}; content_sha256={_digest(content)}",
                name="upsert_scratch_file",
                tool_call_id=runtime.tool_call_id,
                status="success",
            )

        def validate_artifact_contract(
            contract_id: str,
            html_file_path: str,
            javascript_file_path: str,
            runtime: ToolRuntime[Any, Any],
        ) -> ToolMessage:
            from graph.session_manager import session_manager
            from harness.artifact_contracts import validate_heatmap_year_contract
            from harness.models import ValidationReceipt, VerificationActivation

            if contract_id != "heatmap_year_contract/v1":
                return ToolMessage(
                    content=f"Error: unknown artifact contract {contract_id}",
                    name="validate_artifact_contract",
                    tool_call_id=runtime.tool_call_id,
                    status="error",
                )
            html, html_error = _read_all(self.backend, html_file_path)
            javascript, javascript_error = _read_all(
                self.backend, javascript_file_path
            )
            if html is None or javascript is None:
                return ToolMessage(
                    content=f"Error: {html_error or javascript_error or 'unable to read contract inputs'}",
                    name="validate_artifact_contract",
                    tool_call_id=runtime.tool_call_id,
                    status="error",
                )
            context = runtime.context if isinstance(runtime.context, dict) else {}
            session_id = str(context.get("session_id") or "")
            run_id = str(context.get("run_id") or "")
            query_id = str(context.get("query_id") or "")
            goal_id = str(context.get("goal_id") or "")
            goal_revision = context.get("goal_revision")

            def formal_target(observed_path: str) -> str | None:
                normalized = posixpath.normpath(observed_path.replace("\\", "/"))
                for lease in session_manager.list_external_artifact_leases(session_id):
                    if posixpath.normpath(str(lease.get("staged_path") or "")) == normalized:
                        return str(Path(str(lease.get("target_path") or "")).expanduser().resolve())
                roots = sorted(
                    (
                        (
                            posixpath.normpath(str(lease.get("staged_dir") or "")),
                            lease,
                        )
                        for lease in session_manager.list_external_directory_leases(session_id)
                        if lease.get("staged_dir") and lease.get("directory_path")
                    ),
                    key=lambda item: len(item[0]),
                    reverse=True,
                )
                for staged_root, lease in roots:
                    if normalized.startswith(f"{staged_root}/"):
                        relative = posixpath.relpath(normalized, staged_root)
                        return str(
                            (
                                Path(str(lease["directory_path"])).expanduser().resolve()
                                / relative
                            ).resolve()
                        )
                if normalized.startswith("/scratch/"):
                    return None
                return normalized

            input_pairs = [
                (html_file_path, html),
                (javascript_file_path, javascript),
            ]
            artifact_refs = []
            for observed_path, content in input_pairs:
                target_path = formal_target(observed_path)
                if target_path is None:
                    return ToolMessage(
                        content=(
                            "Error: contract input is an unbound scratch file; validate files from an "
                            "active external artifact/directory lease."
                        ),
                        name="validate_artifact_contract",
                        tool_call_id=runtime.tool_call_id,
                        status="error",
                    )
                artifact_refs.append(
                    {
                        "artifact_id": "artifact-"
                        + hashlib.sha256(f"external\0{target_path}".encode()).hexdigest()[:20],
                        "content_sha256": _digest(content),
                        "path": target_path,
                        "observed_path": posixpath.normpath(
                            observed_path.replace("\\", "/")
                        ),
                    }
                )
            result = validate_heatmap_year_contract(
                html=html,
                javascript=javascript,
                javascript_filename=Path(artifact_refs[1]["path"]).name,
            )
            passed = bool(result.get("passed"))
            if not passed:
                emit_harness_metric(
                    logger,
                    "artifact_ui_contract_failure_count",
                    session_id=session_id,
                    contract_id=contract_id,
                )
            receipt_seed = json.dumps(
                {
                    "run_id": run_id,
                    "contract_id": contract_id,
                    "artifact_refs": artifact_refs,
                    "passed": passed,
                },
                ensure_ascii=False,
                sort_keys=True,
            )
            receipt = ValidationReceipt(
                validation_receipt_id="validation-"
                + hashlib.sha256(receipt_seed.encode()).hexdigest()[:20],
                run_id=run_id,
                goal_id=goal_id or None,
                goal_revision=(
                    int(goal_revision) if goal_revision is not None else None
                ),
                validator_kind="artifact_ui_contract",
                validator_version=contract_id,
                artifact_refs=artifact_refs,
                command_evidence_ref="sha256:"
                + hashlib.sha256(
                    json.dumps(result, sort_keys=True).encode()
                ).hexdigest(),
                exit_code=0 if passed else 1,
                checks_passed=sum(
                    1 for value in result.get("checks", {}).values() if value
                ),
                checks_failed=sum(
                    1 for value in result.get("checks", {}).values() if not value
                ),
                status="passed" if passed else "failed",
                blocking=True,
                commit_authority=True,
                obligation_key=f"artifact_ui_contract:{contract_id}",
            )
            activation = VerificationActivation(
                activation_id="activation-" + receipt.validation_receipt_id,
                run_id=run_id,
                query_id=query_id,
                tool_call_id=runtime.tool_call_id,
                tool_name="validate_artifact_contract",
                pack="artifact",
                status="succeeded",
                evidence_refs=[
                    {
                        "kind": "validation_receipt",
                        **receipt.model_dump(mode="json"),
                        "contract_result": result,
                        "material": True,
                    }
                ],
            )
            try:
                session_manager.append_run_verification_activation(
                    session_id,
                    run_id,
                    activation.model_dump(mode="json"),
                )
            except (FileNotFoundError, ValueError):
                return ToolMessage(
                    content="Error: artifact contract validation requires an active persisted Run",
                    name="validate_artifact_contract",
                    tool_call_id=runtime.tool_call_id,
                    status="error",
                )
            return ToolMessage(
                content=json.dumps(
                    {
                        **result,
                        "validation_receipt_id": receipt.validation_receipt_id,
                        "artifact_refs": artifact_refs,
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                ),
                name="validate_artifact_contract",
                tool_call_id=runtime.tool_call_id,
                status="success" if passed else "error",
                artifact={
                    "validation_receipt": receipt.model_dump(mode="json"),
                    "contract_result": result,
                },
            )

        self.tools = [
            StructuredTool.from_function(
                name="inspect_file_version",
                description=(
                    "Read a UTF-8 file and return its full content plus sha256 version. "
                    "Always call this before patch_file."
                ),
                func=inspect_file_version,
                args_schema=InspectFileVersionInput,
                infer_schema=False,
            ),
            StructuredTool.from_function(
                name="patch_file",
                description=(
                    "Apply one atomic, optimistic patch against expected_sha256. "
                    "On conflict, inspect the latest version and rebase once; do not repeatedly guess exact strings."
                ),
                func=patch_file,
                args_schema=PatchFileInput,
                infer_schema=False,
            ),
            StructuredTool.from_function(
                name="stage_external_artifact",
                description=(
                    "Create an ExternalArtifactLease by copying one approved external file into "
                    "/scratch for Python/Node validation. The scratch copy is temporary, never the delivered artifact."
                ),
                func=stage_external_artifact,
                args_schema=StageExternalArtifactInput,
                infer_schema=False,
            ),
            StructuredTool.from_function(
                name="commit_external_artifact",
                description=(
                    "Atomically commit a validated ExternalArtifactLease back to its exact original path. "
                    "The source hash is read from the lease by default; expected_source_sha256, if supplied, is the "
                    "source hash captured at staging, never the edited staged-file hash. Code-like artifacts also "
                    "require expected_draft_sha256 plus successful validation_receipt_ids bound to that target/hash."
                ),
                func=commit_external_artifact,
                args_schema=CommitExternalArtifactInput,
                infer_schema=False,
            ),
            StructuredTool.from_function(
                name="upsert_scratch_file",
                description=(
                    "Create or atomically replace a temporary /scratch file. Existing files require the exact "
                    "expected_sha256 returned by inspect_file_version. This never grants host-path write access."
                ),
                func=upsert_scratch_file,
                args_schema=UpsertScratchFileInput,
                infer_schema=False,
            ),
            StructuredTool.from_function(
                name="validate_artifact_contract",
                description=(
                    "Run a registered deterministic cross-file artifact contract and persist a "
                    "ValidationReceipt bound to every exact input path/hash. Use "
                    "heatmap_year_contract/v1 for heatmap selector/data/default/matrix consistency."
                ),
                func=validate_artifact_contract,
                args_schema=ValidateArtifactContractInput,
                infer_schema=False,
            ),
        ]
