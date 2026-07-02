"""Async database wiring for PuddingClaw.

PostgreSQL is the intended production catalog database. For local desktop
development and tests, the backend can fall back to a SQLite file when no
DATABASE_URL is configured.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from config import get_database_config

BASE_DIR = Path(__file__).resolve().parent
DEFAULT_SQLITE_URL = f"sqlite+aiosqlite:///{BASE_DIR / 'data' / 'puddingclaw.db'}"

_engine: AsyncEngine | None = None
_sessionmaker: async_sessionmaker[AsyncSession] | None = None
_last_error: str | None = None


def get_database_url() -> str:
    return get_database_config().get("url") or DEFAULT_SQLITE_URL


def _engine_kwargs(url: str) -> dict[str, object]:
    kwargs: dict[str, object] = {
        "pool_pre_ping": True,
        "future": True,
    }
    if url.startswith("sqlite+"):
        kwargs["connect_args"] = {"check_same_thread": False}
    return kwargs


def get_engine() -> AsyncEngine:
    global _engine, _sessionmaker
    if _engine is None:
        url = get_database_url()
        Path(BASE_DIR / "data").mkdir(parents=True, exist_ok=True)
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
        _last_error = None
        return True
    except Exception as exc:
        _last_error = str(exc)
        return False


def get_database_status() -> dict[str, object]:
    url = get_database_url()
    config = get_database_config()
    postgres_configured = bool(config.get("url"))
    provider = "postgresql" if url.startswith("postgresql") else "sqlite" if url.startswith("sqlite") else "unknown"
    safe_url = url
    if "@" in safe_url and "://" in safe_url:
        scheme, rest = safe_url.split("://", 1)
        safe_url = f"{scheme}://***@{rest.split('@', 1)[1]}"
    return {
        "configured": postgres_configured,
        "provider": provider,
        "url": safe_url,
        "configured_by": config.get("configured_by"),
        "environment_override": config.get("environment_override"),
        "mode": config.get("mode"),
        "configuration_hint": (
            ""
            if postgres_configured
            else "PostgreSQL 未配置；请前往 Settings -> 知识库 -> Catalog Database 配置 bundled 或 external DATABASE_URL。"
        ),
        "last_error": _last_error,
        "healthy": postgres_configured and _last_error is None,
    }


async def get_db_session() -> AsyncGenerator[AsyncSession, None]:
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        yield session
