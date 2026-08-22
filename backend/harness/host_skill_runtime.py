"""Seatbelt-backed materialization for ordinary Skill dependencies.

This backend intentionally implements the small installer protocol consumed by
``SoftwareRuntimeManager``.  The manager remains responsible for locks,
integrity evidence and atomic publication; this module replaces Docker as the
ordinary Skill package execution surface.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import platform
import re
import shlex
import shutil
import signal
import subprocess
import sys
import sysconfig
import tarfile
import tempfile
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from deepagents.backends.protocol import ExecuteResponse

from harness.kernel_sandbox import MacOSSeatbeltRunner
from harness.sandbox_profiles import SandboxGrantProfile
from runtime_identity.host_lark_cli import HostLarkCliRuntime
from runtime_identity.paths import PuddingClawPaths

_DIGEST = re.compile(r"sha256:[0-9a-f]{64}")


@dataclass(frozen=True)
class HostNodePackageResolution:
    package: str
    version: str
    integrity: str
    distribution: str
    runtime_image_digest: str
    executables: tuple[str, ...] = ()


@dataclass(frozen=True)
class HostExecutionProjection:
    """Typed, non-shell execution context bound into a Tool Gate permit."""

    command: str
    read_roots: tuple[Path, ...] = ()
    environment: tuple[tuple[str, str], ...] = field(default=(), repr=False, compare=False)
    secret_values: tuple[str, ...] = field(default=(), repr=False, compare=False)
    environment_binding_digest: str = ""
    environment_current: Callable[[], bool] = field(
        default=lambda: True,
        repr=False,
        compare=False,
    )

    def __iter__(self):
        # Transitional compatibility for internal callers that only projected
        # command/read roots before environment injection existed.
        yield self.command
        yield self.read_roots


@dataclass
class _ManagedBrowserJob:
    job_id: str
    owner_user_id: str
    provider: str
    profile_id: str
    adapter_id: str
    authorization_contract_fingerprint: str
    credential_state_fingerprint: str
    operation: Path
    home: Path
    output_path: Path
    output_handle: Any
    process: subprocess.Popen[str]
    state_paths: tuple[str, ...]
    created_at: float


class HostSkillRuntimeBackend:
    """Build PuddingClaw-owned Node/Python environments on the local ABI."""

    contract_schema = "host-skill-runtime-v1"
    _browser_jobs: dict[str, _ManagedBrowserJob] = {}
    _browser_jobs_lock = threading.RLock()

    def __init__(
        self,
        paths: PuddingClawPaths,
        *,
        timeout: int = 900,
    ) -> None:
        self.paths = paths
        self.timeout = timeout
        self.lark_cli = HostLarkCliRuntime(paths)
        self._runtime_root = self.paths.root / "runtime"
        self._operations = self._runtime_root / "operations"
        self._node_cache = self._runtime_root / "node-cache"
        self._python_cache = self.paths.python_uv_cache()
        for root in (self._runtime_root, self._operations, self._node_cache, self._python_cache):
            if root.is_symlink():
                raise ValueError("managed host runtime root must not be a symlink")

        self.python = self._python_executable()
        library_dir = self.python.parent.parent / "lib"
        library_name = str(sysconfig.get_config_var("LDLIBRARY") or "")
        library_candidates = [library_dir / library_name] if library_name else []
        # Some macOS Python builds report their static archive as LDLIBRARY
        # even though the embeddable shared library is the usable artifact.
        # Prefer the reported name, then fall back to the platform's shared
        # library variants without accepting an arbitrary path.
        if library_name.endswith(".a"):
            library_candidates.extend(
                [
                    library_dir / (library_name.removesuffix(".a") + ".dylib"),
                    library_dir / (library_name.removesuffix(".a") + ".so"),
                ]
            )
        self.python_library = next(
            (candidate for candidate in library_candidates if candidate.is_file()),
            library_candidates[0] if library_candidates else library_dir / "",
        )
        if not self.python_library.is_file():
            raise ValueError("managed host Python shared library is unavailable")
        self.node = self._optional_tool("node")
        self.npm = self._optional_tool("npm")
        self.uv = self._optional_tool("uv")
        identity = self._identity()
        encoded = json.dumps(identity, sort_keys=True, separators=(",", ":")).encode()
        self._runtime_digest = "sha256:" + hashlib.sha256(encoded).hexdigest()
        python_identity = {
            key: identity[key]
            for key in (
                "schema",
                "system",
                "release",
                "machine",
                "python",
                "python_version",
                "python_abi",
                "uv",
                "uv_version",
            )
        }
        self._python_runtime_digest = "sha256:" + hashlib.sha256(
            json.dumps(python_identity, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        python_version = identity["python_version"].split(".")
        node_major = re.search(r"\d+", identity["node_version"])
        self.python_runtime_contract = (
            f"host-seatbelt-v1-python{python_version[0]}.{python_version[1]}"
        )
        self.runtime_contract = (
            f"{self.python_runtime_contract}-"
            f"node{node_major.group(0) if node_major else 'none'}"
        )

    def _ensure_roots(self) -> None:
        for root in (self._runtime_root, self._operations, self._node_cache, self._python_cache):
            if root.is_symlink():
                raise ValueError("managed host runtime root must not be a symlink")
            root.mkdir(parents=True, mode=0o700, exist_ok=True)

    @staticmethod
    def _python_executable() -> Path:
        candidate = Path(getattr(sys, "_base_executable", "") or sys.executable).expanduser()
        resolved = candidate.resolve(strict=True)
        if not resolved.is_file() or not os.access(resolved, os.X_OK):
            raise ValueError("managed host Python interpreter is unavailable")
        return resolved

    @staticmethod
    def _optional_tool(name: str) -> Path | None:
        value = shutil.which(name)
        if not value:
            return None
        candidate = Path(value).expanduser().resolve(strict=True)
        return candidate if candidate.is_file() and os.access(candidate, os.X_OK) else None

    @staticmethod
    def _version(executable: Path | None, *argv: str) -> str:
        if executable is None:
            return "unavailable"
        try:
            result = subprocess.run(  # noqa: S603
                [str(executable), *argv],
                check=False,
                capture_output=True,
                text=True,
                timeout=10,
                env={
                    "PATH": "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin",
                    "LANG": "C.UTF-8",
                },
            )
        except (OSError, subprocess.TimeoutExpired):
            return "unavailable"
        output = (result.stdout or result.stderr).strip().splitlines()
        return output[0] if result.returncode == 0 and output else "unavailable"

    def _identity(self) -> dict[str, str]:
        return {
            "schema": self.contract_schema,
            "system": platform.system().lower(),
            "release": platform.release(),
            "machine": platform.machine().lower(),
            "python": str(self.python),
            "python_version": platform.python_version(),
            "python_abi": str(sysconfig.get_config_var("SOABI") or "unknown"),
            "node": str(self.node or "unavailable"),
            "node_version": self._version(self.node, "--version"),
            "npm": str(self.npm or "unavailable"),
            "npm_version": self._version(self.npm, "--version"),
            "uv": str(self.uv or "unavailable"),
            "uv_version": self._version(self.uv, "--version"),
        }

    def managed_runtime_image_digest(self) -> str:
        """Compatibility name for the manager's immutable runtime identity."""

        return self._runtime_digest

    def managed_python_runtime_image_digest(self) -> str:
        """Return the immutable identity relevant to Python environments only."""

        return self._python_runtime_digest

    def _require_digest(self, expected: str) -> None:
        if _DIGEST.fullmatch(expected) is None or expected != self._runtime_digest:
            raise ValueError("host runtime contract changed while dependency installation was pending")

    def _require_python_digest(self, expected: str) -> None:
        if _DIGEST.fullmatch(expected) is None or expected != self._python_runtime_digest:
            raise ValueError("host Python runtime changed while dependency installation was pending")

    def _candidate(self, value: Path) -> Path:
        candidate = value.expanduser().resolve(strict=True)
        candidate.relative_to(self._runtime_root.resolve(strict=True))
        if candidate.is_symlink() or not candidate.is_dir():
            raise ValueError("managed host runtime candidate is invalid")
        return candidate

    def _run_installer(
        self,
        candidate: Path,
        command: str,
        *,
        extra_read_roots: tuple[Path, ...] = (),
        timeout: int | None = None,
    ) -> ExecuteResponse:
        self._ensure_roots()
        candidate = self._candidate(candidate)
        with tempfile.TemporaryDirectory(prefix="installer-", dir=self._operations) as raw:
            operation = Path(raw).resolve(strict=True)
            tool_roots = [
                item.parent
                for item in (self.node, self.npm, self.uv, self.python)
                if item is not None and not str(item).startswith(("/usr/", "/bin/", "/opt/homebrew/"))
            ]
            read_roots = [
                self._node_cache,
                self._python_cache,
                *tool_roots,
                *extra_read_roots,
            ]
            profile = SandboxGrantProfile.build(
                workspace_root=candidate,
                scratch_root=operation,
                # ``candidate`` is the transaction's unpublished staging
                # root. Installers must be able to materialize its lockfile
                # and package tree; the project workspace is not mounted here.
                workspace_writable=True,
                external_read_roots=read_roots,
                external_write_roots=(self._node_cache, self._python_cache),
                network_allowed=True,
                timeout_seconds=timeout or self.timeout,
            )
            runner = MacOSSeatbeltRunner(profile, runtime_root=operation / "runner")
            path_entries = [
                str(item.parent)
                for item in (self.node, self.npm, self.uv, self.python)
                if item is not None
            ]
            path_entries.extend(
                ["/opt/homebrew/bin", "/usr/local/bin", "/usr/bin", "/bin", "/usr/sbin", "/sbin"]
            )
            return runner.execute(
                command,
                timeout=timeout or self.timeout,
                environment={
                    "PATH": ":".join(dict.fromkeys(path_entries)),
                    "HOME": str(runner.home),
                    "TMPDIR": str(runner.tmp),
                    "npm_config_cache": str(self._node_cache),
                    "UV_CACHE_DIR": str(self._python_cache),
                    "PYTHONNOUSERSITE": "1",
                },
            )

    @staticmethod
    def _write_new_json(path: Path, value: object) -> None:
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        descriptor = os.open(path, flags, 0o600)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8", closefd=False) as destination:
                json.dump(value, destination, ensure_ascii=False, sort_keys=True)
                destination.write("\n")
                destination.flush()
                os.fsync(destination.fileno())
        finally:
            os.close(descriptor)

    def resolve_shared_node_runtime(
        self,
        *,
        dependencies: dict[str, str],
        expected_runtime_image_digest: str,
        resolution_path: Path,
    ) -> ExecuteResponse:
        self._require_digest(expected_runtime_image_digest)
        if self.npm is None or self.node is None:
            return ExecuteResponse(output="Host Node/npm runtime is unavailable.", exit_code=69)
        candidate = self._candidate(resolution_path)
        self._write_new_json(
            candidate / "package.json",
            {
                "name": "puddingclaw-host-runtime",
                "private": True,
                "version": "0.0.0",
                "dependencies": dict(sorted(dependencies.items())),
            },
        )
        command = shlex.join(
            [
                str(self.npm),
                "install",
                "--package-lock-only",
                "--ignore-scripts",
                "--no-audit",
                "--no-fund",
                "--registry=https://registry.npmjs.org/",
            ]
        )
        return self._run_installer(candidate, command, timeout=300)

    def build_shared_node_runtime(
        self,
        *,
        expected_runtime_image_digest: str,
        runtime_path: Path,
        container_path: str,
    ) -> ExecuteResponse:
        del container_path
        self._require_digest(expected_runtime_image_digest)
        if self.npm is None or self.node is None:
            return ExecuteResponse(output="Host Node/npm runtime is unavailable.", exit_code=69)
        candidate = self._candidate(runtime_path)
        command = shlex.join(
            [
                str(self.npm),
                "ci",
                "--no-audit",
                "--no-fund",
                "--registry=https://registry.npmjs.org/",
            ]
        )
        return self._run_installer(candidate, command)

    def resolve_python_skill_runtime(
        self,
        *,
        expected_runtime_image_digest: str,
        resolution_path: Path,
    ) -> ExecuteResponse:
        self._require_python_digest(expected_runtime_image_digest)
        if self.uv is None:
            return ExecuteResponse(output="Host uv runtime is unavailable.", exit_code=69)
        candidate = self._candidate(resolution_path)
        command = shlex.join(
            [
                str(self.uv),
                "pip",
                "compile",
                "--generate-hashes",
                "--no-header",
                "--no-annotate",
                "--index-url",
                "https://pypi.org/simple",
                "--output-file",
                str(candidate / "requirements.lock"),
                str(candidate / "requirements.in"),
            ]
        )
        return self._run_installer(candidate, command, extra_read_roots=(self.python.parent.parent,), timeout=300)

    def build_python_skill_runtime(
        self,
        *,
        expected_runtime_image_digest: str,
        runtime_path: Path,
        container_path: str,
        uv_cache_path: Path,
    ) -> ExecuteResponse:
        del container_path
        self._require_python_digest(expected_runtime_image_digest)
        if self.uv is None:
            return ExecuteResponse(output="Host uv runtime is unavailable.", exit_code=69)
        candidate = self._candidate(runtime_path)
        cache = uv_cache_path.expanduser().resolve(strict=True)
        if cache != self._python_cache.resolve(strict=True):
            raise ValueError("Python installer requested an unexpected uv cache")
        venv = candidate / ".venv"
        interpreter = venv / "bin" / "python"
        command = " && ".join(
            (
                shlex.join(
                    [
                        str(self.uv),
                        "venv",
                        "--no-project",
                        "--no-python-downloads",
                        "--python",
                        str(self.python),
                        str(venv),
                    ]
                ),
                shlex.join(["/bin/rm", str(interpreter)]),
                shlex.join(["/bin/cp", str(self.python), str(interpreter)]),
                shlex.join(["/bin/cp", str(self.python_library), str(venv / "lib" / self.python_library.name)]),
                shlex.join(
                    [
                        str(self.uv),
                        "pip",
                        "sync",
                        "--require-hashes",
                        "--python",
                        str(interpreter),
                        str(candidate / "requirements.lock"),
                    ]
                ),
            )
        )
        return self._run_installer(candidate, command, extra_read_roots=(self.python.parent.parent,))

    def install_packages(
        self,
        skill_id: str,
        skill_version: str,
        ecosystem: str,
        packages: list[str],
        executables: dict[str, list[str]] | None = None,
    ) -> ExecuteResponse:
        from runtime_identity.software_runtime import SoftwareRuntimeManager

        self._ensure_roots()
        try:
            if ecosystem == "node":
                manager = SoftwareRuntimeManager(self.paths, self.runtime_contract)
                installed = manager.install_node_owner(
                    self,
                    owner=f"skill:{skill_id}",
                    owner_revision=skill_version,
                    distributions=packages,
                    declared_bins={
                        package: tuple(bins) for package, bins in (executables or {}).items()
                    },
                    merge_owner=True,
                )
            elif ecosystem == "python":
                manager = SoftwareRuntimeManager(self.paths, self.python_runtime_contract)
                installed = manager.install_python_skill(
                    self,
                    skill_id=skill_id,
                    skill_version=skill_version,
                    requirements=packages,
                )
            else:
                return ExecuteResponse(output=f"Unsupported package ecosystem: {ecosystem}", exit_code=64)
        except (OSError, ValueError) as exc:
            return ExecuteResponse(
                output=f"Skill dependency transaction failed: {type(exc).__name__}: {exc}",
                exit_code=65,
            )
        payload = {
            "status": "installed" if installed.exit_code == 0 else "failed",
            "skill_id": skill_id,
            "skill_version": skill_version,
            "ecosystem": ecosystem,
            "runtime_kind": installed.runtime_kind,
            "revision": installed.revision,
            "previous_revision": installed.previous_revision,
            "runtime_environment_digest": installed.runtime_image_digest,
            "changed": installed.changed,
            "diagnostic": installed.output,
        }
        return ExecuteResponse(
            output=json.dumps(payload, ensure_ascii=False, sort_keys=True),
            exit_code=installed.exit_code,
        )

    def resolve_generic_node_cli(
        self,
        *,
        distribution: str,
        package: str,
    ) -> HostNodePackageResolution:
        """Resolve public npm metadata without involving a Docker image."""

        self._ensure_roots()
        if self.npm is None or self.node is None:
            raise ValueError("Host Node/npm runtime is unavailable")
        with tempfile.TemporaryDirectory(prefix="node-metadata-", dir=self._operations) as raw:
            candidate = Path(raw).resolve(strict=True)
            result = self._run_installer(
                candidate,
                shlex.join(
                    [
                        str(self.npm),
                        "view",
                        distribution,
                        "name",
                        "version",
                        "dist.integrity",
                        "bin",
                        "--json",
                    ]
                ),
                timeout=60,
            )
        if int(result.exit_code or 0) != 0:
            raise ValueError("npm package selector could not be resolved")
        try:
            value = json.loads(str(result.output or ""))
        except (TypeError, ValueError) as exc:
            raise ValueError("npm registry returned invalid package metadata") from exc
        if not isinstance(value, dict):
            raise ValueError("npm selector must resolve to exactly one version")
        resolved_name = str(value.get("name") or "")
        resolved_version = str(value.get("version") or "")
        dist = value.get("dist")
        resolved_integrity = str(
            value.get("dist.integrity")
            or (dist.get("integrity") if isinstance(dist, dict) else "")
            or ""
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
            or re.fullmatch(r"\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?", resolved_version) is None
            or re.fullmatch(r"sha512-[A-Za-z0-9+/]+={0,2}", resolved_integrity) is None
            or any(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", item) is None for item in executables)
        ):
            raise ValueError("npm package registry identity is incompatible")
        return HostNodePackageResolution(
            package=resolved_name,
            version=resolved_version,
            integrity=resolved_integrity,
            distribution=f"{resolved_name}@{resolved_version}",
            runtime_image_digest=self._runtime_digest,
            executables=executables,
        )

    def generic_node_runtime_current(self, runtime_digest: str) -> Path:
        from runtime_identity.software_runtime import SoftwareRuntimeManager

        return SoftwareRuntimeManager(self.paths, self.runtime_contract).node_current(runtime_digest)

    def install_generic_node_cli(
        self,
        *,
        package: str,
        distribution: str,
        executables: tuple[str, ...],
        integrity: str,
        owner_revision: str,
        runtime_digest: str,
        base_revision: str,
    ) -> object:
        from runtime_identity.software_runtime import SoftwareRuntimeManager

        self._ensure_roots()
        return SoftwareRuntimeManager(self.paths, self.runtime_contract).install_node_owner(
            self,
            owner=f"cli:{package}",
            owner_revision=owner_revision,
            distributions=[distribution],
            declared_bins={package: executables},
            expected_integrities={package: integrity},
            expected_runtime_image_digest=runtime_digest,
            expected_base_revision=base_revision,
        )

    def resolve_managed_node_cli(
        self,
        *,
        distribution: str,
        package: str,
    ) -> HostNodePackageResolution:
        """Resolve an Adapter-owned CLI on the same immutable host runtime."""

        return self.resolve_generic_node_cli(distribution=distribution, package=package)

    def resolve_host_lark_cli(self) -> object:
        """Resolve the globally installed official CLI without materializing it."""

        return self.lark_cli.resolve()

    def resolve_host_lark_package(self, *, distribution: str, package: str) -> object:
        return self.lark_cli.resolve_package(distribution, package)

    def install_host_lark_cli(self, distribution: str, *, expected_version: str) -> object:
        return self.lark_cli.install(
            distribution,
            expected_version=expected_version,
            timeout=self.timeout,
        )

    @staticmethod
    def _credential_paths(spec: object | None) -> tuple[str, ...]:
        from harness.workspace_backends import _credential_state_paths

        if spec is None:
            return ()
        return _credential_state_paths(tuple(getattr(spec, "paths", ())), allow_empty=True)

    @staticmethod
    def _import_credential_state(home: Path, state_paths: tuple[str, ...], payload: bytes) -> None:
        from runtime_identity.profiles import validate_credential_archive

        for relative in state_paths:
            target = home / relative
            target.mkdir(parents=True, mode=0o700, exist_ok=True)
        if not payload:
            return
        validate_credential_archive(payload, allowed_roots=state_paths)
        with tarfile.open(fileobj=io.BytesIO(payload), mode="r:gz") as archive:
            archive.extractall(home, filter="data")

    @staticmethod
    def _export_credential_state(
        home: Path,
        state_paths: tuple[str, ...],
        credential_state_spec: object | None = None,
    ) -> bytes | None:
        if not state_paths:
            return None
        from runtime_identity.adapters import CredentialStateSpec
        from runtime_identity.host_credentials import collect_host_credential_state
        from runtime_identity.profiles import validate_credential_archive

        if isinstance(credential_state_spec, CredentialStateSpec):
            collect_host_credential_state(credential_state_spec, home)

        output = io.BytesIO()
        with tarfile.open(fileobj=output, mode="w:gz", dereference=False) as archive:
            for relative in state_paths:
                target = home / relative
                if not target.is_dir() or target.is_symlink():
                    raise ValueError("managed credential state root is invalid")
                archive.add(target, arcname=relative, recursive=True)
        payload = output.getvalue()
        return validate_credential_archive(payload, allowed_roots=state_paths)

    def _managed_runner(
        self,
        *,
        workspace: Path,
        toolchain_path: Path,
        operation: Path,
        network_enabled: bool,
        workspace_writable: bool,
        timeout: int,
        max_output_bytes: int,
    ) -> MacOSSeatbeltRunner:
        workspace = workspace.expanduser().resolve(strict=True)
        toolchain_path = toolchain_path.expanduser().resolve(strict=True)
        profile = SandboxGrantProfile.build(
            workspace_root=workspace,
            scratch_root=operation,
            external_read_roots=(toolchain_path,),
            workspace_writable=workspace_writable,
            network_allowed=network_enabled,
            timeout_seconds=timeout,
            max_output_bytes=max_output_bytes,
        )
        return MacOSSeatbeltRunner(profile, runtime_root=operation / "runner")

    def _managed_environment(
        self,
        *,
        release: Path,
        home: Path,
        command_environment: dict[str, str],
        credential_state_spec: object | None,
    ) -> dict[str, str]:
        credential_environment = dict(getattr(credential_state_spec, "env", ()))
        if set(command_environment) & set(credential_environment):
            raise ValueError("command environment cannot override Adapter credential-state environment")
        environment = {
            "PATH": ":".join(
                dict.fromkeys(
                    (
                        str(release / "bin"),
                        str(self.node.parent) if self.node is not None else "",
                        "/opt/homebrew/bin",
                        "/usr/local/bin",
                        "/usr/bin",
                        "/bin",
                        "/usr/sbin",
                        "/sbin",
                    )
                )
            ),
            "NODE_PATH": str(release / "lib" / "node_modules"),
            **command_environment,
        }
        for key, value in credential_environment.items():
            rendered = str(value).replace("/home/puddingclaw", str(home))
            if "/home/puddingclaw" in rendered:
                raise ValueError("credential environment contains an invalid runner HOME projection")
            environment[str(key)] = rendered
        return environment

    def run_managed_provider_cli(
        self,
        workspace: Path,
        *,
        argv: list[str],
        environment: dict[str, str],
        credential_state_spec: object | None,
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
        owner_user_id: str = "local",
        profile_id: str = "lark_default",
    ) -> object:
        """Run exact Adapter argv.

        Lark is a host-native user CLI: its binary and provider state are
        shared outside Spawn/Kernel. Other managed runtimes retain the generic
        immutable-runtime path below.
        """

        if argv and Path(argv[0]).name.lower() == "lark-cli":
            resolution = self.lark_cli.resolve()
            if resolution.executable is None:
                from harness.workspace_backends import ManagedProviderExecutionResult

                return ManagedProviderExecutionResult(
                    output="lark-cli is not installed globally.",
                    exit_code=127,
                    credential_state=None,
                )
            return self.lark_cli.execute(
                executable=resolution.executable,
                workspace=workspace,
                argv=argv,
                environment=environment,
                owner_user_id=owner_user_id,
                # Local inspections such as ``lark-cli --version`` have no
                # CredentialProfile. They still need a deterministic config
                # directory because the official CLI resolves its runtime
                # context before dispatching the command.
                profile_id=profile_id or "lark_default",
                credential_state=credential_state,
                credential_state_spec=credential_state_spec,
                continuation_secret=continuation_secret,
                continuation_argument=continuation_argument,
                continuation_trailing_argv=continuation_trailing_argv,
                timeout=timeout,
                max_output_bytes=max_output_bytes,
            )

        del container_path
        from harness.workspace_backends import (
            ManagedProviderExecutionResult,
            _shared_runtime_executable,
        )

        self._ensure_roots()
        self._require_digest(expected_runtime_image_digest)
        release = toolchain_path.expanduser().resolve(strict=True)
        if not argv:
            raise ValueError("managed provider command requires an executable")
        executable = _shared_runtime_executable(release, argv[0], expected_runtime_image_digest)
        state_paths = self._credential_paths(credential_state_spec)
        if not state_paths and (credential_state or getattr(credential_state_spec, "env", ())):
            raise ValueError("credentialless managed command cannot receive provider state")
        if continuation_secret is not None and not (
            continuation_argument and re.fullmatch(r"--[a-z0-9][a-z0-9-]*", continuation_argument)
        ):
            raise ValueError("continuation secret requires a trusted Driver argument contract")
        if continuation_secret is None and (continuation_argument is not None or continuation_trailing_argv):
            raise ValueError("continuation arguments require a continuation secret")
        if any(not value or "\x00" in value for value in continuation_trailing_argv):
            raise ValueError("continuation trailing argv is invalid")
        operation = Path(tempfile.mkdtemp(prefix="provider-", dir=self._operations)).resolve(strict=True)
        try:
            runner = self._managed_runner(
                workspace=workspace,
                toolchain_path=release,
                operation=operation,
                network_enabled=network_enabled,
                workspace_writable=workspace_writable,
                timeout=timeout,
                max_output_bytes=max_output_bytes,
            )
            self._import_credential_state(runner.home, state_paths, credential_state)
            from runtime_identity.adapters import CredentialStateSpec
            from runtime_identity.host_credentials import prepare_host_credential_state

            if isinstance(credential_state_spec, CredentialStateSpec):
                prepare_host_credential_state(credential_state_spec, runner.home)
            run_environment = self._managed_environment(
                release=release,
                home=runner.home,
                command_environment=environment,
                credential_state_spec=credential_state_spec,
            )
            base_argv = [str(release / "bin" / executable), *argv[1:]]
            if continuation_secret is None:
                # Adapter planning has already normalized the command to argv.
                # Keep payload values (Markdown, JSON, IDs) opaque all the way
                # into sandbox-exec; do not round-trip them through a shell.
                response = runner.execute_argv(
                    base_argv,
                    timeout=timeout,
                    environment=run_environment,
                    cwd=workspace.expanduser().resolve(strict=True),
                )
            else:
                secret = continuation_secret.decode("utf-8")
                if "\x00" in secret:
                    raise ValueError("authorization continuation contains a NUL byte")
                continuation_command = shlex.join([*base_argv, str(continuation_argument)])
                trailing = shlex.join(list(continuation_trailing_argv))
                command = (
                    f'secret="$(cat)"; exec {continuation_command} "$secret"'
                    + (f" {trailing}" if trailing else "")
                )
                # The continuation secret is deliberately absent from the
                # host argv and environment. A tiny shell inside Seatbelt reads
                # it from stdin and appends it to the trusted Driver argv.
                response = runner.execute(
                    command,
                    timeout=timeout,
                    environment=run_environment,
                    cwd=workspace.expanduser().resolve(strict=True),
                    input_text=secret,
                )
            exported = self._export_credential_state(
                runner.home,
                state_paths,
                credential_state_spec,
            )
            return ManagedProviderExecutionResult(
                output=response.output,
                exit_code=response.exit_code,
                credential_state=exported,
                truncated=response.truncated,
            )
        finally:
            shutil.rmtree(operation, ignore_errors=True)

    @staticmethod
    def _managed_browser_job_id(owner_user_id: str, provider: str, profile_id: str) -> str:
        return hashlib.sha256(f"{owner_user_id}\0{provider}\0{profile_id}".encode()).hexdigest()[:24]

    @staticmethod
    def _browser_output(job: _ManagedBrowserJob, max_output_bytes: int) -> tuple[str, bool]:
        job.output_handle.flush()
        data = job.output_path.read_bytes()
        truncated = len(data) > max_output_bytes
        if truncated:
            data = data[-max_output_bytes:]
        output = data.decode("utf-8", errors="replace") or "<no output>"
        if truncated:
            output = f"... Earlier output truncated.\n{output}"
        return output, truncated

    def collect_managed_browser_auth_cli(
        self,
        *,
        owner_user_id: str,
        provider: str,
        profile_id: str,
        credential_state_spec: object,
        adapter_id: str,
        authorization_contract_fingerprint: str,
        max_output_bytes: int = 100_000,
    ) -> object:
        if provider == "lark":
            return self.lark_cli.collect_browser(
                owner_user_id=owner_user_id,
                profile_id=profile_id,
                adapter_id=adapter_id,
                authorization_contract_fingerprint=authorization_contract_fingerprint,
                credential_state_spec=credential_state_spec,
                max_output_bytes=max_output_bytes,
            )
        from harness.workspace_backends import ManagedProviderExecutionResult

        job_id = self._managed_browser_job_id(owner_user_id, provider, profile_id)
        with self._browser_jobs_lock:
            job = self._browser_jobs.get(job_id)
        fingerprint = str(getattr(credential_state_spec, "fingerprint", ""))
        if (
            job is None
            or job.owner_user_id != owner_user_id
            or job.provider != provider
            or job.profile_id != profile_id
            or job.adapter_id != adapter_id
            or job.authorization_contract_fingerprint != authorization_contract_fingerprint
            or job.credential_state_fingerprint != fingerprint
        ):
            return ManagedProviderExecutionResult(
                output="Managed browser authorization job is missing or expired.",
                exit_code=1,
                credential_state=None,
                browser_status="missing",
                browser_job_id=job_id,
            )
        if time.time() - job.created_at > 1500 and job.process.poll() is None:
            os.killpg(job.process.pid, signal.SIGTERM)
            try:
                job.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                os.killpg(job.process.pid, signal.SIGKILL)
        output, truncated = self._browser_output(job, max_output_bytes)
        exit_code = job.process.poll()
        if exit_code is None:
            return ManagedProviderExecutionResult(
                output=output,
                exit_code=0,
                credential_state=None,
                truncated=truncated,
                browser_status="awaiting_user_browser",
                browser_job_id=job_id,
            )
        try:
            state = self._export_credential_state(job.home, job.state_paths, credential_state_spec)
        except (OSError, ValueError) as exc:
            output = f"{output.rstrip()}\n\nCredential export failed: {type(exc).__name__}: {exc}"
            exit_code = exit_code or 1
            state = None
        if exit_code:
            output = f"{output.rstrip()}\n\nExit code: {exit_code}"
        return ManagedProviderExecutionResult(
            output=output,
            exit_code=int(exit_code or 0),
            credential_state=state,
            truncated=truncated,
            browser_status="completed" if exit_code == 0 else "failed",
            browser_job_id=job_id,
        )

    def run_managed_browser_auth_cli(
        self,
        workspace: Path,
        *,
        argv: list[str],
        environment: dict[str, str],
        credential_state_spec: object,
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
    ) -> object:
        if provider == "lark":
            resolution = self.lark_cli.resolve()
            if resolution.executable is None:
                from harness.workspace_backends import ManagedProviderExecutionResult

                return ManagedProviderExecutionResult(
                    output="lark-cli is not installed globally.",
                    exit_code=127,
                    credential_state=None,
                    browser_status="missing",
                    browser_job_id=self.lark_cli.job_id(owner_user_id, profile_id),
                )
            return self.lark_cli.start_browser(
                executable=resolution.executable,
                workspace=workspace,
                argv=argv,
                environment=environment,
                owner_user_id=owner_user_id,
                profile_id=profile_id,
                adapter_id=adapter_id,
                authorization_contract_fingerprint=authorization_contract_fingerprint,
                credential_state=credential_state,
                credential_state_spec=credential_state_spec,
                wait_for_output_seconds=wait_for_url_seconds,
                max_output_bytes=max_output_bytes,
            )
        del container_path
        from harness.workspace_backends import (
            ManagedProviderExecutionResult,
            _shared_runtime_executable,
        )

        self._ensure_roots()
        self._require_digest(expected_runtime_image_digest)
        if not re.fullmatch(r"[a-z0-9][a-z0-9_.-]*", adapter_id):
            raise ValueError("managed browser Adapter id is invalid")
        if not re.fullmatch(r"[0-9a-f]{64}", authorization_contract_fingerprint):
            raise ValueError("managed browser authorization contract is invalid")
        release = toolchain_path.expanduser().resolve(strict=True)
        if not argv:
            raise ValueError("managed browser command requires an executable")
        executable = _shared_runtime_executable(release, argv[0], expected_runtime_image_digest)
        state_paths = self._credential_paths(credential_state_spec)
        job_id = self._managed_browser_job_id(owner_user_id, provider, profile_id)
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
        operation = Path(tempfile.mkdtemp(prefix="browser-auth-", dir=self._operations)).resolve(strict=True)
        output_handle = None
        try:
            runner = self._managed_runner(
                workspace=workspace,
                toolchain_path=release,
                operation=operation,
                network_enabled=True,
                workspace_writable=False,
                timeout=1500,
                max_output_bytes=max_output_bytes,
            )
            self._import_credential_state(runner.home, state_paths, credential_state)
            from runtime_identity.adapters import CredentialStateSpec
            from runtime_identity.host_credentials import prepare_host_credential_state

            if isinstance(credential_state_spec, CredentialStateSpec):
                prepare_host_credential_state(credential_state_spec, runner.home)
            run_environment = self._managed_environment(
                release=release,
                home=runner.home,
                command_environment=environment,
                credential_state_spec=credential_state_spec,
            )
            output_path = operation / "browser-output.log"
            output_handle = output_path.open("w", encoding="utf-8")
            process = runner.start_background_argv(
                [str(release / "bin" / executable), *argv[1:]],
                environment=run_environment,
                cwd=workspace.expanduser().resolve(strict=True),
                output=output_handle,
            )
            job = _ManagedBrowserJob(
                job_id=job_id,
                owner_user_id=owner_user_id,
                provider=provider,
                profile_id=profile_id,
                adapter_id=adapter_id,
                authorization_contract_fingerprint=authorization_contract_fingerprint,
                credential_state_fingerprint=str(getattr(credential_state_spec, "fingerprint", "")),
                operation=operation,
                home=runner.home,
                output_path=output_path,
                output_handle=output_handle,
                process=process,
                state_paths=state_paths,
                created_at=time.time(),
            )
            with self._browser_jobs_lock:
                if job_id in self._browser_jobs:
                    os.killpg(process.pid, signal.SIGTERM)
                    raise ValueError("managed browser authorization job already exists")
                self._browser_jobs[job_id] = job
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
                if result.output.strip() and result.output != "<no output>":
                    output_seen_at = output_seen_at or time.monotonic()
                    if time.monotonic() - output_seen_at >= 0.5:
                        return result
                if time.monotonic() >= deadline:
                    self.finalize_managed_browser_auth_cli(
                        owner_user_id=owner_user_id,
                        provider=provider,
                        profile_id=profile_id,
                        browser_job_id=job_id,
                    )
                    return ManagedProviderExecutionResult(
                        output="Browser authorization did not emit public output in time.",
                        exit_code=1,
                        credential_state=None,
                        browser_status="failed",
                        browser_job_id=job_id,
                    )
                time.sleep(0.25)
        except Exception:
            with self._browser_jobs_lock:
                registered = self._browser_jobs.pop(job_id, None)
            if registered is not None and registered.process.poll() is None:
                os.killpg(registered.process.pid, signal.SIGKILL)
            if output_handle is not None:
                output_handle.close()
            shutil.rmtree(operation, ignore_errors=True)
            raise

    def finalize_managed_browser_auth_cli(
        self,
        *,
        owner_user_id: str,
        provider: str,
        profile_id: str,
        browser_job_id: str,
    ) -> bool:
        if provider == "lark":
            return self.lark_cli.finalize_browser(owner_user_id, profile_id, browser_job_id)
        expected = self._managed_browser_job_id(owner_user_id, provider, profile_id)
        if browser_job_id != expected:
            raise ValueError("browser authorization job identity mismatch")
        with self._browser_jobs_lock:
            job = self._browser_jobs.pop(expected, None)
        if job is None:
            return False
        try:
            if job.process.poll() is None:
                os.killpg(job.process.pid, signal.SIGTERM)
                try:
                    job.process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    os.killpg(job.process.pid, signal.SIGKILL)
                    job.process.wait(timeout=5)
            return True
        finally:
            job.output_handle.close()
            shutil.rmtree(job.operation, ignore_errors=True)

    def list_managed_browser_auth_jobs(self, *, owner_user_id: str) -> list[dict[str, str]]:
        lark_jobs = self.lark_cli.list_browser_jobs(owner_user_id)
        with self._browser_jobs_lock:
            jobs = tuple(self._browser_jobs.values())
        generic_jobs = [
            {
                "provider": job.provider,
                "profile_id": job.profile_id,
                "browser_job_id": job.job_id,
                "adapter_id": job.adapter_id,
                "authorization_contract_fingerprint": job.authorization_contract_fingerprint,
                "credential_state_fingerprint": job.credential_state_fingerprint,
            }
            for job in jobs
            if job.owner_user_id == owner_user_id
        ]
        return [*lark_jobs, *generic_jobs]

    def project_cli_execution(self, command: str) -> HostExecutionProjection:
        """Expose only verified credentialless CLI bins to a Seatbelt command."""

        from runtime_identity.software_runtime import SoftwareRuntimeManager

        root = self.paths.shared_node_runtime(self.runtime_contract)
        if not root.is_dir():
            return HostExecutionProjection(command)
        release = SoftwareRuntimeManager(self.paths, self.runtime_contract).node_current(
            self._runtime_digest
        )
        if release.name == "empty":
            return HostExecutionProjection(command)
        public_bin = release / "public-bin"
        if not any(public_bin.iterdir()):
            return HostExecutionProjection(command)
        path = ":".join(
            (
                "/opt/homebrew/bin",
                "/usr/local/bin",
                "/usr/bin",
                "/bin",
                "/usr/sbin",
                "/sbin",
                str(public_bin),
            )
        )
        return HostExecutionProjection(
            command,
            (release,),
            (("PATH", path),),
            environment_binding_digest=hashlib.sha256(
                f"cli\0{release.name}\0{path}".encode()
            ).hexdigest(),
        )

    def project_skill_execution(
        self,
        command: str,
        aliases: tuple[tuple[str, Path], ...],
        *,
        skill_id: str | None = None,
    ) -> HostExecutionProjection:
        """Bind one Skill command to already-published host runtime releases.

        This method never installs or repairs anything.  Missing environments
        simply leave the command on the base host runtime; a damaged published
        environment fails closed through ``SoftwareRuntimeManager`` validation.
        """

        from runtime_identity.software_runtime import SoftwareRuntimeManager, skill_content_version

        explicit_skill_ids = {
            match.group(1)
            for match in re.finditer(r"(?:^|[\s'\"])/skills/([A-Za-z0-9][A-Za-z0-9_.-]{0,127})(?:/|[\s'\"]|$)", command)
        }
        if len(explicit_skill_ids) > 1:
            return HostExecutionProjection(command)
        if explicit_skill_ids:
            explicit_skill_id = next(iter(explicit_skill_ids))
            if skill_id is not None and skill_id != explicit_skill_id:
                raise ValueError("active Skill does not match the command Skill path")
            skill_id = explicit_skill_id
        if skill_id is None or not re.fullmatch(
            r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}",
            skill_id,
        ):
            return HostExecutionProjection(command)
        skills_root = next((root for virtual, root in aliases if virtual == "/skills"), None)
        if skills_root is None:
            return HostExecutionProjection(command)
        skill_root = (skills_root / skill_id).resolve(strict=True)
        skill_root.relative_to(skills_root.resolve(strict=True))
        if skill_root.is_symlink() or not (skill_root / "SKILL.md").is_file():
            raise ValueError("managed Skill source is invalid")
        skill_version = skill_content_version(skill_root)
        python_manager = SoftwareRuntimeManager(self.paths, self.python_runtime_contract)
        node_manager = SoftwareRuntimeManager(self.paths, self.runtime_contract)
        read_roots: list[Path] = []
        path_entries: list[str] = []
        environment: dict[str, str] = {"PYTHONNOUSERSITE": "1"}

        python_root = self.paths.python_skill_runtime(
            self.python_runtime_contract,
            skill_id,
            skill_version,
        )
        if python_root.is_dir():
            release = python_manager.python_skill_current(
                skill_id,
                skill_version,
                self._python_runtime_digest,
            )
            if release is not None:
                path_entries.append(str(release / ".venv" / "bin"))
                read_roots.extend((release, self.python.parent.parent))
                environment["PYTHONHOME"] = str(self.python.parent.parent)

        node_root = self.paths.shared_node_runtime(self.runtime_contract)
        if node_root.is_dir():
            release = node_manager.node_current(self._runtime_digest)
            if release.name != "empty":
                path_entries.extend((str(release / "bin"), str(release / "public-bin")))
                environment["NODE_PATH"] = str(release / "lib" / "node_modules")
                read_roots.append(release)

        public_bins = [item for item in path_entries if item.endswith("/public-bin")]
        private_runtime_bins = [item for item in path_entries if item not in public_bins]
        environment["PATH"] = ":".join(
            dict.fromkeys(
                (
                    *private_runtime_bins,
                    "/opt/homebrew/bin",
                    "/usr/local/bin",
                    "/usr/bin",
                    "/bin",
                    "/usr/sbin",
                    "/sbin",
                    *public_bins,
                )
            )
        )
        from runtime_identity.paths import trusted_owner_user_id
        from runtime_identity.skill_secrets import SkillSecretStore

        secret_store = SkillSecretStore(self.paths, trusted_owner_user_id())
        secret_projection = secret_store.projection(
            skill_id=skill_id,
            skill_version=skill_version,
        )
        overlap = set(environment).intersection(secret_projection.environment)
        if overlap:
            raise ValueError("Skill Secret attempts to override a managed runtime variable")
        environment.update(secret_projection.environment)
        binding_digest = hashlib.sha256(
            json.dumps(
                {
                    "skill": skill_id,
                    "skill_version": skill_version,
                    "runtime": self._runtime_digest,
                    "roots": [str(item) for item in read_roots],
                    "secret_binding": secret_projection.binding_digest,
                    "environment_names": sorted(environment),
                },
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        ).hexdigest()
        def validator() -> bool:
            if not secret_projection.environment:
                return True
            return secret_store.revision_is_current(secret_projection.registry_revision)
        return HostExecutionProjection(
            command=command,
            read_roots=tuple(dict.fromkeys(read_roots)),
            environment=tuple(sorted(environment.items())),
            secret_values=tuple(secret_projection.environment.values()),
            environment_binding_digest=binding_digest,
            environment_current=validator,
        )

    def published_python_skill_ids(
        self,
        skill_ids: tuple[str, ...],
        aliases: tuple[tuple[str, Path], ...],
    ) -> tuple[str, ...]:
        """Return activated Skills that own a verified published Python env.

        The ids come from the durable Run activation registry, not from Agent
        command text.  A damaged environment is deliberately allowed to raise
        rather than being hidden as an absent dependency.
        """

        from runtime_identity.software_runtime import SoftwareRuntimeManager, skill_content_version

        skills_root = next((root for virtual, root in aliases if virtual == "/skills"), None)
        if skills_root is None:
            return ()
        trusted_root = skills_root.resolve(strict=True)
        manager = SoftwareRuntimeManager(self.paths, self.python_runtime_contract)
        published: list[str] = []
        for candidate in dict.fromkeys(skill_ids):
            if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", candidate):
                continue
            skill_root = (trusted_root / candidate).resolve(strict=True)
            skill_root.relative_to(trusted_root)
            if skill_root.is_symlink() or not (skill_root / "SKILL.md").is_file():
                raise ValueError("managed Skill source is invalid")
            skill_version = skill_content_version(skill_root)
            runtime_root = self.paths.python_skill_runtime(
                self.python_runtime_contract,
                candidate,
                skill_version,
            )
            if not runtime_root.is_dir():
                continue
            if (
                manager.python_skill_current(
                    candidate,
                    skill_version,
                    self._python_runtime_digest,
                )
                is not None
            ):
                published.append(candidate)
        return tuple(published)
