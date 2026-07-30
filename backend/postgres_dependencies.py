"""PostgreSQL server-side dependency inspection for PuddingClaw."""

from __future__ import annotations

import asyncio
import platform
from collections.abc import Mapping
from typing import Any

PGVECTOR_STATUS_SQL = """
SELECT
  current_setting('server_version_num')::integer AS server_version_num,
  EXISTS (
    SELECT 1 FROM pg_available_extensions WHERE name = 'vector'
  ) AS available,
  (
    SELECT default_version FROM pg_available_extensions WHERE name = 'vector'
  ) AS default_version,
  (
    SELECT installed_version FROM pg_available_extensions WHERE name = 'vector'
  ) AS installed_version
"""


def pgvector_install_command(
    server_version_num: int | str | None = None,
    *,
    system: str | None = None,
) -> str:
    """Return a platform-appropriate server extension installation command."""

    try:
        major = max(1, int(server_version_num or 0) // 10000)
    except (TypeError, ValueError):
        major = 16
    platform_name = (system or platform.system()).lower()
    if platform_name == "darwin":
        return "./scripts/start-local-infra.sh"
    if platform_name == "linux":
        return f"sudo apt install postgresql-{major}-pgvector"
    return "Install pgvector for this PostgreSQL server: https://github.com/pgvector/pgvector"


def normalize_pgvector_status(row: Mapping[str, Any]) -> dict[str, Any]:
    server_version_num = int(row.get("server_version_num") or 0)
    available = bool(row.get("available"))
    installed_version = str(row.get("installed_version") or "")
    return {
        "required": True,
        "available": available,
        "installed": bool(installed_version),
        "version": installed_version or str(row.get("default_version") or ""),
        "server_major": server_version_num // 10000 if server_version_num else None,
        "install_command": "" if available else pgvector_install_command(server_version_num),
    }


async def inspect_pgvector_dsn(database_url: str, *, timeout: float = 5.0) -> dict[str, Any]:
    """Inspect whether the target PostgreSQL server ships the vector extension."""

    import asyncpg

    normalized = str(database_url or "").replace("postgresql+asyncpg://", "postgresql://", 1)
    conn = await asyncpg.connect(dsn=normalized, timeout=timeout)
    try:
        row = await conn.fetchrow(PGVECTOR_STATUS_SQL)
        return normalize_pgvector_status(dict(row or {}))
    finally:
        await conn.close()


def inspect_pgvector_dsn_sync(database_url: str, *, timeout: float = 5.0) -> dict[str, Any]:
    """Sync wrapper for worker-thread setup paths such as gbrain init."""

    return asyncio.run(inspect_pgvector_dsn(database_url, timeout=timeout))
