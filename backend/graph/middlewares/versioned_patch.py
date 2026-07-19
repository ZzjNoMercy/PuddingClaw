"""Optimistic, versioned file patch tools for DeepAgents."""

from __future__ import annotations

import hashlib
import re
import time
from typing import Any

from langchain.agents.middleware.types import AgentMiddleware
from langchain.tools import ToolRuntime
from langchain_core.messages import ToolMessage
from langchain_core.tools import StructuredTool
from pydantic import BaseModel, Field, model_validator


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
    expected_source_sha256: str


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
            lease_seed = f"{session_id}:{run_id}:{runtime.tool_call_id}:{file_path}"
            lease_id = "artifact-lease-" + hashlib.sha256(
                lease_seed.encode("utf-8")
            ).hexdigest()[:16]
            safe_name = re.sub(r"[^A-Za-z0-9._-]+", "_", file_path.rsplit("/", 1)[-1]) or "artifact.txt"
            staged_path = f"/scratch/external/{lease_id}/{safe_name}"
            existing_lease = session_manager.get_external_artifact_lease(session_id, lease_id)
            if isinstance(existing_lease, dict):
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
                return ToolMessage(
                    content=(
                        f"ExternalArtifactLease already staged. lease_id={lease_id}; "
                        f"staged_path={staged_path}; expected_source_sha256={source_version}."
                    ),
                    name="stage_external_artifact",
                    tool_call_id=runtime.tool_call_id,
                    status="success",
                )
            write_result = self.backend.write(staged_path, original)
            if write_result.error:
                return ToolMessage(
                    content=f"Error staging external artifact: {write_result.error}",
                    name="stage_external_artifact",
                    tool_call_id=runtime.tool_call_id,
                    status="error",
                )
            lease = {
                "lease_id": lease_id,
                "session_id": session_id,
                "run_id": run_id,
                "query_id": query_id,
                "goal_id": goal_id,
                "goal_revision": goal_revision,
                "target_path": file_path,
                "staged_path": staged_path,
                "expected_source_sha256": source_version,
                "status": "staged",
                "created_at": time.time(),
                "expires_at": time.time() + 6 * 60 * 60,
            }
            session_manager.upsert_external_artifact_lease(session_id, lease)
            return ToolMessage(
                content=(
                    f"ExternalArtifactLease created. lease_id={lease_id}; "
                    f"staged_path={staged_path}; expected_source_sha256={source_version}. "
                    "Validate or edit only staged_path, then call commit_external_artifact."
                ),
                name="stage_external_artifact",
                tool_call_id=runtime.tool_call_id,
                status="success",
            )

        def commit_external_artifact(
            lease_id: str,
            file_path: str,
            expected_source_sha256: str,
            runtime: ToolRuntime[Any, Any],
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
            if file_path != lease.get("target_path"):
                return ToolMessage(
                    content="Error: file_path does not match the exact target bound to this lease.",
                    name="commit_external_artifact",
                    tool_call_id=runtime.tool_call_id,
                    status="error",
                )
            lease_version = str(lease.get("expected_source_sha256") or "")
            if expected_source_sha256 != lease_version:
                return ToolMessage(
                    content=(
                        f"Commit conflict: lease expects {lease_version}, request supplied "
                        f"{expected_source_sha256}."
                    ),
                    name="commit_external_artifact",
                    tool_call_id=runtime.tool_call_id,
                    status="error",
                )
            if str(lease.get("run_id") or "") != run_id or str(lease.get("query_id") or "") != query_id:
                return ToolMessage(
                    content="Error: ExternalArtifactLease belongs to a different Run/query.",
                    name="commit_external_artifact",
                    tool_call_id=runtime.tool_call_id,
                    status="error",
                )
            if str(lease.get("goal_id") or "") != goal_id or lease.get("goal_revision") != goal_revision:
                return ToolMessage(
                    content="Error: ExternalArtifactLease belongs to a different Goal revision.",
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
                }
            )
            session_manager.upsert_external_artifact_lease(session_id, lease)
            return ToolMessage(
                content=(
                    f"External artifact committed. lease_id={lease_id}; file_path={file_path}; "
                    f"previous_sha256={current_version}; content_sha256={_digest(staged)}"
                ),
                name="commit_external_artifact",
                tool_call_id=runtime.tool_call_id,
                status="success",
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
                    "Requires the source sha captured at staging and fails if the target changed."
                ),
                func=commit_external_artifact,
                args_schema=CommitExternalArtifactInput,
                infer_schema=False,
            ),
        ]
