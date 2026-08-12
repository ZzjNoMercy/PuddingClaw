"""Session-scoped attachment storage for Agent mode."""

from __future__ import annotations

import json
import hashlib
import io
import os
import shutil
import time
import threading
import uuid
import warnings
from functools import wraps
from pathlib import Path
from typing import BinaryIO, Any
from urllib.parse import quote

import filetype
from PIL import Image

IMAGE_MIME_PREFIX = "image/"
INLINE_IMAGE_MIME_TYPES = frozenset(
    {
        "image/jpeg",
        "image/png",
        "image/webp",
    }
)
MAX_INLINE_IMAGE_PIXELS = 40_000_000
MAX_ATTACHMENT_BYTES = 100 * 1024 * 1024


def _attachment_store_locked(method):
    @wraps(method)
    def wrapped(self, *args, **kwargs):
        with self._lock:
            return method(self, *args, **kwargs)

    return wrapped


class AttachmentStore:
    def __init__(self) -> None:
        self._base_dir: Path | None = None
        self._lock = threading.RLock()

    def initialize(
        self,
        base_dir: Path,
    ) -> None:
        canonical = (base_dir / "data" / "attachments").resolve()
        canonical.mkdir(parents=True, exist_ok=True)
        with self._lock:
            self._base_dir = canonical

    @property
    def root_dir(self) -> Path | None:
        return self._base_dir

    def _read_roots(self) -> tuple[Path, ...]:
        assert self._base_dir is not None
        return (self._base_dir,)

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

    @_attachment_store_locked
    def save(
        self,
        *,
        session_id: str,
        filename: str,
        mime_type: str,
        source: str,
        stream: BinaryIO,
        derived_from: str | None = None,
        created_by_run_id: str | None = None,
        created_by_query_id: str | None = None,
        created_by_goal_id: str | None = None,
        created_by_goal_revision: int | None = None,
        attachment_id: str | None = None,
    ) -> dict[str, Any]:
        assert self._base_dir is not None
        safe_session = self._safe_id(session_id)
        if safe_session != session_id:
            raise ValueError("invalid session_id")
        attachment_id = attachment_id or f"att_{uuid.uuid4().hex[:12]}"
        if self._safe_id(attachment_id) != attachment_id or not attachment_id.startswith("att_"):
            raise ValueError("invalid attachment_id")
        safe_name = self._safe_name(filename)
        session_folder = self._base_dir / safe_session
        session_folder.mkdir(parents=True, exist_ok=True)
        folder = session_folder / attachment_id
        if folder.exists():
            # A deterministic id may be left behind by a process crash before
            # manifest commit.  A valid committed attachment is never removed;
            # only an unreadable half-transaction is reclaimed for replay.
            if self.get(safe_session, attachment_id) is not None:
                raise FileExistsError(f"attachment {attachment_id} already exists")
            shutil.rmtree(folder, ignore_errors=True)
        temp_folder = session_folder / f".{attachment_id}.{uuid.uuid4().hex}.tmp"
        temp_folder.mkdir(parents=False, exist_ok=False)
        temp_file_path = temp_folder / safe_name
        final_file_path = folder / safe_name
        hasher = hashlib.sha256()
        total_bytes = 0
        try:
            with temp_file_path.open("wb") as target:
                while chunk := stream.read(1024 * 1024):
                    total_bytes += len(chunk)
                    if total_bytes > MAX_ATTACHMENT_BYTES:
                        raise ValueError(
                            f"attachment exceeds the {MAX_ATTACHMENT_BYTES} byte storage limit"
                        )
                    target.write(chunk)
                    hasher.update(chunk)
                target.flush()
                os.fsync(target.fileno())
            stat = temp_file_path.stat()
            content_sha256 = f"sha256:{hasher.hexdigest()}"
            item = {
                "id": attachment_id,
                "type": self.classify(safe_name, mime_type),
                "name": safe_name,
                "mime_type": mime_type or "application/octet-stream",
                "size": stat.st_size,
                "source": source if source in {"upload", "paste", "generated"} else "upload",
                "path": str(final_file_path),
                "session_id": safe_session,
                "sha256": content_sha256,
                "created_at": time.time(),
            }
            if derived_from:
                item["derived_from"] = derived_from
            if created_by_run_id:
                item["created_by_run_id"] = created_by_run_id
            if created_by_query_id:
                item["created_by_query_id"] = created_by_query_id
            if created_by_goal_id:
                item["created_by_goal_id"] = created_by_goal_id
            if created_by_goal_revision is not None:
                item["created_by_goal_revision"] = created_by_goal_revision
            manifest_path = temp_folder / "manifest.json"
            with manifest_path.open("w", encoding="utf-8") as manifest:
                json.dump(item, manifest, ensure_ascii=False, indent=2)
                manifest.flush()
                os.fsync(manifest.fileno())
            temp_folder.replace(folder)
            try:
                directory_fd = os.open(session_folder, os.O_RDONLY)
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
            except OSError:
                # Some filesystems do not support directory fsync. Atomic
                # rename still prevents readers from observing half a manifest.
                pass
        except Exception:
            shutil.rmtree(temp_folder, ignore_errors=True)
            raise
        return self.public_item(item)

    @_attachment_store_locked
    def save_bytes(
        self,
        *,
        session_id: str,
        filename: str,
        mime_type: str,
        data: bytes,
        source: str = "generated",
        derived_from: str | None = None,
        created_by_run_id: str | None = None,
        created_by_query_id: str | None = None,
        created_by_goal_id: str | None = None,
        created_by_goal_revision: int | None = None,
        attachment_id: str | None = None,
    ) -> dict[str, Any]:
        """Persist immutable bytes as a new Session attachment."""

        if attachment_id:
            existing = self.get(session_id, attachment_id)
            if existing is not None:
                expected_sha = f"sha256:{hashlib.sha256(data).hexdigest()}"
                same_derivation = (
                    existing.get("sha256") == expected_sha
                    and existing.get("name") == self._safe_name(filename)
                    and existing.get("source") == source
                    and str(existing.get("derived_from") or "") == str(derived_from or "")
                    and str(existing.get("created_by_run_id") or "") == str(created_by_run_id or "")
                    and str(existing.get("created_by_query_id") or "") == str(created_by_query_id or "")
                )
                if not same_derivation:
                    raise FileExistsError(
                        f"attachment identity collision for {attachment_id}"
                    )
                return self.public_item(existing)

        return self.save(
            session_id=session_id,
            filename=filename,
            mime_type=mime_type,
            source=source,
            stream=io.BytesIO(data),
            derived_from=derived_from,
            created_by_run_id=created_by_run_id,
            created_by_query_id=created_by_query_id,
            created_by_goal_id=created_by_goal_id,
            created_by_goal_revision=created_by_goal_revision,
            attachment_id=attachment_id,
        )

    @_attachment_store_locked
    def get(self, session_id: str, attachment_id: str) -> dict[str, Any] | None:
        assert self._base_dir is not None
        safe_session = self._safe_id(session_id)
        safe_attachment = self._safe_id(attachment_id)
        if safe_session != session_id or safe_attachment != attachment_id:
            return None
        for root in self._read_roots():
            manifest = root / safe_session / safe_attachment / "manifest.json"
            if not manifest.exists():
                continue
            try:
                item = json.loads(manifest.read_text(encoding="utf-8"))
            except Exception:
                continue
            path = Path(str(item.get("path") or ""))
            try:
                resolved_folder = manifest.parent.resolve(strict=True)
                resolved_path = path.resolve(strict=True)
                resolved_path.relative_to(resolved_folder)
            except (FileNotFoundError, OSError, ValueError):
                continue
            if not resolved_path.is_file():
                continue
            item["path"] = str(resolved_path)
            item.setdefault("session_id", safe_session)
            if not item.get("sha256"):
                item["sha256"] = f"sha256:{hashlib.sha256(resolved_path.read_bytes()).hexdigest()}"
            return item
        return None

    @_attachment_store_locked
    def delete_session(self, session_id: str) -> None:
        """Delete all attachment bytes owned by one deleted Session."""

        assert self._base_dir is not None
        safe_session = self._safe_id(session_id)
        if safe_session != session_id:
            raise ValueError("invalid session_id")
        for root in self._read_roots():
            shutil.rmtree(root / safe_session, ignore_errors=True)

    @staticmethod
    def public_item(item: dict[str, Any]) -> dict[str, Any]:
        public = {
            "id": item.get("id"),
            "type": item.get("type"),
            "name": item.get("name"),
            "mime_type": item.get("mime_type"),
            "size": item.get("size"),
            "source": item.get("source"),
            "sha256": item.get("sha256"),
            "created_at": item.get("created_at"),
        }
        for key in (
            "derived_from",
            "created_by_run_id",
            "created_by_query_id",
            "created_by_goal_id",
            "created_by_goal_revision",
        ):
            if item.get(key) is not None:
                public[key] = item[key]
        session_id = str(item.get("session_id") or "")
        attachment_id = str(item.get("id") or "")
        if session_id and attachment_id:
            public["download_url"] = (
                f"/api/attachments/{quote(attachment_id, safe='')}/download"
                f"?session_id={quote(session_id, safe='')}"
            )
            preview = AttachmentStore.preview_info(item)
            if preview is not None:
                public["preview_url"] = (
                    f"/api/attachments/{quote(attachment_id, safe='')}/preview"
                    f"?session_id={quote(session_id, safe='')}"
                )
                public["preview_mime_type"] = preview["mime_type"]
                public["width"] = preview["width"]
                public["height"] = preview["height"]
        return public

    @staticmethod
    def preview_info(item: dict[str, Any]) -> dict[str, Any] | None:
        """Verify that stored bytes are an inert raster image suitable for inline display."""

        path = Path(str(item.get("path") or ""))
        if not path.is_file():
            return None
        try:
            detected = filetype.guess(path)
            detected_mime = str(getattr(detected, "mime", "") or "").lower()
            if detected_mime not in INLINE_IMAGE_MIME_TYPES:
                return None
            with warnings.catch_warnings():
                warnings.simplefilter("error", Image.DecompressionBombWarning)
                with Image.open(path) as image:
                    width, height = image.size
                    if width < 1 or height < 1 or width * height > MAX_INLINE_IMAGE_PIXELS:
                        return None
                    # Animated WebP/APNG can contain substantially more decoded
                    # data than the first frame suggests.  Screenshot and QR
                    # preview intentionally supports inert, single-frame images.
                    if bool(getattr(image, "is_animated", False)) or int(
                        getattr(image, "n_frames", 1)
                    ) != 1:
                        return None
                    image.verify()
            return {
                "mime_type": detected_mime,
                "width": width,
                "height": height,
            }
        except (OSError, ValueError, Image.DecompressionBombError, Image.DecompressionBombWarning):
            return None


attachment_store = AttachmentStore()
