from pathlib import Path

from harness.execution_context import (
    AuthorizedExecution,
    bind_authorized_execution,
    current_authorized_execution,
)
from harness.execution_permits import ExecutionPermit
from harness.sandbox_profiles import SandboxGrantProfile
from harness.tool_execution import ShellPolicyAnalyzer


def test_authorized_execution_rechecks_revision_at_spawn(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    scratch = tmp_path / "scratch"
    workspace.mkdir()
    scratch.mkdir()
    command = "mkdir /workspace/output"
    requirements = ShellPolicyAnalyzer.requirements(command, workspace_path=workspace)
    profile = SandboxGrantProfile.build(
        workspace_root=workspace,
        scratch_root=scratch,
    )
    revision = [4]
    permit = ExecutionPermit.issue(
        tool_call_id="call-one",
        command=command,
        requirements=requirements,
        permission_revision=4,
        profile_digest=profile.digest,
        selected_runner="kernel_macos_seatbelt",
    )
    authorized = AuthorizedExecution(
        permit=permit,
        command=command,
        requirements=requirements,
        profile=profile,
        current_permission_revision=lambda: revision[0],
    )

    assert current_authorized_execution() is None
    with bind_authorized_execution(authorized):
        assert current_authorized_execution() is authorized
        assert authorized.valid_at_spawn(
            command=command,
            selected_runner="kernel_macos_seatbelt",
        )
        revision[0] = 5
        assert not authorized.valid_at_spawn(
            command=command,
            selected_runner="kernel_macos_seatbelt",
        )
    assert current_authorized_execution() is None
