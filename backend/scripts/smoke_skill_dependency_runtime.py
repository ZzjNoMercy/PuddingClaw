"""Real-Docker smoke for one isolated Python Skill dependency runtime."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

from harness.workspace_backends import ProjectSandboxManager
from runtime_identity.paths import PuddingClawPaths
from runtime_identity.software_runtime import SoftwareRuntimeManager, skill_content_version


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="puddingclaw-skill-runtime-") as temporary:
        root = Path(temporary).resolve()
        os.environ["PUDDINGCLAW_HOME"] = str(root / ".puddingclaw")
        workspace = root / "workspace"
        workspace.mkdir()
        skill = root / "skills" / "fixture-python"
        scripts = skill / "scripts"
        scripts.mkdir(parents=True)
        (skill / "SKILL.md").write_text("---\nname: fixture-python\n---\n", encoding="utf-8")
        (scripts / "version.py").write_text(
            "import packaging\nprint(packaging.__version__)\n",
            encoding="utf-8",
        )

        manager = ProjectSandboxManager({})
        runtimes = SoftwareRuntimeManager(
            PuddingClawPaths.from_environment(),
            manager.runtime_contract,
        )
        version = skill_content_version(skill)
        installed = runtimes.install_python_skill(
            manager,
            skill_id="fixture-python",
            skill_version=version,
            requirements=["packaging==25.0"],
        )
        if installed.exit_code != 0:
            print(installed.output)
            return installed.exit_code
        runtime = runtimes.python_skill_current(
            "fixture-python",
            version,
            manager.managed_runtime_image_digest(workspace),
        )
        if runtime is None:
            raise RuntimeError("published Python Skill runtime is unavailable")
        executed = manager.run_python_skill(
            workspace,
            skill_id="fixture-python",
            skill_root=skill,
            runtime_path=runtime,
            script_relative="scripts/version.py",
            interpreter_args=[],
            script_args=[],
            timeout=120,
            max_output_bytes=10_000,
            network_enabled=False,
            expected_runtime_image_digest=installed.runtime_image_digest or "",
        )
        print(
            json.dumps(
                {
                    "install_revision": installed.revision,
                    "runtime_image_digest": installed.runtime_image_digest,
                    "output": executed.output,
                    "exit_code": executed.exit_code,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return executed.exit_code


if __name__ == "__main__":
    raise SystemExit(main())
