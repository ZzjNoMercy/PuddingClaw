"""DeepAgents filesystem backend with exact-file external write grants."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from deepagents.backends import CompositeBackend, FilesystemBackend
from deepagents.backends.protocol import EditResult, WriteResult

from graph.session_manager import session_manager


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
    ) -> None:
        super().__init__(default=default, routes=routes)
        self.session_id = session_id
        self.managed_readonly_roots = tuple(root.expanduser().resolve() for root in managed_readonly_roots)

    @classmethod
    def _readonly_virtual_path(cls, file_path: str) -> bool:
        normalized = file_path.replace("\\", "/")
        return any(
            normalized == prefix.rstrip("/") or normalized.startswith(prefix)
            for prefix in cls._READONLY_VIRTUAL_PREFIXES
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

    def _approved_external_target(self, file_path: str) -> tuple[FilesystemBackend, str, str] | None:
        if not self.session_id:
            return None
        if self._managed_readonly(file_path):
            return None
        requested = Path(file_path).expanduser()
        if not requested.is_absolute():
            return None
        resolved = requested.resolve()
        if not session_manager.has_external_file_write_permission(self.session_id, resolved):
            return None
        backend = FilesystemBackend(root_dir=resolved.parent, virtual_mode=True)
        return backend, f"/{resolved.name}", str(resolved)

    @staticmethod
    def _restore_external_path(result: Any, resolved: str):
        if result.path is not None:
            result.path = resolved
        return result

    def write(self, file_path: str, content: str):
        if self._managed_readonly(file_path):
            return WriteResult(error=f"Managed resource is read-only: {file_path}")
        target = self._approved_external_target(file_path)
        if target is None:
            return super().write(file_path, content)
        backend, backend_path, resolved = target
        result = backend.write(backend_path, content)
        return self._restore_external_path(result, resolved)

    async def awrite(self, file_path: str, content: str):
        if self._managed_readonly(file_path):
            return WriteResult(error=f"Managed resource is read-only: {file_path}")
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
        target = self._approved_external_target(file_path)
        if target is None:
            return await super().aedit(file_path, old_string, new_string, replace_all=replace_all)
        backend, backend_path, resolved = target
        result = await backend.aedit(backend_path, old_string, new_string, replace_all=replace_all)
        return self._restore_external_path(result, resolved)
