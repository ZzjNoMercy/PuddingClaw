from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from harness.kernel_sandbox import LinuxBwrapSeccompRunner
from harness.sandbox_profiles import SandboxGrantProfile

pytestmark = pytest.mark.skipif(
    os.environ.get("PUDDINGCLAW_RUN_KERNEL_E2E") != "1" or sys.platform != "linux",
    reason="set PUDDINGCLAW_RUN_KERNEL_E2E=1 on Linux to run real bubblewrap E2E",
)


def test_bwrap_unrestricted_keeps_workspace_scratch_and_external_paths_writable(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    scratch = tmp_path / "scratch"
    external = tmp_path / "ordinary-host-directory"
    for path in (workspace, scratch, external):
        path.mkdir()
    profile = SandboxGrantProfile.build(
        workspace_root=workspace,
        scratch_root=scratch,
        filesystem="unrestricted",
    )
    runner = LinuxBwrapSeccompRunner(profile)
    workspace_target = workspace / "workspace-write.txt"
    scratch_target = scratch / "scratch-write.txt"
    external_target = external / "external-write.txt"

    result = runner.execute(
        " && ".join(
            (
                f"printf workspace > {workspace_target}",
                f"printf scratch > {scratch_target}",
                f"printf external > {external_target}",
                "printf 'HOME=%s\\n' \"$HOME\"",
            )
        )
    )

    assert result.exit_code == 0, result.output
    assert workspace_target.read_text(encoding="utf-8") == "workspace"
    assert scratch_target.read_text(encoding="utf-8") == "scratch"
    assert external_target.read_text(encoding="utf-8") == "external"
    assert f"HOME={Path.home().resolve()}" in result.output
