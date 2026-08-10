"""Real-Docker smoke test for the managed Lark CLI runtime.

The script uses one temporary workspace and one temporary PuddingClaw Home. It
does not import, mount, or modify the user's real Credential Profiles.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

from harness.workspace_backends import ProjectSandboxManager
from runtime_identity.adapters import ManagedCliRegistry
from runtime_identity.paths import PuddingClawPaths
from runtime_identity.service import ManagedCliService


class _EphemeralBackend:
    def __init__(self, manager: ProjectSandboxManager, workspace: Path) -> None:
        self.manager = manager
        self.workspace = workspace

    def managed_runtime_image_digest(self) -> str:
        return self.manager.managed_runtime_image_digest(self.workspace)

    def resolve_managed_node_cli(self, *, distribution: str, package: str):
        return self.manager.resolve_managed_node_cli(
            self.workspace,
            distribution=distribution,
            package=package,
        )

    def resolve_shared_node_runtime(self, **kwargs):
        return self.manager.resolve_shared_node_runtime(
            self.workspace,
            **kwargs,
        )

    def build_shared_node_runtime(self, **kwargs):
        return self.manager.build_shared_node_runtime(
            self.workspace,
            **kwargs,
        )

    def run_managed_provider_cli(self, **kwargs):
        return self.manager.run_managed_provider_cli(self.workspace, **kwargs)


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="puddingclaw-managed-cli-") as temporary:
        root = Path(temporary)
        workspace = root / "workspace"
        workspace.mkdir()
        os.environ["PUDDINGCLAW_HOME"] = str(root / ".puddingclaw")
        manager = ProjectSandboxManager({})
        available, reason = manager.probe()
        if not available:
            raise RuntimeError(f"Docker is unavailable: {reason}")
        service = ManagedCliService(
            _EphemeralBackend(manager, workspace),
            paths=PuddingClawPaths(root / ".puddingclaw"),
        )
        registry = ManagedCliRegistry()
        install = service.execute(registry.match("npm install -g @larksuite/cli"), {})
        if install.exit_code != 0:
            print(install.content)
            return install.exit_code
        version = service.execute(registry.match("lark-cli --version"), {})
        print(
            json.dumps(
                {
                    "install": install.payload,
                    "version": version.payload,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return version.exit_code


if __name__ == "__main__":
    raise SystemExit(main())
