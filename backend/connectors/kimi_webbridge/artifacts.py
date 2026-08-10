"""Run-scoped, backend-owned WebBridge screenshot/PDF artifacts."""

from __future__ import annotations

import hashlib
import os
import uuid
from pathlib import Path

import filetype
from runtime_identity.paths import PuddingClawPaths

MAX_ARTIFACT_BYTES = 100 * 1024 * 1024


class WebBridgeArtifactError(ValueError):
    pass


class WebBridgeArtifactStore:
    def __init__(self, paths: PuddingClawPaths) -> None:
        self.root = paths.root / "data" / "webbridge-artifacts"

    @staticmethod
    def _bucket(session_id: str, run_id: str) -> str:
        return hashlib.sha256(f"{session_id}\0{run_id}".encode("utf-8")).hexdigest()[:32]

    def allocate(self, *, session_id: str, run_id: str, kind: str, extension: str) -> Path:
        if not session_id or not run_id or kind not in {"screenshot", "pdf"}:
            raise WebBridgeArtifactError("invalid_artifact_binding")
        extension = extension.lower().lstrip(".")
        if extension not in {"png", "jpeg", "webp", "pdf"}:
            raise WebBridgeArtifactError("invalid_artifact_extension")
        folder = self.root / self._bucket(session_id, run_id)
        folder.mkdir(parents=True, exist_ok=True)
        try:
            folder.chmod(0o700)
        except OSError:
            pass
        path = folder / f"{kind}-{uuid.uuid4().hex}.{extension}"
        return path

    def validate_result(self, *, expected: Path, returned: object) -> Path:
        candidate = Path(str(returned or expected)).expanduser()
        expected_resolved = expected.resolve(strict=False)
        candidate_resolved = candidate.resolve(strict=False)
        try:
            candidate_resolved.relative_to(self.root.resolve(strict=False))
        except ValueError as exc:
            raise WebBridgeArtifactError("artifact_path_outside_managed_root") from exc
        if candidate_resolved != expected_resolved:
            raise WebBridgeArtifactError("artifact_path_does_not_match_backend_allocation")
        if not candidate.is_file():
            raise WebBridgeArtifactError("artifact_file_missing")
        size = candidate.stat().st_size
        if size <= 0 or size > MAX_ARTIFACT_BYTES:
            raise WebBridgeArtifactError("artifact_size_invalid")
        guessed = filetype.guess(candidate)
        detected = str(getattr(guessed, "mime", "") or "").lower()
        extension = candidate.suffix.lower()
        expected_mimes = {
            ".png": "image/png",
            ".jpeg": "image/jpeg",
            ".webp": "image/webp",
            ".pdf": "application/pdf",
        }
        if detected != expected_mimes.get(extension):
            raise WebBridgeArtifactError("artifact_mime_mismatch")
        try:
            os.chmod(candidate, 0o600)
        except OSError:
            pass
        return candidate
