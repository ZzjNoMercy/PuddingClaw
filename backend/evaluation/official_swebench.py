"""Platform-managed SWE-bench Docker Harness execution."""

from __future__ import annotations

import asyncio
import hashlib
import importlib.util
import json
import os
import platform
import re
import shutil
import signal
import stat
import sys
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .contracts import EvalDataset, EvalExperiment
from .swebench_adapter import (
    frozen_swebench_dataset_json,
    prediction_jsonl,
    swebench_run_manifest,
)

SWEBENCH_PACKAGE = "swebench==4.1.0"
MAX_PROCESS_LOG_BYTES = 20 * 1024 * 1024
MAX_PROCESS_TAIL_BYTES = 200_000
MAX_REPORT_BYTES = 20 * 1024 * 1024
DOCKER_GUARD_MODULE = """\
import os

from docker.models.containers import ContainerCollection

_original_create = ContainerCollection.create

def _puddingclaw_guarded_create(self, image, command=None, **kwargs):
    kwargs["network_disabled"] = True
    kwargs["cap_add"] = []
    kwargs["cap_drop"] = ["ALL"]
    kwargs["security_opt"] = ["no-new-privileges:true"]
    kwargs["mem_limit"] = os.environ["PUDDINGCLAW_SWEBENCH_CONTAINER_MEMORY"]
    kwargs["nano_cpus"] = int(os.environ["PUDDINGCLAW_SWEBENCH_CONTAINER_NANO_CPUS"])
    kwargs["pids_limit"] = int(os.environ["PUDDINGCLAW_SWEBENCH_CONTAINER_PIDS"])
    kwargs["storage_opt"] = {"size": os.environ["PUDDINGCLAW_SWEBENCH_CONTAINER_DISK"]}
    kwargs["init"] = True
    labels = dict(kwargs.get("labels") or {})
    labels["com.puddingclaw.swebench.run_id"] = os.environ["PUDDINGCLAW_SWEBENCH_RUN_ID"]
    kwargs["labels"] = labels
    return _original_create(self, image, command, **kwargs)

_puddingclaw_guarded_create.__puddingclaw_guarded__ = True
ContainerCollection.create = _puddingclaw_guarded_create
"""
HARNESS_ENTRY_MODULE = """\
import runpy

from docker.models.containers import ContainerCollection

if not getattr(ContainerCollection.create, "__puddingclaw_guarded__", False):
    raise RuntimeError("PuddingClaw Docker safety guard was not installed")

runpy.run_module("swebench.harness.run_evaluation", run_name="__main__")
"""


@dataclass(frozen=True)
class ProcessResult:
    exit_code: int
    output_tail: str
    timed_out: bool = False


def _bounded_int(name: str, default: int, minimum: int, maximum: int) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except ValueError:
        value = default
    return min(maximum, max(minimum, value))


def _bounded_memory() -> str:
    value = os.getenv("PUDDINGCLAW_SWEBENCH_CONTAINER_MEMORY", "8g").lower()
    match = re.fullmatch(r"([1-9][0-9]*)([gG])", value)
    if match is None:
        return "8g"
    return f"{min(64, max(2, int(match.group(1))))}g"


def _namespace() -> str:
    configured = os.getenv("PUDDINGCLAW_SWEBENCH_NAMESPACE")
    if configured is not None:
        return configured or "none"
    return "none" if platform.machine().lower() in {"arm64", "aarch64"} else "swebench"


async def _terminate_process_tree(
    process: asyncio.subprocess.Process,
    *,
    process_group: bool,
) -> None:
    if process.returncode is not None:
        return
    try:
        if process_group and os.name != "nt":
            os.killpg(process.pid, signal.SIGTERM)
        else:
            process.terminate()
    except ProcessLookupError:
        return
    if process_group and os.name != "nt":
        deadline = asyncio.get_running_loop().time() + 10
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
        return
    try:
        await asyncio.wait_for(process.wait(), timeout=10)
    except TimeoutError:
        try:
            if process_group and os.name != "nt":
                os.killpg(process.pid, signal.SIGKILL)
            else:
                process.kill()
        except ProcessLookupError:
            return
        await process.wait()


async def _run_process(
    argv: list[str],
    *,
    cwd: Path,
    environment: dict[str, str],
    timeout_seconds: int,
    log_path: Path,
    isolate_process_group: bool = False,
    pid_path: Path | None = None,
) -> ProcessResult:
    process = await asyncio.create_subprocess_exec(
        *argv,
        cwd=cwd,
        env=environment,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        start_new_session=isolate_process_group and os.name != "nt",
    )
    if pid_path is not None:
        process_started_at = ""
        if os.name != "nt":
            identity_process = await asyncio.create_subprocess_exec(
                "ps",
                "-p",
                str(process.pid),
                "-o",
                "lstart=",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            identity_stdout, _ = await asyncio.wait_for(identity_process.communicate(), timeout=5)
            if identity_process.returncode != 0 or not identity_stdout.strip():
                await _terminate_process_tree(process, process_group=isolate_process_group)
                raise RuntimeError("Could not establish official Harness process identity")
            process_started_at = identity_stdout.decode("ascii", errors="strict").strip()
        pid_path.write_text(
            json.dumps(
                {
                    "pid": process.pid,
                    "run_id": environment.get("PUDDINGCLAW_SWEBENCH_RUN_ID"),
                    "process_started_at": process_started_at,
                },
                separators=(",", ":"),
            ),
            encoding="ascii",
        )
    tail = bytearray()
    written = 0
    timed_out = False
    try:
        with log_path.open("wb") as log_file:
            async with asyncio.timeout(timeout_seconds):
                assert process.stdout is not None
                while chunk := await process.stdout.read(65_536):
                    if written < MAX_PROCESS_LOG_BYTES:
                        accepted = chunk[: MAX_PROCESS_LOG_BYTES - written]
                        log_file.write(accepted)
                        written += len(accepted)
                    tail.extend(chunk)
                    if len(tail) > MAX_PROCESS_TAIL_BYTES:
                        del tail[: len(tail) - MAX_PROCESS_TAIL_BYTES]
                await process.wait()
    except TimeoutError:
        timed_out = True
        await _terminate_process_tree(process, process_group=isolate_process_group)
    except BaseException:
        await _terminate_process_tree(process, process_group=isolate_process_group)
        raise
    finally:
        if pid_path is not None and process.returncode is not None:
            pid_path.unlink(missing_ok=True)
    return ProcessResult(
        exit_code=process.returncode if process.returncode is not None else -1,
        output_tail=tail.decode("utf-8", errors="replace"),
        timed_out=timed_out,
    )


async def _docker_probe(environment: dict[str, str], root: Path) -> tuple[bool, str]:
    docker = shutil.which("docker")
    if docker is None:
        return False, "Docker CLI is not installed"
    result = await _run_process(
        [docker, "version", "--format", "{{.Server.Version}}"],
        cwd=root,
        environment=environment,
        timeout_seconds=20,
        log_path=root / "docker-probe.log",
    )
    version = result.output_tail.strip().splitlines()[-1] if result.output_tail.strip() else ""
    if result.exit_code != 0 or not version:
        return False, f"Docker daemon is unavailable: {result.output_tail[-500:].strip()}"
    return True, version


def _docker_backend_is_approved() -> bool:
    if platform.system() == "Darwin":
        # Docker Desktop executes Linux containers inside its managed VM. Linux
        # deployments must point at a dedicated rootless/VM daemon explicitly.
        endpoint = os.getenv("DOCKER_HOST", "")
        return not endpoint or endpoint.startswith("unix://") or os.getenv(
            "PUDDINGCLAW_SWEBENCH_ISOLATED_DOCKER"
        ) == "1"
    if platform.system() == "Windows":
        # Python's subprocess API cannot guarantee Job Object tree termination
        # here yet, so fail closed until that implementation exists.
        return False
    endpoint = os.getenv("DOCKER_HOST", "")
    return bool(endpoint) and os.getenv("PUDDINGCLAW_SWEBENCH_ISOLATED_DOCKER") == "1"


def _official_environment() -> dict[str, str]:
    allowed = {
        "PATH",
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
        "DOCKER_HOST",
        "DOCKER_TLS_VERIFY",
        "DOCKER_CERT_PATH",
        "UV_CACHE_DIR",
        "TMPDIR",
    }
    return {key: value for key, value in os.environ.items() if key in allowed}


async def _cleanup_run_containers(run_id: str, environment: dict[str, str], root: Path) -> None:
    docker = shutil.which("docker")
    if docker is None:
        return
    listing = await _run_process(
        [docker, "ps", "-aq", "--filter", f"label=com.puddingclaw.swebench.run_id={run_id}"],
        cwd=root,
        environment=environment,
        timeout_seconds=20,
        log_path=root / "docker-cleanup-list.log",
    )
    container_ids = [
        value
        for value in listing.output_tail.splitlines()
        if 12 <= len(value) <= 64
        and all(character in "0123456789abcdef" for character in value.lower())
    ]
    if listing.exit_code != 0 or not container_ids:
        return
    await _run_process(
        [docker, "rm", "-f", *container_ids],
        cwd=root,
        environment=environment,
        timeout_seconds=30,
        log_path=root / "docker-cleanup.log",
    )


async def probe_official_swebench_runtime() -> dict[str, Any]:
    if not _docker_backend_is_approved():
        return {
            "available": False,
            "package": SWEBENCH_PACKAGE,
            "reason": (
                "SWE-bench requires a dedicated rootless/VM Docker endpoint on Linux; "
                "configure DOCKER_HOST and PUDDINGCLAW_SWEBENCH_ISOLATED_DOCKER=1 "
                "(Windows is not supported until process-tree Job Object cleanup is available)"
            ),
        }
    if importlib.util.find_spec("swebench") is None:
        return {
            "available": False,
            "package": SWEBENCH_PACKAGE,
            "reason": "Official verifier is not installed; run `uv sync --extra evaluation` in backend",
        }
    with tempfile.TemporaryDirectory(prefix="puddingclaw-swebench-probe-") as temporary:
        available, detail = await _docker_probe(_official_environment(), Path(temporary))
    return {
        "available": available,
        "package": SWEBENCH_PACKAGE,
        "docker_server_version": detail if available else None,
        "reason": None if available else detail,
    }


def _read_report(path: Path) -> tuple[dict[str, Any], str]:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_REPORT_BYTES:
            raise ValueError("SWE-bench aggregate report is missing, unsafe, or too large")
        chunks: list[bytes] = []
        remaining = MAX_REPORT_BYTES + 1
        while remaining:
            chunk = os.read(descriptor, min(65_536, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        payload = b"".join(chunks)
        if len(payload) > MAX_REPORT_BYTES:
            raise ValueError("SWE-bench aggregate report is too large")
    finally:
        os.close(descriptor)
    value = json.loads(payload.decode("utf-8"))
    if not isinstance(value, dict):
        raise ValueError("SWE-bench aggregate report must be an object")
    return value, hashlib.sha256(payload).hexdigest()


async def run_official_swebench_harness(
    experiment: EvalExperiment,
    dataset: EvalDataset,
    run_envelopes: dict[str, list[dict[str, Any]]],
    runtime_root: Path,
) -> dict[str, Any]:
    """Run the official verifier and return platform-originated per-instance results."""

    harness_root = runtime_root / "official-swebench"
    if harness_root.exists():
        shutil.rmtree(harness_root)
    harness_root.mkdir(parents=True, mode=0o700)
    (harness_root / "sitecustomize.py").write_text(DOCKER_GUARD_MODULE, encoding="utf-8")
    (harness_root / "puddingclaw_swebench_entry.py").write_text(HARNESS_ENTRY_MODULE, encoding="utf-8")
    started_at = datetime.now(UTC)
    model_name = f"puddingclaw-{experiment.candidate.candidate_id}"
    fixture_content = frozen_swebench_dataset_json(dataset)
    prediction_content = prediction_jsonl(dataset, run_envelopes, model_name_or_path=model_name)
    manifest = swebench_run_manifest(
        dataset,
        run_envelopes,
        model_name_or_path=model_name,
        experiment_id=experiment.experiment_id,
        dataset_version_id=experiment.dataset_version_id,
        dataset_content_hash=experiment.dataset_content_hash,
    )
    fixture_path = harness_root / "dataset.json"
    predictions_path = harness_root / "predictions.jsonl"
    fixture_path.write_text(fixture_content, encoding="utf-8")
    predictions_path.write_text(prediction_content, encoding="utf-8")

    environment = _official_environment()
    docker_available, docker_detail = await _docker_probe(environment, harness_root)
    expected_ids = set(manifest["patch_sha256"])
    base_receipt: dict[str, Any] = {
        "schema_version": "1",
        "provenance": "platform_managed_official_harness",
        "package": SWEBENCH_PACKAGE,
        "experiment_id": experiment.experiment_id,
        "dataset_version_id": experiment.dataset_version_id,
        "source_snapshot_sha256": manifest.get("source_snapshot_sha256"),
        "predictions_sha256": manifest["predictions_sha256"],
        "patch_sha256": manifest["patch_sha256"],
        "fixture_sha256": hashlib.sha256(fixture_content.encode("utf-8")).hexdigest(),
        "started_at": started_at.isoformat(),
    }
    if not docker_available:
        reason = docker_detail or "Docker daemon is unavailable"
        return {
            "status": "error",
            "reason": reason,
            "results": {instance_id: {"status": "error", "reason": reason} for instance_id in expected_ids},
            "receipt": {**base_receipt, "status": "error", "reason": reason},
        }

    if not _docker_backend_is_approved():
        reason = "Docker endpoint is not approved for untrusted SWE-bench candidate execution"
        return {
            "status": "error",
            "reason": reason,
            "results": {instance_id: {"status": "error", "reason": reason} for instance_id in expected_ids},
            "receipt": {**base_receipt, "status": "error", "docker_server_version": docker_detail, "reason": reason},
        }

    test_timeout = _bounded_int("PUDDINGCLAW_SWEBENCH_TEST_TIMEOUT_SECONDS", 1_800, 60, 7_200)
    max_workers = _bounded_int("PUDDINGCLAW_SWEBENCH_MAX_WORKERS", 1, 1, 4)
    default_job_timeout = 1_800 + ((test_timeout + 1_800) * max(1, len(expected_ids)) // max_workers)
    job_timeout = _bounded_int(
        "PUDDINGCLAW_SWEBENCH_JOB_TIMEOUT_SECONDS",
        default_job_timeout,
        600,
        24 * 3_600,
    )
    run_id = f"puddingclaw-{experiment.experiment_id}"
    environment.update(
        {
            "PYTHONPATH": str(harness_root),
            "PUDDINGCLAW_SWEBENCH_RUN_ID": run_id,
            "PUDDINGCLAW_SWEBENCH_CONTAINER_MEMORY": _bounded_memory(),
            "PUDDINGCLAW_SWEBENCH_CONTAINER_NANO_CPUS": str(
                _bounded_int("PUDDINGCLAW_SWEBENCH_CONTAINER_CPUS", 4, 1, 16) * 1_000_000_000
            ),
            "PUDDINGCLAW_SWEBENCH_CONTAINER_PIDS": str(
                _bounded_int("PUDDINGCLAW_SWEBENCH_CONTAINER_PIDS", 1024, 64, 4096)
            ),
            "PUDDINGCLAW_SWEBENCH_CONTAINER_DISK": (
                f'{_bounded_int("PUDDINGCLAW_SWEBENCH_CONTAINER_DISK_GB", 20, 4, 100)}G'
            ),
        }
    )
    argv = [
        sys.executable,
        "-m",
        "puddingclaw_swebench_entry",
        "--dataset_name",
        str(fixture_path),
        "--split",
        "test",
        "--predictions_path",
        str(predictions_path),
        "--max_workers",
        str(max_workers),
        "--timeout",
        str(test_timeout),
        "--run_id",
        run_id,
        "--namespace",
        _namespace(),
        "--cache_level",
        "env",
        "--clean",
        "false",
        "--report_dir",
        str(harness_root),
    ]
    process = await _run_process(
        argv,
        cwd=harness_root,
        environment=environment,
        timeout_seconds=job_timeout,
        log_path=harness_root / "harness.log",
        isolate_process_group=True,
        pid_path=harness_root / "harness.pid",
    )
    if process.timed_out or process.exit_code != 0:
        await _cleanup_run_containers(run_id, environment, harness_root)
    finished_at = datetime.now(UTC)
    report_candidates = list(harness_root.glob(f"*.{run_id}.json"))
    aggregate: dict[str, Any] = {}
    report_sha256: str | None = None
    parse_error: str | None = None
    if len(report_candidates) == 1:
        try:
            aggregate, report_sha256 = _read_report(report_candidates[0])
        except (OSError, ValueError) as exc:
            parse_error = f"{type(exc).__name__}: {str(exc)[:500]}"
    else:
        parse_error = f"Expected one aggregate report, found {len(report_candidates)}"

    resolved_ids = {str(item) for item in aggregate.get("resolved_ids") or []}
    unresolved_ids = {str(item) for item in aggregate.get("unresolved_ids") or []}
    error_ids = {str(item) for item in aggregate.get("error_ids") or []}
    completed_ids = {str(item) for item in aggregate.get("completed_ids") or []}
    report_valid = (
        not parse_error
        and not (resolved_ids & unresolved_ids)
        and completed_ids == resolved_ids | unresolved_ids
        and expected_ids == completed_ids | error_ids
        and not ((resolved_ids | unresolved_ids | error_ids) - expected_ids)
    )
    if not report_valid and parse_error is None:
        parse_error = "Official aggregate report has inconsistent or incomplete instance sets"
    results: dict[str, dict[str, Any]] = {}
    for instance_id in sorted(expected_ids):
        if process.exit_code == 0 and report_valid and instance_id in resolved_ids:
            results[instance_id] = {"status": "passed", "resolved": True, "reason": "Official SWE-bench Harness resolved the instance"}
        elif process.exit_code == 0 and report_valid and instance_id in unresolved_ids:
            results[instance_id] = {"status": "failed", "resolved": False, "reason": "Official SWE-bench Harness did not resolve the instance"}
        else:
            reason = (
                "Official SWE-bench Harness timed out"
                if process.timed_out
                else parse_error
                or "Official SWE-bench Harness could not complete the instance"
            )
            results[instance_id] = {
                "status": "error",
                "resolved": None,
                "reason": reason,
                "reported_error": instance_id in error_ids or instance_id not in completed_ids,
            }
    overall_status = "completed" if all(item["status"] in {"passed", "failed"} for item in results.values()) else "error"
    receipt = {
        **base_receipt,
        "status": overall_status,
        "docker_server_version": docker_detail,
        "namespace": _namespace(),
        "test_timeout_seconds": test_timeout,
        "job_timeout_seconds": job_timeout,
        "max_workers": max_workers,
        "container_policy": {
            "network": "none",
            "cap_drop": ["ALL"],
            "no_new_privileges": True,
            "memory": environment["PUDDINGCLAW_SWEBENCH_CONTAINER_MEMORY"],
            "cpus": int(environment["PUDDINGCLAW_SWEBENCH_CONTAINER_NANO_CPUS"]) / 1_000_000_000,
            "pids": int(environment["PUDDINGCLAW_SWEBENCH_CONTAINER_PIDS"]),
            "writable_layer": environment["PUDDINGCLAW_SWEBENCH_CONTAINER_DISK"],
        },
        "run_id": run_id,
        "exit_code": process.exit_code,
        "timed_out": process.timed_out,
        "finished_at": finished_at.isoformat(),
        "duration_seconds": (finished_at - started_at).total_seconds(),
        "report_sha256": report_sha256,
        "aggregate": aggregate,
        "output_tail": process.output_tail[-20_000:],
    }
    return {
        "status": overall_status,
        "reason": "Official SWE-bench Harness completed" if overall_status == "completed" else "Official SWE-bench Harness completed with infrastructure errors",
        "results": results,
        "receipt": receipt,
    }
