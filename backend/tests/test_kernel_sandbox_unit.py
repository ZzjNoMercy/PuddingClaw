from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from harness import kernel_sandbox
from harness.kernel_sandbox import LinuxBwrapSeccompRunner, MacOSSeatbeltRunner
from harness.sandbox_profiles import SandboxGrantProfile
from harness.workspace_backends import KernelWorkspaceBackend


def _profile(tmp_path: Path, *, workspace_writable: bool = True) -> SandboxGrantProfile:
    workspace = (tmp_path / "workspace").resolve()
    scratch = (tmp_path / "scratch").resolve()
    denied = (workspace / ".puddingclaw-control").resolve()
    workspace.mkdir()
    scratch.mkdir()
    denied.mkdir()
    return SandboxGrantProfile.build(
        workspace_root=workspace,
        scratch_root=scratch,
        workspace_writable=workspace_writable,
        external_deny_roots=[denied],
    )


def test_linux_seccomp_program_is_native_bpf_and_blocks_escape_syscalls(monkeypatch):
    monkeypatch.setattr(kernel_sandbox.sys, "platform", "linux")
    monkeypatch.setattr(
        kernel_sandbox.os,
        "uname",
        lambda: SimpleNamespace(machine="x86_64"),
    )

    program = kernel_sandbox._seccomp_filter_bytes()

    assert program
    assert len(program) % 8 == 0
    for syscall in (165, 166, 101, 272, 308, 428, 430, 442, 435):
        assert syscall.to_bytes(4, "little") in program


def test_linux_mount_projection_is_minimal_and_read_only_by_default(tmp_path: Path):
    profile = _profile(tmp_path, workspace_writable=False)
    runner = object.__new__(LinuxBwrapSeccompRunner)
    runner.profile = profile

    args = runner._mount_args()

    assert ["--ro-bind", "/", "/"] not in [args[index : index + 3] for index in range(len(args) - 2)]
    assert ("--ro-bind", str(profile.workspace_root)) in zip(args, args[1:])
    assert ("--tmpfs", str(profile.deny_roots[0])) in zip(args, args[1:])


def test_linux_unrestricted_mount_does_not_restrict_workspace_or_scratch_again(tmp_path: Path):
    workspace = (tmp_path / "workspace").resolve()
    scratch = (tmp_path / "scratch").resolve()
    workspace.mkdir()
    scratch.mkdir()
    profile = SandboxGrantProfile.build(
        workspace_root=workspace,
        scratch_root=scratch,
        filesystem="unrestricted",
    )
    runner = object.__new__(LinuxBwrapSeccompRunner)
    runner.profile = profile

    args = runner._mount_args()

    assert args[:3] == ["--bind", "/", "/"]
    assert str(workspace) not in args
    assert str(scratch) not in args
    assert not any(arg in {"--bind", "--ro-bind"} for arg in args[3:])
    assert ["--tmpfs", "/sys"] in [args[index : index + 2] for index in range(len(args) - 1)]


def test_kernel_environment_rejects_interpreter_injection():
    with pytest.raises(ValueError, match="not allowed"):
        kernel_sandbox._safe_environment(
            {"LD_PRELOAD": "/tmp/evil.so"},
            home=Path("/tmp/home"),
            tmp=Path("/tmp/tmp"),
        )


def test_mac_profile_projects_deny_carveout(tmp_path: Path):
    profile = _profile(tmp_path)
    runner = object.__new__(MacOSSeatbeltRunner)
    runner.profile = profile

    rendered = runner.render_profile()

    assert f'(deny file-read-metadata file-read* file-write* (subpath "{profile.deny_roots[0]}"))' in rendered


def test_kernel_virtual_paths_preserve_real_tmp_and_project_explicit_locators(tmp_path: Path):
    workspace = (tmp_path / "workspace").resolve()
    scratch = (tmp_path / "scratch").resolve()
    workspace.mkdir()
    scratch.mkdir()
    profile = SandboxGrantProfile.build(
        workspace_root=workspace,
        scratch_root=scratch,
    )

    mapped = kernel_sandbox._map_kernel_virtual_paths(
        "cd /tmp && cp /workspace/input.pdf /scratch/output.pdf && printf x > '/tmp/result.txt'",
        profile=profile,
    )

    assert "cd /tmp" in mapped
    assert str(workspace / "input.pdf") in mapped
    assert str(scratch / "output.pdf") in mapped
    assert "'/tmp/result.txt'" in mapped


def test_kernel_external_directory_projects_exact_cwd_and_write_root(
    tmp_path: Path,
    monkeypatch,
):
    workspace = (tmp_path / "workspace").resolve()
    scratch = (tmp_path / "scratch").resolve()
    external = (tmp_path / "external").resolve()
    workspace.mkdir()
    scratch.mkdir()
    external.mkdir()
    captured = {}

    class FakeRunner:
        def execute(self, command, **kwargs):
            captured["command"] = command
            captured.update(kwargs)
            return SimpleNamespace(output="ok", exit_code=0, truncated=False)

    fake_runner = FakeRunner()
    monkeypatch.setattr(kernel_sandbox, "kernel_runner_for_profile", lambda profile, **_: (captured.setdefault("profile", profile), fake_runner)[1])
    backend = object.__new__(KernelWorkspaceBackend)
    backend.workspace_path = workspace
    backend.scratch_path = scratch
    backend._default_timeout = 17

    response = backend.execute_external_directory(
        str(external),
        "printf ok",
        timeout=11,
        writable=True,
    )

    assert response.exit_code == 0
    assert captured["command"] == "printf ok"
    assert captured["cwd"] == external
    assert captured["timeout"] == 11
    assert captured["profile"].workspace_writable is False
    assert external in captured["profile"].read_roots
    assert external in captured["profile"].write_roots
    assert workspace not in captured["profile"].write_roots


def test_kernel_working_directory_must_be_profile_covered(tmp_path: Path):
    profile = _profile(tmp_path, workspace_writable=False)
    outside = (tmp_path / "outside").resolve()
    outside.mkdir()

    with pytest.raises(ValueError, match="outside the execution profile"):
        kernel_sandbox._validated_working_directory(profile, outside)
