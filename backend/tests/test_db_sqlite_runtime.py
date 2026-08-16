"""Tests for the SQLite-default Core catalog runtime wiring in db.py."""

from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

import db
from knowledge.models import KnowledgeImportJob
from schema_migrations import CURRENT_SCHEMA_VERSION


def _reset_db_singletons() -> None:
    db._engine = None
    db._sessionmaker = None
    db._last_error = None
    db._last_schema_version = None


@pytest.fixture
def isolated_db_singleton(monkeypatch):
    # The stock config defaults database.provider to "sqlite"; the CLI
    # deployment contract opts into the Core SQLite catalog explicitly.
    monkeypatch.setenv("PUDDINGCLAW_DATABASE_MODE", "sqlite")
    _reset_db_singletons()
    yield
    _reset_db_singletons()


def test_sqlite_connection_pragmas_are_applied(isolated_db_singleton) -> None:
    async def run() -> None:
        engine = db.get_engine()
        assert db.is_sqlite_url(db.get_database_url())
        async with engine.connect() as connection:
            foreign_keys = await connection.scalar(text("PRAGMA foreign_keys"))
            busy_timeout = await connection.scalar(text("PRAGMA busy_timeout"))
            synchronous = await connection.scalar(text("PRAGMA synchronous"))
        assert int(foreign_keys) == 1
        assert int(busy_timeout) == 10_000
        assert int(synchronous) == 1  # NORMAL
        await engine.dispose()

    asyncio.run(run())


def test_init_database_enables_wal_and_migrates_to_latest(isolated_db_singleton) -> None:
    async def run() -> None:
        ready = await db.init_database()
        assert ready is True

        engine = db.get_engine()
        async with engine.connect() as connection:
            journal_mode = await connection.scalar(text("PRAGMA journal_mode"))
        assert str(journal_mode).lower() == "wal"

        status = db.get_database_status()
        assert status["provider"] == "sqlite"
        assert status["schema_version"] == CURRENT_SCHEMA_VERSION
        assert status["last_error"] is None
        assert status["healthy"] is True
        await engine.dispose()

    asyncio.run(run())


def test_init_database_fails_closed_when_sqlite_is_too_old(isolated_db_singleton, monkeypatch) -> None:
    monkeypatch.setattr(db, "MIN_SQLITE_VERSION", (999, 0, 0))

    async def run() -> None:
        ready = await db.init_database()
        assert ready is False

        status = db.get_database_status()
        last_error = str(status["last_error"] or "")
        assert "SQLite" in last_error
        assert "UPDATE ... RETURNING" in last_error
        assert status["healthy"] is False
        await db.get_engine().dispose()

    asyncio.run(run())


def test_foreign_key_violations_are_rejected(isolated_db_singleton) -> None:
    async def run() -> None:
        ready = await db.init_database()
        assert ready is True

        sessionmaker = db.get_sessionmaker()
        async with sessionmaker() as session:
            session.add(
                KnowledgeImportJob(
                    knowledge_base_id="kb-does-not-exist",
                    status="queued",
                    file_name="orphan.md",
                    source_path="/tmp/orphan.md",
                )
            )
            with pytest.raises(IntegrityError):
                await session.commit()
        await db.get_engine().dispose()

    asyncio.run(run())
