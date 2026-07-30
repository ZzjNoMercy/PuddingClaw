from pathlib import Path

import pytest

from graph.effective_grants import EffectiveGrantSet
from harness.shell_access import ShellAccessPlan
from harness.tool_execution import ShellPolicyAnalyzer


def _empty_effective() -> EffectiveGrantSet:
    return EffectiveGrantSet(grants=(), run_id="run-one", permission_revision=0)


def test_cp_compiles_one_atomic_multi_directory_shell_plan(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    source = tmp_path / "source"
    destination = tmp_path / "destination"
    for path in (workspace, source, destination):
        path.mkdir()
    (source / "report.txt").write_text("report", encoding="utf-8")
    requirements = ShellPolicyAnalyzer.requirements(
        f"cp {source / 'report.txt'} {destination / 'copy.txt'}",
        workspace_path=workspace,
    )

    plan = ShellAccessPlan.compile(requirements, _empty_effective())

    assert [
        (spec.target, spec.access, spec.delete) for spec in plan.grant_specs
    ] == [
        (str(destination), "read", False),
        (str(destination), "write", False),
        (str(source), "read", False),
    ]


def test_mv_requests_delete_only_on_source_directory(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    source = tmp_path / "source"
    destination = tmp_path / "destination"
    for path in (workspace, source, destination):
        path.mkdir()
    (source / "report.txt").write_text("report", encoding="utf-8")
    requirements = ShellPolicyAnalyzer.requirements(
        f"mv {source / 'report.txt'} {destination / 'report.txt'}",
        workspace_path=workspace,
    )

    plan = ShellAccessPlan.compile(requirements, _empty_effective())

    assert any(
        spec.target == str(source) and spec.access == "write" and spec.delete
        for spec in plan.grant_specs
    )
    assert not any(spec.target == str(destination) and spec.delete for spec in plan.grant_specs)


def test_mkdir_p_requests_nearest_existing_directory_ancestor(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    external = tmp_path / "external"
    workspace.mkdir()
    external.mkdir()
    requirements = ShellPolicyAnalyzer.requirements(
        f"mkdir -p {external / 'one' / 'two'}",
        workspace_path=workspace,
    )

    plan = ShellAccessPlan.compile(requirements, _empty_effective())

    assert [(spec.target, spec.access) for spec in plan.grant_specs] == [
        (str(external), "read"),
        (str(external), "write"),
    ]


def test_unsupported_compound_command_never_becomes_a_directory_prompt(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    requirements = ShellPolicyAnalyzer.requirements(
        "cp /tmp/a /tmp/b || echo done",
        workspace_path=workspace,
    )

    with pytest.raises(ValueError, match="Opaque"):
        ShellAccessPlan.compile(requirements, _empty_effective())
