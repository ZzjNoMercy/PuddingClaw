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
    async def _terminate_managed_process(
        experiment_id: str,
        *,
        pid_path: Path,
        command_markers: tuple[str, ...],
    ) -> None:
        if os.name == "nt":
            return
        unlink_receipt = False
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
            # PID receipts are JSON.  Read them as UTF-8 so localized process
            # identity values remain valid even if a producer stops escaping
            # non-ASCII characters in a future revision.
            identity = json.loads(payload.decode("utf-8"))
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
            if inspect.returncode != 0 or not process_line:
                try:
                    os.kill(pid, 0)
                except ProcessLookupError:
                    unlink_receipt = True
                return
            fields = process_line.split(maxsplit=7)
            if len(fields) < 8:
                return
            observed_started_at = " ".join(fields[2:7])
            command = fields[7]
            if identity.get("process_started_at") != observed_started_at:
                # A different kernel start identity proves PID reuse.
                unlink_receipt = True
                return
            if (
                fields[0] != str(pid)
                or fields[1] != str(pid)
                or (
                    f"--run_id {expected_run_id}" not in command
                    and f"--run-id {expected_run_id}" not in command
                )
                or any(marker not in command for marker in command_markers)
            ):
                # Formatting/PG/command mismatches do not prove reuse. Keep
                # the receipt so a later cleanup pass can inspect it again.
                return
            os.killpg(pid, signal.SIGTERM)
            for _ in range(50):
                try:
                    os.killpg(pid, 0)
                except ProcessLookupError:
                    unlink_receipt = True
                    return
                await asyncio.sleep(0.1)
            os.killpg(pid, signal.SIGKILL)
            for _ in range(50):
                try:
                    os.killpg(pid, 0)
                except ProcessLookupError:
                    unlink_receipt = True
                    return
                await asyncio.sleep(0.1)
        except (OSError, TimeoutError, ValueError):
            # Preserve the receipt on transient inspection/termination errors
            # so restart and explicit cleanup can retry the same process group.
            return
        finally:
            if unlink_receipt:
                pid_path.unlink(missing_ok=True)

    @staticmethod
    async def _terminate_official_harness(experiment_id: str) -> None:
        from runtime_identity.paths import PuddingClawPaths

        await EvaluationWorkerManager._terminate_managed_process(
            experiment_id,
            pid_path=(
                PuddingClawPaths.from_environment().data()
                / "evaluation-runs"
                / experiment_id
                / "official-swebench"
                / "harness.pid"
            ),
            command_markers=("puddingclaw_swebench_entry",),
        )

    @staticmethod
    async def _terminate_swebench_image_preparation(experiment_id: str) -> None:
        from runtime_identity.paths import PuddingClawPaths

        runtime_root = (
            PuddingClawPaths.from_environment().data()
            / "evaluation-runs"
            / experiment_id
        )
        for pid_path in runtime_root.glob("agent-scratch/*/image-preparation/candidate-image.pid"):
            await EvaluationWorkerManager._terminate_managed_process(
                experiment_id,
                pid_path=pid_path,
                command_markers=("evaluation.swebench_agent_backend", "--prepare-image"),
            )

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
                "PUDDINGCLAW_SWEBENCH_ARCH",
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
            stopping = self._stopping
        if return_code != 0 and not stopping:
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
        await self._terminate_swebench_image_preparation(experiment_id)
        await self._cleanup_official_swebench_containers(experiment_id)
        self._cleanup_runtime_if_needed(experiment_id)
        if not self._stopping:
            await self.start_pending(recover_orphans=False)

    @staticmethod
    def _restart_summary(experiment: object, *, total_attempts: int | None = None) -> dict[str, object]:
        from .contracts import utc_now

        summary = dict(getattr(experiment, "summary", {}) or {})
        lineage = {
            key: value
            for key, value in summary.items()
            if key in {"retry_of_experiment_id", "retry_root_experiment_id", "retry_generation"}
        }
        previous_progress = dict(summary.get("progress") or {})
        total = total_attempts if total_attempts is not None else int(previous_progress.get("total") or 0)
        return {
            **lineage,
            "application_restart_pending": True,
            "progress": {
                "stage": "queued",
                "message": "开发服务已热重载，评测将从第一个 Case 自动重启",
                "total": total,
                "completed": 0,
                "failed": 0,
                "updated_at": utc_now().isoformat(),
            },
        }

    @staticmethod
    def _refresh_restart_candidate(repository: object, experiment: object) -> object:
        """Freeze the post-reload source/model snapshot before restarting."""

        from .candidate import CandidateRequest, bind_candidate_capability, resolve_candidate

        backend_dir = Path(__file__).resolve().parent.parent
        previous = experiment.candidate
        candidate = resolve_candidate(
            backend_dir,
            CandidateRequest(
                name=previous.name,
                llm_model_id=previous.llm_model_id,
                thinking_level=previous.thinking_level,
                credential_name=previous.credential_name,
                analytics_model_id=previous.analytics_model_id,
            ),
        )
        candidate = bind_candidate_capability(candidate, experiment.profile_id)
        summary = dict(experiment.summary)
        summary.pop("application_restart_pending", None)
        refreshed = experiment.model_copy(update={"candidate": candidate, "summary": summary})
        return repository.update_experiment(
            refreshed,
            expected_status="queued",
        )

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
                if experiment.summary.get("application_restart_pending"):
                    try:
                        experiment = self._refresh_restart_candidate(repository, experiment)
                    except Exception as exc:
                        from .contracts import EvalError, utc_now

                        repository.update_experiment(
                            experiment.model_copy(
                                update={
                                    "status": ExperimentStatus.FAILED,
                                    "finished_at": utc_now(),
                                    "error": EvalError(
                                        code="restart_snapshot_failed",
                                        message=(
                                            "Failed to freeze the Agent snapshot after application restart: "
                                            f"{type(exc).__name__}: {str(exc)[:500]}"
                                        ),
                                        retryable=True,
                                    ),
                                }
                            ),
                            expected_status=ExperimentStatus.QUEUED,
                        )
                        continue
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
                await self._terminate_swebench_image_preparation(experiment.experiment_id)
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
            await self._terminate_swebench_image_preparation(experiment_id)
            await self._terminate_worker_group(process)
            from .contracts import ExperimentStatus, utc_now
            from .repository import get_evaluation_repository

            repository = get_evaluation_repository()
            try:
                experiment = repository.get_experiment(experiment_id)
                if experiment.status in {ExperimentStatus.QUEUED, ExperimentStatus.RUNNING}:
                    execution_mode = experiment.summary.get("execution_mode")
                    incremental_recovery = execution_mode in {
                        "official_verifier_replay",
                        "swebench_missing_case_resume",
                    }
                    if incremental_recovery:
                        if execution_mode == "swebench_missing_case_resume":
                            repository.cancel_running_attempts(
                                experiment.experiment_id,
                                "Application reloaded during missing-Case resume",
                            )
                        replay = dict(
                            experiment.summary.get("swebench_verifier_replay") or {}
                        )
                        case_resume = dict(
                            experiment.summary.get("swebench_case_resume") or {}
                        )
                        is_case_resume = execution_mode == "swebench_missing_case_resume"
                        incremental_summary = dict(experiment.summary)
                        if is_case_resume:
                            incremental_summary["swebench_case_resume"] = {
                                **case_resume,
                                "status": "queued",
                                "restart_pending": True,
                            }
                        else:
                            incremental_summary["swebench_verifier_replay"] = {
                                **replay,
                                "status": "queued",
                                "restart_pending": True,
                            }
                        restart = experiment.model_copy(
                            update={
                                "status": ExperimentStatus.QUEUED,
                                "verdict": "pending",
                                "error": None,
                                "started_at": None,
                                "finished_at": None,
                                "summary": {
                                    **incremental_summary,
                                    "progress": {
                                        **dict(experiment.summary.get("progress") or {}),
                                        "stage": "queued",
                                        "message": (
                                            "开发服务已热重载，将继续补跑缺失 Case"
                                            if is_case_resume
                                            else "开发服务已热重载，将继续复用 patch 重新判卷"
                                        ),
                                        "completed": 0,
                                        "failed": 0,
                                        "updated_at": utc_now().isoformat(),
                                    },
                                },
                            }
                        )
                        repository.update_experiment(
                            restart,
                            expected_status=experiment.status,
                        )
                    else:
                        restart = experiment.model_copy(
                            update={
                                "status": ExperimentStatus.QUEUED,
                                "verdict": "pending",
                                "error": None,
                                "started_at": None,
                                "finished_at": None,
                                "remote_experiment_id": None,
                                "remote_url": None,
                                "summary": self._restart_summary(experiment),
                            }
                        )
                        repository.reset_experiment_execution(
                            restart,
                            expected_status=experiment.status,
                        )
            except Exception:
                # Shutdown must continue even if the durable recovery marker
                # cannot be written; startup orphan handling remains fail-closed.
                pass

    async def cancel(self, experiment_id: str) -> bool:
        async with self._lock:
            process = self._processes.get(experiment_id)
            if process is None or process.returncode is not None:
                return False
        await self._terminate_official_harness(experiment_id)
        await self._terminate_swebench_image_preparation(experiment_id)
        await self._terminate_worker_group(process)
        return True

    async def delete_artifacts(self, experiment_id: str) -> bool:
        """Remove local runtime artifacts after a terminal Experiment is deleted."""

        async with self._lock:
            process = self._processes.get(experiment_id)
            if process is not None and process.returncode is None:
                return False
        await self._terminate_official_harness(experiment_id)
        await self._terminate_swebench_image_preparation(experiment_id)
        await self._cleanup_official_swebench_containers(experiment_id)
        from runtime_identity.paths import PuddingClawPaths

        shutil.rmtree(
            PuddingClawPaths.from_environment().data() / "evaluation-runs" / experiment_id,
            ignore_errors=True,
        )
        return True


evaluation_worker_manager = EvaluationWorkerManager()
