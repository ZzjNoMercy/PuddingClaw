"""Installation/enablement state and bounded daemon startup."""

from __future__ import annotations

import json
import os
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from runtime_identity.paths import PuddingClawPaths

from .adapter import KimiWebBridgeAdapter, WebBridgeError

DAEMON_PATH = Path.home() / ".kimi-webbridge" / "bin" / "kimi-webbridge"
STATE_FILE_NAME = "kimi-webbridge.json"
START_TIMEOUT_SECONDS = 15.0
STATUS_TIMEOUT_SECONDS = 3.0
STATUS_CACHE_SECONDS = 1.0
UPGRADE_TIMEOUT_SECONDS = 30.0
STUCK_GRACE_SECONDS = 1.0
RECOVERY_COOLDOWN_SECONDS = 30.0

_RECOVERY_LOCK = threading.RLock()
_LAST_RECOVERY_AT: dict[str, float] = {}
_FORCED_RECOVERY_AT: dict[str, float] = {}


@dataclass(frozen=True)
class WebBridgeState:
    enabled: bool
    installed: bool
    daemon_running: bool
    extension_connected: bool
    version: str | None = None
    extension_version: str | None = None
    version_compatible: bool = True
    daemon_pid: int | None = None
    error: str | None = None

    @property
    def ready(self) -> bool:
        return (
            self.enabled
            and self.installed
            and self.daemon_running
            and self.extension_connected
            and self.version_compatible
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "installed": self.installed,
            "daemon_running": self.daemon_running,
            "extension_connected": self.extension_connected,
            "version": self.version,
            "extension_version": self.extension_version,
            "version_compatible": self.version_compatible,
            "daemon_pid": self.daemon_pid,
            "ready": self.ready,
            "error": self.error,
        }


class KimiWebBridgeLifecycle:
    def __init__(
        self,
        paths: PuddingClawPaths,
        *,
        adapter: KimiWebBridgeAdapter | None = None,
        daemon_path: Path = DAEMON_PATH,
    ) -> None:
        self.paths = paths
        self.adapter = adapter or KimiWebBridgeAdapter()
        self.daemon_path = daemon_path
        self._probe_lock = threading.RLock()
        self._cached_state: WebBridgeState | None = None
        self._cached_at = 0.0

    @property
    def state_path(self) -> Path:
        return self.paths.root / "connectors" / STATE_FILE_NAME

    def is_enabled(self) -> bool:
        try:
            payload = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return False
        return bool(isinstance(payload, dict) and payload.get("enabled") is True)

    def set_enabled(self, enabled: bool) -> None:
        path = self.state_path
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(".tmp")
        temporary.write_text(
            json.dumps({"version": 1, "enabled": bool(enabled)}, ensure_ascii=False),
            encoding="utf-8",
        )
        os.replace(temporary, path)
        try:
            path.chmod(0o600)
        except OSError:
            pass
        self._cached_state = None
        self._cached_at = 0.0

    def upgrade_to(self, version: str) -> WebBridgeState:
        """Run the daemon's own versioned upgrade command after UI intent."""

        normalized = str(version or "").strip()
        if not normalized or len(normalized) > 32 or any(char not in "vV0123456789.-" for char in normalized):
            raise ValueError("invalid WebBridge extension version")
        # The daemon's status contract reports extension versions without a
        # prefix, while its CLI upgrade syntax requires the vendor-style `v`.
        if not normalized.lower().startswith("v"):
            normalized = f"v{normalized}"
        if not self.daemon_path.is_file() or not os.access(self.daemon_path, os.X_OK):
            raise FileNotFoundError("WebBridge daemon is not installed")
        try:
            subprocess.run(
                [str(self.daemon_path), "upgrade", normalized],
                check=True,
                capture_output=True,
                text=True,
                timeout=UPGRADE_TIMEOUT_SECONDS,
                env={"PATH": os.environ.get("PATH", "")},
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise WebBridgeError("daemon_upgrade_failed", "WebBridge daemon 升级失败", retryable=False) from exc
        self._cached_state = None
        self._cached_at = 0.0
        return self.probe()

    def _status(self) -> dict[str, Any]:
        """Read the real daemon status contract instead of guessing /health."""

        injected_status = getattr(self.adapter, "status", None)
        if callable(injected_status):
            payload = injected_status()
            if not isinstance(payload, dict):
                raise WebBridgeError("invalid_daemon_status", "WebBridge status 根节点不是对象")
            return payload
        try:
            completed = subprocess.run(
                [str(self.daemon_path), "status"],
                check=False,
                capture_output=True,
                text=True,
                timeout=STATUS_TIMEOUT_SECONDS,
                env={"PATH": os.environ.get("PATH", "")},
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise WebBridgeError("daemon_status_unavailable", "无法读取 WebBridge daemon 状态", retryable=True) from exc
        if completed.returncode != 0:
            raise WebBridgeError("daemon_status_failed", "WebBridge daemon status 命令失败", retryable=True)
        output = (completed.stdout or "").strip()
        try:
            payload = json.loads(output)
        except (TypeError, ValueError) as exc:
            raise WebBridgeError("invalid_daemon_status", "WebBridge status 不是有效 JSON") from exc
        if not isinstance(payload, dict):
            raise WebBridgeError("invalid_daemon_status", "WebBridge status 根节点不是对象")
        return payload

    def probe(self) -> WebBridgeState:
        with self._probe_lock:
            now = time.monotonic()
            if self._cached_state is not None and now - self._cached_at < STATUS_CACHE_SECONDS:
                return self._cached_state
            enabled = self.is_enabled()
            installed = self.daemon_path.is_file() and os.access(self.daemon_path, os.X_OK)
            if not installed:
                state = WebBridgeState(
                    enabled=enabled, installed=False, daemon_running=False, extension_connected=False,
                    error="daemon_not_installed",
                )
            else:
                try:
                    payload = self._status()
                    version_mismatch = bool(payload.get("version_mismatch"))
                    raw_pid = payload.get("pid")
                    daemon_pid = raw_pid if isinstance(raw_pid, int) and not isinstance(raw_pid, bool) and raw_pid > 0 else None
                    state = WebBridgeState(
                        enabled=enabled,
                        installed=True,
                        daemon_running=payload.get("running") is True,
                        extension_connected=payload.get("extension_connected") is True,
                        version=payload.get("version") if isinstance(payload.get("version"), str) else None,
                        extension_version=(
                            payload.get("extension_version")
                            if isinstance(payload.get("extension_version"), str)
                            else None
                        ),
                        version_compatible=not version_mismatch,
                        daemon_pid=daemon_pid,
                        error=(
                            "webbridge_version_mismatch" if version_mismatch
                            else str(payload.get("note"))
                            if payload.get("running") is not True and payload.get("note")
                            else None
                        ),
                    )
                except WebBridgeError as exc:
                    state = WebBridgeState(
                        enabled=enabled, installed=True, daemon_running=False,
                        extension_connected=False, error=exc.code,
                    )
            self._cached_state = state
            self._cached_at = now
            return state

    def ensure_ready(self) -> WebBridgeState:
        recovery_key = str(self.daemon_path)
        state = self.probe()
        forced_recovery = recovery_key in _FORCED_RECOVERY_AT
        if (
            not state.enabled
            or not state.installed
            or (state.ready and not forced_recovery)
            or (state.daemon_running and not forced_recovery)
        ):
            return state

        # BrowserTool instances are short-lived, so recovery coordination must
        # be process-wide rather than tied to one lifecycle object. This also
        # prevents concurrent tool calls from repeatedly restarting the same
        # localhost daemon.
        with _RECOVERY_LOCK:
            self._cached_state = None
            state = self.probe()
            forced_recovery = recovery_key in _FORCED_RECOVERY_AT
            if (
                not state.enabled
                or not state.installed
                or (state.ready and not forced_recovery)
                or (state.daemon_running and not forced_recovery)
            ):
                return state

            # A PID with a failed HTTP probe can be a daemon that is still
            # starting. Give it a short bounded grace period before treating it
            # as stuck and invoking the vendor-supported restart command.
            if state.daemon_pid is not None and STUCK_GRACE_SECONDS > 0:
                grace_deadline = time.monotonic() + STUCK_GRACE_SECONDS
                while time.monotonic() < grace_deadline:
                    time.sleep(0.2)
                    self._cached_state = None
                    state = self.probe()
                    if state.daemon_running and not forced_recovery:
                        return state

            now = time.monotonic()
            if now - _LAST_RECOVERY_AT.get(recovery_key, 0.0) < RECOVERY_COOLDOWN_SECONDS:
                return state
            _LAST_RECOVERY_AT[recovery_key] = now

            status_failed = bool(state.error and str(state.error).startswith("daemon_status"))
            action = "restart" if forced_recovery or state.daemon_pid is not None or status_failed else "start"
            try:
                subprocess.run(
                    [str(self.daemon_path), action],
                    check=True,
                    capture_output=True,
                    text=True,
                    timeout=5,
                    env={"PATH": os.environ.get("PATH", "")},
                )
            except (OSError, subprocess.SubprocessError):
                # The vendor restart command can return before its detached
                # child is healthy (and may itself report a probe failure).
                # Poll the resulting daemon state before declaring recovery
                # failed; do not force the user to clean a PID that the CLI may
                # already have replaced.
                pass

            deadline = time.monotonic() + START_TIMEOUT_SECONDS
            while time.monotonic() < deadline:
                self._cached_state = None
                state = self.probe()
                if state.ready:
                    _FORCED_RECOVERY_AT.pop(recovery_key, None)
                    return state
                if state.daemon_running and not state.version_compatible:
                    _FORCED_RECOVERY_AT.pop(recovery_key, None)
                    return state
                time.sleep(0.2)
            self._cached_state = None
            state = self.probe()
            if state.daemon_running:
                _FORCED_RECOVERY_AT.pop(recovery_key, None)
            return state

    def note_transport_failure(self) -> None:
        """Force one bounded restart before the next browser command.

        CLI status and `/command` can race: a daemon may still report running
        while its command transport is wedged. Recording the transport failure
        closes that gap without replaying the action whose outcome is unknown.
        """

        recovery_key = str(self.daemon_path)
        with _RECOVERY_LOCK:
            _FORCED_RECOVERY_AT[recovery_key] = time.monotonic()
            self._cached_state = None
