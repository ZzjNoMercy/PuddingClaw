"""Kernel-backed command runners.

The macOS implementation uses the deprecated-but-still-shipped Seatbelt
``sandbox-exec`` interface. It is guarded by a real startup self-test and must
fail closed when the host no longer enforces the profile.
"""

from __future__ import annotations

import ctypes
import errno
import hashlib
import json
import os
import re
import signal
import stat
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from deepagents.backends.protocol import ExecuteResponse

from harness.sandbox_profiles import SandboxGrantProfile


def _trusted_bwrap_path() -> Path | None:
    """Resolve a root-owned, non-user-writable bubblewrap binary."""

    candidates = [
        Path("/usr/bin/bwrap"),
        Path("/usr/local/bin/bwrap"),
        Path("/bin/bwrap"),
    ]
    for candidate in candidates:
        try:
            resolved = candidate.expanduser().resolve(strict=True)
            metadata = resolved.stat()
        except OSError:
            continue
        mode = stat.S_IMODE(metadata.st_mode)
        if (
            resolved.is_file()
            and os.access(resolved, os.X_OK)
            and metadata.st_uid == 0
            and not mode & 0o022
        ):
            return resolved
    return None


def _trusted_python_path() -> Path | None:
    """Return a root-owned, non-user-writable host Python executable."""

    for raw in ("/usr/bin/python3", "/usr/local/bin/python3"):
        candidate = Path(raw)
        try:
            metadata = candidate.stat()
        except OSError:
            continue
        mode = stat.S_IMODE(metadata.st_mode)
        if (
            candidate.is_file()
            and os.access(candidate, os.X_OK)
            and metadata.st_uid == 0
            and not mode & 0o022
        ):
            return candidate
    return None


def _file_digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return "sha256:" + digest.hexdigest()


def _linux_syscalls() -> tuple[int, ...]:
    """Return syscall numbers for the native architecture's escape surface."""

    if sys.platform != "linux":
        return ()
    machine = os.uname().machine.lower()
    if machine in {"x86_64", "amd64"}:
        numbers = {
            "mount": 165,
            "umount2": 166,
            "ptrace": 101,
            "process_vm_readv": 310,
            "process_vm_writev": 311,
            "bpf": 321,
            "perf_event_open": 298,
            "keyctl": 250,
            "add_key": 248,
            "request_key": 249,
            "kexec_load": 246,
            "reboot": 169,
            "init_module": 175,
            "finit_module": 313,
            "delete_module": 176,
            "unshare": 272,
            "setns": 308,
            "userfaultfd": 323,
            "open_by_handle_at": 304,
            "name_to_handle_at": 303,
            "open_tree": 428,
            "move_mount": 429,
            "fsopen": 430,
            "fsconfig": 431,
            "fsmount": 432,
            "fspick": 433,
            "pidfd_getfd": 438,
            "mount_setattr": 442,
            "pivot_root": 155,
            "chroot": 161,
            "swapon": 167,
            "swapoff": 168,
            "acct": 163,
            "io_uring_setup": 425,
            "io_uring_enter": 426,
            "io_uring_register": 427,
            "clone3": 435,
        }
    elif machine in {"aarch64", "arm64"}:
        numbers = {
            "mount": 40,
            "umount2": 39,
            "ptrace": 117,
            "process_vm_readv": 270,
            "process_vm_writev": 271,
            "bpf": 280,
            "perf_event_open": 241,
            "keyctl": 219,
            "add_key": 217,
            "request_key": 218,
            "kexec_load": 104,
            "reboot": 142,
            "init_module": 105,
            "finit_module": 273,
            "delete_module": 106,
            "unshare": 97,
            "setns": 268,
            "userfaultfd": 282,
            "open_by_handle_at": 265,
            "name_to_handle_at": 264,
            "open_tree": 428,
            "move_mount": 429,
            "fsopen": 430,
            "fsconfig": 431,
            "fsmount": 432,
            "fspick": 433,
            "pidfd_getfd": 438,
            "mount_setattr": 442,
            "pivot_root": 41,
            "chroot": 51,
            "swapon": 224,
            "swapoff": 225,
            "acct": 89,
            "io_uring_setup": 425,
            "io_uring_enter": 426,
            "io_uring_register": 427,
            "clone3": 435,
        }
    else:
        return ()
    return tuple(sorted(set(numbers.values())))


class _BpfInsn(ctypes.Structure):
    _fields_ = [
        ("code", ctypes.c_ushort),
        ("jt", ctypes.c_ubyte),
        ("jf", ctypes.c_ubyte),
        ("k", ctypes.c_uint32),
    ]


def _seccomp_filter_bytes() -> bytes:
    """Build a small classic-BPF filter consumed by bubblewrap.

    The launcher performs namespace setup before installing this filter. The
    command cannot directly call mount, unshare or setns, attach to other
    processes, access kernel control planes, or use io_uring. Parameter-level
    clone filtering and nested-userns proof remain release-gate work.
    """

    # Linux UAPI constants: BPF_LD|W|ABS, BPF_JMP|JEQ|K, BPF_RET|K.
    bpf_ld_abs = 0x20
    bpf_jmp_jeq = 0x15
    bpf_ret_k = 0x06
    seccomp_allow = 0x7FFF0000
    seccomp_errno = 0x00050000 | errno.EPERM
    seccomp_kill_process = 0x80000000
    audit_arch = {
        "x86_64": 0xC000003E,
        "amd64": 0xC000003E,
        "aarch64": 0xC00000B7,
        "arm64": 0xC00000B7,
    }.get(os.uname().machine.lower())
    syscalls = _linux_syscalls()
    if audit_arch is None or not syscalls:
        raise RuntimeError("Unsupported Linux architecture for seccomp profile")

    instructions = [
        _BpfInsn(bpf_ld_abs, 0, 0, 4),
        _BpfInsn(bpf_jmp_jeq, 1, 0, audit_arch),
        _BpfInsn(bpf_ret_k, 0, 0, seccomp_kill_process),
        _BpfInsn(bpf_ld_abs, 0, 0, 0),
    ]
    for syscall in syscalls:
        instructions.extend(
            (
                _BpfInsn(bpf_jmp_jeq, 0, 1, syscall),
                _BpfInsn(bpf_ret_k, 0, 0, seccomp_errno),
            )
        )
    instructions.append(_BpfInsn(bpf_ret_k, 0, 0, seccomp_allow))
    array_type = _BpfInsn * len(instructions)
    return bytes(array_type(*instructions))


def _write_seccomp_fd() -> int:
    """Create an inherited descriptor containing the BPF program."""

    read_fd, write_fd = os.pipe()
    try:
        os.write(write_fd, _seccomp_filter_bytes())
    finally:
        os.close(write_fd)
    return read_fd


def _safe_environment(
    environment: Mapping[str, str] | None,
    *,
    home: Path,
    tmp: Path,
) -> dict[str, str]:
    denied_names = {
        "BASH_ENV",
        "ENV",
        "GCONV_PATH",
        "IFS",
        "NODE_OPTIONS",
        "PERL5OPT",
        "PYTHONINSPECT",
        "PYTHONPATH",
        "PYTHONSTARTUP",
        "RUBYOPT",
        "SSLKEYLOGFILE",
    }
    env: dict[str, str] = {
        "PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
        "HOME": str(home),
        "TMPDIR": str(tmp),
        "LANG": "C.UTF-8",
        "PYTHONNOUSERSITE": "1",
    }
    for key, value in (environment or {}).items():
        normalized_key = str(key)
        normalized_value = str(value)
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", normalized_key):
            raise ValueError("Kernel environment contains an invalid variable name")
        if (
            normalized_key in denied_names
            or normalized_key.startswith(("LD_", "DYLD_"))
        ):
            raise ValueError(f"Kernel environment variable is not allowed: {normalized_key}")
        if "\x00" in normalized_value:
            raise ValueError("Kernel environment contains a NUL byte")
        env[normalized_key] = normalized_value
    return env


def _bounded_output(stdout: str, stderr: str, *, limit: int) -> tuple[str, bool]:
    parts = [stdout] if stdout else []
    if stderr:
        parts.extend(f"[stderr] {line}" for line in stderr.strip().splitlines())
    output = "\n".join(parts) if parts else "<no output>"
    encoded = output.encode("utf-8", errors="replace")
    if len(encoded) <= limit:
        return output, False
    return (
        encoded[:limit].decode("utf-8", errors="ignore") + f"\n\n... Output truncated at {limit} bytes.",
        True,
    )


def _validated_working_directory(
    profile: SandboxGrantProfile,
    cwd: Path | None,
) -> Path:
    """Resolve a process cwd only inside the profile's explicit read roots."""

    candidate = profile.workspace_root if cwd is None else Path(cwd).expanduser()
    if not candidate.is_absolute():
        raise ValueError("Kernel working directory must be absolute")
    if candidate.is_symlink() or not candidate.is_dir():
        raise ValueError("Kernel working directory must be an existing non-symlink directory")
    canonical = candidate.resolve(strict=True)
    if canonical != candidate:
        raise ValueError("Kernel working directory must already be canonical")
    if any(
        canonical == denied or denied in canonical.parents
        for denied in profile.deny_roots
    ):
        raise ValueError("Kernel working directory is explicitly denied")
    if not any(
        canonical == root or root in canonical.parents
        for root in profile.read_roots
    ):
        raise ValueError("Kernel working directory is outside the execution profile")
    return canonical


def _map_kernel_virtual_paths(
    command: str,
    *,
    profile: SandboxGrantProfile,
) -> str:
    """Give Kernel commands the same /workspace, /scratch, and /tmp surface."""

    tmp = profile.scratch_root / "tmp"
    mapped = command
    for virtual, target in (
        ("workspace", profile.workspace_root),
        ("scratch", profile.scratch_root),
        ("tmp", tmp),
    ):
        mapped = re.sub(
            rf"(?<![A-Za-z0-9_./-])/{virtual}(?=(?:/|\s|$|[\"']))",
            str(target),
            mapped,
        )
    return mapped


class MacOSSeatbeltRunner:
    """Execute one command in a process-scoped macOS Seatbelt profile."""

    mode = "kernel_macos_seatbelt"
    executable = Path("/usr/bin/sandbox-exec")
    _PROBE_FAILURE_TTL_SECONDS = 5.0
    _probe_cache: tuple[float, tuple[bool, str]] | None = None
    _probe_lock = threading.Lock()
    _SYSTEM_READ_ROOTS = (
        Path("/System"),
        Path("/usr"),
        Path("/bin"),
        Path("/sbin"),
        Path("/Library/Apple"),
        Path("/Applications"),
        Path("/opt/homebrew"),
        Path("/usr/local"),
        Path("/private/etc"),
        Path("/private/var/select"),
        Path("/private/var/db/timezone"),
        Path("/dev"),
    )

    def __init__(
        self,
        profile: SandboxGrantProfile,
        *,
        runtime_root: Path | None = None,
    ) -> None:
        if sys.platform != "darwin" or not self.executable.is_file():
            raise RuntimeError("macOS Seatbelt sandbox-exec is unavailable")
        self.profile = profile
        runtime = (
            runtime_root.expanduser().resolve()
            if runtime_root is not None
            else profile.workspace_root / ".puddingclaw" / "runtime" / "kernel"
        )
        if runtime.is_symlink():
            raise ValueError("Seatbelt runtime root must not be a symlink")
        self.home = runtime / "home"
        self.tmp = profile.scratch_root / "tmp"
        self.home.mkdir(parents=True, exist_ok=True)
        self.tmp.mkdir(parents=True, exist_ok=True)

    @classmethod
    def binding_digest(cls) -> str:
        payload = {
            "mode": cls.mode,
            "executable": str(cls.executable),
            "executable_digest": _file_digest(cls.executable) if cls.executable.is_file() else "missing",
            "policy_schema": "macos-seatbelt-profile-v2",
        }
        return "sha256:" + hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()

    @classmethod
    def probe(cls) -> tuple[bool, str]:
        """Run a real allow/deny probe without pinning transient failures.

        Successful enforcement is stable for the lifetime of the Backend
        process and can be cached indefinitely.  A failed startup probe may be
        caused by a short-lived host condition, so retain it only briefly and
        retry on a later Run instead of silently disabling the kernel backend
        until Uvicorn is restarted.
        """

        now = time.monotonic()
        cached = cls._probe_cache
        if cached is not None:
            cached_at, result = cached
            if result[0] or now - cached_at < cls._PROBE_FAILURE_TTL_SECONDS:
                return result

        with cls._probe_lock:
            now = time.monotonic()
            cached = cls._probe_cache
            if cached is not None:
                cached_at, result = cached
                if result[0] or now - cached_at < cls._PROBE_FAILURE_TTL_SECONDS:
                    return result
            result = cls._probe_once()
            cls._probe_cache = (now, result)
            return result

    @classmethod
    def _probe_once(cls) -> tuple[bool, str]:
        """Perform one uncached Seatbelt enforcement probe."""

        if sys.platform != "darwin" or not cls.executable.is_file():
            return False, "macOS sandbox-exec is unavailable"
        try:
            with tempfile.TemporaryDirectory(prefix="puddingclaw-seatbelt-probe-") as raw:
                # macOS commonly exposes TMPDIR through the /var -> /private/var
                # symlink. Grant Profiles intentionally require canonical
                # roots, so canonicalize the probe root before creating them.
                root = Path(raw).resolve()
                workspace = root / "workspace"
                scratch = root / "scratch"
                workspace.mkdir()
                scratch.mkdir()
                secret = root / "must-not-read.txt"
                marker = "PUDDINGCLAW_SEATBELT_DENY_PROBE"
                secret.write_text(marker, encoding="utf-8")
                runner = cls(
                    SandboxGrantProfile.build(
                        workspace_root=workspace,
                        scratch_root=scratch,
                        timeout_seconds=3,
                    )
                )
                allowed = runner.execute("true")
                denied = runner.execute(f"cat {secret}")
                if allowed.exit_code != 0:
                    return False, f"kernel allow probe failed: {allowed.output}"
                if denied.exit_code == 0 or marker in denied.output:
                    return False, "kernel deny probe did not enforce the filesystem boundary"
        except Exception as exc:  # noqa: BLE001
            return False, f"kernel sandbox probe failed ({type(exc).__name__}): {exc}"
        return True, "macOS Seatbelt allow/deny probe passed"

    @staticmethod
    def _literal(path: Path) -> str:
        return json.dumps(str(path))

    def render_profile(self) -> str:
        read_roots = tuple(dict.fromkeys((*self._SYSTEM_READ_ROOTS, *self.profile.read_roots)))
        lines = [
            "(version 1)",
            "(deny default)",
            '(import "system.sb")',
            "(allow process*)",
        ]
        for root in read_roots:
            if root.exists():
                lines.append(f"(allow file-read-metadata file-test-existence (path-ancestors {self._literal(root)}))")
                lines.append(f"(allow file-read* (subpath {self._literal(root)}))")
        for root in self.profile.write_roots:
            lines.append(f"(allow file-write* (subpath {self._literal(root)}))")
        for root in sorted(self.profile.deny_roots, key=lambda path: (-len(path.parts), str(path))):
            lines.append(
                f"(deny file-read-metadata file-read* file-write* (subpath {self._literal(root)}))"
            )
        lines.extend(
            (
                '(allow file-write-data (literal "/dev/null"))',
                '(allow file-write-data (literal "/dev/zero"))',
            )
        )
        if self.profile.network_allowed:
            lines.append("(allow network*)")
        return "\n".join(lines)

    def _map_virtual_paths(self, command: str) -> str:
        return _map_kernel_virtual_paths(
            command,
            profile=self.profile,
        )

    def _execution_environment(
        self,
        environment: Mapping[str, str] | None,
    ) -> dict[str, str]:
        env: dict[str, str] = {
            "PATH": "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin",
            "HOME": str(self.home),
            "TMPDIR": str(self.tmp),
            "LANG": "C.UTF-8",
        }
        for key, value in (environment or {}).items():
            normalized_key = str(key)
            normalized_value = str(value)
            if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", normalized_key):
                raise ValueError("Seatbelt environment contains an invalid variable name")
            if "\x00" in normalized_value:
                raise ValueError("Seatbelt environment contains a NUL byte")
            env[normalized_key] = normalized_value
        return env

    @staticmethod
    def _direct_argv(argv: list[str] | tuple[str, ...]) -> list[str]:
        values = [str(value) for value in argv]
        if (
            not values
            or not Path(values[0]).is_absolute()
            or any(not value or "\x00" in value for value in values)
        ):
            raise ValueError("Seatbelt direct argv is invalid")
        return values

    def execute(
        self,
        command: str,
        *,
        timeout: int | None = None,
        spawn_guard: Callable[[], bool] | None = None,
        environment: Mapping[str, str] | None = None,
        cwd: Path | None = None,
        input_text: str | None = None,
    ) -> ExecuteResponse:
        if not isinstance(command, str) or not command.strip():
            return ExecuteResponse(output="Error: Command must be non-empty.", exit_code=1)
        if not self.profile.valid_at_spawn():
            return ExecuteResponse(
                output="Error: Sandbox profile roots changed before process spawn.",
                exit_code=126,
            )
        if spawn_guard is not None and not spawn_guard():
            return ExecuteResponse(
                output="Error: Execution permit became invalid before process spawn.",
                exit_code=126,
            )
        working_directory = _validated_working_directory(self.profile, cwd)
        effective_timeout = timeout or self.profile.timeout_seconds
        argv = [
            str(self.executable),
            "-p",
            self.render_profile(),
            "/bin/sh",
            "-c",
            self._map_virtual_paths(command),
        ]
        env = self._execution_environment(environment)
        process = subprocess.Popen(  # noqa: S603
            argv,
            cwd=working_directory,
            env=env,
            stdin=subprocess.PIPE if input_text is not None else subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
            close_fds=True,
        )
        try:
            stdout, stderr = process.communicate(input=input_text, timeout=effective_timeout)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            stdout, stderr = process.communicate()
            output, truncated = _bounded_output(
                stdout,
                stderr,
                limit=self.profile.max_output_bytes,
            )
            return ExecuteResponse(
                output=f"{output}\n\nError: Command timed out after {effective_timeout} seconds.",
                exit_code=124,
                truncated=truncated,
            )
        output, truncated = _bounded_output(
            stdout,
            stderr,
            limit=self.profile.max_output_bytes,
        )
        if process.returncode:
            output = f"{output.rstrip()}\n\nExit code: {process.returncode}"
        return ExecuteResponse(
            output=output,
            exit_code=int(process.returncode or 0),
            truncated=truncated,
        )

    def execute_argv(
        self,
        argv: list[str] | tuple[str, ...],
        *,
        timeout: int | None = None,
        environment: Mapping[str, str] | None = None,
        cwd: Path | None = None,
    ) -> ExecuteResponse:
        """Execute already-normalized argv without another shell parse."""

        if not self.profile.valid_at_spawn():
            return ExecuteResponse(
                output="Error: Sandbox profile roots changed before process spawn.",
                exit_code=126,
            )
        working_directory = _validated_working_directory(self.profile, cwd)
        effective_timeout = timeout or self.profile.timeout_seconds
        process = subprocess.Popen(  # noqa: S603
            [str(self.executable), "-p", self.render_profile(), *self._direct_argv(argv)],
            cwd=working_directory,
            env=self._execution_environment(environment),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
            close_fds=True,
        )
        try:
            stdout, stderr = process.communicate(timeout=effective_timeout)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            stdout, stderr = process.communicate()
            output, truncated = _bounded_output(
                stdout,
                stderr,
                limit=self.profile.max_output_bytes,
            )
            return ExecuteResponse(
                output=f"{output}\n\nError: Command timed out after {effective_timeout} seconds.",
                exit_code=124,
                truncated=truncated,
            )
        output, truncated = _bounded_output(
            stdout,
            stderr,
            limit=self.profile.max_output_bytes,
        )
        if process.returncode:
            output = f"{output.rstrip()}\n\nExit code: {process.returncode}"
        return ExecuteResponse(
            output=output,
            exit_code=int(process.returncode or 0),
            truncated=truncated,
        )

    def start_background(
        self,
        command: str,
        *,
        environment: Mapping[str, str] | None,
        cwd: Path,
        output: Any,
    ) -> subprocess.Popen[str]:
        """Start one long-lived command under the same immutable Seatbelt profile."""

        if not isinstance(command, str) or not command.strip() or not self.profile.valid_at_spawn():
            raise ValueError("Seatbelt background command or profile is invalid")
        working_directory = _validated_working_directory(self.profile, cwd)
        argv = [
            str(self.executable),
            "-p",
            self.render_profile(),
            "/bin/sh",
            "-c",
            self._map_virtual_paths(command),
        ]
        env = self._execution_environment(environment)
        return subprocess.Popen(  # noqa: S603
            argv,
            cwd=working_directory,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=output,
            stderr=subprocess.STDOUT,
            text=True,
            start_new_session=True,
            close_fds=True,
        )

    def start_background_argv(
        self,
        argv: list[str] | tuple[str, ...],
        *,
        environment: Mapping[str, str] | None,
        cwd: Path,
        output: Any,
    ) -> subprocess.Popen[str]:
        """Start normalized argv in Seatbelt without a shell intermediary."""

        if not self.profile.valid_at_spawn():
            raise ValueError("Seatbelt background profile is invalid")
        return subprocess.Popen(  # noqa: S603
            [str(self.executable), "-p", self.render_profile(), *self._direct_argv(argv)],
            cwd=_validated_working_directory(self.profile, cwd),
            env=self._execution_environment(environment),
            stdin=subprocess.DEVNULL,
            stdout=output,
            stderr=subprocess.STDOUT,
            text=True,
            start_new_session=True,
            close_fds=True,
        )


class LinuxBwrapSeccompRunner:
    """Execute a command in a bubblewrap Linux kernel sandbox.

    The host filesystem is not exposed wholesale: only the system runtime and
    roots in the immutable Grant Profile are mounted.  Bubblewrap creates the
    namespaces, then a classic seccomp filter and ``PR_SET_NO_NEW_PRIVS`` are
    applied before the user's shell is exec'd. The current Python launcher is
    deliberately treated as a development implementation; a native helper is
    still required before claiming a hostile-code threat model.
    """

    mode = "kernel_linux_bwrap_seccomp"
    _PROBE_FAILURE_TTL_SECONDS = 5.0
    _probe_cache: tuple[float, tuple[bool, str]] | None = None
    _probe_lock = threading.Lock()
    _SYSTEM_ROOTS = ("/usr", "/bin", "/sbin", "/lib", "/lib64", "/usr/local", "/etc")
    _HIDDEN_ROOTS = ("/home", "/root", "/run", "/tmp", "/var/tmp", "/mnt", "/media", "/srv", "/opt")

    def __init__(
        self,
        profile: SandboxGrantProfile,
        *,
        runtime_root: Path | None = None,
    ) -> None:
        if sys.platform != "linux":
            raise RuntimeError("Linux bubblewrap sandbox is unavailable on this host")
        executable = _trusted_bwrap_path()
        python = _trusted_python_path()
        if executable is None or python is None:
            raise RuntimeError("Linux bubblewrap or trusted host Python is unavailable")
        self.profile = profile
        self.executable = executable
        self.python = python
        runtime = (
            runtime_root.expanduser().resolve()
            if runtime_root is not None
            else profile.scratch_root / ".kernel-runtime"
        )
        if runtime.is_symlink():
            raise ValueError("Linux sandbox runtime root must not be a symlink")
        runtime.mkdir(parents=True, exist_ok=True)
        self.home = runtime / "home"
        self.tmp = profile.scratch_root / "tmp"
        self.home.mkdir(parents=True, exist_ok=True)
        self.tmp.mkdir(parents=True, exist_ok=True)

    @classmethod
    def binding_digest(cls) -> str:
        bwrap = _trusted_bwrap_path()
        python = _trusted_python_path()
        payload = {
            "mode": cls.mode,
            "bwrap": str(bwrap) if bwrap else "missing",
            "bwrap_digest": _file_digest(bwrap) if bwrap else "missing",
            "python": str(python) if python else "missing",
            "python_digest": _file_digest(python) if python else "missing",
            "seccomp_policy_digest": "sha256:" + hashlib.sha256(_seccomp_filter_bytes()).hexdigest()
            if sys.platform == "linux"
            else "unsupported",
            "mount_policy": "minimal-system-roots-v2",
            "namespace_policy": "user-pid-ipc-uts-cgroup-net-v2",
        }
        return "sha256:" + hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()

    @classmethod
    def probe(cls) -> tuple[bool, str]:
        now = time.monotonic()
        cached = cls._probe_cache
        if cached is not None:
            cached_at, result = cached
            if result[0] or now - cached_at < cls._PROBE_FAILURE_TTL_SECONDS:
                return result
        with cls._probe_lock:
            now = time.monotonic()
            cached = cls._probe_cache
            if cached is not None:
                cached_at, result = cached
                if result[0] or now - cached_at < cls._PROBE_FAILURE_TTL_SECONDS:
                    return result
            result = cls._probe_once()
            cls._probe_cache = (now, result)
            return result

    @classmethod
    def _probe_once(cls) -> tuple[bool, str]:
        if sys.platform != "linux":
            return False, "Linux bubblewrap is unavailable on this host"
        if _trusted_bwrap_path() is None:
            return False, "bubblewrap executable is unavailable"
        if _trusted_python_path() is None:
            return False, "trusted host Python is unavailable"
        try:
            with tempfile.TemporaryDirectory(prefix="puddingclaw-bwrap-probe-") as raw:
                root = Path(raw).resolve()
                workspace = root / "workspace"
                scratch = root / "scratch"
                secret_root = root / "secret"
                workspace.mkdir()
                scratch.mkdir()
                secret_root.mkdir()
                secret = secret_root / "must-not-read.txt"
                marker = "PUDDINGCLAW_BWRAP_DENY_PROBE"
                secret.write_text(marker, encoding="utf-8")
                runner = cls(
                    SandboxGrantProfile.build(
                        workspace_root=workspace,
                        scratch_root=scratch,
                        timeout_seconds=3,
                    )
                )
                allowed = runner.execute("printf probe > /workspace/allowed.txt")
                denied = runner.execute(f"cat {secret}")
                no_new_privs = runner.execute("grep '^NoNewPrivs:[[:space:]]*1$' /proc/self/status")
                if allowed.exit_code != 0:
                    return False, f"bubblewrap allow probe failed: {allowed.output}"
                if not (workspace / "allowed.txt").is_file():
                    return False, "bubblewrap writable bind probe did not persist"
                if denied.exit_code == 0 or marker in denied.output:
                    return False, "bubblewrap deny probe did not enforce the filesystem boundary"
                if no_new_privs.exit_code != 0:
                    return False, "bubblewrap no_new_privs probe failed"
        except Exception as exc:  # noqa: BLE001
            return False, f"bubblewrap probe failed ({type(exc).__name__}): {exc}"
        return True, "Linux bubblewrap namespace/seccomp allow/deny probe passed"

    @staticmethod
    def _ensure_mountpoint(
        argv: list[str],
        path: Path,
        *,
        known: set[Path] | None = None,
    ) -> None:
        """Create destination parents after hiding host-owned directories."""

        parts = path.parts
        current = Path(parts[0]) if parts else Path("/")
        for part in parts[1:]:
            current /= part
            if known is not None and current in known:
                continue
            argv.extend(("--dir", str(current)))
            if known is not None:
                known.add(current)

    def _mount_args(self) -> list[str]:
        argv: list[str] = []
        known_mountpoints: set[Path] = set()
        for raw_root in self._SYSTEM_ROOTS:
            root = Path(raw_root)
            if not root.exists():
                continue
            self._ensure_mountpoint(argv, root, known=known_mountpoints)
            argv.extend(("--ro-bind", str(root), str(root)))
        for root in self._HIDDEN_ROOTS:
            self._ensure_mountpoint(argv, Path(root), known=known_mountpoints)
            argv.extend(("--tmpfs", root))
        self._ensure_mountpoint(argv, Path("/proc"), known=known_mountpoints)
        self._ensure_mountpoint(argv, Path("/dev"), known=known_mountpoints)
        argv.extend(("--proc", "/proc", "--dev", "/dev"))
        # Explicitly remount common control paths as empty, not host-backed.
        self._ensure_mountpoint(argv, Path("/sys"), known=known_mountpoints)
        argv.extend(("--tmpfs", "/sys"))

        roots = set(self.profile.read_roots) | set(self.profile.write_roots)
        roots = {
            root
            for root in roots
            if root not in {self.profile.workspace_root, self.profile.scratch_root}
        }
        ordered = [self.profile.workspace_root, self.profile.scratch_root]
        ordered.extend(sorted(roots, key=lambda path: (len(path.parts), str(path))))
        for root in ordered:
            self._ensure_mountpoint(argv, root, known=known_mountpoints)
            writable = root in self.profile.write_roots or root == self.profile.scratch_root
            argv.extend(("--bind" if writable else "--ro-bind", str(root), str(root)))
        for root in sorted(self.profile.deny_roots, key=lambda path: (-len(path.parts), str(path))):
            self._ensure_mountpoint(argv, root, known=known_mountpoints)
            argv.extend(("--tmpfs", str(root)))
        return argv

    def _argv(self, command: str, *, seccomp_fd: int, cwd: Path) -> list[str]:
        command = _map_kernel_virtual_paths(
            command,
            profile=self.profile,
        )
        argv = [
            str(self.executable),
            "--die-with-parent",
            "--new-session",
            "--unshare-user",
            "--unshare-pid",
            "--unshare-ipc",
            "--unshare-uts",
            "--unshare-cgroup-try",
            "--disable-userns",
            "--cap-drop",
            "ALL",
            "--seccomp",
            str(seccomp_fd),
        ]
        if not self.profile.network_allowed:
            argv.append("--unshare-net")
        argv.extend(self._mount_args())
        argv.extend(
            (
                "--chdir",
                str(cwd),
                str(self.python),
                "-S",
                "-c",
                (
                    f"import ctypes,os,resource,sys; "
                    f"resource.setrlimit(resource.RLIMIT_NPROC,({self.profile.max_processes},{self.profile.max_processes})); "
                    "libc=ctypes.CDLL(None,use_errno=True); "
                    "rc=libc.prctl(38,1,0,0,0); "
                    "raise SystemExit(rc) if rc != 0 else "
                    "os.execve('/bin/sh',['sh','-c',sys.argv[1]],os.environ)"
                ),
                "puddingclaw-command",
                command,
            )
        )
        return argv

    def execute(
        self,
        command: str,
        *,
        timeout: int | None = None,
        spawn_guard: Callable[[], bool] | None = None,
        environment: Mapping[str, str] | None = None,
        cwd: Path | None = None,
    ) -> ExecuteResponse:
        if not isinstance(command, str) or not command.strip():
            return ExecuteResponse(output="Error: Command must be non-empty.", exit_code=1)
        if not self.profile.valid_at_spawn():
            return ExecuteResponse(
                output="Error: Sandbox profile roots changed before process spawn.",
                exit_code=126,
            )
        if spawn_guard is not None and not spawn_guard():
            return ExecuteResponse(
                output="Error: Execution permit became invalid before process spawn.",
                exit_code=126,
            )
        working_directory = _validated_working_directory(self.profile, cwd)
        effective_timeout = timeout or self.profile.timeout_seconds
        seccomp_fd = _write_seccomp_fd()
        env = _safe_environment(environment, home=self.home, tmp=self.tmp)
        python_home = env.get("PYTHONHOME")
        if python_home:
            try:
                python_home_path = Path(python_home).expanduser().resolve(strict=True)
                if not any(
                    python_home_path == root or root in python_home_path.parents
                    for root in self.profile.read_roots
                ):
                    raise ValueError("PYTHONHOME must stay inside an authorized read root")
            except OSError as exc:
                raise ValueError("PYTHONHOME must resolve to an authorized read root") from exc
        try:
            process = subprocess.Popen(  # noqa: S603
                self._argv(command, seccomp_fd=seccomp_fd, cwd=working_directory),
                # Keep the launcher cwd aligned with bubblewrap's inner cwd.
                # The latter is independently checked by the profile mounts.
                cwd=working_directory,
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                start_new_session=True,
                close_fds=True,
                pass_fds=(seccomp_fd,),
            )
        finally:
            os.close(seccomp_fd)
        try:
            stdout, stderr = process.communicate(timeout=effective_timeout)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            stdout, stderr = process.communicate()
            output, truncated = _bounded_output(stdout, stderr, limit=self.profile.max_output_bytes)
            return ExecuteResponse(
                output=f"{output}\n\nError: Command timed out after {effective_timeout} seconds.",
                exit_code=124,
                truncated=truncated,
            )
        output, truncated = _bounded_output(stdout, stderr, limit=self.profile.max_output_bytes)
        if process.returncode:
            output = f"{output.rstrip()}\n\nExit code: {process.returncode}"
        return ExecuteResponse(
            output=output,
            exit_code=int(process.returncode or 0),
            truncated=truncated,
        )


def kernel_runner_for_profile(
    profile: SandboxGrantProfile,
    *,
    runtime_root: Path | None = None,
) -> MacOSSeatbeltRunner | LinuxBwrapSeccompRunner:
    if sys.platform == "darwin":
        return MacOSSeatbeltRunner(profile, runtime_root=runtime_root)
    if sys.platform == "linux":
        return LinuxBwrapSeccompRunner(profile, runtime_root=runtime_root)
    raise RuntimeError(f"No supported kernel sandbox runner for platform {sys.platform}")


def kernel_runner_binding_digest() -> str:
    if sys.platform == "darwin":
        return MacOSSeatbeltRunner.binding_digest()
    if sys.platform == "linux":
        return LinuxBwrapSeccompRunner.binding_digest()
    raise RuntimeError(f"No supported kernel sandbox runner for platform {sys.platform}")
