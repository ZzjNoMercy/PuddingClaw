"""Versioned schema migrations for the Core catalog database.

`Base.metadata.create_all` can only create missing tables; it cannot upgrade
existing ones. This module is the versioned migration runner that replaces the
create_all-only upgrade path:

- every migration is recorded in the ``core_schema_migrations`` table;
- migrations run inside the caller's transaction (``engine.begin()``), so a
  failed or interrupted migration rolls back and leaves the previous database
  recoverable instead of serving a half-migrated schema;
- each migration covers both SQLite and PostgreSQL;
- fresh databases are created from the current models via ``create_all`` and
  stamped at the current version, while legacy databases (tables present, no
  version table) are stamped at the baseline and then upgraded.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import datetime, timezone

from sqlalchemy import inspect, text
from sqlalchemy.engine import Connection

logger = logging.getLogger(__name__)

CURRENT_SCHEMA_VERSION = 3

_VERSION_TABLE_DDL = (
    "CREATE TABLE IF NOT EXISTS core_schema_migrations ("
    "version INTEGER PRIMARY KEY, "
    "description VARCHAR(200) NOT NULL, "
    "applied_at VARCHAR(40) NOT NULL)"
)

_JOB_TABLES = ("knowledge_import_jobs", "semantic_dimension_build_jobs")


def _ensure_version_table(conn: Connection) -> None:
    conn.exec_driver_sql(_VERSION_TABLE_DDL)


def _applied_versions(conn: Connection) -> set[int]:
    rows = conn.exec_driver_sql("SELECT version FROM core_schema_migrations").all()
    return {int(row[0]) for row in rows}


def _record_version(conn: Connection, version: int, description: str) -> None:
    conn.execute(
        text(
            "INSERT INTO core_schema_migrations (version, description, applied_at) "
            "VALUES (:version, :description, :applied_at)"
        ),
        {
            "version": version,
            "description": description,
            "applied_at": datetime.now(timezone.utc).isoformat(),
        },
    )


def _migrate_v2_queue_lease_columns(conn: Connection) -> None:
    """Add the at-least-once lease protocol columns to both Core job tables."""

    inspector = inspect(conn)
    timestamp_type = "TIMESTAMP WITH TIME ZONE" if conn.dialect.name == "postgresql" else "DATETIME"
    lease_columns = [
        ("lease_owner", "VARCHAR(120)"),
        ("lease_expires_at", timestamp_type),
        ("heartbeat_at", timestamp_type),
        ("attempt", "INTEGER NOT NULL DEFAULT 0"),
    ]
    for table in _JOB_TABLES:
        if table not in inspector.get_table_names():
            # Partial legacy database: nothing to alter; a later repair path
            # would recreate the table from the current models.
            continue
        existing = {column["name"] for column in inspector.get_columns(table)}
        for name, ddl in lease_columns:
            if name in existing:
                continue
            conn.exec_driver_sql(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}")
            logger.info("[schema-migration] %s: added column %s", table, name)


def _migrate_v3_runtime_control(conn: Connection) -> None:
    """Create the ``core_runtime_control`` singleton table.

    This table exists only as migration DDL (no ORM model): it backs the
    database-level drain/maintenance lease protocol in ``runtime_control``.
    """

    inspector = inspect(conn)
    if "core_runtime_control" in inspector.get_table_names():
        return
    timestamp_type = "TIMESTAMP WITH TIME ZONE" if conn.dialect.name == "postgresql" else "DATETIME"
    conn.exec_driver_sql(
        "CREATE TABLE core_runtime_control ("
        "id INTEGER PRIMARY KEY CHECK (id = 1), "
        "write_mode VARCHAR(20) NOT NULL DEFAULT 'normal', "
        "maintenance_owner VARCHAR(120), "
        f"lease_expires_at {timestamp_type}, "
        "generation INTEGER NOT NULL DEFAULT 0, "
        "reason TEXT NOT NULL DEFAULT '', "
        f"updated_at {timestamp_type})"
    )
    logger.info("[schema-migration] created table core_runtime_control")


# (version, description, upgrade). ``None`` marks the baseline that fresh
# databases receive through create_all and legacy databases already have.
MIGRATIONS: list[tuple[int, str, Callable[[Connection], None] | None]] = [
    (1, "baseline catalog schema", None),
    (2, "queue lease columns on background job tables", _migrate_v2_queue_lease_columns),
    (3, "core_runtime_control singleton table", _migrate_v3_runtime_control),
]


def migrate_to_latest(conn: Connection) -> list[int]:
    """Bring the catalog schema to CURRENT_SCHEMA_VERSION.

    Returns the versions applied (or stamped) by this call. Must run inside a
    transaction so interruption leaves the old schema untouched and a retry
    never produces duplicate rows or a half-migrated schema.
    """

    _ensure_version_table(conn)
    applied = _applied_versions(conn)
    done: list[int] = []
    has_core_tables = "knowledge_bases" in inspect(conn).get_table_names()

    if not applied and not has_core_tables:
        # Fresh database: build the current schema directly and stamp every
        # known version so no ALTER path runs on an empty catalog.
        from knowledge.models import Base

        Base.metadata.create_all(bind=conn)
        for version, description, upgrade in MIGRATIONS:
            # Some schema objects exist only as migration DDL with no ORM
            # model (e.g. core_runtime_control in v3). All upgrades are
            # idempotent, so run them on fresh databases too; column/table
            # probes skip whatever create_all already built.
            if upgrade is not None:
                upgrade(conn)
            _record_version(conn, version, description)
        logger.info("[schema-migration] fresh catalog initialized at schema version %s", CURRENT_SCHEMA_VERSION)
        return [version for version, _description, _upgrade in MIGRATIONS]

    if not applied:
        # Legacy database created by the create_all-only path: the baseline
        # schema already exists, so stamp v1 and upgrade from there. Recreate
        # any tables the legacy database is missing first — the old
        # init_database ran create_all unconditionally, so partial legacy
        # catalogs (e.g. without task_notifications or
        # semantic_dimension_build_jobs) must be repaired, not just stamped.
        # create_all is checkfirst-idempotent and leaves existing tables and
        # data untouched.
        from knowledge.models import Base

        Base.metadata.create_all(bind=conn)
        _record_version(conn, 1, f"{MIGRATIONS[0][1]} (stamped on legacy database)")
        applied.add(1)
        done.append(1)

    for version, description, upgrade in MIGRATIONS:
        if version in applied or upgrade is None:
            continue
        upgrade(conn)
        _record_version(conn, version, description)
        applied.add(version)
        done.append(version)
        logger.info("[schema-migration] applied v%s: %s", version, description)
    return done


def current_schema_version(conn: Connection) -> int:
    """Return the highest applied schema version (0 when never migrated)."""

    inspector = inspect(conn)
    if "core_schema_migrations" not in inspector.get_table_names():
        return 0
    row = conn.exec_driver_sql("SELECT MAX(version) FROM core_schema_migrations").first()
    return int(row[0]) if row and row[0] is not None else 0
