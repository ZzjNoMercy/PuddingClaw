"""Lease-backed and reversible external filesystem tools."""

import hashlib
import json
import logging
import time
from pathlib import Path, PurePosixPath
from typing import Any, Literal

from langchain.tools import ToolRuntime
from langchain_core.messages import ToolMessage
from langchain_core.tools import StructuredTool

from observability import emit_harness_metric
from tools.filesystem.inspect import digest, read_all
from tools.filesystem.schemas import (
    CommitExternalArtifactInput,
    DeleteFileInput,
    ExecuteExternalDirectoryInput,
    RewindExternalFileChangesInput,
    StageExternalArtifactInput,
    UpsertScratchFileInput,
)
from tools.filesystem.validation import (
    accepted_receipts_for_target,
    code_like_target,
    persisted_validation_receipts,
)

logger = logging.getLogger(__name__)

EXTERNAL_ARTIFACT_LEASE_TTL_SECONDS = 6 * 60 * 60


def safe_staged_filename(value: str) -> str:
    """Preserve readable Unicode names while removing path/control syntax."""

    import re
    import unicodedata

    normalized = unicodedata.normalize("NFC", value).replace("/", "_").replace("\\", "_")
    normalized = "".join(
        character
        for character in normalized
        if not unicodedata.category(character).startswith("C")
    )
    return re.sub(r"[^\w.\-]+", "_", normalized, flags=re.UNICODE).strip("._") or "artifact.txt"


def build_lease_tools(backend: Any) -> list[StructuredTool]:
    def rewind_external_file_changes(
        runtime: ToolRuntime[Any, Any],
    ) -> ToolMessage:
        rewind = getattr(backend, "rewind_external_file_changes", None)
        if not callable(rewind):
            return ToolMessage(
                content="io_error: this Backend does not support external-file rewind",
                name="rewind_external_file_changes",
                tool_call_id=runtime.tool_call_id,
                status="error",
            )
        result = rewind()
        status = str(result.get("status") or "io_error")
        return ToolMessage(
            content=json.dumps(result, ensure_ascii=False, sort_keys=True),
            name="rewind_external_file_changes",
            tool_call_id=runtime.tool_call_id,
            status=("success" if status in {"completed", "noop"} else "error"),
        )

    def delete_file(
        file_path: str,
        expected_sha256: str,
        runtime: ToolRuntime[Any, Any],
    ) -> ToolMessage:
        delete = getattr(backend, "delete_external_file", None)
        if not callable(delete):
            return ToolMessage(
                content="io_error: this Backend does not support external-file deletion",
                name="delete_file",
                tool_call_id=runtime.tool_call_id,
                status="error",
            )
        result = delete(file_path, expected_sha256=expected_sha256)
        status = str(result.get("status") or "io_error")
        return ToolMessage(
            content=json.dumps(result, ensure_ascii=False, sort_keys=True),
            name="delete_file",
            tool_call_id=runtime.tool_call_id,
            status="success" if status == "completed" else "error",
        )

    def execute_external_directory(
        directory_path: str,
        command: str,
        timeout: int,
        runtime: ToolRuntime[Any, Any],
        mode: Literal["read_only", "writable_draft"] = "read_only",
        lease_id: str | None = None,
    ) -> ToolMessage:
        execute = getattr(
            backend,
            "execute_external_directory_command",
            None,
        )
        if not callable(execute):
            return ToolMessage(
                content="io_error: this Backend does not support ephemeral external commands",
                name="execute_external_directory",
                tool_call_id=runtime.tool_call_id,
                status="error",
            )
        result = execute(
            directory_path,
            command,
            timeout=timeout,
            mode=mode,
            lease_id=lease_id,
        )
        status = str(result.get("status") or "io_error")
        return ToolMessage(
            content=json.dumps(result, ensure_ascii=False, sort_keys=True),
            name="execute_external_directory",
            tool_call_id=runtime.tool_call_id,
            status="success" if status == "completed" else "error",
        )

    # DEPRECATED COMPATIBILITY SURFACE: hidden from every new Run. Keep
    # behavior frozen for active historical checkpoints until the P2
    # retirement audit proves two release cycles with zero calls.
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
        original, error = read_all(backend, file_path)
        if error is not None or original is None:
            return ToolMessage(
                content=f"Error: {error or 'unable to read external artifact'}",
                name="stage_external_artifact",
                tool_call_id=runtime.tool_call_id,
                status="error",
            )
        source_version = digest(original)
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
            _, staged_error = read_all(backend, staged_path)
            rehydrated = staged_error is not None
            if staged_error is not None:
                write_result = backend.write(staged_path, original)
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
        safe_name = safe_staged_filename(file_path.rsplit("/", 1)[-1])
        staged_path = f"/scratch/external/{lease_id}/{safe_name}"
        validation_scratch = f"/scratch/validation/{lease_id}"
        existing_lease = session_manager.get_external_artifact_lease(session_id, lease_id)
        if isinstance(existing_lease, dict):
            existing_lease.setdefault("validation_scratch", validation_scratch)
            staged_content, staged_error = read_all(
                backend,
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
                and digest(staged_content) == source_version
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
        write_result = backend.write(staged_path, original)
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

    # DEPRECATED COMPATIBILITY SURFACE. New external mutations are owned by
    # HostFileBroker and external_mutation_completed receipts.
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
        current, current_error = read_all(backend, file_path)
        staged, staged_error = read_all(backend, str(lease.get("staged_path") or ""))
        if current_error or staged_error or current is None or staged is None:
            return ToolMessage(
                content=f"Error: unable to read lease inputs: {current_error or staged_error}",
                name="commit_external_artifact",
                tool_call_id=runtime.tool_call_id,
                status="error",
            )
        current_version = digest(current)
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
        draft_version = digest(staged)
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
        if code_like_target(file_path):
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
            persisted_receipts = persisted_validation_receipts(
                session_manager,
                session_id=session_id,
                run_id=run_id,
                goal_id=goal_id,
                goal_revision=goal_revision,
            )
            selected_successes, blocking_failures = accepted_receipts_for_target(
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
        commit_result = backend.edit(file_path, current, staged, replace_all=False)
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
                "committed_sha256": digest(staged),
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
            rollback = backend.edit(
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
        current, error = read_all(backend, normalized)
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
            write_result = backend.write(normalized, content)
        else:
            current_sha = digest(current)
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
            write_result = backend.edit(normalized, current, content, replace_all=False)
        if write_result.error:
            return ToolMessage(
                content=f"Error writing scratch file: {write_result.error}",
                name="upsert_scratch_file",
                tool_call_id=runtime.tool_call_id,
                status="error",
            )
        return ToolMessage(
            content=f"Scratch file upserted: {normalized}; content_sha256={digest(content)}",
            name="upsert_scratch_file",
            tool_call_id=runtime.tool_call_id,
            status="success",
        )
    return [
        StructuredTool.from_function(
            name="rewind_external_file_changes",
            description=(
                "Undo only HostFileBroker changes made by the current Run. "
                "Every target must still match the Run's recorded after-hash; "
                "otherwise rewind refuses instead of overwriting concurrent edits."
            ),
            func=rewind_external_file_changes,
            args_schema=RewindExternalFileChangesInput,
            infer_schema=False,
        ),
        StructuredTool.from_function(
            name="delete_file",
            description=(
                "Delete one exact authorized external file after verifying the version from "
                "inspect_file_version. This never deletes directories and never performs bulk "
                "or recursive deletion. Exact-file write permission does not imply delete permission."
            ),
            func=delete_file,
            args_schema=DeleteFileInput,
            infer_schema=False,
        ),
        StructuredTool.from_function(
            name="execute_external_directory",
            description=(
                "Run one command in a disposable offline docker run --rm. read_only mounts the exact "
                "authorized directory read-only. writable_draft requires a staged directory lease and "
                "writes only its isolated snapshot; then use prepare_external_directory_commit and "
                "commit_external_directory for reviewed atomic write-back. The host directory is never "
                "mounted writable during command execution."
            ),
            func=execute_external_directory,
            args_schema=ExecuteExternalDirectoryInput,
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
    ]
