"""Vanna training helpers for database-source backed NL2SQL.

Vanna is only attached to configured database sources/tables. Excel/CSV assets
remain Pandas assets and never write training data into Vanna automatically.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Literal

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from analytics.nl2sql.entity_candidates import recommend_entity_candidates
from analytics.nl2sql.runtime import build_vanna_client_from_app_config
from knowledge.database_sources import (
    KnowledgeDatabaseSourceError,
    database_source_selected_tables,
    database_source_url,
)
from knowledge.models import KnowledgeDatabaseSource


TrainingKind = Literal["sql", "ddl", "documentation"]


class VannaTrainingError(RuntimeError):
    """Raised when Vanna training cannot be completed."""


@dataclass(slots=True)
class TrainingResult:
    training_type: TrainingKind
    ids: list[str]
    count: int
    message: str


def _as_list(value: str | list[str] | None) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(item) for item in value if str(item or "").strip()]
    return [str(value)] if str(value or "").strip() else []


def _record_type(record_id: str) -> str:
    if record_id.endswith("-sql"):
        return "sql"
    if record_id.endswith("-ddl"):
        return "ddl"
    if record_id.endswith("-doc"):
        return "documentation"
    return "unknown"


def _table_context_markers(table_name: str | None) -> list[str]:
    if not table_name:
        return []
    schema, table = _split_table_name(table_name)
    schema = schema or "public"
    return [
        f"{schema}.{table}",
        f'"{schema}"."{table}"',
        f"数据库表：{schema}.{table}",
        f"数据库表：{table}",
        table,
    ]


def _matches_table_context(record: dict[str, Any], table_name: str | None) -> bool:
    markers = _table_context_markers(table_name)
    if not markers:
        return True
    haystack = "\n".join(
        str(value or "")
        for value in (
            record.get("question"),
            record.get("content"),
            record.get("preview"),
        )
    )
    return any(marker and marker in haystack for marker in markers)


def _split_table_name(name: str) -> tuple[str | None, str]:
    value = str(name or "").strip().strip('"')
    if "." not in value:
        return None, value
    schema, table = value.split(".", 1)
    return schema.strip('"') or None, table.strip('"')


def _quote_ident(identifier: str) -> str:
    return '"' + str(identifier).replace('"', '""') + '"'


def _qualified_table_sql(raw_table: str) -> tuple[str, str, str]:
    schema, table = _split_table_name(raw_table)
    schema = schema or "public"
    if not table:
        raise VannaTrainingError("表名不能为空。")
    return schema, table, f"{_quote_ident(schema)}.{_quote_ident(table)}"


async def _list_columns(source: KnowledgeDatabaseSource | dict[str, Any], table_name: str) -> list[dict[str, Any]]:
    schema, table, _ = _qualified_table_sql(table_name)
    engine = create_async_engine(database_source_url(source), pool_pre_ping=True)
    try:
        async with engine.connect() as conn:
            result = await conn.execute(
                text(
                    """
                    SELECT column_name, data_type, udt_name
                    FROM information_schema.columns
                    WHERE table_schema = :schema
                      AND table_name = :table
                    ORDER BY ordinal_position
                    """
                ),
                {"schema": schema, "table": table},
            )
            return [{"name": row.column_name, "dtype": row.data_type or row.udt_name or ""} for row in result]
    finally:
        await engine.dispose()


async def recommend_database_entity_candidates(
    source: KnowledgeDatabaseSource | dict[str, Any],
    *,
    table_name: str,
    max_candidates: int = 12,
) -> list[dict[str, Any]]:
    """Recommend entity columns from a live PostgreSQL table."""

    schema, table, qualified = _qualified_table_sql(table_name)
    columns = await _list_columns(source, table_name)
    textual_markers = ("character", "text", "varchar", "char", "boolean")
    profile_columns: list[dict[str, Any]] = []
    engine = create_async_engine(database_source_url(source), pool_pre_ping=True)
    try:
        async with engine.connect() as conn:
            total_result = await conn.execute(text(f"SELECT COUNT(*) AS total FROM {qualified}"))
            total = int(total_result.scalar() or 0)
            for column in columns:
                dtype = str(column.get("dtype") or "").lower()
                if not any(marker in dtype for marker in textual_markers):
                    continue
                column_sql = _quote_ident(str(column["name"]))
                stats = await conn.execute(
                    text(
                        f"""
                        SELECT COUNT({column_sql}) AS non_null,
                               COUNT(DISTINCT {column_sql}) AS distinct_count
                        FROM {qualified}
                        """
                    )
                )
                stats_row = stats.first()
                samples = await conn.execute(
                    text(
                        f"""
                        SELECT DISTINCT {column_sql}::text AS value
                        FROM {qualified}
                        WHERE {column_sql} IS NOT NULL
                        LIMIT 10
                        """
                    )
                )
                sample_values = [str(row.value) for row in samples if str(row.value or "").strip()]
                distinct_count = int(stats_row.distinct_count or 0) if stats_row else 0
                profile_columns.append(
                    {
                        "name": column["name"],
                        "dtype": column["dtype"],
                        "non_null": int(stats_row.non_null or 0) if stats_row else 0,
                        "distinct_count": distinct_count,
                        "distinct_ratio": distinct_count / max(total, 1) if total else None,
                        "sample_values": sample_values,
                    }
                )
    finally:
        await engine.dispose()

    profile = {"shape": [total if "total" in locals() else 0, len(columns)], "columns": profile_columns}
    return recommend_entity_candidates(profile, table_name=f"{schema}.{table}", max_candidates=max_candidates)


def _column_sql(row: Any) -> str:
    data_type = str(row.data_type or row.udt_name or "text")
    if row.character_maximum_length and data_type in {"character varying", "character", "varchar", "char"}:
        data_type = f"{data_type}({int(row.character_maximum_length)})"
    elif row.numeric_precision and data_type in {"numeric", "decimal"}:
        if row.numeric_scale is not None:
            data_type = f"{data_type}({int(row.numeric_precision)},{int(row.numeric_scale)})"
        else:
            data_type = f"{data_type}({int(row.numeric_precision)})"
    nullable = "" if str(row.is_nullable).upper() == "YES" else " NOT NULL"
    default = f" DEFAULT {row.column_default}" if row.column_default else ""
    return f"  {_quote_ident(row.column_name)} {data_type}{default}{nullable}"


async def generate_postgres_ddl(
    source: KnowledgeDatabaseSource | dict[str, Any],
    *,
    table_names: list[str] | None = None,
) -> list[str]:
    """Generate lightweight PostgreSQL DDL strings for selected tables."""

    selected = table_names or database_source_selected_tables(source)
    selected = [table for table in selected if table.strip()]
    if not selected:
        raise VannaTrainingError("请先在数据库源里选择要训练的表。")

    engine = create_async_engine(database_source_url(source), pool_pre_ping=True)
    ddls: list[str] = []
    try:
        async with engine.connect() as conn:
            for raw_table in selected:
                schema, table_name = _split_table_name(raw_table)
                if not table_name:
                    continue
                if schema:
                    result = await conn.execute(
                        text(
                            """
                            SELECT table_schema, table_name, column_name, data_type, udt_name,
                                   is_nullable, column_default, character_maximum_length,
                                   numeric_precision, numeric_scale, ordinal_position
                            FROM information_schema.columns
                            WHERE table_schema = :schema
                              AND table_name = :table_name
                            ORDER BY ordinal_position
                            """
                        ),
                        {"schema": schema, "table_name": table_name},
                    )
                else:
                    result = await conn.execute(
                        text(
                            """
                            SELECT table_schema, table_name, column_name, data_type, udt_name,
                                   is_nullable, column_default, character_maximum_length,
                                   numeric_precision, numeric_scale, ordinal_position
                            FROM information_schema.columns
                            WHERE table_schema NOT IN ('pg_catalog', 'information_schema')
                              AND table_name = :table_name
                            ORDER BY table_schema, ordinal_position
                            """
                        ),
                        {"table_name": table_name},
                    )
                rows = list(result)
                if not rows:
                    continue
                first = rows[0]
                full_name = f"{_quote_ident(first.table_schema)}.{_quote_ident(first.table_name)}"
                columns = ",\n".join(_column_sql(row) for row in rows)
                ddls.append(f"CREATE TABLE {full_name} (\n{columns}\n);")
    finally:
        await engine.dispose()

    if not ddls:
        raise VannaTrainingError("没有读取到可训练的表结构，请确认表名和权限。")
    return ddls


_BLOCKED_SQL_RE = re.compile(r"\b(insert|update|delete|drop|alter|truncate|create|grant|revoke|copy|call)\b", re.I)


def validate_training_sql(sql: str) -> str:
    cleaned = str(sql or "").strip()
    if not cleaned:
        raise VannaTrainingError("请填写 SQL 示例。")
    if _BLOCKED_SQL_RE.search(cleaned):
        raise VannaTrainingError("SQL 示例只允许 SELECT / WITH 查询。")
    if not re.match(r"^(select|with)\b", cleaned, flags=re.I):
        raise VannaTrainingError("SQL 示例需要以 SELECT 或 WITH 开头。")
    return cleaned


def _normalize_ids(value: str | list[str] | None) -> list[str]:
    return _as_list(value)


async def train_vanna_ddl(
    source: KnowledgeDatabaseSource | dict[str, Any],
    *,
    table_names: list[str] | None = None,
    ddl: str | None = None,
) -> TrainingResult:
    vn = build_vanna_client_from_app_config()
    ddls = _as_list(ddl) or await generate_postgres_ddl(source, table_names=table_names)
    ids = _normalize_ids(vn.train(ddl=ddls))
    return TrainingResult("ddl", ids, len(ids), f"已写入 {len(ids)} 条表结构训练资料。")


async def train_vanna_documentation(documentation: str) -> TrainingResult:
    content = str(documentation or "").strip()
    if not content:
        raise VannaTrainingError("请填写业务说明。")
    vn = build_vanna_client_from_app_config()
    ids = _normalize_ids(vn.train(documentation=content))
    return TrainingResult("documentation", ids, len(ids), "已写入业务说明训练资料。")


async def train_vanna_sql(question: str, sql: str) -> TrainingResult:
    prompt = str(question or "").strip()
    if not prompt:
        raise VannaTrainingError("请填写自然语言问法。")
    cleaned_sql = validate_training_sql(sql)
    vn = build_vanna_client_from_app_config()
    ids = _normalize_ids(vn.train(question=prompt, sql=cleaned_sql))
    return TrainingResult("sql", ids, len(ids), "已写入 SQL 示例训练资料。")


def list_vanna_training_data(*, table_name: str | None = None) -> dict[str, Any]:
    vn = build_vanna_client_from_app_config()
    frame = vn.get_training_data()
    records: list[dict[str, Any]] = []
    if frame is not None and not frame.empty:
        for raw in frame.to_dict("records"):
            record_id = str(raw.get("id") or "")
            content = str(raw.get("content") or "")
            question = raw.get("question")
            training_type = _record_type(record_id)
            records.append(
                {
                    "id": record_id,
                    "training_type": training_type,
                    "question": str(question) if question else None,
                    "content": content,
                    "preview": content[:240],
                }
            )
    records = [record for record in records if _matches_table_context(record, table_name)]
    counts: dict[str, int] = {"sql": 0, "ddl": 0, "documentation": 0, "unknown": 0}
    for record in records:
        key = str(record["training_type"])
        counts[key] = counts.get(key, 0) + 1
    return {"records": records, "count": len(records), "counts": counts}


def remove_vanna_training_data(training_id: str) -> bool:
    if not str(training_id or "").strip():
        raise VannaTrainingError("训练资料 ID 不能为空。")
    vn = build_vanna_client_from_app_config()
    return bool(vn.remove_training_data(str(training_id).strip()))


async def import_table_entities(
    source: KnowledgeDatabaseSource | dict[str, Any],
    *,
    table_name: str,
    column: str,
    entity_type: str,
    alias_columns: list[str] | None = None,
    max_values: int = 1000,
) -> dict[str, Any]:
    """Import distinct values from a database table column into Vanna entities."""

    selected = set(database_source_selected_tables(source))
    if selected and table_name not in selected:
        raise VannaTrainingError("请先在数据库源里勾选这张表。")
    entity_type = str(entity_type or "").strip()
    if not entity_type:
        raise VannaTrainingError("请填写实体类型。")
    column = str(column or "").strip()
    if not column:
        raise VannaTrainingError("请选择实体字段。")
    max_values = max(1, min(int(max_values or 1000), 10000))
    alias_columns = [str(item).strip() for item in alias_columns or [] if str(item or "").strip() and str(item).strip() != column]

    schema, table, qualified = _qualified_table_sql(table_name)
    available_columns = {item["name"] for item in await _list_columns(source, table_name)}
    if column not in available_columns:
        raise VannaTrainingError("实体字段不存在。")
    for alias_column in alias_columns:
        if alias_column not in available_columns:
            raise VannaTrainingError(f"别名字段不存在：{alias_column}")

    selected_sql = ", ".join([f"{_quote_ident(column)}::text AS canonical", *[f"{_quote_ident(alias)}::text AS {_quote_ident(alias)}" for alias in alias_columns]])
    engine = create_async_engine(database_source_url(source), pool_pre_ping=True)
    grouped: dict[str, set[str]] = {}
    try:
        async with engine.connect() as conn:
            result = await conn.execute(
                text(
                    f"""
                    SELECT {selected_sql}
                    FROM {qualified}
                    WHERE {_quote_ident(column)} IS NOT NULL
                    LIMIT :limit
                    """
                ),
                {"limit": max_values * 10 if alias_columns else max_values},
            )
            for row in result.mappings():
                canonical = str(row.get("canonical") or "").strip()
                if not canonical:
                    continue
                aliases = grouped.setdefault(canonical, set())
                for alias_column in alias_columns:
                    value = str(row.get(alias_column) or "").strip()
                    if value and value != canonical:
                        aliases.add(value)
                if len(grouped) >= max_values:
                    break
    finally:
        await engine.dispose()

    if not grouped:
        raise VannaTrainingError("没有读取到可导入的实体值。")

    vn = build_vanna_client_from_app_config()
    table_column = f"{schema}.{table}.{column}"
    imported: list[dict[str, Any]] = []
    for canonical, aliases in grouped.items():
        entity_id = vn.add_entity(
            canonical_name=canonical,
            entity_type=entity_type,
            aliases=sorted(aliases),
            table_column=table_column,
        )
        imported.append({"id": entity_id, "canonical_name": canonical, "aliases": sorted(aliases)})

    return {
        "ok": True,
        "source_table": f"{schema}.{table}",
        "table_column": table_column,
        "entity_type": entity_type,
        "count": len(imported),
        "entities": imported[:50],
    }


def list_vanna_entities(*, entity_type: str | None = None, table_column: str | None = None) -> dict[str, Any]:
    vn = build_vanna_client_from_app_config()
    rows = vn.get_all_entities(entity_type=entity_type) or []
    records: list[dict[str, Any]] = []
    for row in rows:
        record = dict(row)
        if table_column and str(record.get("table_column") or "") != table_column:
            continue
        records.append(record)
    return {"entities": records, "count": len(records)}


def remove_vanna_entity(entity_id: str) -> bool:
    if not str(entity_id or "").strip():
        raise VannaTrainingError("实体 ID 不能为空。")
    vn = build_vanna_client_from_app_config()
    return bool(vn.remove_entity(str(entity_id).strip()))


__all__ = [
    "TrainingResult",
    "VannaTrainingError",
    "generate_postgres_ddl",
    "import_table_entities",
    "list_vanna_training_data",
    "list_vanna_entities",
    "recommend_database_entity_candidates",
    "remove_vanna_entity",
    "remove_vanna_training_data",
    "train_vanna_ddl",
    "train_vanna_documentation",
    "train_vanna_sql",
]
