"""Tests for the versioned Core catalog schema migration runner."""

from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import inspect, text
from sqlalchemy.ext.asyncio import create_async_engine

import db
import schema_migrations
from knowledge.models import Base

LEASE_COLUMNS = {"lease_owner", "lease_expires_at", "heartbeat_at", "attempt"}
JOB_TABLES = ("knowledge_import_jobs", "semantic_dimension_build_jobs")


def _engine(tmp_path, name: str = "catalog.db"):
    return create_async_engine(f"sqlite+aiosqlite:///{tmp_path / name}")


async def _migrate(engine) -> list[int]:
    async with engine.begin() as connection:
        return await connection.run_sync(schema_migrations.migrate_to_latest)


async def _column_names(connection, table: str) -> set[str]:
    return await connection.run_sync(lambda conn: {column["name"] for column in inspect(conn).get_columns(table)})


async def _table_names(connection) -> set[str]:
    return await connection.run_sync(lambda conn: set(inspect(conn).get_table_names()))


async def _make_legacy_database(engine) -> None:
    """Current tables minus the lease columns and without the version table."""

    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
        for table in JOB_TABLES:
            for column in ("lease_owner", "lease_expires_at", "heartbeat_at", "attempt"):
                await connection.exec_driver_sql(f"ALTER TABLE {table} DROP COLUMN {column}")


def test_fresh_database_is_created_at_current_version(tmp_path) -> None:
    async def run() -> None:
        engine = _engine(tmp_path)
        applied = await _migrate(engine)
        assert applied == [1, 2, 3, 4]

        async with engine.connect() as connection:
            tables = await _table_names(connection)
            assert "knowledge_bases" in tables
            assert "core_schema_migrations" in tables
            assert "core_runtime_control" in tables
            assert {
                "knowledge_source_connections",
                "knowledge_source_items",
                "knowledge_sync_runs",
                "feishu_app_credentials",
                "feishu_user_grants",
                "feishu_oauth_sessions",
            } <= tables
            for table in JOB_TABLES:
                assert table in tables
                assert LEASE_COLUMNS <= await _column_names(connection, table)
            version = await connection.run_sync(schema_migrations.current_schema_version)
            assert version == 4
            assert schema_migrations.CURRENT_SCHEMA_VERSION == 4
            rows = (await connection.execute(text("SELECT version FROM core_schema_migrations ORDER BY version"))).all()
            assert [row[0] for row in rows] == [1, 2, 3, 4]
        await engine.dispose()

    asyncio.run(run())


def test_legacy_database_is_stamped_and_upgraded(tmp_path) -> None:
    async def run() -> None:
        engine = _engine(tmp_path)
        await _make_legacy_database(engine)
        async with engine.connect() as connection:
            assert "core_schema_migrations" not in await _table_names(connection)
            for table in JOB_TABLES:
                assert LEASE_COLUMNS.isdisjoint(await _column_names(connection, table))

        applied = await _migrate(engine)
        assert applied == [1, 2, 3, 4]

        async with engine.connect() as connection:
            for table in JOB_TABLES:
                assert LEASE_COLUMNS <= await _column_names(connection, table)
            assert "core_runtime_control" in await _table_names(connection)
            rows = (await connection.execute(text("SELECT version FROM core_schema_migrations ORDER BY version"))).all()
            assert [row[0] for row in rows] == [1, 2, 3, 4]
            assert await connection.run_sync(schema_migrations.current_schema_version) == 4
        await engine.dispose()

    asyncio.run(run())


def test_migrate_to_latest_is_idempotent(tmp_path) -> None:
    async def run() -> None:
        engine = _engine(tmp_path)
        first = await _migrate(engine)
        assert first == [1, 2, 3, 4]
        second = await _migrate(engine)
        assert second == []

        async with engine.connect() as connection:
            assert await connection.run_sync(schema_migrations.current_schema_version) == 4
            count = await connection.scalar(text("SELECT COUNT(*) FROM core_schema_migrations"))
            assert count == 4
        await engine.dispose()

    asyncio.run(run())


def test_failed_migration_rolls_back_and_retry_is_clean(tmp_path, monkeypatch) -> None:
    """Migration atomicity on the production engine built by db.get_engine()."""

    # db.get_engine() only picks SQLite when the deployment contract opts in;
    # the stock config defaults database.provider to "sqlite".
    monkeypatch.setenv("PUDDINGCLAW_DATABASE_MODE", "sqlite")
    db._engine = None
    db._sessionmaker = None
    db._last_error = None
    db._last_schema_version = None

    async def run() -> None:
        engine = db.get_engine()
        assert await _migrate(engine) == [1, 2, 3, 4]
        original_migrations = list(schema_migrations.MIGRATIONS)

        def failing_v5(connection) -> None:
            connection.exec_driver_sql("ALTER TABLE knowledge_bases ADD COLUMN migration_probe VARCHAR(10)")
            raise RuntimeError("simulated mid-migration crash")

        monkeypatch.setattr(
            schema_migrations,
            "MIGRATIONS",
            [*original_migrations, (5, "failing probe migration", failing_v5)],
        )
        with pytest.raises(RuntimeError, match="simulated mid-migration crash"):
            await _migrate(engine)

        # The half-applied v5 must be invisible: no version row, no column.
        async with engine.connect() as connection:
            assert await connection.run_sync(schema_migrations.current_schema_version) == 4
            assert "migration_probe" not in await _column_names(connection, "knowledge_bases")

        def good_v5(connection) -> None:
            connection.exec_driver_sql("ALTER TABLE knowledge_bases ADD COLUMN migration_probe VARCHAR(10)")

        monkeypatch.setattr(
            schema_migrations,
            "MIGRATIONS",
            [*original_migrations, (5, "probe migration", good_v5)],
        )
        applied = await _migrate(engine)
        assert applied == [5]

        async with engine.connect() as connection:
            assert await connection.run_sync(schema_migrations.current_schema_version) == 5
            assert "migration_probe" in await _column_names(connection, "knowledge_bases")
            count = await connection.scalar(text("SELECT COUNT(*) FROM core_schema_migrations WHERE version = 5"))
            assert count == 1
        await engine.dispose()

    try:
        asyncio.run(run())
    finally:
        db._engine = None
        db._sessionmaker = None
        db._last_error = None
        db._last_schema_version = None


def test_legacy_database_upgrade_preserves_existing_rows(tmp_path) -> None:
    async def run() -> None:
        engine = _engine(tmp_path)
        await _make_legacy_database(engine)
        async with engine.begin() as connection:
            await connection.exec_driver_sql(
                "INSERT INTO knowledge_bases (id, name, description, created_at, updated_at) "
                "VALUES ('kb-1', 'legacy', '', '2026-01-01 00:00:00', '2026-01-01 00:00:00')"
            )
            await connection.exec_driver_sql(
                "INSERT INTO knowledge_import_jobs ("
                "id, knowledge_base_id, status, file_name, file_type, file_size, source_path, "
                "source_sha256, publish_targets, current_step, progress, retry_count, job_metadata, "
                "created_at, updated_at"
                ") VALUES ("
                "'job-1', 'kb-1', 'failed', 'legacy.md', 'markdown', 10, '/tmp/legacy.md', "
                "'', '[]', 'failed', 0, 2, '{}', '2026-01-01 00:00:00', '2026-01-01 00:00:00')"
            )

        applied = await _migrate(engine)
        assert applied == [1, 2, 3, 4]

        async with engine.connect() as connection:
            assert "core_runtime_control" in await _table_names(connection)
            row = (
                await connection.execute(
                    text(
                        "SELECT status, retry_count, attempt, lease_owner, source_connection_id, source_item_id "
                        "FROM knowledge_import_jobs WHERE id = 'job-1'"
                    )
                )
            ).first()
            assert row is not None
            assert row[0] == "failed"
            assert row[1] == 2
            assert row[2] == 0
            assert row[3] is None
            assert row[4]
            assert row[5]
            builtins = (
                await connection.execute(
                    text(
                        "SELECT connector_key FROM knowledge_source_connections "
                        "WHERE knowledge_base_id = 'kb-1' ORDER BY connector_key"
                    )
                )
            ).scalars().all()
            assert builtins == ["local_upload", "web_capture"]
        await engine.dispose()

    asyncio.run(run())


def test_partial_legacy_database_recreates_missing_tables(tmp_path) -> None:
    """A legacy catalog missing newer tables is repaired, not just stamped.

    The old init_database ran create_all unconditionally; the migration
    runner must keep that guarantee for partial legacy databases (e.g. ones
    that predate task_notifications / semantic_dimension_build_jobs).
    """

    async def run() -> None:
        engine = _engine(tmp_path)
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
            # Keep only the two oldest tables, as an early legacy catalog.
            for table in sorted(set(Base.metadata.tables) - {"knowledge_bases", "knowledge_import_jobs"}):
                await connection.exec_driver_sql(f"DROP TABLE {table}")
            for column in ("lease_owner", "lease_expires_at", "heartbeat_at", "attempt"):
                await connection.exec_driver_sql(f"ALTER TABLE knowledge_import_jobs DROP COLUMN {column}")
            await connection.exec_driver_sql(
                "INSERT INTO knowledge_bases (id, name, description, created_at, updated_at) "
                "VALUES ('kb-1', 'legacy', '', '2026-01-01 00:00:00', '2026-01-01 00:00:00')"
            )
            await connection.exec_driver_sql(
                "INSERT INTO knowledge_import_jobs ("
                "id, knowledge_base_id, status, file_name, file_type, file_size, source_path, "
                "source_sha256, publish_targets, current_step, progress, retry_count, job_metadata, "
                "created_at, updated_at"
                ") VALUES ("
                "'job-1', 'kb-1', 'succeeded', 'legacy.md', 'markdown', 10, '/tmp/legacy.md', "
                "'', '[]', 'done', 100, 0, '{}', '2026-01-01 00:00:00', '2026-01-01 00:00:00')"
            )

        async with engine.connect() as connection:
            tables = await _table_names(connection)
            assert "task_notifications" not in tables
            assert "semantic_dimension_build_jobs" not in tables

        applied = await _migrate(engine)
        assert applied == [1, 2, 3, 4]

        async with engine.connect() as connection:
            tables = await _table_names(connection)
            expected = set(Base.metadata.tables) | {"core_schema_migrations", "core_runtime_control"}
            assert expected <= tables
            for table in JOB_TABLES:
                assert LEASE_COLUMNS <= await _column_names(connection, table)
            # Old data survives the repair.
            row = (
                await connection.execute(
                    text("SELECT status, progress, attempt, lease_owner FROM knowledge_import_jobs WHERE id = 'job-1'")
                )
            ).first()
            assert row is not None
            assert row[0] == "succeeded"
            assert row[1] == 100
            assert row[2] == 0
            assert row[3] is None
            name = await connection.scalar(text("SELECT name FROM knowledge_bases WHERE id = 'kb-1'"))
            assert name == "legacy"
        await engine.dispose()

    asyncio.run(run())


def test_v4_rebuilds_legacy_sqlite_document_identity_without_losing_rows(tmp_path) -> None:
    async def run() -> None:
        engine = _engine(tmp_path)
        async with engine.begin() as connection:
            await connection.exec_driver_sql(
                "CREATE TABLE knowledge_bases ("
                "id VARCHAR(64) PRIMARY KEY, name VARCHAR(200) NOT NULL, description TEXT NOT NULL, "
                "created_at DATETIME NOT NULL, updated_at DATETIME NOT NULL)"
            )
            await connection.exec_driver_sql(
                "CREATE TABLE knowledge_documents ("
                "id VARCHAR(64) PRIMARY KEY, knowledge_base_id VARCHAR(64) NOT NULL REFERENCES knowledge_bases(id), "
                "title VARCHAR(300) NOT NULL, source_type VARCHAR(40) NOT NULL, source_path TEXT NOT NULL, "
                "storage_path TEXT NOT NULL, virtual_path TEXT NOT NULL, mime_type VARCHAR(120) NOT NULL, "
                "content_sha256 VARCHAR(64) NOT NULL, size_bytes INTEGER NOT NULL, status VARCHAR(40) NOT NULL, "
                "publish_targets JSON NOT NULL, doc_metadata JSON NOT NULL, created_at DATETIME NOT NULL, "
                "updated_at DATETIME NOT NULL, CONSTRAINT uq_kb_document_content_sha256 "
                "UNIQUE (knowledge_base_id, content_sha256))"
            )
            await connection.exec_driver_sql(
                "INSERT INTO knowledge_bases VALUES "
                "('kb-1', 'legacy', '', '2026-01-01 00:00:00', '2026-01-01 00:00:00')"
            )
            await connection.exec_driver_sql(
                "INSERT INTO knowledge_documents VALUES ("
                "'doc-1', 'kb-1', 'first', 'local_markdown', '/tmp/a.md', '/tmp/a.md', '/knowledge/a.md', "
                "'text/markdown', 'same-hash', 1, 'ready', '[]', '{}', "
                "'2026-01-01 00:00:00', '2026-01-01 00:00:00')"
            )

        assert await _migrate(engine) == [1, 2, 3, 4]
        async with engine.begin() as connection:
            constraints = await connection.run_sync(
                lambda conn: inspect(conn).get_unique_constraints("knowledge_documents")
            )
            assert not any(
                set(item.get("column_names") or ()) == {"knowledge_base_id", "content_sha256"}
                for item in constraints
            )
            assert await connection.scalar(text("SELECT COUNT(*) FROM knowledge_documents")) == 1
            await connection.exec_driver_sql(
                "INSERT INTO knowledge_documents ("
                "id, knowledge_base_id, title, source_type, source_path, storage_path, virtual_path, mime_type, "
                "content_sha256, size_bytes, status, publish_targets, doc_metadata, created_at, updated_at"
                ") VALUES ("
                "'doc-2', 'kb-1', 'second', 'feishu_docx', 'wiki-1', '/tmp/b.md', '/knowledge/b.md', "
                "'text/markdown', 'same-hash', 1, 'ready', '[]', '{}', "
                "'2026-01-01 00:00:00', '2026-01-01 00:00:00')"
            )
        await engine.dispose()

    asyncio.run(run())
