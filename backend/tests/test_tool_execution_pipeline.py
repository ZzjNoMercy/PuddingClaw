"""Adversarial tests for managed terminal policy and workspace backends."""

from __future__ import annotations

import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest
from langchain.agents.middleware.types import ToolCallRequest

from graph.permission_resume import PermissionResumeRegistry
from graph.session_manager import SessionManager
from harness.tool_execution import (
    PolicyDecision,
    ShellPolicyAnalyzer,
    ToolExecutionPipeline,
)
from harness.workspace_backends import (
    ProjectSandboxManager,
    RestrictedHostWorkspaceBackend,
    build_workspace_execution_backend,
)


@pytest.mark.parametrize(
    ("command", "decision", "reason"),
    [
        ("ls -la", PolicyDecision.ALLOW, "safe_read"),
        ("git status --short", PolicyDecision.ALLOW, "safe_git_read"),
        ("pytest -q", PolicyDecision.ALLOW, "project_test"),
        ("python -m pytest -q", PolicyDecision.ALLOW, "project_test"),
        ("curl https://example.com", PolicyDecision.ASK, "network_access:curl"),
        ("pip install pandas", PolicyDecision.ASK, "package_management:pip"),
        ("rm -rf build", PolicyDecision.ASK, "managed_workspace_write:rm"),
        ("python script.py", PolicyDecision.ASK, "arbitrary_interpreter:python"),
        ("sudo ls", PolicyDecision.DENY, "hard_denied_command:sudo"),
        ("docker ps", PolicyDecision.DENY, "hard_denied_command:docker"),
        ("cat /etc/passwd", PolicyDecision.DENY, "host_filesystem_access"),
        ("cat ../../etc/passwd", PolicyDecision.DENY, "host_filesystem_access"),
        ("find . -delete", PolicyDecision.ASK, "managed_workspace_write:find:-delete"),
        ("find . -exec sudo id ;", PolicyDecision.DENY, "hard_denied_command:sudo"),
        ("rg --pre 'python filter.py' term", PolicyDecision.ASK, "external_command_hook:rg"),
        ("git diff --ext-diff", PolicyDecision.ASK, "external_command_hook:git"),
        ("sort -o output.txt input.txt", PolicyDecision.ASK, "managed_workspace_write:sort"),
        ("uniq input.txt output.txt", PolicyDecision.ASK, "managed_workspace_write:uniq"),
    ],
)
def test_restricted_host_shell_policy(command, decision, reason, tmp_path):
    result = ShellPolicyAnalyzer(
        workspace_path=str(tmp_path),
        backend_mode="restricted_host",
    ).analyze(command)

    assert result.decision == decision
    assert result.reason == reason


def test_shell_chain_uses_strictest_segment(tmp_path):
    analyzer = ShellPolicyAnalyzer(
        workspace_path=str(tmp_path),
        backend_mode="restricted_host",
    )

    assert analyzer.analyze("ls && sudo id").decision == PolicyDecision.DENY
    assert analyzer.analyze("ls && curl https://example.com").decision == PolicyDecision.ASK
    assert analyzer.analyze("ls > output.txt").reason == "shell_redirection"
    assert analyzer.analyze("echo $(cat secret)").reason == "complex_shell_expansion"


def test_restricted_host_denies_symlink_escape_for_reads_and_new_writes(tmp_path):
    workspace = tmp_path / "workspace"
    outside = tmp_path / "outside"
    workspace.mkdir()
    outside.mkdir()
    (outside / "secret.txt").write_text("secret", encoding="utf-8")
    (workspace / "escape").symlink_to(outside, target_is_directory=True)
    analyzer = ShellPolicyAnalyzer(
        workspace_path=str(workspace),
        backend_mode="restricted_host",
    )

    assert analyzer.analyze("cat escape/secret.txt").decision == PolicyDecision.DENY
    assert analyzer.analyze("touch escape/new.txt").decision == PolicyDecision.DENY
    assert analyzer.analyze("cat --file=escape/secret.txt").decision == PolicyDecision.DENY
    assert analyzer.analyze("./escape/tool").decision == PolicyDecision.DENY


def test_docker_mode_does_not_treat_container_as_authorization(tmp_path):
    analyzer = ShellPolicyAnalyzer(
        workspace_path=str(tmp_path),
        backend_mode="docker",
    )

    assert analyzer.analyze("curl https://example.com").decision == PolicyDecision.ASK
    assert analyzer.analyze("sudo ls").decision == PolicyDecision.DENY
    # Container-local /etc is not the host filesystem, but the command still
    # passes through deterministic command classification.
    assert analyzer.analyze("cat /etc/os-release").decision == PolicyDecision.ALLOW


def test_unknown_tool_fails_closed(tmp_path):
    request = ToolCallRequest(
        tool_call={"id": "call-1", "name": "forged_tool", "args": {}},
        tool=None,
        state={},
        runtime=SimpleNamespace(context={"workspace_path": str(tmp_path)}),
    )
    result = ToolExecutionPipeline(
        known_tools={"read_resource"},
        backend_mode="restricted_host",
    )._preflight(request)

    assert result.decision == PolicyDecision.DENY
    assert result.reason == "unknown_tool:forged_tool"


def test_registered_but_unclassified_tool_fails_closed(tmp_path):
    request = ToolCallRequest(
        tool_call={"id": "call-1", "name": "new_mutating_tool", "args": {}},
        tool=None,
        state={},
        runtime=SimpleNamespace(context={"workspace_path": str(tmp_path)}),
    )
    result = ToolExecutionPipeline(
        known_tools={"new_mutating_tool"},
        backend_mode="restricted_host",
    )._preflight(request)

    assert result.decision == PolicyDecision.DENY
    assert result.reason == "unclassified_tool:new_mutating_tool"


def test_network_tool_requires_hitl_and_fingerprints_arguments(tmp_path):
    request = ToolCallRequest(
        tool_call={
            "id": "call-1",
            "name": "fetch_url",
            "args": {"url": "https://example.com/private"},
        },
        tool=None,
        state={},
        runtime=SimpleNamespace(context={"workspace_path": str(tmp_path)}),
    )
    pipeline = ToolExecutionPipeline(
        known_tools={"fetch_url"},
        backend_mode="restricted_host",
    )

    result = pipeline._preflight(request)

    assert result.decision == PolicyDecision.ASK
    assert result.reason == "network_access:fetch_url"
    assert "example.com/private" in pipeline._action_preview(request)


def test_tool_action_fingerprint_preserves_semantic_whitespace():
    first = PermissionResumeRegistry.tool_action_fingerprint(
        tool_name="execute",
        command="printf 'a  b'",
        reason="unknown_command:printf",
    )
    second = PermissionResumeRegistry.tool_action_fingerprint(
        tool_name="execute",
        command="printf 'a b'",
        reason="unknown_command:printf",
    )

    assert first != second


def test_once_tool_grant_is_consumed_atomically(tmp_path):
    sessions = SessionManager()
    sessions.initialize(tmp_path)
    sessions.create_session("session-1")
    fingerprint = PermissionResumeRegistry.tool_action_fingerprint(
        tool_name="execute",
        command="rm build.txt",
        reason="managed_workspace_write:rm",
    )
    sessions.add_permission_grant(
        "session-1",
        grant_type="tool_action",
        target_kind="fingerprint",
        target=fingerprint,
        capabilities=["execute"],
        scope="once",
    )

    assert sessions.consume_tool_action_permission("session-1", fingerprint) is True
    assert sessions.consume_tool_action_permission("session-1", fingerprint) is False


def test_session_tool_grant_remains_available(tmp_path):
    sessions = SessionManager()
    sessions.initialize(tmp_path)
    sessions.create_session("session-1")
    fingerprint = PermissionResumeRegistry.tool_action_fingerprint(
        tool_name="execute",
        command="npm install",
        reason="package_management",
    )
    sessions.add_permission_grant(
        "session-1",
        grant_type="tool_action",
        target_kind="fingerprint",
        target=fingerprint,
        capabilities=["execute"],
        scope="session",
    )

    assert sessions.consume_tool_action_permission("session-1", fingerprint) is True
    assert sessions.consume_tool_action_permission("session-1", fingerprint) is True


def test_restricted_host_backend_has_sanitized_environment(tmp_path):
    workspace = tmp_path / "project with spaces"
    workspace.mkdir()
    backend = RestrictedHostWorkspaceBackend(root_dir=workspace)

    result = backend.execute(
        "printf '%s\\n%s' \"$PWD\" \"$HOME\"",
    )

    assert result.exit_code == 0
    lines = result.output.splitlines()
    assert lines[0] == str(workspace)
    assert lines[1].startswith(str(workspace / ".puddingclaw" / "runtime"))


def test_docker_unavailable_falls_back_to_controlled_host(tmp_path, monkeypatch):
    monkeypatch.setattr(
        ProjectSandboxManager,
        "probe",
        lambda self: (False, "daemon unavailable"),
    )

    selection = build_workspace_execution_backend(
        tmp_path,
        {
            "docker_enabled": True,
            "on_unavailable": "fallback",
            "docker": {},
        },
    )

    assert selection.mode == "restricted_host"
    assert selection.fallback_reason == "daemon unavailable"
    assert isinstance(selection.backend, RestrictedHostWorkspaceBackend)


def test_docker_unavailable_can_fail_closed(tmp_path, monkeypatch):
    monkeypatch.setattr(
        ProjectSandboxManager,
        "probe",
        lambda self: (False, "daemon unavailable"),
    )

    with pytest.raises(RuntimeError, match="daemon unavailable"):
        build_workspace_execution_backend(
            tmp_path,
            {
                "docker_enabled": True,
                "on_unavailable": "deny",
                "docker": {},
            },
        )


def test_project_container_spec_has_no_docker_socket_or_host_home(tmp_path, monkeypatch):
    workspace = tmp_path / "project"
    workspace.mkdir()
    manager = ProjectSandboxManager(
        {
            "image": "python:3.12-slim",
            "network_enabled": False,
        }
    )
    calls: list[list[str]] = []

    def fake_run(args, *, timeout=30):
        calls.append(list(args))
        if args[0] == "inspect":
            return subprocess.CompletedProcess(args, 1, "", "not found")
        return subprocess.CompletedProcess(args, 0, "ok", "")

    monkeypatch.setattr(manager, "_run", fake_run)

    manager.ensure_container(workspace)

    create = next(args for args in calls if args[0] == "create")
    joined = " ".join(create)
    assert f"src={workspace.resolve()},dst=/workspace" in joined
    assert "/var/run/docker.sock" not in joined
    assert str(Path.home()) not in joined
    assert "--network none" in joined
    assert "--cap-drop ALL" in joined
    assert "no-new-privileges" in joined


def test_project_container_idle_stop_uses_generation_guard(monkeypatch):
    manager = ProjectSandboxManager({"idle_stop_minutes": 30})
    calls: list[list[str]] = []

    def fake_run(args, *, timeout=30):
        calls.append(list(args))
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(manager, "_run", fake_run)
    manager._idle_timers.clear()

    manager._stop_if_current_generation("project-1", "stale")

    assert calls == []


def test_project_container_idle_stop_stops_current_generation(monkeypatch):
    manager = ProjectSandboxManager({"idle_stop_minutes": 30})
    calls: list[list[str]] = []

    def fake_run(args, *, timeout=30):
        calls.append(list(args))
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(manager, "_run", fake_run)
    manager._idle_timers["project-1"] = ("current", SimpleNamespace(cancel=lambda: None))

    manager._stop_if_current_generation("project-1", "current")

    assert calls == [["stop", "--time", "10", "project-1"]]
