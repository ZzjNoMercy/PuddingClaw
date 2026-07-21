"""Adversarial tests for managed terminal policy and workspace backends."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest
from langchain.agents.middleware.types import ToolCallRequest

from graph.permission_policy import RunPermissionContext
from graph.permission_resume import PermissionResumeRegistry
from graph.session_manager import SessionManager, session_manager
from harness.dependency_setup import detect_workspace_dependency_plan
from harness.permission_reviewer import ModelPermissionReviewer, PermissionReviewVerdict
from harness.tool_execution import (
    PolicyDecision,
    ShellPolicyAnalyzer,
    ToolExecutionPipeline,
)
from harness.workspace_backends import (
    DEFAULT_SANDBOX_IMAGE,
    RUNTIME_CONTRACT,
    DockerWorkspaceBackend,
    ProjectSandboxManager,
    RestrictedHostWorkspaceBackend,
    _canonical_docker_mount_source,
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
        ("rm -rf build", PolicyDecision.ASK, "destructive_workspace_delete:rm_recursive"),
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


def test_restricted_host_policy_preserves_quoted_paths_with_spaces(tmp_path):
    workspace = tmp_path / "project with spaces"
    workspace.mkdir()
    inside = workspace / "report v2.html"
    inside.write_text("ok", encoding="utf-8")
    outside = tmp_path / "outside report.html"
    outside.write_text("no", encoding="utf-8")
    analyzer = ShellPolicyAnalyzer(
        workspace_path=str(workspace),
        backend_mode="restricted_host",
    )

    assert analyzer.analyze(f'cat "{inside}"').decision == PolicyDecision.ALLOW
    denied = analyzer.analyze(f'cat "{outside}"')
    assert denied.decision == PolicyDecision.DENY
    assert denied.reason == "host_filesystem_access"


def test_shell_policy_allows_virtual_scratch_but_denies_internal_mount(tmp_path):
    analyzer = ShellPolicyAnalyzer(
        workspace_path=str(tmp_path),
        backend_mode="restricted_host",
    )

    assert analyzer.analyze("cat /scratch/report.html").decision == PolicyDecision.ALLOW
    denied = analyzer.analyze("cat /harness-scratch/other-session/report.html")
    assert denied.decision == PolicyDecision.DENY
    assert denied.reason == "harness_internal_path_access"

    traversal = analyzer.analyze("cat /scratch/../../../../etc/passwd")
    assert traversal.decision == PolicyDecision.DENY
    assert traversal.reason == "scratch_path_traversal"


def test_docker_python_heredoc_with_data_arrays_is_not_path_expansion(tmp_path):
    analyzer = ShellPolicyAnalyzer(
        workspace_path=str(tmp_path),
        backend_mode="docker",
    )
    command = """python3 << 'PYEOF'
import json
data = {"categories": ["2020", "2021", "2026"]}
with open("/scratch/external/artifact-lease-1/report.js", "r") as handle:
    original = handle.read()
PYEOF"""

    result = analyzer.analyze(command)

    assert result.decision == PolicyDecision.ASK
    assert result.reason == "complex_shell_expansion"


def test_docker_inline_program_arrays_are_not_shell_path_expansion(tmp_path):
    analyzer = ShellPolicyAnalyzer(
        workspace_path=str(tmp_path),
        backend_mode="docker",
    )

    result = analyzer.analyze(
        'node -e "const years = [2024, 2025, 2026]; console.log(years.length)"'
    )

    assert result.reason != "container_path_expansion"


def test_docker_python_heredoc_cannot_glob_harness_private_scratch(tmp_path):
    analyzer = ShellPolicyAnalyzer(
        workspace_path=str(tmp_path),
        backend_mode="docker",
    )
    command = """python3 << 'PYEOF'
import glob
print(glob.glob('/harness-scrat[c]h/*'))
PYEOF"""

    result = analyzer.analyze(command)

    assert result.decision == PolicyDecision.DENY
    assert result.reason == "container_path_expansion"


def test_restricted_host_backend_rejects_scratch_traversal_before_rewrite(tmp_path):
    workspace = tmp_path / "workspace"
    scratch = tmp_path / "scratch"
    workspace.mkdir()
    scratch.mkdir()
    backend = RestrictedHostWorkspaceBackend(
        root_dir=workspace,
        scratch_path=scratch,
    )

    result = backend.execute("cat /scratch/../../../../etc/passwd")

    assert result.exit_code == 126
    assert "parent traversal" in result.output


def test_docker_runtime_validation_requires_exact_writable_scratch_mount(tmp_path, monkeypatch):
    workspace = tmp_path / "project"
    scratch = tmp_path / "scratch-project"
    workspace.mkdir()
    scratch.mkdir()
    manager = ProjectSandboxManager(
        {
            "image": DEFAULT_SANDBOX_IMAGE,
            "_managed_writable_mounts": [
                {"source": str(scratch), "target": "/harness-scratch"}
            ],
            "_scratch_relative": "session/query",
        }
    )
    spec = manager._spec(workspace)

    monkeypatch.setattr(
        manager,
        "_run",
        lambda args, **_kwargs: subprocess.CompletedProcess(
            args,
            0,
            json.dumps(
                [
                    {
                        "Mounts": [],
                        "Config": {"User": f"{os.getuid()}:{os.getgid()}"},
                    }
                ]
            ),
            "",
        ),
    )

    with pytest.raises(RuntimeError, match="writable mount contract mismatch"):
        manager._validate_runtime("sandbox", spec)


def test_docker_desktop_mount_source_normalizes_host_mnt_projection():
    assert _canonical_docker_mount_source(
        "/host_mnt/Users/pet/project/.puddingclaw/scratch"
    ) == "/Users/pet/project/.puddingclaw/scratch"


def test_restricted_host_backend_maps_virtual_scratch_outside_workspace(tmp_path):
    workspace = tmp_path / "workspace"
    scratch = tmp_path / "harness-scratch" / "session" / "query"
    workspace.mkdir()
    scratch.mkdir(parents=True)
    backend = RestrictedHostWorkspaceBackend(root_dir=workspace, scratch_path=scratch)

    result = backend.execute("printf scratch > /scratch/result.txt")

    assert result.exit_code == 0
    assert (scratch / "result.txt").read_text() == "scratch"
    assert not (workspace / "result.txt").exists()


def test_restricted_host_backend_maps_quoted_and_assigned_scratch_paths(tmp_path):
    workspace = tmp_path / "workspace"
    scratch = tmp_path / "harness-scratch" / "session" / "query"
    workspace.mkdir()
    scratch.mkdir(parents=True)
    backend = RestrictedHostWorkspaceBackend(root_dir=workspace, scratch_path=scratch)

    quoted = backend.execute("printf quoted > \"/scratch/quoted.txt\"")
    assigned = backend.execute("OUT=/scratch/assigned.txt; printf assigned > \"$OUT\"")

    assert quoted.exit_code == 0
    assert assigned.exit_code == 0
    assert (scratch / "quoted.txt").read_text() == "quoted"
    assert (scratch / "assigned.txt").read_text() == "assigned"


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


def test_docker_mode_denies_relative_harness_scratch_and_workspace_escape(tmp_path):
    analyzer = ShellPolicyAnalyzer(
        workspace_path=str(tmp_path),
        backend_mode="docker",
    )

    for command in (
        "find ../harness-scratch -type f",
        "cat ../harness-scratch/other-session/other-query/secret.txt",
        "cd ..",
        "cat ../../etc/passwd",
        "cd / && find harness-scrat[c]h -type f",
        "cd / && cat h*/other/query/x",
    ):
        result = analyzer.analyze(command)
        assert result.decision == PolicyDecision.DENY, command
        assert result.risk == "critical"


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
    assert result.reason == "missing_tool_control_descriptor:new_mutating_tool"


def test_attachment_lease_tools_are_internal_capabilities_not_host_write_grants(tmp_path):
    pipeline = ToolExecutionPipeline(
        known_tools={"prepare_attachment_edit", "publish_attachment"},
        backend_mode="docker",
    )
    for name, args in (
        ("prepare_attachment_edit", {"attachment_id": "att_source"}),
        (
            "publish_attachment",
            {
                "lease_id": "attachment-lease-1",
                "output_path": "/scratch/attachments/attachment-lease-1/result.html",
            },
        ),
    ):
        request = ToolCallRequest(
            tool_call={"id": f"call-{name}", "name": name, "args": args},
            tool=None,
            state={},
            runtime=SimpleNamespace(context={"workspace_path": str(tmp_path)}),
        )
        result = pipeline._preflight(request)
        assert result.decision == PolicyDecision.ALLOW


def test_external_directory_lease_tools_are_known_harness_capabilities(tmp_path):
    pipeline = ToolExecutionPipeline(
        known_tools=set(),
        backend_mode="docker",
    )
    for name, args in (
        ("stage_external_directory", {"directory_path": str(tmp_path)}),
        (
            "prepare_external_directory_commit",
            {"directory_path": str(tmp_path), "lease_id": "directory-lease-1"},
        ),
        (
            "commit_external_directory",
            {
                "directory_path": str(tmp_path),
                "lease_id": "directory-lease-1",
                "plan_digest": "sha256:plan",
            },
        ),
    ):
        request = ToolCallRequest(
            tool_call={"id": f"call-{name}", "name": name, "args": args},
            tool=None,
            state={},
            runtime=SimpleNamespace(context={"workspace_path": str(tmp_path)}),
        )
        result = pipeline._preflight(request)
        assert result.decision == PolicyDecision.ALLOW


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


@pytest.mark.parametrize("tool_name", ["fetch_url", "tavily_search"])
def test_network_tool_session_scope_opens_network_for_session(tmp_path, tool_name):
    request = ToolCallRequest(
        tool_call={
            "id": "call-1",
            "name": tool_name,
            "args": {"url": "https://example.com/report", "query": "AI news"},
        },
        tool=None,
        state={},
        runtime=SimpleNamespace(context={"workspace_path": str(tmp_path)}),
    )

    pipeline = ToolExecutionPipeline(
        known_tools={tool_name},
        backend_mode="restricted_host",
    )
    assert pipeline._session_grant_scope(request) == {
        "target_kind": "capability",
        "target": "session_network_access",
        "label": "本 Session 允许访问所有网络来源",
    }
    assert pipeline._required_capabilities(request) == ["execute", "network_access"]


def test_execute_curl_session_scope_opens_network_for_session(tmp_path):
    request = ToolCallRequest(
        tool_call={
            "id": "call-1",
            "name": "execute",
            "args": {"command": "curl -sS https://aihot.virxact.com/api/public/version"},
        },
        tool=None,
        state={},
        runtime=SimpleNamespace(context={"workspace_path": str(tmp_path)}),
    )

    pipeline = ToolExecutionPipeline(
        known_tools={"execute"},
        backend_mode="restricted_host",
    )
    assert pipeline._session_grant_scope(request) == {
        "target_kind": "capability",
        "target": "session_network_access",
        "label": "本 Session 允许访问所有网络来源",
    }


def test_curl_dev_null_probe_reuses_session_network_scope(tmp_path):
    command = 'curl -sS -o /dev/null -w "%{http_code}" --max-time 10 "https://example.com"'
    request = ToolCallRequest(
        tool_call={"id": "call-probe", "name": "execute", "args": {"command": command}},
        tool=None,
        state={},
        runtime=SimpleNamespace(context={"workspace_path": str(tmp_path)}),
    )
    pipeline = ToolExecutionPipeline(known_tools={"execute"}, backend_mode="docker")

    effects = ShellPolicyAnalyzer.capabilities(command)
    assert effects.network is True
    assert effects.workspace_write is False
    assert pipeline._required_capabilities(request) == ["execute", "network_access"]
    assert pipeline._session_grant_scope(request) == {
        "target_kind": "capability",
        "target": "session_network_access",
        "label": "本 Session 允许访问所有网络来源",
    }


@pytest.mark.parametrize(
    "command",
    [
        "curl -sS -o report.json https://example.com/report",
        "pip install requests",
        "git clone https://example.com/repo.git",
    ],
)
def test_session_network_scope_does_not_absorb_other_capabilities(tmp_path, command):
    request = ToolCallRequest(
        tool_call={"id": "call-1", "name": "execute", "args": {"command": command}},
        tool=None,
        state={},
        runtime=SimpleNamespace(context={"workspace_path": str(tmp_path)}),
    )
    pipeline = ToolExecutionPipeline(known_tools={"execute"}, backend_mode="restricted_host")

    assert pipeline._session_grant_scope(request) is None


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
    assert sessions.list_permission_grants("session-1") == []
    history = sessions.list_permission_grant_history("session-1")
    assert len(history) == 1
    assert history[0]["scope"] == "once"
    assert history[0]["consumed_at"] == history[0]["revoked_at"]


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


def test_equivalent_session_tool_grants_are_deduplicated(tmp_path):
    sessions = SessionManager()
    sessions.initialize(tmp_path)
    sessions.create_session("session-dedup")
    kwargs = {
        "grant_type": "tool_action",
        "target_kind": "capability",
        "target": "workspace_commands",
        "capabilities": ["execute", "managed_write"],
        "scope": "session",
    }

    first = sessions.add_permission_grant("session-dedup", **kwargs)
    second = sessions.add_permission_grant(
        "session-dedup",
        **kwargs,
        metadata={"policy_source": "codex_grok_smart_reviewer"},
    )

    assert second["id"] == first["id"]
    assert len(sessions.list_permission_grants("session-dedup")) == 1
    assert second["metadata"]["policy_source"] == "codex_grok_smart_reviewer"


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


def test_session_network_grant_reuses_different_tools_and_sources(tmp_path):
    sessions = SessionManager()
    sessions.initialize(tmp_path)
    sessions.create_session("session-network")
    sessions.add_permission_grant(
        "session-network",
        grant_type="tool_action",
        target_kind="capability",
        target="session_network_access",
        capabilities=["execute", "network_access"],
        scope="session",
    )

    for fingerprint in ("sha256:curl-aihot", "sha256:fetch-other", "sha256:search"):
        assert sessions.consume_tool_action_permission(
            "session-network",
            fingerprint,
            session_target_kind="capability",
            session_target="session_network_access",
            required_capabilities=["execute", "network_access"],
        )
    assert not sessions.consume_tool_action_permission(
        "session-network",
        "sha256:package-install",
        session_target_kind="capability",
        session_target="session_network_access",
        required_capabilities=["execute", "network_access", "package_install"],
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


def test_managed_sandbox_runtime_declares_curl_as_base_capability():
    dockerfile = Path(__file__).parents[1] / "harness" / "docker" / "Dockerfile"
    content = dockerfile.read_text(encoding="utf-8")

    assert 'com.puddingclaw.runtime="python3.12-node22-curl-v3"' in content
    assert "curl" in content
    assert "curl" in RUNTIME_CONTRACT


def test_project_container_spec_has_no_docker_socket_or_host_home(tmp_path, monkeypatch):
    workspace = tmp_path / "project"
    workspace.mkdir()
    skills = workspace / "backend" / "skills"
    skills.mkdir(parents=True)
    (workspace / "package.json").write_text('{"name":"demo"}', encoding="utf-8")
    (workspace / "package-lock.json").write_text('{"lockfileVersion":3}', encoding="utf-8")
    manager = ProjectSandboxManager(
        {
            "image": DEFAULT_SANDBOX_IMAGE,
            "network_enabled": False,
            "_managed_readonly_mounts": [
                {
                    "source": str(skills),
                    "target": "/skills",
                }
            ],
        }
    )
    calls: list[list[str]] = []

    def fake_run(args, *, timeout=30):
        calls.append(list(args))
        if args[0] == "inspect":
            if len(args) == 2:
                return subprocess.CompletedProcess(
                    args,
                    0,
                    json.dumps(
                        [
                            {
                                "Mounts": [],
                                "Config": {"User": f"{os.getuid()}:{os.getgid()}"},
                            }
                        ]
                    ),
                    "",
                )
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
    assert f"src={skills.resolve()},dst=/skills,readonly" in joined
    assert f"src={skills.resolve()},dst=/workspace/backend/skills,readonly" in joined
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
    assert f"type=bind,src={workspace.resolve()},dst=/workspace" in command
    assert ["--entrypoint", "sh"] == command[command.index("--entrypoint") : command.index("--entrypoint") + 2]
    assert command[-3:] == ["sha256:immutable-image", "-c", "npm ci"]
    runtime_home = manager._runtime_home_volume_name(
        workspace.resolve(),
        image="sha256:immutable-image",
    )
    assert f"type=volume,src={runtime_home},dst=/home/puddingclaw" in command
    assert all("network connect" not in " ".join(call) for call in calls)


def test_docker_backend_gives_approved_python_network_command_real_network(
    tmp_path,
    monkeypatch,
):
    workspace = tmp_path / "project"
    workspace.mkdir()
    skills = tmp_path / "skills"
    skills.mkdir()
    manager = ProjectSandboxManager(
        {
            "network_enabled": False,
            "dependency_setup_enabled": False,
            "_managed_readonly_mounts": [
                {"source": str(skills), "target": "/skills"},
            ],
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
        return subprocess.CompletedProcess(args, 0, "remote ok", "")

    monkeypatch.setattr(manager, "_run", fake_run)
    backend = DockerWorkspaceBackend(root_dir=workspace, manager=manager)

    result = backend.execute(
        'python3 -c "import urllib.request; '
        "urllib.request.urlopen('https://aihot.virxact.com/aihot-skill/SKILL.md')\""
    )

    assert result.exit_code == 0
    command = calls[0]
    assert command[:5] == ["run", "--rm", "--network", "bridge", "--read-only"]
    assert f"type=bind,src={workspace.resolve()},dst=/workspace,readonly" in command
    assert f"type=bind,src={skills.resolve()},dst=/skills,readonly" in command


@pytest.mark.parametrize(
    "command",
    [
        "python3 -c \"import urllib.request; urllib.request.urlopen('https://example.com')\"",
        "python3 -c \"import http.client; http.client.HTTPSConnection('example.com')\"",
        "python3 /skills/aihot/scripts/aihot_query.py --user-query latest",
        "python3 -u /skills/aihot/scripts/aihot_query.py --user-query latest",
        "node -e \"fetch('https://example.com')\"",
        "sh -c \"curl https://example.com\"",
        "git -C repo pull",
        "npm --prefix app install",
        "python3 -m pip --disable-pip-version-check install requests",
    ],
)
def test_embedded_network_clients_require_network_capability(tmp_path, command):
    request = ToolCallRequest(
        tool_call={
            "id": "network-script",
            "name": "execute",
            "args": {"command": command},
        },
        tool=None,
        state={},
        runtime=SimpleNamespace(context={"workspace_path": str(tmp_path)}),
    )
    pipeline = ToolExecutionPipeline(
        known_tools={"execute"},
        backend_mode="docker",
    )

    assert ShellPolicyAnalyzer.requires_network(command) is True
    assert {"execute", "network_access"}.issubset(
        set(pipeline._required_capabilities(request))
    )


def test_local_skill_hash_does_not_request_network_capability(tmp_path):
    command = (
        'python3 -c "import hashlib; '
        "print(hashlib.sha256(open('/skills/aihot/SKILL.md', 'rb').read()).hexdigest())\""
    )
    request = ToolCallRequest(
        tool_call={
            "id": "local-hash",
            "name": "execute",
            "args": {"command": command},
        },
        tool=None,
        state={},
        runtime=SimpleNamespace(context={"workspace_path": str(tmp_path)}),
    )
    pipeline = ToolExecutionPipeline(
        known_tools={"execute"},
        backend_mode="docker",
    )

    assert ShellPolicyAnalyzer.requires_network(command) is False
    assert pipeline._required_capabilities(request) == ["execute"]


def test_safe_read_containing_url_does_not_silently_gain_network(tmp_path):
    command = "echo https://example.com"
    analyzer = ShellPolicyAnalyzer(
        workspace_path=str(tmp_path),
        backend_mode="docker",
    )

    assert analyzer.analyze(command).decision == PolicyDecision.ALLOW
    assert analyzer.capabilities(command).network is False


def test_read_only_package_inspection_does_not_gain_network():
    assert ShellPolicyAnalyzer.capabilities("pip list").network is False
    assert ShellPolicyAnalyzer.capabilities("pip show requests").package_install is False


@pytest.mark.parametrize(
    ("command", "required"),
    [
        ("timeout 10 curl https://example.com", {"network"}),
        ("nice -n 5 curl https://example.com", {"network"}),
        (
            "PYTHONUNBUFFERED=1 python3 /skills/aihot/scripts/aihot_query.py --limit 10",
            {"network", "workspace_write"},
        ),
        ("find . -delete", {"workspace_write"}),
        ("find . -exec curl https://example.com {} +", {"network", "workspace_write"}),
        ("sed -i s/a/b/ report.txt", {"workspace_write"}),
        ("sort -o sorted.txt input.txt", {"workspace_write"}),
        ("uniq input.txt output.txt", {"workspace_write"}),
        ("git fetch origin", {"network", "workspace_write"}),
        ("curl --output=update.zip https://example.com/update.zip", {"network", "workspace_write"}),
        ("curl -OJ https://example.com/update.zip", {"network", "workspace_write"}),
        (
            "python3 -c \"import urllib.request; urllib.request.urlretrieve('https://example.com/a','a')\"",
            {"network", "workspace_write"},
        ),
        ("python3 script.py", {"workspace_write"}),
    ],
)
def test_shell_capabilities_never_understate_known_effects(command, required):
    effects = ShellPolicyAnalyzer.capabilities(command)
    enabled = {
        name
        for name in ("network", "workspace_write", "package_install")
        if getattr(effects, name)
    }

    assert required.issubset(enabled)


def test_input_redirection_is_not_mislabeled_as_workspace_write():
    assert ShellPolicyAnalyzer.capabilities("wc -l < input.txt").workspace_write is False


def test_allowed_project_test_with_remote_url_requires_network_approval(tmp_path):
    command = "pytest --base-url https://example.com"
    request = ToolCallRequest(
        tool_call={"id": "remote-test", "name": "execute", "args": {"command": command}},
        tool=None,
        state={},
        runtime=SimpleNamespace(context={"workspace_path": str(tmp_path)}),
    )
    pipeline = ToolExecutionPipeline(known_tools={"execute"}, backend_mode="docker")

    result = pipeline._preflight(request)
    assert result.decision == PolicyDecision.ASK
    assert result.reason == "network_access:embedded_command"
    assert pipeline._required_capabilities(request) == ["execute", "network_access"]


class _FakePermissionReviewer:
    def __init__(self, verdict: PermissionReviewVerdict) -> None:
        self.verdict = verdict
        self.calls: list[dict[str, object]] = []

    async def review(self, **kwargs):
        self.calls.append(kwargs)
        return self.verdict


def _smart_docker_pipeline(
    tmp_path: Path,
    *,
    reviewer=None,
) -> ToolExecutionPipeline:
    context = RunPermissionContext.from_config_snapshot(
        {
            "permissions": {"approval_mode": "smart", "policy_epoch": 1},
            "execution": {
                "backend_mode": "docker",
                "backend_id": "docker:project:spec",
                "workspace_id": "sha256:workspace",
            },
        }
    )
    return ToolExecutionPipeline(
        known_tools={"execute"},
        backend_mode="docker",
        permission_context=context,
        reviewer=reviewer,
    )


@pytest.mark.parametrize(
    "command",
    [
        "python3 compute_data.py",
        "node --check product-config-charts.js",
        "sed -i s/2024/2026/g report.html",
        "cp source.js generated.js",
        "cd /workspace && python3 -c \"\\nwith open('report.html', 'w') as f:\\n    f.write('ok')\\n\"",
        "cd /workspace && python3 -c \"import subprocess; subprocess.run(['node', '--check', 'report.js'])\"",
        "python3 << 'PYEOF'\nimport json\ndata = {\"categories\": [\"2020\", \"2026\"]}\nwith open('/scratch/external/lease/report.js', 'w') as f:\n    f.write(json.dumps(data))\nPYEOF",
    ],
)
def test_smart_docker_auto_approves_ordinary_workspace_execution(tmp_path, command):
    pipeline = _smart_docker_pipeline(tmp_path)
    request = ToolCallRequest(
        tool_call={"id": "smart", "name": "execute", "args": {"command": command}},
        tool=None,
        state={},
        runtime=SimpleNamespace(context={"workspace_path": str(tmp_path)}),
    )

    result = pipeline._preflight(request)

    assert result.decision == PolicyDecision.ALLOW
    assert result.reason.startswith("smart_docker_workspace_")


@pytest.mark.parametrize(
    "command",
    [
        "rm -rf build",
        "find . -delete",
        "git reset --hard HEAD~1",
        "python3 -c \"import shutil; shutil.rmtree('build')\"",
        "node -e \"require('fs').rmSync('build', {recursive: true})\"",
        "echo $(cat secret)",
    ],
)
def test_smart_docker_still_asks_for_destructive_or_opaque_execution(tmp_path, command):
    pipeline = _smart_docker_pipeline(tmp_path)
    request = ToolCallRequest(
        tool_call={"id": "smart-risk", "name": "execute", "args": {"command": command}},
        tool=None,
        state={},
        runtime=SimpleNamespace(context={"workspace_path": str(tmp_path)}),
    )

    assert pipeline._preflight(request).decision == PolicyDecision.ASK


@pytest.mark.parametrize(
    "command",
    [
        "pip install pandas",
        "python3 -c \"import urllib.request; urllib.request.urlopen('https://example.com')\"",
    ],
)
def test_smart_docker_still_asks_for_package_or_network_execution(tmp_path, command):
    pipeline = _smart_docker_pipeline(tmp_path)
    request = ToolCallRequest(
        tool_call={"id": "smart-network", "name": "execute", "args": {"command": command}},
        tool=None,
        state={},
        runtime=SimpleNamespace(context={"workspace_path": str(tmp_path)}),
    )

    assert pipeline._preflight(request).decision == PolicyDecision.ASK


@pytest.mark.parametrize(
    "command",
    [
        "git add report.html",
        "git commit -m refresh-report",
        "git switch main",
        "git stash push -m before-refresh",
        "mv report.tmp report.html",
        "rm report.tmp",
    ],
)
def test_smart_docker_allows_reversible_project_mutations(tmp_path, command):
    pipeline = _smart_docker_pipeline(tmp_path)
    request = ToolCallRequest(
        tool_call={"id": "smart-mutation", "name": "execute", "args": {"command": command}},
        tool=None,
        state={},
        runtime=SimpleNamespace(context={"workspace_path": str(tmp_path)}),
    )

    result = pipeline._preflight(request)

    assert result.decision == PolicyDecision.ALLOW
    assert result.risk == "managed_write"


@pytest.mark.parametrize(
    "command",
    [
        "rm --recursive build",
        "git checkout -- report.html",
        "git stash drop",
        "mv /workspace/report.html /etc/report.html",
        "bash missing-script.sh",
    ],
)
def test_smart_docker_keeps_irreversible_or_uninspectable_actions_gated(tmp_path, command):
    pipeline = _smart_docker_pipeline(tmp_path)
    request = ToolCallRequest(
        tool_call={"id": "smart-gated", "name": "execute", "args": {"command": command}},
        tool=None,
        state={},
        runtime=SimpleNamespace(context={"workspace_path": str(tmp_path)}),
    )

    assert pipeline._preflight(request).decision in {PolicyDecision.ASK, PolicyDecision.DENY}


def test_shell_script_is_inspected_before_smart_mode_allows_it(tmp_path):
    script = tmp_path / "refresh.sh"
    script.write_text("#!/bin/sh\nprintf 'ok'\n", encoding="utf-8")
    strict = ShellPolicyAnalyzer(workspace_path=str(tmp_path), backend_mode="docker")
    smart = _smart_docker_pipeline(tmp_path)
    request = ToolCallRequest(
        tool_call={"id": "script", "name": "execute", "args": {"command": "bash refresh.sh"}},
        tool=None,
        state={},
        runtime=SimpleNamespace(context={"workspace_path": str(tmp_path)}),
    )

    assert strict.analyze("bash refresh.sh").reason == "inspected_shell_script:bash"
    assert strict.analyze("bash refresh.sh").decision == PolicyDecision.ASK
    assert smart._preflight(request).decision == PolicyDecision.ALLOW


def test_shell_script_with_recursive_delete_is_not_auto_approved(tmp_path):
    script = tmp_path / "cleanup.sh"
    script.write_text("#!/bin/sh\nrm -rf build\n", encoding="utf-8")
    pipeline = _smart_docker_pipeline(tmp_path)
    request = ToolCallRequest(
        tool_call={"id": "script-delete", "name": "execute", "args": {"command": "bash cleanup.sh"}},
        tool=None,
        state={},
        runtime=SimpleNamespace(context={"workspace_path": str(tmp_path)}),
    )

    result = pipeline._preflight(request)

    assert result.decision == PolicyDecision.ASK
    assert result.reason == "destructive_workspace_delete:rm_recursive"


@pytest.mark.asyncio
async def test_smart_gray_zone_uses_reviewer_instead_of_silent_allow(tmp_path):
    reviewer = _FakePermissionReviewer(
        PermissionReviewVerdict(
            decision="allow",
            risk="low",
            explanation="命令仅在项目边界内生成可逆产物。",
        )
    )
    pipeline = _smart_docker_pipeline(tmp_path, reviewer=reviewer)
    request = ToolCallRequest(
        tool_call={"id": "review", "name": "execute", "args": {"command": "custom-formatter report.html"}},
        tool=None,
        state={},
        runtime=SimpleNamespace(context={"workspace_path": str(tmp_path)}),
    )

    result = await pipeline._apreflight(request)

    assert result.decision == PolicyDecision.ALLOW
    assert result.source == "codex_grok_smart_reviewer"
    assert len(reviewer.calls) == 1


@pytest.mark.asyncio
async def test_smart_reviewer_never_sees_known_destructive_action(tmp_path):
    reviewer = _FakePermissionReviewer(
        PermissionReviewVerdict(decision="allow", risk="low", explanation="allow")
    )
    pipeline = _smart_docker_pipeline(tmp_path, reviewer=reviewer)
    request = ToolCallRequest(
        tool_call={"id": "review-delete", "name": "execute", "args": {"command": "rm -rf build"}},
        tool=None,
        state={},
        runtime=SimpleNamespace(context={"workspace_path": str(tmp_path)}),
    )

    result = await pipeline._apreflight(request)

    assert result.decision == PolicyDecision.ASK
    assert reviewer.calls == []


@pytest.mark.asyncio
async def test_smart_reviewer_receives_inspected_shell_body(tmp_path):
    script = tmp_path / "wrapped.sh"
    script.write_text("#!/bin/sh\nset -e\nprintf ok\n", encoding="utf-8")
    reviewer = _FakePermissionReviewer(
        PermissionReviewVerdict(decision="ask", risk="high", explanation="需要确认脚本语义。")
    )
    pipeline = _smart_docker_pipeline(tmp_path, reviewer=reviewer)
    request = ToolCallRequest(
        tool_call={"id": "script-review", "name": "execute", "args": {"command": "bash wrapped.sh"}},
        tool=None,
        state={},
        runtime=SimpleNamespace(context={"workspace_path": str(tmp_path)}),
    )

    await pipeline._apreflight(request)

    assert len(reviewer.calls) == 1
    assert "set -e" in str(reviewer.calls[0]["action"])


@pytest.mark.asyncio
async def test_model_permission_reviewer_is_structured_and_fail_closed():
    class _Model:
        async def ainvoke(self, _messages):
            return SimpleNamespace(
                content='```json\n{"decision":"allow","risk":"low","explanation":"仅格式化项目文件"}\n```'
            )

    verdict = await ModelPermissionReviewer(_Model()).review(
        tool_name="execute",
        action="formatter report.html",
        deterministic_reason="unknown_command:formatter",
        deterministic_risk="high",
        context={"backend_mode": "docker", "workspace_path": "/workspace"},
        capabilities={"network": False, "workspace_write": True, "package_install": False, "destructive": False},
    )

    assert verdict.decision == "allow"
    assert verdict.explanation == "仅格式化项目文件"

    class _BrokenModel:
        async def ainvoke(self, _messages):
            raise RuntimeError("offline")

    fallback = await ModelPermissionReviewer(_BrokenModel()).review(
        tool_name="execute",
        action="formatter report.html",
        deterministic_reason="unknown_command:formatter",
        deterministic_risk="high",
        context={"backend_mode": "docker"},
        capabilities={"network": False, "workspace_write": True, "package_install": False, "destructive": False},
    )

    assert fallback.decision == "ask"


def test_network_download_declares_write_and_network_capabilities(tmp_path):
    command = "curl -o update.zip https://example.com/update.zip"
    request = ToolCallRequest(
        tool_call={"id": "download", "name": "execute", "args": {"command": command}},
        tool=None,
        state={},
        runtime=SimpleNamespace(context={"workspace_path": str(tmp_path)}),
    )
    pipeline = ToolExecutionPipeline(known_tools={"execute"}, backend_mode="docker")

    assert pipeline._required_capabilities(request) == [
        "execute",
        "network_access",
        "managed_write",
    ]


def test_once_tool_grant_is_bound_to_originating_run(tmp_path):
    sessions = SessionManager()
    sessions.initialize(tmp_path)
    sessions.create_session("session-run-bound")
    sessions.add_permission_grant(
        "session-run-bound",
        grant_type="tool_action",
        target_kind="fingerprint",
        target="sha256:run-bound",
        capabilities=["execute"],
        scope="once",
        metadata={"run_id": "run-a"},
    )

    assert not sessions.consume_tool_action_permission(
        "session-run-bound",
        "sha256:run-bound",
        current_run_id="run-b",
    )
    assert sessions.consume_tool_action_permission(
        "session-run-bound",
        "sha256:run-bound",
        current_run_id="run-a",
    )


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
