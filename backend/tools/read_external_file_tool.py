"""ReadExternalFileTool — permission-gated reads outside the workspace."""

from __future__ import annotations

from pathlib import Path
from typing import Type

from langchain_core.tools import BaseTool
from pydantic import BaseModel, Field

from graph.managed_paths import is_managed_resource_path
from graph.session_manager import session_manager
from graph.trace_collector import get_current_trace_collector


class ReadExternalFileInput(BaseModel):
    path: str = Field(description="Absolute path of the external file to read")


class ReadExternalFileTool(BaseTool):
    name: str = "read_external_file"
    description: str = (
        "Read a local file outside the current workspace after the user has granted "
        "external-file read permission for this session. Use this only for absolute "
        "paths pasted by the user, such as files under Downloads or Documents. "
        "For workspace files, use the normal read_file tool."
    )
    args_schema: Type[BaseModel] = ReadExternalFileInput
    risk_level: str = "moderate"
    session_id: str = ""
    workspace_path: str = ""

    @staticmethod
    def _is_relative_to(path: Path, parent: Path) -> bool:
        try:
            path.relative_to(parent)
            return True
        except ValueError:
            return False

    def _record_permission_span(
        self,
        *,
        action: str,
        path: str,
        outcome: str,
        grant_kind: str | None = None,
        error: str | None = None,
    ) -> None:
        collector = get_current_trace_collector()
        if collector is None:
            return
        metadata = {
            "harness": {
                "mechanism": "permission",
                "pillars": [{"name": "architectural_constraints", "role": "primary"}],
            },
            "permission": {
                "type": "external_file_read",
                "action": action,
                "target_kind": grant_kind or "exact_file",
                "capabilities": ["read", "external_path"],
                "outcome": outcome,
            },
        }
        if error:
            metadata["permission"]["error"] = error
        collector.add_custom_span(
            f"permission.{action}",
            {"path": path, "outcome": outcome, **({"error": error} if error else {})},
            span_type="permission",
            metadata=metadata,
        )

    def _run(self, path: str) -> str:
        raw_path = path.strip()
        if not raw_path:
            return "❌ Permission required: missing external file path."

        try:
            requested = Path(raw_path).expanduser().resolve()
            workspace = Path(self.workspace_path).expanduser().resolve() if self.workspace_path else None

            if not requested.is_absolute():
                self._record_permission_span(
                    action="enforce",
                    path=raw_path,
                    outcome="denied",
                    error="path_not_absolute",
                )
                return "❌ Permission denied: read_external_file only accepts absolute paths."

            if not requested.exists():
                self._record_permission_span(
                    action="enforce",
                    path=str(requested),
                    outcome="denied",
                    error="file_not_found",
                )
                return f"❌ File not found: {requested}"

            if not requested.is_file():
                self._record_permission_span(
                    action="enforce",
                    path=str(requested),
                    outcome="denied",
                    error="not_a_file",
                )
                return f"❌ Not a file: {requested}"

            base_dir = Path(__file__).resolve().parent.parent
            if is_managed_resource_path(requested, base_dir):
                content = requested.read_text(encoding="utf-8", errors="replace")
                if len(content) > 20000:
                    content = content[:20000] + "\n...[truncated]"
                self._record_permission_span(
                    action="enforce",
                    path=str(requested),
                    outcome="allowed",
                    grant_kind="managed_resource",
                )
                return content

            if workspace is not None and self._is_relative_to(requested, workspace):
                self._record_permission_span(
                    action="enforce",
                    path=str(requested),
                    outcome="denied",
                    error="workspace_path",
                )
                return "❌ Use read_file for workspace files; read_external_file is only for paths outside the workspace."

            grants = session_manager.list_permission_grants(self.session_id)
            matching_grant = next(
                (
                    grant
                    for grant in grants
                    if grant.get("type") == "external_file_read"
                    and "read" in (grant.get("capabilities") or [])
                    and (
                        grant.get("target_kind") == "all_external_files"
                        or (
                            grant.get("target_kind") == "exact_file"
                            and grant.get("target") == str(requested)
                        )
                    )
                ),
                None,
            )
            if not matching_grant:
                self._record_permission_span(
                    action="request",
                    path=str(requested),
                    outcome="needs_user",
                )
                return (
                    "🔒 Permission required: this file is outside the current workspace.\n"
                    f"Path: {requested}\n\n"
                    "Grant this session access before retrying. API options:\n"
                    f"- POST /api/sessions/{self.session_id}/permissions/external-files "
                    'with {"target_kind":"exact_file","path":"'
                    f"{requested}"
                    '"}\n'
                    f"- POST /api/sessions/{self.session_id}/permissions/external-files "
                    'with {"target_kind":"all_external_files"}'
                )

            content = requested.read_text(encoding="utf-8", errors="replace")
            if len(content) > 20000:
                content = content[:20000] + "\n...[truncated]"
            self._record_permission_span(
                action="enforce",
                path=str(requested),
                outcome="allowed",
                grant_kind=str(matching_grant.get("target_kind") or "exact_file"),
            )
            return content
        except Exception as exc:
            self._record_permission_span(
                action="enforce",
                path=raw_path,
                outcome="error",
                error=str(exc),
            )
            return f"❌ Error reading external file: {exc}"


def create_read_external_file_tool(base_dir: Path) -> ReadExternalFileTool:
    return ReadExternalFileTool()
