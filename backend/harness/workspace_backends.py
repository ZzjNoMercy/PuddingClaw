"""Execution-capable workspace backends for Docker and restricted host fallback."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shlex
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from deepagents.backends import FilesystemBackend
from deepagents.backends.protocol import ExecuteResponse, SandboxBackendProtocol

from harness.dependency_setup import (
    WorkspaceDependencyPlan,
    detect_workspace_dependency_plan,
)

DEFAULT_SANDBOX_IMAGE = "puddingclaw/sandbox:python3.12-node22-chromium-v4"
RUNTIME_CONTRACT = "python3.12+node22+chromium-v4"
logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ManagedProviderExecutionResult:
    output: str
    exit_code: int
    credential_state: bytes | None
    truncated: bool = False


def _canonical_docker_mount_source(value: str) -> str:
    """Normalize Docker Desktop's `/host_mnt/...` inspect projection."""

    normalized = str(value or "").replace("\\", "/")
    if normalized.startswith("/host_mnt/"):
        normalized = normalized.removeprefix("/host_mnt")
    try:
        return str(Path(normalized).resolve())
    except OSError:
        return normalized


def _reject_scratch_traversal(command: str) -> ExecuteResponse | None:
    """Fail closed when virtual scratch syntax contains a parent hop."""

    for match in re.finditer(r"(?<![A-Za-z0-9_./-])/scratch(?:/[^\s\"'|;&<>]*)?", command):
        if ".." in Path(match.group(0)).parts:
            return ExecuteResponse(
                output="Error: /scratch parent traversal is not allowed.",
                exit_code=126,
            )
    return None


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
        scratch_path: Path | None = None,
        timeout: int = 120,
        max_output_bytes: int = 100_000,
    ) -> None:
        super().__init__(root_dir=root_dir, virtual_mode=True)
        self.workspace_path = root_dir.expanduser().resolve()
        self.scratch_path = scratch_path.expanduser().resolve() if scratch_path is not None else None
        self._default_timeout = timeout
        self._max_output_bytes = max_output_bytes
        workspace_digest = hashlib.sha256(str(self.workspace_path).encode("utf-8")).hexdigest()[:16]
        self._id = f"restricted-host:{workspace_digest}"
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
        traversal = _reject_scratch_traversal(command)
        if traversal is not None:
            return traversal
        command = re.sub(
            r"(^|\s)/workspace(?=(/|\s|$))",
            lambda match: f"{match.group(1)}{shlex.quote(str(self.workspace_path))}",
            command,
        )
        if self.scratch_path is not None:
            command = re.sub(
                r"(?<![A-Za-z0-9_./-])/scratch(?=(?:/|\s|$|[\"']))",
                shlex.quote(str(self.scratch_path)),
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
    _interactive_network_jobs: dict[str, str] = {}
    _interactive_network_jobs_guard = threading.Lock()

    def __init__(self, docker_config: dict[str, Any]) -> None:
        self.config = dict(docker_config)

    @property
    def runtime_contract(self) -> str:
        return RUNTIME_CONTRACT

    @staticmethod
    def _owner_label() -> str:
        from runtime_identity.paths import trusted_owner_user_id

        return hashlib.sha256(trusted_owner_user_id().encode("utf-8")).hexdigest()[:20]

    def gc_stopped_workspace_containers(self) -> int:
        """Remove only stopped workspace containers owned by this Backend user."""

        listed = self._run(
            [
                "ps",
                "-aq",
                "--filter",
                "status=exited",
                "--filter",
                "label=com.puddingclaw.managed=true",
            ],
            timeout=30,
        )
        if listed.returncode != 0:
            return 0
        container_ids = [item.strip() for item in listed.stdout.splitlines() if item.strip()]
        removed = 0
        for container_id in container_ids:
            inspected = self._run(["inspect", container_id], timeout=30)
            if inspected.returncode != 0:
                continue
            try:
                container = json.loads(inspected.stdout)[0]
            except (json.JSONDecodeError, IndexError, TypeError):
                continue
            labels = container.get("Config", {}).get("Labels") or {}
            name = str(container.get("Name") or "").lstrip("/")
            current_workspace = (
                labels.get("com.puddingclaw.kind") == "workspace"
                and labels.get("com.puddingclaw.owner") == self._owner_label()
            )
            legacy_workspace = (
                not labels.get("com.puddingclaw.kind")
                and not labels.get("com.puddingclaw.owner")
                and bool(labels.get("com.puddingclaw.spec-hash"))
                and re.fullmatch(r"puddingclaw-project-[0-9a-f]{16}", name) is not None
            )
            if not (current_workspace or legacy_workspace):
                continue
            result = self._run(["rm", container_id], timeout=30)
            if result.returncode == 0:
                removed += 1
        return removed

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

    def _run_bytes(
        self,
        args: list[str],
        *,
        input_bytes: bytes | None = None,
        timeout: int = 30,
    ) -> subprocess.CompletedProcess[bytes]:
        return subprocess.run(
            [*self._docker_prefix(), *args],
            check=False,
            input=input_bytes,
            capture_output=True,
            timeout=timeout,
            env=self._env(),
        )

    @staticmethod
    def _interactive_lark_authorization(argv: list[str]) -> bool:
        """Return whether argv starts a browser-assisted Lark handshake.

        These commands are not ordinary request/response programs: they emit a
        verification URL and then remain alive while the user completes a
        browser step.  Running them through ``subprocess.run`` hides that URL
        until the process exits and therefore deadlocks the interaction.
        """

        if argv[:2] != ["sh", "-c"] or len(argv) != 3:
            return False
        command = re.sub(r"\s+2\s*>\s*&\s*1\s*$", "", argv[2].strip())
        try:
            tokens = shlex.split(command)
        except ValueError:
            return False
        return tokens in (
            ["lark-cli", "config", "init", "--new"],
            ["lark-cli", "auth", "login", "--recommend"],
        )

    def _expire_interactive_network_job(self, job_key: str, container_name: str) -> None:
        """Bound one-shot network authority even if browser auth is abandoned."""

        with self._interactive_network_jobs_guard:
            if self._interactive_network_jobs.get(job_key) != container_name:
                return
            self._interactive_network_jobs.pop(job_key, None)
        self._run(["rm", "-f", container_name], timeout=30)

    def _interactive_network_job_output(
        self,
        *,
        job_key: str,
        container_name: str,
        max_output_bytes: int,
        wait_seconds: float = 20.0,
    ) -> ExecuteResponse:
        """Wait only for actionable auth output, not for browser completion."""

        deadline = time.monotonic() + max(0.0, wait_seconds)
        stdout = ""
        stderr = ""
        running = True
        exit_code = 0
        while True:
            logs = self._run(["logs", container_name], timeout=10)
            stdout = logs.stdout or stdout
            stderr = logs.stderr or stderr
            inspection = self._run(
                ["inspect", "--format", "{{.State.Running}} {{.State.ExitCode}}", container_name],
                timeout=10,
            )
            if inspection.returncode != 0:
                running = False
                exit_code = 1
                stderr = stderr or inspection.stderr
            else:
                state = inspection.stdout.strip().split()
                running = bool(state and state[0].lower() == "true")
                if len(state) > 1 and state[1].lstrip("-").isdigit():
                    exit_code = int(state[1])
            combined = f"{stdout}\n{stderr}"
            if re.search(r"https?://\S+", combined) or not running or time.monotonic() >= deadline:
                break
            time.sleep(0.25)

        output, truncated = _bounded_output(
            stdout,
            stderr,
            max_output_bytes=max_output_bytes,
        )
        if running:
            output = (
                "Managed browser authorization started.\n"
                "Status: awaiting_user_browser\n"
                "Authorization completed: false\n"
                "Configuration saved: pending\n"
                "Launch result: the detached authorization job started successfully; "
                "this is not authorization success.\n"
                "Next action: show the QR code/link, end the current turn, and wait for "
                "the user. Do not continue to the next setup step until `lark-cli config "
                "show` confirms the configuration.\n"
                f"Job: {container_name}\n\n{output}"
            )
            # The detached process is the already-approved exact command.  It
            # may keep polling only for a bounded period while the user opens
            # the URL; no general-purpose shell remains exposed.
            return ExecuteResponse(output=output, exit_code=0, truncated=truncated)

        with self._interactive_network_jobs_guard:
            if self._interactive_network_jobs.get(job_key) == container_name:
                self._interactive_network_jobs.pop(job_key, None)
        if exit_code != 0:
            output = f"{output.rstrip()}\n\nExit code: {exit_code}"
        return ExecuteResponse(output=output, exit_code=exit_code, truncated=truncated)

    def _image_id(self, image: str) -> str:
        inspected = self._run(["image", "inspect", "--format", "{{.Id}}", image])
        if inspected.returncode != 0 or not inspected.stdout.strip():
            return ""
        return inspected.stdout.strip()

    def ensure_image(self, image: str) -> str:
        image_id = self._image_id(image)
        if image_id:
            return image_id
        if image == DEFAULT_SANDBOX_IMAGE:
            docker_dir = Path(__file__).resolve().parent / "docker"
            built = self._run(
                [
                    "build",
                    "--pull",
                    "--tag",
                    image,
                    "--file",
                    str(docker_dir / "Dockerfile"),
                    str(docker_dir),
                ],
                timeout=900,
            )
            if built.returncode != 0:
                raise RuntimeError(
                    built.stderr.strip() or built.stdout.strip() or "failed to build PuddingClaw sandbox image"
                )
        else:
            pulled = self._run(["pull", image], timeout=900)
            if pulled.returncode != 0:
                raise RuntimeError(
                    pulled.stderr.strip() or pulled.stdout.strip() or f"failed to pull Docker image {image}"
                )
        image_id = self._image_id(image)
        if not image_id:
            raise RuntimeError(f"failed to resolve immutable Docker image id for {image}")
        return image_id

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
        # One long-lived execution container belongs to one project path, not
        # to a Session or Run. The explicit prefix makes that lifecycle visible
        # in Docker Desktop while the stable path hash prevents duplicates.
        return f"puddingclaw-project-{digest}"

    @staticmethod
    def _dependency_volume_name(
        workspace: Path,
        *,
        ecosystem: str,
        working_directory: str,
        image: str,
    ) -> str:
        digest = hashlib.sha256(
            (f"{workspace}:{ecosystem}:{working_directory}:{image}:{RUNTIME_CONTRACT}").encode()
        ).hexdigest()[:20]
        return f"puddingclaw-deps-{ecosystem}-{digest}"

    @staticmethod
    def _runtime_home_volume_name(workspace: Path, *, image: str) -> str:
        digest = hashlib.sha256(f"{workspace}:{image}:{RUNTIME_CONTRACT}".encode()).hexdigest()[:20]
        return f"puddingclaw-runtime-home-{digest}"

    def _initialize_dependency_volume(
        self,
        *,
        image: str,
        volume_name: str,
        uid: int | None,
        gid: int | None,
    ) -> None:
        if uid is None or gid is None:
            return
        initialized = self._run(
            [
                "run",
                "--rm",
                "--network",
                "none",
                "--user",
                "0:0",
                "--mount",
                f"type=volume,src={volume_name},dst=/runtime",
                "--entrypoint",
                "sh",
                image,
                "-c",
                f"mkdir -p /runtime && chown {uid}:{gid} /runtime",
            ],
            timeout=120,
        )
        if initialized.returncode != 0:
            raise RuntimeError(
                initialized.stderr.strip()
                or initialized.stdout.strip()
                or "failed to initialize dependency volume ownership"
            )

    def dependency_plan(self, workspace: Path) -> WorkspaceDependencyPlan | None:
        return detect_workspace_dependency_plan(
            workspace,
            enabled=bool(self.config.get("dependency_setup_enabled", False)),
        )

    def _spec(self, workspace: Path) -> dict[str, Any]:
        plan = self.dependency_plan(workspace)
        readonly_mounts = []
        for item in self.config.get("_managed_readonly_mounts") or []:
            if not isinstance(item, dict):
                continue
            source = Path(str(item.get("source") or "")).expanduser().resolve()
            target = str(item.get("target") or "")
            if source.is_dir() and target in {"/skills", "/opt/puddingclaw/toolchain/node"}:
                workspace_target = None
                try:
                    relative = source.relative_to(workspace.expanduser().resolve())
                    if target == "/skills" and relative.parts:
                        workspace_target = f"/workspace/{relative.as_posix()}"
                except ValueError:
                    pass
                readonly_mounts.append(
                    {
                        "source": str(source),
                        "target": target,
                        "workspace_target": workspace_target,
                    }
                )
        if bool(self.config.get("_managed_user_toolchain", False)):
            from runtime_identity.paths import PuddingClawPaths
            from runtime_identity.toolchains import ToolchainManager

            toolchain = ToolchainManager(PuddingClawPaths.from_environment(), RUNTIME_CONTRACT).resolve_node()
            readonly_mounts.append(
                {
                    # Resolve on every spec calculation. An atomic ``current``
                    # switch therefore changes the source and forces stale
                    # workspace containers to be recreated.
                    "source": str(toolchain.mount_path.resolve()),
                    "target": "/opt/puddingclaw/toolchain/node",
                    "workspace_target": None,
                }
            )
        writable_mounts = []
        for item in self.config.get("_managed_writable_mounts") or []:
            if not isinstance(item, dict):
                continue
            source = Path(str(item.get("source") or "")).expanduser().resolve()
            target = str(item.get("target") or "")
            if source.is_dir() and target in {"/harness-scratch", "/scratch"}:
                writable_mounts.append({"source": str(source), "target": target})
        return {
            "workspace": str(workspace),
            "image": str(self.config.get("image") or DEFAULT_SANDBOX_IMAGE),
            "network_enabled": bool(self.config.get("network_enabled", False)),
            "cpu_limit": str(self.config.get("cpu_limit") or "2"),
            "memory_limit_mb": int(self.config.get("memory_limit_mb") or 2048),
            "pids_limit": int(self.config.get("pids_limit") or 256),
            "uid": os.getuid() if hasattr(os, "getuid") else None,
            "gid": os.getgid() if hasattr(os, "getgid") else None,
            "runtime_contract": RUNTIME_CONTRACT,
            "readonly_mounts": readonly_mounts,
            "writable_mounts": writable_mounts,
            "runtime_mounts": ([item.to_dict() for item in plan.runtime_mounts] if plan is not None else []),
        }

    def ensure_container(self, workspace: Path) -> tuple[str, str]:
        workspace = workspace.expanduser().resolve()
        name = self._container_name(workspace)
        spec = self._spec(workspace)
        spec["image_id"] = self.ensure_image(spec["image"])
        spec_hash = hashlib.sha256(json.dumps(spec, sort_keys=True).encode("utf-8")).hexdigest()
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
                        raise RuntimeError(removed.stderr.strip() or "failed to replace Docker sandbox")
                elif running.strip().lower() == "true":
                    try:
                        self._validate_runtime(name, spec)
                    except RuntimeError:
                        logger.warning(
                            "Replacing Docker sandbox %s because runtime validation failed",
                            name,
                            exc_info=True,
                        )
                        removed = self._run(["rm", "-f", name])
                        if removed.returncode != 0:
                            raise RuntimeError(removed.stderr.strip() or "failed to replace invalid Docker sandbox")
                    else:
                        self.mark_activity(name)
                        return name, spec_hash
                else:
                    started = self._run(["start", name])
                    if started.returncode != 0:
                        raise RuntimeError(started.stderr.strip() or "failed to start Docker sandbox")
                    try:
                        self._validate_runtime(name, spec)
                    except RuntimeError:
                        removed = self._run(["rm", "-f", name])
                        if removed.returncode != 0:
                            raise RuntimeError(removed.stderr.strip() or "failed to replace invalid Docker sandbox")
                    else:
                        self.mark_activity(name)
                        return name, spec_hash

            runtime_mount_args: list[str] = []
            runtime_home_volume = self._runtime_home_volume_name(
                workspace,
                image=spec["image_id"],
            )
            runtime_home = self._run(
                [
                    "volume",
                    "create",
                    "--label",
                    "com.puddingclaw.managed=true",
                    "--label",
                    f"com.puddingclaw.workspace={name}",
                    runtime_home_volume,
                ]
            )
            if runtime_home.returncode != 0:
                raise RuntimeError(
                    runtime_home.stderr.strip()
                    or runtime_home.stdout.strip()
                    or "failed to create sandbox runtime-home volume"
                )
            self._initialize_dependency_volume(
                image=spec["image_id"],
                volume_name=runtime_home_volume,
                uid=spec["uid"],
                gid=spec["gid"],
            )
            runtime_mount_args.extend(
                [
                    "--mount",
                    (f"type=volume,src={runtime_home_volume},dst=/home/puddingclaw"),
                ]
            )
            for mount in spec["runtime_mounts"]:
                volume_name = self._dependency_volume_name(
                    workspace,
                    ecosystem=str(mount["ecosystem"]),
                    working_directory=str(mount["working_directory"]),
                    image=spec["image_id"],
                )
                volume = self._run(
                    [
                        "volume",
                        "create",
                        "--label",
                        "com.puddingclaw.managed=true",
                        "--label",
                        f"com.puddingclaw.workspace={name}",
                        volume_name,
                    ]
                )
                if volume.returncode != 0:
                    raise RuntimeError(
                        volume.stderr.strip() or volume.stdout.strip() or "failed to create dependency volume"
                    )
                self._initialize_dependency_volume(
                    image=spec["image_id"],
                    volume_name=volume_name,
                    uid=spec["uid"],
                    gid=spec["gid"],
                )
                relative = str(mount["working_directory"])
                project_dir = "/workspace" if relative == "." else f"/workspace/{relative}"
                runtime_mount_args.extend(
                    [
                        "--mount",
                        (f"type=volume,src={volume_name},dst={project_dir}/{mount['target_name']}"),
                    ]
                )
            for mount in spec["readonly_mounts"]:
                targets = [mount["target"]]
                if mount.get("workspace_target"):
                    targets.append(mount["workspace_target"])
                for target in targets:
                    runtime_mount_args.extend(
                        [
                            "--mount",
                            (f"type=bind,src={mount['source']},dst={target},readonly"),
                        ]
                    )
            for mount in spec["writable_mounts"]:
                runtime_mount_args.extend(
                    [
                        "--mount",
                        f"type=bind,src={mount['source']},dst={mount['target']}",
                    ]
                )
            create_args = [
                "create",
                "--name",
                name,
                "--label",
                "com.puddingclaw.managed=true",
                "--label",
                "com.puddingclaw.kind=workspace",
                "--label",
                f"com.puddingclaw.owner={self._owner_label()}",
                "--label",
                f"com.puddingclaw.spec-hash={spec_hash}",
                "--workdir",
                "/workspace",
                "--mount",
                f"type=bind,src={workspace},dst=/workspace",
                *runtime_mount_args,
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
                "--env",
                "HOME=/home/puddingclaw",
                "--env",
                "XDG_CACHE_HOME=/home/puddingclaw/.cache",
                "--env",
                "PIP_CACHE_DIR=/home/puddingclaw/.cache/pip",
                "--env",
                "UV_CACHE_DIR=/home/puddingclaw/.cache/uv",
                "--env",
                "npm_config_cache=/home/puddingclaw/.cache/npm",
                "--env",
                "PYTHONUSERBASE=/home/puddingclaw/.local",
                "--env",
                "npm_config_prefix=/home/puddingclaw/.npm-global",
                "--env",
                (
                    "PATH=/opt/puddingclaw/toolchain/node/bin:/home/puddingclaw/.local/bin:"
                    "/home/puddingclaw/.npm-global/bin:/usr/local/bin:/usr/bin:/bin"
                ),
                "--env",
                (
                    "NODE_PATH=/opt/puddingclaw/toolchain/node/lib/node_modules:"
                    "/home/puddingclaw/.npm-global/lib/node_modules"
                ),
                "--tmpfs",
                "/home/puddingclaw/.lark-cli:rw,nosuid,nodev,size=16m",
            ]
            if spec["uid"] is not None and spec["gid"] is not None:
                create_args.extend(["--user", f"{spec['uid']}:{spec['gid']}"])
            create_args.extend(
                [
                    spec["image"],
                    "sh",
                    "-c",
                    (
                        'mkdir -p "$HOME" "$XDG_CACHE_HOME" "$PIP_CACHE_DIR" '
                        '"$UV_CACHE_DIR" "$npm_config_cache"; '
                        "trap : TERM INT; sleep infinity & wait"
                    ),
                ]
            )
            created = self._run(create_args, timeout=120)
            if created.returncode != 0:
                raise RuntimeError(
                    created.stderr.strip() or created.stdout.strip() or "failed to create Docker sandbox"
                )
            started = self._run(["start", name])
            if started.returncode != 0:
                raise RuntimeError(started.stderr.strip() or "failed to start Docker sandbox")
            try:
                self._validate_runtime(name, spec)
            except Exception:
                self._run(["rm", "-f", name])
                raise
            self.mark_activity(name)
            return name, spec_hash

    def run_ephemeral_network_command(
        self,
        workspace: Path,
        *,
        argv: list[str],
        timeout: int,
        max_output_bytes: int,
        workspace_writable: bool = False,
    ) -> ExecuteResponse:
        """Run one approved argv in a disposable networked container.

        Raw terminal actions may need to download or update project files, so
        their exact approved command gets a writable workspace. Typed package
        installation keeps the workspace read-only and writes only persistent
        runtime/dependency volumes.
        """

        workspace = workspace.expanduser().resolve()
        spec = self._spec(workspace)
        image_id = self.ensure_image(spec["image"])
        runtime_home_volume = self._runtime_home_volume_name(
            workspace,
            image=image_id,
        )
        workspace_mount = f"type=bind,src={workspace},dst=/workspace"
        if not workspace_writable:
            workspace_mount += ",readonly"
        mount_args = [
            "--mount",
            workspace_mount,
            "--mount",
            f"type=volume,src={runtime_home_volume},dst=/home/puddingclaw",
        ]
        for mount in spec["readonly_mounts"]:
            targets = [mount["target"]]
            if mount.get("workspace_target"):
                targets.append(mount["workspace_target"])
            for target in targets:
                mount_args.extend(
                    [
                        "--mount",
                        f"type=bind,src={mount['source']},dst={target},readonly",
                    ]
                )
        for mount in spec["writable_mounts"]:
            mount_args.extend(
                [
                    "--mount",
                    f"type=bind,src={mount['source']},dst={mount['target']}",
                ]
            )
        for mount in spec["runtime_mounts"]:
            volume_name = self._dependency_volume_name(
                workspace,
                ecosystem=str(mount["ecosystem"]),
                working_directory=str(mount["working_directory"]),
                image=image_id,
            )
            relative = str(mount["working_directory"])
            project_dir = "/workspace" if relative == "." else f"/workspace/{relative}"
            mount_args.extend(
                [
                    "--mount",
                    (f"type=volume,src={volume_name},dst={project_dir}/{mount['target_name']}"),
                ]
            )
        args = [
            "run",
            "--rm",
            "--network",
            "bridge",
            "--read-only",
            "--tmpfs",
            "/tmp:rw,nosuid,nodev,size=256m",
            "--workdir",
            "/workspace",
            *mount_args,
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
            "--env",
            "HOME=/home/puddingclaw",
            "--env",
            "XDG_CACHE_HOME=/home/puddingclaw/.cache",
            "--env",
            "PIP_CACHE_DIR=/home/puddingclaw/.cache/pip",
            "--env",
            "npm_config_cache=/home/puddingclaw/.cache/npm",
            "--env",
            "PYTHONUSERBASE=/home/puddingclaw/.local",
            "--env",
            "npm_config_prefix=/home/puddingclaw/.npm-global",
            "--env",
            (
                "PATH=/opt/puddingclaw/toolchain/node/bin:/home/puddingclaw/.local/bin:"
                "/home/puddingclaw/.npm-global/bin:/usr/local/bin:/usr/bin:/bin"
            ),
            "--env",
            (
                "NODE_PATH=/opt/puddingclaw/toolchain/node/lib/node_modules:"
                "/home/puddingclaw/.npm-global/lib/node_modules"
            ),
            "--tmpfs",
            "/home/puddingclaw/.lark-cli:rw,nosuid,nodev,size=16m",
            "--entrypoint",
            argv[0],
        ]
        if spec["uid"] is not None and spec["gid"] is not None:
            args.extend(["--user", f"{spec['uid']}:{spec['gid']}"])
        args.extend([image_id, *argv[1:]])
        if self._interactive_lark_authorization(argv):
            job_key = hashlib.sha256(
                f"{workspace}:{json.dumps(argv, ensure_ascii=False)}".encode()
            ).hexdigest()[:24]
            with self._interactive_network_jobs_guard:
                existing_name = self._interactive_network_jobs.get(job_key)
            if existing_name:
                inspection = self._run(
                    ["inspect", "--format", "{{.State.Running}}", existing_name],
                    timeout=10,
                )
                if inspection.returncode == 0:
                    return self._interactive_network_job_output(
                        job_key=job_key,
                        container_name=existing_name,
                        max_output_bytes=max_output_bytes,
                        wait_seconds=2.0,
                    )
                with self._interactive_network_jobs_guard:
                    if self._interactive_network_jobs.get(job_key) == existing_name:
                        self._interactive_network_jobs.pop(job_key, None)

            container_name = f"puddingclaw-auth-{job_key}-{uuid.uuid4().hex[:8]}"
            ttl_seconds = max(300, min(timeout * 5, 900))
            # Replace the blocking ``docker run --rm`` prefix with a detached,
            # named container.  A TTY makes CLIs flush their verification URL;
            # the exact approved process remains isolated by the same mounts,
            # capabilities, and bridge-network policy as other one-shot runs.
            # The in-container timeout is authoritative even if Backend
            # restarts and loses its cleanup timer.
            bounded_command = (
                f"timeout --signal=TERM --kill-after=10s {ttl_seconds}s "
                f"sh -c {shlex.quote(argv[2])}"
            )
            detached_args = [
                "run",
                "--detach",
                "--tty",
                "--rm",
                "--name",
                container_name,
                *args[2:-1],
                bounded_command,
            ]
            started = self._run(detached_args, timeout=min(timeout, 30))
            if started.returncode != 0:
                output, truncated = _bounded_output(
                    started.stdout,
                    started.stderr,
                    max_output_bytes=max_output_bytes,
                )
                return ExecuteResponse(output=output, exit_code=started.returncode, truncated=truncated)
            with self._interactive_network_jobs_guard:
                self._interactive_network_jobs[job_key] = container_name
            expiry = threading.Timer(
                ttl_seconds,
                self._expire_interactive_network_job,
                args=(job_key, container_name),
            )
            expiry.daemon = True
            expiry.start()
            return self._interactive_network_job_output(
                job_key=job_key,
                container_name=container_name,
                max_output_bytes=max_output_bytes,
            )
        try:
            result = self._run(args, timeout=timeout)
        except subprocess.TimeoutExpired:
            return ExecuteResponse(
                output=f"Error: Networked command timed out after {timeout} seconds.",
                exit_code=124,
            )
        output, truncated = _bounded_output(
            result.stdout,
            result.stderr,
            max_output_bytes=max_output_bytes,
        )
        if result.returncode != 0:
            output = f"{output.rstrip()}\n\nExit code: {result.returncode}"
        return ExecuteResponse(
            output=output,
            exit_code=result.returncode,
            truncated=truncated,
        )

    def install_managed_node_cli(
        self,
        workspace: Path,
        *,
        distribution: str,
        toolchain_path: Path,
        container_path: str,
        timeout: int = 600,
        max_output_bytes: int = 100_000,
    ) -> ExecuteResponse:
        """Install one Adapter-approved distribution into a shared Toolchain."""

        workspace = workspace.expanduser().resolve()
        toolchain_path = toolchain_path.expanduser().resolve()
        toolchain_path.mkdir(parents=True, exist_ok=True)
        spec = self._spec(workspace)
        image_id = self.ensure_image(spec["image"])
        args = [
            "run",
            "--rm",
            "--label",
            "com.puddingclaw.managed=true",
            "--label",
            "com.puddingclaw.kind=installer",
            "--label",
            f"com.puddingclaw.owner={self._owner_label()}",
            "--network",
            "bridge",
            "--read-only",
            "--tmpfs",
            "/tmp:rw,nosuid,nodev,size=256m",
            "--tmpfs",
            "/home/puddingclaw:rw,nosuid,nodev,size=256m",
            "--mount",
            f"type=bind,src={toolchain_path},dst={container_path}",
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
            "--env",
            "HOME=/home/puddingclaw",
            "--env",
            "npm_config_cache=/home/puddingclaw/.cache/npm",
            "--env",
            f"npm_config_prefix={container_path}",
            "--env",
            f"PATH={container_path}/bin:/usr/local/bin:/usr/bin:/bin",
            "--entrypoint",
            "npm",
        ]
        if spec["uid"] is not None and spec["gid"] is not None:
            args.extend(["--user", f"{spec['uid']}:{spec['gid']}"])
        args.extend([image_id, "install", "--global", distribution])
        try:
            installed = self._run(args, timeout=timeout)
        except subprocess.TimeoutExpired:
            return ExecuteResponse(
                output=f"Error: Managed CLI installation timed out after {timeout} seconds.",
                exit_code=124,
            )
        output, truncated = _bounded_output(
            installed.stdout,
            installed.stderr,
            max_output_bytes=max_output_bytes,
        )
        if installed.returncode != 0:
            return ExecuteResponse(output=output, exit_code=installed.returncode, truncated=truncated)
        verified = self._run(
            [
                "run",
                "--rm",
                "--network",
                "none",
                "--read-only",
                "--mount",
                f"type=bind,src={toolchain_path},dst={container_path},readonly",
                "--cap-drop",
                "ALL",
                "--security-opt",
                "no-new-privileges",
                "--entrypoint",
                f"{container_path}/bin/lark-cli",
                image_id,
                "--version",
            ],
            timeout=60,
        )
        verification, verification_truncated = _bounded_output(
            verified.stdout,
            verified.stderr,
            max_output_bytes=max_output_bytes,
        )
        combined = f"{output.rstrip()}\n\nVerification:\n{verification}"
        return ExecuteResponse(
            output=combined,
            exit_code=verified.returncode,
            truncated=truncated or verification_truncated,
        )

    def run_managed_provider_cli(
        self,
        workspace: Path,
        *,
        argv: list[str],
        environment: dict[str, str],
        toolchain_path: Path,
        container_path: str,
        credential_state: bytes,
        network_enabled: bool,
        workspace_writable: bool,
        timeout: int = 120,
        max_output_bytes: int = 100_000,
    ) -> ManagedProviderExecutionResult:
        """Run exact Adapter-owned argv with credentials only in container tmpfs."""

        if not argv or argv[0] != "lark-cli":
            raise ValueError("provider runner accepts only Adapter-normalized lark-cli argv")
        workspace = workspace.expanduser().resolve()
        toolchain_path = toolchain_path.expanduser().resolve(strict=True)
        spec = self._spec(workspace)
        image_id = self.ensure_image(spec["image"])
        name = f"puddingclaw-provider-{uuid.uuid4().hex[:20]}"
        workspace_mount = f"type=bind,src={workspace},dst=/workspace"
        if not workspace_writable:
            workspace_mount += ",readonly"
        home_tmpfs = "/home/puddingclaw:rw,nosuid,nodev,size=128m"
        if spec["uid"] is not None and spec["gid"] is not None:
            home_tmpfs += f",uid={spec['uid']},gid={spec['gid']}"
        create = [
            "create",
            "--name",
            name,
            "--label",
            "com.puddingclaw.managed=true",
            "--label",
            "com.puddingclaw.kind=provider-runner",
            "--label",
            f"com.puddingclaw.owner={self._owner_label()}",
            "--network",
            "bridge" if network_enabled else "none",
            "--read-only",
            "--tmpfs",
            "/tmp:rw,nosuid,nodev,size=256m",
            "--tmpfs",
            home_tmpfs,
            "--mount",
            workspace_mount,
            "--mount",
            f"type=bind,src={toolchain_path},dst={container_path},readonly",
            "--workdir",
            "/workspace",
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
            "--env",
            "HOME=/home/puddingclaw",
            "--env",
            f"PATH={container_path}/bin:/usr/local/bin:/usr/bin:/bin",
            "--env",
            f"NODE_PATH={container_path}/lib/node_modules",
        ]
        if spec["uid"] is not None and spec["gid"] is not None:
            create.extend(["--user", f"{spec['uid']}:{spec['gid']}"])
        create.extend(
            [
                image_id,
                "sh",
                "-c",
                f"mkdir -p /home/puddingclaw/.lark-cli && timeout {max(timeout + 60, 300)}s sleep infinity",
            ]
        )
        created = self._run(create, timeout=60)
        if created.returncode != 0:
            output, truncated = _bounded_output(
                created.stdout,
                created.stderr,
                max_output_bytes=max_output_bytes,
            )
            return ManagedProviderExecutionResult(output, created.returncode, None, truncated)
        try:
            started = self._run(["start", name], timeout=30)
            if started.returncode != 0:
                output, truncated = _bounded_output(
                    started.stdout,
                    started.stderr,
                    max_output_bytes=max_output_bytes,
                )
                return ManagedProviderExecutionResult(output, started.returncode, None, truncated)
            if credential_state:
                imported = self._run_bytes(
                    ["exec", "-i", name, "tar", "-xzf", "-", "-C", "/home/puddingclaw"],
                    input_bytes=credential_state,
                    timeout=30,
                )
                if imported.returncode != 0:
                    raise RuntimeError(imported.stderr.decode("utf-8", errors="replace"))
            exec_args = ["exec", "--workdir", "/workspace"]
            for key, value in sorted(environment.items()):
                exec_args.extend(["--env", f"{key}={value}"])
            exec_args.extend([name, *argv])
            try:
                result = self._run(exec_args, timeout=timeout)
            except subprocess.TimeoutExpired:
                return ManagedProviderExecutionResult(
                    f"Error: Managed provider command timed out after {timeout} seconds. Use a non-blocking auth form.",
                    124,
                    None,
                )
            output, truncated = _bounded_output(
                result.stdout,
                result.stderr,
                max_output_bytes=max_output_bytes,
            )
            exported = self._run_bytes(
                [
                    "exec",
                    name,
                    "tar",
                    "-czf",
                    "-",
                    "-C",
                    "/home/puddingclaw",
                    ".lark-cli",
                ],
                timeout=30,
            )
            if exported.returncode != 0:
                raise RuntimeError(exported.stderr.decode("utf-8", errors="replace"))
            if result.returncode != 0:
                output = f"{output.rstrip()}\n\nExit code: {result.returncode}"
            return ManagedProviderExecutionResult(
                output=output,
                exit_code=result.returncode,
                credential_state=exported.stdout,
                truncated=truncated,
            )
        finally:
            self._run(["rm", "-f", name], timeout=30)

    def run_ephemeral_external_directory_command(
        self,
        workspace: Path,
        *,
        external_directory: Path,
        command: str,
        timeout: int,
        max_output_bytes: int,
        writable: bool = False,
    ) -> ExecuteResponse:
        """Run one approved command with one exact external directory mount.

        Writable mounts are only for server-owned directory drafts. The caller
        remains responsible for diff review and transactional host write-back.
        The container root and project workspace stay read-only, networking is
        disabled, and only this one mount can receive writes.
        """

        workspace = workspace.expanduser().resolve(strict=True)
        external_directory = external_directory.expanduser().resolve(strict=True)
        if not external_directory.is_dir():
            return ExecuteResponse(
                output="Error: external command target must be one exact directory.",
                exit_code=1,
            )
        if external_directory == workspace:
            return ExecuteResponse(
                output="Error: this directory is already the project workspace; use execute instead.",
                exit_code=1,
            )
        spec = self._spec(workspace)
        image_id = self.ensure_image(spec["image"])
        runtime_home_volume = self._runtime_home_volume_name(
            workspace,
            image=image_id,
        )
        mount_args = [
            "--mount",
            f"type=bind,src={workspace},dst=/workspace,readonly",
            "--mount",
            (
                f"type=bind,src={external_directory},dst=/external-workspace"
                + ("" if writable else ",readonly")
            ),
            "--mount",
            f"type=volume,src={runtime_home_volume},dst=/home/puddingclaw",
        ]
        for mount in spec["readonly_mounts"]:
            mount_args.extend(
                [
                    "--mount",
                    f"type=bind,src={mount['source']},dst={mount['target']},readonly",
                ]
            )
        for mount in spec["runtime_mounts"]:
            volume_name = self._dependency_volume_name(
                workspace,
                ecosystem=str(mount["ecosystem"]),
                working_directory=str(mount["working_directory"]),
                image=image_id,
            )
            relative = str(mount["working_directory"])
            project_dir = "/workspace" if relative == "." else f"/workspace/{relative}"
            mount_args.extend(
                [
                    "--mount",
                    f"type=volume,src={volume_name},dst={project_dir}/{mount['target_name']}",
                ]
            )
        args = [
            "run",
            "--rm",
            "--network",
            "none",
            "--read-only",
            "--tmpfs",
            "/tmp:rw,nosuid,nodev,size=256m",
            "--workdir",
            "/external-workspace",
            *mount_args,
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
            "--env",
            "HOME=/home/puddingclaw",
            "--env",
            (
                "PATH=/opt/puddingclaw/toolchain/node/bin:/home/puddingclaw/.local/bin:"
                "/home/puddingclaw/.npm-global/bin:/usr/local/bin:/usr/bin:/bin"
            ),
            "--tmpfs",
            "/home/puddingclaw/.lark-cli:rw,nosuid,nodev,size=16m",
            "--entrypoint",
            "sh",
        ]
        if spec["uid"] is not None and spec["gid"] is not None:
            args.extend(["--user", f"{spec['uid']}:{spec['gid']}"])
        args.extend([image_id, "-c", command])
        try:
            result = self._run(args, timeout=timeout)
        except subprocess.TimeoutExpired:
            return ExecuteResponse(
                output=f"Error: External directory command timed out after {timeout} seconds.",
                exit_code=124,
            )
        output, truncated = _bounded_output(
            result.stdout,
            result.stderr,
            max_output_bytes=max_output_bytes,
        )
        if result.returncode != 0:
            output = f"{output.rstrip()}\n\nExit code: {result.returncode}"
        return ExecuteResponse(
            output=output,
            exit_code=result.returncode,
            truncated=truncated,
        )

    def _validate_runtime(self, container_name: str, spec: dict[str, Any]) -> None:
        inspect_result = self._run(["inspect", container_name])
        if inspect_result.returncode != 0:
            raise RuntimeError(inspect_result.stderr.strip() or "failed to inspect Docker sandbox")
        try:
            inspected = json.loads(inspect_result.stdout)[0]
        except (json.JSONDecodeError, IndexError, TypeError) as exc:
            raise RuntimeError("Docker sandbox inspect output is invalid") from exc
        mounts = {
            str(item.get("Destination") or ""): item
            for item in inspected.get("Mounts") or []
            if isinstance(item, dict)
        }
        for expected in spec.get("writable_mounts") or []:
            target = str(expected.get("target") or "")
            actual = mounts.get(target)
            expected_source = _canonical_docker_mount_source(
                str(expected.get("source") or "")
            )
            actual_source = _canonical_docker_mount_source(
                str(actual.get("Source") or "")
            ) if actual else ""
            if actual is None or actual_source != expected_source or actual.get("RW") is not True:
                raise RuntimeError(
                    f"Docker sandbox writable mount contract mismatch for {target or '<empty>'}."
                )
        expected_user = ""
        if spec.get("uid") is not None and spec.get("gid") is not None:
            expected_user = f"{spec['uid']}:{spec['gid']}"
        configured_user = str(inspected.get("Config", {}).get("User") or "")
        if expected_user and configured_user != expected_user:
            raise RuntimeError(
                f"Docker sandbox user contract mismatch: expected {expected_user}, got {configured_user or '<root>'}."
            )
        scratch_relative = str(self.config.get("_scratch_relative") or "").strip("/")
        scratch_path = (
            "/scratch"
            if any(item.get("target") == "/scratch" for item in spec.get("writable_mounts") or [])
            else f"/harness-scratch/{scratch_relative}"
            if scratch_relative
            else "/harness-scratch"
        )
        probe_name = f".puddingclaw-write-probe-{uuid.uuid4().hex[:12]}"
        result = self._run(
            [
                "exec",
                container_name,
                "sh",
                "-c",
                (
                    "python3 --version >/dev/null 2>&1 && "
                    "node --version >/dev/null 2>&1 && "
                    "curl --version >/dev/null 2>&1 && "
                    f"test -d {shlex.quote(scratch_path)} && "
                    f"test -w {shlex.quote(scratch_path)} && "
                    f": > {shlex.quote(f'{scratch_path}/{probe_name}')} && "
                    f"rm -f {shlex.quote(f'{scratch_path}/{probe_name}')}"
                ),
            ]
        )
        if result.returncode != 0:
            raise RuntimeError(
                "Docker sandbox image must provide python3, node, and curl. "
                + (result.stderr.strip() or result.stdout.strip())
            )

    def mark_activity(self, container_name: str) -> None:
        """Re-arm the project container's idle-removal timer after use."""

        idle_minutes = self.config.get("idle_stop_minutes", 30)
        if not isinstance(idle_minutes, int) or isinstance(idle_minutes, bool) or idle_minutes <= 0:
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
        with self._lock(container_name):
            # A command may have been running while this timer waited for the
            # keyed container lock. Re-check only after acquiring it so a
            # freshly re-armed generation cannot be removed by a stale timer.
            with self._idle_timers_guard:
                current = self._idle_timers.get(container_name)
                if current is None or current[0] != generation:
                    return
                self._idle_timers.pop(container_name, None)
            self._run(["rm", "-f", container_name], timeout=30)

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
        self.dependency_plan = manager.dependency_plan(self.workspace_path)
        scratch_relative = str(manager.config.get("_scratch_relative") or "").strip("/")
        spec = manager._spec(self.workspace_path)
        self.scratch_container_path = (
            "/scratch"
            if any(item.get("target") == "/scratch" for item in spec.get("writable_mounts") or [])
            else f"/harness-scratch/{scratch_relative}"
            if scratch_relative
            else "/harness-scratch"
        )
        self.container_name, self.spec_hash = manager.ensure_container(self.workspace_path)

    @property
    def id(self) -> str:
        return f"docker:{self.container_name}:{self.spec_hash[:12]}"

    def execute(
        self,
        command: str,
        *,
        timeout: int | None = None,
    ) -> ExecuteResponse:
        traversal = _reject_scratch_traversal(command)
        if traversal is not None:
            return traversal
        effective_timeout = timeout if timeout is not None else self._default_timeout
        if not isinstance(effective_timeout, int) or effective_timeout <= 0:
            raise ValueError("timeout must be a positive integer")
        command = re.sub(
            r"(?<![A-Za-z0-9_./-])/scratch(?=(?:/|\s|$|[\"']))",
            self.scratch_container_path,
            command,
        )
        try:
            with self.manager._lock(self.container_name):
                # An idle timer may stop a project container between Runs.
                # Reconcile it under the same keyed lock used by lifecycle
                # cleanup, then serialize commands for this project.
                container_name, spec_hash = self.manager.ensure_container(self.workspace_path)
                if container_name != self.container_name or spec_hash != self.spec_hash:
                    raise RuntimeError(
                        "Docker sandbox specification changed after this Run started; start a new Run."
                    )
                from harness.tool_execution import ShellPolicyAnalyzer

                effects = ShellPolicyAnalyzer.capabilities(
                    command,
                    workspace_path=self.workspace_path,
                )
                if not bool(self.manager.config.get("network_enabled", False)) and effects.network:
                    result = self.manager.run_ephemeral_network_command(
                        self.workspace_path,
                        argv=["sh", "-c", command],
                        timeout=effective_timeout,
                        max_output_bytes=self._max_output_bytes,
                        workspace_writable=effects.workspace_write,
                    )
                    self.manager.mark_activity(self.container_name)
                    return result
                result = self.manager._run(
                    [
                        "exec",
                        "--workdir",
                        "/workspace",
                        self.container_name,
                        "sh",
                        "-c",
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

    def execute_external_directory(
        self,
        directory_path: str,
        command: str,
        *,
        timeout: int | None = None,
        writable: bool = False,
    ) -> ExecuteResponse:
        """Execute one approved read-only exact-directory command ephemerally."""

        effective_timeout = timeout if timeout is not None else self._default_timeout
        if not isinstance(effective_timeout, int) or effective_timeout <= 0:
            raise ValueError("timeout must be a positive integer")
        try:
            return self.manager.run_ephemeral_external_directory_command(
                self.workspace_path,
                external_directory=Path(directory_path),
                command=command,
                timeout=effective_timeout,
                max_output_bytes=self._max_output_bytes,
                writable=writable,
            )
        except Exception as exc:  # noqa: BLE001
            return ExecuteResponse(
                output=f"Error executing external directory command ({type(exc).__name__}): {exc}",
                exit_code=1,
            )

    def install_packages(
        self,
        ecosystem: str,
        packages: list[str],
    ) -> ExecuteResponse:
        """Install Skill dependencies without networking the runtime container."""

        if ecosystem == "python":
            argv = ["python3", "-m", "pip", "install", "--user", *packages]
        elif ecosystem == "node":
            argv = ["npm", "install", "--global", *packages]
        else:
            return ExecuteResponse(
                output=f"Error: Unsupported package ecosystem {ecosystem!r}.",
                exit_code=1,
            )
        with self.manager._lock(self.container_name):
            # Ensure the persistent runtime/dependency volumes exist, but do
            # not attach the long-lived container to a network.
            container_name, spec_hash = self.manager.ensure_container(self.workspace_path)
            if container_name != self.container_name or spec_hash != self.spec_hash:
                raise RuntimeError(
                    "Docker sandbox specification changed after this Run started; start a new Run."
                )
            result = self.manager.run_ephemeral_network_command(
                self.workspace_path,
                argv=argv,
                timeout=max(self._default_timeout, 300),
                max_output_bytes=self._max_output_bytes,
            )
            self.manager.mark_activity(self.container_name)
            return result

    def install_managed_node_cli(
        self,
        *,
        distribution: str,
        toolchain_path: Path,
        container_path: str,
    ) -> ExecuteResponse:
        return self.manager.install_managed_node_cli(
            self.workspace_path,
            distribution=distribution,
            toolchain_path=toolchain_path,
            container_path=container_path,
            timeout=max(self._default_timeout, 600),
            max_output_bytes=self._max_output_bytes,
        )

    def run_managed_provider_cli(
        self,
        *,
        argv: list[str],
        environment: dict[str, str],
        toolchain_path: Path,
        container_path: str,
        credential_state: bytes,
        network_enabled: bool,
        workspace_writable: bool,
    ) -> ManagedProviderExecutionResult:
        return self.manager.run_managed_provider_cli(
            self.workspace_path,
            argv=argv,
            environment=environment,
            toolchain_path=toolchain_path,
            container_path=container_path,
            credential_state=credential_state,
            network_enabled=network_enabled,
            workspace_writable=workspace_writable,
            timeout=self._default_timeout,
            max_output_bytes=self._max_output_bytes,
        )


@dataclass(frozen=True)
class WorkspaceBackendSelection:
    backend: RestrictedHostWorkspaceBackend | DockerWorkspaceBackend
    mode: str
    fallback_reason: str | None = None
    dependency_plan: WorkspaceDependencyPlan | None = None


def build_workspace_execution_backend(
    workspace_path: Path,
    terminal_config: dict[str, Any],
) -> WorkspaceBackendSelection:
    """Select Docker when explicitly enabled, otherwise controlled host fallback."""

    timeout = int(terminal_config.get("default_timeout_seconds") or 120)
    docker_enabled = bool(terminal_config.get("docker_enabled", False))
    docker_config = dict(terminal_config.get("docker") or {})
    scratch_host_path_raw = str(terminal_config.get("_scratch_host_path") or "").strip()
    scratch_host_path = Path(scratch_host_path_raw) if scratch_host_path_raw else None
    if docker_enabled:
        manager = ProjectSandboxManager(docker_config)
        available, reason = manager.probe()
        if available:
            try:
                backend = DockerWorkspaceBackend(
                    root_dir=workspace_path,
                    manager=manager,
                    timeout=timeout,
                )
                return WorkspaceBackendSelection(
                    backend=backend,
                    mode="docker",
                    dependency_plan=backend.dependency_plan,
                )
            except Exception as exc:
                reason = f"{type(exc).__name__}: {exc}"
        if str(terminal_config.get("on_unavailable") or "fallback") == "deny":
            raise RuntimeError(f"Docker sandbox is unavailable: {reason}")
        return WorkspaceBackendSelection(
            backend=RestrictedHostWorkspaceBackend(
                root_dir=workspace_path,
                scratch_path=scratch_host_path,
                timeout=timeout,
            ),
            mode="restricted_host",
            fallback_reason=reason,
        )
    return WorkspaceBackendSelection(
        backend=RestrictedHostWorkspaceBackend(
            root_dir=workspace_path,
            scratch_path=scratch_host_path,
            timeout=timeout,
        ),
        mode="restricted_host",
    )
