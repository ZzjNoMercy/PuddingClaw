"""SWE-bench candidate execution through the production Harness contract.

The Agent still runs through ``DeepAgentsAgentManager``.  Only its workspace
execution backend is replaced with the official per-instance SWE-bench image,
so ``execute`` sees the repository dependencies that a real coding session
would have while the untrusted checkout remains outside the host process.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import platform
import posixpath
import queue
import shlex
import shutil
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

from deepagents.backends import FilesystemBackend
from deepagents.backends.protocol import ExecuteResponse, SandboxBackendProtocol

from .code_eval import (
    MAX_WORKSPACE_BYTES,
    MAX_WORKSPACE_FILES,
    _git_control_dir,
    _run_git,
    _validate_workspace_budget,
)
from .contracts import EvalCase
from .official_swebench import (
    _bounded_int,
    _bounded_memory,
    _docker_backend_is_approved,
    _namespace,
    _official_environment,
    _run_process,
)

MAX_AGENT_EXECUTE_OUTPUT = 200_000
AGENT_UID_GID = "65532:65532"


def _architecture() -> str:
    return "arm64" if platform.machine().lower() in {"arm64", "aarch64"} else "x86_64"


def _instance_payload(case: EvalCase) -> dict[str, Any]:
    reference = case.code.repository.swebench if case.code is not None else None
    if reference is None:
        raise ValueError("SWE-bench Agent backend requires a frozen SWE-bench reference")
    return {
        "instance_id": reference.instance_id,
        "repo": reference.repo,
        "version": reference.version,
        "base_commit": reference.base_commit,
        "problem_statement": case.input.message or "",
        "hints_text": str(case.metadata.get("hints_text") or ""),
        "test_patch": reference.test_patch,
        "FAIL_TO_PASS": list(reference.fail_to_pass),
        "PASS_TO_PASS": list(reference.pass_to_pass),
    }


def _make_test_spec(payload: dict[str, Any]):
    from swebench.harness.test_spec.test_spec import make_test_spec

    namespace_value = _namespace()
    namespace = None if namespace_value == "none" else namespace_value
    return make_test_spec(payload, namespace=namespace, arch=_architecture())


def ensure_swebench_instance_image_payload(payload: dict[str, Any]) -> None:
    """Ensure one image in an isolated provisioning process."""

    if not _docker_backend_is_approved():
        raise RuntimeError("SWE-bench Agent environment requires an approved isolated Docker backend")
    try:
        import docker
        from docker.errors import ImageNotFound
        from swebench.harness.docker_build import build_instance_images
    except ImportError as exc:
        raise RuntimeError("Official SWE-bench runtime is not installed") from exc

    namespace_value = _namespace()
    namespace = None if namespace_value == "none" else namespace_value
    spec = _make_test_spec(payload)
    client = docker.from_env(timeout=60)
    try:
        client.images.get(spec.instance_image_key)
    except ImageNotFound:
        if namespace is not None:
            client.images.pull(spec.instance_image_key)
        else:
            _successful, failed = build_instance_images(
                client,
                [spec],
                force_rebuild=False,
                max_workers=1,
                namespace=namespace,
            )
            if failed:
                raise RuntimeError(
                    "Official SWE-bench instance image preparation failed: "
                    + ", ".join(str(item) for item in failed[:5])
                )
            # SWE-bench 4.1 returns the successful thread-pool payloads
            # (tuples containing TestSpec + DockerClient), not image-name
            # strings. The Docker daemon is the authoritative postcondition.
            try:
                client.images.get(spec.instance_image_key)
            except ImageNotFound as exc:
                raise RuntimeError(
                    "Official SWE-bench image builder reported success but the instance image is missing: "
                    + spec.instance_image_key
                ) from exc
    finally:
        client.close()


async def ensure_swebench_instance_image(
    case: EvalCase,
    *,
    runtime_path: Path,
    experiment_id: str,
) -> Any:
    """Prepare an image behind a killable process-group and hard deadline."""

    runtime_path.mkdir(parents=True, exist_ok=True)
    payload_path = runtime_path / "test-spec-input.json"
    payload_path.write_text(
        json.dumps(_instance_payload(case), ensure_ascii=False, separators=(",", ":")),
        encoding="utf-8",
    )
    os.chmod(payload_path, 0o600)
    run_id = f"puddingclaw-{experiment_id}"
    environment = {
        **_official_environment(),
        "PYTHONPATH": str(Path(__file__).resolve().parents[1]),
        "PUDDINGCLAW_SWEBENCH_RUN_ID": run_id,
    }
    for name in (
        "PUDDINGCLAW_SWEBENCH_ISOLATED_DOCKER",
        "PUDDINGCLAW_SWEBENCH_NAMESPACE",
    ):
        if name in os.environ:
            environment[name] = os.environ[name]
    timeout_seconds = _bounded_int("PUDDINGCLAW_SWEBENCH_IMAGE_TIMEOUT_SECONDS", 1800, 60, 3600)
    try:
        result = await _run_process(
            [
                sys.executable,
                "-m",
                "evaluation.swebench_agent_backend",
                "--prepare-image",
                str(payload_path),
                "--run-id",
                run_id,
            ],
            cwd=Path(__file__).resolve().parents[1],
            environment=environment,
            timeout_seconds=timeout_seconds,
            log_path=runtime_path / "image-prepare.log",
            isolate_process_group=True,
            pid_path=runtime_path / "candidate-image.pid",
        )
        if result.exit_code != 0 or result.timed_out:
            reason = "timed out" if result.timed_out else f"exited with {result.exit_code}"
            raise RuntimeError(
                f"Official SWE-bench Agent image preparation {reason}: {result.output_tail[-1000:]}"
            )
        return _make_test_spec(_instance_payload(case))
    finally:
        payload_path.unlink(missing_ok=True)


class SWEbenchAgentWorkspaceBackend(FilesystemBackend, SandboxBackendProtocol):
    """Host file protocol plus dependency-complete Docker ``execute``."""

    # The PuddingClaw permission compiler understands spawn/kernel semantics.
    # This backend implements the kernel contract with Docker as its isolation
    # mechanism; it never grants host-spawn authority.
    mode = "kernel"
    dependency_plan = None

    def __init__(
        self,
        *,
        workspace_path: Path,
        scratch_path: Path,
        test_spec: Any,
        base_commit: str,
        experiment_id: str,
    ) -> None:
        super().__init__(root_dir=workspace_path, virtual_mode=True)
        self.workspace_path = workspace_path.expanduser().resolve()
        self.scratch_path = scratch_path.expanduser().resolve()
        self.scratch_path.mkdir(parents=True, exist_ok=True)
        self.test_spec = test_spec
        self._base_commit = base_commit
        self.experiment_id = experiment_id
        self._container: Any | None = None
        self._container_id: str | None = None
        self._image_id: str | None = None
        self._start()

    @property
    def id(self) -> str:
        image_id = str(self._image_id or "unavailable").removeprefix("sha256:")[:16]
        return f"swebench-docker:{self.test_spec.instance_id}:image-{image_id}"

    @property
    def base_commit(self) -> str:
        return self._base_commit

    @property
    def kernel_runner_mode(self) -> str:
        return "kernel_swebench_docker"

    @property
    def kernel_runner_binding_digest(self) -> str:
        policy = {
            "runner": self.kernel_runner_mode,
            "docker_host": os.getenv("DOCKER_HOST", "docker-context-default"),
            "container_id": self._container_id,
            "image_id": self._image_id,
            "base_commit": self.base_commit,
            "workspace": str(self.workspace_path),
            "scratch": str(self.scratch_path),
            "network": "none",
            "cap_drop": "ALL",
            "platform_sync_cap_add": "CHOWN",
            "candidate_home": "/scratch/home",
            "conda_plugins": "disabled",
            "no_new_privileges": True,
            "memory": _bounded_memory(),
            "cpus": _bounded_int("PUDDINGCLAW_SWEBENCH_CONTAINER_CPUS", 4, 1, 16),
            "pids": _bounded_int("PUDDINGCLAW_SWEBENCH_CONTAINER_PIDS", 1024, 64, 4096),
            "workspace_layer_gb": _bounded_int("PUDDINGCLAW_SWEBENCH_AGENT_WORKSPACE_GB", 2, 1, 2),
        }
        return "sha256:" + hashlib.sha256(
            json.dumps(policy, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()

    def _start(self) -> None:
        import docker

        client = docker.from_env()
        run_id = f"puddingclaw-{self.experiment_id}"
        try:
            image = client.images.get(self.test_spec.instance_image_key)
            self._image_id = str(image.id)
            container = client.containers.create(
                self.test_spec.instance_image_key,
                command=["bash", "-lc", "trap : TERM INT; sleep infinity & wait"],
                detach=True,
                working_dir="/workspace",
                network_disabled=True,
                # Trusted root synchronization needs CHOWN to hand the
                # quota-backed tree to the unprivileged candidate UID. The
                # candidate itself runs as UID 65532 and receives no effective
                # capability; every other capability remains dropped.
                cap_add=["CHOWN"],
                cap_drop=["ALL"],
                security_opt=["no-new-privileges:true"],
                mem_limit=_bounded_memory(),
                nano_cpus=_bounded_int("PUDDINGCLAW_SWEBENCH_CONTAINER_CPUS", 4, 1, 16)
                * 1_000_000_000,
                pids_limit=_bounded_int("PUDDINGCLAW_SWEBENCH_CONTAINER_PIDS", 1024, 64, 4096),
                # The source tree used by shell commands lives in the writable
                # layer (not a host RW bind), so this is a real hard quota.
                storage_opt={
                    "size": f'{_bounded_int("PUDDINGCLAW_SWEBENCH_AGENT_WORKSPACE_GB", 2, 1, 2)}G'
                },
                init=True,
                read_only=False,
                tmpfs={
                    "/tmp": "rw,nosuid,nodev,size=512m",
                    "/root/.cache": "rw,nosuid,nodev,size=512m",
                    "/scratch": "rw,nosuid,nodev,size=512m",
                },
                mounts=[],
                labels={
                    "com.puddingclaw.managed": "true",
                    "com.puddingclaw.kind": "swebench-agent",
                    "com.puddingclaw.swebench.run_id": run_id,
                },
                environment={
                    # The candidate UID must never inherit root's unreadable
                    # config/cache paths. Keep all user state in bounded tmpfs.
                    "HOME": "/scratch/home",
                    "XDG_CONFIG_HOME": "/scratch/home/.config",
                    "XDG_CACHE_HOME": "/scratch/home/.cache",
                    "CONDA_NO_PLUGINS": "true",
                    "PUDDINGCLAW_EVALUATION": "1",
                    "GIT_OPTIONAL_LOCKS": "0",
                    "GIT_CONFIG_COUNT": "1",
                    "GIT_CONFIG_KEY_0": "safe.directory",
                    "GIT_CONFIG_VALUE_0": "/workspace",
                },
                platform=self.test_spec.platform,
            )
            self._container = container
            self._container_id = str(container.id)
            container.start()
            self._sync_host_to_container()
            probe = self._execute_raw(
                "python -c 'import sys; print(sys.executable)'",
                timeout=30,
                sync_back=False,
            )
            if probe.exit_code != 0:
                raise RuntimeError(f"SWE-bench Agent environment probe failed: {probe.output[-500:]}")
        except Exception:
            self.close()
            raise
        finally:
            client.close()

    def _docker_cli_run(self, argv: list[str], *, timeout: int = 120) -> subprocess.CompletedProcess[bytes]:
        docker_cli = shutil.which("docker")
        if docker_cli is None:
            raise RuntimeError("Docker CLI is unavailable")
        return subprocess.run(
            [docker_cli, *argv],
            capture_output=True,
            timeout=timeout,
            check=False,
        )

    def _trusted_container_command(self, command: str, *, timeout: int = 120) -> None:
        if self._container_id is None:
            raise RuntimeError("SWE-bench Agent container is unavailable")
        result = self._docker_cli_run(
            ["exec", "--user", "0:0", self._container_id, "bash", "-lc", command],
            timeout=timeout,
        )
        if result.returncode != 0:
            detail = (result.stderr or result.stdout)[-1000:].decode("utf-8", errors="replace")
            raise RuntimeError(f"Candidate workspace synchronization failed: {detail}")

    def _sync_host_to_container(self) -> None:
        if self._container_id is None:
            raise RuntimeError("SWE-bench Agent container is unavailable")
        _validate_workspace_budget(self.workspace_path)
        base_commit = shlex.quote(self.base_commit)
        self._trusted_container_command(
            "chown -R 0:0 /testbed /scratch && "
            f"git -c safe.directory=/testbed -C /testbed reset --hard {base_commit} && "
            f"test \"$(git -c safe.directory=/testbed -C /testbed rev-parse HEAD)\" = {base_commit} && "
            "git -c safe.directory=/testbed -C /testbed clean -fd && "
            "find /scratch -mindepth 1 -delete && "
            "mkdir -p /scratch/home/.config /scratch/home/.cache && "
            "rm -rf /workspace && ln -s /testbed /workspace"
        )
        for source, target in (
            (self.workspace_path, "/testbed"),
            (self.scratch_path, "/scratch"),
        ):
            result = self._docker_cli_run(
                ["cp", f"{source}/.", f"{self._container_id}:{target}"],
                timeout=180,
            )
            if result.returncode != 0:
                detail = (result.stderr or result.stdout)[-1000:].decode("utf-8", errors="replace")
                raise RuntimeError(f"Could not materialize candidate workspace: {detail}")
        from .code_eval import _run_repository_git

        deleted = [
            path
            for path in _run_repository_git(
                self.workspace_path,
                "diff",
                "--name-only",
                "--diff-filter=D",
                "-z",
                "HEAD",
            ).stdout.split("\0")
            if path
        ]
        if deleted:
            quoted = " ".join(shlex.quote(f"/testbed/{path}") for path in deleted)
            self._trusted_container_command(f"rm -f -- {quoted}")
        self._trusted_container_command(f"chown -R {AGENT_UID_GID} /testbed /scratch")

    @staticmethod
    def _validate_archive_member(member: tarfile.TarInfo, *, source: str) -> None:
        path = Path(member.name)
        if path.is_absolute() or ".." in path.parts:
            raise RuntimeError("Candidate archive contains a path escape")
        if member.isdev() or member.isfifo():
            raise RuntimeError("Candidate archive contains a special file")
        if member.issym():
            if posixpath.isabs(member.linkname):
                source_root = posixpath.normpath(source)
                absolute_target = posixpath.normpath(member.linkname)
                if absolute_target == source_root:
                    archive_target = "."
                elif absolute_target.startswith(source_root.rstrip("/") + "/"):
                    archive_target = posixpath.relpath(absolute_target, source_root)
                else:
                    raise RuntimeError("Candidate archive contains an escaping link")
                # The container may create an absolute link which is still
                # internal to the exported tree (Astropy does this for
                # /scratch/home/.config/astropy).  Rebase it to an equivalent
                # relative link before host extraction so it remains internal
                # to the bounded materialization root.
                member.linkname = posixpath.relpath(
                    archive_target,
                    posixpath.dirname(posixpath.normpath(member.name)) or ".",
                )
            linked = posixpath.normpath(
                posixpath.join(posixpath.dirname(member.name), member.linkname)
            )
            if linked == ".." or linked.startswith("../"):
                raise RuntimeError("Candidate archive contains an escaping link")
        elif member.islnk():
            linked = posixpath.normpath(member.linkname)
            if posixpath.isabs(member.linkname):
                source_root = posixpath.normpath(source)
                if linked == source_root:
                    linked = "."
                elif linked.startswith(source_root.rstrip("/") + "/"):
                    linked = posixpath.relpath(linked, source_root)
                else:
                    raise RuntimeError("Candidate archive contains an escaping link")
                member.linkname = linked
            if linked == ".." or linked.startswith("../"):
                raise RuntimeError("Candidate archive contains an escaping link")
        elif not (member.isfile() or member.isdir()):
            raise RuntimeError("Candidate archive contains an unsupported file type")

    def _stream_container_tree(
        self,
        source: str,
        target: Path,
        *,
        max_bytes: int,
        max_files: int,
    ) -> None:
        if self._container_id is None:
            raise RuntimeError("SWE-bench Agent container is unavailable")
        docker_cli = shutil.which("docker")
        if docker_cli is None:
            raise RuntimeError("Docker CLI is unavailable")
        process = subprocess.Popen(
            [
                docker_cli,
                "exec",
                "--user",
                "0:0",
                self._container_id,
                "/bin/tar",
                "--numeric-owner",
                "-C",
                source,
                "-cf",
                "-",
                ".",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            start_new_session=os.name != "nt",
        )
        files = 0
        declared_bytes = 0
        try:
            assert process.stdout is not None
            with tarfile.open(fileobj=process.stdout, mode="r|") as archive:
                for member in archive:
                    self._validate_archive_member(member, source=source)
                    files += 1
                    declared_bytes += max(0, int(member.size))
                    if files > max_files or declared_bytes > max_bytes:
                        raise RuntimeError("Candidate archive exceeded its materialization budget")
                    if shutil.disk_usage(target).free < max_bytes + 1024**3:
                        raise RuntimeError("Worker disk reserve was reached during candidate materialization")
                    archive.extract(member, path=target, filter="data")
            return_code = process.wait(timeout=10)
            if return_code != 0:
                raise RuntimeError(f"Candidate archive process exited with {return_code}")
        except BaseException:
            try:
                process.terminate()
                process.wait(timeout=5)
            except (OSError, subprocess.TimeoutExpired):
                try:
                    process.kill()
                except OSError:
                    pass
            raise

    def _sync_container_to_host(self) -> None:
        if self._container_id is None:
            raise RuntimeError("SWE-bench Agent container is unavailable")
        required_free = 2 * MAX_WORKSPACE_BYTES + 1024**3
        if shutil.disk_usage(self.workspace_path).free < required_free:
            raise RuntimeError("Insufficient disk reserve to materialize the bounded candidate workspace")
        # Candidate commands run as a dedicated unprivileged UID. Stop all of
        # its descendants before taking a deterministic archive snapshot.
        self._trusted_container_command(f"pkill -KILL -U {AGENT_UID_GID.split(':', 1)[0]} || true")
        with tempfile.TemporaryDirectory(
            prefix="puddingclaw-swebench-sync-",
            dir=self.workspace_path.parent,
        ) as temporary:
            temporary_root = Path(temporary)
            extracted_workspace = temporary_root / "workspace"
            extracted_scratch = temporary_root / "scratch"
            extracted_workspace.mkdir()
            extracted_scratch.mkdir()
            self._stream_container_tree(
                "/testbed",
                extracted_workspace,
                max_bytes=MAX_WORKSPACE_BYTES,
                max_files=MAX_WORKSPACE_FILES,
            )
            self._stream_container_tree(
                "/scratch",
                extracted_scratch,
                max_bytes=512 * 1024 * 1024,
                max_files=MAX_WORKSPACE_FILES,
            )
            container_git = extracted_workspace / ".git"
            if container_git.is_symlink() or container_git.is_file():
                container_git.unlink()
            elif container_git.is_dir():
                shutil.rmtree(container_git)
            # Compiled/install artifacts already present in the official image
            # are useful in-container but are not candidate source. Do not
            # project ignored build products back into the host file mirror.
            _run_git(
                extracted_workspace,
                "--git-dir",
                str(_git_control_dir(self.workspace_path)),
                "--work-tree",
                str(extracted_workspace),
                "clean",
                "-fdX",
            )
            _validate_workspace_budget(extracted_workspace)
            scratch_bytes, scratch_files = self._host_workspace_usage_for_root(
                extracted_scratch,
                limit=512 * 1024 * 1024,
            )
            if scratch_bytes > 512 * 1024 * 1024 or scratch_files > MAX_WORKSPACE_FILES:
                raise RuntimeError("Candidate scratch exceeded its hard materialization budget")
            for target, extracted in (
                (self.workspace_path, extracted_workspace),
                (self.scratch_path, extracted_scratch),
            ):
                backup = target.with_name(f".{target.name}.sync-backup")
                if backup.exists():
                    shutil.rmtree(backup)
                target.rename(backup)
                try:
                    extracted.rename(target)
                except Exception:
                    backup.rename(target)
                    raise
                shutil.rmtree(backup)

    @staticmethod
    def _host_workspace_usage_for_root(root: Path, *, limit: int) -> tuple[int, int]:
        total = 0
        files = 0
        pending = [root]
        while pending:
            directory = pending.pop()
            with os.scandir(directory) as entries:
                for entry in entries:
                    if entry.is_dir(follow_symlinks=False):
                        pending.append(Path(entry.path))
                    else:
                        files += 1
                        total += entry.stat(follow_symlinks=False).st_size
                    if total > limit or files > MAX_WORKSPACE_FILES:
                        return total, files
        return total, files

    def _execute_raw(
        self,
        command: str,
        *,
        timeout: int | None = None,
        spawn_guard: Any | None = None,
        sync_back: bool = True,
    ) -> ExecuteResponse:
        if self._container_id is None:
            return ExecuteResponse(output="SWE-bench Agent container is unavailable", exit_code=126)
        docker_cli = shutil.which("docker")
        if docker_cli is None:
            return ExecuteResponse(output="Docker CLI is unavailable", exit_code=126)
        activation = (
            "source /opt/miniconda3/bin/activate && conda activate testbed "
            "&& cd /workspace "
            "&& ulimit -f 524288 "
            "&& "
        )
        if spawn_guard is not None and not bool(spawn_guard()):
            return ExecuteResponse(output="SWE-bench execute permit was invalid or already consumed", exit_code=126)
        try:
            process = subprocess.Popen(
                [
                    docker_cli,
                    "exec",
                    "--user",
                    AGENT_UID_GID,
                    self._container_id,
                    "bash",
                    "-lc",
                    activation + command,
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                start_new_session=os.name != "nt",
            )
        except OSError as exc:
            return ExecuteResponse(output=f"Could not start Docker exec: {exc}", exit_code=126)

        chunks: queue.Queue[bytes | None] = queue.Queue(maxsize=8)

        def read_output() -> None:
            assert process.stdout is not None
            try:
                while chunk := process.stdout.read(65_536):
                    chunks.put(chunk)
            finally:
                chunks.put(None)

        reader = threading.Thread(target=read_output, daemon=True)
        reader.start()
        output = bytearray()
        total_output = 0
        deadline = time.monotonic() + int(timeout or 120)
        failure: str | None = None
        while True:
            now = time.monotonic()
            if now >= deadline:
                failure = "Command timed out; the isolated candidate container was terminated."
                break
            try:
                chunk = chunks.get(timeout=min(0.1, max(0.01, deadline - now)))
            except queue.Empty:
                if process.poll() is not None and not reader.is_alive():
                    break
                continue
            if chunk is None:
                break
            total_output += len(chunk)
            remaining = MAX_AGENT_EXECUTE_OUTPUT - len(output)
            if remaining > 0:
                output.extend(chunk[:remaining])
            if total_output > MAX_AGENT_EXECUTE_OUTPUT:
                failure = "Command output exceeded the evaluation limit; the isolated container was terminated."
                break

        if failure is not None:
            self.close()
            try:
                process.terminate()
                process.wait(timeout=5)
            except (OSError, subprocess.TimeoutExpired):
                try:
                    process.kill()
                except OSError:
                    pass
            return ExecuteResponse(
                output=bytes(output).decode("utf-8", errors="replace") + "\n" + failure,
                exit_code=124 if "timed out" in failure else 125,
                truncated=total_output > MAX_AGENT_EXECUTE_OUTPUT,
            )
        try:
            return_code = process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.close()
            try:
                process.kill()
            except OSError:
                pass
            return ExecuteResponse(
                output=bytes(output).decode("utf-8", errors="replace")
                + "\nDocker exec did not terminate cleanly; the isolated container was terminated.",
                exit_code=124,
                truncated=total_output > MAX_AGENT_EXECUTE_OUTPUT,
            )
        if sync_back:
            try:
                self._sync_container_to_host()
            except Exception as exc:
                self.close()
                return ExecuteResponse(
                    output=bytes(output).decode("utf-8", errors="replace")
                    + f"\nCandidate workspace synchronization failed: {type(exc).__name__}: {exc}",
                    exit_code=125,
                    truncated=total_output > MAX_AGENT_EXECUTE_OUTPUT,
                )
        return ExecuteResponse(
            output=bytes(output).decode("utf-8", errors="replace"),
            exit_code=return_code,
            truncated=total_output > MAX_AGENT_EXECUTE_OUTPUT,
        )

    def execute(self, command: str, *, timeout: int | None = None) -> ExecuteResponse:
        from harness.execution_context import current_authorized_execution

        authorized = current_authorized_execution()
        binding_digest = self.kernel_runner_binding_digest
        if (
            authorized is None
            or authorized.profile.workspace_root != self.workspace_path
            or authorized.profile.scratch_root != self.scratch_path
            or not authorized.valid_at_spawn(
                command=command,
                selected_runner=self.kernel_runner_mode,
                runner_binding_digest=binding_digest,
            )
        ):
            return ExecuteResponse(
                output="SWE-bench execute permit is missing, stale, or bound to another command",
                exit_code=126,
            )
        try:
            # File tools edit the bounded host mirror. Materialize that exact
            # state into the quota-backed layer immediately before shell use.
            self._sync_host_to_container()
        except Exception as exc:
            self.close()
            return ExecuteResponse(
                output=f"Candidate workspace synchronization failed: {type(exc).__name__}: {exc}",
                exit_code=125,
            )
        return self._execute_raw(
            authorized.execution_command,
            timeout=timeout,
            spawn_guard=lambda: authorized.consume_at_spawn(
                command=command,
                selected_runner=self.kernel_runner_mode,
                runner_binding_digest=binding_digest,
            ),
        )

    def execute_external_directory(
        self,
        _directory_path: str,
        _command: str,
        *,
        timeout: int | None = None,
        writable: bool = False,
    ) -> ExecuteResponse:
        del timeout, writable
        return ExecuteResponse(output="External directories are not mounted in SWE-bench evaluation", exit_code=126)

    def run_html_report_e2e(self, _html_path: Path, *, timeout: int) -> ExecuteResponse:
        del timeout
        return ExecuteResponse(output="HTML browser validation is unavailable in SWE-bench evaluation", exit_code=126)

    def close(self) -> None:
        container_id = self._container_id
        self._container = None
        self._container_id = None
        if container_id is None:
            return
        docker_cli = shutil.which("docker")
        if docker_cli is not None:
            try:
                subprocess.run(
                    [docker_cli, "rm", "-f", container_id],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=30,
                    check=False,
                )
                return
            except (OSError, subprocess.TimeoutExpired):
                pass
        try:
            import docker

            client = docker.from_env()
            try:
                client.containers.get(container_id).remove(force=True)
            finally:
                client.close()
        except Exception:
            pass


async def prepare_swebench_agent_backend(
    case: EvalCase,
    *,
    workspace_path: Path,
    scratch_path: Path,
    experiment_id: str,
) -> SWEbenchAgentWorkspaceBackend:
    reference = case.code.repository.swebench if case.code is not None else None
    if reference is None:
        raise ValueError("SWE-bench reference is missing")
    spec = await ensure_swebench_instance_image(
        case,
        runtime_path=scratch_path.parent / "image-preparation",
        experiment_id=experiment_id,
    )
    async with asyncio.timeout(60):
        return await asyncio.to_thread(
            SWEbenchAgentWorkspaceBackend,
            workspace_path=workspace_path,
            scratch_path=scratch_path,
            test_spec=spec,
            base_commit=reference.base_commit,
            experiment_id=experiment_id,
        )


def _main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--prepare-image", type=Path, required=True)
    parser.add_argument("--run-id", required=True)
    args = parser.parse_args()
    if not args.run_id.startswith("puddingclaw-exp_"):
        raise ValueError("invalid managed SWE-bench run id")
    payload_path = args.prepare_image.resolve(strict=True)
    if payload_path.is_symlink() or payload_path.stat().st_size > 10 * 1024 * 1024:
        raise ValueError("invalid TestSpec input")
    payload = json.loads(payload_path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or "patch" in payload:
        raise ValueError("invalid or gold-bearing TestSpec input")
    ensure_swebench_instance_image_payload(payload)
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
