"""DeepAgents filesystem backend with exact-file external write grants."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from deepagents.backends import CompositeBackend, FilesystemBackend
from deepagents.backends.protocol import EditResult, WriteResult

from graph.host_file_broker import HostFileBroker
from graph.session_manager import session_manager


class PermissionedCompositeBackend(CompositeBackend):
    """Delegate approved external writes while keeping normal virtual routing."""

    _ROUTED_VIRTUAL_PREFIXES = (
        "/workspace/",
        "/scratch/",
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
            )
            if session_id and run_id
            else None
        )
        self.managed_readonly_roots = tuple(root.expanduser().resolve() for root in managed_readonly_roots)
        resolved_workspace = workspace_root.expanduser().resolve() if workspace_root is not None else None
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
            return await super().aedit(file_path, old_string, new_string, replace_all=replace_all)
        backend, backend_path, resolved = target
        result = await backend.aedit(backend_path, old_string, new_string, replace_all=replace_all)
        return self._restore_external_path(result, resolved)

    def ls(self, path: str):
        if self.host_file_broker is not None:
            result = self.host_file_broker.ls(path)
            if result is not None:
                return result
        return super().ls(path)

    async def als(self, path: str):
        if self.host_file_broker is not None:
            result = await asyncio.to_thread(self.host_file_broker.ls, path)
            if result is not None:
                return result
        return await super().als(path)

    def glob(self, pattern: str, path: str | None = None):
        if self.host_file_broker is not None:
            result = self.host_file_broker.glob(pattern, path=path)
            if result is not None:
                return result
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
        return await super().aglob(pattern, path=path)

    def grep(self, pattern: str, path: str | None = None, glob: str | None = None):
        if self.host_file_broker is not None:
            result = self.host_file_broker.grep(pattern, path=path, glob=glob)
            if result is not None:
                return result
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
