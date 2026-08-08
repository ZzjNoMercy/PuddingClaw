"""Database source and table-scope helpers for Agent database tools."""

from __future__ import annotations

from typing import Any

from db import get_sessionmaker
from knowledge.database_sources import (
    database_source_selected_tables,
    get_database_source,
    list_database_sources,
)


async def resolve_database_source_scope(
    database_source_id: str | None,
    table_names: list[str] | None,
    *,
    enforce_selected_tables: bool = True,
) -> tuple[Any, dict[str, Any], list[str]]:
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        source_id = database_source_id
        if not source_id:
            sources = await list_database_sources(session)
            configured = [source for source in sources if source.get("selected_tables")]
            if not configured:
                raise RuntimeError("没有配置可问数数据源或 selected_tables。")
            source_id = str(configured[0].get("id") or "")
        source = await get_database_source(session, source_id)

    selected_tables = database_source_selected_tables(source)
    requested_tables = [str(item).strip() for item in (table_names or []) if str(item).strip()]
    if enforce_selected_tables and requested_tables:
        selected_scopes = {normalize_table_scope(item) for item in selected_tables}
        requested_scopes = {normalize_table_scope(item) for item in requested_tables}
        if not requested_scopes.issubset(selected_scopes):
            raise RuntimeError("请求表范围超出数据源 selected_tables。")
    allowed_tables = requested_tables or selected_tables
    if not allowed_tables:
        raise RuntimeError("当前数据源没有 selected_tables，请传入 table_names 或先在数据资产里选择表。")

    public_source = {
        "id": source_id,
        "name": source.get("name") if isinstance(source, dict) else source.name,
        "database": source.get("database") if isinstance(source, dict) else source.database,
        "selected_tables": selected_tables,
    }
    return source, public_source, allowed_tables


def quote_table_identifier(table_name: str) -> str:
    parts = [part.strip().strip('"') for part in str(table_name or "").split(".") if part.strip()]
    if not parts:
        raise ValueError("table_name 不能为空。")
    return ".".join('"' + part.replace('"', '""') + '"' for part in parts[-2:])


def normalize_table_name_for_match(table_name: str) -> str:
    return str(table_name or "").strip().strip('"').split(".")[-1].lower()


def normalize_table_scope(table_name: str) -> str:
    """Normalize a table without erasing its authorization schema."""

    parts = [part.strip().strip('"').lower() for part in str(table_name or "").split(".") if part.strip()]
    if len(parts) == 1:
        return f"public.{parts[0]}"
    return ".".join(parts[-2:])


def table_in_scope(table_name: str, allowed_tables: list[str]) -> bool:
    target = normalize_table_scope(table_name)
    return any(normalize_table_scope(item) == target for item in allowed_tables)
