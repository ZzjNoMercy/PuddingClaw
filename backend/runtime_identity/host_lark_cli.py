"""Direct host runtime for the official Lark CLI.

The official CLI is a local user tool, not an application package that needs
to be copied into every Agent runner.  This module follows the same boundary
as DeerFlow's Lark integration:

* resolve one globally installed executable;
* give every PuddingClaw user/profile a stable config directory;
* execute exact argv directly (never through the project shell);
* keep authorization processes only for commands that genuinely outlive one
  request.

No provider token is passed in argv/environment, copied into a temporary HOME,
or exported back into PuddingClaw's encrypted profile store.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tarfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from runtime_identity.paths import PuddingClawPaths, safe_identity_component

_LARK_DISTRIBUTION = re.compile(
    r"@larksuite/cli(?:@(?:latest|\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?))?"
)


@dataclass(frozen=True)
class HostLarkCliResolution:
    executable: Path | None
    version: str | None

    @property
    def available(self) -> bool:
        return self.executable is not None


@dataclass(frozen=True)
class HostLarkPackageResolution:
    package: str
    version: str
    integrity: str
    distribution: str


@dataclass
class _HostLarkBrowserJob:
    job_id: str
    owner_user_id: str
    profile_id: str
    adapter_id: str
    authorization_contract_fingerprint: str
    output_path: Path
    output_handle: Any
    process: subprocess.Popen[str]
    created_at: float


class HostLarkCliRuntime:
    """Shared official CLI plus durable, provider-native profile state."""

    _jobs: dict[str, _HostLarkBrowserJob] = {}
    _jobs_lock = threading.RLock()
    _version = re.compile(r"(?:lark-cli\s+version\s+)?(\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?)")

    def __init__(self, paths: PuddingClawPaths) -> None:
        self.paths = paths

    @staticmethod
    def _candidate_paths() -> tuple[Path, ...]:
        configured = os.environ.get("PUDDINGCLAW_LARK_CLI_PATH", "").strip()
        candidates: list[Path] = []
        if configured:
            candidate = Path(configured).expanduser()
            if not candidate.is_absolute():
                raise ValueError("PUDDINGCLAW_LARK_CLI_PATH must be absolute")
            return (candidate,)
        discovered = shutil.which("lark-cli")
        if discovered:
            candidates.append(Path(discovered))
        home = Path.home()
        candidates.extend(
            (
                home / ".npm-global" / "bin" / "lark-cli",
                home / ".local" / "bin" / "lark-cli",
                Path("/opt/homebrew/bin/lark-cli"),
                Path("/usr/local/bin/lark-cli"),
                Path("/usr/bin/lark-cli"),
            )
        )
        return tuple(dict.fromkeys(candidates))

    def resolve(self) -> HostLarkCliResolution:
        for candidate in self._candidate_paths():
            try:
                resolved = candidate.resolve(strict=True)
            except (FileNotFoundError, OSError):
                continue
            if not resolved.is_file() or not os.access(resolved, os.X_OK):
                continue
            try:
                completed = subprocess.run(
                    [str(resolved), "--version"],
                    capture_output=True,
                    text=True,
                    timeout=10,
                    check=False,
                    env=self._base_environment(),
                )
            except (OSError, subprocess.SubprocessError):
                continue
            output = f"{completed.stdout}\n{completed.stderr}"
            matched = self._version.search(output)
            if completed.returncode == 0 and matched is not None:
                return HostLarkCliResolution(resolved, matched.group(1))
        return HostLarkCliResolution(None, None)

    def resolve_package(self, distribution: str, package: str) -> HostLarkPackageResolution:
        """Resolve official npm metadata without involving a Pudding Toolchain."""

        if package != "@larksuite/cli" or _LARK_DISTRIBUTION.fullmatch(distribution) is None:
            raise ValueError("host lark-cli resolution requires the official npm package")
        npm = shutil.which("npm")
        if not npm:
            raise ValueError("npm is unavailable; install Node.js before lark-cli")
        completed = subprocess.run(
            [npm, "view", distribution, "name", "version", "dist.integrity", "--json"],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
            env=self._base_environment(),
        )
        if completed.returncode != 0:
            raise ValueError("official lark-cli package metadata could not be resolved")
        try:
            value = json.loads(completed.stdout)
        except (TypeError, ValueError) as exc:
            raise ValueError("npm registry returned invalid lark-cli metadata") from exc
        if not isinstance(value, dict):
            raise ValueError("lark-cli selector must resolve to exactly one version")
        resolved_package = str(value.get("name") or "")
        version = str(value.get("version") or "")
        dist = value.get("dist")
        integrity = str(
            value.get("dist.integrity")
            or (dist.get("integrity") if isinstance(dist, dict) else "")
            or ""
        )
        if (
            resolved_package != package
            or self._version.fullmatch(version) is None
            or re.fullmatch(r"sha512-[A-Za-z0-9+/]+={0,2}", integrity) is None
        ):
            raise ValueError("npm registry returned an incompatible lark-cli identity")
        return HostLarkPackageResolution(
            package=resolved_package,
            version=version,
            integrity=integrity,
            distribution=f"{resolved_package}@{version}",
        )

    @staticmethod
    def _native_credential_dir() -> Path:
        configured = os.environ.get("PUDDINGCLAW_LARK_NATIVE_CREDENTIAL_DIR", "").strip()
        if configured:
            path = Path(configured).expanduser()
            if not path.is_absolute():
                raise ValueError("PUDDINGCLAW_LARK_NATIVE_CREDENTIAL_DIR must be absolute")
            return path
        if sys.platform == "darwin":
            return Path.home() / "Library" / "Application Support" / "lark-cli"
        return Path.home() / ".local" / "share" / "lark-cli"

    def install(self, distribution: str, *, expected_version: str, timeout: int = 900) -> object:
        """Install the Adapter-pinned official package into the user's npm prefix."""

        from harness.workspace_backends import ManagedProviderExecutionResult

        if not re.fullmatch(r"@larksuite/cli@\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?", distribution):
            raise ValueError("lark-cli installation requires an exact official distribution")
        npm = shutil.which("npm")
        if not npm:
            raise ValueError("npm is unavailable; install Node.js before lark-cli")
        completed = subprocess.run(
            [npm, "install", "--global", distribution],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
            env=self._base_environment(),
            start_new_session=True,
        )
        output, truncated = self._bounded_output(completed.stdout, completed.stderr, 100_000)
        if completed.returncode == 0:
            resolution = self.resolve()
            if not resolution.available or resolution.version != expected_version:
                return ManagedProviderExecutionResult(
                    output=f"{output}\n\nInstalled executable did not match version {expected_version}.",
                    exit_code=1,
                    credential_state=None,
                    truncated=truncated,
                )
        return ManagedProviderExecutionResult(
            output=output,
            exit_code=completed.returncode,
            credential_state=None,
            truncated=truncated,
        )

    @staticmethod
    def _base_environment() -> dict[str, str]:
        environment = dict(os.environ)
        environment["LARKSUITE_CLI_NO_UPDATE_NOTIFIER"] = "1"
        environment["LARKSUITE_CLI_NO_SKILLS_NOTIFIER"] = "1"
        return environment

    def _profile_directories(self, owner_user_id: str, profile_id: str) -> tuple[Path, Path]:
        owner = safe_identity_component(owner_user_id, field="owner_user_id")
        profile = safe_identity_component(profile_id, field="profile_id")
        root = self.paths.lark_cli_profile_root(owner, profile)
        config = self.paths.lark_cli_config_dir(owner, profile)
        for directory in (root, config, root / "jobs"):
            if directory.is_symlink():
                raise ValueError("lark-cli profile directories must not be symlinks")
            directory.mkdir(parents=True, mode=0o700, exist_ok=True)
            os.chmod(directory, 0o700)
        return root, config

    def environment(
        self,
        owner_user_id: str,
        profile_id: str,
        command_environment: dict[str, str] | None = None,
    ) -> dict[str, str]:
        _, config = self._profile_directories(owner_user_id, profile_id)
        environment = self._base_environment()
        environment.update(command_environment or {})
        environment["LARKSUITE_CLI_CONFIG_DIR"] = str(config)
        return environment

    def profile_state_present(self, owner_user_id: str, profile_id: str) -> bool:
        root, config = self._profile_directories(owner_user_id, profile_id)
        if (root / ".legacy-vault-migrated").is_file() or (root / ".profile-initialized").is_file():
            return True
        config_files = (
            path for path in config.rglob("*") if path.is_file() and "cache" not in path.relative_to(config).parts
        )
        return next(config_files, None) is not None

    def mark_profile_initialized(self, owner_user_id: str, profile_id: str) -> None:
        root, _ = self._profile_directories(owner_user_id, profile_id)
        marker = root / ".profile-initialized"
        marker.write_text("host-native-v1\n", encoding="utf-8")
        os.chmod(marker, 0o600)

    def migrate_legacy_archive_once(
        self,
        owner_user_id: str,
        profile_id: str,
        payload: bytes,
        credential_state_spec: object | None,
    ) -> bool:
        """Import the old Vault archive once, then make native dirs authoritative."""

        root, config = self._profile_directories(owner_user_id, profile_id)
        marker = root / ".legacy-vault-migrated"
        if marker.exists() or not payload:
            return False
        allowed_roots = tuple(str(item) for item in getattr(credential_state_spec, "paths", ()))
        if not allowed_roots:
            return False
        if sys.platform == "win32":
            # The historical archive contains the Linux AES-GCM file-store
            # representation. The official Windows backend stores values in
            # HKCU protected by DPAPI, so byte-copying that archive would not
            # be a migration and could destroy the only usable old copy.
            raise ValueError(
                "legacy lark-cli credential migration is unsupported on Windows; "
                "create a new native profile and authorize it again"
            )
        from runtime_identity.profiles import validate_credential_archive

        validate_credential_archive(payload, allowed_roots=allowed_roots)
        migration = root / ".migration"
        if migration.exists():
            shutil.rmtree(migration)
        migration.mkdir(mode=0o700)
        try:
            with tarfile.open(fileobj=io.BytesIO(payload), mode="r:gz") as archive:
                archive.extractall(migration, filter="data")
            legacy_config = migration / ".lark-cli"
            legacy_file_store = legacy_config / ".credential-data" / "lark-cli"
            if legacy_config.is_dir():
                for child in legacy_config.iterdir():
                    if child.name == ".credential-data":
                        continue
                    target = config / child.name
                    if child.is_dir():
                        shutil.copytree(child, target, dirs_exist_ok=True)
                    elif child.is_file():
                        shutil.copy2(child, target)
            legacy_share = migration / ".local" / "share" / "lark-cli"
            # Older PuddingClaw releases normalized the official CLI's
            # encrypted file-keychain state into the archive. Restore that
            # state to the official host location once; the current CLI then
            # owns refresh/CAS semantics through its keychain package.
            if sys.platform == "darwin":
                from runtime_identity.adapters import CredentialStateSpec
                from runtime_identity.host_credentials import prepare_host_credential_state

                if isinstance(credential_state_spec, CredentialStateSpec):
                    prepare_host_credential_state(credential_state_spec, migration)
                native_source = migration / "Library" / "Application Support" / "lark-cli"
                native_target = self._native_credential_dir()
            else:
                native_source = legacy_share if legacy_share.is_dir() else legacy_file_store
                native_target = self._native_credential_dir()
            if native_source.is_dir():
                if native_target.is_symlink():
                    raise ValueError("native lark-cli credential directory must not be a symlink")
                native_target.mkdir(parents=True, mode=0o700, exist_ok=True)
                os.chmod(native_target, 0o700)
                for source in native_source.iterdir():
                    if not source.is_file() or source.is_symlink():
                        continue
                    target = native_target / source.name
                    if not target.exists():
                        shutil.copyfile(source, target, follow_symlinks=False)
                        os.chmod(target, 0o600)
            marker.write_text("host-native-v1\n", encoding="utf-8")
            os.chmod(marker, 0o600)
            return True
        finally:
            shutil.rmtree(migration, ignore_errors=True)

    def retire_legacy_vault(self, owner_user_id: str, profile_id: str) -> None:
        """Remove the obsolete duplicate after native state has executed successfully."""

        profile_root = self.paths.lark_cli_profile_root(owner_user_id, profile_id)
        if not (profile_root / ".legacy-vault-migrated").is_file():
            return
        legacy = self.paths.provider_profile(owner_user_id, "lark", profile_id)
        for name in ("vault.enc", "profile.json"):
            path = legacy / name
            if path.is_symlink():
                raise ValueError("legacy lark credential state must not be a symlink")
            try:
                path.unlink()
            except FileNotFoundError:
                pass

    @staticmethod
    def compatibility_state_marker(credential_state_spec: object | None) -> bytes | None:
        """Return a token-free marker for callers that still expose a state field.

        The legacy service uses ``None`` to mean "no profile involved".  Until
        all of its bookkeeping fields are removed, an empty directory archive
        preserves that distinction without copying provider credentials.
        """

        roots = tuple(str(item) for item in getattr(credential_state_spec, "paths", ()))
        if not roots:
            return None
        output = io.BytesIO()
        with tarfile.open(fileobj=output, mode="w:gz") as archive:
            now = int(time.time())
            for root in roots:
                info = tarfile.TarInfo(root.rstrip("/"))
                info.type = tarfile.DIRTYPE
                info.mode = 0o700
                info.mtime = now
                archive.addfile(info)
        return output.getvalue()

    @staticmethod
    def _bounded_output(stdout: str, stderr: str, max_output_bytes: int) -> tuple[str, bool]:
        output = "\n".join(part.rstrip() for part in (stdout, stderr) if part).strip() or "<no output>"
        encoded = output.encode("utf-8", errors="replace")
        if len(encoded) <= max_output_bytes:
            return output, False
        tail = encoded[-max_output_bytes:].decode("utf-8", errors="replace")
        return f"... Earlier output truncated.\n{tail}", True

    def execute(
        self,
        *,
        executable: Path,
        workspace: Path,
        argv: list[str],
        environment: dict[str, str],
        owner_user_id: str,
        profile_id: str,
        credential_state: bytes = b"",
        credential_state_spec: object | None = None,
        continuation_secret: bytes | None = None,
        continuation_argument: str | None = None,
        continuation_trailing_argv: tuple[str, ...] = (),
        timeout: int = 120,
        max_output_bytes: int = 100_000,
    ) -> object:
        from harness.workspace_backends import ManagedProviderExecutionResult

        self.migrate_legacy_archive_once(
            owner_user_id,
            profile_id,
            credential_state,
            credential_state_spec,
        )
        command = [str(executable), *argv[1:]]
        if continuation_secret is not None:
            if not continuation_argument:
                raise ValueError("authorization continuation argument is missing")
            secret = continuation_secret.decode("utf-8")
            if not secret or "\x00" in secret:
                raise ValueError("authorization continuation is invalid")
            command.extend((continuation_argument, secret, *continuation_trailing_argv))
        try:
            completed = subprocess.run(
                command,
                cwd=workspace.expanduser().resolve(strict=True),
                env=self.environment(owner_user_id, profile_id, environment),
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
                start_new_session=True,
            )
            output, truncated = self._bounded_output(completed.stdout, completed.stderr, max_output_bytes)
            if completed.returncode == 0:
                self.retire_legacy_vault(owner_user_id, profile_id)
            state_marker = (
                self.compatibility_state_marker(credential_state_spec)
                if self.profile_state_present(owner_user_id, profile_id)
                else None
            )
            return ManagedProviderExecutionResult(
                output=output,
                exit_code=completed.returncode,
                credential_state=state_marker,
                truncated=truncated,
            )
        except subprocess.TimeoutExpired as exc:
            stdout = exc.stdout.decode(errors="replace") if isinstance(exc.stdout, bytes) else (exc.stdout or "")
            stderr = exc.stderr.decode(errors="replace") if isinstance(exc.stderr, bytes) else (exc.stderr or "")
            output, truncated = self._bounded_output(stdout, stderr, max_output_bytes)
            return ManagedProviderExecutionResult(
                output=f"{output}\n\nCommand timed out after {timeout}s.",
                exit_code=124,
                credential_state=(
                    self.compatibility_state_marker(credential_state_spec)
                    if self.profile_state_present(owner_user_id, profile_id)
                    else None
                ),
                truncated=truncated,
            )

    @staticmethod
    def job_id(owner_user_id: str, profile_id: str) -> str:
        return hashlib.sha256(f"{owner_user_id}\0lark\0{profile_id}".encode()).hexdigest()[:24]

    def start_browser(
        self,
        *,
        executable: Path,
        workspace: Path,
        argv: list[str],
        environment: dict[str, str],
        owner_user_id: str,
        profile_id: str,
        adapter_id: str,
        authorization_contract_fingerprint: str,
        credential_state: bytes,
        credential_state_spec: object | None,
        wait_for_output_seconds: float = 30.0,
        max_output_bytes: int = 100_000,
    ) -> object:
        existing = self.collect_browser(
            owner_user_id=owner_user_id,
            profile_id=profile_id,
            adapter_id=adapter_id,
            authorization_contract_fingerprint=authorization_contract_fingerprint,
            credential_state_spec=credential_state_spec,
            max_output_bytes=max_output_bytes,
        )
        if existing.browser_status != "missing":
            return existing
        self.migrate_legacy_archive_once(owner_user_id, profile_id, credential_state, credential_state_spec)
        root, _ = self._profile_directories(owner_user_id, profile_id)
        job_id = self.job_id(owner_user_id, profile_id)
        output_path = root / "jobs" / f"{job_id}.log"
        output_handle = output_path.open("w", encoding="utf-8")
        process = subprocess.Popen(
            [str(executable), *argv[1:]],
            cwd=workspace.expanduser().resolve(strict=True),
            env=self.environment(owner_user_id, profile_id, environment),
            stdout=output_handle,
            stderr=subprocess.STDOUT,
            text=True,
            start_new_session=True,
        )
        job = _HostLarkBrowserJob(
            job_id=job_id,
            owner_user_id=owner_user_id,
            profile_id=profile_id,
            adapter_id=adapter_id,
            authorization_contract_fingerprint=authorization_contract_fingerprint,
            output_path=output_path,
            output_handle=output_handle,
            process=process,
            created_at=time.time(),
        )
        with self._jobs_lock:
            self._jobs[job_id] = job
        deadline = time.monotonic() + max(1.0, wait_for_output_seconds)
        while time.monotonic() < deadline:
            result = self.collect_browser(
                owner_user_id=owner_user_id,
                profile_id=profile_id,
                adapter_id=adapter_id,
                authorization_contract_fingerprint=authorization_contract_fingerprint,
                credential_state_spec=credential_state_spec,
                max_output_bytes=max_output_bytes,
            )
            if result.browser_status != "awaiting_user_browser" or result.output != "<no output>":
                return result
            time.sleep(0.1)
        return self.collect_browser(
            owner_user_id=owner_user_id,
            profile_id=profile_id,
            adapter_id=adapter_id,
            authorization_contract_fingerprint=authorization_contract_fingerprint,
            credential_state_spec=credential_state_spec,
            max_output_bytes=max_output_bytes,
        )

    def collect_browser(
        self,
        *,
        owner_user_id: str,
        profile_id: str,
        adapter_id: str,
        authorization_contract_fingerprint: str,
        credential_state_spec: object | None,
        max_output_bytes: int = 100_000,
    ) -> object:
        from harness.workspace_backends import ManagedProviderExecutionResult

        job_id = self.job_id(owner_user_id, profile_id)
        with self._jobs_lock:
            job = self._jobs.get(job_id)
        if (
            job is None
            or job.owner_user_id != owner_user_id
            or job.profile_id != profile_id
            or job.adapter_id != adapter_id
            or job.authorization_contract_fingerprint != authorization_contract_fingerprint
        ):
            return ManagedProviderExecutionResult(
                output="Managed browser authorization job is missing or expired.",
                exit_code=1,
                credential_state=None,
                browser_status="missing",
                browser_job_id=job_id,
            )
        job.output_handle.flush()
        data = job.output_path.read_bytes() if job.output_path.exists() else b""
        truncated = len(data) > max_output_bytes
        if truncated:
            data = data[-max_output_bytes:]
        output = data.decode("utf-8", errors="replace").strip() or "<no output>"
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
        if exit_code == 0:
            self.retire_legacy_vault(owner_user_id, profile_id)
        return ManagedProviderExecutionResult(
            output=output,
            exit_code=int(exit_code),
            credential_state=(
                self.compatibility_state_marker(credential_state_spec)
                if self.profile_state_present(owner_user_id, profile_id)
                else None
            ),
            truncated=truncated,
            browser_status="completed" if exit_code == 0 else "failed",
            browser_job_id=job_id,
        )

    def finalize_browser(self, owner_user_id: str, profile_id: str, browser_job_id: str) -> bool:
        expected = self.job_id(owner_user_id, profile_id)
        if browser_job_id != expected:
            raise ValueError("browser authorization job identity mismatch")
        with self._jobs_lock:
            job = self._jobs.pop(expected, None)
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

    def list_browser_jobs(self, owner_user_id: str) -> list[dict[str, str]]:
        from runtime_identity.adapters import _LARK_CREDENTIAL_STATE

        with self._jobs_lock:
            jobs = tuple(self._jobs.values())
        return [
            {
                "provider": "lark",
                "profile_id": job.profile_id,
                "browser_job_id": job.job_id,
                "adapter_id": job.adapter_id,
                "authorization_contract_fingerprint": job.authorization_contract_fingerprint,
                "credential_state_fingerprint": _LARK_CREDENTIAL_STATE.fingerprint,
            }
            for job in jobs
            if job.owner_user_id == owner_user_id
        ]
