from __future__ import annotations

from graph.virtual_paths import PathAuthority, classify_path_authority


def test_path_authority_has_one_canonical_workspace_decision(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    target = workspace / "reports" / "report.html"
    target.parent.mkdir()
    target.write_text("ok", encoding="utf-8")

    virtual = classify_path_authority(
        "/workspace/reports/report.html",
        workspace_root=workspace,
    )
    absolute = classify_path_authority(str(target), workspace_root=workspace)
    relative = classify_path_authority(
        "reports/report.html",
        workspace_root=workspace,
    )

    assert {virtual.authority, absolute.authority, relative.authority} == {
        PathAuthority.WORKSPACE
    }
    assert {
        virtual.virtual_path,
        absolute.virtual_path,
        relative.virtual_path,
    } == {"/workspace/reports/report.html"}
    assert virtual.canonical_host_path == target.resolve()


def test_path_authority_separates_internal_managed_and_external(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    assert (
        classify_path_authority("/scratch/report.html", workspace_root=workspace).authority
        is PathAuthority.SCRATCH
    )
    assert (
        classify_path_authority("/knowledge/report.md", workspace_root=workspace).authority
        is PathAuthority.MANAGED
    )
    assert (
        classify_path_authority(str(tmp_path / "outside.txt"), workspace_root=workspace).authority
        is PathAuthority.EXTERNAL
    )
    assert (
        classify_path_authority("/workspace-other/report.html", workspace_root=workspace).authority
        is PathAuthority.EXTERNAL
    )


def test_workspace_traversal_and_symlink_escape_are_rejected(tmp_path):
    workspace = tmp_path / "workspace"
    outside = tmp_path / "outside"
    workspace.mkdir()
    outside.mkdir()
    (workspace / "linked").symlink_to(outside, target_is_directory=True)

    traversal = classify_path_authority(
        "/workspace/../outside/secret.txt",
        workspace_root=workspace,
    )
    symlink = classify_path_authority(
        "/workspace/linked/secret.txt",
        workspace_root=workspace,
    )

    assert traversal.authority is PathAuthority.ESCAPE
    assert symlink.authority is PathAuthority.ESCAPE


def test_relative_backslash_traversal_is_rejected_consistently(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    direct = classify_path_authority(r"..\outside.txt", workspace_root=workspace)
    nested = classify_path_authority(
        r"reports\..\outside.txt",
        workspace_root=workspace,
    )

    assert direct.authority is PathAuthority.ESCAPE
    assert nested.authority is PathAuthority.ESCAPE


def test_host_workspace_symlink_escape_is_not_reclassified_as_external(tmp_path):
    workspace = tmp_path / "workspace"
    outside = tmp_path / "outside"
    workspace.mkdir()
    outside.mkdir()
    (workspace / "linked").symlink_to(outside, target_is_directory=True)

    classified = classify_path_authority(
        str(workspace / "linked" / "secret.txt"),
        workspace_root=workspace,
    )

    assert classified.authority is PathAuthority.ESCAPE


def test_bare_posix_absolute_path_is_never_a_dynamic_workspace_alias(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "report.html").write_text("workspace", encoding="utf-8")

    classified = classify_path_authority(
        "/report.html",
        workspace_root=workspace,
    )

    assert classified.authority is PathAuthority.EXTERNAL
    assert classified.virtual_path is None
