from __future__ import annotations

from pathlib import Path

import pytest

from graph.effective_grants import EffectiveGrantSet, SelectedGrantSet
from harness.tool_execution import ShellPolicyAnalyzer


def _bindings(*, backend_mode: str = "docker", workspace_id: str = "workspace:one"):
    return {
        "approval_mode": "strict",
        "backend_mode": backend_mode,
        "backend_id": "runtime:ignored-for-session-directory",
        "policy_epoch": 1,
        "policy_version": "tool-execution-v4",
        "workspace_id": workspace_id,
    }


def _shell_bindings(*, workspace_id: str = "workspace:one"):
    return {
        "approval_mode": "strict",
        "policy_epoch": 1,
        "policy_version": "tool-execution-v4",
        "workspace_id": workspace_id,
        "isolation_policy_id": "kernel-docker-shared-v1",
        "profile_schema": "sandbox-grant-profile-v1",
    }


def test_run_directory_grant_without_bindings_is_effective_only_for_its_run(
    tmp_path: Path,
) -> None:
    root = tmp_path / "external"
    root.mkdir()
    grant = {
        "id": "grant-run",
        "type": "external_directory_read",
        "scope": "run",
        "target_kind": "exact_directory",
        "target": str(root),
        "capabilities": ["read", "recursive", "external_path"],
        "metadata": {"run_id": "run-one"},
    }

    current = EffectiveGrantSet.resolve([grant], run_id="run-one", current_bindings=_bindings())
    other = EffectiveGrantSet.resolve([grant], run_id="run-two", current_bindings=_bindings())

    assert current.allows_directory(root / "report.txt", access="read")
    assert not other.grants


def test_session_directory_grant_requires_equivalent_stable_bindings(tmp_path: Path) -> None:
    root = tmp_path / "external"
    root.mkdir()
    grant = {
        "id": "grant-session",
        "type": "external_directory_write",
        "scope": "session",
        "target_kind": "exact_directory",
        "target": str(root),
        "capabilities": ["write", "recursive", "external_path"],
        "bindings": _bindings(),
    }

    matching = EffectiveGrantSet.resolve([grant], run_id="run-one", current_bindings=_bindings())
    wrong_workspace = EffectiveGrantSet.resolve(
        [grant],
        run_id="run-one",
        current_bindings=_bindings(workspace_id="workspace:other"),
    )

    assert matching.allows_directory(root / "created.txt", access="write")
    assert not wrong_workspace.grants


def test_shell_access_requires_explicit_read_write_composite(tmp_path: Path) -> None:
    root = tmp_path / "external"
    root.mkdir()
    legacy = {
        "id": "grant-legacy",
        "type": "external_directory_write",
        "scope": "run",
        "target_kind": "exact_directory",
        "target": str(root),
        "capabilities": ["write", "recursive", "external_path"],
        "metadata": {"run_id": "run-one"},
    }
    shell_write = {
        **legacy,
        "id": "grant-shell-write",
        "capabilities": ["write", "recursive", "external_path", "shell_access"],
        "binding_schema_version": 3,
        "bindings": _shell_bindings(),
    }
    shell_read = {
        **legacy,
        "id": "grant-shell-read",
        "type": "external_directory_read",
        "capabilities": ["read", "recursive", "external_path", "shell_access"],
        "binding_schema_version": 3,
        "bindings": _shell_bindings(),
    }

    legacy_set = EffectiveGrantSet.resolve([legacy], run_id="run-one", current_bindings=_bindings())
    write_only_shell_set = EffectiveGrantSet.resolve(
        [shell_write],
        run_id="run-one",
        current_bindings=_bindings(),
        current_shell_bindings=_shell_bindings(),
    )
    composite_shell_set = EffectiveGrantSet.resolve(
        [shell_read, shell_write],
        run_id="run-one",
        current_bindings=_bindings(),
        current_shell_bindings=_shell_bindings(),
    )

    assert legacy_set.allows_directory(root / "created.txt", access="write")
    assert not legacy_set.allows_directory(
        root / "created.txt",
        access="write",
        required_capabilities={"shell_access"},
    )
    assert not legacy_set.allows_shell_directory(root / "created.txt", access="write")
    assert not write_only_shell_set.allows_shell_directory(root / "created.txt", access="write")
    assert composite_shell_set.allows_shell_directory(root / "created.txt", access="write")


def test_unbound_exact_file_grant_remains_effective_session_authority(
    tmp_path: Path,
) -> None:
    target = tmp_path / "report.txt"
    grant = {
        "id": "grant-file",
        "type": "external_file_write",
        "scope": "session",
        "target_kind": "exact_file",
        "target": str(target),
        "capabilities": ["write", "external_path"],
    }

    resolved = EffectiveGrantSet.resolve([grant], run_id="run-one", current_bindings=_bindings())

    assert [item.grant_id for item in resolved.grants] == ["grant-file"]


def test_revoked_or_superseded_grants_never_enter_effective_set(tmp_path: Path) -> None:
    root = tmp_path / "external"
    grants = [
        {
            "id": "revoked",
            "type": "external_directory_read",
            "scope": "run",
            "target_kind": "exact_directory",
            "target": str(root),
            "capabilities": ["read"],
            "metadata": {"run_id": "run-one"},
            "revoked_at": 1,
        },
        {
            "id": "superseded",
            "type": "external_directory_read",
            "scope": "run",
            "target_kind": "exact_directory",
            "target": str(root),
            "capabilities": ["read"],
            "metadata": {"run_id": "run-one"},
            "superseded_at": 1,
        },
    ]

    resolved = EffectiveGrantSet.resolve(grants, run_id="run-one", current_bindings=_bindings())

    assert not resolved.grants


def test_shell_projection_rejects_replaced_symlink_root(tmp_path: Path) -> None:
    original = tmp_path / "external"
    redirected = tmp_path / "redirected"
    original.mkdir()
    redirected.mkdir()
    grants = [
        {
            "id": f"grant-{access}",
            "type": f"external_directory_{access}",
            "scope": "run",
            "target_kind": "exact_directory",
            "target": str(original),
            "capabilities": [access, "recursive", "external_path", "shell_access"],
            "binding_schema_version": 3,
            "bindings": _shell_bindings(),
            "metadata": {"run_id": "run-one"},
        }
        for access in ("read", "write")
    ]
    effective = EffectiveGrantSet.resolve(
        grants,
        run_id="run-one",
        current_bindings=_bindings(),
        current_shell_bindings=_shell_bindings(),
    )
    original.rmdir()
    original.symlink_to(redirected, target_is_directory=True)

    assert not effective.allows_shell_directory(redirected / "report.txt", access="write")


def test_effective_set_carries_permission_revision() -> None:
    effective = EffectiveGrantSet.resolve(
        [],
        run_id="run-one",
        current_bindings=_bindings(),
        permission_revision=7,
    )

    assert effective.permission_revision == 7


def test_legacy_or_incomplete_shell_grant_is_never_effective(tmp_path: Path) -> None:
    root = tmp_path / "external"
    root.mkdir()
    base = {
        "type": "external_directory_read",
        "scope": "run",
        "target_kind": "exact_directory",
        "target": str(root),
        "capabilities": ["read", "recursive", "external_path", "shell_access"],
        "metadata": {"run_id": "run-one"},
    }
    legacy = {**base, "id": "legacy", "binding_schema_version": 2}
    incomplete = {
        **base,
        "id": "incomplete",
        "binding_schema_version": 3,
        "bindings": {"workspace_id": "workspace:one"},
    }

    effective = EffectiveGrantSet.resolve(
        [legacy, incomplete],
        run_id="run-one",
        current_bindings=_bindings(),
        current_shell_bindings=_shell_bindings(),
    )

    assert not effective.grants


def test_selected_grants_project_only_roots_used_by_current_command(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    source = tmp_path / "source"
    destination = tmp_path / "destination"
    unrelated = tmp_path / "unrelated"
    for path in (workspace, source, destination, unrelated):
        path.mkdir()
    (source / "report.txt").write_text("report", encoding="utf-8")
    grants = []
    for root, accesses in (
        (source, ("read",)),
        (destination, ("read", "write")),
        (unrelated, ("read", "write")),
    ):
        for access in accesses:
            grants.append(
                {
                    "id": f"{root.name}-{access}",
                    "type": f"external_directory_{access}",
                    "scope": "run",
                    "target_kind": "exact_directory",
                    "target": str(root),
                    "capabilities": [
                        access,
                        "recursive",
                        "external_path",
                        "shell_access",
                    ],
                    "binding_schema_version": 3,
                    "bindings": _shell_bindings(),
                    "metadata": {"run_id": "run-one"},
                }
            )
    effective = EffectiveGrantSet.resolve(
        grants,
        run_id="run-one",
        current_bindings=_bindings(),
        current_shell_bindings=_shell_bindings(),
        permission_revision=8,
    )
    command = f"cp {source / 'report.txt'} {destination / 'copy.txt'}"
    requirements = ShellPolicyAnalyzer.requirements(command, workspace_path=workspace)

    selected = SelectedGrantSet.select(effective, requirements)

    assert selected.read_roots == (destination, source)
    assert selected.write_roots == (destination,)
    assert selected.delete_roots == ()
    assert "unrelated-read" not in selected.grant_ids
    assert "unrelated-write" not in selected.grant_ids
    assert selected.permission_revision == 8


def test_selected_grants_require_delete_capability_for_mv(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    external = tmp_path / "external"
    workspace.mkdir()
    external.mkdir()
    source = external / "source.txt"
    source.write_text("source", encoding="utf-8")
    grants = [
        {
            "id": f"grant-{access}",
            "type": f"external_directory_{access}",
            "scope": "run",
            "target_kind": "exact_directory",
            "target": str(external),
            "capabilities": [access, "recursive", "external_path", "shell_access"],
            "binding_schema_version": 3,
            "bindings": _shell_bindings(),
            "metadata": {"run_id": "run-one"},
        }
        for access in ("read", "write")
    ]
    effective = EffectiveGrantSet.resolve(
        grants,
        run_id="run-one",
        current_bindings=_bindings(),
        current_shell_bindings=_shell_bindings(),
    )
    requirements = ShellPolicyAnalyzer.requirements(
        f"mv {source} {workspace / 'source.txt'}",
        workspace_path=workspace,
    )

    with pytest.raises(PermissionError, match="delete"):
        SelectedGrantSet.select(effective, requirements)
