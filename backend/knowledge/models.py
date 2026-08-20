"""SQLAlchemy models for the knowledge base catalog."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from sqlalchemy import DateTime, ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship
from sqlalchemy.types import JSON


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso_utc(value: datetime | None) -> str | None:
    """Serialize a datetime with an explicit UTC offset.

    SQLite drops tzinfo on read, so datetimes loaded from the catalog are
    naive UTC. Emitting them bare makes browsers parse the timestamp as local
    time (an 8-hour skew in UTC+8); attach the offset instead.
    """
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.isoformat()


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex[:24]}"


class Base(DeclarativeBase):
    pass


class KnowledgeBase(Base):
    __tablename__ = "knowledge_bases"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=lambda: new_id("kb"))
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str] = mapped_column(Text, default="", nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False)

    documents: Mapped[list["KnowledgeDocument"]] = relationship(back_populates="knowledge_base")
    source_connections: Mapped[list["KnowledgeSourceConnection"]] = relationship(back_populates="knowledge_base")


class KnowledgeDocument(Base):
    __tablename__ = "knowledge_documents"
    __table_args__ = (
        Index("ix_knowledge_documents_kb_created", "knowledge_base_id", "created_at"),
        Index("ix_knowledge_documents_source_item", "source_item_id"),
        Index("ix_knowledge_documents_source_connection", "source_connection_id", "updated_at"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=lambda: new_id("doc"))
    knowledge_base_id: Mapped[str] = mapped_column(String(64), ForeignKey("knowledge_bases.id"), nullable=False)
    title: Mapped[str] = mapped_column(String(300), nullable=False)
    source_type: Mapped[str] = mapped_column(String(40), nullable=False, default="local_markdown")
    source_path: Mapped[str] = mapped_column(Text, nullable=False)
    storage_path: Mapped[str] = mapped_column(Text, nullable=False)
    virtual_path: Mapped[str] = mapped_column(Text, nullable=False)
    mime_type: Mapped[str] = mapped_column(String(120), nullable=False, default="text/markdown")
    content_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    size_bytes: Mapped[int] = mapped_column(default=0, nullable=False)
    status: Mapped[str] = mapped_column(String(40), nullable=False, default="ready")
    publish_targets: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    doc_metadata: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    source_connection_id: Mapped[str | None] = mapped_column(
        String(64), ForeignKey("knowledge_source_connections.id"), nullable=True
    )
    # Deliberately not a database FK: KnowledgeSourceItem.document_id owns the
    # physical relationship. Keeping this reverse lookup denormalized avoids a
    # circular FK, which is important for portable SQLite/PostgreSQL restores.
    source_item_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    origin_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    source_revision: Mapped[str | None] = mapped_column(String(200), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False)

    knowledge_base: Mapped[KnowledgeBase] = relationship(back_populates="documents")


class KnowledgeSourceConnection(Base):
    """A configured source instance scoped to one knowledge base."""

    __tablename__ = "knowledge_source_connections"
    __table_args__ = (
        Index("ix_knowledge_source_connections_kb_updated", "knowledge_base_id", "updated_at"),
        Index("ix_knowledge_source_connections_status", "status", "updated_at"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=lambda: new_id("src"))
    knowledge_base_id: Mapped[str] = mapped_column(String(64), ForeignKey("knowledge_bases.id"), nullable=False)
    connector_key: Mapped[str] = mapped_column(String(80), nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    status: Mapped[str] = mapped_column(String(40), nullable=False, default="ready")
    auth_type: Mapped[str] = mapped_column(String(40), nullable=False, default="builtin")
    credential_ref: Mapped[str] = mapped_column(Text, nullable=False, default="")
    config_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    schedule_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    last_sync_run_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    last_synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_error_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False)

    knowledge_base: Mapped[KnowledgeBase] = relationship(back_populates="source_connections")
    items: Mapped[list["KnowledgeSourceItem"]] = relationship(back_populates="source_connection")
    sync_runs: Mapped[list["KnowledgeSyncRun"]] = relationship(back_populates="source_connection")


class KnowledgeSourceItem(Base):
    """Stable identity and latest remote state for one source entity."""

    __tablename__ = "knowledge_source_items"
    __table_args__ = (
        UniqueConstraint("source_connection_id", "external_id", name="uq_source_item_external_id"),
        Index("ix_knowledge_source_items_source_status", "source_connection_id", "status"),
        Index("ix_knowledge_source_items_document", "document_id"),
        Index("ix_knowledge_source_items_last_seen", "source_connection_id", "last_seen_run_id"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=lambda: new_id("sitem"))
    knowledge_base_id: Mapped[str] = mapped_column(String(64), ForeignKey("knowledge_bases.id"), nullable=False)
    source_connection_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("knowledge_source_connections.id"), nullable=False
    )
    external_id: Mapped[str] = mapped_column(String(500), nullable=False)
    external_parent_id: Mapped[str | None] = mapped_column(String(500), nullable=True)
    external_type: Mapped[str] = mapped_column(String(80), nullable=False)
    title: Mapped[str] = mapped_column(String(500), nullable=False, default="")
    source_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    path_json: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    revision: Mapped[str | None] = mapped_column(String(200), nullable=True)
    content_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    document_id: Mapped[str | None] = mapped_column(String(64), ForeignKey("knowledge_documents.id"), nullable=True)
    status: Mapped[str] = mapped_column(String(40), nullable=False, default="discovered")
    remote_created_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    remote_updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_seen_run_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    permissions_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False)

    source_connection: Mapped[KnowledgeSourceConnection] = relationship(back_populates="items")


class KnowledgeSyncRun(Base):
    """Durable source synchronization run with resumable progress."""

    __tablename__ = "knowledge_sync_runs"
    __table_args__ = (
        Index("ix_knowledge_sync_runs_status_created", "status", "created_at"),
        Index("ix_knowledge_sync_runs_source_created", "source_connection_id", "created_at"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=lambda: new_id("sync"))
    source_connection_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("knowledge_source_connections.id"), nullable=False
    )
    mode: Mapped[str] = mapped_column(String(40), nullable=False, default="incremental")
    status: Mapped[str] = mapped_column(String(40), nullable=False, default="queued")
    cursor_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    stats_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    current_step: Mapped[str] = mapped_column(String(80), nullable=False, default="queued")
    progress: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    error_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    lease_owner: Mapped[str | None] = mapped_column(String(120), nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    attempt: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False)

    source_connection: Mapped[KnowledgeSourceConnection] = relationship(back_populates="sync_runs")


class FeishuAppCredential(Base):
    """Non-secret metadata for an encrypted Feishu application credential."""

    __tablename__ = "feishu_app_credentials"
    __table_args__ = (Index("ix_feishu_app_credentials_owner", "owner_id", "updated_at"),)

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=lambda: new_id("fapp"))
    owner_id: Mapped[str] = mapped_column(String(120), nullable=False, default="local")
    app_id_masked: Mapped[str] = mapped_column(String(120), nullable=False)
    credential_ref: Mapped[str] = mapped_column(Text, nullable=False)
    api_base_url: Mapped[str] = mapped_column(String(300), nullable=False, default="https://open.feishu.cn")
    app_name: Mapped[str] = mapped_column(String(200), nullable=False, default="")
    tenant_key: Mapped[str] = mapped_column(String(200), nullable=False, default="")
    status: Mapped[str] = mapped_column(String(40), nullable=False, default="pending_validation")
    validated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    rotated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False)


class FeishuUserGrant(Base):
    """Public OAuth grant metadata; access and refresh tokens remain in the Vault."""

    __tablename__ = "feishu_user_grants"
    __table_args__ = (
        UniqueConstraint(
            "app_credential_id", "source_connection_id", "principal_id", name="uq_feishu_user_grant_binding"
        ),
        Index("ix_feishu_user_grants_status", "status", "updated_at"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=lambda: new_id("fgrant"))
    app_credential_id: Mapped[str] = mapped_column(String(64), ForeignKey("feishu_app_credentials.id"), nullable=False)
    source_connection_id: Mapped[str | None] = mapped_column(
        String(64), ForeignKey("knowledge_source_connections.id"), nullable=True
    )
    principal_id: Mapped[str] = mapped_column(String(120), nullable=False, default="local")
    open_id: Mapped[str] = mapped_column(String(200), nullable=False, default="")
    union_id: Mapped[str] = mapped_column(String(200), nullable=False, default="")
    tenant_key: Mapped[str] = mapped_column(String(200), nullable=False, default="")
    token_credential_ref: Mapped[str] = mapped_column(Text, nullable=False)
    granted_scopes: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    access_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    refresh_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    token_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    status: Mapped[str] = mapped_column(String(40), nullable=False, default="active")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False)


class FeishuOAuthSession(Base):
    """One-time OAuth state metadata; PKCE verifier remains in the Vault."""

    __tablename__ = "feishu_oauth_sessions"
    __table_args__ = (
        UniqueConstraint("state_hash", name="uq_feishu_oauth_session_state_hash"),
        Index("ix_feishu_oauth_sessions_expiry", "status", "expires_at"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=lambda: new_id("foauth"))
    state_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    app_credential_id: Mapped[str] = mapped_column(String(64), ForeignKey("feishu_app_credentials.id"), nullable=False)
    source_connection_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("knowledge_source_connections.id"), nullable=False
    )
    principal_id: Mapped[str] = mapped_column(String(120), nullable=False, default="local")
    redirect_uri: Mapped[str] = mapped_column(Text, nullable=False)
    verifier_credential_ref: Mapped[str] = mapped_column(Text, nullable=False)
    requested_scopes: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    status: Mapped[str] = mapped_column(String(40), nullable=False, default="pending")
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class ReadLaterItem(Base):
    """A durable URL bookmark whose extracted Markdown lives in /knowledge/."""

    __tablename__ = "read_later_items"
    __table_args__ = (
        UniqueConstraint("knowledge_base_id", "canonical_url", name="uq_read_later_kb_canonical_url"),
        Index("ix_read_later_kb_created", "knowledge_base_id", "created_at"),
        Index("ix_read_later_kb_status", "knowledge_base_id", "reading_status", "parse_status"),
        Index("ix_read_later_source_item", "source_item_id"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=lambda: new_id("later"))
    knowledge_base_id: Mapped[str] = mapped_column(String(64), ForeignKey("knowledge_bases.id"), nullable=False)
    original_url: Mapped[str] = mapped_column(Text, nullable=False)
    canonical_url: Mapped[str] = mapped_column(Text, nullable=False)
    title: Mapped[str] = mapped_column(String(500), nullable=False, default="")
    site_name: Mapped[str] = mapped_column(String(300), nullable=False, default="")
    author: Mapped[str] = mapped_column(String(300), nullable=False, default="")
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    image_url: Mapped[str] = mapped_column(Text, nullable=False, default="")
    storage_path: Mapped[str] = mapped_column(Text, nullable=False, default="")
    virtual_path: Mapped[str] = mapped_column(Text, nullable=False, default="")
    content_sha256: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    parse_status: Mapped[str] = mapped_column(String(40), nullable=False, default="queued")
    reading_status: Mapped[str] = mapped_column(String(40), nullable=False, default="unread")
    error_message: Mapped[str] = mapped_column(Text, nullable=False, default="")
    tags: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    note: Mapped[str] = mapped_column(Text, nullable=False, default="")
    document_id: Mapped[str | None] = mapped_column(String(64), ForeignKey("knowledge_documents.id"), nullable=True)
    source_connection_id: Mapped[str | None] = mapped_column(
        String(64), ForeignKey("knowledge_source_connections.id"), nullable=True
    )
    source_item_id: Mapped[str | None] = mapped_column(String(64), ForeignKey("knowledge_source_items.id"), nullable=True)
    raw_snapshot_path: Mapped[str] = mapped_column(Text, nullable=False, default="")
    wiki_job_id: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    fetched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    read_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False)

    knowledge_base: Mapped[KnowledgeBase] = relationship()
    document: Mapped[KnowledgeDocument | None] = relationship()


class KnowledgeDatabaseSource(Base):
    __tablename__ = "knowledge_database_sources"
    __table_args__ = (
        Index("ix_knowledge_database_sources_kb_updated", "knowledge_base_id", "updated_at"),
        UniqueConstraint("knowledge_base_id", "name", name="uq_kb_database_source_name"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=lambda: new_id("dbs"))
    knowledge_base_id: Mapped[str] = mapped_column(String(64), ForeignKey("knowledge_bases.id"), nullable=False)
    source_type: Mapped[str] = mapped_column(String(40), nullable=False, default="postgresql")
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str] = mapped_column(Text, default="", nullable=False)
    host: Mapped[str] = mapped_column(String(300), nullable=False, default="127.0.0.1")
    port: Mapped[int] = mapped_column(Integer, nullable=False, default=5432)
    database: Mapped[str] = mapped_column(String(200), nullable=False, default="")
    username: Mapped[str] = mapped_column(String(200), nullable=False, default="")
    password: Mapped[str] = mapped_column(Text, nullable=False, default="")
    selected_tables: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    source_metadata: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False)

    knowledge_base: Mapped[KnowledgeBase] = relationship()


class KnowledgeTableAsset(Base):
    __tablename__ = "knowledge_table_assets"
    __table_args__ = (
        UniqueConstraint("knowledge_base_id", "virtual_path", "sheet_name", name="uq_kb_table_asset_virtual_sheet"),
        Index("ix_knowledge_table_assets_kb_updated", "knowledge_base_id", "updated_at"),
        Index("ix_knowledge_table_assets_kb_profile", "knowledge_base_id", "profile_status"),
    )

    asset_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    knowledge_base_id: Mapped[str] = mapped_column(String(64), ForeignKey("knowledge_bases.id"), nullable=False)
    document_id: Mapped[str | None] = mapped_column(String(64), ForeignKey("knowledge_documents.id"), nullable=True)
    source_type: Mapped[str] = mapped_column(String(40), nullable=False)
    file_name: Mapped[str] = mapped_column(String(500), nullable=False)
    storage_path: Mapped[str] = mapped_column(Text, nullable=False)
    virtual_path: Mapped[str] = mapped_column(Text, nullable=False)
    sheet_name: Mapped[str | None] = mapped_column(String(300), nullable=True)
    size_bytes: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    modified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    content_sha256: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    profile_status: Mapped[str] = mapped_column(String(40), nullable=False, default="missing")
    profile_path: Mapped[str] = mapped_column(Text, nullable=False, default="")
    rows: Mapped[int | None] = mapped_column(Integer, nullable=True)
    columns_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    columns: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    reference_status: Mapped[str] = mapped_column(String(40), nullable=False, default="pending")
    asset_metadata: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False)

    knowledge_base: Mapped[KnowledgeBase] = relationship()
    document: Mapped[KnowledgeDocument | None] = relationship()


class AnalyticsQueryResult(Base):
    __tablename__ = "analytics_query_results"
    __table_args__ = (
        Index("ix_analytics_query_results_created", "created_at"),
        Index("ix_analytics_query_results_expires", "expires_at"),
        Index("ix_analytics_query_results_session", "session_id", "created_at"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=lambda: new_id("qr"))
    session_id: Mapped[str] = mapped_column(String(120), nullable=False, default="")
    tool_call_id: Mapped[str] = mapped_column(String(120), nullable=False, default="")
    question: Mapped[str] = mapped_column(Text, nullable=False, default="")
    sql: Mapped[str] = mapped_column(Text, nullable=False, default="")
    columns: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    row_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    profile_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    artifact_path: Mapped[str] = mapped_column(Text, nullable=False, default="")
    artifact_format: Mapped[str] = mapped_column(String(20), nullable=False, default="jsonl")
    status: Mapped[str] = mapped_column(String(40), nullable=False, default="ready")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class WorkerAccessLog(Base):
    """Activity record for a local Headless Run (legacy table/column names)."""

    __tablename__ = "worker_access_logs"
    __table_args__ = (
        Index("ix_worker_access_logs_created", "created_at"),
        Index("ix_worker_access_logs_key_name", "key_name"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=lambda: new_id("wal"))
    key_id: Mapped[str] = mapped_column(String(120), nullable=False)
    key_name: Mapped[str] = mapped_column(String(120), nullable=False)
    query: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class KnowledgeImportJob(Base):
    __tablename__ = "knowledge_import_jobs"
    __table_args__ = (
        Index("ix_knowledge_import_jobs_status_created", "status", "created_at"),
        Index("ix_knowledge_import_jobs_kb_created", "knowledge_base_id", "created_at"),
        Index("ix_knowledge_import_jobs_source", "source_connection_id", "created_at"),
        Index("ix_knowledge_import_jobs_sync_run", "sync_run_id", "created_at"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=lambda: new_id("job"))
    knowledge_base_id: Mapped[str] = mapped_column(String(64), ForeignKey("knowledge_bases.id"), nullable=False)
    status: Mapped[str] = mapped_column(String(40), nullable=False, default="queued")
    file_name: Mapped[str] = mapped_column(String(500), nullable=False)
    file_type: Mapped[str] = mapped_column(String(40), nullable=False, default="file")
    file_size: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    source_path: Mapped[str] = mapped_column(Text, nullable=False)
    source_sha256: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    title: Mapped[str | None] = mapped_column(String(300), nullable=True)
    publish_targets: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    current_step: Mapped[str] = mapped_column(String(80), nullable=False, default="queued")
    progress: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    document_id: Mapped[str | None] = mapped_column(String(64), ForeignKey("knowledge_documents.id"), nullable=True)
    source_connection_id: Mapped[str | None] = mapped_column(
        String(64), ForeignKey("knowledge_source_connections.id"), nullable=True
    )
    source_item_id: Mapped[str | None] = mapped_column(String(64), ForeignKey("knowledge_source_items.id"), nullable=True)
    sync_run_id: Mapped[str | None] = mapped_column(String(64), ForeignKey("knowledge_sync_runs.id"), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    retry_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    job_metadata: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    lease_owner: Mapped[str | None] = mapped_column(String(120), nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    attempt: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    knowledge_base: Mapped[KnowledgeBase] = relationship()
    document: Mapped[KnowledgeDocument | None] = relationship()
    events: Mapped[list["KnowledgeImportEvent"]] = relationship(back_populates="job", cascade="all, delete-orphan")


class KnowledgeImportEvent(Base):
    __tablename__ = "knowledge_import_events"
    __table_args__ = (Index("ix_knowledge_import_events_job_created", "job_id", "created_at"),)

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=lambda: new_id("evt"))
    job_id: Mapped[str] = mapped_column(String(64), ForeignKey("knowledge_import_jobs.id"), nullable=False)
    level: Mapped[str] = mapped_column(String(20), nullable=False, default="info")
    message: Mapped[str] = mapped_column(Text, nullable=False)
    event_metadata: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)

    job: Mapped[KnowledgeImportJob] = relationship(back_populates="events")


class SemanticDimensionBuildJob(Base):
    __tablename__ = "semantic_dimension_build_jobs"
    __table_args__ = (
        Index("ix_semantic_dimension_build_jobs_status_created", "status", "created_at"),
        Index("ix_semantic_dimension_build_jobs_dimension_created", "dimension_id", "created_at"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=lambda: new_id("sdb"))
    session_id: Mapped[str] = mapped_column(String(120), nullable=False, default="")
    query_id: Mapped[str] = mapped_column(String(120), nullable=False, default="")
    dimension_id: Mapped[str] = mapped_column(String(160), nullable=False)
    adapter: Mapped[str] = mapped_column(String(160), nullable=False)
    requested_scope: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    input_snapshot: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    status: Mapped[str] = mapped_column(String(60), nullable=False, default="queued")
    current_step: Mapped[str] = mapped_column(String(80), nullable=False, default="queued")
    progress: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    staging_path: Mapped[str] = mapped_column(Text, nullable=False, default="")
    published_reference_path: Mapped[str] = mapped_column(Text, nullable=False, default="")
    result_summary: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    retry_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    lease_owner: Mapped[str | None] = mapped_column(String(120), nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    attempt: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    events: Mapped[list["SemanticDimensionBuildEvent"]] = relationship(back_populates="job", cascade="all, delete-orphan")


class SemanticDimensionBuildEvent(Base):
    __tablename__ = "semantic_dimension_build_events"
    __table_args__ = (Index("ix_semantic_dimension_build_events_job_created", "job_id", "created_at"),)

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=lambda: new_id("sde"))
    job_id: Mapped[str] = mapped_column(String(64), ForeignKey("semantic_dimension_build_jobs.id"), nullable=False)
    level: Mapped[str] = mapped_column(String(20), nullable=False, default="info")
    message: Mapped[str] = mapped_column(Text, nullable=False)
    event_metadata: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)

    job: Mapped[SemanticDimensionBuildJob] = relationship(back_populates="events")


class TaskNotification(Base):
    """A unified task-center notification without merging the underlying job tables."""

    __tablename__ = "task_notifications"
    __table_args__ = (
        Index("ix_task_notifications_unread_created", "read_at", "created_at"),
        Index("ix_task_notifications_subject_created", "subject_type", "subject_id", "created_at"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=lambda: new_id("ntf"))
    category: Mapped[str] = mapped_column(String(80), nullable=False, default="task")
    subject_type: Mapped[str] = mapped_column(String(80), nullable=False)
    subject_id: Mapped[str] = mapped_column(String(64), nullable=False)
    title: Mapped[str] = mapped_column(String(300), nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False, default="")
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    read_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
