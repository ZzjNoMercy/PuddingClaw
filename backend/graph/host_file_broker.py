"""Authorized host-file operations outside the project workspace.

The broker is the only direct host-filesystem authority exposed to Agent file
tools. It consumes existing exact-file or exact-directory grants, canonicalizes
every target, prevents symlink escape, performs optimistic atomic edits, and
persists mutation receipts. Shell execution remains outside this module and
continues to use the project execution backend.
"""

from __future__ import annotations

import hashlib
import os
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from deepagents.backends import FilesystemBackend
from deepagents.backends.protocol import EditResult, GlobResult, GrepResult, LsResult, WriteResult

from graph.session_manager import session_manager


def _sha256(data: bytes) -> str:
    return f"sha256:{hashlib.sha256(data).hexdigest()}"


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


@dataclass(frozen=True)
class AuthorizedHostPath:
    canonical_path: Path
    authority_root: Path
    grant_id: str
    access: str

    @property
    def backend_path(self) -> str:
        relative = self.canonical_path.relative_to(self.authority_root).as_posix()
        return f"/{relative}" if relative else "/"


class HostFileBroker:
    """Direct, grant-bound file operations for one Run."""

    def __init__(self, *, session_id: str, run_id: str, query_id: str) -> None:
        self.session_id = session_id
        self.run_id = run_id
        self.query_id = query_id

    @staticmethod
    def _canonical(path: str | Path, *, allow_missing_leaf: bool = False) -> Path:
        requested = Path(path).expanduser()
        if not requested.is_absolute():
            raise ValueError("HostFileBroker requires an absolute path")
        if allow_missing_leaf and not requested.exists():
            parent = requested.parent.resolve(strict=True)
            return parent / requested.name
        return requested.resolve(strict=True)

    def authorize(
        self,
        path: str | Path,
        *,
        access: str,
        allow_missing_leaf: bool = False,
    ) -> AuthorizedHostPath | None:
        if access not in {"read", "write"} or not self.session_id or not self.run_id:
            return None
        try:
            canonical = self._canonical(path, allow_missing_leaf=allow_missing_leaf)
        except (OSError, ValueError):
            return None

        candidates: list[AuthorizedHostPath] = []
        for grant in session_manager.list_permission_grants(self.session_id):
            capabilities = set(grant.get("capabilities") or [])
            if access not in capabilities:
                continue
            grant_id = str(grant.get("id") or "")
            target = str(grant.get("target") or "")
            if not grant_id or not target:
                continue
            target_kind = str(grant.get("target_kind") or "")
            grant_type = str(grant.get("type") or "")
            try:
                authority_root = Path(target).expanduser().resolve(strict=True)
            except OSError:
                continue
            if target_kind == "exact_file":
                expected_type = f"external_file_{access}"
                if grant_type != expected_type or canonical != authority_root:
                    continue
                candidates.append(
                    AuthorizedHostPath(
                        canonical_path=canonical,
                        authority_root=authority_root.parent,
                        grant_id=grant_id,
                        access=access,
                    )
                )
                continue
            if target_kind != "exact_directory" or grant_type != f"external_directory_{access}":
                continue
            if not authority_root.is_dir() or not _is_relative_to(canonical, authority_root):
                continue
            if not session_manager.has_external_directory_permission(
                self.session_id,
                authority_root,
                access=access,
                run_id=self.run_id,
            ):
                continue
            candidates.append(
                AuthorizedHostPath(
                    canonical_path=canonical,
                    authority_root=authority_root,
                    grant_id=grant_id,
                    access=access,
                )
            )
        if not candidates:
            return None
        # Prefer the narrowest authority. Exact-file roots naturally win over
        # broader directory grants because their parent depth is greatest or
        # equal and the canonical target is exact.
        return max(candidates, key=lambda item: len(item.authority_root.parts))

    @staticmethod
    def _filesystem(target: AuthorizedHostPath) -> FilesystemBackend:
        return FilesystemBackend(root_dir=target.authority_root, virtual_mode=True)

    @staticmethod
    def _restore_paths(result: LsResult | GlobResult | GrepResult, root: Path) -> Any:
        items = getattr(result, "entries", None)
        if items is None:
            items = getattr(result, "matches", None)
        if isinstance(items, list):
            for item in items:
                if not isinstance(item, dict) or not item.get("path"):
                    continue
                item["path"] = str(root / str(item["path"]).lstrip("/"))
        return result

    def read_target(self, path: str | Path) -> tuple[FilesystemBackend, str] | None:
        target = self.authorize(path, access="read")
        if target is None:
            return None
        return self._filesystem(target), target.backend_path

    def ls(self, path: str) -> LsResult | None:
        target = self.authorize(path, access="read")
        if target is None:
            return None
        result = self._filesystem(target).ls(target.backend_path)
        return self._restore_paths(result, target.authority_root)

    def glob(self, pattern: str, path: str | None = None) -> GlobResult | None:
        if not path:
            return None
        target = self.authorize(path, access="read")
        if target is None:
            return None
        result = self._filesystem(target).glob(pattern, path=target.backend_path)
        return self._restore_paths(result, target.authority_root)

    def grep(self, pattern: str, path: str | None = None, glob: str | None = None) -> GrepResult | None:
        if not path:
            return None
        target = self.authorize(path, access="read")
        if target is None:
            return None
        result = self._filesystem(target).grep(pattern, path=target.backend_path, glob=glob)
        return self._restore_paths(result, target.authority_root)

    @staticmethod
    def _current_bytes(path: Path) -> bytes | None:
        try:
            return path.read_bytes()
        except FileNotFoundError:
            return None

    @staticmethod
    def _atomic_replace(path: Path, content: bytes, *, expected_before: bytes | None) -> None:
        current = HostFileBroker._current_bytes(path)
        if current != expected_before:
            raise FileExistsError("conflict: target changed; re-read and re-apply the edit")
        mode = (path.stat().st_mode & 0o777) if path.exists() else 0o644
        fd, temporary = tempfile.mkstemp(prefix=".puddingclaw-", suffix=".tmp", dir=path.parent)
        temporary_path = Path(temporary)
        try:
            os.fchmod(fd, mode)
            with os.fdopen(fd, "wb") as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            if HostFileBroker._current_bytes(path) != expected_before:
                raise FileExistsError("conflict: target changed; re-read and re-apply the edit")
            os.replace(temporary_path, path)
            directory_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        finally:
            try:
                temporary_path.unlink()
            except FileNotFoundError:
                pass

    def _record_mutation(
        self,
        *,
        target: AuthorizedHostPath,
        operation: str,
        before: bytes | None,
        after: bytes,
    ) -> dict[str, Any]:
        now = time.time()
        payload = {
            "receipt_id": "external-mutation-"
            + hashlib.sha256(
                f"{self.session_id}:{self.run_id}:{target.canonical_path}:{_sha256(after)}".encode()
            ).hexdigest()[:20],
            "kind": "external_mutation_completed",
            "session_id": self.session_id,
            "run_id": self.run_id,
            "query_id": self.query_id,
            "permission_grant_id": target.grant_id,
            "canonical_path": str(target.canonical_path),
            "authority_root": str(target.authority_root),
            "operation": operation,
            "before_sha256": _sha256(before) if before is not None else None,
            "after_sha256": _sha256(after),
            "changed_files": [str(target.canonical_path)],
            "atomic": True,
            "status": "completed",
            "created_at": now,
        }
        return session_manager.append_external_mutation_receipt(self.session_id, payload)

    def write(self, path: str, content: str) -> WriteResult | None:
        target = self.authorize(path, access="write", allow_missing_leaf=True)
        if target is None:
            return None
        before = self._current_bytes(target.canonical_path)
        if before is not None:
            return WriteResult(error=f"File already exists: {target.canonical_path}")
        encoded = content.encode("utf-8")
        try:
            self._atomic_replace(target.canonical_path, encoded, expected_before=None)
        except OSError as exc:
            return WriteResult(error=str(exc))
        self._record_mutation(target=target, operation="create", before=None, after=encoded)
        return WriteResult(path=str(target.canonical_path))

    def edit(
        self,
        path: str,
        old_string: str,
        new_string: str,
        *,
        replace_all: bool,
    ) -> EditResult | None:
        target = self.authorize(path, access="write")
        if target is None:
            return None
        before = self._current_bytes(target.canonical_path)
        if before is None:
            return EditResult(error=f"File not found: {target.canonical_path}")
        try:
            text = before.decode("utf-8")
        except UnicodeDecodeError:
            return EditResult(error=f"File is not UTF-8 text: {target.canonical_path}")
        occurrences = text.count(old_string)
        if occurrences == 0:
            return EditResult(error="old_string was not found; re-read and rebase the edit")
        if not replace_all and occurrences != 1:
            return EditResult(error=f"old_string matched {occurrences} times; provide a unique context")
        updated = text.replace(old_string, new_string, -1 if replace_all else 1).encode("utf-8")
        try:
            self._atomic_replace(target.canonical_path, updated, expected_before=before)
        except OSError as exc:
            return EditResult(error=str(exc))
        self._record_mutation(target=target, operation="edit", before=before, after=updated)
        return EditResult(path=str(target.canonical_path), occurrences=occurrences if replace_all else 1)


__all__ = ["AuthorizedHostPath", "HostFileBroker"]
