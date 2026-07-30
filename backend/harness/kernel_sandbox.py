"""Kernel-backed command runners.

The macOS implementation uses the deprecated-but-still-shipped Seatbelt
``sandbox-exec`` interface. It is guarded by a real startup self-test and must
fail closed when the host no longer enforces the profile.
"""

from __future__ import annotations

import json
import os
import re
import signal
import subprocess
import sys
import tempfile
from collections.abc import Callable
from functools import lru_cache
from pathlib import Path

from deepagents.backends.protocol import ExecuteResponse

from harness.sandbox_profiles import SandboxGrantProfile


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


class MacOSSeatbeltRunner:
    """Execute one command in a process-scoped macOS Seatbelt profile."""

    mode = "kernel_macos_seatbelt"
    executable = Path("/usr/bin/sandbox-exec")
    _SYSTEM_READ_ROOTS = (
        Path("/System"),
        Path("/usr"),
        Path("/bin"),
        Path("/sbin"),
        Path("/Library/Apple"),
        Path("/opt/homebrew"),
        Path("/usr/local"),
        Path("/private/etc"),
        Path("/private/var/select"),
        Path("/private/var/db/timezone"),
        Path("/dev"),
    )

    def __init__(self, profile: SandboxGrantProfile) -> None:
        if sys.platform != "darwin" or not self.executable.is_file():
            raise RuntimeError("macOS Seatbelt sandbox-exec is unavailable")
        self.profile = profile
        runtime = profile.workspace_root / ".puddingclaw" / "runtime" / "kernel"
        self.home = runtime / "home"
        self.tmp = runtime / "tmp"
        self.home.mkdir(parents=True, exist_ok=True)
        self.tmp.mkdir(parents=True, exist_ok=True)

    @classmethod
    @lru_cache(maxsize=1)
    def probe(cls) -> tuple[bool, str]:
        """Run one real allow/deny probe, cached for the Backend process."""

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
        mapped = re.sub(
            r"(^|\s)/workspace(?=(/|\s|$))",
            lambda match: f"{match.group(1)}{self.profile.workspace_root}",
            command,
        )
        return re.sub(
            r"(?<![A-Za-z0-9_./-])/scratch(?=(?:/|\s|$|[\"']))",
            str(self.profile.scratch_root),
            mapped,
        )

    def execute(
        self,
        command: str,
        *,
        timeout: int | None = None,
        spawn_guard: Callable[[], bool] | None = None,
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
        effective_timeout = timeout or self.profile.timeout_seconds
        argv = [
            str(self.executable),
            "-p",
            self.render_profile(),
            "/bin/sh",
            "-c",
            self._map_virtual_paths(command),
        ]
        env = {
            "PATH": "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin",
            "HOME": str(self.home),
            "TMPDIR": str(self.tmp),
            "LANG": "C.UTF-8",
        }
        process = subprocess.Popen(  # noqa: S603
            argv,
            cwd=self.profile.workspace_root,
            env=env,
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
