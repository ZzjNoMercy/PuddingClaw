"""Execution-capable workspace backends for Docker and restricted host fallback."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import subprocess
import threading
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from deepagents.backends import FilesystemBackend
from deepagents.backends.protocol import ExecuteResponse, SandboxBackendProtocol


def _bounded_output(
    stdout: str,
    stderr: str,
    *,
    max_output_bytes: int,
) -> tuple[str, bool]:
    parts: list[str] = []
    if stdout:
        parts.append(stdout)
    if stderr:
        parts.extend(f"[stderr] {line}" for line in stderr.strip().splitlines())
    output = "\n".join(parts) if parts else "<no output>"
    encoded = output.encode("utf-8", errors="replace")
    if len(encoded) <= max_output_bytes:
        return output, False
    truncated = encoded[:max_output_bytes].decode("utf-8", errors="ignore")
    return f"{truncated}\n\n... Output truncated at {max_output_bytes} bytes.", True


class RestrictedHostWorkspaceBackend(FilesystemBackend, SandboxBackendProtocol):
    """Best-effort host fallback whose commands still require Tool policy."""

    mode = "restricted_host"
    _workspace_locks: dict[str, threading.RLock] = {}
    _workspace_locks_guard = threading.Lock()

    def __init__(
        self,
        *,
        root_dir: Path,
        timeout: int = 120,
        max_output_bytes: int = 100_000,
    ) -> None:
        super().__init__(root_dir=root_dir, virtual_mode=True)
        self.workspace_path = root_dir.expanduser().resolve()
        self._default_timeout = timeout
        self._max_output_bytes = max_output_bytes
        self._id = f"restricted-host-{uuid.uuid4().hex[:8]}"
        runtime_dir = self.workspace_path / ".puddingclaw" / "runtime"
        runtime_dir.mkdir(parents=True, exist_ok=True)
        self._env = {
            "PATH": os.environ.get(
                "PATH",
                "/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin",
            ),
            "HOME": str(runtime_dir / "host-home"),
            "TMPDIR": str(runtime_dir / "tmp"),
            "LANG": os.environ.get("LANG", "C.UTF-8"),
            "LC_ALL": os.environ.get("LC_ALL", ""),
        }
        Path(self._env["HOME"]).mkdir(parents=True, exist_ok=True)
        Path(self._env["TMPDIR"]).mkdir(parents=True, exist_ok=True)

    @property
    def id(self) -> str:
        return self._id

    def execute(
        self,
        command: str,
        *,
        timeout: int | None = None,
    ) -> ExecuteResponse:
        if not isinstance(command, str) or not command.strip():
            return ExecuteResponse(
                output="Error: Command must be a non-empty string.",
                exit_code=1,
            )
        command = re.sub(
            r"(^|\s)/workspace(?=(/|\s|$))",
            lambda match: f"{match.group(1)}{shlex.quote(str(self.workspace_path))}",
            command,
        )
        effective_timeout = timeout if timeout is not None else self._default_timeout
        if not isinstance(effective_timeout, int) or effective_timeout <= 0:
            raise ValueError("timeout must be a positive integer")
        try:
            with self._workspace_lock(str(self.workspace_path)):
                result = subprocess.run(  # noqa: S602
                    command,
                    check=False,
                    shell=True,
                    cwd=self.workspace_path,
                    env={key: value for key, value in self._env.items() if value},
                    capture_output=True,
                    text=True,
                    stdin=subprocess.DEVNULL,
                    timeout=effective_timeout,
                    start_new_session=True,
                )
            output, truncated = _bounded_output(
                result.stdout,
                result.stderr,
                max_output_bytes=self._max_output_bytes,
            )
            if result.returncode != 0:
                output = f"{output.rstrip()}\n\nExit code: {result.returncode}"
            return ExecuteResponse(
                output=output,
                exit_code=result.returncode,
                truncated=truncated,
            )
        except subprocess.TimeoutExpired:
            return ExecuteResponse(
                output=f"Error: Command timed out after {effective_timeout} seconds.",
                exit_code=124,
            )
        except Exception as exc:  # noqa: BLE001
            return ExecuteResponse(
                output=f"Error executing command ({type(exc).__name__}): {exc}",
                exit_code=1,
            )

    @classmethod
    def _workspace_lock(cls, key: str) -> threading.RLock:
        with cls._workspace_locks_guard:
            return cls._workspace_locks.setdefault(key, threading.RLock())


class ProjectSandboxManager:
    """Create and reuse one hardened Docker container per project workspace."""

    _locks: dict[str, threading.RLock] = {}
    _locks_guard = threading.Lock()
    _idle_timers: dict[str, tuple[str, threading.Timer]] = {}
    _idle_timers_guard = threading.Lock()

    def __init__(self, docker_config: dict[str, Any]) -> None:
        self.config = dict(docker_config)

    def _docker_prefix(self) -> list[str]:
        command = ["docker"]
        context = str(self.config.get("context") or "").strip()
        if context:
            command.extend(["--context", context])
        return command

    def _env(self) -> dict[str, str]:
        env = os.environ.copy()
        connection = str(self.config.get("connection") or "").strip()
        if connection:
            env["DOCKER_HOST"] = connection
        return env

    def _run(
        self,
        args: list[str],
        *,
        timeout: int = 30,
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [*self._docker_prefix(), *args],
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=self._env(),
        )

    def probe(self) -> tuple[bool, str]:
        try:
            result = self._run(["version", "--format", "{{.Server.Version}}"])
        except Exception as exc:  # noqa: BLE001
            return False, f"{type(exc).__name__}: {exc}"
        if result.returncode != 0:
            return False, result.stderr.strip() or result.stdout.strip()
        return True, result.stdout.strip()

    @staticmethod
    def _container_name(workspace: Path) -> str:
        digest = hashlib.sha256(str(workspace).encode("utf-8")).hexdigest()[:16]
        return f"puddingclaw-{digest}"

    def _spec(self, workspace: Path) -> dict[str, Any]:
        return {
            "workspace": str(workspace),
            "image": str(self.config.get("image") or "python:3.12-slim"),
            "network_enabled": bool(self.config.get("network_enabled", False)),
            "cpu_limit": str(self.config.get("cpu_limit") or "2"),
            "memory_limit_mb": int(self.config.get("memory_limit_mb") or 2048),
            "pids_limit": int(self.config.get("pids_limit") or 256),
            "uid": os.getuid() if hasattr(os, "getuid") else None,
            "gid": os.getgid() if hasattr(os, "getgid") else None,
        }

    def ensure_container(self, workspace: Path) -> tuple[str, str]:
        workspace = workspace.expanduser().resolve()
        name = self._container_name(workspace)
        spec = self._spec(workspace)
        spec_hash = hashlib.sha256(
            json.dumps(spec, sort_keys=True).encode("utf-8")
        ).hexdigest()
        with self._lock(name):
            inspect = self._run(
                [
                    "inspect",
                    "--format",
                    '{{index .Config.Labels "com.puddingclaw.spec-hash"}} {{.State.Running}}',
                    name,
                ]
            )
            if inspect.returncode == 0:
                existing_hash, _, running = inspect.stdout.strip().partition(" ")
                if existing_hash != spec_hash:
                    removed = self._run(["rm", "-f", name])
                    if removed.returncode != 0:
                        raise RuntimeError(
                            removed.stderr.strip() or "failed to replace Docker sandbox"
                        )
                elif running.strip().lower() == "true":
                    self.mark_activity(name)
                    return name, spec_hash
                else:
                    started = self._run(["start", name])
                    if started.returncode != 0:
                        raise RuntimeError(
                            started.stderr.strip() or "failed to start Docker sandbox"
                        )
                    self.mark_activity(name)
                    return name, spec_hash

            create_args = [
                "create",
                "--name",
                name,
                "--label",
                "com.puddingclaw.managed=true",
                "--label",
                f"com.puddingclaw.spec-hash={spec_hash}",
                "--workdir",
                "/workspace",
                "--mount",
                f"type=bind,src={workspace},dst=/workspace",
                "--cap-drop",
                "ALL",
                "--security-opt",
                "no-new-privileges",
                "--pids-limit",
                str(spec["pids_limit"]),
                "--memory",
                f"{spec['memory_limit_mb']}m",
                "--cpus",
                spec["cpu_limit"],
                "--network",
                "bridge" if spec["network_enabled"] else "none",
            ]
            if spec["uid"] is not None and spec["gid"] is not None:
                create_args.extend(["--user", f"{spec['uid']}:{spec['gid']}"])
            create_args.extend(
                [
                    spec["image"],
                    "sh",
                    "-lc",
                    "trap : TERM INT; sleep infinity & wait",
                ]
            )
            created = self._run(create_args, timeout=120)
            if created.returncode != 0:
                raise RuntimeError(
                    created.stderr.strip()
                    or created.stdout.strip()
                    or "failed to create Docker sandbox"
                )
            started = self._run(["start", name])
            if started.returncode != 0:
                raise RuntimeError(
                    started.stderr.strip() or "failed to start Docker sandbox"
                )
            self.mark_activity(name)
            return name, spec_hash

    def mark_activity(self, container_name: str) -> None:
        """Re-arm the project container's idle-stop timer after use."""

        idle_minutes = self.config.get("idle_stop_minutes", 30)
        if (
            not isinstance(idle_minutes, int)
            or isinstance(idle_minutes, bool)
            or idle_minutes <= 0
        ):
            return
        generation = uuid.uuid4().hex
        timer = threading.Timer(
            idle_minutes * 60,
            self._stop_if_current_generation,
            args=(container_name, generation),
        )
        timer.daemon = True
        with self._idle_timers_guard:
            previous = self._idle_timers.get(container_name)
            self._idle_timers[container_name] = (generation, timer)
        if previous is not None:
            previous[1].cancel()
        timer.start()

    def _stop_if_current_generation(
        self,
        container_name: str,
        generation: str,
    ) -> None:
        with self._idle_timers_guard:
            current = self._idle_timers.get(container_name)
            if current is None or current[0] != generation:
                return
            self._idle_timers.pop(container_name, None)
        with self._lock(container_name):
            self._run(["stop", "--time", "10", container_name], timeout=30)

    @classmethod
    def _lock(cls, key: str) -> threading.RLock:
        with cls._locks_guard:
            return cls._locks.setdefault(key, threading.RLock())


class DockerWorkspaceBackend(FilesystemBackend, SandboxBackendProtocol):
    """FilesystemBackend plus command execution inside a project container."""

    mode = "docker"

    def __init__(
        self,
        *,
        root_dir: Path,
        manager: ProjectSandboxManager,
        timeout: int = 120,
        max_output_bytes: int = 100_000,
    ) -> None:
        super().__init__(root_dir=root_dir, virtual_mode=True)
        self.workspace_path = root_dir.expanduser().resolve()
        self.manager = manager
        self._default_timeout = timeout
        self._max_output_bytes = max_output_bytes
        self.container_name, self.spec_hash = manager.ensure_container(
            self.workspace_path
        )

    @property
    def id(self) -> str:
        return f"docker:{self.container_name}:{self.spec_hash[:12]}"

    def execute(
        self,
        command: str,
        *,
        timeout: int | None = None,
    ) -> ExecuteResponse:
        effective_timeout = timeout if timeout is not None else self._default_timeout
        if not isinstance(effective_timeout, int) or effective_timeout <= 0:
            raise ValueError("timeout must be a positive integer")
        try:
            with self.manager._lock(self.container_name):
                # An idle timer may stop a project container between Runs.
                # Reconcile it under the same keyed lock used by lifecycle
                # cleanup, then serialize commands for this project.
                self.container_name, self.spec_hash = self.manager.ensure_container(
                    self.workspace_path
                )
                result = self.manager._run(
                    [
                        "exec",
                        "--workdir",
                        "/workspace",
                        self.container_name,
                        "sh",
                        "-lc",
                        command,
                    ],
                    timeout=effective_timeout,
                )
                self.manager.mark_activity(self.container_name)
            output, truncated = _bounded_output(
                result.stdout,
                result.stderr,
                max_output_bytes=self._max_output_bytes,
            )
            if result.returncode != 0:
                output = f"{output.rstrip()}\n\nExit code: {result.returncode}"
            return ExecuteResponse(
                output=output,
                exit_code=result.returncode,
                truncated=truncated,
            )
        except subprocess.TimeoutExpired:
            return ExecuteResponse(
                output=f"Error: Command timed out after {effective_timeout} seconds.",
                exit_code=124,
            )
        except Exception as exc:  # noqa: BLE001
            return ExecuteResponse(
                output=f"Error executing command ({type(exc).__name__}): {exc}",
                exit_code=1,
            )


@dataclass(frozen=True)
class WorkspaceBackendSelection:
    backend: RestrictedHostWorkspaceBackend | DockerWorkspaceBackend
    mode: str
    fallback_reason: str | None = None


def build_workspace_execution_backend(
    workspace_path: Path,
    terminal_config: dict[str, Any],
) -> WorkspaceBackendSelection:
    """Select Docker when explicitly enabled, otherwise controlled host fallback."""

    timeout = int(terminal_config.get("default_timeout_seconds") or 120)
    docker_enabled = bool(terminal_config.get("docker_enabled", False))
    docker_config = dict(terminal_config.get("docker") or {})
    if docker_enabled:
        manager = ProjectSandboxManager(docker_config)
        available, reason = manager.probe()
        if available:
            return WorkspaceBackendSelection(
                backend=DockerWorkspaceBackend(
                    root_dir=workspace_path,
                    manager=manager,
                    timeout=timeout,
                ),
                mode="docker",
            )
        if str(terminal_config.get("on_unavailable") or "fallback") == "deny":
            raise RuntimeError(f"Docker sandbox is unavailable: {reason}")
        return WorkspaceBackendSelection(
            backend=RestrictedHostWorkspaceBackend(
                root_dir=workspace_path,
                timeout=timeout,
            ),
            mode="restricted_host",
            fallback_reason=reason,
        )
    return WorkspaceBackendSelection(
        backend=RestrictedHostWorkspaceBackend(
            root_dir=workspace_path,
            timeout=timeout,
        ),
        mode="restricted_host",
    )
