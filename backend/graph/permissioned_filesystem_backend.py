"""DeepAgents filesystem backend with exact-file external write grants."""

from __future__ import annotations

import asyncio
import hashlib
import json
import shlex
import shutil
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

from graph.host_file_broker import HostFileBroker
from graph.session_manager import session_manager


class PermissionedCompositeBackend(CompositeBackend):
    """Delegate approved external writes while keeping normal virtual routing."""

    _ROUTED_VIRTUAL_PREFIXES = (
        "/workspace/",
        "/scratch/",
        "/large_tool_results/",
    )
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
        if validator is None:
            return None
        execution_backend = getattr(self, "execution_backend", None)
        scratch_root = str(getattr(self, "execution_scratch_host_path", "") or "")
        if execution_backend is None or not scratch_root:
            return None

        content_sha256 = f"sha256:{hashlib.sha256(content).hexdigest()}"
        digest = content_sha256.removeprefix("sha256:")
        safe_name = "".join(
            character
            for character in target.canonical_path.name
            if character.isalnum() or character in "._-"
        ) or f"candidate{suffix}"
        relative = Path("validation") / digest / safe_name
        host_path = Path(scratch_root) / relative
        host_path.parent.mkdir(parents=True, exist_ok=True)
        host_path.write_bytes(content)
        virtual_path = f"/scratch/{relative.as_posix()}"
        validator_kind, validator_version, command_prefix = validator
        command = f"{command_prefix} {shlex.quote(virtual_path)}"
        try:
            result = execution_backend.execute(command, timeout=60)
        finally:
            shutil.rmtree(host_path.parent, ignore_errors=True)
            try:
                host_path.parent.parent.rmdir()
            except OSError:
                pass
        output = str(getattr(result, "output", "") or "")
        raw_exit_code = getattr(result, "exit_code", None)
        exit_code = int(raw_exit_code) if isinstance(raw_exit_code, int) else 1
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
        if self.host_file_broker is None:
            return {
                "status": "permission_required",
                "error": "permission_required: no active HostFileBroker Run",
            }
        return self.host_file_broker.apply_transaction(changes)

    def delete_external_file(
        self,
        file_path: str,
        *,
        expected_sha256: str,
    ) -> dict[str, Any]:
        if self._managed_readonly(file_path):
            return {
                "status": "permission_required",
                "error": f"permission_required: managed resource is read-only: {file_path}",
            }
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
    ) -> dict[str, Any]:
        """Run one separately approved read-only directory command in Docker."""

        if self.host_file_broker is None:
            return {
                "status": "permission_required",
                "error": "permission_required: no active HostFileBroker Run",
            }
        try:
            directory = Path(directory_path).expanduser().resolve(strict=True)
        except OSError as exc:
            return {"status": "io_error", "error": f"io_error: {exc}"}
        if not directory.is_dir() or not self.host_file_broker.authorize(
            directory,
            access="read",
        ):
            return {
                "status": "permission_required",
                "error": (
                    "permission_required: exact-directory read permission is required; "
                    "file permission never grants a shell mount"
                ),
            }
        execution_backend = getattr(self, "execution_backend", None)
        execute = getattr(execution_backend, "execute_external_directory", None)
        if not callable(execute):
            return {
                "status": "io_error",
                "error": "io_error: external directory commands require the Docker backend",
            }
        response = execute(str(directory), command, timeout=timeout)
        exit_code = getattr(response, "exit_code", None)
        return {
            "status": "completed" if exit_code == 0 else "io_error",
            "directory_path": str(directory),
            "read_only": True,
            "ephemeral": True,
            "exit_code": exit_code,
            "output": str(getattr(response, "output", "") or ""),
            "truncated": bool(getattr(response, "truncated", False)),
        }

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
        return self._readonly_virtual_path(file_path) or self._readonly_host_path(file_path)

    def _unrouted_external_path(self, file_path: str | None) -> bool:
        """Identify a host absolute path that no normal Backend may touch.

        WorkspacePathRouter normally resolves this boundary before dispatch,
        but the Backend remains the final authority. An ungranted absolute
        path must never fall through to FilesystemBackend, whose root handling
        is not a permission decision.
        """

        if not file_path or self._routed_virtual_path(file_path) or self._managed_readonly(file_path):
            return False
        requested = Path(file_path).expanduser()
        if not requested.is_absolute():
            return False
        # DeepAgents' default virtual backend also uses root-absolute paths
        # (for example ``/dashboard.html``). Preserve that namespace when the
        # corresponding project target or its parent exists. Real host paths
        # outside the workspace have no such project projection and remain
        # fail-closed below.
        if self.workspace_root is not None:
            virtual_candidate = self.workspace_root / file_path.lstrip("/")
            if virtual_candidate.exists() or virtual_candidate.parent.exists():
                return False
        try:
            resolved = requested.resolve(strict=False)
        except OSError:
            return True
        if self.workspace_root is not None:
            try:
                resolved.relative_to(self.workspace_root)
                return False
            except ValueError:
                pass
        return True

    @staticmethod
    def _permission_error(file_path: str) -> str:
        return (
            "permission_required: external host path is not covered by an "
            f"effective file Grant: {file_path}"
        )

    @classmethod
    def _routed_virtual_path(cls, file_path: str) -> bool:
        """Return whether CompositeBackend, not host grants, owns this path."""

        normalized = file_path.replace("\\", "/")
        return any(
            normalized == prefix.rstrip("/") or normalized.startswith(prefix)
            for prefix in cls._ROUTED_VIRTUAL_PREFIXES
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

    @staticmethod
    def _restore_external_path(result: Any, resolved: str):
        if result.path is not None:
            result.path = resolved
        return result

    def write(self, file_path: str, content: str):
        if self._managed_readonly(file_path):
            return WriteResult(error=f"Managed resource is read-only: {file_path}")
        if self.host_file_broker is not None:
            broker_result = self.host_file_broker.write(file_path, content)
            if broker_result is not None:
                return broker_result
        target = self._approved_external_target(file_path)
        if target is None:
            if self._unrouted_external_path(file_path):
                return WriteResult(error=self._permission_error(file_path))
            return super().write(file_path, content)
        backend, backend_path, resolved = target
        result = backend.write(backend_path, content)
        return self._restore_external_path(result, resolved)

    def read(self, file_path: str, offset: int = 0, limit: int = 2000):
        if self.host_file_broker is not None:
            broker_target = self.host_file_broker.read_target(file_path)
            if broker_target is not None:
                backend, backend_path = broker_target
                return backend.read(backend_path, offset=offset, limit=limit)
        target = self._approved_external_read_target(file_path)
        if target is None:
            if self._unrouted_external_path(file_path):
                return ReadResult(error=self._permission_error(file_path))
            return super().read(file_path, offset=offset, limit=limit)
        backend, backend_path = target
        return backend.read(backend_path, offset=offset, limit=limit)

    async def aread(self, file_path: str, offset: int = 0, limit: int = 2000):
        if self.host_file_broker is not None:
            broker_target = self.host_file_broker.read_target(file_path)
            if broker_target is not None:
                backend, backend_path = broker_target
                return await backend.aread(backend_path, offset=offset, limit=limit)
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
        if self.host_file_broker is not None:
            broker_result = await asyncio.to_thread(
                self.host_file_broker.write,
                file_path,
                content,
            )
            if broker_result is not None:
                return broker_result
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
        if self.host_file_broker is not None:
            broker_result = self.host_file_broker.edit(
                file_path,
                old_string,
                new_string,
                replace_all=replace_all,
            )
            if broker_result is not None:
                return broker_result
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
        target = self._approved_external_target(file_path)
        if target is None:
            if self._unrouted_external_path(file_path):
                return EditResult(error=self._permission_error(file_path))
            return await super().aedit(file_path, old_string, new_string, replace_all=replace_all)
        backend, backend_path, resolved = target
        result = await backend.aedit(backend_path, old_string, new_string, replace_all=replace_all)
        return self._restore_external_path(result, resolved)

    def ls(self, path: str):
        if self.host_file_broker is not None:
            result = self.host_file_broker.ls(path)
            if result is not None:
                return result
        if self._unrouted_external_path(path):
            return LsResult(error=self._permission_error(path))
        return super().ls(path)

    async def als(self, path: str):
        if self.host_file_broker is not None:
            result = await asyncio.to_thread(self.host_file_broker.ls, path)
            if result is not None:
                return result
        if self._unrouted_external_path(path):
            return LsResult(error=self._permission_error(path))
        return await super().als(path)

    def glob(self, pattern: str, path: str | None = None):
        if self.host_file_broker is not None:
            result = self.host_file_broker.glob(pattern, path=path)
            if result is not None:
                return result
        if self._unrouted_external_path(path):
            return GlobResult(error=self._permission_error(str(path)))
        return super().glob(pattern, path=path)

    async def aglob(self, pattern: str, path: str | None = None):
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
        if self.host_file_broker is not None:
            result = self.host_file_broker.grep(pattern, path=path, glob=glob)
            if result is not None:
                return result
        if self._unrouted_external_path(path):
            return GrepResult(error=self._permission_error(str(path)))
        return super().grep(pattern, path=path, glob=glob)

    async def agrep(self, pattern: str, path: str | None = None, glob: str | None = None):
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
