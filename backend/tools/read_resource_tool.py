"""ReadResourceTool — attachment and visual-resource bridge."""

from __future__ import annotations

from pathlib import Path
from urllib.parse import urlsplit

from langchain_core.tools import BaseTool
from pydantic import BaseModel, Field

from graph.attachment_store import attachment_store
from graph.host_read_policy import is_sensitive_host_read_path
from graph.managed_paths import is_managed_resource_path
from graph.session_manager import session_manager
from knowledge.paths import get_knowledge_root

IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".tif", ".tiff"}


class ReadResourceInput(BaseModel):
    resource: str = Field(
        description=(
            "Attachment or visual resource to open. Pass an attachment id like att_11d3cfb4dc67 for an "
            "uploaded/pasted attachment, or an absolute/home-relative/virtual local image path that must be "
            "injected into visual analysis. Ordinary text paths must use read_file. HTTP(S) URLs must use "
            "fetch_url."
        )
    )
    offset: int = Field(default=0, ge=0, description="Zero-based line offset for text attachments")
    limit: int = Field(default=2000, ge=1, le=2000, description="Maximum attachment text lines to return")


class ReadResourceTool(BaseTool):
    name: str = "read_resource"
    description: str = (
        "Open an uploaded/pasted attachment ref (`att_xxx`) or a local image path that must be injected into "
        "visual analysis. This is not a general filesystem reader: use read_file for every ordinary text "
        "path, whether real or virtual. This tool does not extract PDF text; activate the PDF Skill and use "
        "its extraction flow."
    )
    args_schema: type[BaseModel] = ReadResourceInput
    risk_level: str = "moderate"
    session_id: str = ""
    run_id: str = ""
    workspace_path: str = ""
    backend_mode: str = "kernel"
    approval_mode: str = "strict"
    allowed_attachment_ids: list[str] = Field(default_factory=list, exclude=True)
    enforce_attachment_allowlist: bool = Field(default=False, exclude=True)

    def _read_attachment(self, attachment_id: str, *, offset: int = 0, limit: int = 2000) -> str:
        item = attachment_store.get(self.session_id, attachment_id)
        if not item:
            return f"❌ Attachment not found: {attachment_id}"
        if self.enforce_attachment_allowlist:
            explicitly_allowed = attachment_id in set(self.allowed_attachment_ids)
            generated_by_current_run = (
                str(item.get("source") or "") == "generated"
                and bool(self.run_id)
                and str(item.get("created_by_run_id") or "") == self.run_id
            )
            if not explicitly_allowed and not generated_by_current_run:
                return "❌ Attachment is outside this image-analysis delegation."
        attachment_type = str(item.get("type") or "file")
        if attachment_type == "image":
            return (
                f"Attachment: {item.get('name')}\n"
                "Type: image\n"
                f"Size: {item.get('size')} bytes\n"
                f"PuddingClaw-Resource-Image: {attachment_id}\n\n"
                "The image resource has been opened for the image_analyzer subagent. Continue with visual analysis."
            )
        if attachment_type in {"pdf", "document"}:
            return (
                f"Attachment {attachment_id} is {attachment_type}. Text extraction for this type is not enabled yet. "
                "Do not retry its internal artifact path; treat it as a user-facing output attachment."
            )
        path = Path(str(item.get("path") or ""))
        if not path.is_file():
            return f"❌ Attachment file missing: {attachment_id}"
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except Exception as exc:
            return f"❌ Error reading attachment: {exc}"
        selected = lines[offset : offset + limit]
        content = "\n".join(selected)
        suffix = (
            f"\n\n[Showing lines {offset + 1}-{offset + len(selected)} of {len(lines)}. "
            f"Continue with offset={offset + limit}.]"
            if offset + limit < len(lines)
            else ""
        )
        return (
            f"Attachment: {item.get('name')}\n"
            f"Type: {attachment_type}\n"
            f"Size: {item.get('size')} bytes\n\n"
            f"{content}{suffix}"
        )

    @staticmethod
    def _is_relative_to(path: Path, parent: Path) -> bool:
        try:
            path.relative_to(parent)
            return True
        except ValueError:
            return False

    def _read_image_path_marker(self, value: str) -> str | None:
        path = Path(value).expanduser().resolve()
        if path.suffix.lower() not in IMAGE_EXTENSIONS:
            return None
        if not path.is_absolute():
            return "❌ Image path must be absolute or home-relative."
        if not path.exists():
            return f"❌ File not found: {path}"
        if not path.is_file():
            return f"❌ Not a file: {path}"

        workspace = Path(self.workspace_path).expanduser().resolve() if self.workspace_path else None
        base_dir = Path(__file__).resolve().parent.parent
        if workspace is not None and not self._is_relative_to(path, workspace):
            smart_ordinary_read = (
                self.approval_mode == "smart"
                and self.backend_mode in {"spawn", "kernel"}
                and not is_sensitive_host_read_path(path)
            )
            unrestricted_spawn_read = (
                self.backend_mode == "spawn" and self.approval_mode != "smart"
            )
            if (
                not unrestricted_spawn_read
                and not smart_ordinary_read
                and not is_managed_resource_path(path, base_dir)
                and not session_manager.has_external_path_read_permission(
                    self.session_id,
                    path,
                    run_id=self.run_id,
                )
            ):
                return (
                    "🔒 Permission required: this file is outside the current workspace.\n"
                    f"Path: {path}"
                )

        return (
            f"Local resource: {path}\n"
            "Type: image\n"
            f"Size: {path.stat().st_size} bytes\n"
            f"PuddingClaw-Resource-Image-Path: {path}\n\n"
            "The image resource has been opened for the image_analyzer subagent. Continue with visual analysis."
        )

    def _resolve_knowledge_virtual_path(self, value: str) -> Path | None:
        if not value.startswith("/knowledge/"):
            return None
        base_dir = Path(__file__).resolve().parent.parent
        relative = value.removeprefix("/knowledge/").lstrip("/")
        path = (get_knowledge_root(base_dir) / relative).expanduser().resolve()
        knowledge_root = get_knowledge_root(base_dir).expanduser().resolve()
        try:
            path.relative_to(knowledge_root)
        except ValueError:
            return None
        return path

    def _resolve_workspace_virtual_path(self, value: str) -> Path | None:
        if not value.startswith("/workspace/") or not self.workspace_path:
            return None
        workspace = Path(self.workspace_path).expanduser().resolve()
        relative = value.removeprefix("/workspace/").lstrip("/")
        path = (workspace / relative).resolve()
        try:
            path.relative_to(workspace)
        except ValueError:
            return None
        return path

    def _run(self, resource: str, offset: int = 0, limit: int = 2000) -> str:
        value = resource.strip()
        if not value:
            return "❌ Missing resource."
        try:
            parsed = urlsplit(value)
        except ValueError:
            parsed = None
        if parsed is not None and parsed.scheme.lower() in {"http", "https"} and parsed.netloc:
            return "❌ Web URL supplied to read_resource; use fetch_url for HTTP(S) resources."
        if value.startswith("att_"):
            return self._read_attachment(value, offset=offset, limit=limit)
        if Path(value).suffix.lower() not in IMAGE_EXTENSIONS:
            return (
                "❌ read_resource only accepts attachment refs (`att_xxx`) and local image paths. "
                "Use read_file for ordinary text paths."
            )
        workspace_path = self._resolve_workspace_virtual_path(value)
        if workspace_path is not None:
            value = str(workspace_path)
        knowledge_path = self._resolve_knowledge_virtual_path(value)
        if knowledge_path is not None:
            value = str(knowledge_path)
        image_marker = self._read_image_path_marker(value)
        if image_marker is not None:
            return image_marker
        return "❌ read_resource could not resolve the local image path."


def create_read_resource_tool(base_dir: Path) -> ReadResourceTool:
    return ReadResourceTool()
