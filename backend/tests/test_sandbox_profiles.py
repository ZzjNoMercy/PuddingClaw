from pathlib import Path

import pytest

from harness.sandbox_profiles import SandboxGrantProfile


def test_profile_rejects_write_only_external_shell_root(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    scratch = tmp_path / "scratch"
    external = tmp_path / "external"
    for path in (workspace, scratch, external):
        path.mkdir()

    with pytest.raises(ValueError, match="write root"):
        SandboxGrantProfile.build(
            workspace_root=workspace,
            scratch_root=scratch,
            external_write_roots=[external],
        )


def test_profile_digest_changes_with_authority_not_input_order(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    scratch = tmp_path / "scratch"
    first = tmp_path / "first"
    second = tmp_path / "second"
    for path in (workspace, scratch, first, second):
        path.mkdir()

    left = SandboxGrantProfile.build(
        workspace_root=workspace,
        scratch_root=scratch,
        external_read_roots=[first, second],
    )
    reordered = SandboxGrantProfile.build(
        workspace_root=workspace,
        scratch_root=scratch,
        external_read_roots=[first, second, first],
    )
    narrower = SandboxGrantProfile.build(
        workspace_root=workspace,
        scratch_root=scratch,
        external_read_roots=[first],
    )

    assert left.digest == reordered.digest
    assert left.digest != narrower.digest


def test_profile_rejects_root_replaced_by_symlink_before_spawn(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    scratch = tmp_path / "scratch"
    external = tmp_path / "external"
    redirected = tmp_path / "redirected"
    for path in (workspace, scratch, external, redirected):
        path.mkdir()
    profile = SandboxGrantProfile.build(
        workspace_root=workspace,
        scratch_root=scratch,
        external_read_roots=[external],
        external_write_roots=[external],
    )

    external.rmdir()
    external.symlink_to(redirected, target_is_directory=True)

    assert profile.valid_at_spawn() is False
