"""Single-instance advisory lease for the Backend's background workers.

Desktop/local mode runs exactly one Backend instance per PuddingClaw Home. The
lease is an OS advisory lock (``flock``) on ``$PUDDINGCLAW_HOME/state/
backend.lease``; it is an operational guardrail that prevents a second Backend
from starting duplicate background consumers, duplicate state pushes and port
confusion. It is NOT a database correctness barrier: queue claim correctness
is guaranteed independently by the CAS/lease protocol in
``knowledge/queue_repository.py``.
"""

from __future__ import annotations

import json
import logging
import os
import socket
from datetime import datetime, timezone
from pathlib import Path

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows
    fcntl = None  # type: ignore[assignment]

logger = logging.getLogger(__name__)

LEASE_FILE_NAME = "backend.lease"


class BackendInstanceLease:
    """Holds the backend.lease advisory lock for the process lifetime."""

    def __init__(self) -> None:
        self._fd: int | None = None
        self.path: Path | None = None
        self.enforced = fcntl is not None
        self.diagnostic = ""

    @property
    def acquired(self) -> bool:
        return self._fd is not None

    def acquire(self, state_dir: Path) -> bool:
        """Try to take the lease. Returns False only when another live Backend
        holds it; on platforms without ``flock`` the guard degrades to
        unenforced (workers still start) with a logged warning."""

        state_dir.mkdir(parents=True, exist_ok=True)
        self.path = state_dir / LEASE_FILE_NAME
        if fcntl is None:
            self.diagnostic = "OS advisory lock (flock) unsupported on this platform; single-instance guard disabled."
            logger.warning("[backend-lease] %s", self.diagnostic)
            return True
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o644)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            try:
                holder = self.path.read_text(encoding="utf-8").strip()
            except OSError:
                holder = ""
            os.close(fd)
            self.diagnostic = (
                f"另一个 Backend 实例已持有 {self.path}"
                + (f"（{holder}）" if holder else "")
                + "；本实例不会启动数据库后台 Worker。请先停止另一个实例。"
            )
            logger.warning("[backend-lease] %s", self.diagnostic)
            return False
        self._fd = fd
        payload = {
            "pid": os.getpid(),
            "host": socket.gethostname(),
            "started_at": datetime.now(timezone.utc).isoformat(),
        }
        os.ftruncate(fd, 0)
        os.lseek(fd, 0, os.SEEK_SET)
        os.write(fd, json.dumps(payload, ensure_ascii=False).encode("utf-8"))
        logger.info("[backend-lease] acquired %s (pid=%s)", self.path, payload["pid"])
        return True

    def release(self) -> None:
        if self._fd is None:
            return
        try:
            if fcntl is not None:
                fcntl.flock(self._fd, fcntl.LOCK_UN)
        except OSError:
            pass
        os.close(self._fd)
        self._fd = None
