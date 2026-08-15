from __future__ import annotations

import shlex
import tempfile
from pathlib import Path
from types import SimpleNamespace

import pytest
from langchain.agents.middleware.types import ToolCallRequest


def _request(command: str, workspace: Path) -> ToolCallRequest:
    return ToolCallRequest(
        tool_call={"id": "authority-test", "name": "execute", "args": {"command": command}},
        tool=None,
        state={},
        runtime=SimpleNamespace(context={"workspace_path": str(workspace)}),
    )


def _smart_pipeline(workspace: Path, backend_mode: str):
    from graph.permission_policy import RunPermissionContext
    from harness.tool_execution import ToolExecutionPipeline

    context = RunPermissionContext.from_config_snapshot(
        {
            "permissions": {"approval_mode": "smart", "policy_epoch": 1},
            "execution": {
                "backend_mode": backend_mode,
                "backend_id": f"{backend_mode}:authority-matrix",
                "filesystem_mode": "unrestricted",
            },
        }
    )
    backend = SimpleNamespace(
        mode=backend_mode,
        effective_mode=backend_mode,
        filesystem_mode="unrestricted",
        filesystem_read_roots=(),
        filesystem_write_roots=(),
        filesystem_delete_roots=(),
        resolve_execution_path=lambda value: value,
    )
    return ToolExecutionPipeline(
        known_tools={"execute"},
        backend_mode=backend_mode,
        permission_context=context,
        workspace_backend=backend,
    )


@pytest.mark.parametrize(
    ("module_name", "keyword"),
    [
        ("harness.workspace_backends", "max_output_bytes"),
        ("harness.kernel_sandbox", "limit"),
    ],
)
def test_bounded_output_filters_only_known_grpc_fork_noise(module_name: str, keyword: str) -> None:
    import importlib

    bounded_output = importlib.import_module(module_name)._bounded_output
    stderr = "\n".join(
        [
            "E0000 ev_poll_posix.cc:123] FD from fork parent still in poll list 7",
            "permission denied",
            "ev_poll_posix.cc: this is a different diagnostic",
        ]
    )

    output, truncated = bounded_output("stdout", stderr, **{keyword: 10_000})

    assert "FD from fork parent still in poll list" not in output
    assert "[stderr] permission denied" in output
    assert "[stderr] ev_poll_posix.cc: this is a different diagnostic" in output
    assert truncated is False


def test_unrestricted_profile_has_no_project_filesystem_roots(tmp_path: Path) -> None:
    from harness.sandbox_profiles import SandboxGrantProfile

    workspace = tmp_path / "workspace"
    scratch = tmp_path / "scratch"
    workspace.mkdir()
    scratch.mkdir()
    profile = SandboxGrantProfile.build(
        workspace_root=workspace,
        scratch_root=scratch,
        filesystem="unrestricted",
    )

    assert profile.filesystem == "unrestricted"
    assert profile.read_roots == ()
    assert profile.write_roots == ()
    assert profile.delete_roots == ()


def test_kernel_unrestricted_profiles_expose_host_filesystem_view(tmp_path: Path) -> None:
    from harness.kernel_sandbox import LinuxBwrapSeccompRunner, MacOSSeatbeltRunner
    from harness.sandbox_profiles import SandboxGrantProfile

    workspace = tmp_path / "workspace"
    scratch = tmp_path / "scratch"
    workspace.mkdir()
    scratch.mkdir()
    profile = SandboxGrantProfile.build(
        workspace_root=workspace,
        scratch_root=scratch,
        filesystem="unrestricted",
    )
    linux = object.__new__(LinuxBwrapSeccompRunner)
    linux.profile = profile
    linux_args = linux._mount_args()
    macos = object.__new__(MacOSSeatbeltRunner)
    macos.profile = profile
    macos_profile = macos.render_profile()

    assert linux_args[:3] == ["--bind", "/", "/"]
    assert ["--tmpfs", "/home"] not in [linux_args[index : index + 2] for index in range(len(linux_args) - 1)]
    assert str(workspace) not in linux_args
    assert str(scratch) not in linux_args
    assert not any(arg in {"--ro-bind", "--bind"} for arg in linux_args[3:])
    assert "(allow file-read*)" in macos_profile
    assert '(allow file-write* (regex #"^/"))' in macos_profile
    assert f"(subpath {workspace}" not in macos_profile


def test_smart_shell_does_not_classify_host_path_as_harness_denial(tmp_path: Path) -> None:
    from harness.tool_execution import PolicyDecision, ShellPolicyAnalyzer

    workspace = tmp_path / "workspace"
    outside = tmp_path / "other"
    workspace.mkdir()
    outside.mkdir()
    command = f"cp {outside / 'source.txt'} {outside / 'target.txt'}"

    smart = ShellPolicyAnalyzer(
        workspace_path=str(workspace),
        backend_mode="spawn",
        filesystem_mode="unrestricted",
    ).analyze(command)
    strict = ShellPolicyAnalyzer(
        workspace_path=str(workspace),
        backend_mode="spawn",
        filesystem_mode="restricted",
    ).analyze(command)

    assert smart.decision is not PolicyDecision.DENY
    assert smart.reason != "host_filesystem_access"
    assert strict.reason == "host_filesystem_access"


def test_smart_compound_python_write_is_not_misclassified_as_webbridge_access(
    tmp_path: Path,
) -> None:
    from harness.tool_execution import PolicyDecision

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    command = (
        'python3 -c \'from pathlib import Path; Path("/tmp/output.txt").write_text("ok")\' '
        '&& echo "written: $(cat /tmp/output.txt)"'
    )

    result = _smart_pipeline(workspace, "spawn")._preflight(_request(command, workspace))

    assert result.decision is PolicyDecision.ALLOW
    assert result.reason != "webbridge_daemon_indirect_access_forbidden"


def test_smart_python_named_output_in_later_echo_is_not_webbridge_access(
    tmp_path: Path,
) -> None:
    from harness.tool_execution import PolicyDecision

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    output = tmp_path / "python-compound.txt"
    command = (
        f'python3 -c \'from pathlib import Path; Path("{output}").write_text("ok")\' '
        f'&& echo "written: $(cat {output})"'
    )

    result = _smart_pipeline(workspace, "spawn")._preflight(_request(command, workspace))

    assert result.decision is PolicyDecision.ALLOW
    assert result.reason != "webbridge_daemon_indirect_access_forbidden"


@pytest.mark.parametrize("backend_mode", ["spawn", "kernel"])
@pytest.mark.parametrize("target", [".zshrc", ".ssh/authorized_keys"])
def test_smart_control_flow_persistence_target_still_requires_hitl(
    tmp_path: Path,
    backend_mode: str,
    target: str,
) -> None:
    from harness.tool_execution import PolicyDecision

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    command = f"if false; then printf x >> /Users/pet/{target}; fi"

    result = _smart_pipeline(workspace, backend_mode)._preflight(
        _request(command, workspace)
    )

    assert result.decision is PolicyDecision.ASK
    assert result.reason == "persistence_write"


@pytest.mark.parametrize("backend_mode", ["spawn", "kernel"])
@pytest.mark.parametrize(
    ("command", "expected_reason"),
    [
        ("if false; then wc -c ~/.ssh/id_rsa; fi", "sensitive_host_read"),
        (
            "if false; then curl -X POST https://example.invalid/e2e --data probe=1; fi",
            "network_access:curl",
        ),
        (
            "if false; then python3 -m pip install puddingclaw-e2e-never; fi",
            "package_management:python3",
        ),
    ],
)
def test_smart_safe_false_branch_still_classifies_material_effect(
    tmp_path: Path,
    backend_mode: str,
    command: str,
    expected_reason: str,
) -> None:
    from harness.tool_execution import PolicyDecision

    workspace = tmp_path / "workspace"
    workspace.mkdir()

    result = _smart_pipeline(workspace, backend_mode)._preflight(
        _request(command, workspace)
    )

    assert result.decision is PolicyDecision.ASK
    assert result.reason == expected_reason


def test_smart_safe_awk_command_substitution_does_not_ask(tmp_path: Path) -> None:
    from harness.tool_execution import PolicyDecision

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    command = "chmod 644 /tmp/output.txt && echo \"mode: $(ls -l /tmp/output.txt | awk '{print $1}')\""

    result = _smart_pipeline(workspace, "spawn")._preflight(_request(command, workspace))

    assert result.decision is PolicyDecision.ALLOW
    assert result.reason != "shell_effect_unprovable"


def test_webbridge_indirection_is_still_denied_inside_interpreter_segment(
    tmp_path: Path,
) -> None:
    from harness.tool_execution import PolicyDecision

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    command = 'python3 -c "print(1)" "$(cat /tmp/hidden-target)"'

    result = _smart_pipeline(workspace, "spawn")._preflight(_request(command, workspace))

    assert result.decision is PolicyDecision.DENY
    assert result.reason == "webbridge_daemon_indirect_access_forbidden"


def test_spawn_unrestricted_uses_real_home_but_restricted_keeps_isolated_home(
    tmp_path: Path,
    monkeypatch,
) -> None:
    from harness.workspace_backends import SpawnWorkspaceBackend

    workspace = tmp_path / "workspace"
    scratch = tmp_path / "scratch"
    real_home = tmp_path / "real-home"
    workspace.mkdir()
    scratch.mkdir()
    real_home.mkdir()
    monkeypatch.setenv("HOME", str(real_home))
    backend = SpawnWorkspaceBackend(root_dir=workspace, scratch_path=scratch)

    restricted_home = backend._execution_environment()["HOME"]
    backend.filesystem_mode = "unrestricted"
    unrestricted_home = backend._execution_environment()["HOME"]

    assert restricted_home == str(workspace / ".puddingclaw" / "runtime" / "host-home")
    assert unrestricted_home == str(real_home.resolve())


def test_external_file_conflict_restores_full_real_path(tmp_path: Path) -> None:
    from deepagents.backends.protocol import WriteResult

    from graph.permissioned_filesystem_backend import PermissionedCompositeBackend

    target = tmp_path / "nested" / "source.txt"
    result = WriteResult(error="Cannot write to /source.txt because it already exists.")

    restored = PermissionedCompositeBackend._restore_external_path(result, str(target))

    assert str(target) in str(restored.error)
    assert "Cannot write to /source.txt" not in str(restored.error)


def test_external_permission_error_does_not_duplicate_an_already_real_path(
    tmp_path: Path,
) -> None:
    from deepagents.backends.protocol import ReadResult

    from graph.permissioned_filesystem_backend import PermissionedCompositeBackend

    target = tmp_path / "result" / "os-denied.txt"
    raw_error = (
        "Error reading file '/os-denied.txt': [Errno 13] Permission denied: "
        f"'{target}'"
    )

    restored = PermissionedCompositeBackend._restore_external_path(
        ReadResult(error=raw_error),
        str(target),
    )

    assert restored.error == (
        f"Error reading file '{target}': [Errno 13] Permission denied: '{target}'"
    )
    assert f"{target.parent}{target}" not in str(restored.error)


def test_kernel_unrestricted_uses_real_home_but_restricted_keeps_runtime_home(
    tmp_path: Path,
    monkeypatch,
) -> None:
    from harness.kernel_sandbox import _kernel_home
    from harness.sandbox_profiles import SandboxGrantProfile

    workspace = tmp_path / "workspace"
    scratch = tmp_path / "scratch"
    runtime = tmp_path / "kernel-runtime"
    real_home = tmp_path / "real-home"
    workspace.mkdir()
    scratch.mkdir()
    runtime.mkdir()
    real_home.mkdir()
    monkeypatch.setenv("HOME", str(real_home))
    restricted = SandboxGrantProfile.build(
        workspace_root=workspace,
        scratch_root=scratch,
        filesystem="restricted",
    )
    unrestricted = SandboxGrantProfile.build(
        workspace_root=workspace,
        scratch_root=scratch,
        filesystem="unrestricted",
    )

    assert _kernel_home(restricted, runtime) == runtime / "home"
    assert _kernel_home(unrestricted, runtime) == real_home.resolve()


def test_smart_pipeline_skips_external_directory_authority_for_cp(tmp_path: Path) -> None:
    from graph.permission_policy import RunPermissionContext
    from harness.tool_execution import PolicyDecision, ToolExecutionPipeline

    workspace = tmp_path / "workspace"
    outside = tmp_path / "other"
    workspace.mkdir()
    outside.mkdir()
    backend = SimpleNamespace(
        mode="spawn",
        effective_mode="spawn",
        filesystem_mode="unrestricted",
        filesystem_read_roots=(),
        filesystem_write_roots=(),
        filesystem_delete_roots=(),
        resolve_execution_path=lambda value: value,
    )
    context = RunPermissionContext.from_config_snapshot(
        {
            "permissions": {"approval_mode": "smart", "policy_epoch": 1},
            "execution": {"backend_mode": "spawn", "backend_id": "spawn:test"},
        }
    )
    pipeline = ToolExecutionPipeline(
        known_tools={"execute"},
        backend_mode="spawn",
        permission_context=context,
        workspace_backend=backend,
    )

    command = f"cp {outside / 'source.txt'} {outside / 'target.txt'}"
    request = _request(command, workspace)

    assert pipeline._require_external_shell_authority(request) is None
    result = pipeline._preflight(request)
    assert result.decision is PolicyDecision.ALLOW
    assert result.reason != "filesystem_grant_access_denied"


@pytest.mark.parametrize("backend_mode", ["spawn", "kernel"])
def test_smart_unrestricted_mv_between_real_paths_never_asks_for_project_write(
    tmp_path: Path,
    backend_mode: str,
) -> None:
    from graph.permission_policy import RunPermissionContext
    from harness.tool_execution import PolicyDecision, ToolExecutionPipeline

    workspace = tmp_path / "workspace"
    scratch = tmp_path / "scratch"
    workspace.mkdir()
    scratch.mkdir()
    backend = SimpleNamespace(
        mode=backend_mode,
        effective_mode=backend_mode,
        filesystem_mode="unrestricted",
        filesystem_read_roots=(),
        filesystem_write_roots=(),
        filesystem_delete_roots=(),
        resolve_execution_path=lambda value: value,
    )
    context = RunPermissionContext.from_config_snapshot(
        {
            "permissions": {"approval_mode": "smart", "policy_epoch": 1},
            "execution": {
                "backend_mode": backend_mode,
                "backend_id": f"{backend_mode}:test",
                "filesystem_mode": "unrestricted",
            },
        }
    )
    pipeline = ToolExecutionPipeline(
        known_tools={"execute"},
        backend_mode=backend_mode,
        permission_context=context,
        workspace_backend=backend,
    )
    command = (
        "mv /tmp/puddingclaw-fs-e2e/project-b/source.txt /tmp/puddingclaw-fs-e2e/result/shell-moved.txt && echo MV_OK"
    )

    result = pipeline._preflight(_request(command, workspace))

    assert result.decision is PolicyDecision.ALLOW
    assert result.reason == "smart_sandbox_workspace_write"


def test_smart_spawn_executes_real_tmp_mv_without_scratch_projection(tmp_path: Path) -> None:
    from graph.permission_policy import RunPermissionContext
    from harness.tool_execution import PolicyDecision, ToolExecutionPipeline
    from harness.workspace_backends import SpawnWorkspaceBackend

    workspace = tmp_path / "workspace"
    scratch = tmp_path / "scratch"
    workspace.mkdir()
    scratch.mkdir()
    backend = SpawnWorkspaceBackend(root_dir=workspace, scratch_path=scratch)
    backend.filesystem_mode = "unrestricted"
    context = RunPermissionContext.from_config_snapshot(
        {
            "permissions": {"approval_mode": "smart", "policy_epoch": 1},
            "execution": {
                "backend_mode": "spawn",
                "backend_id": backend.id,
                "filesystem_mode": "unrestricted",
            },
        }
    )
    pipeline = ToolExecutionPipeline(
        known_tools={"execute"},
        backend_mode="spawn",
        permission_context=context,
        workspace_backend=backend,
    )

    with tempfile.TemporaryDirectory(prefix="puddingclaw-fs-e2e-", dir="/tmp") as real_tmp:
        source = Path(real_tmp) / "project-b" / "source.txt"
        target = Path(real_tmp) / "result" / "shell-moved.txt"
        source.parent.mkdir()
        target.parent.mkdir()
        source.write_text("move me", encoding="utf-8")
        command = f"mv {shlex.quote(str(source))} {shlex.quote(str(target))} && echo MV_OK"

        preflight = pipeline._preflight(_request(command, workspace))
        executed = backend.execute(command)

        assert preflight.decision is PolicyDecision.ALLOW
        assert executed.exit_code == 0
        assert "MV_OK" in executed.output
        assert not source.exists()
        assert target.read_text(encoding="utf-8") == "move me"
        assert not (scratch / "tmp" / target.name).exists()


def test_smart_unrestricted_keeps_dynamic_effect_policy(tmp_path: Path) -> None:
    from graph.permission_policy import RunPermissionContext
    from harness.tool_execution import PolicyDecision, ToolExecutionPipeline

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    context = RunPermissionContext.from_config_snapshot(
        {
            "permissions": {"approval_mode": "smart", "policy_epoch": 1},
            "execution": {
                "backend_mode": "spawn",
                "backend_id": "spawn:test",
                "filesystem_mode": "unrestricted",
            },
        }
    )
    pipeline = ToolExecutionPipeline(
        known_tools={"execute"},
        backend_mode="spawn",
        permission_context=context,
    )
    command = "python3 -c \"__import__('os').__getattribute__('remove')('/tmp/target')\""

    result = pipeline._preflight(_request(command, workspace))

    assert result.decision is PolicyDecision.ASK
    assert result.reason == "local_dynamic_effect_unprovable"


@pytest.mark.parametrize("backend_mode", ["spawn", "kernel"])
def test_smart_unrestricted_allows_single_file_chmod_for_os_error_probe(
    tmp_path: Path,
    backend_mode: str,
) -> None:
    from graph.permission_policy import RunPermissionContext
    from harness.tool_execution import PolicyDecision, ToolExecutionPipeline

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    context = RunPermissionContext.from_config_snapshot(
        {
            "permissions": {"approval_mode": "smart", "policy_epoch": 1},
            "execution": {
                "backend_mode": backend_mode,
                "backend_id": f"{backend_mode}:test",
                "filesystem_mode": "unrestricted",
            },
        }
    )
    pipeline = ToolExecutionPipeline(
        known_tools={"execute"},
        backend_mode=backend_mode,
        permission_context=context,
    )
    command = (
        "printf 'denied' > /tmp/puddingclaw-fs-e2e/denied.txt && "
        "chmod 000 /tmp/puddingclaw-fs-e2e/denied.txt && "
        "ls -l /tmp/puddingclaw-fs-e2e/denied.txt"
    )

    result = pipeline._preflight(_request(command, workspace))

    assert result.decision is PolicyDecision.ALLOW
    assert result.reason == "smart_sandbox_workspace_write"


def test_smart_unrestricted_keeps_recursive_chmod_gated(tmp_path: Path) -> None:
    from graph.permission_policy import RunPermissionContext
    from harness.tool_execution import PolicyDecision, ToolExecutionPipeline

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    context = RunPermissionContext.from_config_snapshot(
        {
            "permissions": {"approval_mode": "smart", "policy_epoch": 1},
            "execution": {
                "backend_mode": "spawn",
                "backend_id": "spawn:test",
                "filesystem_mode": "unrestricted",
            },
        }
    )
    pipeline = ToolExecutionPipeline(
        known_tools={"execute"},
        backend_mode="spawn",
        permission_context=context,
    )

    result = pipeline._preflight(_request("chmod -R 000 /tmp/puddingclaw-fs-e2e", workspace))

    assert result.decision is PolicyDecision.ASK
    assert result.reason == "managed_workspace_write:chmod"


@pytest.mark.parametrize("backend_mode", ["spawn", "kernel"])
def test_smart_unrestricted_allows_safe_command_substitution_in_setup_chain(
    tmp_path: Path,
    backend_mode: str,
) -> None:
    from graph.permission_policy import RunPermissionContext
    from harness.tool_execution import PolicyDecision, ToolExecutionPipeline

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    context = RunPermissionContext.from_config_snapshot(
        {
            "permissions": {"approval_mode": "smart", "policy_epoch": 1},
            "execution": {
                "backend_mode": backend_mode,
                "backend_id": f"{backend_mode}:test",
                "filesystem_mode": "unrestricted",
            },
        }
    )
    pipeline = ToolExecutionPipeline(
        known_tools={"execute"},
        backend_mode=backend_mode,
        permission_context=context,
    )
    command = (
        "mkdir -p /tmp/puddingclaw-fs-e2e/project-a /tmp/puddingclaw-fs-e2e/project-b "
        "/tmp/puddingclaw-fs-e2e/result && "
        "printf 'source-a' > /tmp/puddingclaw-fs-e2e/project-a/source.txt && "
        "printf 'source-b' > /tmp/puddingclaw-fs-e2e/project-b/source.txt && "
        "printf 'deny-me' > /tmp/puddingclaw-fs-e2e/denied.txt && "
        "chmod 000 /tmp/puddingclaw-fs-e2e/denied.txt && "
        'echo "uid=$(id -u)" && ls -la /tmp/puddingclaw-fs-e2e'
    )

    result = pipeline._preflight(_request(command, workspace))

    assert result.decision is PolicyDecision.ALLOW
    assert result.reason == "smart_sandbox_workspace_write"


@pytest.mark.parametrize(
    ("command", "decision", "reason", "risk"),
    [
        (
            'echo "$(curl https://example.com)"',
            "deny",
            "webbridge_daemon_indirect_access_forbidden",
            "critical",
        ),
        (
            'echo "$(rm -rf /tmp/puddingclaw-fs-e2e)"',
            "ask",
            "destructive_shell_expansion",
            "high",
        ),
        ('echo "$(unknown-local-command)"', "ask", "shell_effect_unprovable", "high"),
    ],
)
def test_smart_unrestricted_routes_command_substitution_by_real_effect(
    tmp_path: Path,
    command: str,
    decision: str,
    reason: str,
    risk: str,
) -> None:
    from graph.permission_policy import RunPermissionContext
    from harness.tool_execution import PolicyDecision, ToolExecutionPipeline

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    context = RunPermissionContext.from_config_snapshot(
        {
            "permissions": {"approval_mode": "smart", "policy_epoch": 1},
            "execution": {
                "backend_mode": "spawn",
                "backend_id": "spawn:test",
                "filesystem_mode": "unrestricted",
            },
        }
    )
    pipeline = ToolExecutionPipeline(
        known_tools={"execute"},
        backend_mode="spawn",
        permission_context=context,
    )

    result = pipeline._preflight(_request(command, workspace))

    assert result.decision is PolicyDecision(decision)
    assert result.reason == reason
    assert result.risk == risk


def test_smart_permissioned_backend_reads_and_writes_external_file_without_grant(tmp_path: Path) -> None:
    import hashlib

    from deepagents.backends import FilesystemBackend

    from graph.permissioned_filesystem_backend import PermissionedCompositeBackend

    workspace = tmp_path / "workspace"
    outside = tmp_path / "other"
    workspace.mkdir()
    outside.mkdir()
    target = outside / "note.txt"
    target.write_text("before", encoding="utf-8")
    backend = PermissionedCompositeBackend(
        default=FilesystemBackend(root_dir=workspace, virtual_mode=True),
        routes={"/workspace/": FilesystemBackend(root_dir=workspace, virtual_mode=True)},
        session_id="",
        workspace_root=workspace,
    )
    backend.filesystem_mode = "unrestricted"

    assert backend.read(str(target)).error is None
    written = backend.edit(str(target), "before", "after")
    assert written.error is None
    assert target.read_text(encoding="utf-8") == "after"
    sibling = outside / "sibling.txt"
    sibling.write_text("needle", encoding="utf-8")

    listed = backend.ls(str(outside))
    globbed = backend.glob("*.txt", path=str(outside))
    grepped = backend.grep("needle", path=str(outside), glob="*.txt")

    assert listed.error is None
    assert {item["path"] for item in listed.entries or []} == {str(target), str(sibling)}
    assert globbed.error is None
    assert {item["path"] for item in globbed.matches or []} == {str(target), str(sibling)}
    assert grepped.error is None
    assert [item["path"] for item in grepped.matches or []] == [str(sibling)]

    copied = outside / "copied.txt"
    copy_result = backend.copy_external_file(
        str(sibling),
        str(copied),
        expected_source_sha256=None,
    )
    assert copy_result["status"] == "completed"
    assert copied.read_text(encoding="utf-8") == "needle"
    copied_sha256 = "sha256:" + hashlib.sha256(copied.read_bytes()).hexdigest()
    delete_result = backend.delete_external_file(
        str(copied),
        expected_sha256=copied_sha256,
    )
    assert delete_result["status"] == "completed"
    assert not copied.exists()

    missing = backend.read(str(outside / "missing.txt"))
    assert missing.error is not None
    assert "permission_required" not in str(missing.error)
    assert "not found" in str(missing.error)
    assert str(outside / "missing.txt") in str(missing.error)


@pytest.mark.parametrize("backend_mode", ["spawn", "kernel"])
@pytest.mark.parametrize(
    "command",
    [
        "cat /tmp/puddingclaw-fs-e2e/source.txt",
        "stat /tmp/puddingclaw-fs-e2e/source.txt",
        "find /tmp/puddingclaw-fs-e2e -maxdepth 1 -type f",
        "mkdir -p /tmp/puddingclaw-fs-e2e/nested",
        "printf x > /tmp/puddingclaw-fs-e2e/output.txt",
        "touch /tmp/puddingclaw-fs-e2e/touched.txt",
        "cp /tmp/puddingclaw-fs-e2e/source.txt /tmp/puddingclaw-fs-e2e/copy.txt",
        "mv /tmp/puddingclaw-fs-e2e/copy.txt /tmp/puddingclaw-fs-e2e/moved.txt",
        "chmod 000 /tmp/puddingclaw-fs-e2e/source.txt",
        "ln -s /tmp/puddingclaw-fs-e2e/source.txt /tmp/puddingclaw-fs-e2e/link.txt",
        "rm /tmp/puddingclaw-fs-e2e/source.txt",
        "sed -i.bak 's/a/b/' /tmp/puddingclaw-fs-e2e/source.txt",
        "sort -o /tmp/puddingclaw-fs-e2e/sorted.txt /tmp/puddingclaw-fs-e2e/source.txt",
        "printf x | tee /tmp/puddingclaw-fs-e2e/tee.txt",
        "cat /tmp/puddingclaw-fs-e2e/source.txt | wc -c",
        "mkdir -p /tmp/puddingclaw-fs-e2e/a; touch /tmp/puddingclaw-fs-e2e/a/b",
        "test -f /tmp/puddingclaw-fs-e2e/source.txt || touch /tmp/puddingclaw-fs-e2e/source.txt",
        "( mkdir -p /tmp/puddingclaw-fs-e2e/a && touch /tmp/puddingclaw-fs-e2e/a/b )",
        "FOO=bar env > /tmp/puddingclaw-fs-e2e/env.txt",
        "echo ${USER} > /tmp/puddingclaw-fs-e2e/user.txt",
        'echo "uid=$(id -u)" > /tmp/puddingclaw-fs-e2e/id.txt',
        "echo $((1+2)) > /tmp/puddingclaw-fs-e2e/arithmetic.txt",
        "ls /tmp/puddingclaw-fs-e2e/*.txt",
        "if true; then mkdir -p /tmp/puddingclaw-fs-e2e/conditional; fi",
        "for name in a b; do touch /tmp/puddingclaw-fs-e2e/$name; done",
        "python3 -c \"from pathlib import Path; Path('/tmp/puddingclaw-fs-e2e/py.txt').write_text('x')\"",
        "rsync -a /tmp/puddingclaw-fs-e2e/source.txt /tmp/puddingclaw-fs-e2e/rsync.txt",
        "install -m 0644 /tmp/puddingclaw-fs-e2e/source.txt /tmp/puddingclaw-fs-e2e/installed.txt",
    ],
)
def test_smart_unrestricted_ordinary_shell_matrix_has_no_hitl(
    tmp_path: Path,
    backend_mode: str,
    command: str,
) -> None:
    from harness.tool_execution import PolicyDecision

    workspace = tmp_path / "workspace"
    workspace.mkdir()

    result = _smart_pipeline(workspace, backend_mode)._preflight(_request(command, workspace))

    assert result.decision is PolicyDecision.ALLOW, (command, result)
    assert result.reason not in {
        "complex_shell_expansion",
        "host_filesystem_access",
        "local_dynamic_effect_unprovable",
        "managed_workspace_write:chmod",
        "managed_workspace_write:mv",
    }


@pytest.mark.parametrize("backend_mode", ["spawn", "kernel"])
@pytest.mark.parametrize(
    ("command", "decision", "reason"),
    [
        ("rm -rf /tmp/puddingclaw-fs-e2e", "ask", "destructive_workspace_delete:rm_recursive"),
        ("( rm -rf /tmp/puddingclaw-fs-e2e )", "ask", "destructive_workspace_delete:rm_recursive"),
        ("if true; then rm -rf /tmp/puddingclaw-fs-e2e; fi", "ask", "destructive_shell_effect"),
        ("echo <(rm -rf /tmp/puddingclaw-fs-e2e)", "ask", "destructive_shell_expansion"),
        ("chmod -R 000 /tmp/puddingclaw-fs-e2e", "ask", "managed_workspace_write:chmod"),
        ("( chmod -R 000 /tmp/puddingclaw-fs-e2e )", "ask", "managed_workspace_write:chmod"),
        ("find /tmp/puddingclaw-fs-e2e -delete", "ask", "managed_workspace_write:find:-delete"),
        ("printf x >> ~/.zshrc", "ask", "persistence_write"),
        ("cat ~/.ssh/id_rsa", "ask", "sensitive_host_read"),
        ("wget https://example.com -O /tmp/puddingclaw-fs-e2e/x", "ask", "network_access:wget"),
        ("npm install left-pad", "ask", "package_management"),
        ("eval 'touch /tmp/puddingclaw-fs-e2e/eval.txt'", "ask", "dynamic_shell_execution:eval"),
        ('printf "$PAYLOAD" | sh', "ask", "arbitrary_shell:sh"),
        ("sudo cp /tmp/a /tmp/b", "deny", "hard_denied_command:sudo"),
    ],
)
def test_smart_unrestricted_effect_policy_cannot_be_bypassed_by_shell_shape(
    tmp_path: Path,
    backend_mode: str,
    command: str,
    decision: str,
    reason: str,
) -> None:
    from harness.tool_execution import PolicyDecision

    workspace = tmp_path / "workspace"
    workspace.mkdir()

    result = _smart_pipeline(workspace, backend_mode)._preflight(_request(command, workspace))

    assert result.decision is PolicyDecision(decision), (command, result)
    assert result.reason == reason


def test_strict_mode_retains_conservative_complex_shell_gate(tmp_path: Path) -> None:
    from graph.permission_policy import RunPermissionContext
    from harness.tool_execution import PolicyDecision, ToolExecutionPipeline

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    context = RunPermissionContext.from_config_snapshot(
        {
            "permissions": {"approval_mode": "strict", "policy_epoch": 1},
            "execution": {
                "backend_mode": "spawn",
                "backend_id": "spawn:strict-matrix",
                "filesystem_mode": "restricted",
            },
        }
    )
    pipeline = ToolExecutionPipeline(
        known_tools={"execute"},
        backend_mode="spawn",
        permission_context=context,
    )

    result = pipeline._preflight(_request('echo "uid=$(id -u)"', workspace))

    assert result.decision is PolicyDecision.ASK
    assert result.reason == "complex_shell_expansion"


def test_smart_spawn_executes_compound_real_path_file_matrix(tmp_path: Path) -> None:
    from graph.permission_policy import RunPermissionContext
    from harness.tool_execution import PolicyDecision, ToolExecutionPipeline
    from harness.workspace_backends import SpawnWorkspaceBackend

    workspace = tmp_path / "workspace"
    scratch = tmp_path / "scratch"
    external = tmp_path / "external"
    for path in (workspace, scratch, external):
        path.mkdir()
    backend = SpawnWorkspaceBackend(root_dir=workspace, scratch_path=scratch)
    backend.filesystem_mode = "unrestricted"
    context = RunPermissionContext.from_config_snapshot(
        {
            "permissions": {"approval_mode": "smart", "policy_epoch": 1},
            "execution": {
                "backend_mode": "spawn",
                "backend_id": backend.id,
                "filesystem_mode": "unrestricted",
            },
        }
    )
    pipeline = ToolExecutionPipeline(
        known_tools={"execute"},
        backend_mode="spawn",
        permission_context=context,
        workspace_backend=backend,
    )
    source = external / "source.txt"
    copied = external / "copied.txt"
    moved = external / "moved.txt"
    command = (
        f"printf 'matrix-data' > {shlex.quote(str(source))} && "
        f"cp {shlex.quote(str(source))} {shlex.quote(str(copied))} && "
        f"mv {shlex.quote(str(copied))} {shlex.quote(str(moved))} && "
        f"chmod 600 {shlex.quote(str(moved))} && cmp {shlex.quote(str(source))} {shlex.quote(str(moved))}"
    )

    preflight = pipeline._preflight(_request(command, workspace))
    executed = backend.execute(command)

    assert preflight.decision is PolicyDecision.ALLOW
    assert executed.exit_code == 0, executed.output
    assert source.read_text(encoding="utf-8") == "matrix-data"
    assert moved.read_text(encoding="utf-8") == "matrix-data"
    assert not copied.exists()
    assert not (scratch / "tmp" / source.name).exists()


@pytest.mark.parametrize(
    "command",
    [
        "ln -s /tmp/source /tmp/link",
        "rsync -a /tmp/source /tmp/target",
        "patch /tmp/target /tmp/change.patch",
        "unlink /tmp/target",
        "tar -xf /tmp/archive.tar -C /tmp/output",
        "unzip /tmp/archive.zip -d /tmp/output",
        "node -e \"require('fs').writeFileSync('/tmp/output.txt', 'x')\"",
    ],
)
def test_ordinary_file_mutators_are_recorded_as_write_capabilities(command: str) -> None:
    from harness.tool_execution import ShellPolicyAnalyzer

    effects = ShellPolicyAnalyzer.capabilities(command)

    assert effects.workspace_write is True


def test_rsync_delete_and_remote_transport_keep_independent_effects() -> None:
    from harness.tool_execution import ShellPolicyAnalyzer

    effects = ShellPolicyAnalyzer.capabilities("rsync -a --delete /tmp/source/ user@example.com:/srv/target/")

    assert effects.workspace_write is True
    assert effects.destructive is True
    assert effects.network is True


def test_reading_ssh_config_is_not_misclassified_as_persistence_write(tmp_path: Path) -> None:
    from harness.tool_execution import PolicyDecision

    workspace = tmp_path / "workspace"
    workspace.mkdir()

    result = _smart_pipeline(workspace, "spawn")._preflight(_request("cat ~/.ssh/config", workspace))

    assert result.decision in {PolicyDecision.ALLOW, PolicyDecision.ASK}
    assert result.reason != "persistence_write"


def test_writing_credential_configuration_remains_an_effect_prompt(tmp_path: Path) -> None:
    from harness.tool_execution import PolicyDecision

    workspace = tmp_path / "workspace"
    workspace.mkdir()

    result = _smart_pipeline(workspace, "spawn")._preflight(
        _request("printf '[default]' > ~/.aws/credentials", workspace)
    )

    assert result.decision is PolicyDecision.ASK
    assert result.reason == "persistence_write"


def test_smart_router_preserves_raw_os_permission_error(tmp_path: Path) -> None:
    from langchain_core.messages import ToolMessage

    from graph.middlewares.workspace_path_router import WorkspacePathRouterMiddleware

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    raw_error = "Error: [Errno 13] Permission denied: '/skills/demo/SKILL.md'"
    request = ToolCallRequest(
        tool_call={
            "id": "smart-os-error",
            "name": "read_file",
            "args": {"file_path": "/skills/demo/SKILL.md"},
        },
        tool=None,
        state={},
        runtime=SimpleNamespace(context={"workspace_path": str(workspace)}),
    )

    result = WorkspacePathRouterMiddleware(approval_mode="smart").wrap_tool_call(
        request,
        lambda routed: ToolMessage(
            content=raw_error,
            name="read_file",
            tool_call_id=str(routed.tool_call["id"]),
            status="error",
        ),
    )

    assert result.status == "error"
    assert result.content == raw_error
    assert "managed_resource_unavailable" not in str(result.content)


@pytest.mark.parametrize("backend_mode", ["spawn", "kernel"])
@pytest.mark.parametrize(
    ("tool_name", "args"),
    [
        (
            "copy_file",
            {
                "source_path": "/tmp/puddingclaw-fs-e2e/source.txt",
                "target_path": "/tmp/puddingclaw-fs-e2e/copied.txt",
            },
        ),
        (
            "patch_files",
            {
                "files": [
                    {
                        "file_path": "/tmp/puddingclaw-fs-e2e/report.txt",
                        "old_string": "before",
                        "new_string": "after",
                    }
                ]
            },
        ),
    ],
)
def test_smart_permission_middleware_special_file_mutators_have_no_path_hitl(
    tmp_path: Path,
    monkeypatch,
    backend_mode: str,
    tool_name: str,
    args: dict,
) -> None:
    from langchain_core.messages import AIMessage

    import graph.permission_middleware as permission_middleware_module
    from graph.permission_middleware import ExternalFilePermissionMiddleware
    from graph.session_manager import session_manager

    session_manager.initialize(tmp_path)
    session_manager.create_session(f"smart-{backend_mode}-{tool_name}")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setattr(
        permission_middleware_module,
        "interrupt",
        lambda _payload: (_ for _ in ()).throw(
            AssertionError("ordinary Smart file mutation must not create path HITL")
        ),
    )
    state = {
        "messages": [
            AIMessage(
                content="",
                tool_calls=[{"name": tool_name, "args": args, "id": "special-mutator", "type": "tool_call"}],
            )
        ]
    }
    runtime = SimpleNamespace(
        context={
            "session_id": f"smart-{backend_mode}-{tool_name}",
            "query_id": "query-special-mutator",
            "workspace_path": str(workspace),
        }
    )

    result = ExternalFilePermissionMiddleware(
        backend_mode=backend_mode,
        approval_mode="smart",
    ).after_model(state, runtime)

    assert result is None


@pytest.mark.parametrize("backend_mode", ["spawn", "kernel"])
@pytest.mark.asyncio
async def test_smart_file_tool_sensitive_write_uses_effect_reason(
    tmp_path: Path,
    monkeypatch,
    backend_mode: str,
) -> None:
    from langchain_core.messages import AIMessage

    import graph.permission_middleware as permission_middleware_module
    from graph.permission_middleware import ExternalFilePermissionMiddleware
    from graph.session_manager import session_manager

    session_manager.initialize(tmp_path)
    session_manager.create_session(f"smart-sensitive-write-{backend_mode}")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    captured: dict = {}
    monkeypatch.setattr(
        permission_middleware_module,
        "interrupt",
        lambda payload: captured.update(payload),
    )
    target = tmp_path / ".ssh" / "authorized_keys"
    state = {
        "messages": [
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "write_file",
                        "args": {"file_path": str(target), "content": "key"},
                        "id": "sensitive-write",
                        "type": "tool_call",
                    }
                ],
            )
        ]
    }
    runtime = SimpleNamespace(
        context={
            "session_id": f"smart-sensitive-write-{backend_mode}",
            "query_id": "query-sensitive-write",
            "workspace_path": str(workspace),
        }
    )

    ExternalFilePermissionMiddleware(
        backend_mode=backend_mode,
        approval_mode="smart",
    ).after_model(state, runtime)

    request = captured["request"]
    assert request["type"] == "external_file_write"
    assert request["path"] == str(target.resolve())
    assert request["reason"] == "persistence_write"
    assert request["risk"] == "high"
    assert request["options"] == ["exact_file_session"]


@pytest.mark.asyncio
async def test_sensitive_file_request_cannot_expand_to_all_external_files(tmp_path: Path) -> None:
    from fastapi import HTTPException

    from api.permissions import ExternalFileGrantRequest, grant_external_file_permission
    from graph.permission_resume import permission_resume_registry
    from graph.session_manager import session_manager

    session_manager.initialize(tmp_path)
    session_manager.create_session("sensitive-grant-scope")
    target = tmp_path / ".ssh" / "id_rsa"
    target.parent.mkdir()
    target.write_text("secret", encoding="utf-8")
    request = permission_resume_registry.create_external_file_request(
        session_id="sensitive-grant-scope",
        query_id="query-sensitive-grant",
        tool_call_id="sensitive-grant",
        path=target,
        access="read",
        effect_reason="sensitive_host_read",
        effect_risk="high",
    )

    with pytest.raises(HTTPException, match="exact pending file"):
        await grant_external_file_permission(
            "sensitive-grant-scope",
            ExternalFileGrantRequest(
                target_kind="all_external_files",
                permission_request_id=request["id"],
            ),
        )
