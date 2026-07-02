"""Session-scoped attachment storage for Agent mode."""

from __future__ import annotations

import json
import shutil
import time
import uuid
from pathlib import Path
from typing import BinaryIO, Any

IMAGE_MIME_PREFIX = "image/"


class AttachmentStore:
    def __init__(self) -> None:
        self._base_dir: Path | None = None

    def initialize(self, base_dir: Path) -> None:
        self._base_dir = base_dir / "data" / "attachments"
        self._base_dir.mkdir(parents=True, exist_ok=True)

    @property
    def root_dir(self) -> Path | None:
        return self._base_dir

    @staticmethod
    def _safe_id(value: str) -> str:
        return "".join(ch for ch in value if ch.isalnum() or ch in "-_") or "default"

    @staticmethod
    def _safe_name(value: str) -> str:
        cleaned = Path(value or "attachment").name
        return "".join(ch for ch in cleaned if ch.isalnum() or ch in " ._-()[]")[:160] or "attachment"

    @staticmethod
    def classify(filename: str, mime_type: str = "") -> str:
        mime = mime_type.lower()
        name = filename.lower()
        if mime.startswith("image/"):
            return "image"
        if mime == "application/pdf" or name.endswith(".pdf"):
            return "pdf"
        if "spreadsheet" in mime or "excel" in mime or name.endswith((".xls", ".xlsx", ".csv", ".tsv")):
            return "spreadsheet"
        if mime == "text/markdown" or name.endswith((".md", ".markdown")):
            return "markdown"
        if mime.startswith("text/") or name.endswith((".txt", ".log", ".json", ".yaml", ".yml")):
            return "text"
        if name.endswith((".doc", ".docx", ".ppt", ".pptx")):
            return "document"
        return "file"

    def save(
        self,
        *,
        session_id: str,
        filename: str,
        mime_type: str,
        source: str,
        stream: BinaryIO,
    ) -> dict[str, Any]:
        assert self._base_dir is not None
        safe_session = self._safe_id(session_id)
        attachment_id = f"att_{uuid.uuid4().hex[:12]}"
        safe_name = self._safe_name(filename)
        folder = self._base_dir / safe_session / attachment_id
        folder.mkdir(parents=True, exist_ok=True)
        file_path = folder / safe_name
        with file_path.open("wb") as target:
            shutil.copyfileobj(stream, target)
        stat = file_path.stat()
        item = {
            "id": attachment_id,
            "type": self.classify(safe_name, mime_type),
            "name": safe_name,
            "mime_type": mime_type or "application/octet-stream",
            "size": stat.st_size,
            "source": source if source in {"upload", "paste"} else "upload",
            "path": str(file_path),
            "created_at": time.time(),
        }
        (folder / "manifest.json").write_text(json.dumps(item, ensure_ascii=False, indent=2), encoding="utf-8")
        return self.public_item(item)

    def get(self, session_id: str, attachment_id: str) -> dict[str, Any] | None:
        assert self._base_dir is not None
        safe_session = self._safe_id(session_id)
        safe_attachment = self._safe_id(attachment_id)
        manifest = self._base_dir / safe_session / safe_attachment / "manifest.json"
        if not manifest.exists():
            matches = list(self._base_dir.glob(f"*/{safe_attachment}/manifest.json"))
            manifest = matches[0] if matches else manifest
        if not manifest.exists():
            return None
        try:
            item = json.loads(manifest.read_text(encoding="utf-8"))
        except Exception:
            return None
        path = Path(str(item.get("path") or ""))
        if not path.is_file():
            return None
        return item

    @staticmethod
    def public_item(item: dict[str, Any]) -> dict[str, Any]:
        return {
            "id": item.get("id"),
            "type": item.get("type"),
            "name": item.get("name"),
            "mime_type": item.get("mime_type"),
            "size": item.get("size"),
            "source": item.get("source"),
            "created_at": item.get("created_at"),
        }


attachment_store = AttachmentStore()
