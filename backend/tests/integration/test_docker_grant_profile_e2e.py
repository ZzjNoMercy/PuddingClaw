from __future__ import annotations

import os
from pathlib import Path

import pytest

from harness.sandbox_profiles import SandboxGrantProfile
from harness.tool_execution import ShellPolicyAnalyzer
from harness.workspace_backends import DEFAULT_SANDBOX_IMAGE, ProjectSandboxManager

pytestmark = pytest.mark.skipif(
    os.environ.get("PUDDINGCLAW_RUN_DOCKER_E2E") != "1",
    reason="set PUDDINGCLAW_RUN_DOCKER_E2E=1 to run the real Docker Grant Profile E2E",
)


def test_docker_executes_canonical_compound_external_command(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    scratch = tmp_path / "scratch"
    external = tmp_path / "external"
    external_alias = tmp_path / "external-alias"
    for path in (workspace, scratch, external):
        path.mkdir()
    external_alias.symlink_to(external, target_is_directory=True)
    source = external / "source.txt"
    target = external / "copy.txt"
    nested = external / "nested"
    source.write_bytes(b"docker-e2e\n")
    original_command = (
        f"cp {external_alias / 'source.txt'} {external_alias / 'copy.txt'}"
        f" && mkdir -p {external_alias / 'nested'}"
    )
    requirements = ShellPolicyAnalyzer.requirements(
        original_command,
        workspace_path=workspace,
    )
    profile = SandboxGrantProfile.build(
        workspace_root=workspace,
        scratch_root=scratch,
        external_read_roots=[external],
        external_write_roots=[external],
    )
    manager = ProjectSandboxManager(
        {
            "image": DEFAULT_SANDBOX_IMAGE,
            "cpu_limit": "2",
            "memory_limit_mb": 1024,
            "pids_limit": 128,
            "network_enabled": False,
        }
    )

    result = manager.run_ephemeral_grant_profile_command(
        workspace,
        command=requirements.execution_command,
        profile=profile,
        timeout=60,
        max_output_bytes=100_000,
    )

    assert result.exit_code == 0, result.output
    assert target.read_bytes() == source.read_bytes()
    assert nested.is_dir()


def test_docker_executes_authorized_external_mv_with_delete(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    scratch = tmp_path / "scratch"
    source_dir = tmp_path / "source"
    destination_dir = tmp_path / "destination"
    for path in (workspace, scratch, source_dir, destination_dir):
        path.mkdir()
    source = source_dir / "input.txt"
    target = destination_dir / "moved.txt"
    source.write_bytes(b"docker-mv-e2e\n")
    command = f"mv {source} {target} && ls -la {destination_dir}"
    requirements = ShellPolicyAnalyzer.requirements(command, workspace_path=workspace)
    profile = SandboxGrantProfile.build(
        workspace_root=workspace,
        scratch_root=scratch,
        external_read_roots=[source_dir, destination_dir],
        external_write_roots=[source_dir, destination_dir],
        external_delete_roots=[source_dir],
    )
    manager = ProjectSandboxManager(
        {
            "image": DEFAULT_SANDBOX_IMAGE,
            "cpu_limit": "2",
            "memory_limit_mb": 1024,
            "pids_limit": 128,
            "network_enabled": False,
        }
    )

    result = manager.run_ephemeral_grant_profile_command(
        workspace,
        command=requirements.execution_command,
        profile=profile,
        timeout=60,
        max_output_bytes=100_000,
    )

    assert result.exit_code == 0, result.output
    assert not source.exists()
    assert target.read_bytes() == b"docker-mv-e2e\n"


def test_docker_executes_authorized_external_python_script_read_only(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    scratch = tmp_path / "scratch"
    external = tmp_path / "external"
    for path in (workspace, scratch, external):
        path.mkdir()
    script = external / "run_once.py"
    sibling = external / "must-not-write.txt"
    script.write_text(
        "from pathlib import Path\n"
        "try:\n"
        "    Path(__file__).with_name('must-not-write.txt').write_text('bad')\n"
        "except OSError:\n"
        "    print('EXTERNAL_READ_ONLY_OK')\n"
        "print('PYTHON_EXTERNAL_EXEC_OK')\n",
        encoding="utf-8",
    )
    command = f"python3 {script}"
    requirements = ShellPolicyAnalyzer.requirements(command, workspace_path=workspace)
    profile = SandboxGrantProfile.build(
        workspace_root=workspace,
        scratch_root=scratch,
        external_read_roots=[external],
    )
    manager = ProjectSandboxManager(
        {
            "image": DEFAULT_SANDBOX_IMAGE,
            "cpu_limit": "2",
            "memory_limit_mb": 1024,
            "pids_limit": 128,
            "network_enabled": False,
        }
    )

    result = manager.run_ephemeral_grant_profile_command(
        workspace,
        command=requirements.execution_command,
        profile=profile,
        timeout=60,
        max_output_bytes=100_000,
    )

    assert result.exit_code == 0, result.output
    assert "EXTERNAL_READ_ONLY_OK" in result.output
    assert "PYTHON_EXTERNAL_EXEC_OK" in result.output
    assert not sibling.exists()
