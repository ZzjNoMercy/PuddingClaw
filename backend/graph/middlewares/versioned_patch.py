"""Optimistic, versioned file patch tools for DeepAgents."""

import hashlib
import json
import logging
import posixpath
import re
import time
import unicodedata
from pathlib import Path, PurePosixPath
from typing import Any, Literal

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


class FilePatchSpec(BaseModel):
    file_path: str
    expected_sha256: str
    replacements: list[ReplacementHunk] = Field(min_length=1, max_length=100)


class PatchFilesInput(BaseModel):
    files: list[FilePatchSpec] = Field(min_length=2, max_length=50)


class ReplaceFileInput(BaseModel):
    file_path: str
    content: str
    expected_sha256: str = Field(
        description="sha256:<hex> for the exact file version being replaced"
    )


class CopyFileInput(BaseModel):
    source_path: str = Field(description="Exact authorized source file")
    target_path: str = Field(
        description="Exact new target file; existing targets are never overwritten"
    )
    expected_source_sha256: str | None = Field(
        default=None,
        description=(
            "Optional sha256:<hex> source precondition. The actual copied hash is "
            "always recorded and returned."
        ),
    )


class MaterializeDestination(BaseModel):
    kind: Literal["file", "slot"]
    target_path: str | None = None
    mode: Literal["create", "replace"] = "create"
    expected_sha256: str | None = None
    template_path: str | None = None
    template_sha256: str | None = None
    slot_id: str | None = None
    output_path: str | None = None
    output_mode: Literal["create", "replace"] = "replace"
    expected_output_sha256: str | None = None

    @model_validator(mode="after")
    def validate_destination(self) -> "MaterializeDestination":
        if self.kind == "file":
            if not self.target_path:
                raise ValueError("file destination requires target_path")
            if self.mode == "replace" and not self.expected_sha256:
                raise ValueError("file replace requires expected_sha256")
        else:
            if not all(
                (
                    self.template_path,
                    self.template_sha256,
                    self.slot_id,
                    self.output_path,
                )
            ):
                raise ValueError(
                    "slot destination requires template_path, template_sha256, "
                    "slot_id and output_path"
                )
            if self.output_mode == "replace" and not self.expected_output_sha256:
                raise ValueError("slot output replace requires expected_output_sha256")
        return self


class MaterializeSourceRefInput(BaseModel):
    source_ref: str
    destination: MaterializeDestination
    renderer: Literal["identity", "json", "csv", "js_array", "text"]
    projection: list[str] = Field(default_factory=list)
    expected_schema_ref: str | None = None
    expected_item_count: int | None = Field(default=None, ge=0)


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


class ValidateHtmlReportInput(BaseModel):
    html_file_path: str = Field(
        description=(
            "Absolute HTML report path. Ordinary validation reads the report and "
            "its local resources directly; contract-required browser E2E mounts "
            "the exact parent directory read-only in an offline container."
        )
    )
    browser_e2e: bool | None = Field(
        default=None,
        description=(
            "Normally omit this server-owned parameter. Harness resolves it "
            "from the frozen verification contract. An explicit value must "
            "match that contract."
        ),
    )
    timeout: int = Field(default=120, ge=1, le=600)


class RewindExternalFileChangesInput(BaseModel):
    """The active Run scope is supplied by the Backend, not the model."""


class DeleteFileInput(BaseModel):
    file_path: str = Field(description="Exact external file to delete; directories are rejected")
    expected_sha256: str = Field(
        description="sha256:<hex> returned by inspect_file_version for the exact file version"
    )


class ExecuteExternalDirectoryInput(BaseModel):
    directory_path: str = Field(
        description="Exact authorized external directory bound to this command"
    )
    command: str = Field(
        min_length=1,
        description="Exact shell command; receives a separate command-level approval",
    )
    timeout: int = Field(default=120, ge=1, le=600)
    mode: Literal["read_only", "writable_draft"] = "read_only"
    lease_id: str | None = Field(
        default=None,
        description=(
            "Required for writable_draft. The command writes only to this "
            "server-owned external-directory snapshot, never directly to the host."
        ),
    )


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


def _render_replacements(
    content: str,
    replacements: list[ReplacementHunk],
) -> tuple[str | None, int, list[dict[str, Any]]]:
    """Render all hunks against one immutable source without overlap."""

    spans: list[tuple[int, int, str, int]] = []
    failures: list[dict[str, Any]] = []
    for index, hunk in enumerate(replacements, start=1):
        starts: list[int] = []
        offset = 0
        while True:
            found = content.find(hunk.old_string, offset)
            if found < 0:
                break
            starts.append(found)
            offset = found + max(1, len(hunk.old_string))
        if not starts:
            failures.append({"hunk": index, "error_code": "old_string_absent"})
            continue
        if not hunk.replace_all and len(starts) != 1:
            failures.append(
                {
                    "hunk": index,
                    "error_code": "old_string_not_unique",
                    "occurrences": len(starts),
                }
            )
            continue
        selected = starts if hunk.replace_all else starts[:1]
        spans.extend(
            (
                start,
                start + len(hunk.old_string),
                hunk.new_string,
                index,
            )
            for start in selected
        )
    if failures:
        return None, 0, failures
    ordered = sorted(spans, key=lambda item: (item[0], item[1]))
    for previous, current in zip(ordered, ordered[1:]):
        if current[0] < previous[1]:
            failures.append(
                {
                    "hunk": current[3],
                    "error_code": "overlapping_hunks",
                    "overlaps_with": previous[3],
                }
            )
    if failures:
        return None, 0, failures
    updated = content
    for start, end, replacement, _index in reversed(ordered):
        updated = updated[:start] + replacement + updated[end:]
    return updated, len(ordered), []


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
        if (
            str(receipt.get("failure_class") or "artifact_failure")
            == "artifact_failure"
            or failed_at >= latest_success_by_obligation.get(key, 0.0)
        )
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
            rebased = expected_sha256 != current_version
            updated, applied, failures = _render_replacements(
                original,
                replacements,
            )
            if updated is None:
                return ToolMessage(
                    content=json.dumps(
                        {
                            "status": "conflict",
                            "error_code": "patch_rebase_conflict",
                            "expected_sha256": expected_sha256,
                            "current_sha256": current_version,
                            "failed_hunks": failures,
                            "next_action": "inspect_conflicting_region",
                        },
                        ensure_ascii=False,
                        sort_keys=True,
                    ),
                    name="patch_file",
                    tool_call_id=runtime.tool_call_id,
                    status="error",
                )
            # Commit with the entire inspected source as the compare-and-swap
            # precondition. A concurrent write becomes an exact-match conflict.
            result = self.backend.edit(file_path, original, updated, replace_all=False)
            if result.error:
                return ToolMessage(
                    content=json.dumps(
                        {
                            "status": "conflict",
                            "error_code": "concurrent_patch_commit_conflict",
                            "current_sha256": current_version,
                            "expected_sha256": expected_sha256,
                            "error": str(result.error),
                            "next_action": "retry_once_with_latest_version",
                        },
                        ensure_ascii=False,
                        sort_keys=True,
                    ),
                    name="patch_file",
                    tool_call_id=runtime.tool_call_id,
                    status="error",
                )
            target_path = str(result.path or file_path)
            target_sha256 = _digest(updated)
            mutation_receipt_id = ""
            validation_receipt_ids: list[str] = []
            context = runtime.context if isinstance(runtime.context, dict) else {}
            session_id = str(context.get("session_id") or "")
            run_id = str(context.get("run_id") or "")
            if session_id and run_id and Path(target_path).is_absolute():
                from graph.session_manager import session_manager

                receipt = session_manager.find_external_mutation_receipt(
                    session_id,
                    run_id=run_id,
                    canonical_path=str(Path(target_path).resolve()),
                    after_sha256=target_sha256,
                )
                if isinstance(receipt, dict):
                    mutation_receipt_id = str(receipt.get("receipt_id") or "")
                    validation_receipt_id = str(
                        receipt.get("validation_receipt_id") or ""
                    )
                    if validation_receipt_id:
                        validation_receipt_ids.append(validation_receipt_id)
            return ToolMessage(
                content=json.dumps(
                    {
                        "status": "completed",
                        "target_path": target_path,
                        "applied_replacements": applied,
                        "previous_sha256": current_version,
                        "target_sha256": target_sha256,
                        "rebased": rebased,
                        "rebased_from_sha256": expected_sha256 if rebased else None,
                        "rebased_to_sha256": current_version if rebased else None,
                        "mutation_receipt_id": mutation_receipt_id,
                        "validation_receipt_ids": validation_receipt_ids,
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                ),
                name="patch_file",
                tool_call_id=runtime.tool_call_id,
                status="success",
            )

        def replace_file(
            file_path: str,
            content: str,
            expected_sha256: str,
            runtime: ToolRuntime[Any, Any],
        ) -> ToolMessage:
            replace = getattr(self.backend, "replace_external_file", None)
            if not callable(replace):
                return ToolMessage(
                    content=json.dumps(
                        {
                            "status": "io_error",
                            "error_code": "replace_not_supported",
                            "next_action": "use_versioned_patch",
                        },
                        sort_keys=True,
                    ),
                    name="replace_file",
                    tool_call_id=runtime.tool_call_id,
                    status="error",
                )
            result = replace(
                file_path,
                content.encode("utf-8"),
                expected_sha256=expected_sha256,
            )
            status = str(result.get("status") or "io_error")
            return ToolMessage(
                content=json.dumps(result, ensure_ascii=False, sort_keys=True),
                name="replace_file",
                tool_call_id=runtime.tool_call_id,
                status="success" if status == "completed" else "error",
            )

        def copy_file(
            source_path: str,
            target_path: str,
            runtime: ToolRuntime[Any, Any],
            expected_source_sha256: str | None = None,
        ) -> ToolMessage:
            copy = getattr(self.backend, "copy_external_file", None)
            if not callable(copy):
                return ToolMessage(
                    content=json.dumps(
                        {
                            "status": "io_error",
                            "error_code": "copy_not_supported",
                            "next_action": "report_infrastructure_error",
                        },
                        sort_keys=True,
                    ),
                    name="copy_file",
                    tool_call_id=runtime.tool_call_id,
                    status="error",
                )
            result = copy(
                source_path,
                target_path,
                expected_source_sha256=expected_source_sha256,
            )
            status = str(result.get("status") or "io_error")
            return ToolMessage(
                content=json.dumps(result, ensure_ascii=False, sort_keys=True),
                name="copy_file",
                tool_call_id=runtime.tool_call_id,
                status="success" if status == "completed" else "error",
            )

        def materialize_source_ref(
            source_ref: str,
            destination: MaterializeDestination,
            renderer: str,
            runtime: ToolRuntime[Any, Any],
            projection: list[str] | None = None,
            expected_schema_ref: str | None = None,
            expected_item_count: int | None = None,
        ) -> ToolMessage:
            from harness.source_materialization import (
                SourceMaterializationError,
                fill_typed_slot,
                persist_materialization_receipt,
                public_source_reference,
                render_source,
                resolve_source_bytes,
            )

            context = runtime.context if isinstance(runtime.context, dict) else {}
            session_id = str(context.get("session_id") or "")
            run_id = str(context.get("run_id") or "")
            query_id = str(context.get("query_id") or "")
            if not session_id or not run_id:
                return ToolMessage(
                    content=json.dumps(
                        {
                            "status": "error",
                            "error_code": "active_run_required",
                            "next_action": "retry_in_active_run",
                        },
                        sort_keys=True,
                    ),
                    name="materialize_source_ref",
                    tool_call_id=runtime.tool_call_id,
                    status="error",
                )
            try:
                if isinstance(destination, dict):
                    destination = MaterializeDestination.model_validate(destination)
                source, source_bytes = resolve_source_bytes(
                    session_id,
                    source_ref,
                )
                rendered, item_count = render_source(
                    source,
                    source_bytes,
                    renderer=renderer,
                    projection=list(projection or []),
                    expected_schema_ref=expected_schema_ref,
                    expected_item_count=expected_item_count,
                )
                template_sha256: str | None = None
                slot_id: str | None = None
                if destination.kind == "slot":
                    template, template_error = _read_all(
                        self.backend,
                        str(destination.template_path),
                    )
                    if template_error is not None or template is None:
                        raise SourceMaterializationError(
                            "template_unavailable",
                            template_error or "unable to read template",
                            next_action="inspect_template",
                        )
                    template_sha256 = _digest(template)
                    if template_sha256 != destination.template_sha256:
                        raise SourceMaterializationError(
                            "template_version_changed",
                            (
                                f"expected {destination.template_sha256}, "
                                f"current {template_sha256}"
                            ),
                            next_action="inspect_template",
                        )
                    slot_id = str(destination.slot_id)
                    output_text = fill_typed_slot(
                        template,
                        slot_id=slot_id,
                        renderer=renderer,
                        rendered=rendered,
                    )
                    candidate = output_text.encode("utf-8")
                    target_path = str(destination.output_path)
                    mode = destination.output_mode
                    expected_target = destination.expected_output_sha256
                else:
                    candidate = rendered
                    target_path = str(destination.target_path)
                    mode = destination.mode
                    expected_target = destination.expected_sha256

                virtual_target = target_path.replace("\\", "/").startswith(
                    ("/workspace/", "/scratch/")
                ) or not Path(target_path).is_absolute()
                if virtual_target:
                    try:
                        candidate_text = candidate.decode("utf-8")
                    except UnicodeDecodeError as exc:
                        raise SourceMaterializationError(
                            "virtual_binary_materialization_unsupported",
                            "binary identity materialization requires an external target",
                            next_action="choose_external_target",
                        ) from exc
                    if mode == "create":
                        write_result = self.backend.write(
                            target_path,
                            candidate_text,
                        )
                    else:
                        current, current_error = _read_all(
                            self.backend,
                            target_path,
                        )
                        if current_error is not None or current is None:
                            raise SourceMaterializationError(
                                "target_unavailable",
                                current_error or "unable to read target",
                                next_action="inspect_target",
                            )
                        current_sha256 = _digest(current)
                        if current_sha256 != expected_target:
                            raise SourceMaterializationError(
                                "target_version_changed",
                                (
                                    f"expected {expected_target}, "
                                    f"current {current_sha256}"
                                ),
                                next_action="inspect_target",
                            )
                        write_result = self.backend.edit(
                            target_path,
                            current,
                            candidate_text,
                            replace_all=False,
                        )
                    if write_result.error:
                        raise SourceMaterializationError(
                            "materialization_commit_failed",
                            str(write_result.error),
                            next_action="inspect_target",
                        )
                    commit_result = {
                        "status": "completed",
                        "target_path": str(write_result.path or target_path),
                        "target_sha256": _digest(candidate_text),
                        "receipt_id": "",
                        "mutation_receipt_id": "",
                        "validation_receipt": None,
                        "validation_receipt_ids": [],
                    }
                else:
                    if mode == "create":
                        create = getattr(self.backend, "create_external_file", None)
                        if not callable(create):
                            raise SourceMaterializationError(
                                "materialization_create_unsupported",
                                "Backend does not support external byte creation",
                                next_action="report_infrastructure_error",
                            )
                        commit_result = create(
                            target_path,
                            candidate,
                            operation="materialize_create",
                        )
                    else:
                        replace = getattr(self.backend, "replace_external_file", None)
                        if not callable(replace):
                            raise SourceMaterializationError(
                                "materialization_replace_unsupported",
                                "Backend does not support external byte replacement",
                                next_action="report_infrastructure_error",
                            )
                        commit_result = replace(
                            target_path,
                            candidate,
                            expected_sha256=str(expected_target),
                            operation="materialize_replace",
                        )
                    if str(commit_result.get("status") or "") != "completed":
                        return ToolMessage(
                            content=json.dumps(
                                commit_result,
                                ensure_ascii=False,
                                sort_keys=True,
                            ),
                            name="materialize_source_ref",
                            tool_call_id=runtime.tool_call_id,
                            status="error",
                        )

                validation_receipt = commit_result.get("validation_receipt")
                validation_ids = [
                    str(item)
                    for item in commit_result.get("validation_receipt_ids") or []
                    if str(item)
                ]
                if (
                    not validation_ids
                    and isinstance(validation_receipt, dict)
                    and validation_receipt.get("validation_receipt_id")
                ):
                    validation_ids = [
                        str(validation_receipt.get("validation_receipt_id"))
                    ]
                receipt = persist_materialization_receipt(
                    session_id=session_id,
                    run_id=run_id,
                    query_id=query_id,
                    source=source,
                    renderer=renderer,
                    target_path=str(commit_result.get("target_path") or target_path),
                    target_sha256=str(commit_result.get("target_sha256") or ""),
                    item_count=item_count,
                    mutation_receipt_id=str(
                        commit_result.get("mutation_receipt_id")
                        or commit_result.get("receipt_id")
                        or ""
                    ),
                    template_sha256=template_sha256,
                    slot_id=slot_id,
                    validation_receipt_ids=validation_ids,
                )
                return ToolMessage(
                    content=json.dumps(
                        {
                            "status": "completed",
                            "source": public_source_reference(source),
                            "renderer": f"{renderer}/v1",
                            "item_count": item_count,
                            "target_path": receipt["target_path"],
                            "target_sha256": receipt["target_sha256"],
                            "materialization_receipt_id": receipt[
                                "materialization_receipt_id"
                            ],
                            "mutation_receipt_id": receipt[
                                "mutation_receipt_id"
                            ],
                            "validation_receipt_ids": validation_ids,
                        },
                        ensure_ascii=False,
                        sort_keys=True,
                    ),
                    name="materialize_source_ref",
                    tool_call_id=runtime.tool_call_id,
                    status="success",
                    artifact={"materialization_receipt": receipt},
                )
            except SourceMaterializationError as exc:
                return ToolMessage(
                    content=json.dumps(
                        exc.as_dict(),
                        ensure_ascii=False,
                        sort_keys=True,
                    ),
                    name="materialize_source_ref",
                    tool_call_id=runtime.tool_call_id,
                    status="error",
                )

        def rewind_external_file_changes(
            runtime: ToolRuntime[Any, Any],
        ) -> ToolMessage:
            rewind = getattr(self.backend, "rewind_external_file_changes", None)
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
            delete = getattr(self.backend, "delete_external_file", None)
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
                self.backend,
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

        def validate_html_report(
            html_file_path: str,
            timeout: int,
            runtime: ToolRuntime[Any, Any],
            browser_e2e: bool | None = None,
        ) -> ToolMessage:
            """Run contract-selected HTML validation without model-authored shell."""

            validate = getattr(self.backend, "validate_html_report", None)
            if not callable(validate):
                result = {
                    "status": "infrastructure_error",
                    "error_code": "html_validator_backend_unavailable",
                    "failure_class": "infrastructure_failure",
                    "error": "this Backend does not support HTML validation",
                }
            else:
                result = validate(
                    html_file_path,
                    browser_e2e=browser_e2e,
                    timeout=timeout,
                )
            status = str(result.get("status") or "infrastructure_error")
            return ToolMessage(
                content=json.dumps(result, ensure_ascii=False, sort_keys=True),
                name="validate_html_report",
                tool_call_id=runtime.tool_call_id,
                status="success" if status == "completed" else "error",
            )

        def patch_files(
            files: list[FilePatchSpec],
            runtime: ToolRuntime[Any, Any],
        ) -> ToolMessage:
            apply_transaction = getattr(
                self.backend,
                "apply_external_file_transaction",
                None,
            )
            if not callable(apply_transaction):
                return ToolMessage(
                    content="io_error: this Backend does not support multi-file transactions",
                    name="patch_files",
                    tool_call_id=runtime.tool_call_id,
                    status="error",
                )
            changes: list[dict[str, Any]] = []
            for spec in files:
                original, error = _read_all(self.backend, spec.file_path)
                if error is not None or original is None:
                    return ToolMessage(
                        content=f"io_error: {error or 'unable to read file'}",
                        name="patch_files",
                        tool_call_id=runtime.tool_call_id,
                        status="error",
                    )
                current = _digest(original)
                if current != spec.expected_sha256:
                    return ToolMessage(
                        content=(
                            f"conflict: {spec.file_path} expected "
                            f"{spec.expected_sha256}, current {current}"
                        ),
                        name="patch_files",
                        tool_call_id=runtime.tool_call_id,
                        status="error",
                    )
                updated = original
                for hunk in spec.replacements:
                    occurrences = updated.count(hunk.old_string)
                    if occurrences == 0 or (
                        not hunk.replace_all and occurrences != 1
                    ):
                        return ToolMessage(
                            content=(
                                f"conflict: replacement is not uniquely applicable "
                                f"to {spec.file_path}; re-inspect and rebase"
                            ),
                            name="patch_files",
                            tool_call_id=runtime.tool_call_id,
                            status="error",
                        )
                    updated = updated.replace(
                        hunk.old_string,
                        hunk.new_string,
                        -1 if hunk.replace_all else 1,
                    )
                changes.append(
                    {
                        "file_path": spec.file_path,
                        "expected_sha256": spec.expected_sha256,
                        "content": updated,
                    }
                )
            result = apply_transaction(changes)
            status = str(result.get("status") or "io_error")
            return ToolMessage(
                content=json.dumps(result, ensure_ascii=False, sort_keys=True),
                name="patch_files",
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
                failure_class=None if passed else "artifact_failure",
                content_observed=True,
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
                    "If the source changed, mechanically rebase once only when every hunk "
                    "still matches uniquely and without overlap."
                ),
                func=patch_file,
                args_schema=PatchFileInput,
                infer_schema=False,
            ),
            StructuredTool.from_function(
                name="replace_file",
                description=(
                    "Atomically replace one authorized UTF-8 file under expected_sha256. "
                    "Validation happens before commit and any conflict leaves the target unchanged. "
                    "Use this instead of delete_file followed by write_file."
                ),
                func=replace_file,
                args_schema=ReplaceFileInput,
                infer_schema=False,
            ),
            StructuredTool.from_function(
                name="copy_file",
                description=(
                    "Create one UTF-8 file from an authorized absolute Host source without "
                    "streaming its body through model context. A /workspace target uses the "
                    "existing workspace boundary and needs no external write Grant; an absolute "
                    "Host target still requires exact Host write authority. Records source/target "
                    "hashes and never overwrites an existing target."
                ),
                func=copy_file,
                args_schema=CopyFileInput,
                infer_schema=False,
            ),
            StructuredTool.from_function(
                name="materialize_source_ref",
                description=(
                    "Materialize an immutable server SourceReference directly into a file "
                    "or one typed template slot through a deterministic renderer. Full payload "
                    "bytes never enter model context. The commit is permission-checked, atomic, "
                    "validated, and returns a MaterializationReceipt."
                ),
                func=materialize_source_ref,
                args_schema=MaterializeSourceRefInput,
                infer_schema=False,
            ),
            StructuredTool.from_function(
                name="patch_files",
                description=(
                    "Apply one atomic optimistic transaction across 2-50 authorized external files. "
                    "Every file must carry the version returned by inspect_file_version; any permission, "
                    "version, validation, or I/O failure leaves all targets unchanged."
                ),
                func=patch_files,
                args_schema=PatchFilesInput,
                infer_schema=False,
            ),
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
                name="validate_html_report",
                description=(
                    "Validate one HTML report against its current hash. Ordinary "
                    "runs perform lightweight structure, duplicate-ID, and local-"
                    "resource checks. The frozen verification contract enables "
                    "PuddingClaw's fixed offline Chromium adapter only for explicit "
                    "browser E2E requirements. Omit browser_e2e; no model-authored "
                    "shell or per-call HITL is required after directory read "
                    "permission exists. Returns an exact-hash ValidationReceipt."
                ),
                func=validate_html_report,
                args_schema=ValidateHtmlReportInput,
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
