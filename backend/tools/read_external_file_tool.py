"""ReadExternalFileTool — permission-gated reads outside the workspace."""

from __future__ import annotations

from pathlib import Path

from langchain_core.tools import BaseTool
from pydantic import BaseModel, Field

from graph.host_read_policy import is_sensitive_host_read_path
from graph.managed_paths import is_managed_resource_path
from graph.session_manager import session_manager
from graph.trace_collector import get_current_trace_collector


class ReadExternalFileInput(BaseModel):
    path: str = Field(description="Absolute path of the external file to read")
    offset: int = Field(default=0, ge=0, description="Zero-based line offset")
    limit: int = Field(default=2000, ge=1, le=2000, description="Maximum lines to return")


class ReadExternalFileTool(BaseTool):
    name: str = "read_external_file"
    description: str = (
        "Read a local text file outside the current workspace under the current Run Profile. "
        "Use this only for exact absolute paths supplied by the user, such as files under "
        "Downloads or Documents. Submit the read once; Harness decides whether it runs or "
        "interrupts. For workspace, /scratch, or /tmp files, use read_file. This tool does "
        "not parse PDF or other binary document formats."
    )
    args_schema: type[BaseModel] = ReadExternalFileInput
    risk_level: str = "moderate"
    session_id: str = ""
    run_id: str = ""
    workspace_path: str = ""
    backend_mode: str = "kernel"
    approval_mode: str = "strict"

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

    @staticmethod
    def _read_page(path: Path, offset: int, limit: int) -> str:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        selected = lines[offset : offset + limit]
        content = "\n".join(selected)
        if offset + limit < len(lines):
            content += (
                f"\n\n[Showing lines {offset + 1}-{offset + len(selected)} of {len(lines)}. "
                f"Continue with offset={offset + limit}.]"
            )
        return content

    def _run(self, path: str, offset: int = 0, limit: int = 2000) -> str:
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
                content = self._read_page(requested, offset, limit)
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

            smart_ordinary_read = (
                self.approval_mode == "smart"
                and self.backend_mode in {"spawn", "kernel"}
                and not is_sensitive_host_read_path(requested)
            )
            unrestricted_spawn_read = (
                self.backend_mode == "spawn" and self.approval_mode != "smart"
            )
            matching_grant = None
            directory_granted = False
            if not unrestricted_spawn_read and not smart_ordinary_read:
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
                directory_granted = session_manager.has_external_path_read_permission(
                    self.session_id,
                    requested,
                    run_id=self.run_id,
                )
            if not unrestricted_spawn_read and not smart_ordinary_read and not (
                matching_grant or directory_granted
            ):
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

            content = self._read_page(requested, offset, limit)
            self._record_permission_span(
                action="enforce",
                path=str(requested),
                outcome="allowed",
                grant_kind=(
                    "smart_host_read"
                    if smart_ordinary_read
                    else "spawn_host_read"
                    if unrestricted_spawn_read
                    else str(
                        (matching_grant or {}).get("target_kind")
                        or ("exact_directory" if directory_granted else "exact_file")
                    )
                ),
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
