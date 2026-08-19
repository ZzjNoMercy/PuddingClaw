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
import hashlib
from collections.abc import Callable
from datetime import datetime, timezone

from sqlalchemy import inspect, select, text
from sqlalchemy.engine import Connection

logger = logging.getLogger(__name__)

CURRENT_SCHEMA_VERSION = 4

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


def _builtin_source_id(knowledge_base_id: str, connector_key: str) -> str:
    digest = hashlib.sha256(f"{knowledge_base_id}:{connector_key}".encode("utf-8")).hexdigest()[:24]
    return f"src_{digest}"


def _source_item_id(source_connection_id: str, external_id: str) -> str:
    digest = hashlib.sha256(f"{source_connection_id}:{external_id}".encode("utf-8")).hexdigest()[:24]
    return f"sitem_{digest}"


def _add_missing_columns(conn: Connection, table: str, columns: list[tuple[str, str]]) -> None:
    inspector = inspect(conn)
    if table not in inspector.get_table_names():
        return
    existing = {column["name"] for column in inspector.get_columns(table)}
    for name, ddl in columns:
        if name in existing:
            continue
        conn.exec_driver_sql(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}")
        logger.info("[schema-migration] %s: added column %s", table, name)


def _drop_legacy_document_hash_uniqueness(conn: Connection) -> None:
    """Remove the pre-Source `(knowledge_base_id, content_sha256)` identity.

    PostgreSQL can drop the named constraint directly. SQLite requires a
    transactional table rebuild; deferred FK checks keep references from
    read-later/jobs/source-items valid when the final table is renamed back.
    """

    inspector = inspect(conn)
    constraints = {item.get("name") for item in inspector.get_unique_constraints("knowledge_documents")}
    unique_indexes = {
        item.get("name")
        for item in inspector.get_indexes("knowledge_documents")
        if item.get("unique")
        and set(item.get("column_names") or ()) == {"knowledge_base_id", "content_sha256"}
    }
    constraint_name = "uq_kb_document_content_sha256"
    if constraint_name not in constraints and not unique_indexes:
        return
    if constraint_name not in constraints and unique_indexes:
        for index_name in unique_indexes:
            conn.exec_driver_sql(f"DROP INDEX {index_name}")
        return
    if conn.dialect.name == "postgresql":
        conn.exec_driver_sql(f"ALTER TABLE knowledge_documents DROP CONSTRAINT {constraint_name}")
        return
    if conn.dialect.name != "sqlite":
        raise RuntimeError(f"Unsupported catalog dialect for document identity migration: {conn.dialect.name}")

    conn.exec_driver_sql("PRAGMA defer_foreign_keys = ON")
    conn.exec_driver_sql("DROP TABLE IF EXISTS knowledge_documents_source_v4")
    conn.exec_driver_sql(
        "CREATE TABLE knowledge_documents_source_v4 ("
        "id VARCHAR(64) PRIMARY KEY NOT NULL, "
        "knowledge_base_id VARCHAR(64) NOT NULL REFERENCES knowledge_bases(id), "
        "title VARCHAR(300) NOT NULL, "
        "source_type VARCHAR(40) NOT NULL, "
        "source_path TEXT NOT NULL, "
        "storage_path TEXT NOT NULL, "
        "virtual_path TEXT NOT NULL, "
        "mime_type VARCHAR(120) NOT NULL, "
        "content_sha256 VARCHAR(64) NOT NULL, "
        "size_bytes INTEGER NOT NULL, "
        "status VARCHAR(40) NOT NULL, "
        "publish_targets JSON NOT NULL, "
        "doc_metadata JSON NOT NULL, "
        "source_connection_id VARCHAR(64) REFERENCES knowledge_source_connections(id), "
        "source_item_id VARCHAR(64), "
        "origin_url TEXT, "
        "source_revision VARCHAR(200), "
        "created_at DATETIME NOT NULL, "
        "updated_at DATETIME NOT NULL)"
    )
    columns = (
        "id, knowledge_base_id, title, source_type, source_path, storage_path, virtual_path, mime_type, "
        "content_sha256, size_bytes, status, publish_targets, doc_metadata, source_connection_id, "
        "source_item_id, origin_url, source_revision, created_at, updated_at"
    )
    conn.exec_driver_sql(
        f"INSERT INTO knowledge_documents_source_v4 ({columns}) SELECT {columns} FROM knowledge_documents"
    )
    conn.exec_driver_sql("DROP TABLE knowledge_documents")
    conn.exec_driver_sql("ALTER TABLE knowledge_documents_source_v4 RENAME TO knowledge_documents")
    logger.info("[schema-migration] removed legacy knowledge document content-hash uniqueness")


def _migrate_v4_knowledge_sources(conn: Connection) -> None:
    """Create the knowledge Source control plane and backfill built-in sources.

    Secrets and OAuth tokens are deliberately represented only by Vault
    references. Existing local uploads and read-later rows become the two
    built-in source connections without changing their public API contracts.
    """

    from knowledge.models import (
        Base,
        KnowledgeBase,
        KnowledgeDocument,
        KnowledgeImportJob,
        KnowledgeSourceConnection,
        KnowledgeSourceItem,
        ReadLaterItem,
    )

    # create_all is checkfirst-idempotent and creates the new control-plane
    # tables. It intentionally cannot alter the three legacy tables below.
    Base.metadata.create_all(bind=conn)
    _add_missing_columns(
        conn,
        "knowledge_documents",
        [
            ("source_connection_id", "VARCHAR(64) REFERENCES knowledge_source_connections(id)"),
            ("source_item_id", "VARCHAR(64)"),
            ("origin_url", "TEXT"),
            ("source_revision", "VARCHAR(200)"),
        ],
    )
    _add_missing_columns(
        conn,
        "knowledge_import_jobs",
        [
            ("source_connection_id", "VARCHAR(64) REFERENCES knowledge_source_connections(id)"),
            ("source_item_id", "VARCHAR(64) REFERENCES knowledge_source_items(id)"),
            ("sync_run_id", "VARCHAR(64) REFERENCES knowledge_sync_runs(id)"),
        ],
    )
    _add_missing_columns(
        conn,
        "read_later_items",
        [
            ("source_connection_id", "VARCHAR(64) REFERENCES knowledge_source_connections(id)"),
            ("source_item_id", "VARCHAR(64) REFERENCES knowledge_source_items(id)"),
        ],
    )
    _drop_legacy_document_hash_uniqueness(conn)

    # Existing tables were skipped wholesale by create_all, including their
    # new indexes, so create those explicitly after the ALTERs.
    index_statements = (
        "CREATE INDEX IF NOT EXISTS ix_knowledge_documents_kb_created ON knowledge_documents (knowledge_base_id, created_at)",
        "CREATE INDEX IF NOT EXISTS ix_knowledge_documents_source_item ON knowledge_documents (source_item_id)",
        "CREATE INDEX IF NOT EXISTS ix_knowledge_documents_source_connection ON knowledge_documents (source_connection_id, updated_at)",
        "CREATE INDEX IF NOT EXISTS ix_knowledge_import_jobs_source ON knowledge_import_jobs (source_connection_id, created_at)",
        "CREATE INDEX IF NOT EXISTS ix_knowledge_import_jobs_sync_run ON knowledge_import_jobs (sync_run_id, created_at)",
        "CREATE INDEX IF NOT EXISTS ix_read_later_source_item ON read_later_items (source_item_id)",
    )
    for statement in index_statements:
        conn.exec_driver_sql(statement)

    kb_table = KnowledgeBase.__table__
    document_table = KnowledgeDocument.__table__
    job_table = KnowledgeImportJob.__table__
    source_table = KnowledgeSourceConnection.__table__
    item_table = KnowledgeSourceItem.__table__
    later_table = ReadLaterItem.__table__
    now = datetime.now(timezone.utc)

    source_ids: dict[tuple[str, str], str] = {}
    for knowledge_base_id in conn.execute(select(kb_table.c.id)).scalars():
        for connector_key, name in (("local_upload", "本地上传"), ("web_capture", "网页收藏")):
            source_id = _builtin_source_id(str(knowledge_base_id), connector_key)
            source_ids[(str(knowledge_base_id), connector_key)] = source_id
            exists = conn.execute(select(source_table.c.id).where(source_table.c.id == source_id)).scalar_one_or_none()
            if exists is None:
                conn.execute(
                    source_table.insert().values(
                        id=source_id,
                        knowledge_base_id=knowledge_base_id,
                        connector_key=connector_key,
                        name=name,
                        status="ready",
                        auth_type="builtin",
                        credential_ref="",
                        config_json={},
                        schedule_json={},
                        last_error_json={},
                        created_at=now,
                        updated_at=now,
                    )
                )

    later_rows = [dict(row._mapping) for row in conn.execute(select(later_table)).all()]
    later_by_document = {str(row["document_id"]): row for row in later_rows if row.get("document_id")}
    item_by_document: dict[str, tuple[str, str]] = {}

    for row in (dict(result._mapping) for result in conn.execute(select(document_table)).all()):
        document_id = str(row["id"])
        knowledge_base_id = str(row["knowledge_base_id"])
        later = later_by_document.get(document_id)
        connector_key = "web_capture" if later is not None else "local_upload"
        source_id = source_ids[(knowledge_base_id, connector_key)]
        external_id = f"read-later:{later['id']}" if later is not None else f"document:{document_id}"
        item_id = _source_item_id(source_id, external_id)
        exists = conn.execute(select(item_table.c.id).where(item_table.c.id == item_id)).scalar_one_or_none()
        if exists is None:
            conn.execute(
                item_table.insert().values(
                    id=item_id,
                    knowledge_base_id=knowledge_base_id,
                    source_connection_id=source_id,
                    external_id=external_id,
                    external_type="web_page" if later is not None else "file",
                    title=row.get("title") or "",
                    source_url=later.get("canonical_url") if later is not None else None,
                    path_json=[],
                    revision=row.get("source_revision"),
                    content_sha256=row.get("content_sha256"),
                    document_id=document_id,
                    status="ready" if row.get("status") == "ready" else "pending",
                    metadata_json={},
                    permissions_json={},
                    created_at=row.get("created_at") or now,
                    updated_at=row.get("updated_at") or now,
                )
            )
        conn.execute(
            document_table.update()
            .where(document_table.c.id == document_id)
            .values(
                source_connection_id=source_id,
                source_item_id=item_id,
                origin_url=later.get("canonical_url") if later is not None else row.get("origin_url"),
            )
        )
        item_by_document[document_id] = (source_id, item_id)

    # Preserve read-later's reading workflow while assigning every bookmark a
    # stable web_capture source identity, including rows not parsed yet.
    for row in later_rows:
        knowledge_base_id = str(row["knowledge_base_id"])
        source_id = source_ids[(knowledge_base_id, "web_capture")]
        external_id = f"read-later:{row['id']}"
        item_id = _source_item_id(source_id, external_id)
        exists = conn.execute(select(item_table.c.id).where(item_table.c.id == item_id)).scalar_one_or_none()
        if exists is None:
            conn.execute(
                item_table.insert().values(
                    id=item_id,
                    knowledge_base_id=knowledge_base_id,
                    source_connection_id=source_id,
                    external_id=external_id,
                    external_type="web_page",
                    title=row.get("title") or row.get("canonical_url") or "",
                    source_url=row.get("canonical_url"),
                    path_json=[],
                    content_sha256=row.get("content_sha256") or None,
                    document_id=row.get("document_id"),
                    status="ready" if row.get("parse_status") == "ready" else row.get("parse_status") or "queued",
                    metadata_json={"reading_status": row.get("reading_status") or "unread"},
                    permissions_json={},
                    created_at=row.get("created_at") or now,
                    updated_at=row.get("updated_at") or now,
                )
            )
        conn.execute(
            later_table.update()
            .where(later_table.c.id == row["id"])
            .values(source_connection_id=source_id, source_item_id=item_id)
        )
        if row.get("document_id"):
            item_by_document[str(row["document_id"])] = (source_id, item_id)

    # Queued legacy imports have no document yet; still give each one a stable
    # local_upload item so the old worker can later materialize into it.
    for row in (dict(result._mapping) for result in conn.execute(select(job_table)).all()):
        knowledge_base_id = str(row["knowledge_base_id"])
        binding = item_by_document.get(str(row["document_id"])) if row.get("document_id") else None
        if binding is None:
            source_id = source_ids[(knowledge_base_id, "local_upload")]
            external_id = f"import-job:{row['id']}"
            item_id = _source_item_id(source_id, external_id)
            exists = conn.execute(select(item_table.c.id).where(item_table.c.id == item_id)).scalar_one_or_none()
            if exists is None:
                conn.execute(
                    item_table.insert().values(
                        id=item_id,
                        knowledge_base_id=knowledge_base_id,
                        source_connection_id=source_id,
                        external_id=external_id,
                        external_type="file",
                        title=row.get("title") or row.get("file_name") or "",
                        path_json=[],
                        content_sha256=row.get("source_sha256") or None,
                        document_id=row.get("document_id"),
                        status=(
                            "ready"
                            if row.get("status") in {"completed", "succeeded", "success"}
                            else row.get("status") or "queued"
                        ),
                        metadata_json={},
                        permissions_json={},
                        created_at=row.get("created_at") or now,
                        updated_at=row.get("updated_at") or now,
                    )
                )
            binding = (source_id, item_id)
        conn.execute(
            job_table.update()
            .where(job_table.c.id == row["id"])
            .values(source_connection_id=binding[0], source_item_id=binding[1])
        )

    logger.info("[schema-migration] knowledge source control plane created and built-in sources backfilled")


# (version, description, upgrade). ``None`` marks the baseline that fresh
# databases receive through create_all and legacy databases already have.
MIGRATIONS: list[tuple[int, str, Callable[[Connection], None] | None]] = [
    (1, "baseline catalog schema", None),
    (2, "queue lease columns on background job tables", _migrate_v2_queue_lease_columns),
    (3, "core_runtime_control singleton table", _migrate_v3_runtime_control),
    (4, "knowledge source control plane and built-in source backfill", _migrate_v4_knowledge_sources),
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
