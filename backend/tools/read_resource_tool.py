"""ReadResourceTool — unified PuddingClaw resource reader."""

from __future__ import annotations

from pathlib import Path

from langchain_core.tools import BaseTool
from pydantic import BaseModel, Field

from graph.attachment_store import attachment_store
from graph.managed_paths import is_managed_resource_path
from graph.session_manager import session_manager
from knowledge.paths import get_knowledge_root
from tools.read_external_file_tool import ReadExternalFileTool

IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".tif", ".tiff"}


class ReadResourceInput(BaseModel):
    resource: str = Field(
        description=(
            "Resource to read. Pass either an attachment id like att_11d3cfb4dc67 for uploaded/pasted "
            "attachments, a /knowledge/... virtual path, or the exact non-workspace path the user provided. "
            "This includes POSIX absolute paths, Windows absolute paths, and home-relative paths. "
            "Do not pass /workspace or /scratch virtual paths here."
        )
    )
    offset: int = Field(default=0, ge=0, description="Zero-based line offset for text files")
    limit: int = Field(default=2000, ge=1, le=2000, description="Maximum text lines to return")


class ReadResourceTool(BaseTool):
    name: str = "read_resource"
    description: str = (
        "Read a PuddingClaw resource from a single entry point. Use this for uploaded/pasted attachment refs "
        "(`att_xxx`), `/knowledge/...` virtual paths, and user-provided paths outside the `/workspace/` virtual namespace. "
        "Never pass `/scratch/...` here: staged scratch artifacts belong to the Backend/Docker namespace and "
        "must be read with read_file."
    )
    args_schema: type[BaseModel] = ReadResourceInput
    risk_level: str = "moderate"
    session_id: str = ""
    workspace_path: str = ""

    def _read_attachment(self, attachment_id: str) -> str:
        item = attachment_store.get(self.session_id, attachment_id)
        if not item:
            return f"❌ Attachment not found: {attachment_id}"
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
                f"Attachment {attachment_id} is {attachment_type}. Text extraction for this type is not enabled yet; "
                "ask the user to export it as Markdown/text for now."
            )

        path = Path(str(item.get("path") or ""))
        if not path.is_file():
            return f"❌ Attachment file missing: {attachment_id}"
        try:
            content = path.read_text(encoding="utf-8", errors="replace")
        except Exception as exc:
            return f"❌ Error reading attachment: {exc}"
        if len(content) > 40000:
            content = content[:40000] + "\n...[truncated]"
        return (
            f"Attachment: {item.get('name')}\n"
            f"Type: {attachment_type}\n"
            f"Size: {item.get('size')} bytes\n\n"
            f"{content}"
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
            if not is_managed_resource_path(path, base_dir) and not session_manager.has_external_file_read_permission(self.session_id, path):
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

    def _read_workspace_path(
        self,
        path: Path,
        *,
        offset: int,
        limit: int,
    ) -> str:
        if not path.exists():
            return f"❌ File not found: {path}"
        if not path.is_file():
            return f"❌ Not a file: {path}"
        image_marker = self._read_image_path_marker(str(path))
        if image_marker is not None:
            return image_marker
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except Exception as exc:
            return f"❌ Error reading workspace resource: {exc}"
        selected = lines[offset : offset + limit]
        suffix = (
            f"\n...[{len(lines) - offset - len(selected)} more lines]"
            if offset + len(selected) < len(lines)
            else ""
        )
        return "\n".join(selected) + suffix

    def _run(self, resource: str, offset: int = 0, limit: int = 2000) -> str:
        value = resource.strip()
        if not value:
            return "❌ Missing resource."
        if value.startswith("att_"):
            return self._read_attachment(value)
        workspace_path = self._resolve_workspace_virtual_path(value)
        if workspace_path is not None:
            return self._read_workspace_path(
                workspace_path,
                offset=offset,
                limit=limit,
            )
        knowledge_path = self._resolve_knowledge_virtual_path(value)
        if knowledge_path is not None:
            value = str(knowledge_path)
        image_marker = self._read_image_path_marker(value)
        if image_marker is not None:
            return image_marker
        return ReadExternalFileTool(
            session_id=self.session_id,
            workspace_path=self.workspace_path,
        ).invoke({"path": value, "offset": offset, "limit": limit})


def create_read_resource_tool(base_dir: Path) -> ReadResourceTool:
    return ReadResourceTool()
