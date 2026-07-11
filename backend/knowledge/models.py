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


class KnowledgeDocument(Base):
    __tablename__ = "knowledge_documents"
    __table_args__ = (
        UniqueConstraint("knowledge_base_id", "content_sha256", name="uq_kb_document_content_sha256"),
        Index("ix_knowledge_documents_kb_created", "knowledge_base_id", "created_at"),
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
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False)

    knowledge_base: Mapped[KnowledgeBase] = relationship(back_populates="documents")


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


class KnowledgeImportJob(Base):
    __tablename__ = "knowledge_import_jobs"
    __table_args__ = (
        Index("ix_knowledge_import_jobs_status_created", "status", "created_at"),
        Index("ix_knowledge_import_jobs_kb_created", "knowledge_base_id", "created_at"),
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
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    retry_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    job_metadata: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

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
