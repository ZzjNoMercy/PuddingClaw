"""Async database wiring for PuddingClaw.

PostgreSQL is the intended production catalog database. For local desktop
development and tests, the backend can fall back to a SQLite file when no
DATABASE_URL is configured.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from config import get_database_config
from runtime_identity.paths import PuddingClawPaths

BASE_DIR = Path(__file__).resolve().parent
DEFAULT_SQLITE_URL = ""

_engine: AsyncEngine | None = None
_sessionmaker: async_sessionmaker[AsyncSession] | None = None
_last_error: str | None = None


def get_database_url() -> str:
    configured = get_database_config().get("url") or DEFAULT_SQLITE_URL
    if configured:
        return configured
    return f"sqlite+aiosqlite:///{PuddingClawPaths.from_environment().databases() / 'catalog.sqlite3'}"


def _engine_kwargs(url: str) -> dict[str, object]:
    kwargs: dict[str, object] = {
        "pool_pre_ping": True,
        "future": True,
    }
    if url.startswith("sqlite+"):
        kwargs["connect_args"] = {"check_same_thread": False}
    elif url.startswith("postgresql+asyncpg"):
        kwargs["poolclass"] = NullPool
    return kwargs


def get_engine() -> AsyncEngine:
    global _engine, _sessionmaker
    if _engine is None:
        url = get_database_url()
        PuddingClawPaths.from_environment().databases().mkdir(parents=True, exist_ok=True)
        _engine = create_async_engine(url, **_engine_kwargs(url))
        _sessionmaker = async_sessionmaker(_engine, expire_on_commit=False)
    return _engine


def get_sessionmaker() -> async_sessionmaker[AsyncSession]:
    if _sessionmaker is None:
        get_engine()
    assert _sessionmaker is not None
    return _sessionmaker


async def init_database() -> bool:
    """Create MVP tables if the configured database is reachable.

    Alembic migrations are the target long-term path. The first development
    slice uses `create_all` so the feature is immediately runnable.
    """

    global _last_error
    try:
        from knowledge.models import Base

        engine = get_engine()
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        from analytics.nl2sql.result_store import (
            backfill_query_result_catalogs,
            cleanup_expired_query_results,
            scavenge_orphaned_query_result_files,
        )

        sessionmaker = get_sessionmaker()
        async with sessionmaker() as session:
            await backfill_query_result_catalogs(session)
            await cleanup_expired_query_results(session)
            await scavenge_orphaned_query_result_files(session)
        _last_error = None
        return True
    except Exception as exc:
        _last_error = str(exc)
        return False


def get_database_status() -> dict[str, object]:
    url = get_database_url()
    config = get_database_config()
    provider = "postgresql" if url.startswith("postgresql") else "sqlite" if url.startswith("sqlite") else "unknown"
    configured = provider == "sqlite" or bool(config.get("url"))
    safe_url = url
    if "@" in safe_url and "://" in safe_url:
        scheme, rest = safe_url.split("://", 1)
        safe_url = f"{scheme}://***@{rest.split('@', 1)[1]}"
    return {
        "configured": configured,
        "provider": provider,
        "url": safe_url,
        "configured_by": config.get("configured_by"),
        "environment_override": config.get("environment_override"),
        "mode": config.get("mode"),
        "configuration_hint": (
            ""
            if configured
            else "PostgreSQL 未配置；请前往 Settings -> 数据库配置共享数据库连接。"
        ),
        "last_error": _last_error,
        "healthy": configured and _last_error is None,
    }


async def get_db_session() -> AsyncGenerator[AsyncSession, None]:
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        yield session
