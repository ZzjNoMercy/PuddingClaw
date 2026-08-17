from __future__ import annotations

import asyncio

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from knowledge import database_sources
from knowledge.database_sources import KnowledgeDatabaseSourceError
from knowledge.models import Base, KnowledgeBase, KnowledgeDatabaseSource
from knowledge.service import DEFAULT_KNOWLEDGE_BASE_ID


def test_sqlite_core_hides_stale_project_postgres_source(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    async def run() -> None:
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'catalog.db'}")
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        sessions = async_sessionmaker(engine, expire_on_commit=False)

        async with sessions() as session:
            session.add(KnowledgeBase(id=DEFAULT_KNOWLEDGE_BASE_ID, name="Default", description=""))
            session.add_all(
                [
                    KnowledgeDatabaseSource(
                        id="project_postgres",
                        knowledge_base_id=DEFAULT_KNOWLEDGE_BASE_ID,
                        source_type="postgresql",
                        name="PuddingClaw PostgreSQL",
                        description="legacy built-in metadata",
                        host="127.0.0.1",
                        port=5432,
                        database="puddingclaw",
                        username="puddingclaw",
                        selected_tables=[],
                        source_metadata={"builtin": True},
                    ),
                    KnowledgeDatabaseSource(
                        id="sales_postgres",
                        knowledge_base_id=DEFAULT_KNOWLEDGE_BASE_ID,
                        source_type="postgresql",
                        name="Sales PostgreSQL",
                        description="user configured source",
                        host="db.example.test",
                        port=5432,
                        database="sales",
                        username="analyst",
                        selected_tables=["public.orders"],
                        source_metadata={},
                    ),
                ]
            )
            await session.commit()

            monkeypatch.setattr(database_sources, "get_database_config", lambda: {"provider": "sqlite"})
            sources = await database_sources.list_database_sources(session)

            assert [source["id"] for source in sources] == ["sales_postgres"]
            with pytest.raises(KnowledgeDatabaseSourceError, match="当前使用 SQLite"):
                await database_sources.get_database_source(session, "project_postgres")
            with pytest.raises(KnowledgeDatabaseSourceError, match="当前使用 SQLite"):
                await database_sources.upsert_database_source(
                    session,
                    {
                        "id": "project_postgres",
                        "name": "PuddingClaw PostgreSQL",
                        "database": "puddingclaw",
                    },
                )

        await engine.dispose()

    asyncio.run(run())


def test_postgresql_core_still_exposes_project_default(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    async def run() -> None:
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'catalog.db'}")
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        sessions = async_sessionmaker(engine, expire_on_commit=False)

        monkeypatch.setattr(
            database_sources,
            "get_database_config",
            lambda: {
                "provider": "postgresql",
                "host": "postgres.internal",
                "port": 5433,
                "database": "puddingclaw",
                "username": "app",
                "password": "secret",
            },
        )

        async with sessions() as session:
            sources = await database_sources.list_database_sources(session)

        assert len(sources) == 1
        assert sources[0]["id"] == "project_postgres"
        assert sources[0]["builtin"] is True
        assert sources[0]["host"] == "postgres.internal"
        assert sources[0]["port"] == 5433

        await engine.dispose()

    asyncio.run(run())
