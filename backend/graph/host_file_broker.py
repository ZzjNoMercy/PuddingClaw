"""Authorized host-file operations outside the project workspace.

The broker is the only direct host-filesystem authority exposed to Agent file
tools. It consumes existing exact-file or exact-directory grants, canonicalizes
every target, prevents symlink escape, performs optimistic atomic edits, and
persists mutation receipts. Shell execution remains outside this module and
continues to use the project execution backend.
"""

from __future__ import annotations

import difflib
import hashlib
import os
import stat
import time
import uuid
from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from deepagents.backends import FilesystemBackend
from deepagents.backends.protocol import (
    EditResult,
    GlobResult,
    GrepResult,
    LsResult,
    ReadResult,
    WriteResult,
)

from graph.session_manager import session_manager

BrokerErrorCode = Literal[
    "permission_required",
    "conflict",
    "validation_failed",
    "io_error",
]
ValidationRunner = Callable[["AuthorizedHostPath", bytes], dict[str, Any] | None]


def _broker_error(code: BrokerErrorCode, detail: str) -> str:
    return f"{code}: {detail}"


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
    authority_device: int
    authority_inode: int

    @property
    def backend_path(self) -> str:
        relative = self.canonical_path.relative_to(self.authority_root).as_posix()
        return f"/{relative}" if relative else "/"


class HostFileBroker:
    """Direct, grant-bound file operations for one Run."""

    def __init__(
        self,
        *,
        session_id: str,
        run_id: str,
        query_id: str,
        validation_runner: ValidationRunner | None = None,
    ) -> None:
        self.session_id = session_id
        self.run_id = run_id
        self.query_id = query_id
        self.validation_runner = validation_runner

    @staticmethod
    def _authorized_path(
        *,
        canonical_path: Path,
        authority_root: Path,
        grant_id: str,
        access: str,
    ) -> AuthorizedHostPath | None:
        try:
            root_stat = authority_root.stat(follow_symlinks=False)
        except OSError:
            return None
        if not stat.S_ISDIR(root_stat.st_mode):
            return None
        return AuthorizedHostPath(
            canonical_path=canonical_path,
            authority_root=authority_root,
            grant_id=grant_id,
            access=access,
            authority_device=root_stat.st_dev,
            authority_inode=root_stat.st_ino,
        )

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
        if access not in {"read", "write", "delete"} or not self.session_id or not self.run_id:
            return None
        try:
            canonical = self._canonical(path, allow_missing_leaf=allow_missing_leaf)
        except (OSError, ValueError):
            return None
        requested = Path(path).expanduser()
        if access in {"write", "delete"} and requested.is_symlink():
            return None
        if access == "read" and canonical.is_file():
            receipt = session_manager.find_external_mutation_receipt(
                self.session_id,
                run_id=self.run_id,
                canonical_path=str(canonical),
            )
            if isinstance(receipt, dict):
                current = self._current_bytes(canonical)
                current_sha256 = _sha256(current) if current is not None else ""
                if (
                    str(receipt.get("after_sha256") or "") == current_sha256
                    and current_sha256.startswith("sha256:")
                ):
                    return self._authorized_path(
                        canonical_path=canonical,
                        authority_root=canonical.parent,
                        grant_id=(
                            "run-mutation:"
                            + str(receipt.get("receipt_id") or self.run_id)
                        ),
                        access=access,
                    )

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
            if target_kind == "all_external_files":
                # A broad file-read Grant covers any *known exact file*, but
                # never authorizes directory discovery.  In particular,
                # grep(path=<file>) may reuse it while grep/glob/ls on a
                # directory must still obtain an exact-directory Grant.
                if (
                    access == "read"
                    and grant_type == "external_file_read"
                    and canonical.is_file()
                ):
                    candidate = self._authorized_path(
                        canonical_path=canonical,
                        authority_root=canonical.parent,
                        grant_id=grant_id,
                        access=access,
                    )
                    if candidate is not None:
                        candidates.append(candidate)
                continue
            try:
                authority_root = Path(target).expanduser().resolve(strict=True)
            except OSError:
                continue
            if target_kind == "exact_file":
                expected_type = f"external_file_{access}"
                if grant_type != expected_type or canonical != authority_root:
                    continue
                candidate = self._authorized_path(
                    canonical_path=canonical,
                    authority_root=authority_root.parent,
                    grant_id=grant_id,
                    access=access,
                )
                if candidate is not None:
                    candidates.append(candidate)
                continue
            directory_grant_type = (
                "external_directory_write"
                if access == "delete"
                else f"external_directory_{access}"
            )
            if target_kind != "exact_directory" or grant_type != directory_grant_type:
                continue
            if not authority_root.is_dir() or not _is_relative_to(canonical, authority_root):
                continue
            if not session_manager.has_external_directory_permission(
                self.session_id,
                authority_root,
                access="write" if access == "delete" else access,
                run_id=self.run_id,
            ):
                continue
            candidate = self._authorized_path(
                canonical_path=canonical,
                authority_root=authority_root,
                grant_id=grant_id,
                access=access,
            )
            if candidate is not None:
                candidates.append(candidate)
        if not candidates:
            if access == "write":
                run = session_manager.get_run_state(self.session_id, self.run_id)
                declared_targets = (
                    run.get("declared_artifact_targets")
                    if isinstance(run, dict)
                    else None
                )
                for raw_target in declared_targets or []:
                    try:
                        declared = self._canonical(
                            str(raw_target),
                            allow_missing_leaf=True,
                        )
                    except (OSError, ValueError):
                        continue
                    if declared == canonical:
                        return self._authorized_path(
                            canonical_path=canonical,
                            authority_root=canonical.parent,
                            grant_id=f"declared-artifact:{self.run_id}",
                            access=access,
                        )
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

    def read(self, path: str | Path, *, offset: int = 0, limit: int = 2000) -> ReadResult | None:
        target = self.authorize(path, access="read")
        if target is None:
            return None
        try:
            content = self._read_bound_bytes(target)
        except OSError as exc:
            return ReadResult(error=_broker_error("io_error", str(exc)))
        if content is None:
            return ReadResult(
                error=_broker_error(
                    "io_error",
                    f"target no longer exists: {target.canonical_path}",
                )
            )
        try:
            text = content.decode("utf-8")
        except UnicodeDecodeError:
            return ReadResult(
                error=_broker_error(
                    "io_error",
                    f"target is not UTF-8 text: {target.canonical_path}",
                )
            )
        lines = text.splitlines(keepends=True)
        return ReadResult(
            file_data={
                "content": "".join(lines[max(0, offset) : max(0, offset) + max(0, limit)]),
                "encoding": "utf-8",
            }
        )

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
    @contextmanager
    def _bound_parent(
        target: AuthorizedHostPath,
    ):
        """Bind file access to the directory inode that authorized the call.

        Path re-resolution after authorization is a classic check/use race.
        Opening every path component with ``O_NOFOLLOW`` and then using *at
        syscalls keeps the commit inside the granted directory even if an
        attacker renames or replaces a parent while validation is running.
        """

        nofollow = getattr(os, "O_NOFOLLOW", 0)
        cloexec = getattr(os, "O_CLOEXEC", 0)
        flags = os.O_RDONLY | os.O_DIRECTORY | nofollow | cloexec
        directory_fd = os.open(target.authority_root, flags)
        try:
            root_stat = os.fstat(directory_fd)
            if (
                root_stat.st_dev != target.authority_device
                or root_stat.st_ino != target.authority_inode
            ):
                raise PermissionError(
                    "authority root changed after permission was resolved"
                )
            relative = target.canonical_path.relative_to(target.authority_root)
            if not relative.parts or relative.name in {"", ".", ".."}:
                raise PermissionError("file operation requires a leaf below authority root")
            for component in relative.parent.parts:
                if component in {"", ".", ".."}:
                    raise PermissionError("unsafe relative path component")
                next_fd = os.open(component, flags, dir_fd=directory_fd)
                os.close(directory_fd)
                directory_fd = next_fd
            yield directory_fd, relative.name
        finally:
            os.close(directory_fd)

    @staticmethod
    def _read_bound_bytes(target: AuthorizedHostPath) -> bytes | None:
        nofollow = getattr(os, "O_NOFOLLOW", 0)
        cloexec = getattr(os, "O_CLOEXEC", 0)
        with HostFileBroker._bound_parent(target) as (directory_fd, leaf):
            try:
                file_fd = os.open(
                    leaf,
                    os.O_RDONLY | nofollow | cloexec,
                    dir_fd=directory_fd,
                )
            except FileNotFoundError:
                return None
            try:
                file_stat = os.fstat(file_fd)
                if not stat.S_ISREG(file_stat.st_mode):
                    raise OSError(f"target is not a regular file: {target.canonical_path}")
                with os.fdopen(file_fd, "rb", closefd=False) as stream:
                    return stream.read()
            finally:
                os.close(file_fd)

    @staticmethod
    def _atomic_replace(
        target: AuthorizedHostPath,
        content: bytes,
        *,
        expected_before: bytes | None,
    ) -> None:
        nofollow = getattr(os, "O_NOFOLLOW", 0)
        cloexec = getattr(os, "O_CLOEXEC", 0)
        with HostFileBroker._bound_parent(target) as (directory_fd, leaf):
            current = HostFileBroker._read_bound_bytes(target)
            if current != expected_before:
                raise FileExistsError(
                    "conflict: target changed; re-read and re-apply the edit"
                )
            try:
                target_stat = os.stat(
                    leaf,
                    dir_fd=directory_fd,
                    follow_symlinks=False,
                )
                if not stat.S_ISREG(target_stat.st_mode):
                    raise OSError(
                        f"target is not a regular file: {target.canonical_path}"
                    )
                mode = target_stat.st_mode & 0o777
            except FileNotFoundError:
                mode = 0o644
            temporary_name = f".puddingclaw-{uuid.uuid4().hex}.tmp"
            temporary_fd = os.open(
                temporary_name,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | nofollow | cloexec,
                mode,
                dir_fd=directory_fd,
            )
            try:
                os.fchmod(temporary_fd, mode)
                with os.fdopen(temporary_fd, "wb", closefd=False) as stream:
                    stream.write(content)
                    stream.flush()
                    os.fsync(stream.fileno())
                if HostFileBroker._read_bound_bytes(target) != expected_before:
                    raise FileExistsError(
                        "conflict: target changed; re-read and re-apply the edit"
                    )
                os.replace(
                    temporary_name,
                    leaf,
                    src_dir_fd=directory_fd,
                    dst_dir_fd=directory_fd,
                )
                os.fsync(directory_fd)
            finally:
                os.close(temporary_fd)
                try:
                    os.unlink(temporary_name, dir_fd=directory_fd)
                except FileNotFoundError:
                    pass

    @staticmethod
    def _unlink_bound(
        target: AuthorizedHostPath,
        *,
        expected_before: bytes,
    ) -> None:
        with HostFileBroker._bound_parent(target) as (directory_fd, leaf):
            if HostFileBroker._read_bound_bytes(target) != expected_before:
                raise FileExistsError(
                    "conflict: target changed; re-read before deleting"
                )
            target_stat = os.stat(
                leaf,
                dir_fd=directory_fd,
                follow_symlinks=False,
            )
            if not stat.S_ISREG(target_stat.st_mode):
                raise OSError(
                    f"target is not a regular file: {target.canonical_path}"
                )
            os.unlink(leaf, dir_fd=directory_fd)
            os.fsync(directory_fd)

    def _record_mutation(
        self,
        *,
        target: AuthorizedHostPath,
        operation: str,
        before: bytes | None,
        after: bytes | None,
        validation_receipt: dict[str, Any] | None = None,
        warnings: list[str] | None = None,
    ) -> dict[str, Any]:
        payload = self._mutation_payload(
            target=target,
            operation=operation,
            before=before,
            after=after,
            validation_receipt=validation_receipt,
            warnings=warnings,
        )
        return session_manager.append_external_mutation_receipt(
            self.session_id,
            payload,
            before_bytes=before,
        )

    def _mutation_payload(
        self,
        *,
        target: AuthorizedHostPath,
        operation: str,
        before: bytes | None,
        after: bytes | None,
        validation_receipt: dict[str, Any] | None = None,
        transaction_id: str | None = None,
        warnings: list[str] | None = None,
    ) -> dict[str, Any]:
        now = time.time()
        before_sha256 = _sha256(before) if before is not None else None
        after_sha256 = _sha256(after) if after is not None else "deleted"
        diff = self._text_diff(target.canonical_path, before, after)
        payload = {
            "receipt_id": "external-mutation-"
            + hashlib.sha256(
                (
                    f"{self.session_id}:{self.run_id}:{target.canonical_path}:"
                    f"{operation}:{before_sha256}:{after_sha256}:{uuid.uuid4().hex}"
                ).encode()
            ).hexdigest()[:20],
            "kind": "external_mutation_completed",
            "session_id": self.session_id,
            "run_id": self.run_id,
            "query_id": self.query_id,
            "permission_grant_id": target.grant_id,
            "canonical_path": str(target.canonical_path),
            "authority_root": str(target.authority_root),
            "authority_device": target.authority_device,
            "authority_inode": target.authority_inode,
            "operation": operation,
            "before_sha256": before_sha256,
            "after_sha256": after_sha256,
            "before_version_token": before_sha256 or "missing",
            "after_version_token": after_sha256,
            "changed_files": [str(target.canonical_path)],
            "atomic": True,
            "status": "completed",
            "created_at": now,
        }
        if transaction_id:
            payload["transaction_id"] = transaction_id
        if warnings:
            payload["warnings"] = list(dict.fromkeys(warnings))
        if diff:
            payload["diff"] = diff
            payload["diff_sha256"] = _sha256(diff.encode("utf-8"))
        if validation_receipt is not None:
            payload["validation_receipt"] = validation_receipt
            payload["validation_receipt_id"] = validation_receipt.get(
                "validation_receipt_id"
            )
        return payload

    @staticmethod
    def _text_diff(
        path: Path,
        before: bytes | None,
        after: bytes | None,
    ) -> str:
        try:
            before_text = before.decode("utf-8") if before is not None else ""
            after_text = after.decode("utf-8") if after is not None else ""
        except UnicodeDecodeError:
            return ""
        rendered = "".join(
            difflib.unified_diff(
                before_text.splitlines(keepends=True),
                after_text.splitlines(keepends=True),
                fromfile=f"a/{path.name}",
                tofile=f"b/{path.name}",
            )
        )
        return rendered[:100_000]

    def _validate_candidate(
        self,
        target: AuthorizedHostPath,
        content: bytes,
    ) -> tuple[dict[str, Any] | None, str | None]:
        validation_required = target.canonical_path.suffix.lower() in {
            ".js",
            ".mjs",
            ".cjs",
            ".py",
            ".json",
            ".html",
            ".htm",
        }
        if self.validation_runner is None:
            if validation_required:
                return None, _broker_error(
                    "io_error",
                    "required registered validator is unavailable",
                )
            return None, None
        try:
            receipt = self.validation_runner(target, content)
        except Exception as exc:  # noqa: BLE001
            # Validation infrastructure is not itself permission to corrupt a
            # target. Surface an explicit unverified condition and leave the
            # original bytes untouched.
            return None, _broker_error(
                "io_error",
                f"validation bridge failed ({type(exc).__name__}): {exc}",
            )
        if not isinstance(receipt, dict):
            if validation_required:
                return None, _broker_error(
                    "io_error",
                    "required registered validator returned no receipt",
                )
            return None, None
        if str(receipt.get("status") or "") == "failed":
            return receipt, _broker_error(
                "validation_failed",
                str(receipt.get("summary") or "candidate bytes failed validation"),
            )
        return receipt, None

    @staticmethod
    def _result_receipt_fields(
        receipt: dict[str, Any],
        validation_receipt: dict[str, Any] | None,
    ) -> dict[str, Any]:
        """Expose stable protocol names while retaining legacy aliases."""

        mutation_receipt_id = str(receipt.get("receipt_id") or "")
        validation_receipt_id = (
            str(validation_receipt.get("validation_receipt_id") or "")
            if isinstance(validation_receipt, dict)
            else ""
        )
        return {
            # Compatibility for existing clients.
            "receipt_id": mutation_receipt_id,
            "validation_receipt": validation_receipt,
            # Canonical control-plane contract.
            "mutation_receipt_id": mutation_receipt_id,
            "validation_receipt_ids": (
                [validation_receipt_id] if validation_receipt_id else []
            ),
        }

    def write(self, path: str, content: str) -> WriteResult | None:
        result = self.create(path, content.encode("utf-8"))
        if result.get("status") == "permission_required":
            return None
        if result.get("status") != "completed":
            return WriteResult(error=str(result.get("error") or "io_error: create failed"))
        return WriteResult(path=str(result.get("target_path") or path))

    def create(
        self,
        path: str,
        content: bytes,
        *,
        operation: str = "create",
    ) -> dict[str, Any]:
        target = self.authorize(path, access="write", allow_missing_leaf=True)
        if target is None:
            return {
                "status": "permission_required",
                "error_code": "permission_required",
                "error": _broker_error(
                    "permission_required",
                    f"write permission is required for {path}",
                ),
                "next_action": "request_exact_write_permission",
            }
        try:
            before = self._read_bound_bytes(target)
        except OSError as exc:
            return {
                "status": "io_error",
                "error_code": "authorized_path_changed",
                "error": _broker_error("io_error", str(exc)),
                "next_action": "request_exact_write_permission",
            }
        if before is not None:
            return {
                "status": "conflict",
                "error_code": "target_already_exists",
                "error": _broker_error(
                    "conflict",
                    f"target already exists: {target.canonical_path}",
                ),
                "current_sha256": _sha256(before),
                "next_action": "use_replace_file",
            }
        prior_delete = next(
            (
                item
                for item in reversed(
                    session_manager.list_external_mutation_receipts(
                        self.session_id,
                        run_id=self.run_id,
                    )
                )
                if str(item.get("canonical_path") or "")
                == str(target.canonical_path)
                and str(item.get("operation") or "") == "delete"
                and str(item.get("status") or "") == "completed"
            ),
            None,
        )
        warnings = (
            [
                "overwrite_via_delete_deprecated: use replace_file with "
                "expected_sha256 so the original remains recoverable until commit"
            ]
            if prior_delete is not None
            else []
        )
        validation_receipt, validation_error = self._validate_candidate(target, content)
        if validation_error is not None:
            return {
                "status": "validation_failed",
                "error_code": "candidate_validation_failed",
                "error": validation_error,
                "next_action": "fix_candidate_content",
                "validation_receipt": validation_receipt,
            }
        try:
            self._atomic_replace(target, content, expected_before=None)
        except OSError as exc:
            code: BrokerErrorCode = "conflict" if isinstance(exc, FileExistsError) else "io_error"
            return {
                "status": code,
                "error_code": "atomic_create_failed",
                "error": _broker_error(code, str(exc)),
                "next_action": (
                    "use_replace_file"
                    if code == "conflict"
                    else "report_infrastructure_error"
                ),
            }
        receipt = self._record_mutation(
            target=target,
            operation=operation,
            before=None,
            after=content,
            validation_receipt=validation_receipt,
            warnings=warnings,
        )
        return {
            "status": "completed",
            "target_path": str(target.canonical_path),
            "target_sha256": _sha256(content),
            **self._result_receipt_fields(receipt, validation_receipt),
            "warnings": warnings,
        }

    def replace(
        self,
        path: str,
        content: bytes,
        *,
        expected_sha256: str,
        operation: str = "replace",
    ) -> dict[str, Any]:
        """Atomically replace one exact file under a compare-and-swap guard."""

        target = self.authorize(path, access="write")
        if target is None:
            return {
                "status": "permission_required",
                "error_code": "permission_required",
                "error": _broker_error(
                    "permission_required",
                    f"write permission is required for {path}",
                ),
                "next_action": "request_exact_write_permission",
            }
        try:
            before = self._read_bound_bytes(target)
        except OSError as exc:
            return {
                "status": "io_error",
                "error_code": "authorized_path_changed",
                "error": _broker_error("io_error", str(exc)),
                "next_action": "request_exact_write_permission",
            }
        if before is None:
            return {
                "status": "conflict",
                "error_code": "target_missing",
                "error": _broker_error("conflict", f"target no longer exists: {path}"),
                "current_sha256": "missing",
                "expected_sha256": expected_sha256,
                "next_action": "use_create_mode",
            }
        current_sha256 = _sha256(before)
        if current_sha256 != expected_sha256:
            return {
                "status": "conflict",
                "error_code": "source_version_changed",
                "error": _broker_error(
                    "conflict",
                    f"target changed; expected {expected_sha256}, current {current_sha256}",
                ),
                "current_sha256": current_sha256,
                "expected_sha256": expected_sha256,
                "next_action": "inspect_conflicting_region",
            }
        validation_receipt, validation_error = self._validate_candidate(
            target,
            content,
        )
        if validation_error is not None:
            return {
                "status": "validation_failed",
                "error_code": "candidate_validation_failed",
                "error": validation_error,
                "current_sha256": current_sha256,
                "expected_sha256": expected_sha256,
                "next_action": "fix_candidate_content",
                "validation_receipt": validation_receipt,
            }
        try:
            self._atomic_replace(target, content, expected_before=before)
        except OSError as exc:
            code: BrokerErrorCode = (
                "conflict" if isinstance(exc, FileExistsError) else "io_error"
            )
            return {
                "status": code,
                "error_code": (
                    "concurrent_write_conflict"
                    if code == "conflict"
                    else "atomic_replace_failed"
                ),
                "error": _broker_error(code, str(exc)),
                "current_sha256": (
                    _sha256(self._read_bound_bytes(target) or b"")
                    if self._read_bound_bytes(target) is not None
                    else "missing"
                ),
                "expected_sha256": expected_sha256,
                "next_action": (
                    "retry_once_with_latest_version"
                    if code == "conflict"
                    else "report_infrastructure_error"
                ),
            }
        receipt = self._record_mutation(
            target=target,
            operation=operation,
            before=before,
            after=content,
            validation_receipt=validation_receipt,
        )
        return {
            "status": "completed",
            "target_path": str(target.canonical_path),
            "previous_sha256": current_sha256,
            "target_sha256": _sha256(content),
            **self._result_receipt_fields(receipt, validation_receipt),
        }

    def copy(
        self,
        source_path: str,
        target_path: str,
        *,
        expected_source_sha256: str | None = None,
    ) -> dict[str, Any]:
        """Create one exact target from source bytes without model transport."""

        source = self.authorize(source_path, access="read")
        if source is None:
            return {
                "status": "permission_required",
                "error_code": "source_read_permission_required",
                "error": _broker_error(
                    "permission_required",
                    f"read permission is required for {source_path}",
                ),
                "next_action": "request_exact_read_permission",
            }
        target = self.authorize(
            target_path,
            access="write",
            allow_missing_leaf=True,
        )
        if target is None:
            return {
                "status": "permission_required",
                "error_code": "target_write_permission_required",
                "error": _broker_error(
                    "permission_required",
                    f"write permission is required for {target_path}",
                ),
                "next_action": "request_exact_write_permission",
            }
        try:
            content = self._read_bound_bytes(source)
            target_before = self._read_bound_bytes(target)
        except OSError as exc:
            return {
                "status": "io_error",
                "error_code": "authorized_path_changed",
                "error": _broker_error("io_error", str(exc)),
                "next_action": "request_exact_directory_permission",
            }
        if content is None:
            return {
                "status": "io_error",
                "error_code": "source_not_regular_file",
                "error": _broker_error(
                    "io_error",
                    f"source is not a regular file: {source.canonical_path}",
                ),
                "next_action": "choose_regular_source_file",
            }
        if target_before is not None:
            return {
                "status": "conflict",
                "error_code": "target_already_exists",
                "error": _broker_error(
                    "conflict",
                    f"target already exists: {target.canonical_path}",
                ),
                "current_sha256": _sha256(target_before),
                "next_action": "use_replace_file",
            }
        source_sha256 = _sha256(content)
        if expected_source_sha256 and expected_source_sha256 != source_sha256:
            return {
                "status": "conflict",
                "error_code": "source_version_changed",
                "error": _broker_error(
                    "conflict",
                    f"source changed; expected {expected_source_sha256}, current {source_sha256}",
                ),
                "current_sha256": source_sha256,
                "expected_sha256": expected_source_sha256,
                "next_action": "retry_once_with_latest_version",
            }
        validation_receipt, validation_error = self._validate_candidate(
            target,
            content,
        )
        if validation_error is not None:
            return {
                "status": "validation_failed",
                "error_code": "candidate_validation_failed",
                "error": validation_error,
                "next_action": "fix_source_before_copy",
                "validation_receipt": validation_receipt,
            }
        try:
            self._atomic_replace(target, content, expected_before=None)
        except OSError as exc:
            code: BrokerErrorCode = (
                "conflict" if isinstance(exc, FileExistsError) else "io_error"
            )
            return {
                "status": code,
                "error_code": (
                    "target_already_exists"
                    if code == "conflict"
                    else "atomic_copy_failed"
                ),
                "error": _broker_error(code, str(exc)),
                "next_action": (
                    "use_replace_file"
                    if code == "conflict"
                    else "report_infrastructure_error"
                ),
            }
        payload = self._mutation_payload(
            target=target,
            operation="copy",
            before=None,
            after=content,
            validation_receipt=validation_receipt,
        )
        payload["source_path"] = str(source.canonical_path)
        payload["source_sha256"] = source_sha256
        receipt = session_manager.append_external_mutation_receipt(
            self.session_id,
            payload,
            before_bytes=None,
        )
        return {
            "status": "completed",
            "source_path": str(source.canonical_path),
            "source_sha256": source_sha256,
            "target_path": str(target.canonical_path),
            "target_sha256": source_sha256,
            **self._result_receipt_fields(receipt, validation_receipt),
        }

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
        try:
            before = self._read_bound_bytes(target)
        except OSError as exc:
            return EditResult(error=_broker_error("io_error", str(exc)))
        if before is None:
            return EditResult(
                error=_broker_error(
                    "conflict",
                    f"target no longer exists: {target.canonical_path}",
                )
            )
        try:
            text = before.decode("utf-8")
        except UnicodeDecodeError:
            return EditResult(
                error=_broker_error(
                    "io_error",
                    f"target is not UTF-8 text: {target.canonical_path}",
                )
            )
        occurrences = text.count(old_string)
        if occurrences == 0:
            return EditResult(
                error=_broker_error(
                    "conflict",
                    "old_string was not found; re-read and rebase the edit",
                )
            )
        if not replace_all and occurrences != 1:
            return EditResult(
                error=_broker_error(
                    "conflict",
                    f"old_string matched {occurrences} times; provide a unique context",
                )
            )
        updated = text.replace(old_string, new_string, -1 if replace_all else 1).encode("utf-8")
        validation_receipt, validation_error = self._validate_candidate(target, updated)
        if validation_error is not None:
            return EditResult(error=validation_error)
        try:
            self._atomic_replace(target, updated, expected_before=before)
        except OSError as exc:
            code = "conflict" if isinstance(exc, FileExistsError) else "io_error"
            return EditResult(error=_broker_error(code, str(exc)))
        self._record_mutation(
            target=target,
            operation="edit",
            before=before,
            after=updated,
            validation_receipt=validation_receipt,
        )
        return EditResult(path=str(target.canonical_path), occurrences=occurrences if replace_all else 1)

    def delete(self, path: str, *, expected_sha256: str) -> dict[str, Any]:
        """Delete one exact file; directory and bulk deletion are never implicit."""

        target = self.authorize(path, access="delete")
        if target is None:
            return {
                "status": "permission_required",
                "error": _broker_error(
                    "permission_required",
                    f"delete permission is required for {path}",
                ),
            }
        try:
            before = self._read_bound_bytes(target)
        except OSError as exc:
            return {
                "status": "io_error",
                "error": _broker_error("io_error", str(exc)),
            }
        if before is None:
            return {
                "status": "conflict",
                "error": _broker_error("conflict", f"target no longer exists: {path}"),
            }
        current = _sha256(before)
        if expected_sha256 != current:
            return {
                "status": "conflict",
                "error": _broker_error(
                    "conflict",
                    f"target changed; expected {expected_sha256}, current {current}",
                ),
            }
        try:
            self._unlink_bound(target, expected_before=before)
        except OSError as exc:
            return {"status": "io_error", "error": _broker_error("io_error", str(exc))}
        receipt = self._record_mutation(
            target=target,
            operation="delete",
            before=before,
            after=None,
        )
        return {
            "status": "completed",
            "deleted_path": str(target.canonical_path),
            "receipt_id": receipt.get("receipt_id"),
        }

    def apply_transaction(
        self,
        changes: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """Atomically apply multiple already-rendered external file updates."""

        if not changes:
            return {
                "status": "io_error",
                "error": _broker_error("io_error", "transaction has no changes"),
            }
        transaction_id = "external-transaction-" + uuid.uuid4().hex[:20]
        prepared: list[
            tuple[AuthorizedHostPath, bytes | None, bytes, dict[str, Any] | None]
        ] = []
        seen: set[Path] = set()
        for change in changes:
            path = str(change.get("file_path") or "")
            content = change.get("content")
            if not isinstance(content, str):
                return {
                    "status": "io_error",
                    "error": _broker_error("io_error", f"missing text content for {path}"),
                }
            target = self.authorize(path, access="write", allow_missing_leaf=True)
            if target is None:
                return {
                    "status": "permission_required",
                    "error": _broker_error(
                        "permission_required",
                        f"write permission is required for {path}",
                    ),
                }
            if target.canonical_path in seen:
                return {
                    "status": "conflict",
                    "error": _broker_error(
                        "conflict",
                        f"transaction contains duplicate target {target.canonical_path}",
                    ),
                }
            seen.add(target.canonical_path)
            try:
                before = self._read_bound_bytes(target)
            except OSError as exc:
                return {
                    "status": "io_error",
                    "error": _broker_error("io_error", str(exc)),
                }
            expected = str(change.get("expected_sha256") or "")
            current = _sha256(before) if before is not None else "missing"
            if expected != current:
                return {
                    "status": "conflict",
                    "error": _broker_error(
                        "conflict",
                        f"{target.canonical_path} expected {expected}, current {current}",
                    ),
                }
            after = content.encode("utf-8")
            validation_receipt, validation_error = self._validate_candidate(
                target,
                after,
            )
            if validation_error is not None:
                return {"status": "validation_failed", "error": validation_error}
            prepared.append((target, before, after, validation_receipt))

        applied: list[tuple[AuthorizedHostPath, bytes | None, bytes]] = []
        try:
            for target, before, after, _validation in prepared:
                self._atomic_replace(target, after, expected_before=before)
                applied.append((target, before, after))
            payloads = [
                (
                    self._mutation_payload(
                        target=target,
                        operation="create" if before is None else "edit",
                        before=before,
                        after=after,
                        validation_receipt=validation,
                        transaction_id=transaction_id,
                    ),
                    before,
                )
                for target, before, after, validation in prepared
            ]
            persisted = session_manager.append_external_mutation_receipts_atomic(
                self.session_id,
                payloads,
            )
        except Exception as exc:  # noqa: BLE001
            for target, before, after in reversed(applied):
                try:
                    current = self._read_bound_bytes(target)
                except OSError:
                    continue
                if current != after:
                    continue
                try:
                    if before is None:
                        self._unlink_bound(target, expected_before=after)
                    else:
                        self._atomic_replace(target, before, expected_before=after)
                except OSError:
                    pass
            code: BrokerErrorCode = (
                "conflict" if isinstance(exc, FileExistsError) else "io_error"
            )
            return {"status": code, "error": _broker_error(code, str(exc))}
        return {
            "status": "completed",
            "transaction_id": transaction_id,
            "changed_files": [str(item[0].canonical_path) for item in prepared],
            "receipt_ids": [str(item.get("receipt_id") or "") for item in persisted],
        }

    def rewind_run(self) -> dict[str, Any]:
        """Undo this Run's completed Broker mutations after exact hash checks."""

        receipts = session_manager.list_external_mutation_receipts(
            self.session_id,
            run_id=self.run_id,
        )
        already_rewound = {
            str(receipt_id)
            for receipt in receipts
            if str(receipt.get("operation") or "") == "rewind"
            for receipt_id in receipt.get("rewinds_receipt_ids") or []
        }
        candidates = [
            item
            for item in receipts
            if str(item.get("status") or "") == "completed"
            and str(item.get("operation") or "") in {"create", "edit", "delete"}
            and str(item.get("receipt_id") or "") not in already_rewound
            and item.get("rewindable") is True
        ]
        if not candidates:
            return {"status": "noop", "rewound_receipt_ids": []}

        snapshots: list[
            tuple[dict[str, Any], AuthorizedHostPath, bytes | None]
        ] = []
        for receipt in reversed(candidates):
            path = Path(str(receipt.get("canonical_path") or "")).expanduser()
            authority_root = Path(
                str(receipt.get("authority_root") or path.parent)
            ).expanduser()
            try:
                target = self._authorized_path(
                    canonical_path=path,
                    authority_root=authority_root,
                    grant_id=str(receipt.get("permission_grant_id") or ""),
                    access="write",
                )
                if target is None:
                    raise PermissionError(
                        "authority root is unavailable or no longer a directory"
                    )
                recorded_device = receipt.get("authority_device")
                recorded_inode = receipt.get("authority_inode")
                if (
                    recorded_device is not None
                    and int(recorded_device) != target.authority_device
                ) or (
                    recorded_inode is not None
                    and int(recorded_inode) != target.authority_inode
                ):
                    raise PermissionError(
                        "authority root changed since the mutation receipt"
                    )
                current = self._read_bound_bytes(target)
            except (OSError, ValueError) as exc:
                return {
                    "status": "conflict",
                    "error": _broker_error("conflict", str(exc)),
                    "rewound_receipt_ids": [],
                }
            operation = str(receipt.get("operation") or "")
            current_matches = (
                current is None
                if operation == "delete"
                else current is not None
                and _sha256(current) == str(receipt.get("after_sha256") or "")
            )
            if not current_matches:
                return {
                    "status": "conflict",
                    "error": _broker_error(
                        "conflict",
                        f"{path} changed after this Run; rewind refused",
                    ),
                    "rewound_receipt_ids": [],
                }
            snapshots.append((receipt, target, current))

        restored: list[tuple[AuthorizedHostPath, bytes | None]] = []
        try:
            for receipt, target, current in snapshots:
                path = target.canonical_path
                operation = str(receipt.get("operation") or "")
                if operation == "create":
                    assert current is not None
                    self._unlink_bound(target, expected_before=current)
                else:
                    before = session_manager.load_external_mutation_backup(
                        self.session_id,
                        str(receipt.get("receipt_id") or ""),
                    )
                    if before is None or _sha256(before) != str(
                        receipt.get("before_sha256") or ""
                    ):
                        raise OSError(f"rewind backup unavailable for {path}")
                    self._atomic_replace(target, before, expected_before=current)
                restored.append((target, current))
        except OSError as exc:
            for target, after in reversed(restored):
                current = self._read_bound_bytes(target)
                try:
                    if after is None:
                        if current is not None:
                            self._unlink_bound(target, expected_before=current)
                    else:
                        self._atomic_replace(target, after, expected_before=current)
                except OSError:
                    pass
            return {
                "status": "io_error",
                "error": _broker_error("io_error", str(exc)),
                "rewound_receipt_ids": [],
            }

        rewound_ids = [str(item[0].get("receipt_id") or "") for item in snapshots]
        receipt = {
            "receipt_id": "external-rewind-" + uuid.uuid4().hex[:20],
            "kind": "external_mutation_rewind",
            "session_id": self.session_id,
            "run_id": self.run_id,
            "query_id": self.query_id,
            "operation": "rewind",
            "rewinds_receipt_ids": rewound_ids,
            "changed_files": [str(item[1]) for item in snapshots],
            "atomic": True,
            "status": "completed",
            "created_at": time.time(),
        }
        session_manager.append_external_mutation_receipt(self.session_id, receipt)
        return {"status": "completed", "rewound_receipt_ids": rewound_ids}


__all__ = [
    "AuthorizedHostPath",
    "BrokerErrorCode",
    "HostFileBroker",
]
