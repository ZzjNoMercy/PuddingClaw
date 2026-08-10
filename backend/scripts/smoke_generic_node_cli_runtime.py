"""Real-Docker smoke for install-then-run of an Adapter-free npm CLI."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

from harness.workspace_backends import DockerWorkspaceBackend, ProjectSandboxManager
from runtime_identity.paths import PuddingClawPaths
from runtime_identity.service import GenericNodeCliInstallPlan, ManagedCliService


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="puddingclaw-generic-cli-") as temporary:
        root = Path(temporary).resolve()
        os.environ["PUDDINGCLAW_HOME"] = str(root / ".puddingclaw")
        workspace = root / "workspace"
        scratch = root / "scratch"
        workspace.mkdir()
        scratch.mkdir()
        manager = ProjectSandboxManager(
            {
                "_managed_user_toolchain": True,
                "_managed_writable_mounts": [
                    {"source": str(scratch), "target": "/scratch"},
                ],
            }
        )
        service = ManagedCliService(manager, paths=PuddingClawPaths.from_environment())
        installs = []
        for distribution in ("prettier@3.6.2", "json5@2.2.3"):
            plan = service.plan_command(f"npm install --global {distribution}", {})
            if not isinstance(plan, GenericNodeCliInstallPlan):
                raise RuntimeError("generic CLI installation was not claimed")
            installed = service.execute(plan)
            if installed.exit_code != 0:
                print(installed.content)
                return installed.exit_code
            installs.append(installed.payload)
        backend = DockerWorkspaceBackend(
            root_dir=workspace,
            manager=manager,
            scratch_path=scratch,
        )
        try:
            prettier = backend.execute("prettier --version")
            json5 = backend.execute("json5 --version")
            print(
                json.dumps(
                    {
                        "installs": installs,
                        "executions": {
                            "prettier": {
                                "output": prettier.output,
                                "exit_code": prettier.exit_code,
                            },
                            "json5": {
                                "output": json5.output,
                                "exit_code": json5.exit_code,
                            },
                        },
                    },
                    ensure_ascii=False,
                    indent=2,
                )
            )
            return prettier.exit_code or json5.exit_code
        finally:
            manager._run(["rm", "-f", backend.container_name], timeout=30)


if __name__ == "__main__":
    raise SystemExit(main())
