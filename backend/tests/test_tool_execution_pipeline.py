"""Adversarial tests for managed terminal policy and workspace backends."""

from __future__ import annotations

import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest
from langchain.agents.middleware.types import ToolCallRequest

from graph.permission_policy import RunPermissionContext
from graph.permission_resume import PermissionResumeRegistry
from graph.session_manager import SessionManager, session_manager
from harness.dependency_setup import detect_workspace_dependency_plan
from harness.tool_execution import (
    PolicyDecision,
    ShellPolicyAnalyzer,
    ToolExecutionPipeline,
)
from harness.workspace_backends import (
    DEFAULT_SANDBOX_IMAGE,
    DockerWorkspaceBackend,
    ProjectSandboxManager,
    RestrictedHostWorkspaceBackend,
    build_workspace_execution_backend,
)


def test_run_permission_context_preserves_frozen_policy_version():
    context = RunPermissionContext.from_config_snapshot(
        {
            "permissions": {
                "approval_mode": "smart",
                "policy_epoch": 7,
                "policy_version": "tool-execution-v1",
            }
        }
    )

    assert context.policy_version == "tool-execution-v1"


def test_restricted_host_backend_id_is_stable_for_workspace(tmp_path):
    first = RestrictedHostWorkspaceBackend(root_dir=tmp_path)
    second = RestrictedHostWorkspaceBackend(root_dir=tmp_path)

    assert first.id == second.id


@pytest.mark.asyncio
async def test_tool_action_request_is_idempotent_across_graph_replay():
    registry = PermissionResumeRegistry()
    kwargs = {
        "session_id": "session-1",
        "query_id": "query-1",
        "run_id": "run-1",
        "tool_call_id": "call-1",
        "tool_name": "execute",
        "command": "python script.py",
        "reason": "arbitrary_interpreter:python",
        "risk": "execute",
    }

    first = registry.create_tool_action_request(**kwargs)
    second = registry.create_tool_action_request(**kwargs)

    assert second["id"] == first["id"]
    assert len(registry._pending) == 1
    assert registry.resolve(first["id"], {"type": "reject"})


@pytest.mark.asyncio
async def test_permission_interrupt_persists_waiting_and_resume_status(tmp_path, monkeypatch):
    from harness import tool_execution as tool_execution_module
    from harness.coordinators import HarnessRunCoordinator
    from harness.models import RunStatus

    session_manager.initialize(tmp_path)
    session_manager.create_session("session-hitl")
    coordinator = HarnessRunCoordinator(session_manager)
    run, _ = coordinator.start_run(
        session_id="session-hitl",
        query_id="query-hitl",
        objective="run python",
        goal_mode=False,
    )
    coordinator.bind_execution_snapshot(
        run,
        {
            "backend_mode": "restricted_host",
            "backend_id": "restricted-host:test",
            "workspace_id": "workspace:test",
        },
    )
    coordinator.transition(run, RunStatus.RUNNING)
    persisted = session_manager.get_run_state("session-hitl", run.run_id)
    assert persisted is not None
    context = RunPermissionContext.from_config_snapshot(persisted["config_snapshot"])
    pipeline = ToolExecutionPipeline(
        known_tools={"execute"},
        backend_mode="restricted_host",
        permission_context=context,
    )
    request = ToolCallRequest(
        tool_call={"id": "call-hitl", "name": "execute", "args": {"command": "python script.py"}},
        tool=None,
        state={},
        runtime=SimpleNamespace(
            context={
                "session_id": "session-hitl",
                "query_id": "query-hitl",
                "run_id": run.run_id,
                "workspace_path": str(tmp_path),
            }
        ),
    )
    observed_statuses: list[str] = []

    def fake_interrupt(payload):
        current = session_manager.get_run_state("session-hitl", run.run_id)
        observed_statuses.append(str(current["status"]))
        request_id = payload["request"]["id"]
        assert tool_execution_module.permission_resume_registry.resolve(
            request_id,
            {"type": "reject", "message": "no"},
        )
        return {"type": "reject", "message": "no"}

    monkeypatch.setattr(tool_execution_module, "interrupt", fake_interrupt)

    async def handler(_request):
        raise AssertionError("rejected action must not execute")

    result = await pipeline.awrap_tool_call(request, handler)

    assert observed_statuses == ["waiting_hitl"]
    assert session_manager.get_run_state("session-hitl", run.run_id)["status"] == "running"
    assert result.status == "error"


def test_docker_backend_rejects_spec_drift_within_run(tmp_path, monkeypatch):
    workspace = tmp_path / "project"
    workspace.mkdir()
    manager = ProjectSandboxManager({"network_enabled": False})
    specs = iter(
        [
            ("puddingclaw-test", "spec-before"),
            ("puddingclaw-test", "spec-after"),
        ]
    )
    monkeypatch.setattr(manager, "ensure_container", lambda _workspace: next(specs))
    backend = DockerWorkspaceBackend(root_dir=workspace, manager=manager)

    result = backend.execute("ls")

    assert result.exit_code == 1
    assert "specification changed after this Run started" in result.output


@pytest.mark.parametrize(
    ("command", "decision", "reason"),
    [
        ("ls -la", PolicyDecision.ALLOW, "safe_read"),
        ("git status --short", PolicyDecision.ALLOW, "safe_git_read"),
        ("pytest -q", PolicyDecision.ALLOW, "project_test"),
        ("python -m pytest -q", PolicyDecision.ALLOW, "project_test"),
        ("curl https://example.com", PolicyDecision.ASK, "network_access:curl"),
        ("pip install pandas", PolicyDecision.ASK, "package_management:pip"),
        (
            "python3 -m pip install pandas",
            PolicyDecision.ASK,
            "package_management:python3",
        ),
        ("npm ci", PolicyDecision.ASK, "package_management"),
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
    assert ShellPolicyAnalyzer.requires_network("npm ci") is True
    assert ShellPolicyAnalyzer.requires_network("npx playwright install") is True
    assert ShellPolicyAnalyzer.requires_network("uv sync --frozen") is True
    assert ShellPolicyAnalyzer.requires_network("uvx ruff check .") is True
    assert ShellPolicyAnalyzer.requires_network("git status") is False


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


def test_smart_mode_allows_only_controlled_network_tools(tmp_path):
    context = RunPermissionContext.from_config_snapshot(
        {
            "permissions": {
                "approval_mode": "smart",
                "policy_epoch": 4,
            },
            "execution": {
                "backend_mode": "docker",
                "backend_id": "docker:project:spec",
                "workspace_id": "sha256:workspace",
            },
        }
    )
    pipeline = ToolExecutionPipeline(
        known_tools={"fetch_url", "tavily_search", "execute"},
        backend_mode="docker",
        permission_context=context,
    )

    tavily = ToolCallRequest(
        tool_call={"id": "search", "name": "tavily_search", "args": {"query": "AI"}},
        tool=None,
        state={},
        runtime=SimpleNamespace(context={"workspace_path": str(tmp_path)}),
    )
    public_fetch = ToolCallRequest(
        tool_call={
            "id": "fetch",
            "name": "fetch_url",
            "args": {"url": "https://example.com/report?id=1"},
        },
        tool=None,
        state={},
        runtime=SimpleNamespace(context={"workspace_path": str(tmp_path)}),
    )
    private_fetch = ToolCallRequest(
        tool_call={
            "id": "private",
            "name": "fetch_url",
            "args": {"url": "http://127.0.0.1/admin"},
        },
        tool=None,
        state={},
        runtime=SimpleNamespace(context={"workspace_path": str(tmp_path)}),
    )
    shell_network = ToolCallRequest(
        tool_call={
            "id": "curl",
            "name": "execute",
            "args": {"command": "curl https://example.com"},
        },
        tool=None,
        state={},
        runtime=SimpleNamespace(context={"workspace_path": str(tmp_path)}),
    )

    assert pipeline._preflight(tavily).decision == PolicyDecision.ALLOW
    assert pipeline._preflight(public_fetch).decision == PolicyDecision.ALLOW
    assert pipeline._preflight(private_fetch).decision == PolicyDecision.DENY
    assert pipeline._preflight(shell_network).decision == PolicyDecision.ASK


@pytest.mark.parametrize(
    ("url", "target"),
    [
        ("https://Example.com/private?token=1", "https://example.com"),
        ("https://example.com:443/other", "https://example.com"),
        ("http://example.com:80/report", "http://example.com"),
        ("https://example.com:8443/report", "https://example.com:8443"),
        ("https://[2001:db8::1]/report", "https://[2001:db8::1]"),
    ],
)
def test_fetch_url_session_scope_uses_normalized_origin(tmp_path, url, target):
    request = ToolCallRequest(
        tool_call={
            "id": "call-1",
            "name": "fetch_url",
            "args": {"url": url},
        },
        tool=None,
        state={},
        runtime=SimpleNamespace(context={"workspace_path": str(tmp_path)}),
    )

    pipeline = ToolExecutionPipeline(
        known_tools={"fetch_url"},
        backend_mode="restricted_host",
    )
    scope = pipeline._session_grant_scope(request)

    assert scope is not None
    assert scope["target_kind"] == "network_origin"
    assert scope["target"] == target
    assert scope["label"] == f"本 Session 允许访问 {target}"


def test_search_session_scope_reuses_network_search_tool(tmp_path):
    request = ToolCallRequest(
        tool_call={
            "id": "call-1",
            "name": "tavily_search",
            "args": {"query": "first query"},
        },
        tool=None,
        state={},
        runtime=SimpleNamespace(context={"workspace_path": str(tmp_path)}),
    )

    pipeline = ToolExecutionPipeline(
        known_tools={"tavily_search"},
        backend_mode="restricted_host",
    )
    assert pipeline._session_grant_scope(request) == {
        "target_kind": "tool_name",
        "target": "tavily_search",
        "label": "本 Session 允许联网搜索",
    }


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


def test_network_origin_session_grant_reuses_paths_but_not_other_origins(tmp_path):
    sessions = SessionManager()
    sessions.initialize(tmp_path)
    sessions.create_session("session-1")
    sessions.add_permission_grant(
        "session-1",
        grant_type="tool_action",
        target_kind="network_origin",
        target="https://example.com",
        capabilities=["execute"],
        scope="session",
    )

    assert sessions.consume_tool_action_permission(
        "session-1",
        "sha256:first-path",
        session_target_kind="network_origin",
        session_target="https://example.com",
    )
    assert sessions.consume_tool_action_permission(
        "session-1",
        "sha256:second-path",
        session_target_kind="network_origin",
        session_target="https://example.com",
    )
    assert not sessions.consume_tool_action_permission(
        "session-1",
        "sha256:other-origin",
        session_target_kind="network_origin",
        session_target="https://api.example.com",
    )


def test_restricted_host_backend_has_sanitized_environment(tmp_path):
    workspace = tmp_path / "project with spaces"
    workspace.mkdir()
    backend = RestrictedHostWorkspaceBackend(root_dir=workspace)

    result = backend.execute(
        'printf \'%s\\n%s\' "$PWD" "$HOME"',
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


def test_dependency_plan_detects_monorepo_manifests_and_isolated_mounts(tmp_path):
    workspace = tmp_path / "project"
    backend = workspace / "backend"
    frontend = workspace / "frontend"
    backend.mkdir(parents=True)
    frontend.mkdir(parents=True)
    (backend / "pyproject.toml").write_text("[project]\nname='demo'\n", encoding="utf-8")
    (backend / "uv.lock").write_text("version = 1\n", encoding="utf-8")
    (frontend / "package.json").write_text('{"name":"demo"}', encoding="utf-8")
    (frontend / "package-lock.json").write_text('{"lockfileVersion":3}', encoding="utf-8")

    plan = detect_workspace_dependency_plan(workspace)

    assert plan is not None
    assert [item.command for item in plan.steps] == [
        "python3 -m pip install --user uv && uv sync --frozen",
        "npm ci",
    ]
    assert {(item.working_directory, item.target_name) for item in plan.runtime_mounts} == {
        ("backend", ".venv"),
        ("frontend", "node_modules"),
    }
    assert ("cd /workspace/backend && python3 -m pip install --user uv && uv sync --frozen") in plan.install_command
    assert "cd /workspace/frontend && npm ci" in plan.install_command
    assert plan.installed is False
    result = ShellPolicyAnalyzer(
        workspace_path=str(workspace),
        backend_mode="docker",
    ).analyze(plan.install_command)
    assert result.reason == "package_management:python3"
    assert result.risk == "package_install"

    marker = workspace / plan.marker_path.removeprefix("/workspace/")
    marker.parent.mkdir(parents=True)
    marker.write_text(plan.fingerprint, encoding="utf-8")
    installed = detect_workspace_dependency_plan(workspace)
    assert installed is not None
    assert installed.installed is True


def test_managed_sandbox_image_is_built_when_missing(monkeypatch):
    manager = ProjectSandboxManager({})
    calls: list[list[str]] = []
    built = False

    def fake_run(args, *, timeout=30):
        nonlocal built
        calls.append(list(args))
        if args[:2] == ["image", "inspect"]:
            return subprocess.CompletedProcess(
                args,
                0 if built else 1,
                "sha256:managed-image\n" if built else "",
                "" if built else "missing",
            )
        if args[0] == "build":
            built = True
        return subprocess.CompletedProcess(args, 0, "ok", "")

    monkeypatch.setattr(manager, "_run", fake_run)

    manager.ensure_image(DEFAULT_SANDBOX_IMAGE)

    build = next(args for args in calls if args[0] == "build")
    assert build[:4] == ["build", "--pull", "--tag", DEFAULT_SANDBOX_IMAGE]
    assert build[-1].endswith("/harness/docker")


def test_project_container_spec_has_no_docker_socket_or_host_home(tmp_path, monkeypatch):
    workspace = tmp_path / "project"
    workspace.mkdir()
    (workspace / "package.json").write_text('{"name":"demo"}', encoding="utf-8")
    (workspace / "package-lock.json").write_text('{"lockfileVersion":3}', encoding="utf-8")
    manager = ProjectSandboxManager(
        {
            "image": DEFAULT_SANDBOX_IMAGE,
            "network_enabled": False,
            "_managed_readonly_mounts": [
                {
                    "source": str(workspace),
                    "target": "/skills",
                }
            ],
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
    assert "dst=/home/puddingclaw" in joined
    assert f"src={workspace.resolve()},dst=/skills,readonly" in joined
    assert "PYTHONUSERBASE=/home/puddingclaw/.local" in joined
    assert "npm_config_prefix=/home/puddingclaw/.npm-global" in joined
    assert "dst=/workspace/node_modules" not in joined
    assert "HOME=/home/puddingclaw" in joined


def test_docker_backend_uses_ephemeral_container_for_approved_network_command(
    tmp_path,
    monkeypatch,
):
    workspace = tmp_path / "project"
    workspace.mkdir()
    manager = ProjectSandboxManager(
        {
            "network_enabled": False,
            "dependency_setup_enabled": False,
        }
    )
    calls: list[list[str]] = []
    monkeypatch.setattr(
        manager,
        "ensure_container",
        lambda _workspace: ("puddingclaw-test", "spec-hash"),
    )
    monkeypatch.setattr(
        manager,
        "ensure_image",
        lambda _image: "sha256:immutable-image",
    )

    def fake_run(args, *, timeout=30):
        calls.append(list(args))
        if args[0] == "run":
            return subprocess.CompletedProcess(args, 0, "installed", "")
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(manager, "_run", fake_run)
    backend = DockerWorkspaceBackend(root_dir=workspace, manager=manager)

    result = backend.execute("npm ci")

    assert result.exit_code == 0
    assert len(calls) == 1
    command = calls[0]
    assert command[:5] == ["run", "--rm", "--network", "bridge", "--read-only"]
    assert f"type=bind,src={workspace.resolve()},dst=/workspace,readonly" in command
    assert ["--entrypoint", "sh"] == command[command.index("--entrypoint") : command.index("--entrypoint") + 2]
    assert command[-3:] == ["sha256:immutable-image", "-c", "npm ci"]
    runtime_home = manager._runtime_home_volume_name(
        workspace.resolve(),
        image="sha256:immutable-image",
    )
    assert f"type=volume,src={runtime_home},dst=/home/puddingclaw" in command
    assert all("network connect" not in " ".join(call) for call in calls)


def test_smart_package_scope_never_applies_to_raw_shell(tmp_path):
    context = RunPermissionContext.from_config_snapshot(
        {
            "permissions": {"approval_mode": "smart", "policy_epoch": 2},
            "execution": {
                "backend_mode": "docker",
                "backend_id": "docker:project:spec",
                "workspace_id": "sha256:workspace",
            },
        }
    )
    pipeline = ToolExecutionPipeline(
        known_tools={"execute", "install_packages"},
        backend_mode="docker",
        permission_context=context,
    )
    typed = ToolCallRequest(
        tool_call={
            "id": "typed",
            "name": "install_packages",
            "args": {"ecosystem": "python", "packages": ["pandas==2.2.0"]},
        },
        tool=None,
        state={},
        runtime=SimpleNamespace(context={"workspace_path": str(tmp_path)}),
    )
    chained = ToolCallRequest(
        tool_call={
            "id": "raw",
            "name": "execute",
            "args": {"command": "pip install pandas && curl https://evil.example"},
        },
        tool=None,
        state={},
        runtime=SimpleNamespace(context={"workspace_path": str(tmp_path)}),
    )

    assert pipeline._preflight(typed).risk == "package_install"
    assert pipeline._session_grant_scope(typed) == {
        "target_kind": "capability",
        "target": "docker_package_install",
        "label": "本 Session 允许在隔离安装器中安装 Skill 依赖",
    }
    assert pipeline._session_grant_scope(chained) is None
    assert pipeline._required_capabilities(typed) == [
        "execute",
        "package_install",
        "temporary_network",
    ]


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
