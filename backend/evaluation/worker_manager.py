"""Spawn/exec manager for isolated evaluation workers."""

from __future__ import annotations

import asyncio
import os
import shutil
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
        backend_dir = Path(__file__).resolve().parent.parent
        shutil.rmtree(backend_dir / "data" / "evaluation-runs" / experiment_id, ignore_errors=True)

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
                "PUDDINGDATA_USER_DATA_DIR",
                "PUDDINGCLAW_USER_DATA_DIR",
            }
            environment = {key: value for key, value in os.environ.items() if key in allowed_keys}
            environment["PYTHONPATH"] = str(backend_dir)
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
                self._cleanup_runtime_if_needed(experiment.experiment_id)

    async def stop(self) -> None:
        async with self._lock:
            self._stopping = True
            processes = [process for process in self._processes.values() if process.returncode is None]
        for process in processes:
            process.terminate()
        if processes:
            await asyncio.gather(*(process.wait() for process in processes), return_exceptions=True)

    async def cancel(self, experiment_id: str) -> bool:
        async with self._lock:
            process = self._processes.get(experiment_id)
            if process is None or process.returncode is not None:
                return False
            process.terminate()
        try:
            await asyncio.wait_for(process.wait(), timeout=5)
        except TimeoutError:
            process.kill()
            await process.wait()
        return True


evaluation_worker_manager = EvaluationWorkerManager()
