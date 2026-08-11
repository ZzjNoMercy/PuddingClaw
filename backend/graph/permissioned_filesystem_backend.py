"""DeepAgents filesystem backend with exact-file external write grants."""

from __future__ import annotations

import asyncio
import hashlib
import json
import shlex
import shutil
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

from deepagents.backends import CompositeBackend, FilesystemBackend
from deepagents.backends.protocol import (
    EditResult,
    GlobResult,
    GrepResult,
    LsResult,
    ReadResult,
    WriteResult,
)

from graph.host_file_broker import AuthorizedHostPath, HostFileBroker
from graph.session_manager import session_manager
from graph.virtual_paths import ClassifiedPath, PathAuthority, classify_path_authority


class _CandidateHTMLValidator(HTMLParser):
    """Small deterministic balance check for pre-commit HTML candidates."""

    _VOID = frozenset(
        {
            "area",
            "base",
            "br",
            "col",
            "embed",
            "hr",
            "img",
            "input",
            "link",
            "meta",
            "param",
            "source",
            "track",
            "wbr",
        }
    )

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.stack: list[str] = []
        self.errors: list[str] = []
        self.tags: set[str] = set()
        self.ids: set[str] = set()
        self.duplicate_ids: set[str] = set()
        self.local_resource_refs: list[str] = []

    def _record_attrs(
        self,
        tag: str,
        attrs: list[tuple[str, str | None]],
    ) -> None:
        normalized_attrs = {
            str(key).lower(): str(value or "")
            for key, value in attrs
        }
        element_id = normalized_attrs.get("id", "").strip()
        if element_id:
            if element_id in self.ids:
                self.duplicate_ids.add(element_id)
            self.ids.add(element_id)
        resource = (
            normalized_attrs.get("src", "")
            if tag == "script"
            else normalized_attrs.get("href", "")
            if tag == "link"
            else ""
        ).strip()
        if (
            resource
            and not resource.startswith(("#", "//"))
            and "://" not in resource
            and not resource.lower().startswith(("data:", "javascript:"))
        ):
            self.local_resource_refs.append(resource)

    def handle_starttag(
        self,
        tag: str,
        attrs: list[tuple[str, str | None]],
    ) -> None:
        normalized = tag.lower()
        self.tags.add(normalized)
        self._record_attrs(normalized, attrs)
        if normalized not in self._VOID:
            self.stack.append(normalized)

    def handle_startendtag(
        self,
        tag: str,
        attrs: list[tuple[str, str | None]],
    ) -> None:
        normalized = tag.lower()
        self.tags.add(normalized)
        self._record_attrs(normalized, attrs)
        return

    def handle_endtag(self, tag: str) -> None:
        normalized = tag.lower()
        if normalized in self._VOID:
            return
        if not self.stack or self.stack[-1] != normalized:
            self.errors.append(f"unexpected closing tag </{normalized}>")
            return
        self.stack.pop()


class PermissionedCompositeBackend(CompositeBackend):
    """Delegate approved external writes while keeping normal virtual routing."""

    _READONLY_VIRTUAL_PREFIXES = (
        "/knowledge/",
        "/semantic-assets/",
        "/sql-guardrails/",
        "/analytics-models/",
        "/skills/",
    )

    def __init__(
        self,
        *,
        default: Any,
        routes: dict[str, Any],
        session_id: str,
        managed_readonly_roots: tuple[Path, ...] = (),
        workspace_root: Path | None = None,
        run_id: str = "",
        query_id: str = "",
    ) -> None:
        super().__init__(default=default, routes=routes)
        self.session_id = session_id
        self.run_id = run_id
        self.query_id = query_id
        self.host_file_broker = (
            HostFileBroker(
                session_id=session_id,
                run_id=run_id,
                query_id=query_id,
                validation_runner=self._validate_external_candidate,
            )
            if session_id and run_id
            else None
        )
        self.managed_readonly_roots = tuple(root.expanduser().resolve() for root in managed_readonly_roots)
        resolved_workspace = workspace_root.expanduser().resolve() if workspace_root is not None else None
        self.workspace_root = resolved_workspace
        workspace_prefixes: list[str] = []
        if resolved_workspace is not None:
            for root in self.managed_readonly_roots:
                try:
                    relative = root.relative_to(resolved_workspace).as_posix().strip("/")
                except ValueError:
                    continue
                if relative:
                    workspace_prefixes.append(f"/workspace/{relative}/")
        self._readonly_workspace_prefixes = tuple(workspace_prefixes)
        self.external_directory_writable_enabled = False

    def _classify_path(self, file_path: str) -> ClassifiedPath:
        return classify_path_authority(
            file_path,
            workspace_root=self.workspace_root,
        )

    def _has_internal_virtual_route(self, classified: ClassifiedPath) -> bool:
        """Require writable virtual namespaces to resolve to their own route."""

        if not classified.internally_writable or not classified.virtual_path:
            return False
        expected_prefix = f"/{classified.authority.value}"
        expected_backend = next(
            (
                backend
                for prefix, backend in self.routes.items()
                if prefix.rstrip("/") == expected_prefix
            ),
            None,
        )
        if expected_backend is None:
            return False
        routed_backend, _routed_path = self._get_backend_and_key(
            classified.virtual_path
        )
        return routed_backend is expected_backend

    @staticmethod
    def _content_sha256(content: bytes) -> str:
        return f"sha256:{hashlib.sha256(content).hexdigest()}"

    @staticmethod
    def _escape_result(file_path: str) -> dict[str, Any]:
        return {
            "status": "io_error",
            "error_code": "path_escape_rejected",
            "error": f"io_error: path escapes its internal authority: {file_path}",
            "next_action": "use_canonical_workspace_or_scratch_path",
        }

    @staticmethod
    def _managed_write_result(file_path: str) -> dict[str, Any]:
        return {
            "status": "permission_required",
            "error_code": "managed_resource_read_only",
            "error": f"permission_required: managed resource is read-only: {file_path}",
        }

    def _internal_host_target(
        self,
        classified: ClassifiedPath,
        *,
        access: str,
    ) -> AuthorizedHostPath | None:
        """Bind a trusted internal path to its owning root without a Grant."""

        if not classified.internally_writable or not classified.virtual_path:
            return None
        canonical = classified.canonical_host_path
        authority_root: Path | None = None
        if classified.authority is PathAuthority.WORKSPACE:
            workspace_route = next(
                (
                    backend
                    for prefix, backend in self.routes.items()
                    if prefix.rstrip("/") == "/workspace"
                ),
                None,
            )
            if workspace_route is None:
                return None
            routed_backend, routed_path = self._get_backend_and_key(
                classified.virtual_path
            )
            backend_root = getattr(routed_backend, "cwd", None)
            if routed_backend is not workspace_route or backend_root is None:
                return None
            authority_root = Path(backend_root).resolve()
            canonical = (authority_root / routed_path.lstrip("/")).resolve(
                strict=False
            )
            if self.workspace_root is not None and authority_root != self.workspace_root:
                return None
        elif classified.authority is PathAuthority.SCRATCH:
            scratch_route = next(
                (
                    backend
                    for prefix, backend in self.routes.items()
                    if prefix.rstrip("/") == "/scratch"
                ),
                None,
            )
            if scratch_route is None:
                return None
            routed_backend, routed_path = self._get_backend_and_key(classified.virtual_path)
            if routed_backend is not scratch_route:
                return None
            backend_root = getattr(routed_backend, "cwd", None)
            if backend_root is not None:
                authority_root = Path(backend_root).resolve()
                canonical = (authority_root / routed_path.lstrip("/")).resolve(strict=False)
        if authority_root is None or canonical is None:
            return None
        try:
            canonical.relative_to(authority_root)
        except ValueError:
            return None
        return HostFileBroker._authorized_path(
            canonical_path=canonical,
            authority_root=authority_root,
            grant_id=f"internal:{classified.authority.value}",
            access=access,
        )

    def _read_internal_bytes(
        self,
        classified: ClassifiedPath,
    ) -> tuple[bytes | None, dict[str, Any] | None]:
        target = self._internal_host_target(classified, access="read")
        if target is not None:
            try:
                content = HostFileBroker._read_bound_bytes(target)
            except OSError as exc:
                return None, {
                    "status": "io_error",
                    "error_code": "internal_read_failed",
                    "error": f"io_error: {exc}",
                }
            if content is None:
                return None, {
                    "status": "conflict",
                    "error_code": "source_missing",
                    "error": f"conflict: source no longer exists: {classified.original_path}",
                }
            return content, None

        # Non-filesystem internal backends still support the common UTF-8
        # protocol. Keep this fallback behind the same authority decision.
        if not self._has_internal_virtual_route(classified):
            return None, {
                "status": "io_error",
                "error_code": "internal_read_route_unavailable",
                "error": "io_error: internal read route is unavailable",
            }
        from tools.filesystem.inspect import read_all

        content, error = read_all(self, classified.virtual_path or classified.original_path)
        if error is not None or content is None:
            return None, {
                "status": "io_error",
                "error_code": "internal_read_failed",
                "error": f"io_error: {error or 'unable to read internal file'}",
            }
        return content.encode("utf-8"), None

    def _replace_internal_file(
        self,
        classified: ClassifiedPath,
        content: bytes,
        *,
        expected_sha256: str,
    ) -> dict[str, Any]:
        target = self._internal_host_target(classified, access="write")
        if target is None:
            return {
                "status": "io_error",
                "error_code": "internal_backend_replace_unavailable",
                "error": "io_error: internal backend does not expose a bound writable root",
            }
        try:
            before = HostFileBroker._read_bound_bytes(target)
        except OSError as exc:
            return {
                "status": "io_error",
                "error_code": "internal_read_failed",
                "error": f"io_error: {exc}",
            }
        if before is None:
            return {
                "status": "conflict",
                "error_code": "target_missing",
                "error": f"conflict: target no longer exists: {classified.original_path}",
                "current_sha256": "missing",
                "expected_sha256": expected_sha256,
                "next_action": "use_create_mode",
            }
        current_sha256 = self._content_sha256(before)
        if current_sha256 != expected_sha256:
            return {
                "status": "conflict",
                "error_code": "source_version_changed",
                "error": (
                    "conflict: target changed; "
                    f"expected {expected_sha256}, current {current_sha256}"
                ),
                "current_sha256": current_sha256,
                "expected_sha256": expected_sha256,
                "next_action": "reinspect_and_rebase",
            }
        try:
            HostFileBroker._atomic_replace(target, content, expected_before=before)
        except FileExistsError as exc:
            return {
                "status": "conflict",
                "error_code": "source_version_changed",
                "error": str(exc),
                "next_action": "reinspect_and_rebase",
            }
        except OSError as exc:
            return {
                "status": "io_error",
                "error_code": "atomic_replace_failed",
                "error": f"io_error: {exc}",
            }
        return {
            "status": "completed",
            "target_path": classified.virtual_path or classified.original_path,
            "previous_sha256": current_sha256,
            "target_sha256": self._content_sha256(content),
            "authority_kind": classified.authority.value,
        }

    def _create_internal_file(
        self,
        classified: ClassifiedPath,
        content: bytes,
    ) -> dict[str, Any]:
        target = self._internal_host_target(classified, access="write")
        if not self._has_internal_virtual_route(classified):
            return {
                "status": "io_error",
                "error_code": "internal_backend_create_unavailable",
                "error": "io_error: internal writable route is unavailable",
            }
        if target is None:
            try:
                text = content.decode("utf-8")
            except UnicodeDecodeError:
                return {
                    "status": "io_error",
                    "error_code": "internal_create_requires_utf8",
                    "error": "io_error: internal virtual files must be UTF-8 text",
                }
            target_path = classified.virtual_path or classified.original_path
            written = super().write(target_path, text)
            if written.error is not None:
                exists = "already exists" in written.error.lower()
                return {
                    "status": "conflict" if exists else "io_error",
                    "error_code": (
                        "target_already_exists"
                        if exists
                        else "internal_backend_create_failed"
                    ),
                    "error": written.error,
                }
            return {
                "status": "completed",
                "target_path": target_path,
                "target_sha256": self._content_sha256(content),
                "authority_kind": classified.authority.value,
                "atomic": False,
            }
        try:
            HostFileBroker._atomic_replace(
                target,
                content,
                expected_before=None,
                create_parents=True,
            )
        except FileExistsError as exc:
            return {
                "status": "conflict",
                "error_code": "target_already_exists",
                "error": str(exc),
                "next_action": "use_patch_file",
            }
        except OSError as exc:
            return {
                "status": "io_error",
                "error_code": "internal_create_failed",
                "error": f"io_error: {exc}",
            }
        return {
            "status": "completed",
            "target_path": classified.virtual_path or classified.original_path,
            "target_sha256": self._content_sha256(content),
            "authority_kind": classified.authority.value,
        }

    def _validate_external_candidate(
        self,
        target: Any,
        content: bytes,
    ) -> dict[str, Any] | None:
        """Validate code-like host bytes inside the existing execution backend."""

        suffix = target.canonical_path.suffix.lower()
        validator: tuple[str, str, str] | None = None
        if suffix in {".js", ".mjs", ".cjs"}:
            validator = ("javascript_syntax", "node-check/v1", "node --check")
        elif suffix == ".py":
            validator = ("static_check", "python-py-compile/v1", "python3 -m py_compile")
        elif suffix == ".json":
            validator = ("json_structure", "python-json-tool/v1", "python3 -m json.tool")
        elif suffix in {".html", ".htm"}:
            validator = (
                "html_structure",
                "python-html-balance/v1",
                "__internal_html__",
            )
        if validator is None:
            return None

        content_sha256 = f"sha256:{hashlib.sha256(content).hexdigest()}"
        digest = content_sha256.removeprefix("sha256:")
        safe_name = "".join(
            character
            for character in target.canonical_path.name
            if character.isalnum() or character in "._-"
        ) or f"candidate{suffix}"
        validator_kind, validator_version, command_prefix = validator
        if command_prefix == "__internal_html__":
            virtual_path = "internal://html-candidate"
            try:
                text = content.decode("utf-8")
                parser = _CandidateHTMLValidator()
                parser.feed(text)
                parser.close()
                if parser.stack:
                    parser.errors.append(
                        "unclosed tags: " + ", ".join(parser.stack[-10:])
                    )
                output = (
                    "; ".join(parser.errors)
                    if parser.errors
                    else "html structure ok"
                )
                exit_code = 1 if parser.errors else 0
            except (UnicodeDecodeError, ValueError) as exc:
                output = f"invalid HTML candidate: {exc}"
                exit_code = 1
        else:
            execution_backend = getattr(self, "execution_backend", None)
            scratch_root = str(
                getattr(self, "execution_scratch_host_path", "") or ""
            )
            execution_error: Exception | None = None
            if execution_backend is None or not scratch_root:
                execution_error = RuntimeError(
                    "registered validation infrastructure is unavailable"
                )
                virtual_path = "unavailable://validation-candidate"
                output = str(execution_error)
                exit_code = 1
            else:
                relative = Path("validation") / digest / safe_name
                host_path = Path(scratch_root) / relative
                host_path.parent.mkdir(parents=True, exist_ok=True)
                host_path.write_bytes(content)
                virtual_path = f"/scratch/{relative.as_posix()}"
                command = f"{command_prefix} {shlex.quote(virtual_path)}"
                try:
                    result = execution_backend.execute(command, timeout=60)
                except Exception as exc:  # execution boundary, not artifact bytes
                    result = None
                    execution_error = exc
                finally:
                    shutil.rmtree(host_path.parent, ignore_errors=True)
                    try:
                        host_path.parent.parent.rmdir()
                    except OSError:
                        pass
                output = (
                    f"validation infrastructure error: {execution_error}"
                    if execution_error is not None
                    else str(getattr(result, "output", "") or "")
                )
                raw_exit_code = getattr(result, "exit_code", None)
                if not isinstance(raw_exit_code, int):
                    execution_error = RuntimeError(
                        "validation backend returned no exit code"
                    )
                    exit_code = 1
                else:
                    exit_code = raw_exit_code
        failure_class: str | None = None
        content_observed = True
        if exit_code != 0:
            lowered_output = output.lower()
            if (
                command_prefix != "__internal_html__"
                and (
                    execution_error is not None
                    or exit_code in {124, 126, 127, 137}
                    or "timed out" in lowered_output
                    or "infrastructure error" in lowered_output
                    or "command not found" in lowered_output
                    or "out of memory" in lowered_output
                    or "killed" in lowered_output
                )
            ):
                failure_class = "infrastructure_failure"
                content_observed = False
            elif (
                "cannot find module" in lowered_output
                or "module_not_found" in lowered_output
                or "no such file" in lowered_output
                or "enoent" in lowered_output
            ):
                failure_class = "invocation_failure"
                content_observed = False
            else:
                failure_class = "artifact_failure"
        receipt_seed = json.dumps(
            {
                "run_id": self.run_id,
                "path": str(target.canonical_path),
                "content_sha256": content_sha256,
                "validator": validator_version,
            },
            sort_keys=True,
        )
        receipt_id = "validation-" + hashlib.sha256(
            receipt_seed.encode("utf-8")
        ).hexdigest()[:20]
        artifact_id = "artifact-" + hashlib.sha256(
            f"external\0{target.canonical_path}".encode()
        ).hexdigest()[:20]
        return {
            "kind": "validation_receipt",
            "validation_receipt_id": receipt_id,
            "run_id": self.run_id,
            "validator_kind": validator_kind,
            "validator_version": validator_version,
            "artifact_refs": [
                {
                    "artifact_id": artifact_id,
                    "path": str(target.canonical_path),
                    "content_sha256": content_sha256,
                }
            ],
            "command_evidence_ref": "sha256:"
            + hashlib.sha256(output.encode("utf-8")).hexdigest(),
            "exit_code": exit_code,
            "checks_passed": 1 if exit_code == 0 else 0,
            "checks_failed": 0 if exit_code == 0 else 1,
            "status": "passed" if exit_code == 0 else "failed",
            "failure_class": failure_class,
            "content_observed": content_observed,
            "blocking": True,
            "commit_authority": True,
            "obligation_key": f"{validator_kind}:{validator_version}",
            "summary": output[:2000],
            "materialized_path": virtual_path,
            "temporary_materialization": True,
        }

    def rewind_external_file_changes(self) -> dict[str, Any]:
        if self.host_file_broker is None:
            return {
                "status": "permission_required",
                "error": "permission_required: no active HostFileBroker Run",
                "rewound_receipt_ids": [],
            }
        return self.host_file_broker.rewind_run()

    def apply_external_file_transaction(
        self,
        changes: list[dict[str, Any]],
    ) -> dict[str, Any]:
        authorities = [
            self._classify_path(str(change.get("file_path") or ""))
            for change in changes
        ]
        if any(item.authority is PathAuthority.ESCAPE for item in authorities):
            escaped = next(
                item for item in authorities if item.authority is PathAuthority.ESCAPE
            )
            return self._escape_result(escaped.original_path)
        if any(item.authority is PathAuthority.MANAGED for item in authorities):
            managed = next(
                item for item in authorities if item.authority is PathAuthority.MANAGED
            )
            return self._managed_write_result(managed.original_path)
        readonly_change = next(
            (
                change
                for change in changes
                if self._managed_readonly(str(change.get("file_path") or ""))
            ),
            None,
        )
        if readonly_change is not None:
            return self._managed_write_result(
                str(readonly_change.get("file_path") or "")
            )
        internal_authorities = {
            item.authority for item in authorities if item.internally_writable
        }
        if len(internal_authorities) > 1:
            return {
                "status": "io_error",
                "error_code": "mixed_authority_transaction_unsupported",
                "error": (
                    "io_error: one transaction cannot mix workspace and scratch "
                    "authority roots"
                ),
                "next_action": "split_transaction_by_authority",
            }
        if authorities and all(item.internally_writable for item in authorities):
            prepared: list[tuple[ClassifiedPath, bytes, bytes, str]] = []
            for change, classified in zip(changes, authorities, strict=True):
                target = self._internal_host_target(classified, access="write")
                if target is None:
                    return {
                        "status": "io_error",
                        "error_code": "internal_transaction_unavailable",
                        "error": "io_error: internal transaction target is unavailable",
                    }
                try:
                    before = HostFileBroker._read_bound_bytes(target)
                except OSError as exc:
                    return {"status": "io_error", "error": f"io_error: {exc}"}
                if before is None:
                    return {
                        "status": "conflict",
                        "error_code": "target_missing",
                        "error": f"conflict: target missing: {classified.original_path}",
                    }
                expected = str(change.get("expected_sha256") or "")
                current = self._content_sha256(before)
                if current != expected:
                    return {
                        "status": "conflict",
                        "error_code": "source_version_changed",
                        "error": (
                            f"conflict: {classified.original_path} expected "
                            f"{expected}, current {current}"
                        ),
                    }
                prepared.append(
                    (
                        classified,
                        before,
                        str(change.get("content") or "").encode("utf-8"),
                        current,
                    )
                )
            committed: list[tuple[AuthorizedHostPath, bytes, bytes]] = []
            try:
                for classified, before, after, _current in prepared:
                    target = self._internal_host_target(classified, access="write")
                    assert target is not None
                    HostFileBroker._atomic_replace(target, after, expected_before=before)
                    committed.append((target, before, after))
            except OSError as exc:
                rollback_errors: list[str] = []
                for target, before, after in reversed(committed):
                    try:
                        HostFileBroker._atomic_replace(
                            target,
                            before,
                            expected_before=after,
                        )
                    except OSError as rollback_exc:
                        rollback_errors.append(str(rollback_exc))
                return {
                    "status": "conflict" if isinstance(exc, FileExistsError) else "io_error",
                    "error_code": "internal_transaction_commit_failed",
                    "error": str(exc),
                    "rollback_errors": rollback_errors,
                }
            return {
                "status": "completed",
                "changed_files": [
                    classified.virtual_path or classified.original_path
                    for classified, _before, _after, _current in prepared
                ],
                "target_sha256": [
                    self._content_sha256(after)
                    for _classified, _before, after, _current in prepared
                ],
                "authority_kind": "internal",
            }
        if any(item.internally_writable for item in authorities):
            return {
                "status": "io_error",
                "error_code": "mixed_authority_transaction_unsupported",
                "error": (
                    "io_error: one transaction cannot mix internal workspace/scratch "
                    "files with external host files"
                ),
                "next_action": "split_transaction_by_authority",
            }
        if self.host_file_broker is None:
            return {
                "status": "permission_required",
                "error": "permission_required: no active HostFileBroker Run",
            }
        return self.host_file_broker.apply_transaction(changes)

    def replace_external_file(
        self,
        file_path: str,
        content: bytes,
        *,
        expected_sha256: str,
        operation: str = "replace",
    ) -> dict[str, Any]:
        classified = self._classify_path(file_path)
        if classified.authority is PathAuthority.ESCAPE:
            return self._escape_result(file_path)
        if self._managed_readonly(file_path):
            return self._managed_write_result(file_path)
        if classified.internally_writable:
            return self._replace_internal_file(
                classified,
                content,
                expected_sha256=expected_sha256,
            )
        if classified.authority is PathAuthority.MANAGED:
            return self._managed_write_result(file_path)
        if self.host_file_broker is None:
            return {
                "status": "permission_required",
                "error_code": "host_file_broker_unavailable",
                "error": "permission_required: no active HostFileBroker Run",
            }
        return self.host_file_broker.replace(
            file_path,
            content,
            expected_sha256=expected_sha256,
            operation=operation,
        )

    def create_external_file(
        self,
        file_path: str,
        content: bytes,
        *,
        operation: str = "create",
    ) -> dict[str, Any]:
        classified = self._classify_path(file_path)
        if classified.authority is PathAuthority.ESCAPE:
            return self._escape_result(file_path)
        if self._managed_readonly(file_path):
            return self._managed_write_result(file_path)
        if classified.internally_writable:
            try:
                content.decode("utf-8")
            except UnicodeDecodeError:
                return {
                    "status": "io_error",
                    "error_code": "internal_create_requires_utf8",
                    "error": "io_error: internal virtual files must be UTF-8 text",
                }
            return self._create_internal_file(classified, content)
        if classified.authority is PathAuthority.MANAGED:
            return self._managed_write_result(file_path)
        if self.host_file_broker is None:
            return {
                "status": "permission_required",
                "error_code": "host_file_broker_unavailable",
                "error": "permission_required: no active HostFileBroker Run",
            }
        return self.host_file_broker.create(
            file_path,
            content,
            operation=operation,
        )

    def copy_external_file(
        self,
        source_path: str,
        target_path: str,
        *,
        expected_source_sha256: str | None = None,
    ) -> dict[str, Any]:
        source_authority = self._classify_path(source_path)
        target_authority = self._classify_path(target_path)
        if source_authority.authority is PathAuthority.ESCAPE:
            return self._escape_result(source_path)
        if target_authority.authority is PathAuthority.ESCAPE:
            return self._escape_result(target_path)
        if target_authority.authority is PathAuthority.MANAGED:
            return self._managed_write_result(target_path)
        if self._managed_readonly(target_path):
            return {
                "status": "permission_required",
                "error_code": "managed_resource_read_only",
                "error": f"permission_required: managed resource is read-only: {target_path}",
            }
        source_result: dict[str, Any]
        if source_authority.authority in {
            PathAuthority.WORKSPACE,
            PathAuthority.SCRATCH,
            PathAuthority.MANAGED,
        }:
            if source_authority.authority is PathAuthority.MANAGED:
                from tools.filesystem.inspect import read_all

                text, error = read_all(self, source_authority.virtual_path or source_path)
                if error is not None or text is None:
                    return {
                        "status": "io_error",
                        "error_code": "internal_read_failed",
                        "error": f"io_error: {error or 'unable to read source'}",
                    }
                content = text.encode("utf-8")
            else:
                content, error_result = self._read_internal_bytes(source_authority)
                if content is None:
                    return error_result or {
                        "status": "io_error",
                        "error_code": "internal_read_failed",
                    }
            source_sha256 = self._content_sha256(content)
            if expected_source_sha256 and expected_source_sha256 != source_sha256:
                return {
                    "status": "conflict",
                    "error_code": "source_version_changed",
                    "error": (
                        f"conflict: source expected {expected_source_sha256}, "
                        f"current {source_sha256}"
                    ),
                    "source_sha256": source_sha256,
                }
            source_result = {
                "source_path": source_authority.virtual_path or source_path,
                "source_sha256": source_sha256,
            }
        else:
            if self.host_file_broker is None:
                return {
                    "status": "permission_required",
                    "error_code": "host_file_broker_unavailable",
                    "error": "permission_required: no active HostFileBroker Run",
                }
            content, source_result = self.host_file_broker.load_authorized_file(
                source_path, expected_sha256=expected_source_sha256
            )
            if content is None:
                return source_result

        if target_authority.internally_writable:
            try:
                content.decode("utf-8")
            except UnicodeDecodeError:
                return {
                    **source_result,
                    "status": "io_error",
                    "error_code": "workspace_copy_requires_utf8",
                    "error": "io_error: virtual-workspace files must be UTF-8 text",
                    "next_action": "use_a_binary_artifact_channel",
                }
            created = self._create_internal_file(target_authority, content)
            return {
                **source_result,
                **created,
                "authority_kind": (
                    f"virtual_{target_authority.authority.value}"
                    if created.get("status") == "completed"
                    else created.get("authority_kind")
                ),
            }
        if self.host_file_broker is None:
            return {
                "status": "permission_required",
                "error_code": "host_file_broker_unavailable",
                "error": "permission_required: no active HostFileBroker Run",
            }
        if source_authority.authority is PathAuthority.EXTERNAL:
            return self.host_file_broker.copy(
                source_path,
                target_path,
                expected_source_sha256=expected_source_sha256,
            )
        return self.host_file_broker.create(
            target_path,
            content,
            operation="copy",
        )

    def delete_external_file(
        self,
        file_path: str,
        *,
        expected_sha256: str,
    ) -> dict[str, Any]:
        classified = self._classify_path(file_path)
        if classified.authority is PathAuthority.ESCAPE:
            return self._escape_result(file_path)
        if self._managed_readonly(file_path):
            return self._managed_write_result(file_path)
        if classified.internally_writable:
            target = self._internal_host_target(classified, access="delete")
            if target is None:
                return {
                    "status": "io_error",
                    "error_code": "internal_delete_unavailable",
                    "error": "io_error: internal backend does not expose a bound writable root",
                }
            try:
                before = HostFileBroker._read_bound_bytes(target)
            except OSError as exc:
                return {"status": "io_error", "error": f"io_error: {exc}"}
            if before is None:
                return {
                    "status": "conflict",
                    "error_code": "target_missing",
                    "error": f"conflict: target no longer exists: {file_path}",
                }
            current_sha256 = self._content_sha256(before)
            if current_sha256 != expected_sha256:
                return {
                    "status": "conflict",
                    "error_code": "source_version_changed",
                    "error": (
                        f"conflict: target expected {expected_sha256}, "
                        f"current {current_sha256}"
                    ),
                }
            try:
                HostFileBroker._unlink_bound(target, expected_before=before)
            except FileExistsError as exc:
                return {
                    "status": "conflict",
                    "error_code": "source_version_changed",
                    "error": str(exc),
                }
            except OSError as exc:
                return {"status": "io_error", "error": f"io_error: {exc}"}
            return {
                "status": "completed",
                "deleted_path": classified.virtual_path or file_path,
                "authority_kind": classified.authority.value,
            }
        if classified.authority is PathAuthority.MANAGED:
            return self._managed_write_result(file_path)
        if self.host_file_broker is None:
            return {
                "status": "permission_required",
                "error": "permission_required: no active HostFileBroker Run",
            }
        return self.host_file_broker.delete(
            file_path,
            expected_sha256=expected_sha256,
        )

    def execute_external_directory_command(
        self,
        directory_path: str,
        command: str,
        *,
        timeout: int,
        mode: str = "read_only",
        lease_id: str | None = None,
    ) -> dict[str, Any]:
        """Run one command against a read-only root or isolated writable draft."""

        execution_backend = getattr(self, "execution_backend", None)
        effective_backend_mode = str(getattr(execution_backend, "mode", "") or "")
        spawn_read_only = mode == "read_only" and effective_backend_mode == "spawn"
        if self.host_file_broker is None and not spawn_read_only:
            return {
                "status": "permission_required",
                "error": "permission_required: no active HostFileBroker Run",
            }
        try:
            directory = Path(directory_path).expanduser().resolve(strict=True)
        except OSError as exc:
            return {"status": "io_error", "error": f"io_error: {exc}"}
        access = "write" if mode == "writable_draft" else "read"
        if mode not in {"read_only", "writable_draft"}:
            return {
                "status": "io_error",
                "error_code": "unsupported_external_directory_mode",
                "error": f"io_error: unsupported mode {mode}",
                "next_action": "choose_read_only_or_writable_draft",
            }
        if (
            mode == "writable_draft"
            and not self.external_directory_writable_enabled
        ):
            return {
                "status": "permission_required",
                "error_code": "external_directory_writable_disabled",
                "error": (
                    "permission_required: writable external-directory drafts "
                    "are disabled by the deployment feature flag"
                ),
                "next_action": "use_copy_replace_or_enable_feature_flag",
            }
        if not directory.is_dir() or (
            not spawn_read_only
            and (
                self.host_file_broker is None
                or not self.host_file_broker.authorize(directory, access=access)
            )
        ):
            return {
                "status": "permission_required",
                "error": (
                    f"permission_required: exact-directory {access} permission is required; "
                    "file permission never grants a shell mount"
                ),
            }
        execute = getattr(execution_backend, "execute_external_directory", None)
        if not callable(execute):
            return {
                "status": "io_error",
                "error": "io_error: external directory commands require a kernel or Docker execution backend",
            }
        execution_directory = directory
        lease: dict[str, Any] | None = None
        if mode == "writable_draft":
            lease = session_manager.get_external_directory_lease(
                self.session_id,
                str(lease_id or ""),
            )
            if not isinstance(lease, dict):
                return {
                    "status": "conflict",
                    "error_code": "directory_lease_required",
                    "error": "conflict: writable_draft requires a valid staged lease",
                    "next_action": "stage_external_directory",
                }
            if (
                str(lease.get("directory_path") or "") != str(directory)
                or str(lease.get("status") or "") not in {"staged", "prepared"}
                or (
                    str(lease.get("goal_id") or "")
                    and str(lease.get("goal_id") or "")
                    != str(
                        (
                            session_manager.get_run_state(
                                self.session_id,
                                self.run_id,
                            )
                            or {}
                        ).get("goal_id")
                        or ""
                    )
                )
                or (
                    not str(lease.get("goal_id") or "")
                    and str(lease.get("run_id") or "") != self.run_id
                )
            ):
                return {
                    "status": "conflict",
                    "error_code": "directory_lease_binding_mismatch",
                    "error": "conflict: directory lease is not bound to this Run/Goal/path",
                    "next_action": "stage_external_directory",
                }
            scratch_root_raw = str(
                getattr(self, "execution_scratch_host_path", "") or ""
            )
            staged_virtual = str(lease.get("staged_dir") or "")
            if (
                not scratch_root_raw
                or not staged_virtual.startswith("/scratch/")
            ):
                return {
                    "status": "io_error",
                    "error_code": "directory_draft_unavailable",
                    "error": "io_error: staged directory has no execution mapping",
                    "next_action": "stage_external_directory",
                }
            scratch_root = Path(scratch_root_raw).resolve(strict=True)
            execution_directory = (
                scratch_root / staged_virtual.removeprefix("/scratch/")
            ).resolve(strict=True)
            try:
                execution_directory.relative_to(scratch_root)
            except ValueError:
                return {
                    "status": "io_error",
                    "error_code": "directory_draft_escape",
                    "error": "io_error: staged directory escaped scratch root",
                    "next_action": "report_infrastructure_error",
                }
            if not execution_directory.is_dir():
                return {
                    "status": "io_error",
                    "error_code": "directory_draft_unavailable",
                    "error": "io_error: staged directory is missing",
                    "next_action": "stage_external_directory",
                }
        execute_kwargs: dict[str, Any] = {"timeout": timeout}
        if mode == "writable_draft":
            execute_kwargs["writable"] = True
        response = execute(
            str(execution_directory),
            command,
            **execute_kwargs,
        )
        exit_code = getattr(response, "exit_code", None)
        draft_plan: dict[str, list[str]] | None = None
        if mode == "writable_draft" and exit_code == 0 and lease is not None:
            from graph.middlewares.external_directory import (
                _scan_source_directory,
            )

            staged_manifest, _contents, _skipped, scan_error = (
                _scan_source_directory(
                    execution_directory,
                    include_content=False,
                )
            )
            if scan_error is not None:
                return {
                    "status": "io_error",
                    "error_code": "directory_draft_scan_failed",
                    "error": f"io_error: {scan_error}",
                    "next_action": "discard_and_restage_directory",
                }
            source_manifest = dict(lease.get("source_manifest") or {})
            source_paths = set(source_manifest)
            staged_paths = set(staged_manifest)
            draft_plan = {
                "added": sorted(staged_paths - source_paths),
                "modified": sorted(
                    path
                    for path in source_paths & staged_paths
                    if str(source_manifest[path].get("sha256") or "")
                    != str(staged_manifest[path].get("sha256") or "")
                ),
                "deleted": sorted(source_paths - staged_paths),
            }
            lease.update(
                {
                    "status": "staged",
                    "draft_dirty": any(draft_plan.values()),
                    "draft_plan_preview": draft_plan,
                }
            )
            lease.pop("commit_plan", None)
            session_manager.upsert_external_directory_lease(
                self.session_id,
                lease,
            )
        result = {
            "status": "completed" if exit_code == 0 else "io_error",
            "directory_path": str(directory),
            "read_only": mode == "read_only",
            "ephemeral": True,
            "exit_code": exit_code,
            "output": str(getattr(response, "output", "") or ""),
            "truncated": bool(getattr(response, "truncated", False)),
        }
        if mode == "writable_draft":
            result.update(
                {
                    "mode": mode,
                    "lease_id": str(lease_id or ""),
                    "draft_plan_preview": draft_plan,
                    "next_action": (
                        "prepare_external_directory_commit"
                        if exit_code == 0
                        else "discard_and_restage_directory"
                    ),
                }
            )
        return result

    def validate_html_report(
        self,
        html_file_path: str,
        *,
        browser_e2e: bool | None,
        timeout: int,
    ) -> dict[str, Any]:
        """Validate HTML proportionally; start Chromium only when contracted."""

        # Accept the same ``/workspace/`` virtual prefix every other fs tool
        # accepts; previously the literal string hit host ``open()`` and every
        # call failed with ENOENT, which agents then retried unchanged.
        original_path = str(html_file_path or "").strip()
        resolved_input = original_path
        if original_path == "/workspace" or original_path.startswith("/workspace/"):
            if self.workspace_root is None:
                return {
                    "status": "invocation_error",
                    "error_code": "html_report_not_found",
                    "failure_class": "invocation_failure",
                    "error": (
                        f"收到虚拟路径 {original_path}，但当前 Run 未绑定 workspace；"
                        "请改用宿主机绝对路径。"
                    ),
                }
            relative = original_path.removeprefix("/workspace/").strip("/")
            resolved_input = str(self.workspace_root / relative) if relative else str(self.workspace_root)
        requested = Path(resolved_input).expanduser()
        if not requested.is_absolute() or requested.suffix.lower() not in {
            ".html",
            ".htm",
        }:
            return {
                "status": "invocation_error",
                "error_code": "invalid_html_report_path",
                "failure_class": "invocation_failure",
                "error": (
                    f"html_file_path 必须是 .html/.htm 文件：收到 {original_path}；"
                    "支持 /workspace/... 虚拟路径或宿主机绝对路径。"
                ),
            }
        try:
            canonical = requested.resolve(strict=True)
        except OSError:
            return {
                "status": "invocation_error",
                "error_code": "html_report_not_found",
                "failure_class": "invocation_failure",
                "error": (
                    f"文件不存在：{original_path}（已解析为 {requested}）。"
                    "请先确认产物已写入该路径，再发起验证。"
                ),
            }
        if original_path.startswith("/workspace") and self.workspace_root is not None:
            try:
                canonical.relative_to(self.workspace_root)
            except ValueError:
                return {
                    "status": "invocation_error",
                    "error_code": "html_report_not_found",
                    "failure_class": "invocation_failure",
                    "error": (
                        f"虚拟路径越出 workspace：{original_path}（解析为 {canonical}）。"
                    ),
                }
        if not canonical.is_file():
            return {
                "status": "invocation_error",
                "error_code": "html_report_not_regular_file",
                "failure_class": "invocation_failure",
                "error": f"not a regular file: {canonical}",
            }

        run_payload = (
            session_manager.get_run_state(self.session_id, self.run_id)
            if self.session_id and self.run_id
            else None
        )
        verification_contract = (
            run_payload.get("verification_contract")
            if isinstance(run_payload, dict)
            and isinstance(run_payload.get("verification_contract"), dict)
            else {}
        )
        contracted_browser_e2e = bool(
            verification_contract.get("browser_e2e_required")
        )
        requested_browser_e2e = (
            contracted_browser_e2e
            if browser_e2e is None
            else bool(browser_e2e)
        )
        if requested_browser_e2e != contracted_browser_e2e:
            return {
                "status": "invocation_error",
                "error_code": "html_validation_mode_contract_mismatch",
                "failure_class": "invocation_failure",
                "error": (
                    "browser_e2e must match the server-authored "
                    f"verification contract ({contracted_browser_e2e})"
                ),
                "html_file_path": str(canonical),
                "browser_e2e_required": contracted_browser_e2e,
            }

        if not contracted_browser_e2e:
            failures: list[str] = []
            try:
                text = canonical.read_text(encoding="utf-8")
                parser = _CandidateHTMLValidator()
                parser.feed(text)
                parser.close()
                failures.extend(parser.errors)
                if parser.stack:
                    failures.append(
                        "unclosed tags: " + ", ".join(parser.stack[-10:])
                    )
                if "html" not in parser.tags or "body" not in parser.tags:
                    failures.append(
                        "full HTML report must contain <html> and <body>"
                    )
                if parser.duplicate_ids:
                    failures.append(
                        "duplicate element ids: "
                        + ", ".join(sorted(parser.duplicate_ids)[:20])
                    )
                missing_resources: list[str] = []
                for raw_ref in parser.local_resource_refs:
                    relative_ref = raw_ref.split("#", 1)[0].split("?", 1)[0]
                    if not relative_ref:
                        continue
                    referenced = (
                        Path(relative_ref)
                        if Path(relative_ref).is_absolute()
                        else canonical.parent / relative_ref
                    )
                    if not referenced.is_file():
                        missing_resources.append(raw_ref)
                if missing_resources:
                    failures.append(
                        "missing local resources: "
                        + ", ".join(sorted(dict.fromkeys(missing_resources))[:20])
                    )
            except (OSError, UnicodeDecodeError, ValueError) as exc:
                failures.append(f"invalid HTML report: {exc}")
                parser = None
            structure_output = {
                "passed": not failures,
                "mode": "structure",
                "html_file_path": str(canonical),
                "failures": failures,
                "element_id_count": len(parser.ids) if parser is not None else 0,
                "local_resource_count": (
                    len(parser.local_resource_refs)
                    if parser is not None
                    else 0
                ),
            }
            return {
                "status": "completed" if not failures else "io_error",
                "html_file_path": str(canonical),
                "validator_kind": "html_structure",
                "validator_version": "puddingclaw-html-structure/v1",
                "browser_e2e_required": False,
                "exit_code": 0 if not failures else 1,
                "output": json.dumps(
                    structure_output,
                    ensure_ascii=False,
                    sort_keys=True,
                ),
                **(
                    {}
                    if not failures
                    else {"failure_class": "artifact_failure"}
                ),
            }

        command_prefix = (
            "node /opt/puddingclaw/bin/validate-html-report-e2e.mjs "
        )
        response_payload: dict[str, Any]
        workspace = self.workspace_root
        in_workspace = False
        relative: Path | None = None
        if workspace is not None:
            try:
                relative = canonical.relative_to(workspace)
                in_workspace = True
            except ValueError:
                pass
        execution_backend = getattr(self, "execution_backend", None)
        run_typed_validator = getattr(execution_backend, "run_html_report_e2e", None)
        if callable(run_typed_validator) and in_workspace:
            response = run_typed_validator(canonical, timeout=timeout)
            exit_code = getattr(response, "exit_code", None)
            response_payload = {
                "status": "completed" if exit_code == 0 else "io_error",
                "workspace_path": str(workspace) if in_workspace else None,
                "directory_path": None if in_workspace else str(canonical.parent),
                "read_only": True,
                "ephemeral": not in_workspace,
                "exit_code": exit_code,
                "output": str(getattr(response, "output", "") or ""),
                "truncated": bool(getattr(response, "truncated", False)),
            }
        elif in_workspace and relative is not None:
            execute = getattr(execution_backend, "execute", None)
            if not callable(execute):
                response_payload = {
                    "status": "infrastructure_error",
                    "error_code": "html_validator_backend_unavailable",
                    "failure_class": "infrastructure_failure",
                    "error": "HTML browser validation requires a sandbox execution backend",
                }
            else:
                response = execute(
                    command_prefix
                    + shlex.quote(f"/workspace/{relative.as_posix()}"),
                    timeout=timeout,
                )
                exit_code = getattr(response, "exit_code", None)
                response_payload = {
                    "status": "completed" if exit_code == 0 else "io_error",
                    "workspace_path": str(workspace),
                    "read_only": True,
                    "ephemeral": False,
                    "exit_code": exit_code,
                    "output": str(getattr(response, "output", "") or ""),
                    "truncated": bool(getattr(response, "truncated", False)),
                }
        else:
            response_payload = self.execute_external_directory_command(
                str(canonical.parent),
                command_prefix + shlex.quote(canonical.name),
                timeout=timeout,
                mode="read_only",
            )

        response_payload["html_file_path"] = str(canonical)
        response_payload["validator_kind"] = "browser_runtime"
        response_payload["validator_version"] = "puddingclaw-html-e2e/v1"
        response_payload["browser_e2e_required"] = True
        if str(response_payload.get("status") or "") != "completed":
            output = str(response_payload.get("output") or "")
            lowered = output.lower()
            if (
                str(response_payload.get("status") or "")
                == "permission_required"
                or "err_file_not_found" in lowered
                or "no such file" in lowered
            ):
                failure_class = "invocation_failure"
            elif (
                int(response_payload.get("exit_code") or 0) == 124
                or "timed out" in lowered
                or not output.lstrip().startswith("{")
            ):
                failure_class = "infrastructure_failure"
            else:
                failure_class = "artifact_failure"
            response_payload["failure_class"] = failure_class
        return response_payload

    def _readonly_virtual_path(self, file_path: str) -> bool:
        normalized = file_path.replace("\\", "/")
        return any(
            normalized == prefix.rstrip("/") or normalized.startswith(prefix)
            for prefix in (*self._READONLY_VIRTUAL_PREFIXES, *self._readonly_workspace_prefixes)
        )

    def _readonly_host_path(self, file_path: str) -> bool:
        requested = Path(file_path).expanduser()
        if not requested.is_absolute():
            return False
        try:
            resolved = requested.resolve()
        except OSError:
            return True
        for root in self.managed_readonly_roots:
            try:
                resolved.relative_to(root)
                return True
            except ValueError:
                continue
        return False

    def _managed_readonly(self, file_path: str) -> bool:
        if self._readonly_virtual_path(file_path) or self._readonly_host_path(file_path):
            return True
        classified = self._classify_path(file_path)
        canonical = classified.canonical_host_path
        if canonical is None:
            return classified.authority is PathAuthority.MANAGED
        for root in self.managed_readonly_roots:
            try:
                canonical.relative_to(root)
                return True
            except ValueError:
                continue
        return False

    def _unrouted_external_path(self, file_path: str | None) -> bool:
        """Identify a host absolute path that no normal Backend may touch.

        WorkspacePathRouter normally resolves this boundary before dispatch,
        but the Backend remains the final authority. An ungranted absolute
        path must never fall through to FilesystemBackend, whose root handling
        is not a permission decision.
        """

        if not file_path or self._managed_readonly(file_path):
            return False
        classified = self._classify_path(file_path)
        if classified.authority in {
            PathAuthority.WORKSPACE,
            PathAuthority.SCRATCH,
            PathAuthority.MANAGED,
        }:
            return False
        if classified.authority is PathAuthority.ESCAPE:
            return True
        return True

    @staticmethod
    def _permission_error(file_path: str) -> str:
        return (
            "permission_required: external host path is not covered by an "
            f"effective file Grant: {file_path}"
        )

    def _routed_virtual_path(self, file_path: str) -> bool:
        """Return whether CompositeBackend, not host grants, owns this path."""

        return self._classify_path(file_path).authority in {
            PathAuthority.WORKSPACE,
            PathAuthority.SCRATCH,
            PathAuthority.MANAGED,
        }

    def _mounted_backend_path(self, file_path: str | None) -> bool:
        """Return whether an explicit CompositeBackend route owns ``file_path``.

        Mounted paths are application capabilities, not external host paths.
        Built-in filesystem tools must dispatch them directly to the owning
        backend instead of consulting host-file Grants or the execution
        sandbox.  Access mode is still enforced separately: managed mounts
        remain read-only while workspace and scratch routes are writable.
        """

        normalized = str(file_path or "").strip().replace("\\", "/")
        if not normalized:
            return False
        return any(
            normalized == prefix.rstrip("/") or normalized.startswith(prefix)
            for prefix in self.routes
        )

    def _approved_external_target(self, file_path: str) -> tuple[FilesystemBackend, str, str] | None:
        if not self.session_id:
            return None
        if self._routed_virtual_path(file_path) or self._managed_readonly(file_path):
            return None
        requested = Path(file_path).expanduser()
        if not requested.is_absolute():
            return None
        resolved = requested.resolve()
        if not session_manager.has_external_file_write_permission(self.session_id, resolved):
            return None
        backend = FilesystemBackend(root_dir=resolved.parent, virtual_mode=True)
        return backend, f"/{resolved.name}", str(resolved)

    def _approved_external_read_target(self, file_path: str) -> tuple[FilesystemBackend, str] | None:
        if (
            not self.session_id
            or self._routed_virtual_path(file_path)
            or self._managed_readonly(file_path)
        ):
            return None
        requested = Path(file_path).expanduser()
        if not requested.is_absolute():
            return None
        resolved = requested.resolve()
        if not session_manager.has_external_file_read_permission(self.session_id, resolved):
            return None
        return FilesystemBackend(root_dir=resolved.parent, virtual_mode=True), f"/{resolved.name}"

    def _spawn_external_read_target(
        self,
        file_path: str | None,
    ) -> tuple[FilesystemBackend, str, Path] | None:
        """Resolve an ordinary host read in Spawn mode without inventing a grant.

        Spawn deliberately has the desktop user's host filesystem authority.
        This helper only opens existing canonical paths and is never used for
        writes, deletes, Kernel execution, or an unknown execution mode.
        """

        if str(getattr(self, "execution_mode", "")) != "spawn" or not file_path:
            return None
        classified = self._classify_path(file_path)
        if classified.authority is not PathAuthority.EXTERNAL:
            return None
        requested = Path(file_path).expanduser()
        if not requested.is_absolute():
            return None
        try:
            resolved = requested.resolve(strict=True)
        except (OSError, ValueError):
            return None
        authority_root = resolved if resolved.is_dir() else resolved.parent
        backend_path = "/" if resolved.is_dir() else f"/{resolved.name}"
        return (
            FilesystemBackend(root_dir=authority_root, virtual_mode=True),
            backend_path,
            authority_root,
        )

    @staticmethod
    def _restore_spawn_host_paths(result: Any, root: Path) -> Any:
        items = getattr(result, "entries", None)
        if items is None:
            items = getattr(result, "matches", None)
        if isinstance(items, list):
            for item in items:
                if isinstance(item, dict) and item.get("path"):
                    item["path"] = str(root / str(item["path"]).lstrip("/"))
        return result

    @staticmethod
    def _restore_external_path(result: Any, resolved: str):
        if result.path is not None:
            result.path = resolved
        return result

    def write(self, file_path: str, content: str):
        if self._managed_readonly(file_path):
            return WriteResult(error=f"Managed resource is read-only: {file_path}")
        if self._mounted_backend_path(file_path):
            return super().write(file_path, content)
        if self.host_file_broker is not None:
            broker_result = self.host_file_broker.write(file_path, content)
            if broker_result is not None:
                return broker_result
            if self._unrouted_external_path(file_path):
                return WriteResult(error=self._permission_error(file_path))
        target = self._approved_external_target(file_path)
        if target is None:
            if self._unrouted_external_path(file_path):
                return WriteResult(error=self._permission_error(file_path))
            return super().write(file_path, content)
        backend, backend_path, resolved = target
        result = backend.write(backend_path, content)
        return self._restore_external_path(result, resolved)

    def read(self, file_path: str, offset: int = 0, limit: int = 2000):
        if self._mounted_backend_path(file_path):
            return super().read(file_path, offset=offset, limit=limit)
        spawn_target = self._spawn_external_read_target(file_path)
        if spawn_target is not None:
            backend, backend_path, _root = spawn_target
            return backend.read(backend_path, offset=offset, limit=limit)
        if self.host_file_broker is not None:
            broker_result = self.host_file_broker.read(
                file_path,
                offset=offset,
                limit=limit,
            )
            if broker_result is not None:
                return broker_result
            if self._unrouted_external_path(file_path):
                return ReadResult(error=self._permission_error(file_path))
        target = self._approved_external_read_target(file_path)
        if target is None:
            if self._unrouted_external_path(file_path):
                return ReadResult(error=self._permission_error(file_path))
            return super().read(file_path, offset=offset, limit=limit)
        backend, backend_path = target
        return backend.read(backend_path, offset=offset, limit=limit)

    async def aread(self, file_path: str, offset: int = 0, limit: int = 2000):
        if self._mounted_backend_path(file_path):
            return await super().aread(file_path, offset=offset, limit=limit)
        spawn_target = self._spawn_external_read_target(file_path)
        if spawn_target is not None:
            backend, backend_path, _root = spawn_target
            return await backend.aread(backend_path, offset=offset, limit=limit)
        if self.host_file_broker is not None:
            broker_result = await asyncio.to_thread(
                self.host_file_broker.read,
                file_path,
                offset=offset,
                limit=limit,
            )
            if broker_result is not None:
                return broker_result
            if self._unrouted_external_path(file_path):
                return ReadResult(error=self._permission_error(file_path))
        target = self._approved_external_read_target(file_path)
        if target is None:
            if self._unrouted_external_path(file_path):
                return ReadResult(error=self._permission_error(file_path))
            return await super().aread(file_path, offset=offset, limit=limit)
        backend, backend_path = target
        return await backend.aread(backend_path, offset=offset, limit=limit)

    async def awrite(self, file_path: str, content: str):
        if self._managed_readonly(file_path):
            return WriteResult(error=f"Managed resource is read-only: {file_path}")
        if self._mounted_backend_path(file_path):
            return await super().awrite(file_path, content)
        if self.host_file_broker is not None:
            broker_result = await asyncio.to_thread(
                self.host_file_broker.write,
                file_path,
                content,
            )
            if broker_result is not None:
                return broker_result
            if self._unrouted_external_path(file_path):
                return WriteResult(error=self._permission_error(file_path))
        target = self._approved_external_target(file_path)
        if target is None:
            if self._unrouted_external_path(file_path):
                return WriteResult(error=self._permission_error(file_path))
            return await super().awrite(file_path, content)
        backend, backend_path, resolved = target
        result = await backend.awrite(backend_path, content)
        return self._restore_external_path(result, resolved)

    def edit(
        self,
        file_path: str,
        old_string: str,
        new_string: str,
        replace_all: bool = False,
    ):
        if self._managed_readonly(file_path):
            return EditResult(error=f"Managed resource is read-only: {file_path}")
        if self._mounted_backend_path(file_path):
            return super().edit(file_path, old_string, new_string, replace_all=replace_all)
        if self.host_file_broker is not None:
            broker_result = self.host_file_broker.edit(
                file_path,
                old_string,
                new_string,
                replace_all=replace_all,
            )
            if broker_result is not None:
                return broker_result
            if self._unrouted_external_path(file_path):
                return EditResult(error=self._permission_error(file_path))
        target = self._approved_external_target(file_path)
        if target is None:
            if self._unrouted_external_path(file_path):
                return EditResult(error=self._permission_error(file_path))
            return super().edit(file_path, old_string, new_string, replace_all=replace_all)
        backend, backend_path, resolved = target
        result = backend.edit(backend_path, old_string, new_string, replace_all=replace_all)
        return self._restore_external_path(result, resolved)

    async def aedit(
        self,
        file_path: str,
        old_string: str,
        new_string: str,
        replace_all: bool = False,
    ):
        if self._managed_readonly(file_path):
            return EditResult(error=f"Managed resource is read-only: {file_path}")
        if self._mounted_backend_path(file_path):
            return await super().aedit(file_path, old_string, new_string, replace_all=replace_all)
        if self.host_file_broker is not None:
            broker_result = await asyncio.to_thread(
                self.host_file_broker.edit,
                file_path,
                old_string,
                new_string,
                replace_all=replace_all,
            )
            if broker_result is not None:
                return broker_result
            if self._unrouted_external_path(file_path):
                return EditResult(error=self._permission_error(file_path))
        target = self._approved_external_target(file_path)
        if target is None:
            if self._unrouted_external_path(file_path):
                return EditResult(error=self._permission_error(file_path))
            return await super().aedit(file_path, old_string, new_string, replace_all=replace_all)
        backend, backend_path, resolved = target
        result = await backend.aedit(backend_path, old_string, new_string, replace_all=replace_all)
        return self._restore_external_path(result, resolved)

    def ls(self, path: str):
        if self._mounted_backend_path(path):
            return super().ls(path)
        spawn_target = self._spawn_external_read_target(path)
        if spawn_target is not None:
            backend, backend_path, root = spawn_target
            return self._restore_spawn_host_paths(backend.ls(backend_path), root)
        if self.host_file_broker is not None:
            result = self.host_file_broker.ls(path)
            if result is not None:
                return result
        if self._unrouted_external_path(path):
            return LsResult(error=self._permission_error(path))
        return super().ls(path)

    async def als(self, path: str):
        if self._mounted_backend_path(path):
            return await super().als(path)
        spawn_target = self._spawn_external_read_target(path)
        if spawn_target is not None:
            backend, backend_path, root = spawn_target
            result = await backend.als(backend_path)
            return self._restore_spawn_host_paths(result, root)
        if self.host_file_broker is not None:
            result = await asyncio.to_thread(self.host_file_broker.ls, path)
            if result is not None:
                return result
        if self._unrouted_external_path(path):
            return LsResult(error=self._permission_error(path))
        return await super().als(path)

    def glob(self, pattern: str, path: str | None = None):
        if self._mounted_backend_path(path):
            return super().glob(pattern, path=path)
        spawn_target = self._spawn_external_read_target(path)
        if spawn_target is not None:
            backend, backend_path, root = spawn_target
            result = backend.glob(pattern, path=backend_path)
            return self._restore_spawn_host_paths(result, root)
        if self.host_file_broker is not None:
            result = self.host_file_broker.glob(pattern, path=path)
            if result is not None:
                return result
        if self._unrouted_external_path(path):
            return GlobResult(error=self._permission_error(str(path)))
        return super().glob(pattern, path=path)

    async def aglob(self, pattern: str, path: str | None = None):
        if self._mounted_backend_path(path):
            return await super().aglob(pattern, path=path)
        spawn_target = self._spawn_external_read_target(path)
        if spawn_target is not None:
            backend, backend_path, root = spawn_target
            result = await backend.aglob(pattern, path=backend_path)
            return self._restore_spawn_host_paths(result, root)
        if self.host_file_broker is not None:
            result = await asyncio.to_thread(
                self.host_file_broker.glob,
                pattern,
                path,
            )
            if result is not None:
                return result
        if self._unrouted_external_path(path):
            return GlobResult(error=self._permission_error(str(path)))
        return await super().aglob(pattern, path=path)

    def grep(self, pattern: str, path: str | None = None, glob: str | None = None):
        if self._mounted_backend_path(path):
            return super().grep(pattern, path=path, glob=glob)
        spawn_target = self._spawn_external_read_target(path)
        if spawn_target is not None:
            backend, backend_path, root = spawn_target
            result = backend.grep(pattern, path=backend_path, glob=glob)
            return self._restore_spawn_host_paths(result, root)
        if self.host_file_broker is not None:
            result = self.host_file_broker.grep(pattern, path=path, glob=glob)
            if result is not None:
                return result
        if self._unrouted_external_path(path):
            return GrepResult(error=self._permission_error(str(path)))
        return super().grep(pattern, path=path, glob=glob)

    async def agrep(self, pattern: str, path: str | None = None, glob: str | None = None):
        if self._mounted_backend_path(path):
            return await super().agrep(pattern, path=path, glob=glob)
        spawn_target = self._spawn_external_read_target(path)
        if spawn_target is not None:
            backend, backend_path, root = spawn_target
            result = await backend.agrep(pattern, path=backend_path, glob=glob)
            return self._restore_spawn_host_paths(result, root)
        if self.host_file_broker is not None:
            result = await asyncio.to_thread(
                self.host_file_broker.grep,
                pattern,
                path,
                glob,
            )
            if result is not None:
                return result
        if self._unrouted_external_path(path):
            return GrepResult(error=self._permission_error(str(path)))
        return await super().agrep(pattern, path=path, glob=glob)

    def can_access_external_path(
        self,
        path: str,
        *,
        access: str,
        allow_missing_leaf: bool = False,
    ) -> bool:
        return bool(
            self.host_file_broker is not None
            and self.host_file_broker.authorize(
                path,
                access=access,
                allow_missing_leaf=allow_missing_leaf,
            )
        )
