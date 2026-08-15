"""Execution-capable workspace backends for spawn and kernel execution."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shlex
import subprocess
import sys
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from deepagents.backends import FilesystemBackend
from deepagents.backends.protocol import ExecuteResponse, SandboxBackendProtocol

from harness.dependency_setup import (
    WorkspaceDependencyPlan,
    detect_workspace_dependency_plan,
)
from harness.sandbox_profiles import SandboxGrantProfile
from runtime_identity.adapters import CredentialStateSpec, ManagedCliRegistry
from runtime_identity.profiles import validate_credential_archive

DEFAULT_SANDBOX_IMAGE = "puddingclaw/sandbox:python3.12-node22-chromium-v5"
RUNTIME_CONTRACT = "python3.12+node22+chromium-v5"
logger = logging.getLogger(__name__)
_ANSI_ESCAPE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
_LARK_CONFIG_URL = re.compile(
    r"https://(?:open\.feishu\.cn|open\.larksuite\.com)/page/cli\?[^\s\x1b<>\"']+",
    re.IGNORECASE,
)
_MANAGED_READONLY_VIRTUAL_ROOTS = frozenset(
    {
        "/skills",
        "/knowledge",
        "/semantic-assets",
        "/sql-guardrails",
        "/analytics-models",
        "/large_tool_results",
    }
)


def _shared_runtime_executable(
    release: Path,
    requested_executable: str,
    expected_runtime_image_digest: str,
) -> str:
    """Validate one executable projection from the shared runtime manifest."""

    if re.fullmatch(r"[A-Za-z0-9_.-]+", requested_executable) is None:
        raise ValueError("managed runtime executable name is invalid")
    release = release.expanduser().resolve(strict=True)
    manifest_path = release / "runtime-manifest.json"
    if manifest_path.is_symlink():
        raise ValueError("managed runtime manifest must not be a symlink")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ValueError("managed runtime manifest is missing or invalid") from exc
    packages = manifest.get("packages") if isinstance(manifest, dict) else None
    if (
        not isinstance(manifest, dict)
        or manifest.get("version") != 1
        or manifest.get("kind") != "shared-node-runtime"
        or manifest.get("runtime_image_digest") != expected_runtime_image_digest
        or not isinstance(packages, dict)
    ):
        raise ValueError("managed runtime manifest contract is incompatible")
    targets: list[Path] = []
    for package in packages.values():
        bins = package.get("declared_bins") if isinstance(package, dict) else None
        relative = bins.get(requested_executable) if isinstance(bins, dict) else None
        if not isinstance(relative, str) or not relative:
            continue
        target = (release / relative).resolve(strict=True)
        target.relative_to(release)
        if not target.is_file():
            raise ValueError("managed runtime executable target is invalid")
        targets.append(target)
    launcher = release / "bin" / requested_executable
    if len(targets) != 1 or not launcher.is_symlink() or launcher.resolve(strict=True) != targets[0]:
        raise ValueError("managed runtime executable projection is invalid")
    return requested_executable


def _managed_readonly_path_aliases(
    docker_config: dict[str, Any],
) -> tuple[tuple[str, Path], ...]:
    """Return trusted virtual roots that exist in every execution runner.

    Docker receives these as read-only mounts.  Host runners need the same
    contract expressed as virtual-to-host aliases instead.
    """

    aliases: list[tuple[str, Path]] = []
    for item in docker_config.get("_managed_readonly_mounts") or []:
        if not isinstance(item, dict):
            continue
        virtual_root = str(item.get("target") or "").rstrip("/")
        if virtual_root not in _MANAGED_READONLY_VIRTUAL_ROOTS:
            continue
        source = Path(str(item.get("source") or "")).expanduser()
        if source.is_symlink() or not source.is_dir():
            continue
        canonical = source.resolve()
        if canonical != source:
            continue
        aliases.append((virtual_root, canonical))
    return tuple(dict.fromkeys(aliases))


def _rewrite_managed_virtual_paths(
    command: str,
    aliases: tuple[tuple[str, Path], ...],
) -> str:
    """Map platform-owned virtual roots without touching partial path names."""

    def quote_state(text: str, position: int) -> str | None:
        state: str | None = None
        escaped = False
        for character in text[:position]:
            if escaped:
                escaped = False
            elif character == "\\" and state != "'":
                escaped = True
            elif character in {"'", '"'}:
                if state == character:
                    state = None
                elif state is None:
                    state = character
        return state

    mapped = command
    for virtual_root, host_root in sorted(aliases, key=lambda item: len(item[0]), reverse=True):
        host_text = str(host_root)

        def replacement(match: re.Match[str]) -> str:
            state = quote_state(mapped, match.start())
            if state == "'":
                return host_text.replace("'", "'\"'\"'")
            if state == '"':
                return re.sub(r'([\\"$`])', r"\\\1", host_text)
            return shlex.quote(host_text)

        mapped = re.sub(
            rf"(?<![A-Za-z0-9_./-]){re.escape(virtual_root)}(?=(?:/|\s|$|[\"']))",
            replacement,
            mapped,
        )
    return mapped


def _resolve_execution_path_alias(
    raw_path: str,
    *,
    workspace_path: Path,
    scratch_path: Path | None,
    managed_readonly_path_aliases: tuple[tuple[str, Path], ...],
) -> str:
    """Resolve a model-facing locator to its host identity.

    This function does not grant access.  Tool policy compares the returned
    host path with the runner's read/write roots; runners may still execute
    the original virtual spelling (notably Docker bind-mount targets).
    """

    normalized = str(raw_path or "").replace("\\", "/")
    aliases: list[tuple[str, Path]] = [("/workspace", workspace_path)]
    if scratch_path is not None:
        aliases.append(("/scratch", scratch_path))
    aliases.extend(managed_readonly_path_aliases)
    for virtual_root, host_root in sorted(
        aliases,
        key=lambda item: len(item[0]),
        reverse=True,
    ):
        if normalized != virtual_root and not normalized.startswith(f"{virtual_root}/"):
            continue
        relative = PurePosixPath(normalized.removeprefix(virtual_root).lstrip("/") or ".")
        if ".." in relative.parts:
            return normalized
        return str((host_root / relative.as_posix()).resolve(strict=False))
    return normalized


def _macos_seatbelt_available() -> bool:
    if sys.platform != "darwin" or not Path("/usr/bin/sandbox-exec").is_file():
        logger.warning("Kernel sandbox unavailable: macOS sandbox-exec is not present")
        return False
    from harness.kernel_sandbox import MacOSSeatbeltRunner

    available, reason = MacOSSeatbeltRunner.probe()
    if not available:
        logger.warning(
            "Kernel sandbox probe failed; will retry after the short failure TTL: %s",
            reason,
        )
    return available


def _linux_bwrap_available() -> bool:
    if sys.platform != "linux":
        logger.warning("Kernel sandbox unavailable: host is not Linux")
        return False
    from harness.kernel_sandbox import LinuxBwrapSeccompRunner

    available, reason = LinuxBwrapSeccompRunner.probe()
    if not available:
        logger.warning(
            "Linux bubblewrap kernel sandbox probe failed; will retry after the short failure TTL: %s",
            reason,
        )
    return available


def _kernel_sandbox_available() -> bool:
    if sys.platform == "darwin":
        return _macos_seatbelt_available()
    if sys.platform == "linux":
        return _linux_bwrap_available()
    logger.warning("Kernel sandbox unavailable: unsupported host platform %s", sys.platform)
    return False


@dataclass(frozen=True)
class ManagedProviderExecutionResult:
    output: str
    exit_code: int
    credential_state: bytes | None
    truncated: bool = False
    browser_status: str | None = None
    browser_job_id: str | None = None


@dataclass(frozen=True)
class ManagedNodePackageResolution:
    package: str
    version: str
    integrity: str
    distribution: str
    runtime_image_digest: str
    executables: tuple[str, ...] = ()


def _lark_config_verification_url(output: str) -> str | None:
    cleaned = _ANSI_ESCAPE.sub("", str(output or ""))
    match = _LARK_CONFIG_URL.search(cleaned)
    return match.group(0).rstrip(".,;:!?)]]}") if match else None


def _credential_state_paths(
    paths: tuple[str, ...] | list[str],
    *,
    allow_empty: bool = False,
) -> tuple[str, ...]:
    """Validate Adapter-declared paths before using them in Docker argv."""

    if not paths and allow_empty:
        return ()
    if not paths:
        raise ValueError("managed credential state requires at least one path")
    normalized: list[str] = []
    for raw_path in paths:
        value = str(raw_path or "")
        path = PurePosixPath(value)
        if (
            not value
            or value.startswith("/")
            or "\\" in value
            or any(ord(character) < 32 or ord(character) == 127 for character in value)
            or value != path.as_posix()
            or path == PurePosixPath(".")
            or any(part in {"", ".", ".."} for part in path.parts)
            or any(part.startswith("-") for part in path.parts)
        ):
            raise ValueError("managed credential state path must be normalized and HOME-relative")
        normalized.append(value)
    if len(set(normalized)) != len(normalized):
        raise ValueError("managed credential state paths must be unique")
    normalized_parts = tuple(PurePosixPath(path).parts for path in normalized)
    for index, path in enumerate(normalized_parts):
        for other in normalized_parts[index + 1 :]:
            if path[: len(other)] == other or other[: len(path)] == path:
                raise ValueError("managed credential state paths must not overlap")
    return tuple(normalized)


def _credential_state_mkdir_command(paths: tuple[str, ...] | list[str]) -> str:
    absolute = [f"/home/puddingclaw/{path}" for path in _credential_state_paths(paths)]
    return "umask 077; mkdir -p -- " + " ".join(shlex.quote(path) for path in absolute)


def _credential_state_export_argv(
    container_name: str,
    paths: tuple[str, ...] | list[str],
) -> list[str]:
    return [
        "exec",
        container_name,
        "tar",
        "-czf",
        "-",
        "-C",
        "/home/puddingclaw",
        "--",
        *_credential_state_paths(paths),
    ]


def _managed_credential_state_tmpfs_args() -> list[str]:
    """Mask every Adapter-owned secret path in ordinary workspace runners."""

    roots: list[str] = []
    for spec in ManagedCliRegistry.credential_state_specs():
        roots.extend(_credential_state_paths(spec.paths))
    unique_roots = tuple(dict.fromkeys(roots))
    args: list[str] = []
    for root in unique_roots:
        args.extend(
            [
                "--tmpfs",
                f"/home/puddingclaw/{root}:rw,nosuid,nodev,size=16m",
            ]
        )
    return args


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
        parts.extend(
            f"[stderr] {line}"
            for line in stderr.strip().splitlines()
            if "ev_poll_posix.cc" not in line or "FD from fork parent still in poll list" not in line
        )
    output = "\n".join(parts) if parts else "<no output>"
    encoded = output.encode("utf-8", errors="replace")
    if len(encoded) <= max_output_bytes:
        return output, False
    truncated = encoded[:max_output_bytes].decode("utf-8", errors="ignore")
    return f"{truncated}\n\n... Output truncated at {max_output_bytes} bytes.", True


class SpawnWorkspaceBackend(FilesystemBackend, SandboxBackendProtocol):
    """Host-spawn backend whose commands still require Tool policy."""

    mode = "spawn"
    filesystem_mode = "restricted"
    _workspace_locks: dict[str, threading.RLock] = {}
    _workspace_locks_guard = threading.Lock()

    def __init__(
        self,
        *,
        root_dir: Path,
        scratch_path: Path | None = None,
        timeout: int = 120,
        max_output_bytes: int = 100_000,
        managed_readonly_path_aliases: tuple[tuple[str, Path], ...] = (),
    ) -> None:
        super().__init__(root_dir=root_dir, virtual_mode=True)
        self.workspace_path = root_dir.expanduser().resolve()
        self.scratch_path = scratch_path.expanduser().resolve() if scratch_path is not None else None
        self._default_timeout = timeout
        self._max_output_bytes = max_output_bytes
        self.managed_readonly_path_aliases = tuple(managed_readonly_path_aliases)
        self.managed_readonly_host_roots = tuple(
            host_root for _virtual_root, host_root in self.managed_readonly_path_aliases
        )
        workspace_digest = hashlib.sha256(str(self.workspace_path).encode("utf-8")).hexdigest()[:16]
        self._id = f"spawn:{workspace_digest}"
        runtime_dir = self.workspace_path / ".puddingclaw" / "runtime"
        runtime_dir.mkdir(parents=True, exist_ok=True)
        run_tmp = self.scratch_path / "tmp" if self.scratch_path is not None else runtime_dir / "tmp"
        self._env = {
            "PATH": os.environ.get(
                "PATH",
                "/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin",
            ),
            "HOME": str(runtime_dir / "host-home"),
            "TMPDIR": str(run_tmp),
            "LANG": os.environ.get("LANG", "C.UTF-8"),
            "LC_ALL": os.environ.get("LC_ALL", ""),
        }
        Path(self._env["HOME"]).mkdir(parents=True, exist_ok=True)
        Path(self._env["TMPDIR"]).mkdir(parents=True, exist_ok=True)

    def _execution_environment(self) -> dict[str, str]:
        """Return the runner environment for the current filesystem mode.

        Restricted execution keeps an isolated HOME.  Smart trusted-local is
        intentionally the host user's filesystem environment, so ``~`` and
        programs that resolve files through HOME must address the real user
        home just like an absolute host path does.
        """

        environment = {key: value for key, value in self._env.items() if value}
        if self.filesystem_mode == "unrestricted":
            environment["HOME"] = str(Path(os.environ.get("HOME") or Path.home()).expanduser().resolve())
        return environment

    @property
    def id(self) -> str:
        return self._id

    @property
    def filesystem_read_roots(self) -> tuple[Path, ...]:
        if self.filesystem_mode == "unrestricted":
            return ()
        return tuple(
            dict.fromkeys(
                (
                    self.workspace_path,
                    *((self.scratch_path,) if self.scratch_path is not None else ()),
                    *self.managed_readonly_host_roots,
                )
            )
        )

    @property
    def filesystem_write_roots(self) -> tuple[Path, ...]:
        if self.filesystem_mode == "unrestricted":
            return ()
        return tuple(root for root in (self.workspace_path, self.scratch_path) if root is not None)

    filesystem_delete_roots = filesystem_write_roots

    def resolve_execution_path(self, raw_path: str) -> str:
        return _resolve_execution_path_alias(
            raw_path,
            workspace_path=self.workspace_path,
            scratch_path=self.scratch_path,
            managed_readonly_path_aliases=self.managed_readonly_path_aliases,
        )

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
        command = _rewrite_managed_virtual_paths(
            command,
            self.managed_readonly_path_aliases,
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
                    env=self._execution_environment(),
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

    def execute_external_directory(
        self,
        directory_path: str,
        command: str,
        *,
        timeout: int | None = None,
        writable: bool = False,
    ) -> ExecuteResponse:
        """Run a typed external-directory command directly on the host.

        Spawn has no OS filesystem boundary; the exact directory is still
        canonicalized here so the convenience tool has the same target
        identity as Kernel. Tool Gate owns the effect decision and draft
        lease, while this method owns only process lifecycle.
        """

        del writable  # The caller validates writable drafts and their lease.
        try:
            directory = Path(directory_path).expanduser()
            if not directory.is_absolute() or directory.is_symlink() or not directory.is_dir():
                raise ValueError("external directory must be an absolute non-symlink directory")
            directory = directory.resolve(strict=True)
            if directory == self.workspace_path or self.workspace_path in directory.parents:
                raise ValueError("external directory must not be inside the workspace")
            effective_timeout = timeout if timeout is not None else self._default_timeout
            if not isinstance(effective_timeout, int) or effective_timeout <= 0:
                raise ValueError("timeout must be a positive integer")
            with self._workspace_lock(str(directory)):
                result = subprocess.run(  # noqa: S602
                    command,
                    check=False,
                    shell=True,
                    cwd=directory,
                    env=self._execution_environment(),
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
            return ExecuteResponse(output=output, exit_code=result.returncode, truncated=truncated)
        except subprocess.TimeoutExpired:
            return ExecuteResponse(
                output=f"Error: Command timed out after {timeout or self._default_timeout} seconds.",
                exit_code=124,
            )
        except Exception as exc:  # noqa: BLE001
            return ExecuteResponse(
                output=f"Error executing external directory command ({type(exc).__name__}): {exc}",
                exit_code=1,
            )

    @classmethod
    def _workspace_lock(cls, key: str) -> threading.RLock:
        with cls._workspace_locks_guard:
            return cls._workspace_locks.setdefault(key, threading.RLock())


class KernelWorkspaceBackend(FilesystemBackend, SandboxBackendProtocol):
    """Workspace filesystem whose shell is always backed by an OS sandbox."""

    mode = "kernel"
    filesystem_mode = "restricted"

    def __init__(
        self,
        *,
        root_dir: Path,
        scratch_path: Path,
        timeout: int = 120,
        managed_readonly_path_aliases: tuple[tuple[str, Path], ...] = (),
    ) -> None:
        super().__init__(root_dir=root_dir, virtual_mode=True)
        self.workspace_path = root_dir.expanduser().resolve()
        self.scratch_path = scratch_path.expanduser().resolve()
        if not self.workspace_path.is_dir() or not self.scratch_path.is_dir():
            raise ValueError("Kernel workspace and scratch roots must exist")
        if not _kernel_sandbox_available():
            raise RuntimeError("No supported kernel sandbox is available on this host")
        self._default_timeout = timeout
        self.managed_readonly_path_aliases = tuple(managed_readonly_path_aliases)
        self.managed_readonly_host_roots = tuple(
            host_root for _virtual_root, host_root in self.managed_readonly_path_aliases
        )
        self._host_runtime: Any | None = None
        self._host_runtime_lock = threading.RLock()
        workspace_digest = hashlib.sha256(str(self.workspace_path).encode("utf-8")).hexdigest()[:16]
        self._kernel_runner_mode = "kernel_macos_seatbelt" if sys.platform == "darwin" else "kernel_linux_bwrap_seccomp"
        self._id = f"kernel:{self._kernel_runner_mode.removeprefix('kernel_')}:{workspace_digest}"

    @property
    def id(self) -> str:
        return self._id

    @property
    def filesystem_read_roots(self) -> tuple[Path, ...]:
        if self.filesystem_mode == "unrestricted":
            return ()
        return tuple(
            dict.fromkeys(
                (
                    self.workspace_path,
                    self.scratch_path,
                    *self.managed_readonly_host_roots,
                )
            )
        )

    @property
    def filesystem_write_roots(self) -> tuple[Path, ...]:
        if self.filesystem_mode == "unrestricted":
            return ()
        return (self.workspace_path, self.scratch_path)

    filesystem_delete_roots = filesystem_write_roots

    def resolve_execution_path(self, raw_path: str) -> str:
        return _resolve_execution_path_alias(
            raw_path,
            workspace_path=self.workspace_path,
            scratch_path=self.scratch_path,
            managed_readonly_path_aliases=self.managed_readonly_path_aliases,
        )

    @property
    def kernel_runner_mode(self) -> str:
        return self._kernel_runner_mode

    @property
    def kernel_runner_binding_digest(self) -> str:
        from harness.kernel_sandbox import kernel_runner_binding_digest

        return kernel_runner_binding_digest()

    def execute(
        self,
        command: str,
        *,
        timeout: int | None = None,
    ) -> ExecuteResponse:
        from harness.execution_context import current_authorized_execution
        from harness.kernel_sandbox import kernel_runner_for_profile

        authorized = current_authorized_execution()
        if authorized is None:
            return ExecuteResponse(
                output="Error: Kernel execution requires a Tool Gate execution permit.",
                exit_code=126,
            )
        profile = authorized.profile
        if profile.workspace_root != self.workspace_path or profile.scratch_root != self.scratch_path:
            return ExecuteResponse(
                output="Error: Execution permit belongs to another workspace.",
                exit_code=126,
            )
        runner = kernel_runner_for_profile(
            profile,
            runtime_root=self.scratch_path / ".kernel-runtime",
        )
        if not authorized.valid_at_spawn(
            command=command,
            selected_runner=runner.mode,
            runner_binding_digest=self.kernel_runner_binding_digest,
        ):
            return ExecuteResponse(
                output="Error: Kernel execution permit became invalid before process spawn.",
                exit_code=126,
            )
        execution_command = _rewrite_managed_virtual_paths(
            authorized.execution_command,
            self.managed_readonly_path_aliases,
        )
        response = runner.execute(
            execution_command,
            timeout=timeout or self._default_timeout,
            spawn_guard=lambda: authorized.consume_at_spawn(
                command=command,
                selected_runner=runner.mode,
                runner_binding_digest=self.kernel_runner_binding_digest,
            ),
            environment=dict(authorized.environment),
        )
        output = response.output
        for secret in sorted(
            {value for value in authorized.secret_values if value},
            key=len,
            reverse=True,
        ):
            output = output.replace(secret, "***")
        if output == response.output:
            return response
        return ExecuteResponse(
            output=output,
            exit_code=response.exit_code,
            truncated=response.truncated,
        )

    def execute_external_directory(
        self,
        directory_path: str,
        command: str,
        *,
        timeout: int | None = None,
        writable: bool = False,
    ) -> ExecuteResponse:
        """Run a HostFileBroker-approved command in one exact external root."""

        from harness.kernel_sandbox import kernel_runner_for_profile

        effective_timeout = timeout if timeout is not None else self._default_timeout
        try:
            directory = Path(directory_path).expanduser()
            if not directory.is_absolute() or directory.is_symlink() or not directory.is_dir():
                raise ValueError("external directory must be an absolute non-symlink directory")
            directory = directory.resolve(strict=True)
            if directory == self.workspace_path or self.workspace_path in directory.parents:
                raise ValueError("external directory must not be inside the workspace")
            execution_command = command
            validator_dir = (Path(__file__).resolve().parent / "docker").resolve()
            external_read_roots: tuple[Path, ...] = (directory,)
            if "/opt/puddingclaw/bin/validate-html-report-e2e.mjs" in command:
                if not validator_dir.is_dir() or validator_dir.is_symlink():
                    raise ValueError("managed HTML validator directory is unavailable")
                execution_command = _rewrite_managed_virtual_paths(
                    command,
                    (("/opt/puddingclaw/bin", validator_dir),),
                )
                external_read_roots += (validator_dir,)
            profile = SandboxGrantProfile.build(
                workspace_root=self.workspace_path,
                scratch_root=self.scratch_path,
                external_read_roots=external_read_roots,
                external_write_roots=(directory,) if writable else (),
                workspace_writable=False,
                network_allowed=False,
                timeout_seconds=effective_timeout,
            )
            runner = kernel_runner_for_profile(
                profile,
                runtime_root=(self.scratch_path / ".kernel-runtime" if sys.platform == "linux" else None),
            )
            return runner.execute(
                execution_command,
                timeout=effective_timeout,
                cwd=directory,
            )
        except Exception as exc:  # noqa: BLE001
            return ExecuteResponse(
                output=f"Error executing external directory command ({type(exc).__name__}): {exc}",
                exit_code=1,
            )

    def run_html_report_e2e(
        self,
        html_path: Path,
        *,
        timeout: int,
    ) -> ExecuteResponse:
        """Run the fixed browser validator with a narrowly typed kernel profile."""

        from harness.kernel_sandbox import kernel_runner_for_profile

        try:
            html = Path(html_path).expanduser()
            if not html.is_absolute() or html.is_symlink() or not html.is_file():
                raise ValueError("HTML report must be an absolute non-symlink file")
            html = html.resolve(strict=True)
            script = (Path(__file__).resolve().parent / "docker" / "validate-html-report-e2e.mjs").resolve(strict=True)
            if script.is_symlink() or not script.is_file():
                raise ValueError("managed HTML validator is not a regular file")

            browser_candidates = (
                Path("/usr/bin/chromium"),
                Path("/usr/bin/chromium-browser"),
                Path("/usr/bin/google-chrome"),
                Path("/opt/homebrew/bin/chromium"),
                Path("/opt/homebrew/bin/chromium-browser"),
                Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"),
            )

            browser = next(
                (
                    candidate.resolve(strict=True)
                    for candidate in browser_candidates
                    if candidate.is_file() and not candidate.is_symlink()
                ),
                None,
            )
            if browser is None:
                return ExecuteResponse(
                    output="Error: no supported Chromium/Chrome executable is installed for HTML browser validation.",
                    exit_code=1,
                )
            profile = SandboxGrantProfile.build(
                workspace_root=self.workspace_path,
                scratch_root=self.scratch_path,
                external_read_roots=(html.parent, script.parent, browser.parent),
                workspace_writable=False,
                network_allowed=False,
                timeout_seconds=timeout,
            )
            runner = kernel_runner_for_profile(
                profile,
                runtime_root=(self.scratch_path / ".kernel-runtime" if sys.platform == "linux" else None),
            )
            command = shlex.join(["node", str(script), html.name])
            return runner.execute(
                command,
                timeout=timeout,
                cwd=html.parent,
                environment={"PUPPETEER_EXECUTABLE_PATH": str(browser)},
            )
        except Exception as exc:  # noqa: BLE001
            return ExecuteResponse(
                output=f"Error executing managed HTML validator ({type(exc).__name__}): {exc}",
                exit_code=1,
            )

    def _host_runtime_backend(self) -> Any:
        from harness.host_skill_runtime import HostSkillRuntimeBackend
        from runtime_identity.paths import PuddingClawPaths

        with self._host_runtime_lock:
            if self._host_runtime is None:
                self._host_runtime = HostSkillRuntimeBackend(
                    PuddingClawPaths.from_environment(),
                    timeout=max(self._default_timeout, 900),
                )
            return self._host_runtime

    def install_packages(self, *args: Any, **kwargs: Any) -> ExecuteResponse:
        """Install ordinary Skill dependencies on the host ABI under Seatbelt."""

        return self._host_runtime_backend().install_packages(*args, **kwargs)

    def prepare_host_skill_execution(
        self,
        command: str,
        *,
        active_skill_ids: tuple[str, ...] = (),
    ) -> Any:
        """Return an immutable host-runtime projection for an installed Skill."""

        match = re.search(r"/skills/([A-Za-z0-9][A-Za-z0-9_.-]{0,127})(?:/|\b)", command)
        skills_root = next(
            (root for virtual, root in self.managed_readonly_path_aliases if virtual == "/skills"),
            None,
        )
        if skills_root is None:
            from harness.host_skill_runtime import HostExecutionProjection

            return HostExecutionProjection(command)
        selected_skill_id = match.group(1) if match is not None else None
        if selected_skill_id is None:
            trusted_ids = tuple(dict.fromkeys(active_skill_ids))
            if len(trusted_ids) == 1:
                selected_skill_id = trusted_ids[0]
            elif trusted_ids:
                published = self._host_runtime_backend().published_python_skill_ids(
                    trusted_ids,
                    self.managed_readonly_path_aliases,
                )
                if len(published) == 1:
                    selected_skill_id = published[0]
        if selected_skill_id is None:
            from harness.host_skill_runtime import HostExecutionProjection

            return HostExecutionProjection(command)
        return self._host_runtime_backend().project_skill_execution(
            command,
            self.managed_readonly_path_aliases,
            skill_id=selected_skill_id,
        )

    def prepare_host_execution(
        self,
        command: str,
        *,
        active_skill_ids: tuple[str, ...] = (),
    ) -> Any:
        """Project published ordinary Skill/CLI runtimes into one command."""

        from runtime_identity.paths import PuddingClawPaths

        paths = PuddingClawPaths.from_environment()
        has_node_runtime = (paths.root / "runtime" / "node").is_dir()
        skill_projection = self.prepare_host_skill_execution(
            command,
            active_skill_ids=active_skill_ids,
        )
        if "/skills/" in command or skill_projection.environment_binding_digest:
            return skill_projection
        if not has_node_runtime:
            return skill_projection
        # The shared Node runtime is only relevant to Backend-owned managed
        # CLIs. Do not initialize HostSkillRuntimeBackend for ordinary shell
        # commands such as `pwd`; that would make an unrelated command depend
        # on the host Python shared-library layout.
        if not ManagedCliRegistry().claims(command):
            return skill_projection
        return self._host_runtime_backend().project_cli_execution(command)


class DeferredKernelWorkspaceBackend(FilesystemBackend, SandboxBackendProtocol):
    """Executable DeepAgents backend held at a Kernel-unavailable boundary."""

    mode = "kernel"
    kernel_unavailable = True

    def __init__(
        self,
        *,
        root_dir: Path,
        scratch_path: Path,
        timeout: int,
        managed_readonly_path_aliases: tuple[tuple[str, Path], ...],
        reason: str,
    ) -> None:
        super().__init__(root_dir=root_dir, virtual_mode=True)
        self.workspace_path = root_dir.expanduser().resolve()
        self.scratch_path = scratch_path.expanduser().resolve()
        self._timeout = timeout
        self.managed_readonly_path_aliases = tuple(managed_readonly_path_aliases)
        self.managed_readonly_host_roots = tuple(path for _name, path in self.managed_readonly_path_aliases)
        self.fallback_reason = reason
        digest = hashlib.sha256(str(self.workspace_path).encode()).hexdigest()[:16]
        self._id = f"kernel-unavailable:{digest}"
        self._delegate: SpawnWorkspaceBackend | None = None

    @property
    def id(self) -> str:
        return self._delegate.id if self._delegate is not None else self._id

    @property
    def effective_mode(self) -> str:
        return "spawn" if self._delegate is not None else "kernel"

    @property
    def filesystem_read_roots(self) -> tuple[Path, ...]:
        return tuple(
            dict.fromkeys(
                (
                    self.workspace_path,
                    self.scratch_path,
                    *self.managed_readonly_host_roots,
                )
            )
        )

    @property
    def filesystem_write_roots(self) -> tuple[Path, ...]:
        return (self.workspace_path, self.scratch_path)

    filesystem_delete_roots = filesystem_write_roots

    def resolve_execution_path(self, raw_path: str) -> str:
        return _resolve_execution_path_alias(
            raw_path,
            workspace_path=self.workspace_path,
            scratch_path=self.scratch_path,
            managed_readonly_path_aliases=self.managed_readonly_path_aliases,
        )

    @property
    def kernel_runner_mode(self) -> str:
        return "kernel_macos_seatbelt" if sys.platform == "darwin" else "kernel_linux_bwrap_seccomp"

    @property
    def kernel_runner_binding_digest(self) -> str:
        return ""

    def activate_spawn(self) -> None:
        if self._delegate is None:
            self._delegate = SpawnWorkspaceBackend(
                root_dir=self.workspace_path,
                scratch_path=self.scratch_path,
                timeout=self._timeout,
                managed_readonly_path_aliases=self.managed_readonly_path_aliases,
            )
        self.kernel_unavailable = False

    def execute(self, command: str, *, timeout: int | None = None) -> ExecuteResponse:
        if self._delegate is not None:
            return self._delegate.execute(command, timeout=timeout)
        return ExecuteResponse(
            output=f"Error: Kernel execution is unavailable and no explicit fallback was approved: {self.fallback_reason}",
            exit_code=126,
        )

    def execute_external_directory(
        self, directory_path: str, command: str, *, timeout: int | None = None, writable: bool = False
    ) -> ExecuteResponse:
        if self._delegate is not None:
            return self._delegate.execute_external_directory(
                directory_path, command, timeout=timeout, writable=writable
            )
        return ExecuteResponse(
            output="Error: Kernel execution is unavailable; external directory execution is blocked until an explicit Run fallback.",
            exit_code=126,
        )

    def run_html_report_e2e(self, html_path: Path, *, timeout: int) -> ExecuteResponse:
        if self._delegate is not None:
            return self._delegate.run_html_report_e2e(html_path, timeout=timeout)
        return ExecuteResponse(
            output="Error: Kernel HTML validation is unavailable; no implicit host fallback is allowed.",
            exit_code=126,
        )

    def __getattr__(self, name: str) -> Any:
        if name.startswith("_"):
            raise AttributeError(name)
        delegate = self.__dict__.get("_delegate")
        if delegate is not None:
            return getattr(delegate, name)
        raise AttributeError(name)


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

    def gc_legacy_unscoped_workspace_containers(self, unscoped_root: Path) -> int:
        """Remove obsolete per-Session containers while preserving their files.

        Before unscoped Sessions shared a default workspace, each direct child
        of ``unscoped_root`` received its own project container.  The bind
        mounted workspace remains on the host after container removal.
        """

        root = _canonical_docker_mount_source(str(unscoped_root.expanduser().resolve()))
        listed = self._run(
            [
                "ps",
                "-aq",
                "--filter",
                "label=com.puddingclaw.managed=true",
                "--filter",
                "label=com.puddingclaw.kind=workspace",
                "--filter",
                f"label=com.puddingclaw.owner={self._owner_label()}",
            ],
            timeout=30,
        )
        if listed.returncode != 0:
            return 0
        removed = 0
        for container_id in (item.strip() for item in listed.stdout.splitlines()):
            if not container_id:
                continue
            inspected = self._run(["inspect", container_id], timeout=30)
            if inspected.returncode != 0:
                continue
            try:
                container = json.loads(inspected.stdout)[0]
            except (json.JSONDecodeError, IndexError, TypeError):
                continue
            labels = container.get("Config", {}).get("Labels") or {}
            if (
                labels.get("com.puddingclaw.managed") != "true"
                or labels.get("com.puddingclaw.kind") != "workspace"
                or labels.get("com.puddingclaw.owner") != self._owner_label()
            ):
                continue
            workspace_mount = next(
                (
                    item
                    for item in container.get("Mounts") or []
                    if isinstance(item, dict) and item.get("Type") == "bind" and item.get("Destination") == "/workspace"
                ),
                None,
            )
            if workspace_mount is None:
                continue
            source = _canonical_docker_mount_source(str(workspace_mount.get("Source") or ""))
            try:
                relative = Path(source).relative_to(root)
            except ValueError:
                continue
            if len(relative.parts) != 1 or relative.name == "default":
                continue
            result = self._run(["rm", "-f", container_id], timeout=30)
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
        raw_timeout = self.config.get("probe_timeout_seconds", 5)
        try:
            timeout = max(1, min(int(raw_timeout), 30))
        except (TypeError, ValueError):
            timeout = 5
        try:
            result = self._run(
                ["version", "--format", "{{.Server.Version}}"],
                timeout=timeout,
            )
        except Exception as exc:  # noqa: BLE001
            return False, f"{type(exc).__name__}: {exc}"
        if result.returncode != 0:
            return False, result.stderr.strip() or result.stdout.strip()
        return True, result.stdout.strip()

    @staticmethod
    def _container_name(workspace: Path) -> str:
        digest = hashlib.sha256(str(workspace).encode("utf-8")).hexdigest()[:16]
        if (
            workspace.name == "default"
            and workspace.parent.name == "unscoped"
            and workspace.parent.parent.name == "agent-workspaces"
        ):
            # Keep the role visible in Docker Desktop while retaining enough
            # path entropy to avoid collisions across local installations.
            return f"puddingclaw-project-default-{digest[:8]}"
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
            if source.is_dir() and target in {
                *_MANAGED_READONLY_VIRTUAL_ROOTS,
                "/opt/puddingclaw/toolchain/node",
            }:
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
            from runtime_identity.software_runtime import SoftwareRuntimeManager

            image = str(self.config.get("image") or DEFAULT_SANDBOX_IMAGE)
            image_id = self.ensure_image(image)
            current = SoftwareRuntimeManager(
                PuddingClawPaths.from_environment(),
                RUNTIME_CONTRACT,
            ).node_current(image_id)
            readonly_mounts.append(
                {
                    # Resolve on every spec calculation. An atomic ``current``
                    # switch therefore changes the source and forces stale
                    # workspace containers to be recreated.
                    "source": str(current),
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
                    "PATH=/home/puddingclaw/.local/bin:/opt/puddingclaw/toolchain/node/public-bin:"
                    "/home/puddingclaw/.npm-global/bin:"
                    "/usr/local/bin:/usr/bin:/bin"
                ),
                "--env",
                (
                    "NODE_PATH=/opt/puddingclaw/toolchain/node/lib/node_modules:"
                    "/home/puddingclaw/.npm-global/lib/node_modules"
                ),
                *_managed_credential_state_tmpfs_args(),
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
                "PATH=/home/puddingclaw/.local/bin:/opt/puddingclaw/toolchain/node/public-bin:"
                "/home/puddingclaw/.npm-global/bin:"
                "/usr/local/bin:/usr/bin:/bin"
            ),
            "--env",
            (
                "NODE_PATH=/opt/puddingclaw/toolchain/node/lib/node_modules:"
                "/home/puddingclaw/.npm-global/lib/node_modules"
            ),
            *_managed_credential_state_tmpfs_args(),
            "--entrypoint",
            argv[0],
        ]
        if spec["uid"] is not None and spec["gid"] is not None:
            args.extend(["--user", f"{spec['uid']}:{spec['gid']}"])
        args.extend([image_id, *argv[1:]])
        if self._interactive_lark_authorization(argv):
            job_key = hashlib.sha256(f"{workspace}:{json.dumps(argv, ensure_ascii=False)}".encode()).hexdigest()[:24]
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
            bounded_command = f"timeout --signal=TERM --kill-after=10s {ttl_seconds}s sh -c {shlex.quote(argv[2])}"
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

    def managed_runtime_image_digest(self, workspace: Path | None = None) -> str:
        workspace = (workspace or Path.cwd()).expanduser().resolve()
        return self.ensure_image(self._spec(workspace)["image"])

    def inspect_managed_runtime_image_digest(self, workspace: Path | None = None) -> str:
        """Resolve the locally present immutable image id without pull/build."""

        workspace = (workspace or Path.cwd()).expanduser().resolve()
        return self._image_id(self._spec(workspace)["image"])

    def _managed_runtime_build_args(
        self,
        *,
        workspace: Path,
        runtime_path: Path | None,
        container_path: str,
        expected_runtime_image_digest: str,
        kind: str,
        network: str,
        extra_mounts: list[str] | None = None,
    ) -> tuple[list[str], str]:
        """Build an isolated installer prefix bound to one immutable image."""

        from runtime_identity.paths import PuddingClawPaths

        runtime_path = runtime_path.expanduser().resolve(strict=True)
        runtime_path.relative_to(PuddingClawPaths.from_environment().root / "runtime")
        if runtime_path.is_symlink() or not runtime_path.is_dir():
            raise ValueError("managed runtime candidate must be a real directory")
        if not re.fullmatch(r"/opt/puddingclaw/runtime/[A-Za-z0-9._/-]+", container_path):
            raise ValueError("managed runtime container path is invalid")
        spec = self._spec(workspace)
        image_id = self.ensure_image(spec["image"])
        if image_id != expected_runtime_image_digest:
            raise ValueError("managed runtime image changed after dependency approval")
        args = [
            "run",
            "--rm",
            "--label",
            "com.puddingclaw.managed=true",
            "--label",
            f"com.puddingclaw.kind={kind}",
            "--label",
            f"com.puddingclaw.owner={self._owner_label()}",
            "--network",
            network,
            "--read-only",
            "--tmpfs",
            "/tmp:rw,nosuid,nodev,size=512m",
            "--tmpfs",
            "/home/puddingclaw:rw,nosuid,nodev,size=256m",
            "--mount",
            f"type=bind,src={runtime_path},dst={container_path}",
            *(extra_mounts or []),
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
            "UV_CACHE_DIR=/home/puddingclaw/.cache/uv",
            "--workdir",
            container_path,
        ]
        if spec["uid"] is not None and spec["gid"] is not None:
            args.extend(["--user", f"{spec['uid']}:{spec['gid']}"])
        return args, image_id

    def resolve_shared_node_runtime(
        self,
        workspace: Path | None = None,
        *,
        dependencies: dict[str, str],
        expected_runtime_image_digest: str,
        resolution_path: Path,
        timeout: int = 300,
    ) -> ExecuteResponse:
        """Generate a complete npm lock without executing package scripts."""

        from runtime_identity.software_runtime import parse_exact_node_distribution

        workspace = (workspace or Path.cwd()).expanduser().resolve()
        normalized: dict[str, str] = {}
        for package, version in sorted(dependencies.items()):
            parsed_package, parsed_version = parse_exact_node_distribution(f"{package}@{version}")
            normalized[parsed_package] = parsed_version
        resolution_path = resolution_path.expanduser().resolve(strict=True)
        package_json = {
            "name": "puddingclaw-managed-runtime",
            "private": True,
            "version": "0.0.0",
            "dependencies": normalized,
        }
        package_path = resolution_path / "package.json"
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        descriptor = os.open(package_path, flags, 0o600)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8", closefd=False) as destination:
                json.dump(package_json, destination, ensure_ascii=False, sort_keys=True)
                destination.write("\n")
                destination.flush()
                os.fsync(destination.fileno())
        finally:
            os.close(descriptor)
        args, image_id = self._managed_runtime_build_args(
            workspace=workspace,
            runtime_path=resolution_path,
            container_path="/opt/puddingclaw/runtime/node-resolution",
            expected_runtime_image_digest=expected_runtime_image_digest,
            kind="node-runtime-resolver",
            network="bridge",
        )
        args.extend(
            [
                "--entrypoint",
                "npm",
                image_id,
                "install",
                "--package-lock-only",
                "--ignore-scripts",
                "--no-audit",
                "--no-fund",
                "--registry=https://registry.npmjs.org/",
            ]
        )
        try:
            result = self._run(args, timeout=timeout)
        except subprocess.TimeoutExpired:
            return ExecuteResponse(output="Node runtime resolution timed out.", exit_code=124)
        output, truncated = _bounded_output(result.stdout, result.stderr, max_output_bytes=100_000)
        return ExecuteResponse(output=output, exit_code=result.returncode, truncated=truncated)

    def build_shared_node_runtime(
        self,
        workspace: Path | None = None,
        *,
        expected_runtime_image_digest: str,
        runtime_path: Path,
        container_path: str,
        timeout: int = 900,
    ) -> ExecuteResponse:
        """Materialize a frozen npm lock from an empty node_modules tree."""

        workspace = (workspace or Path.cwd()).expanduser().resolve()
        args, image_id = self._managed_runtime_build_args(
            workspace=workspace,
            runtime_path=runtime_path,
            container_path=container_path,
            expected_runtime_image_digest=expected_runtime_image_digest,
            kind="node-runtime-builder",
            network="bridge",
        )
        args.extend(
            [
                "--entrypoint",
                "npm",
                image_id,
                "ci",
                "--no-audit",
                "--no-fund",
                "--registry=https://registry.npmjs.org/",
            ]
        )
        try:
            result = self._run(args, timeout=timeout)
        except subprocess.TimeoutExpired:
            return ExecuteResponse(output="Node runtime build timed out.", exit_code=124)
        output, truncated = _bounded_output(result.stdout, result.stderr, max_output_bytes=100_000)
        return ExecuteResponse(output=output, exit_code=result.returncode, truncated=truncated)

    def resolve_python_skill_runtime(
        self,
        workspace: Path | None = None,
        *,
        expected_runtime_image_digest: str,
        resolution_path: Path,
        timeout: int = 300,
    ) -> ExecuteResponse:
        """Resolve exact Skill requirements into a transitive hash lock."""

        workspace = (workspace or Path.cwd()).expanduser().resolve()
        args, image_id = self._managed_runtime_build_args(
            workspace=workspace,
            runtime_path=resolution_path,
            container_path="/opt/puddingclaw/runtime/python-resolution",
            expected_runtime_image_digest=expected_runtime_image_digest,
            kind="python-skill-resolver",
            network="bridge",
        )
        args.extend(
            [
                "--entrypoint",
                "uv",
                image_id,
                "pip",
                "compile",
                "--generate-hashes",
                "--no-header",
                "--no-annotate",
                "--index-url",
                "https://pypi.org/simple",
                "--output-file",
                "/opt/puddingclaw/runtime/python-resolution/requirements.lock",
                "/opt/puddingclaw/runtime/python-resolution/requirements.in",
            ]
        )
        try:
            result = self._run(args, timeout=timeout)
        except subprocess.TimeoutExpired:
            return ExecuteResponse(output="Python Skill dependency resolution timed out.", exit_code=124)
        output, truncated = _bounded_output(result.stdout, result.stderr, max_output_bytes=100_000)
        return ExecuteResponse(output=output, exit_code=result.returncode, truncated=truncated)

    def build_python_skill_runtime(
        self,
        workspace: Path | None = None,
        *,
        expected_runtime_image_digest: str,
        runtime_path: Path,
        container_path: str,
        uv_cache_path: Path,
        timeout: int = 900,
    ) -> ExecuteResponse:
        """Create one isolated Skill venv and install only its frozen lock."""

        workspace = (workspace or Path.cwd()).expanduser().resolve()
        from runtime_identity.paths import PuddingClawPaths

        uv_cache_path = uv_cache_path.expanduser()
        if uv_cache_path.is_symlink():
            raise ValueError("managed uv cache must not be a symlink")
        uv_cache_path.mkdir(parents=True, mode=0o700, exist_ok=True)
        uv_cache_path = uv_cache_path.resolve(strict=True)
        uv_cache_path.relative_to(
            (PuddingClawPaths.from_environment().root / "runtime" / "python").resolve(strict=True)
        )
        args, image_id = self._managed_runtime_build_args(
            workspace=workspace,
            runtime_path=runtime_path,
            container_path=container_path,
            expected_runtime_image_digest=expected_runtime_image_digest,
            kind="python-skill-builder",
            network="bridge",
            extra_mounts=["--mount", f"type=bind,src={uv_cache_path},dst=/uv-cache"],
        )
        command = (
            f"python3 -m venv --copies {shlex.quote(container_path + '/.venv')} && "
            f"UV_CACHE_DIR=/uv-cache uv pip sync --require-hashes --python "
            f"{shlex.quote(container_path + '/.venv/bin/python')} "
            f"{shlex.quote(container_path + '/requirements.lock')}"
        )
        args.extend(["--entrypoint", "sh", image_id, "-c", command])
        try:
            result = self._run(args, timeout=timeout)
        except subprocess.TimeoutExpired:
            return ExecuteResponse(output="Python Skill runtime build timed out.", exit_code=124)
        output, truncated = _bounded_output(result.stdout, result.stderr, max_output_bytes=100_000)
        return ExecuteResponse(output=output, exit_code=result.returncode, truncated=truncated)

    def run_python_skill(
        self,
        workspace: Path,
        *,
        skill_id: str,
        skill_root: Path,
        runtime_path: Path | None,
        script_relative: str,
        interpreter_args: list[str],
        script_args: list[str],
        timeout: int,
        max_output_bytes: int,
        network_enabled: bool,
        expected_runtime_image_digest: str,
        user_query: str = "",
    ) -> ExecuteResponse:
        """Run one exact Skill script with only its validated Python env."""

        from runtime_identity.paths import PuddingClawPaths, safe_identity_component

        skill_id = safe_identity_component(skill_id, field="skill_id")
        workspace = workspace.expanduser().resolve(strict=True)
        skill_root = skill_root.expanduser().resolve(strict=True)
        if runtime_path is not None:
            runtime_path = runtime_path.expanduser().resolve(strict=True)
            runtime_path.relative_to(PuddingClawPaths.from_environment().root / "runtime" / "python")
        script = (skill_root / script_relative).resolve(strict=True)
        script.relative_to(skill_root)
        if script.is_symlink() or not script.is_file() or script.suffix != ".py":
            raise ValueError("managed Skill script is invalid")
        spec = self._spec(workspace)
        image_id = self.ensure_image(spec["image"])
        if image_id != expected_runtime_image_digest:
            raise ValueError("Python Skill runtime image changed before execution")
        runtime_mount = (
            [
                "--mount",
                (f"type=bind,src={runtime_path},dst=/opt/puddingclaw/runtime/python-skill,readonly"),
            ]
            if runtime_path is not None
            else []
        )
        args = [
            "run",
            "--rm",
            "--label",
            "com.puddingclaw.managed=true",
            "--label",
            "com.puddingclaw.kind=python-skill-runner",
            "--label",
            f"com.puddingclaw.owner={self._owner_label()}",
            "--network",
            "bridge" if network_enabled else "none",
            "--read-only",
            "--tmpfs",
            "/tmp:rw,nosuid,nodev,size=256m",
            "--tmpfs",
            "/home/puddingclaw:rw,nosuid,nodev,size=128m",
            "--mount",
            f"type=bind,src={workspace},dst=/workspace",
            "--mount",
            f"type=bind,src={skill_root},dst=/skills/{skill_id},readonly",
            *runtime_mount,
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
            "--workdir",
            "/workspace",
            "--env",
            "HOME=/home/puddingclaw",
            "--env",
            f"SKILL_NAME={skill_id}",
            "--env",
            f"SKILL_USER_QUERY={user_query}",
            *_managed_credential_state_tmpfs_args(),
            "--entrypoint",
            ("/opt/puddingclaw/runtime/python-skill/.venv/bin/python" if runtime_path is not None else "python3"),
        ]
        if spec["uid"] is not None and spec["gid"] is not None:
            args.extend(["--user", f"{spec['uid']}:{spec['gid']}"])
        args.extend(
            [
                image_id,
                *interpreter_args,
                f"/skills/{skill_id}/{script_relative}",
                *script_args,
            ]
        )
        try:
            result = self._run(args, timeout=timeout)
        except subprocess.TimeoutExpired:
            return ExecuteResponse(output="Python Skill execution timed out.", exit_code=124)
        output, truncated = _bounded_output(result.stdout, result.stderr, max_output_bytes=max_output_bytes)
        if result.returncode != 0:
            output = f"{output.rstrip()}\n\nExit code: {result.returncode}"
        return ExecuteResponse(output=output, exit_code=result.returncode, truncated=truncated)

    def resolve_managed_node_cli(
        self,
        workspace: Path | None = None,
        *,
        distribution: str,
        package: str,
        timeout: int = 60,
    ) -> ManagedNodePackageResolution:
        """Resolve a selector to immutable registry identity before approval."""

        workspace = (workspace or Path.cwd()).expanduser().resolve()
        spec = self._spec(workspace)
        image_id = self.ensure_image(spec["image"])
        args = [
            "run",
            "--rm",
            "--label",
            "com.puddingclaw.managed=true",
            "--label",
            "com.puddingclaw.kind=installer-resolver",
            "--label",
            f"com.puddingclaw.owner={self._owner_label()}",
            "--network",
            "bridge",
            "--read-only",
            "--tmpfs",
            "/home/puddingclaw:rw,nosuid,nodev,size=128m",
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
            "--entrypoint",
            "npm",
        ]
        if spec["uid"] is not None and spec["gid"] is not None:
            args.extend(["--user", f"{spec['uid']}:{spec['gid']}"])
        args.extend(
            [
                image_id,
                "view",
                distribution,
                "name",
                "version",
                "dist.integrity",
                "bin",
                "--json",
            ]
        )
        try:
            result = self._run(args, timeout=timeout)
        except subprocess.TimeoutExpired as exc:
            raise ValueError("managed package resolution timed out") from exc
        if result.returncode != 0:
            raise ValueError("managed package selector could not be resolved")
        try:
            value = json.loads(result.stdout)
        except (TypeError, ValueError) as exc:
            raise ValueError("managed package registry returned invalid metadata") from exc
        if not isinstance(value, dict):
            raise ValueError("managed package selector must resolve to exactly one version")
        resolved_name = str(value.get("name") or "")
        resolved_version = str(value.get("version") or "")
        dist = value.get("dist")
        resolved_integrity = str(
            value.get("dist.integrity") or (dist.get("integrity") if isinstance(dist, dict) else "") or ""
        )
        raw_bin = value.get("bin")
        if isinstance(raw_bin, str):
            executables = (resolved_name.rsplit("/", 1)[-1],)
        elif isinstance(raw_bin, dict):
            executables = tuple(sorted(str(name) for name in raw_bin))
        else:
            executables = ()
        if (
            resolved_name != package
            or not re.fullmatch(r"\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?", resolved_version)
            or not re.fullmatch(r"sha512-[A-Za-z0-9+/]+={0,2}", resolved_integrity)
            or any(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", name) is None for name in executables)
        ):
            raise ValueError("managed package registry identity is incompatible")
        return ManagedNodePackageResolution(
            package=resolved_name,
            version=resolved_version,
            integrity=resolved_integrity,
            distribution=f"{resolved_name}@{resolved_version}",
            runtime_image_digest=image_id,
            executables=executables,
        )

    def install_managed_node_cli(
        self,
        workspace: Path | None = None,
        *,
        distribution: str,
        package: str,
        expected_runtime_image_digest: str,
        executable: str,
        verification_argv: tuple[str, ...] = ("--version",),
        toolchain_path: Path,
        container_path: str,
        timeout: int = 600,
        max_output_bytes: int = 100_000,
    ) -> ExecuteResponse:
        """Reject the removed single-package incremental installer."""

        raise RuntimeError(
            "single-package managed CLI installation is disabled; use the declarative shared Node runtime transaction"
        )

    @staticmethod
    def _managed_browser_job_id(owner_user_id: str, provider: str, profile_id: str) -> str:
        return hashlib.sha256(f"{owner_user_id}\0{provider}\0{profile_id}".encode()).hexdigest()[:24]

    @classmethod
    def _managed_browser_container_name(
        cls,
        owner_user_id: str,
        provider: str,
        profile_id: str,
    ) -> str:
        return f"puddingclaw-browser-{cls._managed_browser_job_id(owner_user_id, provider, profile_id)}"

    def collect_managed_browser_auth_cli(
        self,
        *,
        owner_user_id: str,
        provider: str,
        profile_id: str,
        credential_state_spec: CredentialStateSpec,
        adapter_id: str,
        authorization_contract_fingerprint: str,
        max_output_bytes: int = 100_000,
    ) -> ManagedProviderExecutionResult:
        """Poll and prepare one browser-auth result without destroying it.

        The caller persists and verifies ``credential_state`` before ACKing
        with :meth:`finalize_managed_browser_auth_cli`. This two-phase order
        keeps a Vault failure or Backend crash recoverable.
        """

        state_paths = _credential_state_paths(credential_state_spec.paths)
        credential_state_fingerprint = credential_state_spec.fingerprint
        job_id = self._managed_browser_job_id(owner_user_id, provider, profile_id)
        name = self._managed_browser_container_name(owner_user_id, provider, profile_id)
        inspection = self._run(
            [
                "inspect",
                "--format",
                (
                    '{{.State.Running}} {{ index .Config.Labels "com.puddingclaw.credential-state" }} '
                    '{{ index .Config.Labels "com.puddingclaw.adapter" }} '
                    '{{ index .Config.Labels "com.puddingclaw.authorization-contract" }}'
                ),
                name,
            ],
            timeout=10,
        )
        inspection_parts = inspection.stdout.strip().split()
        if (
            inspection.returncode != 0
            or inspection_parts[:1] != ["true"]
            or inspection_parts[1:] != [credential_state_fingerprint, adapter_id, authorization_contract_fingerprint]
        ):
            return ManagedProviderExecutionResult(
                output="Managed browser authorization job is missing or expired.",
                exit_code=1,
                credential_state=None,
                browser_status="missing",
                browser_job_id=job_id,
            )

        output_result = self._run(["exec", name, "cat", "/tmp/puddingclaw-browser-output"], timeout=10)
        raw_output = output_result.stdout if output_result.returncode == 0 else ""
        output, truncated = _bounded_output(raw_output, "", max_output_bytes=max_output_bytes)
        exit_result = self._run(["exec", name, "cat", "/tmp/puddingclaw-browser-exit"], timeout=10)
        if exit_result.returncode != 0:
            awaiting_output = (
                "Managed browser authorization started.\n"
                "Status: awaiting_user_browser\n"
                "Authorization completed: false\n"
                f"Job: {job_id}\n\n{output}"
            )
            return ManagedProviderExecutionResult(
                output=awaiting_output,
                exit_code=0,
                credential_state=None,
                truncated=truncated,
                browser_status="awaiting_user_browser",
                browser_job_id=job_id,
            )

        try:
            exit_code = int(exit_result.stdout.strip())
        except ValueError:
            exit_code = 1
        exported = self._run_bytes(
            _credential_state_export_argv(name, state_paths),
            timeout=30,
        )
        credential_state = exported.stdout if exported.returncode == 0 else None
        if exported.returncode != 0:
            export_error = exported.stderr.decode("utf-8", errors="replace")
            output = f"{output.rstrip()}\n\nCredential export failed: {export_error}"
            exit_code = exit_code or 1
        if exit_code != 0:
            output = f"{output.rstrip()}\n\nExit code: {exit_code}"
        return ManagedProviderExecutionResult(
            output=output,
            exit_code=exit_code,
            credential_state=credential_state,
            truncated=truncated,
            browser_status="completed" if exit_code == 0 else "failed",
            browser_job_id=job_id,
        )

    def finalize_managed_browser_auth_cli(
        self,
        *,
        owner_user_id: str,
        provider: str,
        profile_id: str,
        browser_job_id: str,
    ) -> bool:
        """ACK and remove a browser-auth job after durable Vault commit."""

        expected = self._managed_browser_job_id(owner_user_id, provider, profile_id)
        if browser_job_id != expected:
            raise ValueError("browser authorization job identity mismatch")
        name = self._managed_browser_container_name(owner_user_id, provider, profile_id)
        inspection = self._run(
            [
                "inspect",
                "--format",
                '{{ index .Config.Labels "com.puddingclaw.browser-job" }}',
                name,
            ],
            timeout=10,
        )
        if inspection.returncode != 0:
            return False
        if inspection.stdout.strip() != expected:
            raise ValueError("browser authorization container label mismatch")
        acknowledged = self._run(
            ["exec", name, "touch", "/tmp/puddingclaw-browser-collected"],
            timeout=10,
        )
        if acknowledged.returncode != 0:
            return False
        return self._run(["rm", "-f", name], timeout=30).returncode == 0

    def list_managed_browser_auth_jobs(self, *, owner_user_id: str) -> list[dict[str, str]]:
        """Discover valid live browser jobs for Backend restart recovery."""

        listed = self._run(
            [
                "ps",
                "-q",
                "--filter",
                "label=com.puddingclaw.managed=true",
                "--filter",
                "label=com.puddingclaw.kind=browser-auth",
                "--filter",
                f"label=com.puddingclaw.owner={self._owner_label()}",
                "--filter",
                f"label=com.puddingclaw.owner-user={owner_user_id}",
            ],
            timeout=30,
        )
        if listed.returncode != 0:
            return []
        jobs: list[dict[str, str]] = []
        for container_id in (item.strip() for item in listed.stdout.splitlines()):
            if not container_id:
                continue
            inspected = self._run(
                ["inspect", "--format", "{{json .Config.Labels}}", container_id],
                timeout=10,
            )
            if inspected.returncode != 0:
                continue
            try:
                labels = json.loads(inspected.stdout)
            except (json.JSONDecodeError, TypeError):
                continue
            if not isinstance(labels, dict) or labels.get("com.puddingclaw.owner-user") != owner_user_id:
                continue
            provider = str(labels.get("com.puddingclaw.provider") or "")
            profile_id = str(labels.get("com.puddingclaw.profile") or "")
            job_id = str(labels.get("com.puddingclaw.browser-job") or "")
            adapter_id = str(labels.get("com.puddingclaw.adapter") or "")
            authorization_contract_fingerprint = str(labels.get("com.puddingclaw.authorization-contract") or "")
            credential_state_fingerprint = str(labels.get("com.puddingclaw.credential-state") or "")
            if (
                not provider
                or not profile_id
                or not re.fullmatch(r"[a-z0-9][a-z0-9_.-]*", adapter_id)
                or not re.fullmatch(r"[0-9a-f]{64}", authorization_contract_fingerprint)
                or not re.fullmatch(r"[0-9a-f]{64}", credential_state_fingerprint)
                or job_id != self._managed_browser_job_id(owner_user_id, provider, profile_id)
            ):
                continue
            jobs.append(
                {
                    "provider": provider,
                    "profile_id": profile_id,
                    "browser_job_id": job_id,
                    "adapter_id": adapter_id,
                    "authorization_contract_fingerprint": authorization_contract_fingerprint,
                    "credential_state_fingerprint": credential_state_fingerprint,
                }
            )
        return jobs

    def run_managed_browser_auth_cli(
        self,
        workspace: Path,
        *,
        argv: list[str],
        environment: dict[str, str],
        credential_state_spec: CredentialStateSpec,
        toolchain_path: Path,
        container_path: str,
        credential_state: bytes,
        owner_user_id: str,
        provider: str,
        profile_id: str,
        adapter_id: str,
        authorization_contract_fingerprint: str,
        expected_runtime_image_digest: str,
        wait_for_url_seconds: float = 30.0,
        max_output_bytes: int = 100_000,
    ) -> ManagedProviderExecutionResult:
        """Launch one Adapter-owned blocking browser process.

        Provider output stays opaque at this layer. The trusted Authorization
        Driver validates public URLs and phase semantics after collection.
        """

        if not re.fullmatch(r"[a-z0-9][a-z0-9_.-]*", adapter_id):
            raise ValueError("managed browser Adapter id is invalid")
        if not re.fullmatch(r"[0-9a-f]{64}", authorization_contract_fingerprint):
            raise ValueError("managed browser authorization contract is invalid")
        workspace = workspace.expanduser().resolve()
        toolchain_path = toolchain_path.expanduser().resolve(strict=True)
        if not argv:
            raise ValueError("browser runner argv is not owned by the mounted Toolchain revision")
        try:
            allowed_executable = _shared_runtime_executable(
                toolchain_path,
                argv[0],
                expected_runtime_image_digest,
            )
        except ValueError as exc:
            raise ValueError("browser runner argv is not owned by the mounted Toolchain revision") from exc
        spec = self._spec(workspace)
        image_id = self.ensure_image(spec["image"])
        if image_id != expected_runtime_image_digest:
            raise ValueError("managed browser runtime image changed after approval")
        job_id = self._managed_browser_job_id(owner_user_id, provider, profile_id)
        name = self._managed_browser_container_name(owner_user_id, provider, profile_id)
        state_paths = _credential_state_paths(credential_state_spec.paths)
        credential_state_environment = dict(credential_state_spec.env)
        credential_state_fingerprint = credential_state_spec.fingerprint
        if credential_state:
            credential_state = validate_credential_archive(
                credential_state,
                allowed_roots=state_paths,
            )
        if set(environment) & set(credential_state_environment):
            raise ValueError("command environment cannot override Adapter credential-state environment")
        if not re.fullmatch(r"[0-9a-f]{64}", credential_state_fingerprint):
            raise ValueError("Adapter credential-state fingerprint is invalid")

        existing = self.collect_managed_browser_auth_cli(
            owner_user_id=owner_user_id,
            provider=provider,
            profile_id=profile_id,
            credential_state_spec=credential_state_spec,
            adapter_id=adapter_id,
            authorization_contract_fingerprint=authorization_contract_fingerprint,
            max_output_bytes=max_output_bytes,
        )
        if existing.browser_status != "missing":
            return existing

        home_tmpfs = "/home/puddingclaw:rw,nosuid,nodev,size=128m"
        if spec["uid"] is not None and spec["gid"] is not None:
            home_tmpfs += f",uid={spec['uid']},gid={spec['gid']}"
        exact_command = shlex.join([f"{container_path}/bin/{allowed_executable}", *argv[1:]])
        supervisor = (
            f"set +e; {_credential_state_mkdir_command(state_paths)}; "
            "ready_wait=0; while [ ! -f /tmp/puddingclaw-browser-ready ]; do "
            'ready_wait=$((ready_wait + 1)); [ "$ready_wait" -ge 600 ] && exit 124; '
            "sleep 0.1; done; "
            f"timeout --signal=TERM --kill-after=10s 1500s {exact_command} "
            ">/tmp/puddingclaw-browser-output 2>&1; code=$?; "
            "printf '%s' \"$code\" >/tmp/puddingclaw-browser-exit; "
            'retain=0; while [ "$retain" -lt 86400 ]; do '
            "[ -f /tmp/puddingclaw-browser-collected ] && exit 0; "
            "retain=$((retain + 1)); sleep 1; done; exit 0"
        )
        create = [
            "create",
            "--rm",
            "--name",
            name,
            "--label",
            "com.puddingclaw.managed=true",
            "--label",
            "com.puddingclaw.kind=browser-auth",
            "--label",
            f"com.puddingclaw.owner={self._owner_label()}",
            "--label",
            f"com.puddingclaw.owner-user={owner_user_id}",
            "--label",
            f"com.puddingclaw.browser-job={job_id}",
            "--label",
            f"com.puddingclaw.provider={provider}",
            "--label",
            f"com.puddingclaw.profile={profile_id}",
            "--label",
            f"com.puddingclaw.credential-state={credential_state_fingerprint}",
            "--label",
            f"com.puddingclaw.adapter={adapter_id}",
            "--label",
            f"com.puddingclaw.authorization-contract={authorization_contract_fingerprint}",
            "--network",
            "bridge",
            "--read-only",
            "--tmpfs",
            "/tmp:rw,nosuid,nodev,size=32m",
            "--tmpfs",
            home_tmpfs,
            "--mount",
            f"type=bind,src={workspace},dst=/workspace,readonly",
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
        for key, value in sorted(credential_state_environment.items()):
            create.extend(["--env", f"{key}={value}"])
        for key, value in sorted(environment.items()):
            create.extend(["--env", f"{key}={value}"])
        if spec["uid"] is not None and spec["gid"] is not None:
            create.extend(["--user", f"{spec['uid']}:{spec['gid']}"])
        create.extend([image_id, "sh", "-c", supervisor])
        created = self._run(create, timeout=60)
        if created.returncode != 0:
            output, truncated = _bounded_output(
                created.stdout,
                created.stderr,
                max_output_bytes=max_output_bytes,
            )
            return ManagedProviderExecutionResult(
                output,
                created.returncode,
                None,
                truncated,
                "failed",
                job_id,
            )
        try:
            started = self._run(["start", name], timeout=30)
            if started.returncode != 0:
                output, truncated = _bounded_output(
                    started.stdout,
                    started.stderr,
                    max_output_bytes=max_output_bytes,
                )
                return ManagedProviderExecutionResult(
                    output,
                    started.returncode,
                    None,
                    truncated,
                    "failed",
                    job_id,
                )
            if credential_state:
                imported = self._run_bytes(
                    [
                        "exec",
                        "-i",
                        name,
                        "tar",
                        "-xzf",
                        "-",
                        "-C",
                        "/home/puddingclaw",
                        "--no-same-owner",
                        "--no-same-permissions",
                    ],
                    input_bytes=credential_state,
                    timeout=30,
                )
                if imported.returncode != 0:
                    raise RuntimeError(imported.stderr.decode("utf-8", errors="replace"))
            ready = self._run(["exec", name, "touch", "/tmp/puddingclaw-browser-ready"], timeout=10)
            if ready.returncode != 0:
                raise RuntimeError(ready.stderr)
            deadline = time.monotonic() + max(1.0, wait_for_url_seconds)
            output_seen_at: float | None = None
            while True:
                result = self.collect_managed_browser_auth_cli(
                    owner_user_id=owner_user_id,
                    provider=provider,
                    profile_id=profile_id,
                    credential_state_spec=credential_state_spec,
                    adapter_id=adapter_id,
                    authorization_contract_fingerprint=authorization_contract_fingerprint,
                    max_output_bytes=max_output_bytes,
                )
                if result.browser_status != "awaiting_user_browser":
                    return result
                if result.output.strip():
                    output_seen_at = output_seen_at or time.monotonic()
                    # Give line-buffered CLIs a short provider-neutral window
                    # to finish emitting their initial public material.
                    if time.monotonic() - output_seen_at >= 0.5:
                        return result
                if time.monotonic() >= deadline:
                    self._run(["rm", "-f", name], timeout=30)
                    return ManagedProviderExecutionResult(
                        output="Browser authorization did not emit public output in time.",
                        exit_code=1,
                        credential_state=None,
                        browser_status="failed",
                        browser_job_id=job_id,
                    )
                time.sleep(0.25)
        except Exception:
            self._run(["rm", "-f", name], timeout=30)
            raise

    def run_managed_provider_cli(
        self,
        workspace: Path,
        *,
        argv: list[str],
        environment: dict[str, str],
        credential_state_spec: CredentialStateSpec | None,
        toolchain_path: Path,
        container_path: str,
        credential_state: bytes,
        network_enabled: bool,
        workspace_writable: bool,
        expected_runtime_image_digest: str,
        continuation_secret: bytes | None = None,
        continuation_argument: str | None = None,
        continuation_trailing_argv: tuple[str, ...] = (),
        timeout: int = 120,
        max_output_bytes: int = 100_000,
    ) -> ManagedProviderExecutionResult:
        """Run exact Adapter-owned argv with credentials only in container tmpfs."""

        workspace = workspace.expanduser().resolve()
        toolchain_path = toolchain_path.expanduser().resolve(strict=True)
        if not argv:
            raise ValueError("provider runner argv is not owned by the mounted Toolchain revision")
        try:
            _shared_runtime_executable(
                toolchain_path,
                argv[0],
                expected_runtime_image_digest,
            )
        except ValueError as exc:
            raise ValueError("provider runner argv is not owned by the mounted Toolchain revision") from exc
        spec = self._spec(workspace)
        image_id = self.ensure_image(spec["image"])
        if image_id != expected_runtime_image_digest:
            raise ValueError("managed provider runtime image changed after approval")
        name = f"puddingclaw-provider-{uuid.uuid4().hex[:20]}"
        state_paths = _credential_state_paths(
            credential_state_spec.paths if credential_state_spec is not None else (),
            allow_empty=True,
        )
        credential_state_environment = dict(credential_state_spec.env) if credential_state_spec is not None else {}
        if not state_paths and (credential_state_environment or credential_state):
            raise ValueError("credentialless managed command cannot receive provider state")
        if credential_state:
            credential_state = validate_credential_archive(
                credential_state,
                allowed_roots=state_paths,
            )
        if set(environment) & set(credential_state_environment):
            raise ValueError("command environment cannot override Adapter credential-state environment")
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
        for key, value in sorted(credential_state_environment.items()):
            create.extend(["--env", f"{key}={value}"])
        if spec["uid"] is not None and spec["gid"] is not None:
            create.extend(["--user", f"{spec['uid']}:{spec['gid']}"])
        state_setup = _credential_state_mkdir_command(state_paths) if state_paths else "true"
        create.extend(
            [
                image_id,
                "sh",
                "-c",
                (f"{state_setup} && timeout {max(timeout + 60, 300)}s sleep infinity"),
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
                    [
                        "exec",
                        "-i",
                        name,
                        "tar",
                        "-xzf",
                        "-",
                        "-C",
                        "/home/puddingclaw",
                        "--no-same-owner",
                        "--no-same-permissions",
                    ],
                    input_bytes=credential_state,
                    timeout=30,
                )
                if imported.returncode != 0:
                    raise RuntimeError(imported.stderr.decode("utf-8", errors="replace"))
            exec_args = ["exec", "--workdir", "/workspace"]
            for key, value in sorted(environment.items()):
                exec_args.extend(["--env", f"{key}={value}"])
            if continuation_secret is not None and not (
                continuation_argument and re.fullmatch(r"--[a-z0-9][a-z0-9-]*", continuation_argument)
            ):
                raise ValueError("continuation secret requires a trusted Driver argument contract")
            if continuation_secret is None and continuation_argument is not None:
                raise ValueError("continuation argument cannot be used without a continuation secret")
            if continuation_secret is None and continuation_trailing_argv:
                raise ValueError("continuation trailing argv cannot be used without a continuation secret")
            if any(not value or "\x00" in value for value in continuation_trailing_argv):
                raise ValueError("continuation trailing argv is invalid")
            provider_timeout = max(1, int(timeout))
            docker_exec_timeout = provider_timeout + 10
            try:
                if continuation_secret is None:
                    exec_args.extend(
                        [
                            name,
                            "timeout",
                            "--signal=TERM",
                            "--kill-after=5s",
                            f"{provider_timeout}s",
                            *argv,
                        ]
                    )
                    result = self._run(exec_args, timeout=docker_exec_timeout)
                else:
                    # The provider continuation secret must not appear in
                    # Docker's host argv, process inspection, traces, or logs.
                    # Only this Backend-owned positional wrapper may receive it,
                    # over stdin, inside the credential-isolated runner.
                    # docker exec does not attach stdin unless -i is explicit;
                    # without it `cat` returns an empty string and lark-cli
                    # silently starts/validates a new authorization instead of
                    # consuming the browser-approved device code.
                    exec_args.insert(1, "-i")
                    continuation_command = shlex.join(
                        [
                            "timeout",
                            "--signal=TERM",
                            "--kill-after=5s",
                            f"{provider_timeout}s",
                            *argv,
                            str(continuation_argument),
                        ]
                    )
                    trailing_command = shlex.join(list(continuation_trailing_argv))
                    exec_args.extend(
                        [
                            name,
                            "sh",
                            "-c",
                            (
                                f'secret="$(cat)"; exec {continuation_command} "$secret"'
                                + (f" {trailing_command}" if trailing_command else "")
                            ),
                        ]
                    )
                    result = self._run_bytes(
                        exec_args,
                        input_bytes=continuation_secret,
                        timeout=docker_exec_timeout,
                    )
            except subprocess.TimeoutExpired:
                # The command itself has a shorter in-container timeout, so by
                # the time Docker's host-side grace period expires its writer
                # has already been terminated. Keep the container running:
                # /home/puddingclaw is tmpfs and stop/start may erase the very
                # credential state that must be recovered.
                timeout_message = (
                    f"Error: Managed provider command timed out after {timeout} seconds; "
                    "credential state was collected for independent verification."
                )
                result = subprocess.CompletedProcess(
                    args=exec_args,
                    returncode=124,
                    stdout="",
                    stderr=timeout_message,
                )
            result_stdout = (
                result.stdout.decode("utf-8", errors="replace") if isinstance(result.stdout, bytes) else result.stdout
            )
            result_stderr = (
                result.stderr.decode("utf-8", errors="replace") if isinstance(result.stderr, bytes) else result.stderr
            )
            output, truncated = _bounded_output(
                result_stdout,
                result_stderr,
                max_output_bytes=max_output_bytes,
            )
            exported = None
            if state_paths:
                exported = self._run_bytes(
                    _credential_state_export_argv(name, state_paths),
                    timeout=30,
                )
                if exported.returncode != 0:
                    raise RuntimeError(exported.stderr.decode("utf-8", errors="replace"))
            if result.returncode != 0:
                output = f"{output.rstrip()}\n\nExit code: {result.returncode}"
            return ManagedProviderExecutionResult(
                output=output,
                exit_code=result.returncode,
                credential_state=exported.stdout if exported is not None else None,
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
            (f"type=bind,src={external_directory},dst=/external-workspace" + ("" if writable else ",readonly")),
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
            ("PATH=/home/puddingclaw/.local/bin:/home/puddingclaw/.npm-global/bin:/usr/local/bin:/usr/bin:/bin"),
            *_managed_credential_state_tmpfs_args(),
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

    def run_ephemeral_grant_profile_command(
        self,
        workspace: Path,
        *,
        command: str,
        profile: Any,
        timeout: int,
        max_output_bytes: int,
    ) -> ExecuteResponse:
        """Project one runner-neutral Grant Profile into a disposable container."""

        workspace = workspace.expanduser().resolve(strict=True)
        if profile.workspace_root != workspace or not profile.valid_at_spawn():
            return ExecuteResponse(
                output="Error: Docker Grant Profile is invalid at spawn.",
                exit_code=126,
            )
        spec = self._spec(workspace)
        image_id = self.ensure_image(spec["image"])
        runtime_home_volume = self._runtime_home_volume_name(
            workspace,
            image=image_id,
        )
        mount_args = [
            "--mount",
            f"type=bind,src={workspace},dst=/workspace",
            "--mount",
            f"type=bind,src={profile.scratch_root},dst=/scratch",
            "--mount",
            f"type=volume,src={runtime_home_volume},dst=/home/puddingclaw",
        ]
        external_roots = {
            root
            for root in (*profile.read_roots, *profile.write_roots)
            if root not in {profile.workspace_root, profile.scratch_root}
        }
        for root in sorted(external_roots, key=str):
            writable = root in profile.write_roots
            mount = f"type=bind,src={root},dst={root}"
            if not writable:
                mount += ",readonly"
            mount_args.extend(["--mount", mount])
        for mount in spec["readonly_mounts"]:
            mount_args.extend(
                [
                    "--mount",
                    f"type=bind,src={mount['source']},dst={mount['target']},readonly",
                ]
            )
        args = [
            "run",
            "--rm",
            "--network",
            "bridge" if profile.network_allowed else "none",
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
            str(min(int(spec["pids_limit"]), int(profile.max_processes))),
            "--memory",
            f"{spec['memory_limit_mb']}m",
            "--cpus",
            spec["cpu_limit"],
            "--env",
            "HOME=/home/puddingclaw",
            "--env",
            ("PATH=/home/puddingclaw/.local/bin:/home/puddingclaw/.npm-global/bin:/usr/local/bin:/usr/bin:/bin"),
            *_managed_credential_state_tmpfs_args(),
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
                output=f"Error: Command timed out after {timeout} seconds.",
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
            str(item.get("Destination") or ""): item for item in inspected.get("Mounts") or [] if isinstance(item, dict)
        }
        for expected in spec.get("writable_mounts") or []:
            target = str(expected.get("target") or "")
            actual = mounts.get(target)
            expected_source = _canonical_docker_mount_source(str(expected.get("source") or ""))
            actual_source = _canonical_docker_mount_source(str(actual.get("Source") or "")) if actual else ""
            if actual is None or actual_source != expected_source or actual.get("RW") is not True:
                raise RuntimeError(f"Docker sandbox writable mount contract mismatch for {target or '<empty>'}.")
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
        scratch_path: Path | None = None,
        timeout: int = 120,
        max_output_bytes: int = 100_000,
        require_execution_permit: bool = False,
    ) -> None:
        super().__init__(root_dir=root_dir, virtual_mode=True)
        self.workspace_path = root_dir.expanduser().resolve()
        self.manager = manager
        self._default_timeout = timeout
        self._max_output_bytes = max_output_bytes
        self._require_execution_permit = require_execution_permit
        self.dependency_plan = manager.dependency_plan(self.workspace_path)
        scratch_relative = str(manager.config.get("_scratch_relative") or "").strip("/")
        spec = manager._spec(self.workspace_path)
        scratch_mount = next(
            (item for item in spec.get("writable_mounts") or [] if item.get("target") == "/scratch"),
            None,
        )
        scratch_source = str((scratch_mount or {}).get("source") or "").strip()
        self.scratch_path = (
            scratch_path.expanduser().resolve()
            if scratch_path is not None
            else Path(scratch_source).expanduser().resolve()
            if scratch_source
            else None
        )
        self.scratch_container_path = (
            "/scratch"
            if any(item.get("target") == "/scratch" for item in spec.get("writable_mounts") or [])
            else f"/harness-scratch/{scratch_relative}"
            if scratch_relative
            else "/harness-scratch"
        )
        self.managed_readonly_path_aliases = _managed_readonly_path_aliases(self.manager.config)
        self.managed_readonly_host_roots = tuple(
            host_root for _virtual_root, host_root in self.managed_readonly_path_aliases
        )
        self.container_name, self.spec_hash = manager.ensure_container(self.workspace_path)

    @property
    def id(self) -> str:
        return f"docker:{self.container_name}:{self.spec_hash[:12]}"

    @property
    def filesystem_read_roots(self) -> tuple[Path, ...]:
        return tuple(
            dict.fromkeys(
                (
                    self.workspace_path,
                    *((self.scratch_path,) if self.scratch_path is not None else ()),
                    *self.managed_readonly_host_roots,
                )
            )
        )

    @property
    def filesystem_write_roots(self) -> tuple[Path, ...]:
        return tuple(root for root in (self.workspace_path, self.scratch_path) if root is not None)

    filesystem_delete_roots = filesystem_write_roots

    def resolve_execution_path(self, raw_path: str) -> str:
        return _resolve_execution_path_alias(
            raw_path,
            workspace_path=self.workspace_path,
            scratch_path=self.scratch_path,
            managed_readonly_path_aliases=self.managed_readonly_path_aliases,
        )

    def _python_skill_invocation(
        self,
        command: str,
    ) -> tuple[str, Path, str, list[str], list[str]] | None:
        """Parse the narrow direct Python Skill execution surface."""

        if "/skills/" not in command or re.search(r"(?:^|\s)(?:python|python3)\b", command) is None:
            return None
        try:
            argv = shlex.split(command, posix=True)
        except ValueError as exc:
            raise ValueError("Python Skill command has invalid quoting") from exc
        if not argv or Path(argv[0]).name not in {"python", "python3"}:
            raise ValueError("Python Skill dependencies require a direct managed Python invocation")
        if any(re.search(r"[|&;<>]", token) for token in argv):
            raise ValueError("Python Skill dependencies do not allow a compound shell command")
        interpreter_args: list[str] = []
        script_index = 1
        allowed_flags = {"-B", "-E", "-I", "-s", "-S", "-u"}
        while script_index < len(argv) and argv[script_index].startswith("-"):
            flag = argv[script_index]
            if flag not in allowed_flags:
                raise ValueError(f"unsupported managed Python interpreter flag: {flag}")
            interpreter_args.append(flag)
            script_index += 1
        if script_index >= len(argv):
            raise ValueError("Python Skill command has no script")
        script_virtual = PurePosixPath(argv[script_index])
        if (
            not script_virtual.is_absolute()
            or len(script_virtual.parts) < 4
            or script_virtual.parts[1] != "skills"
            or script_virtual.suffix != ".py"
            or ".." in script_virtual.parts
        ):
            raise ValueError("Python Skill script must be an exact /skills/<id>/*.py path")
        skill_id = script_virtual.parts[2]
        source = next(
            (
                Path(str(item.get("source") or ""))
                for item in self.manager.config.get("_managed_readonly_mounts") or []
                if isinstance(item, dict) and item.get("target") == "/skills"
            ),
            None,
        )
        if source is None:
            raise ValueError("managed Skill source is unavailable")
        source = source.expanduser().resolve(strict=True)
        skill_root = source / skill_id
        if skill_root.is_symlink() or not skill_root.is_dir():
            raise ValueError("managed Skill is unavailable")
        relative = PurePosixPath(*script_virtual.parts[3:]).as_posix()
        host_script = (skill_root / relative).resolve(strict=True)
        host_script.relative_to(skill_root.resolve())
        if host_script.is_symlink() or not host_script.is_file():
            raise ValueError("managed Skill script is unavailable")
        return skill_id, skill_root, relative, interpreter_args, argv[script_index + 1 :]

    def execute(
        self,
        command: str,
        *,
        timeout: int | None = None,
        _trusted_typed: bool = False,
    ) -> ExecuteResponse:
        original_command = command
        traversal = _reject_scratch_traversal(command)
        if traversal is not None:
            return traversal
        effective_timeout = timeout if timeout is not None else self._default_timeout
        if not isinstance(effective_timeout, int) or effective_timeout <= 0:
            raise ValueError("timeout must be a positive integer")
        from harness.execution_context import current_authorized_execution

        authorized = current_authorized_execution()
        if authorized is not None and authorized.permit.selected_runner == "docker":
            command = authorized.execution_command
        command = re.sub(
            r"(?<![A-Za-z0-9_./-])/scratch(?=(?:/|\s|$|[\"']))",
            self.scratch_container_path,
            command,
        )
        if authorized is not None and authorized.permit.selected_runner == "docker":
            profile = authorized.profile
            has_external_roots = any(
                root not in {profile.workspace_root, profile.scratch_root}
                for root in (*profile.read_roots, *profile.write_roots)
            )
            if has_external_roots:
                if not authorized.valid_at_spawn(
                    command=original_command,
                    selected_runner="docker",
                ):
                    return ExecuteResponse(
                        output="Error: Docker execution permit became invalid before process spawn.",
                        exit_code=126,
                    )
                return self.manager.run_ephemeral_grant_profile_command(
                    self.workspace_path,
                    command=command,
                    profile=profile,
                    timeout=effective_timeout,
                    max_output_bytes=self._max_output_bytes,
                )
        try:
            with self.manager._lock(self.container_name):
                # An idle timer may stop a project container between Runs.
                # Reconcile it under the same keyed lock used by lifecycle
                # cleanup, then serialize commands for this project.
                container_name, spec_hash = self.manager.ensure_container(self.workspace_path)
                if container_name != self.container_name or spec_hash != self.spec_hash:
                    raise RuntimeError("Docker sandbox specification changed after this Run started; start a new Run.")
                if self._require_execution_permit and not _trusted_typed:
                    from harness.execution_context import current_authorized_execution

                    authorized = current_authorized_execution()
                    if authorized is None or not authorized.valid_at_spawn(
                        command=original_command,
                        selected_runner="docker",
                    ):
                        return ExecuteResponse(
                            output="Error: Docker execution permit became invalid before process spawn.",
                            exit_code=126,
                        )
                from harness.tool_execution import ShellPolicyAnalyzer

                effects = ShellPolicyAnalyzer.capabilities(
                    command,
                    workspace_path=self.workspace_path,
                )
                skill_invocation = self._python_skill_invocation(command)
                if skill_invocation is not None:
                    from runtime_identity.paths import PuddingClawPaths
                    from runtime_identity.software_runtime import (
                        SoftwareRuntimeManager,
                        skill_content_version,
                    )

                    skill_id, skill_root, script_relative, interpreter_args, script_args = skill_invocation
                    image_id = self.manager.ensure_image(str(self.manager.config.get("image") or DEFAULT_SANDBOX_IMAGE))
                    runtime = SoftwareRuntimeManager(
                        PuddingClawPaths.from_environment(),
                        self.manager.runtime_contract,
                    ).python_skill_current(
                        skill_id,
                        skill_content_version(skill_root),
                        image_id,
                    )
                    result = self.manager.run_python_skill(
                        self.workspace_path,
                        skill_id=skill_id,
                        skill_root=skill_root,
                        runtime_path=runtime,
                        script_relative=script_relative,
                        interpreter_args=interpreter_args,
                        script_args=script_args,
                        timeout=effective_timeout,
                        max_output_bytes=self._max_output_bytes,
                        network_enabled=effects.network,
                        expected_runtime_image_digest=image_id,
                    )
                    self.manager.mark_activity(self.container_name)
                    return result
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

    def managed_runtime_image_digest(self) -> str:
        return self.manager.managed_runtime_image_digest(self.workspace_path)

    def resolve_shared_node_runtime(self, **kwargs: Any) -> ExecuteResponse:
        return self.manager.resolve_shared_node_runtime(self.workspace_path, **kwargs)

    def build_shared_node_runtime(self, **kwargs: Any) -> ExecuteResponse:
        return self.manager.build_shared_node_runtime(self.workspace_path, **kwargs)

    def resolve_python_skill_runtime(self, **kwargs: Any) -> ExecuteResponse:
        return self.manager.resolve_python_skill_runtime(self.workspace_path, **kwargs)

    def build_python_skill_runtime(self, **kwargs: Any) -> ExecuteResponse:
        return self.manager.build_python_skill_runtime(self.workspace_path, **kwargs)

    def resolve_managed_node_cli(
        self,
        *,
        distribution: str,
        package: str,
    ) -> ManagedNodePackageResolution:
        return self.manager.resolve_managed_node_cli(
            self.workspace_path,
            distribution=distribution,
            package=package,
        )

    def run_managed_provider_cli(
        self,
        *,
        argv: list[str],
        environment: dict[str, str],
        credential_state_spec: CredentialStateSpec | None,
        toolchain_path: Path,
        container_path: str,
        credential_state: bytes,
        network_enabled: bool,
        workspace_writable: bool,
        expected_runtime_image_digest: str,
        continuation_secret: bytes | None = None,
        continuation_argument: str | None = None,
        continuation_trailing_argv: tuple[str, ...] = (),
    ) -> ManagedProviderExecutionResult:
        return self.manager.run_managed_provider_cli(
            self.workspace_path,
            argv=argv,
            environment=environment,
            credential_state_spec=credential_state_spec,
            toolchain_path=toolchain_path,
            container_path=container_path,
            credential_state=credential_state,
            network_enabled=network_enabled,
            workspace_writable=workspace_writable,
            expected_runtime_image_digest=expected_runtime_image_digest,
            continuation_secret=continuation_secret,
            continuation_argument=continuation_argument,
            continuation_trailing_argv=continuation_trailing_argv,
            timeout=self._default_timeout,
            max_output_bytes=self._max_output_bytes,
        )

    def run_managed_browser_auth_cli(
        self,
        *,
        argv: list[str],
        environment: dict[str, str],
        credential_state_spec: CredentialStateSpec,
        toolchain_path: Path,
        container_path: str,
        credential_state: bytes,
        owner_user_id: str,
        provider: str,
        profile_id: str,
        adapter_id: str,
        authorization_contract_fingerprint: str,
        expected_runtime_image_digest: str,
    ) -> ManagedProviderExecutionResult:
        return self.manager.run_managed_browser_auth_cli(
            self.workspace_path,
            argv=argv,
            environment=environment,
            credential_state_spec=credential_state_spec,
            toolchain_path=toolchain_path,
            container_path=container_path,
            credential_state=credential_state,
            owner_user_id=owner_user_id,
            provider=provider,
            profile_id=profile_id,
            adapter_id=adapter_id,
            authorization_contract_fingerprint=authorization_contract_fingerprint,
            expected_runtime_image_digest=expected_runtime_image_digest,
            max_output_bytes=self._max_output_bytes,
        )

    def collect_managed_browser_auth_cli(
        self,
        *,
        owner_user_id: str,
        provider: str,
        profile_id: str,
        credential_state_spec: CredentialStateSpec,
        adapter_id: str,
        authorization_contract_fingerprint: str,
    ) -> ManagedProviderExecutionResult:
        return self.manager.collect_managed_browser_auth_cli(
            owner_user_id=owner_user_id,
            provider=provider,
            profile_id=profile_id,
            credential_state_spec=credential_state_spec,
            adapter_id=adapter_id,
            authorization_contract_fingerprint=authorization_contract_fingerprint,
            max_output_bytes=self._max_output_bytes,
        )

    def finalize_managed_browser_auth_cli(
        self,
        *,
        owner_user_id: str,
        provider: str,
        profile_id: str,
        browser_job_id: str,
    ) -> bool:
        return self.manager.finalize_managed_browser_auth_cli(
            owner_user_id=owner_user_id,
            provider=provider,
            profile_id=profile_id,
            browser_job_id=browser_job_id,
        )

    def list_managed_browser_auth_jobs(self, *, owner_user_id: str) -> list[dict[str, str]]:
        return self.manager.list_managed_browser_auth_jobs(owner_user_id=owner_user_id)


class AdaptiveWorkspaceBackend(FilesystemBackend, SandboxBackendProtocol):
    """Kernel-first backend with a Docker delegate constructed only on use."""

    mode = "adaptive"
    runtime_contract = RUNTIME_CONTRACT
    _MANAGED_DOCKER_METHODS = frozenset(
        {
            "managed_runtime_image_digest",
            "resolve_shared_node_runtime",
            "build_shared_node_runtime",
            "resolve_python_skill_runtime",
            "build_python_skill_runtime",
            "resolve_managed_node_cli",
            "run_managed_provider_cli",
            "run_managed_browser_auth_cli",
            "collect_managed_browser_auth_cli",
            "finalize_managed_browser_auth_cli",
            "list_managed_browser_auth_jobs",
        }
    )

    def __init__(
        self,
        *,
        root_dir: Path,
        scratch_path: Path,
        docker_config: dict[str, Any],
        timeout: int = 120,
        managed_readonly_path_aliases: tuple[tuple[str, Path], ...] = (),
    ) -> None:
        super().__init__(root_dir=root_dir, virtual_mode=True)
        self.workspace_path = root_dir.expanduser().resolve()
        self.scratch_path = scratch_path.expanduser().resolve()
        self.managed_readonly_path_aliases = tuple(managed_readonly_path_aliases)
        self.managed_readonly_host_roots = tuple(
            host_root for _virtual_root, host_root in self.managed_readonly_path_aliases
        )
        self.kernel = KernelWorkspaceBackend(
            root_dir=self.workspace_path,
            scratch_path=self.scratch_path,
            timeout=timeout,
            managed_readonly_path_aliases=self.managed_readonly_path_aliases,
        )
        self._docker_config = dict(docker_config)
        self._default_timeout = timeout
        self._docker: DockerWorkspaceBackend | None = None
        self._docker_lock = threading.RLock()
        self._host_runtime: Any | None = None
        self._host_runtime_lock = threading.RLock()
        workspace_digest = hashlib.sha256(str(self.workspace_path).encode("utf-8")).hexdigest()[:16]
        self._id = f"adaptive:kernel-first:{workspace_digest}"

    @property
    def id(self) -> str:
        return self._id

    @property
    def kernel_runner_mode(self) -> str:
        return self.kernel.kernel_runner_mode

    @property
    def kernel_runner_binding_digest(self) -> str:
        return self.kernel.kernel_runner_binding_digest

    @property
    def filesystem_read_roots(self) -> tuple[Path, ...]:
        return self.kernel.filesystem_read_roots

    @property
    def filesystem_write_roots(self) -> tuple[Path, ...]:
        return self.kernel.filesystem_write_roots

    @property
    def filesystem_delete_roots(self) -> tuple[Path, ...]:
        return self.kernel.filesystem_delete_roots

    def resolve_execution_path(self, raw_path: str) -> str:
        return self.kernel.resolve_execution_path(raw_path)

    def _docker_backend(self) -> DockerWorkspaceBackend:
        with self._docker_lock:
            if self._docker is not None:
                return self._docker
            manager = ProjectSandboxManager(self._docker_config)
            available, reason = manager.probe()
            if not available:
                raise RuntimeError(f"Docker sandbox is unavailable: {reason}")
            self._docker = DockerWorkspaceBackend(
                root_dir=self.workspace_path,
                manager=manager,
                scratch_path=self.scratch_path,
                timeout=self._default_timeout,
                require_execution_permit=True,
            )
            return self._docker

    def _host_runtime_backend(self) -> Any:
        from harness.host_skill_runtime import HostSkillRuntimeBackend
        from runtime_identity.paths import PuddingClawPaths

        with self._host_runtime_lock:
            if self._host_runtime is None:
                self._host_runtime = HostSkillRuntimeBackend(
                    PuddingClawPaths.from_environment(),
                    timeout=max(self._default_timeout, 900),
                )
            return self._host_runtime

    def execute(
        self,
        command: str,
        *,
        timeout: int | None = None,
    ) -> ExecuteResponse:
        from harness.execution_context import current_authorized_execution

        authorized = current_authorized_execution()
        if authorized is None:
            return ExecuteResponse(
                output="Error: Adaptive execution requires a Tool Gate execution permit.",
                exit_code=126,
            )
        if authorized.permit.selected_runner.startswith("kernel_"):
            return self.kernel.execute(command, timeout=timeout)
        if authorized.permit.selected_runner != "docker":
            return ExecuteResponse(
                output="Error: Execution permit selected an unknown runner.",
                exit_code=126,
            )
        if not authorized.valid_at_spawn(command=command, selected_runner="docker"):
            return ExecuteResponse(
                output="Error: Docker execution permit became invalid before lazy startup.",
                exit_code=126,
            )
        try:
            return self._docker_backend().execute(command, timeout=timeout)
        except Exception as exc:  # noqa: BLE001
            return ExecuteResponse(
                output=f"Error starting lazy Docker runner ({type(exc).__name__}): {exc}",
                exit_code=1,
            )

    def execute_external_directory(self, *args: Any, **kwargs: Any) -> ExecuteResponse:
        return self.kernel.execute_external_directory(*args, **kwargs)

    def run_html_report_e2e(self, *args: Any, **kwargs: Any) -> ExecuteResponse:
        return self.kernel.run_html_report_e2e(*args, **kwargs)

    def _skill_runtime(self, skill_id: str, skill_version: str) -> str:
        from runtime_identity.paths import PuddingClawPaths
        from runtime_identity.skill_runtimes import SkillRuntimeBindingStore

        return SkillRuntimeBindingStore(PuddingClawPaths.from_environment()).runtime_for(
            skill_id=skill_id,
            skill_version=skill_version,
        )

    def skill_runtime_for_command(self, command: str) -> str:
        from runtime_identity.software_runtime import skill_content_version

        skill_ids = set(
            re.findall(
                r"(?:^|[\s'\"])/skills/([A-Za-z0-9][A-Za-z0-9_.-]{0,127})(?:/|[\s'\"]|$)",
                command,
            )
        )
        if len(skill_ids) != 1:
            return "host"
        skill_id = next(iter(skill_ids))
        skills_root = next(
            (root for virtual, root in self.managed_readonly_path_aliases if virtual == "/skills"),
            None,
        )
        if skills_root is None:
            return "host"
        skill_root = (skills_root / skill_id).resolve(strict=True)
        skill_root.relative_to(skills_root.resolve(strict=True))
        return self._skill_runtime(skill_id, skill_content_version(skill_root))

    def prepare_docker_execution(self, command: str) -> Any:
        """Project only Linux-built runtime bytes for an explicitly bound Skill."""

        from harness.host_skill_runtime import HostExecutionProjection
        from runtime_identity.paths import PuddingClawPaths
        from runtime_identity.software_runtime import SoftwareRuntimeManager

        if self.skill_runtime_for_command(command) != "docker":
            return HostExecutionProjection(command)
        if re.search(r"(?:^|\s)(?:python|python3)\b", command):
            return HostExecutionProjection(command, environment_binding_digest="docker-python")
        docker = self._docker_backend()
        digest = docker.managed_runtime_image_digest()
        release = SoftwareRuntimeManager(
            PuddingClawPaths.from_environment(),
            docker.manager.runtime_contract,
        ).node_current(digest)
        if release.name == "empty":
            return HostExecutionProjection(command, environment_binding_digest="docker-node-empty")
        path = ":".join((str(release / "bin"), "/usr/local/bin", "/usr/bin", "/bin"))
        projected = (
            f"export PATH={shlex.quote(path)}; export NODE_PATH={shlex.quote(str(release / 'node_modules'))}; {command}"
        )
        return HostExecutionProjection(
            projected,
            (release,),
            environment_binding_digest=hashlib.sha256(f"docker-node\0{digest}\0{release.name}".encode()).hexdigest(),
        )

    def install_packages(
        self,
        skill_id: str,
        skill_version: str,
        ecosystem: str,
        packages: list[str],
        executables: dict[str, list[str]] | None = None,
    ) -> ExecuteResponse:
        if self._skill_runtime(skill_id, skill_version) != "docker":
            installer = self._host_runtime_backend().install_packages
            if executables:
                return installer(skill_id, skill_version, ecosystem, packages, executables)
            return installer(skill_id, skill_version, ecosystem, packages)
        from runtime_identity.paths import PuddingClawPaths
        from runtime_identity.software_runtime import SoftwareRuntimeManager

        docker = self._docker_backend()
        manager = SoftwareRuntimeManager(
            PuddingClawPaths.from_environment(),
            docker.manager.runtime_contract,
        )
        try:
            if ecosystem == "python":
                installed = manager.install_python_skill(
                    docker,
                    skill_id=skill_id,
                    skill_version=skill_version,
                    requirements=packages,
                )
            elif ecosystem == "node":
                installed = manager.install_node_owner(
                    docker,
                    owner=f"skill:{skill_id}",
                    owner_revision=skill_version,
                    distributions=packages,
                    declared_bins={package: tuple(bins) for package, bins in (executables or {}).items()},
                    merge_owner=True,
                )
            else:
                return ExecuteResponse(output=f"Unsupported package ecosystem: {ecosystem}", exit_code=64)
        except (OSError, ValueError) as exc:
            return ExecuteResponse(
                output=f"Docker Skill dependency transaction failed: {type(exc).__name__}: {exc}",
                exit_code=65,
            )
        return ExecuteResponse(
            output=json.dumps(
                {
                    "status": "installed" if installed.exit_code == 0 else "failed",
                    "skill_id": skill_id,
                    "skill_version": skill_version,
                    "ecosystem": ecosystem,
                    "runtime": "docker",
                    "revision": installed.revision,
                    "runtime_environment_digest": installed.runtime_image_digest,
                    "diagnostic": installed.output,
                },
                ensure_ascii=False,
                sort_keys=True,
            ),
            exit_code=installed.exit_code,
        )

    def prepare_host_skill_execution(
        self,
        command: str,
        *,
        active_skill_ids: tuple[str, ...] = (),
    ) -> Any:
        return self.kernel.prepare_host_skill_execution(
            command,
            active_skill_ids=active_skill_ids,
        )

    def prepare_host_execution(
        self,
        command: str,
        *,
        active_skill_ids: tuple[str, ...] = (),
    ) -> Any:
        return self.kernel.prepare_host_execution(
            command,
            active_skill_ids=active_skill_ids,
        )

    def resolve_generic_node_cli(self, **kwargs: Any) -> Any:
        return self._host_runtime_backend().resolve_generic_node_cli(**kwargs)

    def generic_node_runtime_current(self, runtime_digest: str) -> Path:
        return self._host_runtime_backend().generic_node_runtime_current(runtime_digest)

    def install_generic_node_cli(self, **kwargs: Any) -> Any:
        return self._host_runtime_backend().install_generic_node_cli(**kwargs)

    def __getattr__(self, name: str) -> Any:
        if name in self._MANAGED_DOCKER_METHODS:
            # Attribute discovery must remain side-effect free.  In
            # particular ManagedCliService probes this surface with hasattr()
            # during construction; eagerly resolving the Docker backend here
            # would make every Agent Run depend on Docker availability before
            # a managed command is actually executed.
            def lazy_managed_method(*args: Any, **kwargs: Any) -> Any:
                return getattr(self._docker_backend(), name)(*args, **kwargs)

            return lazy_managed_method
        raise AttributeError(name)


@dataclass(frozen=True)
class WorkspaceBackendSelection:
    backend: (
        SpawnWorkspaceBackend
        | KernelWorkspaceBackend
        | DeferredKernelWorkspaceBackend
        | AdaptiveWorkspaceBackend
        | DockerWorkspaceBackend
    )
    mode: str
    fallback_reason: str | None = None
    dependency_plan: WorkspaceDependencyPlan | None = None


def build_workspace_execution_backend(
    workspace_path: Path,
    terminal_config: dict[str, Any],
) -> WorkspaceBackendSelection:
    """Build the configured host-spawn or kernel execution backend."""

    timeout = int(terminal_config.get("default_timeout_seconds") or 120)
    execution_mode = str(terminal_config.get("execution_mode") or "").strip().lower()
    if execution_mode not in {"spawn", "kernel"}:
        raise ValueError("execution_mode must be spawn or kernel")
    docker_config = dict(terminal_config.get("docker") or {})
    managed_readonly_aliases = _managed_readonly_path_aliases(docker_config)
    scratch_host_path_raw = str(terminal_config.get("_scratch_host_path") or "").strip()
    scratch_host_path = Path(scratch_host_path_raw) if scratch_host_path_raw else None
    if execution_mode == "kernel":
        if scratch_host_path is None:
            raise ValueError("Kernel sandbox requires a Harness scratch path")
        try:
            return WorkspaceBackendSelection(
                backend=KernelWorkspaceBackend(
                    root_dir=workspace_path,
                    scratch_path=scratch_host_path,
                    timeout=timeout,
                    managed_readonly_path_aliases=managed_readonly_aliases,
                ),
                mode="kernel",
            )
        except RuntimeError as exc:
            logger.warning("Kernel execution mode unavailable; deferring explicit fallback decision: %s", exc)
            return WorkspaceBackendSelection(
                backend=DeferredKernelWorkspaceBackend(
                    root_dir=workspace_path,
                    scratch_path=scratch_host_path,
                    timeout=timeout,
                    managed_readonly_path_aliases=managed_readonly_aliases,
                    reason=str(exc),
                ),
                mode="kernel",
                fallback_reason=str(exc),
            )
    return WorkspaceBackendSelection(
        backend=SpawnWorkspaceBackend(
            root_dir=workspace_path,
            scratch_path=scratch_host_path,
            timeout=timeout,
            managed_readonly_path_aliases=managed_readonly_aliases,
        ),
        mode="spawn",
    )
