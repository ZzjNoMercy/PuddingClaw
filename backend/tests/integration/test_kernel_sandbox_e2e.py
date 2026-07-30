from __future__ import annotations

import os
import socket
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from langchain.agents.middleware.types import ToolCallRequest
from langchain_core.messages import ToolMessage

from graph.permission_policy import RunPermissionContext, ShellDirectoryGrantSpec
from graph.session_manager import session_manager
from harness.coordinators import HarnessRunCoordinator
from harness.kernel_sandbox import MacOSSeatbeltRunner
from harness.models import RunStatus
from harness.sandbox_profiles import SandboxGrantProfile
from harness.tool_execution import ToolExecutionPipeline
from harness.workspace_backends import KernelWorkspaceBackend

pytestmark = pytest.mark.skipif(
    os.environ.get("PUDDINGCLAW_RUN_KERNEL_E2E") != "1" or sys.platform != "darwin",
    reason="set PUDDINGCLAW_RUN_KERNEL_E2E=1 on macOS to run real Seatbelt E2E",
)


def _runner(tmp_path: Path, *, external: Path | None = None) -> MacOSSeatbeltRunner:
    workspace = tmp_path / "workspace"
    scratch = tmp_path / "scratch"
    workspace.mkdir()
    scratch.mkdir()
    profile = SandboxGrantProfile.build(
        workspace_root=workspace,
        scratch_root=scratch,
        external_read_roots=([external] if external else []),
        external_write_roots=([external] if external else []),
        timeout_seconds=1,
    )
    return MacOSSeatbeltRunner(profile)


def test_seatbelt_allows_authorized_writes_and_denies_ungranted_reads(tmp_path: Path) -> None:
    external = tmp_path / "external"
    unauthorized = tmp_path / "unauthorized"
    external.mkdir()
    unauthorized.mkdir()
    (unauthorized / "secret.txt").write_text("TOP_SECRET_CONTENT", encoding="utf-8")
    runner = _runner(tmp_path, external=external)

    workspace_write = runner.execute("mkdir -p /workspace/output && echo ok > /workspace/output/a.txt")
    external_write = runner.execute(f"echo ok > {external / 'report.txt'}")
    unauthorized_read = runner.execute(f"cat {unauthorized / 'secret.txt'}")

    assert workspace_write.exit_code == 0
    assert external_write.exit_code == 0
    assert unauthorized_read.exit_code != 0
    assert "TOP_SECRET_CONTENT" not in unauthorized_read.output


def test_seatbelt_denies_raw_network(tmp_path: Path) -> None:
    runner = _runner(tmp_path)
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    listener.settimeout(1)
    port = listener.getsockname()[1]
    accepted: list[bool] = []

    def accept_once() -> None:
        try:
            connection, _address = listener.accept()
        except OSError:
            return
        accepted.append(True)
        connection.close()

    thread = threading.Thread(target=accept_once)
    thread.start()
    result = runner.execute(f"nc -z 127.0.0.1 {port}")
    thread.join(timeout=2)
    listener.close()

    assert result.exit_code != 0
    assert not accepted


def test_seatbelt_runs_standard_host_toolchains_without_docker(tmp_path: Path) -> None:
    runner = _runner(tmp_path)

    result = runner.execute("python3 --version && node --version && git --version")

    assert result.exit_code == 0
    assert "Python" in result.output
    assert "git version" in result.output


def test_seatbelt_timeout_kills_background_process_group(tmp_path: Path) -> None:
    runner = _runner(tmp_path)
    leaked = runner.profile.workspace_root / "leaked.txt"

    result = runner.execute("(sleep 2; echo leaked > /workspace/leaked.txt) & sleep 10")
    time.sleep(2.2)

    assert result.exit_code == 124
    assert not leaked.exists()


@pytest.mark.asyncio
async def test_tool_gate_permit_executes_canonical_compound_external_command_end_to_end(
    tmp_path: Path,
) -> None:
    state = tmp_path / "state"
    workspace = tmp_path / "workspace"
    scratch = tmp_path / "scratch"
    external = tmp_path / "external"
    external_alias = tmp_path / "external-alias"
    for path in (state, workspace, scratch, external):
        path.mkdir()
    external_alias.symlink_to(external, target_is_directory=True)
    source = external / "source.txt"
    target = external / "copy.txt"
    nested = external / "nested"
    source.write_bytes(b"kernel-e2e\n")
    session_manager.initialize(state)
    session_manager.create_session("kernel-e2e-session")
    coordinator = HarnessRunCoordinator(session_manager)
    run, _goal = coordinator.start_run(
        session_id="kernel-e2e-session",
        query_id="query-kernel-e2e",
        objective="copy an authorized external file",
        goal_mode=False,
        verification_enabled=False,
    )
    coordinator.bind_execution_snapshot(
        run,
        {
            "backend_mode": "kernel",
            "backend_id": "kernel:macos-seatbelt:e2e",
            "workspace_id": "workspace:kernel-e2e",
        },
    )
    coordinator.transition(run, RunStatus.RUNNING)
    run_state = session_manager.get_run_state("kernel-e2e-session", run.run_id)
    permission_context = RunPermissionContext.from_config_snapshot(
        run_state["config_snapshot"]
    )
    session_manager.add_shell_directory_grants_atomic(
        "kernel-e2e-session",
        grant_specs=[
            ShellDirectoryGrantSpec(target=str(external), access="read"),
            ShellDirectoryGrantSpec(target=str(external), access="write"),
        ],
        scope="run",
        run_id=run.run_id,
        bindings=permission_context.shell_grant_bindings(),
    )
    backend = KernelWorkspaceBackend(root_dir=workspace, scratch_path=scratch)
    pipeline = ToolExecutionPipeline(
        known_tools={"execute"},
        backend_mode="kernel",
        permission_context=permission_context,
        workspace_backend=backend,
    )
    command = (
        f"cp {external_alias / 'source.txt'} {external_alias / 'copy.txt'}"
        f" && mkdir -p {external_alias / 'nested'}"
    )
    request = ToolCallRequest(
        tool_call={
            "id": "call-kernel-external-cp",
            "name": "execute",
            "args": {"command": command},
        },
        tool=None,
        state={},
        runtime=SimpleNamespace(
            context={
                "session_id": "kernel-e2e-session",
                "query_id": run.query_id,
                "run_id": run.run_id,
                "workspace_path": str(workspace),
            }
        ),
    )

    async def handler(_request):
        result = backend.execute(command)
        return ToolMessage(
            content=result.output,
            name="execute",
            tool_call_id="call-kernel-external-cp",
            status="success" if result.exit_code == 0 else "error",
        )

    result = await pipeline.awrap_tool_call(request, handler)

    assert result.status == "success"
    assert target.read_bytes() == source.read_bytes()
    assert nested.is_dir()
    mutations = result.artifact["puddingclaw_shell_mutations"]
    assert {item["target_path"] for item in mutations} == {str(target), str(nested)}
    assert all(item["atomic"] is False for item in mutations)

    from harness.verification_activations import _result_evidence_refs

    refs = _result_evidence_refs(
        tool_call_id="call-kernel-external-cp",
        tool_name="execute",
        args={"command": command},
        result=result,
        session_id="kernel-e2e-session",
        run_id=run.run_id,
        query_id=run.query_id,
        workspace_path=str(workspace),
    )
    artifact = next(item for item in refs if item.get("kind") == "artifact_write")
    assert artifact["scope"] == "external"
    assert artifact["authorized"] is True
    assert artifact["host_path"] == str(target)
    target_mutation = next(item for item in mutations if item["target_path"] == str(target))
    assert artifact["content_sha256"] == target_mutation["after"]["content_sha256"]
