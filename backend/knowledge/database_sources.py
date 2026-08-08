"""Knowledge database source catalog.

User configured databases and selected tables are knowledge assets, so they
belong in the catalog database instead of config.json. The project PostgreSQL
source is derived from config.json and exposed as a built-in default.
"""

from __future__ import annotations

import re
import uuid
from typing import Any
from urllib.parse import quote

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from config import get_database_config
from knowledge.models import KnowledgeBase, KnowledgeDatabaseSource, utcnow
from knowledge.service import DEFAULT_KNOWLEDGE_BASE_ID


class KnowledgeDatabaseSourceError(RuntimeError):
    """Raised when a database source cannot be loaded or tested."""


_SOURCE_ID_RE = re.compile(r"^[a-zA-Z0-9_-]{3,80}$")
_SUPPORTED_TYPES = {"postgresql"}


def _normalize_tables(tables: list[str] | None) -> list[str]:
    seen: set[str] = set()
    normalized: list[str] = []
    for raw in tables or []:
        value = str(raw or "").strip()
        if not value or value in seen:
            continue
        seen.add(value)
        normalized.append(value)
    return normalized


def _sanitize_payload(raw: dict[str, Any], *, fallback_id: str | None = None) -> dict[str, Any]:
    source_type = str(raw.get("type") or raw.get("source_type") or "postgresql").strip().lower()
    if source_type not in _SUPPORTED_TYPES:
        raise KnowledgeDatabaseSourceError(f"暂不支持的数据源类型：{source_type}")
    source_id = str(raw.get("id") or fallback_id or f"dbs_{uuid.uuid4().hex[:18]}").strip()
    if not _SOURCE_ID_RE.match(source_id):
        source_id = f"dbs_{uuid.uuid4().hex[:18]}"
    try:
        port = int(raw.get("port") or 5432)
    except (TypeError, ValueError):
        port = 5432
    return {
        "id": source_id,
        "source_type": source_type,
        "name": str(raw.get("name") or "PostgreSQL 数据源").strip(),
        "description": str(raw.get("description") or "").strip(),
        "host": str(raw.get("host") or "127.0.0.1").strip(),
        "port": port,
        "database": str(raw.get("database") or "").strip(),
        "username": str(raw.get("username") or "").strip(),
        "password": str(raw.get("password") or ""),
        "selected_tables": _normalize_tables(
            raw.get("selected_tables") if isinstance(raw.get("selected_tables"), list) else []
        ),
    }


def _project_postgres_source(saved: KnowledgeDatabaseSource | None = None) -> dict[str, Any]:
    db_config = get_database_config()
    source = {
        "id": "project_postgres",
        "source_type": "postgresql",
        "name": "项目 PostgreSQL",
        "description": "PuddingClaw 默认数据库，可作为结构化知识资产使用。",
        "host": db_config.get("host") or "127.0.0.1",
        "port": db_config.get("port") or 5432,
        "database": db_config.get("database") or "puddingclaw",
        "username": db_config.get("username") or "puddingclaw",
        "password": db_config.get("password") or "puddingclaw",
        "selected_tables": [],
        "builtin": True,
        "configured_by": db_config.get("configured_by"),
        "environment_override": db_config.get("environment_override"),
    }
    if saved:
        source["name"] = saved.name or source["name"]
        source["description"] = saved.description or source["description"]
        source["selected_tables"] = saved.selected_tables or []
        source["created_at"] = saved.created_at.isoformat() if saved.created_at else None
        source["updated_at"] = saved.updated_at.isoformat() if saved.updated_at else None
    return _public_source_dict(source)


def _project_postgres_connection_source(saved: KnowledgeDatabaseSource | None = None) -> dict[str, Any]:
    db_config = get_database_config()
    return {
        "id": "project_postgres",
        "source_type": "postgresql",
        "name": saved.name if saved else "项目 PostgreSQL",
        "description": saved.description if saved else "PuddingClaw 默认数据库，可作为结构化知识资产使用。",
        "host": db_config.get("host") or "127.0.0.1",
        "port": db_config.get("port") or 5432,
        "database": db_config.get("database") or "puddingclaw",
        "username": db_config.get("username") or "puddingclaw",
        "password": db_config.get("password") or "puddingclaw",
        "selected_tables": saved.selected_tables if saved else [],
    }


def _public_source_dict(source: KnowledgeDatabaseSource | dict[str, Any]) -> dict[str, Any]:
    if isinstance(source, KnowledgeDatabaseSource):
        payload = {
            "id": source.id,
            "source_type": source.source_type,
            "name": source.name,
            "description": source.description,
            "host": source.host,
            "port": source.port,
            "database": source.database,
            "username": source.username,
            "password": source.password,
            "selected_tables": source.selected_tables or [],
            "builtin": bool((source.source_metadata or {}).get("builtin")),
            "created_at": source.created_at.isoformat() if source.created_at else None,
            "updated_at": source.updated_at.isoformat() if source.updated_at else None,
        }
    else:
        payload = dict(source)
    payload["type"] = payload.get("source_type") or payload.get("type") or "postgresql"
    payload["selected_tables"] = _normalize_tables(payload.get("selected_tables") if isinstance(payload.get("selected_tables"), list) else [])
    payload["password_configured"] = bool(payload.get("password"))
    payload["password"] = ""
    return payload


def _connection_source(source: KnowledgeDatabaseSource | dict[str, Any]) -> dict[str, Any]:
    if isinstance(source, KnowledgeDatabaseSource):
        return {
            "source_type": source.source_type,
            "host": source.host,
            "port": source.port,
            "database": source.database,
            "username": source.username,
            "password": source.password,
        }
    return _sanitize_payload(source)


def _source_url(source: dict[str, Any]) -> str:
    database = str(source.get("database") or "").strip()
    if not database:
        raise KnowledgeDatabaseSourceError("请填写数据库名。")
    username = str(source.get("username") or "")
    password = str(source.get("password") or "")
    host = str(source.get("host") or "127.0.0.1")
    port = int(source.get("port") or 5432)
    return f"postgresql+asyncpg://{quote(username)}:{quote(password)}@{host}:{port}/{quote(database)}"


def database_source_url(source: KnowledgeDatabaseSource | dict[str, Any]) -> str:
    """Build a SQLAlchemy async URL for a configured database source."""

    return _source_url(_connection_source(source))


def database_source_selected_tables(source: KnowledgeDatabaseSource | dict[str, Any]) -> list[str]:
    """Return the normalized selected-table list from a source object or payload."""

    if isinstance(source, KnowledgeDatabaseSource):
        return _normalize_tables(source.selected_tables or [])
    return _normalize_tables(source.get("selected_tables") if isinstance(source.get("selected_tables"), list) else [])


async def ensure_default_base(session: AsyncSession, knowledge_base_id: str) -> None:
    existing = await session.get(KnowledgeBase, knowledge_base_id)
    if existing is not None:
        return
    session.add(
        KnowledgeBase(
            id=knowledge_base_id,
            name="Default Knowledge Base",
            description="Default local knowledge base",
        )
    )
    await session.commit()


async def list_database_sources(
    session: AsyncSession,
    *,
    knowledge_base_id: str = DEFAULT_KNOWLEDGE_BASE_ID,
) -> list[dict[str, Any]]:
    await ensure_default_base(session, knowledge_base_id)
    result = await session.execute(
        select(KnowledgeDatabaseSource)
        .where(KnowledgeDatabaseSource.knowledge_base_id == knowledge_base_id)
        .order_by(KnowledgeDatabaseSource.updated_at.desc())
    )
    stored = list(result.scalars())
    project_saved = next((source for source in stored if source.id == "project_postgres"), None)
    others = [source for source in stored if source.id != "project_postgres"]
    return [_project_postgres_source(project_saved), *[_public_source_dict(source) for source in others]]


async def get_database_source(
    session: AsyncSession,
    source_id: str,
    *,
    knowledge_base_id: str = DEFAULT_KNOWLEDGE_BASE_ID,
) -> KnowledgeDatabaseSource | dict[str, Any]:
    if source_id == "project_postgres":
        source = await session.get(KnowledgeDatabaseSource, source_id)
        return _project_postgres_connection_source(source)
    source = await session.get(KnowledgeDatabaseSource, source_id)
    if source is None or source.knowledge_base_id != knowledge_base_id:
        raise KnowledgeDatabaseSourceError("数据源不存在。")
    return source


async def upsert_database_source(
    session: AsyncSession,
    payload: dict[str, Any],
    *,
    knowledge_base_id: str = DEFAULT_KNOWLEDGE_BASE_ID,
) -> dict[str, Any]:
    await ensure_default_base(session, knowledge_base_id)
    data = _sanitize_payload(payload)
    existing = await session.get(KnowledgeDatabaseSource, data["id"])
    if data["id"] == "project_postgres":
        project = _project_postgres_source()
        data.update(
            {
                "source_type": project["source_type"],
                "host": project["host"],
                "port": project["port"],
                "database": project["database"],
                "username": project["username"],
                "password": get_database_config().get("password") or "",
            }
        )
    if existing is None:
        existing = KnowledgeDatabaseSource(
            id=data["id"],
            knowledge_base_id=knowledge_base_id,
            source_metadata={"builtin": data["id"] == "project_postgres"},
        )
        session.add(existing)
    elif existing.knowledge_base_id != knowledge_base_id:
        raise KnowledgeDatabaseSourceError("数据源不存在。")
    existing.source_type = data["source_type"]
    existing.name = data["name"]
    existing.description = data["description"]
    existing.host = data["host"]
    existing.port = data["port"]
    existing.database = data["database"]
    existing.username = data["username"]
    if data.get("password"):
        existing.password = data["password"]
    elif not existing.password:
        existing.password = data.get("password") or ""
    existing.selected_tables = data["selected_tables"]
    existing.updated_at = utcnow()
    await session.commit()
    await session.refresh(existing)
    if existing.id == "project_postgres":
        return _project_postgres_source(existing)
    return _public_source_dict(existing)


async def delete_database_source(
    session: AsyncSession,
    source_id: str,
    *,
    knowledge_base_id: str = DEFAULT_KNOWLEDGE_BASE_ID,
) -> None:
    source = await session.get(KnowledgeDatabaseSource, source_id)
    if source is None:
        if source_id == "project_postgres":
            return
        raise KnowledgeDatabaseSourceError("数据源不存在。")
    if source.knowledge_base_id != knowledge_base_id:
        raise KnowledgeDatabaseSourceError("数据源不存在。")
    await session.delete(source)
    await session.commit()


async def test_database_source(source: KnowledgeDatabaseSource | dict[str, Any]) -> dict[str, Any]:
    engine = create_async_engine(_source_url(_connection_source(source)), pool_pre_ping=True)
    try:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
        return {"ok": True, "message": "连接成功"}
    except Exception as exc:  # pragma: no cover - depends on local service
        return {"ok": False, "message": str(exc)}
    finally:
        await engine.dispose()


async def list_database_tables(source: KnowledgeDatabaseSource | dict[str, Any]) -> list[str]:
    engine = create_async_engine(_source_url(_connection_source(source)), pool_pre_ping=True)
    try:
        async with engine.connect() as conn:
            result = await conn.execute(
                text(
                    """
                    SELECT table_schema, table_name
                    FROM information_schema.tables
                    WHERE table_type = 'BASE TABLE'
                      AND table_schema NOT IN ('pg_catalog', 'information_schema')
                    ORDER BY table_schema, table_name
                    """
                )
            )
            tables = []
            for row in result:
                schema, table = row[0], row[1]
                tables.append(f"{schema}.{table}" if schema != "public" else str(table))
            return tables
    finally:
        await engine.dispose()


async def list_database_table_columns(
    source: KnowledgeDatabaseSource | dict[str, Any],
    table_name: str,
) -> list[str]:
    """Return columns for one configured table without exposing arbitrary schema browsing."""

    normalized_table = str(table_name or "").strip()
    selected_tables = database_source_selected_tables(source)
    if normalized_table not in selected_tables:
        raise KnowledgeDatabaseSourceError("只能读取数据源已登记表的字段。")

    if "." in normalized_table:
        schema, table = normalized_table.split(".", 1)
    else:
        schema, table = "public", normalized_table
    if not schema or not table:
        raise KnowledgeDatabaseSourceError("表名无效。")

    engine = create_async_engine(_source_url(_connection_source(source)), pool_pre_ping=True)
    try:
        async with engine.connect() as conn:
            result = await conn.execute(
                text(
                    """
                    SELECT column_name
                    FROM information_schema.columns
                    WHERE table_schema = :schema
                      AND table_name = :table
                    ORDER BY ordinal_position
                    """
                ),
                {"schema": schema, "table": table},
            )
            return [str(row.column_name) for row in result]
    finally:
        await engine.dispose()


async def list_database_table_column_values(
    source: KnowledgeDatabaseSource | dict[str, Any],
    table_name: str,
    column: str,
    *,
    search: str = "",
    limit: int = 100,
) -> dict[str, Any]:
    """Return real distinct values for a constrained filter picker."""

    normalized_table = str(table_name or "").strip()
    selected_tables = database_source_selected_tables(source)
    if normalized_table not in selected_tables:
        raise KnowledgeDatabaseSourceError("只能读取数据源已登记表的字段值。")

    if "." in normalized_table:
        schema, table = normalized_table.split(".", 1)
    else:
        schema, table = "public", normalized_table
    normalized_column = str(column or "").strip()
    if not schema or not table or not normalized_column:
        raise KnowledgeDatabaseSourceError("表名或字段名无效。")

    clean_limit = max(1, min(int(limit or 100), 200))
    clean_search = str(search or "").strip()
    engine = create_async_engine(_source_url(_connection_source(source)), pool_pre_ping=True)
    try:
        async with engine.connect() as conn:
            await conn.execute(text("SET LOCAL statement_timeout = '15s'"))
            column_result = await conn.execute(
                text(
                    """
                    SELECT 1
                    FROM information_schema.columns
                    WHERE table_schema = :schema
                      AND table_name = :table
                      AND column_name = :column
                    """
                ),
                {"schema": schema, "table": table, "column": normalized_column},
            )
            if column_result.scalar_one_or_none() is None:
                raise KnowledgeDatabaseSourceError("筛选字段不存在。")

            quoted_schema = '"' + schema.replace('"', '""') + '"'
            quoted_table = '"' + table.replace('"', '""') + '"'
            quoted_column = '"' + normalized_column.replace('"', '""') + '"'
            search_clause = ""
            params: dict[str, Any] = {"limit": clean_limit + 1}
            if clean_search:
                search_clause = f"AND {quoted_column}::text ILIKE :search"
                params["search"] = f"%{clean_search}%"
            result = await conn.execute(
                text(
                    f"""
                    SELECT DISTINCT BTRIM({quoted_column}::text) AS value
                    FROM {quoted_schema}.{quoted_table}
                    WHERE {quoted_column} IS NOT NULL
                      AND BTRIM({quoted_column}::text) <> ''
                      {search_clause}
                    ORDER BY value
                    LIMIT :limit
                    """
                ),
                params,
            )
            values = [str(row.value) for row in result]
            has_more = len(values) > clean_limit
            return {"values": values[:clean_limit], "has_more": has_more}
    finally:
        await engine.dispose()
