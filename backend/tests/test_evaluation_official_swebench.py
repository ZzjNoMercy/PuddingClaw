import asyncio
import io
import json
import os
import subprocess
import tarfile
from pathlib import Path
from types import SimpleNamespace

import pytest
from deepagents.backends.protocol import ExecuteResponse, SandboxBackendProtocol
from langchain.agents.middleware.types import ToolCallRequest

import evaluation.official_swebench as official_module
import evaluation.runner as runner_module
import evaluation.swebench_agent_backend as agent_backend_module
from evaluation.code_eval import _initialize_control_repository, _run_repository_git
from evaluation.contracts import (
    AgentRunEnvelope,
    EvalExperiment,
    ExperimentCandidate,
    ExperimentStatus,
    TraceEvidence,
)
from evaluation.evaluators import evaluator_registry
from evaluation.official_swebench import ProcessResult, _read_report, run_official_swebench_harness
from evaluation.repository import EvaluationRepository
from evaluation.runner import EvaluationRunner
from evaluation.settings import LangSmithSettings
from evaluation.swebench_adapter import swebench_dataset_from_rows
from evaluation.swebench_agent_backend import _instance_payload
from evaluation.worker_manager import EvaluationWorkerManager
from graph.permission_policy import RunPermissionContext
from harness.execution_context import bind_authorized_execution
from harness.tool_execution import ToolExecutionPipeline


def _dataset():
    return swebench_dataset_from_rows(
        [
            {
                "instance_id": "pytest-dev__pytest-1234",
                "repo": "pytest-dev/pytest",
                "base_commit": "b" * 40,
                "problem_statement": "Fix collection",
                "version": "8.0",
                "test_patch": "diff --git a/testing/test_x.py b/testing/test_x.py",
                "FAIL_TO_PASS": "[]",
                "PASS_TO_PASS": "[]",
            }
        ],
        name="SWE managed verifier",
    )


def _experiment(dataset, *, status=ExperimentStatus.RUNNING):
    return EvalExperiment(
        name="SWE experiment",
        dataset_id=dataset.dataset_id,
        dataset_version=1,
        dataset_version_id="dsv_test",
        dataset_content_hash="content-hash",
        candidate=ExperimentCandidate(name="model"),
        profile_id="coding_agent@1",
        status=status,
    )


def _envelopes(experiment, dataset, attempt_id="attempt_test"):
    case = dataset.cases[0]
    return {
        case.case_id: [
            {
                **AgentRunEnvelope(
                    case_id=case.case_id,
                    experiment_id=experiment.experiment_id,
                    candidate_id=experiment.candidate.candidate_id,
                    session_id="session",
                    response="patched",
                    metadata={
                        "code_verification": {
                            "mode": "swebench",
                            "status": "not_evaluated",
                            "patch": "diff --git a/a.py b/a.py",
                            "patch_sha256": "patch-hash",
                            "changed_paths": ["a.py"],
                        }
                    },
                ).model_dump(mode="json"),
                "_attempt_id": attempt_id,
                "_attempt_status": "completed",
            }
        ]
    }


def test_swebench_agent_environment_uses_frozen_reference_without_gold_patch():
    case = _dataset().cases[0]

    payload = _instance_payload(case)

    assert payload["instance_id"] == "pytest-dev__pytest-1234"
    assert payload["base_commit"] == "b" * 40
    assert payload["test_patch"].startswith("diff --git")
    assert "patch" not in payload


@pytest.mark.asyncio
async def test_run_process_accepts_localized_process_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    class ManagedProcess:
        pid = 43210
        returncode = 0

        def __init__(self):
            self.stdout = asyncio.StreamReader()
            self.stdout.feed_eof()

        async def wait(self):
            return 0

    class IdentityProcess:
        returncode = 0

        async def communicate(self):
            return "星期四 8月 13 12:34:56 2026\n".encode(), b""

    processes = iter((ManagedProcess(), IdentityProcess()))
    receipt: dict[str, object] = {}

    async def fake_create_subprocess_exec(*_args, **_kwargs):
        return next(processes)

    def capture_receipt(source: Path, _target: Path):
        receipt.update(json.loads(Path(source).read_text(encoding="utf-8")))
        Path(source).unlink()

    monkeypatch.setattr(official_module.asyncio, "create_subprocess_exec", fake_create_subprocess_exec)
    monkeypatch.setattr(official_module.os, "replace", capture_receipt)
    pid_path = tmp_path / "candidate-image.pid"

    result = await official_module._run_process(
        ["python", "-m", "evaluation.swebench_agent_backend"],
        cwd=tmp_path,
        environment={"PUDDINGCLAW_SWEBENCH_RUN_ID": "puddingclaw-exp_test"},
        timeout_seconds=5,
        log_path=tmp_path / "process.log",
        isolate_process_group=True,
        pid_path=pid_path,
    )

    assert result.exit_code == 0
    assert receipt["process_started_at"].startswith("星期四")


def test_swebench_backend_keeps_base_commit_independent_of_official_testspec(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    payload = _instance_payload(_dataset().cases[0])
    spec = agent_backend_module._make_test_spec(payload)
    assert not hasattr(spec, "base_commit")
    monkeypatch.setattr(
        agent_backend_module.SWEbenchAgentWorkspaceBackend,
        "_start",
        lambda _self: None,
    )
    workspace = tmp_path / "workspace"
    scratch = tmp_path / "scratch"
    workspace.mkdir()
    backend = agent_backend_module.SWEbenchAgentWorkspaceBackend(
        workspace_path=workspace,
        scratch_path=scratch,
        test_spec=spec,
        base_commit=payload["base_commit"],
        experiment_id="exp_test",
    )

    assert backend.base_commit == "b" * 40
    first_binding = backend.kernel_runner_binding_digest
    second = agent_backend_module.SWEbenchAgentWorkspaceBackend(
        workspace_path=workspace,
        scratch_path=scratch,
        test_spec=spec,
        base_commit="c" * 40,
        experiment_id="exp_test",
    )
    assert second.kernel_runner_binding_digest != first_binding


def test_instance_image_preparation_accepts_official_threadpool_payloads(
    monkeypatch: pytest.MonkeyPatch,
):
    import docker
    from docker.errors import ImageNotFound
    from swebench.harness import docker_build

    payload = _instance_payload(_dataset().cases[0])
    spec = agent_backend_module._make_test_spec(payload)

    class Images:
        calls = 0

        def get(self, image_key):
            assert image_key == spec.instance_image_key
            self.calls += 1
            if self.calls == 1:
                raise ImageNotFound("not built yet")
            return SimpleNamespace(id="sha256:built")

    class Client:
        images = Images()

        def close(self):
            return None

    client = Client()
    monkeypatch.setattr(docker, "from_env", lambda **_kwargs: client)

    def fake_build(_client, dataset, **_kwargs):
        assert dataset == [spec]
        # This is the real 4.1 shape: run_threadpool returns input payload
        # tuples, whose TestSpec element is deliberately unhashable.
        return [(spec, _client, None, False)], []

    monkeypatch.setattr(docker_build, "build_instance_images", fake_build)
    monkeypatch.setattr(agent_backend_module, "_docker_backend_is_approved", lambda: True)
    monkeypatch.setattr(agent_backend_module, "_namespace", lambda: "none")

    agent_backend_module.ensure_swebench_instance_image_payload(payload)

    assert client.images.calls == 2


def test_swebench_execute_consumes_real_tool_gate_permit_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    workspace = tmp_path / "workspace"
    scratch = tmp_path / "scratch"
    workspace.mkdir()
    scratch.mkdir()
    backend = agent_backend_module.SWEbenchAgentWorkspaceBackend.__new__(
        agent_backend_module.SWEbenchAgentWorkspaceBackend
    )
    backend.workspace_path = workspace.resolve()
    backend.scratch_path = scratch.resolve()
    backend.test_spec = SimpleNamespace(instance_id="pytest-dev__pytest-1234")
    backend._base_commit = "b" * 40
    backend.experiment_id = "exp_test"
    backend._container = None
    backend._container_id = "container-test"
    backend._image_id = "sha256:image-test"

    pipeline = ToolExecutionPipeline(
        known_tools={"execute"},
        backend_mode="kernel",
        permission_context=RunPermissionContext.from_config_snapshot(
            {
                "permissions": {"approval_mode": "smart", "policy_epoch": 1},
                "execution": {"backend_mode": "kernel", "backend_id": backend.id},
            }
        ),
        workspace_backend=backend,
    )
    command = "python -V"
    request = ToolCallRequest(
        tool_call={"id": "call-swebench", "name": "execute", "args": {"command": command}},
        tool=None,
        state={},
        runtime=SimpleNamespace(context={"workspace_path": str(workspace)}),
    )
    authorized = pipeline._compile_kernel_execution(request)
    assert authorized is not None
    assert authorized.permit.selected_runner == "kernel_swebench_docker"

    def fake_execute_raw(_command, *, timeout=None, spawn_guard=None):
        del timeout
        allowed = bool(spawn_guard and spawn_guard())
        return ExecuteResponse(output="ok" if allowed else "replayed", exit_code=0 if allowed else 126)

    monkeypatch.setattr(backend, "_execute_raw", fake_execute_raw)
    monkeypatch.setattr(backend, "_sync_host_to_container", lambda: None)
    with bind_authorized_execution(authorized):
        assert backend.execute(command).exit_code == 0
        assert backend.execute(command).exit_code == 126


def test_swebench_agent_shell_workspace_has_hard_layer_quota_not_host_rw_bind(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    import docker

    workspace = tmp_path / "workspace"
    scratch = tmp_path / "scratch"
    workspace.mkdir()
    scratch.mkdir()
    (tmp_path / ".workspace-evaluation-git").mkdir()
    captured = {}

    class Container:
        id = "container-test"

        def start(self):
            return None

    class Client:
        images = SimpleNamespace(get=lambda _key: SimpleNamespace(id="sha256:image-test"))

        class Containers:
            def create(self, _image, **kwargs):
                captured.update(kwargs)
                return Container()

        containers = Containers()

        def close(self):
            return None

    backend = agent_backend_module.SWEbenchAgentWorkspaceBackend.__new__(
        agent_backend_module.SWEbenchAgentWorkspaceBackend
    )
    backend.workspace_path = workspace
    backend.scratch_path = scratch
    backend.test_spec = SimpleNamespace(
        instance_id="pytest-dev__pytest-1234",
        instance_image_key="sweb.eval.fixture",
        platform="linux/amd64",
    )
    backend._base_commit = "b" * 40
    backend.experiment_id = "exp_test"
    backend._container = None
    backend._container_id = None
    backend._image_id = None
    monkeypatch.setattr(docker, "from_env", lambda: Client())
    monkeypatch.setattr(backend, "_sync_host_to_container", lambda: None)
    monkeypatch.setattr(
        backend,
        "_execute_raw",
        lambda *_args, **_kwargs: ExecuteResponse(output="ok", exit_code=0),
    )

    backend._start()

    assert captured["storage_opt"] == {"size": "2G"}
    assert captured["tmpfs"]["/scratch"].endswith("size=512m")
    assert captured["mounts"] == []
    assert captured["cap_drop"] == ["ALL"]
    assert captured["cap_add"] == ["CHOWN"]
    assert captured["environment"]["HOME"] == "/scratch/home"
    assert captured["environment"]["CONDA_NO_PLUGINS"] == "true"


@pytest.mark.asyncio
async def test_image_prepare_pid_receipt_survives_transient_inspection_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    pid_path = tmp_path / "candidate-image.pid"
    pid_path.write_text(
        json.dumps(
            {
                "pid": os.getpid(),
                "run_id": "puddingclaw-exp_test",
                "process_started_at": "fixture",
            }
        ),
        encoding="ascii",
    )

    async def fail_inspect(*_args, **_kwargs):
        raise TimeoutError("transient ps timeout")

    monkeypatch.setattr("evaluation.worker_manager.asyncio.create_subprocess_exec", fail_inspect)
    await EvaluationWorkerManager._terminate_managed_process(
        "exp_test",
        pid_path=pid_path,
        command_markers=("evaluation.swebench_agent_backend",),
    )

    assert pid_path.exists()


def test_candidate_archive_is_rejected_during_stream_before_unbounded_extract(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    archive_bytes = io.BytesIO()
    with tarfile.open(fileobj=archive_bytes, mode="w") as archive:
        member = tarfile.TarInfo("large.bin")
        member.size = 8
        archive.addfile(member, io.BytesIO(b"12345678"))
    archive_bytes.seek(0)

    class Process:
        stdout = archive_bytes

        def wait(self, timeout=None):
            del timeout
            return 0

        def terminate(self):
            return None

        def kill(self):
            return None

    backend = agent_backend_module.SWEbenchAgentWorkspaceBackend.__new__(
        agent_backend_module.SWEbenchAgentWorkspaceBackend
    )
    backend._container_id = "container-test"
    target = tmp_path / "extract"
    target.mkdir()
    monkeypatch.setattr(agent_backend_module.shutil, "which", lambda _name: "/usr/bin/docker")
    monkeypatch.setattr(agent_backend_module.subprocess, "Popen", lambda *_args, **_kwargs: Process())

    with pytest.raises(RuntimeError, match="materialization budget"):
        backend._stream_container_tree("/testbed", target, max_bytes=4, max_files=10)


def test_candidate_archive_rebases_internal_absolute_symlink(tmp_path: Path):
    member = tarfile.TarInfo("./home/.config/astropy")
    member.type = tarfile.SYMTYPE
    member.linkname = "/scratch/home/.astropy/config"

    agent_backend_module.SWEbenchAgentWorkspaceBackend._validate_archive_member(
        member,
        source="/scratch",
    )

    assert member.linkname == "../.astropy/config"


def test_candidate_archive_rejects_absolute_symlink_outside_export_root():
    member = tarfile.TarInfo("./home/.config/astropy")
    member.type = tarfile.SYMTYPE
    member.linkname = "/etc/passwd"

    with pytest.raises(RuntimeError, match="escaping link"):
        agent_backend_module.SWEbenchAgentWorkspaceBackend._validate_archive_member(
            member,
            source="/scratch",
        )


@pytest.mark.asyncio
async def test_image_prepare_pid_receipt_survives_command_or_pgid_mismatch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    pid = os.getpid()
    started_at = "Mon Aug 13 12:34:56 2026"
    pid_path = tmp_path / "candidate-image.pid"
    pid_path.write_text(
        json.dumps(
            {
                "pid": pid,
                "run_id": "puddingclaw-exp_test",
                "process_started_at": started_at,
            }
        ),
        encoding="ascii",
    )

    class Inspect:
        returncode = 0

        async def communicate(self):
            return f"{pid} {pid + 1} {started_at} truncated-command\n".encode(), b""

    async def fake_inspect(*_args, **_kwargs):
        return Inspect()

    monkeypatch.setattr("evaluation.worker_manager.asyncio.create_subprocess_exec", fake_inspect)
    await EvaluationWorkerManager._terminate_managed_process(
        "exp_test",
        pid_path=pid_path,
        command_markers=("evaluation.swebench_agent_backend",),
    )

    assert pid_path.exists()


@pytest.mark.skipif(
    os.getenv("PUDDINGCLAW_RUN_SWEBENCH_DOCKER_SMOKE") != "1",
    reason="opt-in real Docker smoke",
)
def test_real_astropy_candidate_container_full_start_execute_sync_cleanup(tmp_path: Path):
    import docker

    image = "sweb.eval.arm64.astropy__astropy-12907:latest"
    base_commit = "d09ad3939c02713f0af5f9d5ba3dfd2e8319d9e6"
    workspace = tmp_path / "workspace"
    scratch = tmp_path / "scratch"
    workspace.mkdir()
    scratch.mkdir()
    client = docker.from_env()
    donor = client.containers.create(image)
    backend = None
    try:
        copied = subprocess.run(
            ["docker", "cp", f"{donor.id}:/testbed/.", str(workspace)],
            capture_output=True,
            check=False,
            timeout=180,
        )
        assert copied.returncode == 0, copied.stderr.decode(errors="replace")
        _initialize_control_repository(workspace)
        _run_repository_git(workspace, "add", "--all")
        _run_repository_git(workspace, "commit", "--quiet", "-m", "smoke baseline")
        backend = agent_backend_module.SWEbenchAgentWorkspaceBackend(
            workspace_path=workspace,
            scratch_path=scratch,
            test_spec=SimpleNamespace(
                instance_id="astropy__astropy-12907",
                instance_image_key=image,
                platform="linux/arm64",
            ),
            base_commit=base_commit,
            experiment_id="exp_docker_smoke",
        )
        assert isinstance(backend, SandboxBackendProtocol)
        (workspace / "SMOKE_FILE_TOOL.txt").write_text("file-tool-ok", encoding="utf-8")
        command = (
            'python -c "import astropy; from pathlib import Path; '
            "Path('/workspace/SMOKE_SHELL.txt').write_text('shell-ok'); "
            "print('probe-ok', astropy.__version__)\""
        )
        pipeline = ToolExecutionPipeline(
            known_tools={"execute"},
            backend_mode="kernel",
            permission_context=RunPermissionContext.from_config_snapshot(
                {
                    "permissions": {"approval_mode": "smart", "policy_epoch": 1},
                    "execution": {"backend_mode": "kernel", "backend_id": backend.id},
                }
            ),
            workspace_backend=backend,
        )
        request = ToolCallRequest(
            tool_call={"id": "call-real-smoke", "name": "execute", "args": {"command": command}},
            tool=None,
            state={},
            runtime=SimpleNamespace(context={"workspace_path": str(workspace)}),
        )
        authorized = pipeline._compile_kernel_execution(request)
        assert authorized is not None
        with bind_authorized_execution(authorized):
            response = backend.execute(command, timeout=120)
        assert response.exit_code == 0, response.output
        assert "probe-ok" in response.output
        assert (workspace / "SMOKE_FILE_TOOL.txt").read_text() == "file-tool-ok"
        assert (workspace / "SMOKE_SHELL.txt").read_text() == "shell-ok"
        astropy_config = scratch / "home" / ".config" / "astropy"
        assert astropy_config.is_symlink()
        assert astropy_config.resolve(strict=False).is_relative_to(scratch.resolve())
    finally:
        if backend is not None:
            backend.close()
        donor.remove(force=True)
        client.close()


@pytest.mark.asyncio
async def test_swebench_case_runs_agent_in_prepared_official_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    dataset = _dataset()
    repository = EvaluationRepository(tmp_path / "evaluation.sqlite3")
    draft = repository.create_dataset(dataset)
    bundle = repository.publish_dataset(draft.dataset_id, draft.revision)
    experiment = repository.create_experiment(
        _experiment(bundle.dataset).model_copy(
            update={
                "dataset_id": draft.dataset_id,
                "dataset_version_id": bundle.version_id,
                "dataset_content_hash": bundle.checksum,
            }
        )
    )
    runtime_root = tmp_path / "runtime"
    monkeypatch.setenv("PUDDINGCLAW_HOME", str(tmp_path / "home"))
    runner = EvaluationRunner(repository, LangSmithSettings(enabled=False), tmp_path)
    runner._initialize_isolated_runtime(runtime_root)

    workspace = runtime_root / "workspace"
    workspace.mkdir(parents=True)
    monkeypatch.setattr(
        runner,
        "_prepare_workspace",
        lambda _root, _case, _repetition: (workspace, "project-swebench"),
    )

    class PreparedBackend:
        closed = False

        def close(self):
            self.closed = True

    prepared = PreparedBackend()
    async def fake_prepare_backend(*args, **kwargs):
        return prepared

    monkeypatch.setattr(
        agent_backend_module,
        "prepare_swebench_agent_backend",
        fake_prepare_backend,
    )

    from graph.deepagents_manager import deepagents_agent_manager

    async def fake_astream(**kwargs):
        assert kwargs["evaluation_workspace_backend"] is prepared
        assert "patch_file" in kwargs["evaluation_required_toolset"]
        yield {"event": "done", "data": '{"content":"implemented"}'}

    monkeypatch.setattr(deepagents_agent_manager, "astream", fake_astream)
    monkeypatch.setattr(
        runner_module,
        "verify_code_case",
        lambda *_args, **_kwargs: {
            "mode": "swebench",
            "status": "not_evaluated",
            "passed": None,
            "reason": "awaiting official verifier",
            "patch": "diff --git a/a.py b/a.py",
            "patch_sha256": "patch-hash",
            "changed_paths": ["a.py"],
            "commands": [],
        },
    )

    result = await runner._run_case(experiment, bundle.dataset.cases[0], 0, runtime_root)

    assert result["attempt_status"] == "completed"
    assert prepared.closed is True


@pytest.mark.asyncio
async def test_swebench_deadline_submits_existing_patch_to_official_verifier(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    dataset = _dataset()
    repository = EvaluationRepository(tmp_path / "evaluation.sqlite3")
    draft = repository.create_dataset(dataset)
    bundle = repository.publish_dataset(draft.dataset_id, draft.revision)
    base = _experiment(bundle.dataset)
    experiment = repository.create_experiment(
        base.model_copy(
            update={
                "dataset_id": draft.dataset_id,
                "dataset_version_id": bundle.version_id,
                "dataset_content_hash": bundle.checksum,
                "execution": base.execution.model_copy(update={"timeout_seconds": 1}),
            }
        )
    )
    runtime_root = tmp_path / "runtime"
    runner = EvaluationRunner(repository, LangSmithSettings(enabled=False), tmp_path)
    runner._initialize_isolated_runtime(runtime_root)
    workspace = runtime_root / "workspace"
    workspace.mkdir(parents=True)
    monkeypatch.setattr(
        runner,
        "_prepare_workspace",
        lambda _root, _case, _repetition: (workspace, "project-swebench"),
    )

    class PreparedBackend:
        closed = False

        def close(self):
            self.closed = True

    prepared = PreparedBackend()

    async def fake_prepare_backend(*_args, **_kwargs):
        return prepared

    monkeypatch.setattr(agent_backend_module, "prepare_swebench_agent_backend", fake_prepare_backend)

    from graph.deepagents_manager import deepagents_agent_manager

    async def deadline_astream(**_kwargs):
        yield {"event": "tool_start", "data": '{"tool":"patch_file"}'}
        yield {"event": "tool_end", "data": '{"tool":"patch_file","is_error":false}'}
        await asyncio.sleep(2)

    monkeypatch.setattr(deepagents_agent_manager, "astream", deadline_astream)
    monkeypatch.setattr(
        runner_module,
        "verify_code_case",
        lambda *_args, **_kwargs: {
            "mode": "swebench",
            "status": "not_evaluated",
            "passed": None,
            "reason": "deadline patch awaiting official verifier",
            "patch": "diff --git a/a.py b/a.py\n",
            "patch_sha256": "deadline-patch-hash",
            "changed_paths": ["a.py"],
            "commands": [],
        },
    )

    result = await runner._run_case(experiment, bundle.dataset.cases[0], 0, runtime_root)

    assert result["attempt_status"] == "completed"
    assert result["agent_budget_exhausted"] is True
    envelope = repository.load_run_envelopes(experiment.experiment_id)[
        bundle.dataset.cases[0].case_id
    ][0]
    assert envelope["outcome"] == "completed"
    assert envelope["metadata"]["agent_budget"]["submission"] == "workspace_patch_at_budget_boundary"
    assert envelope["metadata"]["agent_budget"]["reason"] == "timeout"
    assert envelope["metadata"]["code_verification"]["patch_sha256"] == "deadline-patch-hash"
    assert prepared.closed is True


@pytest.mark.asyncio
async def test_swebench_model_call_budget_submits_existing_patch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    dataset = _dataset()
    repository = EvaluationRepository(tmp_path / "evaluation.sqlite3")
    draft = repository.create_dataset(dataset)
    bundle = repository.publish_dataset(draft.dataset_id, draft.revision)
    base = _experiment(bundle.dataset)
    experiment = repository.create_experiment(
        base.model_copy(
            update={
                "dataset_id": draft.dataset_id,
                "dataset_version_id": bundle.version_id,
                "dataset_content_hash": bundle.checksum,
            }
        )
    )
    runtime_root = tmp_path / "runtime"
    runner = EvaluationRunner(repository, LangSmithSettings(enabled=False), tmp_path)
    runner._initialize_isolated_runtime(runtime_root)
    workspace = runtime_root / "workspace"
    workspace.mkdir(parents=True)
    monkeypatch.setattr(
        runner,
        "_prepare_workspace",
        lambda _root, _case, _repetition: (workspace, "project-swebench"),
    )

    class PreparedBackend:
        closed = False

        def close(self):
            self.closed = True

    prepared = PreparedBackend()

    async def fake_prepare_backend(*_args, **_kwargs):
        return prepared

    monkeypatch.setattr(agent_backend_module, "prepare_swebench_agent_backend", fake_prepare_backend)

    from graph.deepagents_manager import deepagents_agent_manager

    async def budget_astream(**_kwargs):
        yield {"event": "tool_start", "data": '{"tool":"patch_file"}'}
        yield {"event": "tool_end", "data": '{"tool":"patch_file","is_error":false}'}
        yield {
            "event": "run_outcome",
            "data": '{"outcome":"budget_exceeded","run_id":"run-budget","query_id":"query-budget"}',
        }

    monkeypatch.setattr(deepagents_agent_manager, "astream", budget_astream)
    monkeypatch.setattr(
        runner_module,
        "verify_code_case",
        lambda *_args, **_kwargs: {
            "mode": "swebench",
            "status": "not_evaluated",
            "passed": None,
            "reason": "model-call budget patch awaiting official verifier",
            "patch": "diff --git a/a.py b/a.py\n",
            "patch_sha256": "budget-patch-hash",
            "changed_paths": ["a.py"],
            "commands": [],
        },
    )

    result = await runner._run_case(experiment, bundle.dataset.cases[0], 0, runtime_root)

    assert result["attempt_status"] == "completed"
    assert result["agent_budget_exhausted"] is True
    envelope = repository.load_run_envelopes(experiment.experiment_id)[
        bundle.dataset.cases[0].case_id
    ][0]
    assert envelope["outcome"] == "completed"
    assert envelope["metadata"]["agent_budget"] == {
        "exhausted": True,
        "reason": "model_call_limit",
        "timeout_seconds": experiment.execution.timeout_seconds,
        "tool_events": 1,
        "last_tool": "patch_file",
        "submission": "workspace_patch_at_budget_boundary",
    }
    assert envelope["metadata"]["code_verification"]["patch_sha256"] == "budget-patch-hash"
    assert prepared.closed is True


@pytest.mark.asyncio
async def test_managed_official_harness_runs_pinned_package_and_parses_report(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    dataset = _dataset()
    experiment = _experiment(dataset)
    envelopes = _envelopes(experiment, dataset)

    async def docker_probe(environment, root):
        return True, "29.6.2"

    async def fake_process(
        argv, *, cwd, environment, timeout_seconds, log_path, isolate_process_group=False, pid_path=None
    ):
        del timeout_seconds
        assert isolate_process_group is True
        assert pid_path == cwd / "harness.pid"
        assert "LANGSMITH_API_KEY" not in environment
        assert argv[1:3] == ["-m", "puddingclaw_swebench_entry"]
        assert environment["PYTHONPATH"] == str(cwd)
        guard = (cwd / "sitecustomize.py").read_text(encoding="utf-8")
        assert 'kwargs["network_disabled"] = True' in guard
        assert 'kwargs["cap_drop"] = ["ALL"]' in guard
        assert 'kwargs["pids_limit"]' in guard
        assert 'kwargs["storage_opt"]' in guard
        run_id = argv[argv.index("--run_id") + 1]
        report = {
            "completed_ids": ["pytest-dev__pytest-1234"],
            "resolved_ids": ["pytest-dev__pytest-1234"],
            "unresolved_ids": [],
            "error_ids": [],
        }
        (cwd / f"model.{run_id}.json").write_text(json.dumps(report), encoding="utf-8")
        log_path.write_text("official harness complete", encoding="utf-8")
        return ProcessResult(exit_code=0, output_tail="official harness complete")

    monkeypatch.setattr(official_module, "_docker_probe", docker_probe)
    monkeypatch.setattr(official_module, "_run_process", fake_process)
    monkeypatch.setattr(official_module.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setenv("LANGSMITH_API_KEY", "must-not-enter-official-harness")

    result = await run_official_swebench_harness(experiment, dataset, envelopes, tmp_path)

    assert result["status"] == "completed"
    assert result["results"]["pytest-dev__pytest-1234"]["status"] == "passed"
    assert result["receipt"]["provenance"] == "platform_managed_official_harness"
    assert result["receipt"]["report_sha256"]
    assert "diff --git" not in json.dumps(result["receipt"])


@pytest.mark.asyncio
async def test_managed_official_harness_fails_closed_when_docker_is_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    dataset = _dataset()
    experiment = _experiment(dataset)

    async def docker_probe(environment, root):
        return False, "Docker daemon is unavailable"

    monkeypatch.setattr(official_module, "_docker_probe", docker_probe)
    result = await run_official_swebench_harness(
        experiment,
        dataset,
        _envelopes(experiment, dataset),
        tmp_path,
    )

    assert result["status"] == "error"
    assert result["results"]["pytest-dev__pytest-1234"]["status"] == "error"


def test_official_report_reader_rejects_symlink(tmp_path: Path):
    target = tmp_path / "target.json"
    target.write_text("{}", encoding="utf-8")
    link = tmp_path / "report.json"
    link.symlink_to(target)

    with pytest.raises(OSError):
        _read_report(link)


@pytest.mark.asyncio
async def test_managed_official_harness_rejects_inconsistent_aggregate_report(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    dataset = _dataset()
    experiment = _experiment(dataset)

    async def docker_probe(environment, root):
        return True, "29.6.2"

    async def fake_process(
        argv, *, cwd, environment, timeout_seconds, log_path, isolate_process_group=False, pid_path=None
    ):
        del environment, timeout_seconds, log_path, pid_path
        assert isolate_process_group is True
        run_id = argv[argv.index("--run_id") + 1]
        report = {
            "completed_ids": ["pytest-dev__pytest-1234"],
            "resolved_ids": ["pytest-dev__pytest-1234"],
            "unresolved_ids": ["pytest-dev__pytest-1234"],
            "error_ids": [],
        }
        (cwd / f"model.{run_id}.json").write_text(json.dumps(report), encoding="utf-8")
        return ProcessResult(exit_code=0, output_tail="inconsistent report")

    monkeypatch.setattr(official_module, "_docker_probe", docker_probe)
    monkeypatch.setattr(official_module, "_run_process", fake_process)

    result = await run_official_swebench_harness(
        experiment,
        dataset,
        _envelopes(experiment, dataset),
        tmp_path,
    )

    assert result["status"] == "error"
    assert result["results"]["pytest-dev__pytest-1234"]["status"] == "error"
    assert "inconsistent" in result["results"]["pytest-dev__pytest-1234"]["reason"].lower()


@pytest.mark.asyncio
async def test_runner_applies_platform_managed_official_result_to_attempt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    repository = EvaluationRepository(tmp_path / "evaluation.db")
    draft = repository.create_dataset(_dataset())
    bundle = repository.publish_dataset(draft.dataset_id, draft.revision)
    experiment = repository.create_experiment(
        _experiment(bundle.dataset).model_copy(
            update={
                "dataset_id": draft.dataset_id,
                "dataset_version_id": bundle.version_id,
                "dataset_content_hash": bundle.checksum,
            }
        )
    )
    case = bundle.dataset.cases[0]
    attempt_id = repository.create_attempt(experiment.experiment_id, case.case_id, 0)
    envelope = _envelopes(experiment, bundle.dataset, attempt_id)[case.case_id][0]
    run = AgentRunEnvelope.model_validate({key: value for key, value in envelope.items() if not key.startswith("_")})
    repository.finish_attempt(attempt_id, status="completed", run=run)
    code_evaluator = evaluator_registry.get_registered("code_verification.v1")
    assert code_evaluator is not None
    initial = code_evaluator[1](case, run, TraceEvidence(available_kinds={"code_patch", "code_verification"}))
    repository.save_result(experiment.experiment_id, attempt_id, initial)
    all_attempts = [
        {
            "case_id": case.case_id,
            "attempt_id": attempt_id,
            "attempt_status": "completed",
            "results": [initial.model_dump(mode="json")],
            "summary": evaluator_registry.summarize(case, [initial]),
        }
    ]

    async def fake_official(experiment, dataset, run_envelopes, runtime_root):
        del experiment, dataset, run_envelopes, runtime_root
        return {
            "status": "completed",
            "reason": "Official SWE-bench Harness completed",
            "results": {
                "pytest-dev__pytest-1234": {
                    "status": "passed",
                    "resolved": True,
                    "reason": "Official SWE-bench Harness resolved the instance",
                }
            },
            "receipt": {
                "provenance": "platform_managed_official_harness",
                "package": "swebench==4.1.0",
                "run_id": "run",
                "report_sha256": "report",
                "predictions_sha256": "predictions",
                "patch_sha256": {"pytest-dev__pytest-1234": "patch"},
                "source_snapshot_sha256": "snapshot",
                "aggregate": {"resolved_ids": ["pytest-dev__pytest-1234"]},
            },
        }

    monkeypatch.setattr(runner_module, "run_official_swebench_harness", fake_official)
    runner = EvaluationRunner(repository, LangSmithSettings(enabled=False), tmp_path)
    summary = await runner._run_and_apply_official_swebench(
        experiment,
        bundle.dataset,
        tmp_path,
        all_attempts,
    )

    assert summary["status"] == "completed"
    assert summary["resolved"] == 1
    saved_run = repository.load_run_envelopes(experiment.experiment_id)[case.case_id][0]
    assert saved_run["metadata"]["code_verification"]["status"] == "passed"
    saved_code_result = next(
        row["result"]
        for row in repository.list_results(experiment.experiment_id)
        if row["result"] and row["result"]["evaluator_id"] == "code_verification.v1"
    )
    assert saved_code_result["outcome"] == "pass"
    assert all_attempts[0]["summary"]["verdict"] == "pass"
