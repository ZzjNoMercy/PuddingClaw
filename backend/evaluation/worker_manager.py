"""Spawn/exec manager for isolated evaluation workers."""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import signal
import stat
import sys
from pathlib import Path


class EvaluationWorkerManager:
    def __init__(self) -> None:
        self._processes: dict[str, asyncio.subprocess.Process] = {}
        self._lock = asyncio.Lock()
        self._max_workers = max(1, int(os.getenv("PUDDINGCLAW_EVALUATION_MAX_WORKERS", "1")))
        self._stopping = False

    @staticmethod
    def _cleanup_runtime_if_needed(experiment_id: str) -> None:
        from .repository import get_evaluation_repository

        try:
            experiment = get_evaluation_repository().get_experiment(experiment_id)
            if experiment.execution.preserve_workspaces:
                return
        except Exception:
            return
        from runtime_identity.paths import PuddingClawPaths

        shutil.rmtree(
            PuddingClawPaths.from_environment().data() / "evaluation-runs" / experiment_id,
            ignore_errors=True,
        )

    @staticmethod
    async def _resolve_docker_host() -> str | None:
        configured = os.getenv("DOCKER_HOST")
        if configured:
            return configured
        docker = shutil.which("docker")
        if docker is None:
            return None
        try:
            process = await asyncio.create_subprocess_exec(
                docker,
                "context",
                "inspect",
                "--format",
                "{{.Endpoints.docker.Host}}",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            stdout, _ = await asyncio.wait_for(process.communicate(), timeout=10)
        except (OSError, TimeoutError):
            return None
        endpoint = stdout.decode("utf-8", errors="replace").strip()
        if process.returncode != 0 or not endpoint.startswith(("unix://", "tcp://", "npipe://")):
            return None
        return endpoint

    @staticmethod
    async def _terminate_official_harness(experiment_id: str) -> None:
        if os.name == "nt":
            return
        from runtime_identity.paths import PuddingClawPaths

        pid_path = (
            PuddingClawPaths.from_environment().data()
            / "evaluation-runs"
            / experiment_id
            / "official-swebench"
            / "harness.pid"
        )
        try:
            flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
            descriptor = os.open(pid_path, flags)
            try:
                info = os.fstat(descriptor)
                if not stat.S_ISREG(info.st_mode) or info.st_size > 1_024:
                    return
                payload = os.read(descriptor, 1_025)
            finally:
                os.close(descriptor)
            if len(payload) > 1_024:
                return
            identity = json.loads(payload.decode("ascii"))
            pid = int(identity["pid"])
            expected_run_id = f"puddingclaw-{experiment_id}"
            if identity.get("run_id") != expected_run_id:
                return
            inspect = await asyncio.create_subprocess_exec(
                "ps",
                "-p",
                str(pid),
                "-o",
                "pid=,pgid=,lstart=,command=",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            stdout, _ = await asyncio.wait_for(inspect.communicate(), timeout=5)
            process_line = stdout.decode("utf-8", errors="replace").strip()
            fields = process_line.split(maxsplit=7)
            observed_started_at = " ".join(fields[2:7]) if len(fields) >= 8 else ""
            command = fields[7] if len(fields) >= 8 else ""
            if (
                inspect.returncode != 0
                or len(fields) < 8
                or fields[0] != str(pid)
                or fields[1] != str(pid)
                or identity.get("process_started_at") != observed_started_at
                or "puddingclaw_swebench_entry" not in command
                or f"--run_id {expected_run_id}" not in command
            ):
                return
            os.killpg(pid, signal.SIGTERM)
            for _ in range(50):
                try:
                    os.killpg(pid, 0)
                except ProcessLookupError:
                    return
                await asyncio.sleep(0.1)
            os.killpg(pid, signal.SIGKILL)
        except (OSError, TimeoutError, ValueError):
            return
        finally:
            pid_path.unlink(missing_ok=True)

    @staticmethod
    async def _cleanup_official_swebench_containers(experiment_id: str) -> None:
        docker = shutil.which("docker")
        if docker is None:
            return
        run_id = f"puddingclaw-{experiment_id}"
        for _ in range(3):
            try:
                process = await asyncio.create_subprocess_exec(
                    docker,
                    "ps",
                    "-aq",
                    "--filter",
                    f"label=com.puddingclaw.swebench.run_id={run_id}",
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.DEVNULL,
                )
                stdout, _ = await asyncio.wait_for(process.communicate(), timeout=15)
                container_ids = [
                    value
                    for value in stdout.decode("ascii", errors="ignore").splitlines()
                    if 12 <= len(value) <= 64
                    and all(character in "0123456789abcdef" for character in value.lower())
                ]
                if process.returncode != 0 or not container_ids:
                    return
                cleanup = await asyncio.create_subprocess_exec(
                    docker,
                    "rm",
                    "-f",
                    *container_ids,
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.DEVNULL,
                )
                await asyncio.wait_for(cleanup.wait(), timeout=30)
                await asyncio.sleep(0.25)
            except (OSError, TimeoutError):
                return

    @staticmethod
    async def _terminate_worker_group(process: asyncio.subprocess.Process) -> None:
        if process.returncode is not None:
            return
        if os.name == "nt":
            try:
                process.terminate()
                await asyncio.wait_for(process.wait(), timeout=5)
            except ProcessLookupError:
                return
            except TimeoutError:
                process.kill()
                await process.wait()
            return

        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            return
        deadline = asyncio.get_running_loop().time() + 5
        while asyncio.get_running_loop().time() < deadline:
            try:
                os.killpg(process.pid, 0)
            except ProcessLookupError:
                await process.wait()
                return
            await asyncio.sleep(0.1)
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        await process.wait()

    async def start(self, experiment_id: str) -> None:
        async with self._lock:
            if self._stopping:
                return
            existing = self._processes.get(experiment_id)
            if existing and existing.returncode is None:
                return
            active = sum(process.returncode is None for process in self._processes.values())
            if active >= self._max_workers:
                # The durable Experiment remains queued and will be picked up
                # when the current worker is reaped.
                return
            backend_dir = Path(__file__).resolve().parent.parent
            from runtime_identity.paths import PuddingClawPaths

            runtime_root = PuddingClawPaths.from_environment().root
            allowed_keys = {
                "PATH",
                "PYTHONPATH",
                "LANG",
                "LC_ALL",
                "SSL_CERT_FILE",
                "SSL_CERT_DIR",
                "HTTP_PROXY",
                "HTTPS_PROXY",
                "NO_PROXY",
                "http_proxy",
                "https_proxy",
                "no_proxy",
                "LANGSMITH_API_KEY",
                "LANGSMITH_ENDPOINT",
                "LANGSMITH_PROJECT",
                "PUDDINGCLAW_EVALUATION_DB",
                "PUDDINGCLAW_EVALUATION_SETTINGS",
                "DOCKER_HOST",
                "DOCKER_TLS_VERIFY",
                "DOCKER_CERT_PATH",
                "UV_CACHE_DIR",
                "PUDDINGCLAW_SWEBENCH_NAMESPACE",
                "PUDDINGCLAW_SWEBENCH_TEST_TIMEOUT_SECONDS",
                "PUDDINGCLAW_SWEBENCH_JOB_TIMEOUT_SECONDS",
                "PUDDINGCLAW_SWEBENCH_MAX_WORKERS",
                "PUDDINGCLAW_SWEBENCH_CONTAINER_MEMORY",
                "PUDDINGCLAW_SWEBENCH_CONTAINER_CPUS",
                "PUDDINGCLAW_SWEBENCH_CONTAINER_PIDS",
                "PUDDINGCLAW_SWEBENCH_CONTAINER_DISK_GB",
                "PUDDINGCLAW_SWEBENCH_ISOLATED_DOCKER",
            }
            environment = {key: value for key, value in os.environ.items() if key in allowed_keys}
            if "DOCKER_HOST" not in environment:
                docker_host = await self._resolve_docker_host()
                if docker_host:
                    environment["DOCKER_HOST"] = docker_host
            environment["PYTHONPATH"] = str(backend_dir)
            environment["PUDDINGCLAW_HOME"] = str(runtime_root)
            process = await asyncio.create_subprocess_exec(
                sys.executable,
                "-m",
                "evaluation.worker",
                "--experiment-id",
                experiment_id,
                cwd=backend_dir,
                env=environment,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
                start_new_session=os.name != "nt",
            )
            self._processes[experiment_id] = process
            asyncio.create_task(self._reap(experiment_id, process))

    async def _reap(self, experiment_id: str, process: asyncio.subprocess.Process) -> None:
        return_code = await process.wait()
        async with self._lock:
            if self._processes.get(experiment_id) is process:
                self._processes.pop(experiment_id, None)
        if return_code != 0:
            from .contracts import EvalError, ExperimentStatus, utc_now
            from .repository import get_evaluation_repository

            repository = get_evaluation_repository()
            try:
                experiment = repository.get_experiment(experiment_id)
                if experiment.status in {ExperimentStatus.QUEUED, ExperimentStatus.RUNNING}:
                    repository.update_experiment(
                        experiment.model_copy(
                            update={
                                "status": ExperimentStatus.FAILED,
                                "finished_at": utc_now(),
                                "error": EvalError(
                                    code="worker_process_failed",
                                    message=f"Evaluation worker exited with code {return_code}",
                                    retryable=True,
                                ),
                            }
                        ),
                        expected_status=experiment.status,
                    )
            except Exception:
                pass
        await self._terminate_official_harness(experiment_id)
        await self._cleanup_official_swebench_containers(experiment_id)
        self._cleanup_runtime_if_needed(experiment_id)
        if not self._stopping:
            await self.start_pending(recover_orphans=False)

    async def start_pending(self, *, recover_orphans: bool = True) -> None:
        from .contracts import ExperimentStatus
        from .repository import get_evaluation_repository

        if recover_orphans:
            self._stopping = False
        repository = get_evaluation_repository()
        async with self._lock:
            live_ids = {
                experiment_id
                for experiment_id, process in self._processes.items()
                if process.returncode is None
            }
        for experiment in repository.list_experiments():
            if experiment.status == ExperimentStatus.QUEUED:
                await self.start(experiment.experiment_id)
            elif (
                recover_orphans
                and experiment.experiment_id not in live_ids
                and experiment.status in {ExperimentStatus.RUNNING, ExperimentStatus.CANCEL_REQUESTED}
            ):
                terminal = (
                    ExperimentStatus.CANCELLED
                    if experiment.status == ExperimentStatus.CANCEL_REQUESTED
                    else ExperimentStatus.FAILED
                )
                repository.cancel_running_attempts(
                    experiment.experiment_id,
                    "Evaluation worker was interrupted by application restart",
                )
                from .contracts import EvalError, utc_now

                repository.update_experiment(
                    experiment.model_copy(
                        update={
                            "status": terminal,
                            "finished_at": utc_now(),
                            "error": None
                            if terminal == ExperimentStatus.CANCELLED
                            else EvalError(
                                code="worker_interrupted",
                                message="Evaluation worker was interrupted by application restart",
                                retryable=True,
                            ),
                        }
                    ),
                    expected_status=experiment.status,
                )
                await self._terminate_official_harness(experiment.experiment_id)
                await self._cleanup_official_swebench_containers(experiment.experiment_id)
                self._cleanup_runtime_if_needed(experiment.experiment_id)

    async def stop(self) -> None:
        async with self._lock:
            self._stopping = True
            processes = [
                (experiment_id, process)
                for experiment_id, process in self._processes.items()
                if process.returncode is None
            ]
        for experiment_id, process in processes:
            await self._terminate_official_harness(experiment_id)
            await self._terminate_worker_group(process)

    async def cancel(self, experiment_id: str) -> bool:
        async with self._lock:
            process = self._processes.get(experiment_id)
            if process is None or process.returncode is not None:
                return False
        await self._terminate_official_harness(experiment_id)
        await self._terminate_worker_group(process)
        return True


evaluation_worker_manager = EvaluationWorkerManager()
