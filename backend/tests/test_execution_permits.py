from pathlib import Path

from harness.execution_permits import ExecutionPermit
from harness.sandbox_profiles import SandboxGrantProfile
from harness.tool_execution import ShellPolicyAnalyzer


def _inputs(tmp_path: Path):
    workspace = tmp_path / "workspace"
    scratch = tmp_path / "scratch"
    workspace.mkdir()
    scratch.mkdir()
    command = "mkdir /workspace/output"
    requirements = ShellPolicyAnalyzer.requirements(
        command,
        workspace_path=workspace,
    )
    profile = SandboxGrantProfile.build(
        workspace_root=workspace,
        scratch_root=scratch,
    )
    return command, requirements, profile


def test_execution_permit_is_valid_only_for_exact_spawn_snapshot(tmp_path: Path) -> None:
    command, requirements, profile = _inputs(tmp_path)
    permit = ExecutionPermit.issue(
        tool_call_id="call-one",
        command=command,
        requirements=requirements,
        permission_revision=4,
        profile_digest=profile.digest,
        selected_runner="kernel_macos_seatbelt",
    )

    assert permit.valid_at_spawn(
        tool_call_id="call-one",
        command=command,
        requirements=requirements,
        current_permission_revision=4,
        profile_digest=profile.digest,
        selected_runner="kernel_macos_seatbelt",
    )
    assert not permit.valid_at_spawn(
        tool_call_id="call-one",
        command=command,
        requirements=requirements,
        current_permission_revision=5,
        profile_digest=profile.digest,
        selected_runner="kernel_macos_seatbelt",
    )


def test_execution_permit_rejects_command_or_runner_replay(tmp_path: Path) -> None:
    command, requirements, profile = _inputs(tmp_path)
    permit = ExecutionPermit.issue(
        tool_call_id="call-one",
        command=command,
        requirements=requirements,
        permission_revision=1,
        profile_digest=profile.digest,
        selected_runner="kernel_macos_seatbelt",
    )

    assert not permit.valid_at_spawn(
        tool_call_id="call-one",
        command="mkdir /workspace/other",
        requirements=requirements,
        current_permission_revision=1,
        profile_digest=profile.digest,
        selected_runner="kernel_macos_seatbelt",
    )
    assert not permit.valid_at_spawn(
        tool_call_id="call-one",
        command=command,
        requirements=requirements,
        current_permission_revision=1,
        profile_digest=profile.digest,
        selected_runner="docker",
    )
