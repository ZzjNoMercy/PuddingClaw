"""Composition boundary for local integration CLIs.

Ordinary project commands run through the user-selected Spawn or Kernel
backend. Host-native integrations such as the official Lark CLI use one global
binary plus provider-native credential storage, while command classification,
HITL and output redaction remain platform-owned. Generic software runtimes keep
their immutable publication path. Docker is not a local product prerequisite.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any


class LazyManagedCliService:
    """Construct one managed CLI service on first control-plane operation."""

    def __init__(self, factory: Callable[[], object]) -> None:
        self._factory = factory
        self._service: object | None = None
        self._lock = threading.Lock()

    def _resolve(self) -> object:
        service = self._service
        if service is not None:
            return service
        with self._lock:
            if self._service is None:
                self._service = self._factory()
            return self._service

    def __getattr__(self, name: str) -> Any:
        return getattr(self._resolve(), name)


class ManagedIntegrationBackend:
    """Bind managed host-kernel operations to one workspace.

    Lark executes as an exact host argv against its stable user/profile config
    directory. Generic credentialless CLIs may still use declaratively
    published Node bytes. No method guesses a workspace from cwd.
    """

    def __init__(self, host_backend: object, workspace_path: Path) -> None:
        self._host_backend = host_backend
        self.workspace_path = workspace_path.expanduser().resolve(strict=True)
        if not self.workspace_path.is_dir():
            raise ValueError("managed integration workspace must be a directory")
        self.runtime_contract = str(getattr(host_backend, "runtime_contract", ""))
        if not self.runtime_contract:
            raise ValueError("managed integration host runtime has no contract")

    def managed_runtime_image_digest(self) -> str:
        return self._host_backend.managed_runtime_image_digest()

    def resolve_managed_node_cli(self, **kwargs: Any) -> object:
        return self._host_backend.resolve_managed_node_cli(**kwargs)

    def resolve_host_lark_cli(self) -> object:
        return self._host_backend.resolve_host_lark_cli()

    def resolve_host_lark_package(self, **kwargs: Any) -> object:
        return self._host_backend.resolve_host_lark_package(**kwargs)

    def install_host_lark_cli(self, distribution: str, *, expected_version: str) -> object:
        return self._host_backend.install_host_lark_cli(
            distribution,
            expected_version=expected_version,
        )

    def resolve_shared_node_runtime(self, **kwargs: Any) -> object:
        return self._host_backend.resolve_shared_node_runtime(**kwargs)

    def build_shared_node_runtime(self, **kwargs: Any) -> object:
        return self._host_backend.build_shared_node_runtime(**kwargs)

    def run_managed_provider_cli(self, **kwargs: Any) -> object:
        return self._host_backend.run_managed_provider_cli(self.workspace_path, **kwargs)

    def run_managed_browser_auth_cli(self, **kwargs: Any) -> object:
        return self._host_backend.run_managed_browser_auth_cli(self.workspace_path, **kwargs)

    def collect_managed_browser_auth_cli(self, **kwargs: Any) -> object:
        return self._host_backend.collect_managed_browser_auth_cli(**kwargs)

    def finalize_managed_browser_auth_cli(self, **kwargs: Any) -> object:
        return self._host_backend.finalize_managed_browser_auth_cli(**kwargs)

    def list_managed_browser_auth_jobs(self, **kwargs: Any) -> object:
        return self._host_backend.list_managed_browser_auth_jobs(**kwargs)

    def resolve_generic_node_cli(self, **kwargs: Any) -> object:
        return self._host_backend.resolve_generic_node_cli(**kwargs)

    def generic_node_runtime_current(self, runtime_digest: str) -> Path:
        return self._host_backend.generic_node_runtime_current(runtime_digest)

    def install_generic_node_cli(self, **kwargs: Any) -> object:
        return self._host_backend.install_generic_node_cli(**kwargs)


def build_managed_integration_backend(
    workspace_path: Path | None = None,
) -> ManagedIntegrationBackend:
    """Build the Host Toolchain + kernel-runner integration backend."""

    from config import load_config
    from harness.host_skill_runtime import HostSkillRuntimeBackend
    from runtime_identity.paths import PuddingClawPaths

    paths = PuddingClawPaths.from_environment()
    if workspace_path is None:
        workspace_path = paths.root / "data" / "managed-integration-workspace"
        workspace_path.mkdir(parents=True, mode=0o700, exist_ok=True)
    terminal = load_config().get("harness", {}).get("terminal", {})
    timeout = int(terminal.get("default_timeout_seconds") or 120)
    host_backend = HostSkillRuntimeBackend(paths, timeout=max(timeout, 900))
    return ManagedIntegrationBackend(host_backend, workspace_path)


def build_managed_cli_service(workspace_path: Path | None = None) -> object:
    """Build the platform-owned managed CLI service from current config."""

    from runtime_identity.paths import PuddingClawPaths
    from runtime_identity.service import ManagedCliService

    return ManagedCliService(
        build_managed_integration_backend(workspace_path),
        paths=PuddingClawPaths.from_environment(),
    )


def lazy_managed_cli_service(workspace_path: Path | None = None) -> LazyManagedCliService:
    """Return a no-side-effect handle for one Agent execution pipeline."""

    return LazyManagedCliService(lambda: build_managed_cli_service(workspace_path))
