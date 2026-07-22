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
import tempfile
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from deepagents.backends import FilesystemBackend
from deepagents.backends.protocol import EditResult, GlobResult, GrepResult, LsResult, WriteResult

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
                    candidates.append(
                        AuthorizedHostPath(
                            canonical_path=canonical,
                            authority_root=canonical.parent,
                            grant_id=grant_id,
                            access=access,
                        )
                    )
                continue
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
        after: bytes | None,
        validation_receipt: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        payload = self._mutation_payload(
            target=target,
            operation=operation,
            before=before,
            after=after,
            validation_receipt=validation_receipt,
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
        if self.validation_runner is None:
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
            return None, None
        if str(receipt.get("status") or "") == "failed":
            return receipt, _broker_error(
                "validation_failed",
                str(receipt.get("summary") or "candidate bytes failed validation"),
            )
        return receipt, None

    def write(self, path: str, content: str) -> WriteResult | None:
        target = self.authorize(path, access="write", allow_missing_leaf=True)
        if target is None:
            return None
        before = self._current_bytes(target.canonical_path)
        if before is not None:
            return WriteResult(
                error=_broker_error(
                    "conflict",
                    f"target already exists: {target.canonical_path}",
                )
            )
        encoded = content.encode("utf-8")
        validation_receipt, validation_error = self._validate_candidate(target, encoded)
        if validation_error is not None:
            return WriteResult(error=validation_error)
        try:
            self._atomic_replace(target.canonical_path, encoded, expected_before=None)
        except OSError as exc:
            code: BrokerErrorCode = "conflict" if isinstance(exc, FileExistsError) else "io_error"
            return WriteResult(error=_broker_error(code, str(exc)))
        self._record_mutation(
            target=target,
            operation="create",
            before=None,
            after=encoded,
            validation_receipt=validation_receipt,
        )
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
            self._atomic_replace(target.canonical_path, updated, expected_before=before)
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
        before = self._current_bytes(target.canonical_path)
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
            target.canonical_path.unlink()
            directory_fd = os.open(
                target.canonical_path.parent,
                os.O_RDONLY | os.O_DIRECTORY,
            )
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
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
            before = self._current_bytes(target.canonical_path)
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
                self._atomic_replace(
                    target.canonical_path,
                    after,
                    expected_before=before,
                )
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
                current = self._current_bytes(target.canonical_path)
                if current != after:
                    continue
                try:
                    if before is None:
                        target.canonical_path.unlink()
                    else:
                        self._atomic_replace(
                            target.canonical_path,
                            before,
                            expected_before=after,
                        )
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

        snapshots: list[tuple[dict[str, Any], Path, bytes | None]] = []
        for receipt in reversed(candidates):
            path = Path(str(receipt.get("canonical_path") or "")).expanduser()
            current = self._current_bytes(path)
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
            snapshots.append((receipt, path, current))

        restored: list[tuple[Path, bytes | None]] = []
        try:
            for receipt, path, current in snapshots:
                operation = str(receipt.get("operation") or "")
                if operation == "create":
                    assert current is not None
                    path.unlink()
                else:
                    before = session_manager.load_external_mutation_backup(
                        self.session_id,
                        str(receipt.get("receipt_id") or ""),
                    )
                    if before is None or _sha256(before) != str(
                        receipt.get("before_sha256") or ""
                    ):
                        raise OSError(f"rewind backup unavailable for {path}")
                    self._atomic_replace(path, before, expected_before=current)
                restored.append((path, current))
        except OSError as exc:
            for path, after in reversed(restored):
                current = self._current_bytes(path)
                try:
                    if after is None:
                        if current is not None:
                            path.unlink()
                    else:
                        self._atomic_replace(path, after, expected_before=current)
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
