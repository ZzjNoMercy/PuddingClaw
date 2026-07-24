"""Optimistic single- and multi-file patch tools."""

import json
from typing import Any

from langchain.tools import ToolRuntime
from langchain_core.messages import ToolMessage
from langchain_core.tools import StructuredTool

from tools.filesystem.inspect import digest, read_all
from tools.filesystem.schemas import (
    FilePatchSpec,
    PatchFileInput,
    PatchFilesInput,
    ReplaceFileInput,
    ReplacementHunk,
)


def render_replacements(
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


def build_patch_tools(backend: Any) -> list[StructuredTool]:
    """Build optimistic patch/replace tools bound to one workspace backend."""

    def patch_file(
        file_path: str,
        expected_sha256: str,
        replacements: list[ReplacementHunk],
        runtime: ToolRuntime[Any, Any],
    ) -> ToolMessage:
        original, error = read_all(backend, file_path)
        if error is not None or original is None:
            return ToolMessage(
                content=f"Error: {error or 'unable to read file'}",
                name="patch_file",
                tool_call_id=runtime.tool_call_id,
                status="error",
            )
        current_version = digest(original)
        rebased = expected_sha256 != current_version
        updated, applied, failures = render_replacements(original, replacements)
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
        result = backend.edit(file_path, original, updated, replace_all=False)
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
        target_sha256 = digest(updated)
        mutation_receipt_id = ""
        validation_receipt_ids: list[str] = []
        context = runtime.context if isinstance(runtime.context, dict) else {}
        session_id = str(context.get("session_id") or "")
        run_id = str(context.get("run_id") or "")
        if session_id and run_id:
            from pathlib import Path

            if Path(target_path).is_absolute():
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
        replace = getattr(backend, "replace_external_file", None)
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

    def patch_files(
        files: list[FilePatchSpec],
        runtime: ToolRuntime[Any, Any],
    ) -> ToolMessage:
        apply_transaction = getattr(backend, "apply_external_file_transaction", None)
        if not callable(apply_transaction):
            return ToolMessage(
                content="io_error: this Backend does not support multi-file transactions",
                name="patch_files",
                tool_call_id=runtime.tool_call_id,
                status="error",
            )
        changes: list[dict[str, Any]] = []
        for spec in files:
            original, error = read_all(backend, spec.file_path)
            if error is not None or original is None:
                return ToolMessage(
                    content=f"io_error: {error or 'unable to read file'}",
                    name="patch_files",
                    tool_call_id=runtime.tool_call_id,
                    status="error",
                )
            current = digest(original)
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
                            "conflict: replacement is not uniquely applicable "
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

    return [
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
    ]
