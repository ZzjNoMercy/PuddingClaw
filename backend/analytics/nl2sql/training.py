"""Vanna training helpers for database-source backed NL2SQL.

Vanna is only attached to configured database sources/tables. Excel/CSV assets
remain Pandas assets and never write training data into Vanna automatically.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from analytics.nl2sql.entity_candidates import recommend_entity_candidates
from analytics.nl2sql.runtime import build_vanna_client_from_app_config
from analytics.semantic_assets import get_semantic_asset_registry
from knowledge.database_sources import (
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


def _record_type(record_id: str, *, question: Any = None, content: str = "") -> str:
    if record_id.endswith("-sql"):
        return "sql"
    if record_id.endswith("-ddl"):
        return "ddl"
    if record_id.endswith("-doc"):
        return "documentation"
    if question:
        return "sql"
    normalized = str(content or "").lstrip().lower()
    if normalized.startswith("create table"):
        return "ddl"
    return "documentation" if normalized else "unknown"


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
    sample_rows: int = 2000,
) -> list[dict[str, Any]]:
    """Recommend entity columns from a live PostgreSQL table."""

    schema, table, qualified = _qualified_table_sql(table_name)
    columns = await _list_columns(source, table_name)
    textual_markers = ("character", "text", "varchar", "char", "boolean")
    text_columns = [
        column
        for column in columns
        if any(marker in str(column.get("dtype") or "").lower() for marker in textual_markers)
    ]
    if not text_columns:
        return []

    profile_columns: list[dict[str, Any]] = []
    total = 0
    engine = create_async_engine(database_source_url(source), pool_pre_ping=True)
    try:
        async with engine.connect() as conn:
            await conn.execute(text("SET LOCAL statement_timeout = '5s'"))
            total_result = await conn.execute(
                text(
                    """
                    SELECT COALESCE(c.reltuples::bigint, 0) AS total
                    FROM pg_class c
                    JOIN pg_namespace n ON n.oid = c.relnamespace
                    WHERE n.nspname = :schema
                      AND c.relname = :table
                    """
                ),
                {"schema": schema, "table": table},
            )
            total = int(total_result.scalar() or 0)

            select_columns = ", ".join(_quote_ident(str(column["name"])) for column in text_columns)
            sample_limit = max(100, min(int(sample_rows), 10000))
            sample_result = await conn.execute(
                text(f"SELECT {select_columns} FROM {qualified} LIMIT :sample_limit"),
                {"sample_limit": sample_limit},
            )
            sample_records = [dict(row) for row in sample_result.mappings().all()]
            sampled_total = len(sample_records)
            if total <= 0:
                total = sampled_total

            for column in text_columns:
                column_name = str(column["name"])
                raw_values = [
                    str(row.get(column_name) or "").strip()
                    for row in sample_records
                    if str(row.get(column_name) or "").strip()
                ]
                distinct_values = list(dict.fromkeys(raw_values))
                sample_values = distinct_values[:10]

                if not sample_values:
                    column_sql = _quote_ident(column_name)
                    fallback_samples = await conn.execute(
                        text(
                            f"""
                            SELECT {column_sql}::text AS value
                            FROM {qualified}
                            WHERE {column_sql} IS NOT NULL
                            LIMIT 10
                            """
                        )
                    )
                    sample_values = [
                        str(row.value).strip()
                        for row in fallback_samples
                        if str(row.value or "").strip()
                    ]

                distinct_count = len(distinct_values)
                profile_columns.append(
                    {
                        "name": column["name"],
                        "dtype": column["dtype"],
                        "non_null": len(raw_values),
                        "distinct_count": distinct_count,
                        "distinct_ratio": distinct_count / max(sampled_total, 1) if sampled_total else None,
                        "sample_values": sample_values,
                    }
                )
    finally:
        await engine.dispose()

    # Entity recommendation is based on a bounded sample for large database tables.
    # Use the sample size as the scoring denominator; otherwise every sampled
    # column looks sparse when compared with the full table estimate.
    profile = {"shape": [sampled_total if "sampled_total" in locals() else total, len(columns)], "columns": profile_columns}
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


def _milvus_string(value: str) -> str:
    return str(value or "").replace("\\", "\\\\").replace('"', '\\"')


def _chunks(values: list[str], size: int) -> list[list[str]]:
    size = max(1, size)
    return [values[index : index + size] for index in range(0, len(values), size)]


def _query_existing_entities(
    vn: Any,
    *,
    table_column: str,
    entity_type: str,
    canonical_names: list[str],
    batch_size: int,
) -> dict[str, dict[str, Any]]:
    """Return existing Vanna entities keyed by canonical_name.

    Entity IDs are auto-generated by the Vanna Milvus store, so duplicate
    prevention must happen by querying the semantic key before insert:
    table_column + entity_type + canonical_name.
    """

    if not canonical_names:
        return {}
    try:
        if not vn.milvus_client.has_collection(collection_name=vn.entity_collection):
            return {}
    except Exception:
        return {}

    existing: dict[str, dict[str, Any]] = {}
    escaped_table_column = _milvus_string(table_column)
    escaped_entity_type = _milvus_string(entity_type)
    query_batch_size = max(1, min(int(batch_size or 100), 100))

    for chunk in _chunks(canonical_names, query_batch_size):
        quoted_names = ", ".join(f'"{_milvus_string(name)}"' for name in chunk)
        filter_expr = (
            f'table_column == "{escaped_table_column}" '
            f'and entity_type == "{escaped_entity_type}" '
            f"and canonical_name in [{quoted_names}]"
        )
        try:
            rows = vn.milvus_client.query(
                collection_name=vn.entity_collection,
                filter=filter_expr,
                output_fields=["pk", "entity_type", "canonical_name", "aliases", "table_column"],
                limit=len(chunk),
            )
        except Exception:
            rows = []
            for name in chunk:
                try:
                    fallback_rows = vn.milvus_client.query(
                        collection_name=vn.entity_collection,
                        filter=(
                            f'table_column == "{escaped_table_column}" '
                            f'and entity_type == "{escaped_entity_type}" '
                            f'and canonical_name == "{_milvus_string(name)}"'
                        ),
                        output_fields=["pk", "entity_type", "canonical_name", "aliases", "table_column"],
                        limit=1,
                    )
                    rows.extend(fallback_rows or [])
                except Exception:
                    continue
        for row in rows or []:
            canonical = str(row.get("canonical_name") or "").strip()
            if canonical:
                existing[canonical] = dict(row)

    return existing


async def _call_vanna_sync(fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    """Run blocking Vanna/Milvus operations off the FastAPI event loop."""

    return await asyncio.to_thread(fn, *args, **kwargs)


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


async def sync_curated_semantic_entities(
    *,
    source_id: str,
    table_name: str,
    semantic_asset_ids: list[str] | None = None,
) -> TrainingResult:
    """Sync reviewed semantic canonical names/aliases into Vanna entities.

    Only authored semantic metadata is indexed. Dynamic ``type_value`` rows
    remain in the revision-bound Profile Catalog and are never copied here.
    """

    schema, table, _qualified = _qualified_table_sql(table_name)
    registry = get_semantic_asset_registry(Path(__file__).resolve().parents[2])
    available = registry.list_assets().get("assets") or []
    selected = {str(item) for item in semantic_asset_ids or [] if str(item)}
    definitions: dict[str, set[str]] = {}
    for summary in available:
        asset_id = str(summary.get("id") or "")
        if selected and asset_id not in selected:
            continue
        try:
            detail = registry.get_asset(asset_id)
        except Exception:
            continue
        frontmatter = detail.get("frontmatter") if isinstance(detail.get("frontmatter"), dict) else {}
        resolution = frontmatter.get("resolution") if isinstance(frontmatter.get("resolution"), dict) else {}
        for binding in resolution.get("bindings", []) if isinstance(resolution.get("bindings"), list) else []:
            if not isinstance(binding, dict):
                continue
            asset_ref = str(binding.get("asset_ref") or "")
            if asset_ref and not (
                asset_ref == f"{source_id}.{table}"
                or asset_ref == f"{source_id}.{schema}.{table}"
                or asset_ref.endswith(f".{table}")
            ):
                continue
            fields = binding.get("fields") if isinstance(binding.get("fields"), dict) else {}
            canonical = str(fields.get("type_name") or "").strip()
            if not canonical:
                continue
            aliases = definitions.setdefault(canonical, set())
            aliases.update(
                str(item).strip()
                for item in [frontmatter.get("name"), *(frontmatter.get("aliases") or [])]
                if str(item).strip() and str(item).strip() != canonical
            )
    if not definitions:
        raise VannaTrainingError("没有找到绑定该数据源/表的已审核 semantic canonical/alias。")

    vn = build_vanna_client_from_app_config()
    table_column = f"{schema}.{table}.type_name"
    existing = await _call_vanna_sync(
        _query_existing_entities,
        vn,
        table_column=table_column,
        entity_type="配置名称",
        canonical_names=sorted(definitions),
        batch_size=100,
    )
    ids: list[str] = []
    for canonical, aliases in definitions.items():
        previous = existing.get(canonical)
        merged_aliases = sorted(
            {
                *aliases,
                *(
                    {str(item).strip() for item in previous.get("aliases") or [] if str(item).strip()}
                    if previous
                    else set()
                ),
            }
        )
        if previous:
            previous_aliases = {str(item).strip() for item in previous.get("aliases") or [] if str(item).strip()}
            if previous_aliases == set(merged_aliases):
                continue
            previous_id = str(previous.get("pk") or previous.get("id") or "")
            if previous_id:
                await _call_vanna_sync(vn.remove_entity, previous_id)
        entity_id = await _call_vanna_sync(
            vn.add_entity,
            canonical_name=canonical,
            entity_type="配置名称",
            aliases=merged_aliases,
            table_column=table_column,
        )
        ids.append(str(entity_id))
    return TrainingResult(
        "documentation",
        ids,
        len(ids),
        f"已同步 {len(ids)} 条审核后的 EAV canonical/alias；未导入任何 type_value 明细。",
    )


def list_vanna_training_data(*, table_name: str | None = None) -> dict[str, Any]:
    vn = build_vanna_client_from_app_config()
    frame = vn.get_training_data()
    records: list[dict[str, Any]] = []
    if frame is not None and not frame.empty:
        for raw in frame.to_dict("records"):
            record_id = str(raw.get("id") or "")
            content = str(raw.get("content") or "")
            question = raw.get("question")
            training_type = _record_type(record_id, question=question, content=content)
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
    max_values: int | None = None,
    batch_size: int = 100,
    continue_on_error: bool = False,
    on_progress: Callable[[dict[str, Any]], Awaitable[None]] | None = None,
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
    clean_max_values = int(max_values) if max_values is not None else None
    if clean_max_values is not None and clean_max_values < 1:
        raise VannaTrainingError("导入数量必须大于 0。")
    alias_columns = [str(item).strip() for item in alias_columns or [] if str(item or "").strip() and str(item).strip() != column]

    schema, table, qualified = _qualified_table_sql(table_name)
    available_columns = {item["name"] for item in await _list_columns(source, table_name)}
    if column not in available_columns:
        raise VannaTrainingError("实体字段不存在。")
    for alias_column in alias_columns:
        if alias_column not in available_columns:
            raise VannaTrainingError(f"辅助匹配字段不存在：{alias_column}")

    selected_sql = ", ".join([f"{_quote_ident(column)}::text AS canonical", *[f"{_quote_ident(alias)}::text AS {_quote_ident(alias)}" for alias in alias_columns]])
    engine = create_async_engine(database_source_url(source), pool_pre_ping=True)
    grouped: dict[str, set[str]] = {}
    limit_clause = "\n                    LIMIT :limit" if clean_max_values is not None else ""
    query_params = {"limit": clean_max_values * 10 if alias_columns else clean_max_values} if clean_max_values is not None else {}
    try:
        async with engine.connect() as conn:
            result = await conn.execute(
                text(
                    f"""
                    SELECT {selected_sql}
                    FROM {qualified}
                    WHERE {_quote_ident(column)} IS NOT NULL{limit_clause}
                    """
                ),
                query_params,
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
                if clean_max_values is not None and len(grouped) >= clean_max_values:
                    break
    finally:
        await engine.dispose()

    if not grouped:
        raise VannaTrainingError("没有读取到可导入的实体值。")

    vn = build_vanna_client_from_app_config()
    table_column = f"{schema}.{table}.{column}"
    batch_size = max(1, min(int(batch_size or 100), 1000))
    existing_entities = await _call_vanna_sync(
        _query_existing_entities,
        vn,
        table_column=table_column,
        entity_type=entity_type,
        canonical_names=list(grouped.keys()),
        batch_size=batch_size,
    )
    imported: list[dict[str, Any]] = []
    updated: list[dict[str, Any]] = []
    skipped_duplicates = 0
    failed = 0
    total = len(grouped)
    if on_progress:
        await on_progress(
            {
                "stage": "indexing",
                "done": 0,
                "total": total,
                "imported": 0,
                "updated": 0,
                "skipped_duplicates": 0,
                "failed": failed,
                "batch_size": batch_size,
            }
        )
    for canonical, aliases in grouped.items():
        try:
            existing = existing_entities.get(canonical)
            next_aliases = set(aliases)
            if existing:
                existing_aliases = {str(item).strip() for item in existing.get("aliases") or [] if str(item or "").strip()}
                merged_aliases = sorted(existing_aliases | next_aliases)
                if set(merged_aliases) == existing_aliases:
                    skipped_duplicates += 1
                else:
                    existing_id = str(existing.get("pk") or existing.get("id") or "").strip()
                    if not existing_id:
                        raise VannaTrainingError(f"实体已存在但缺少可更新 ID：{canonical}")
                    if not await _call_vanna_sync(vn.remove_entity, existing_id):
                        raise VannaTrainingError(f"更新实体前删除旧记录失败：{canonical}")
                    entity_id = await _call_vanna_sync(
                        vn.add_entity,
                        canonical_name=canonical,
                        entity_type=entity_type,
                        aliases=merged_aliases,
                        table_column=table_column,
                    )
                    updated.append({"id": entity_id, "canonical_name": canonical, "aliases": merged_aliases})
            else:
                entity_id = await _call_vanna_sync(
                    vn.add_entity,
                    canonical_name=canonical,
                    entity_type=entity_type,
                    aliases=sorted(aliases),
                    table_column=table_column,
                )
                imported.append({"id": entity_id, "canonical_name": canonical, "aliases": sorted(aliases)})
        except Exception:
            failed += 1
            if not continue_on_error:
                raise
        processed = len(imported) + len(updated) + skipped_duplicates + failed
        if on_progress and (processed == total or processed % batch_size == 0):
            await on_progress(
                {
                    "stage": "indexing",
                    "done": processed,
                    "total": total,
                    "imported": len(imported),
                    "updated": len(updated),
                    "skipped_duplicates": skipped_duplicates,
                    "failed": failed,
                    "batch_size": batch_size,
                }
            )

    if not imported and not updated and skipped_duplicates <= 0:
        raise VannaTrainingError("实体导入失败，没有成功写入任何实体。")

    return {
        "ok": True,
        "source_table": f"{schema}.{table}",
        "table_column": table_column,
        "entity_type": entity_type,
        "count": len(imported),
        "updated": len(updated),
        "skipped_duplicates": skipped_duplicates,
        "failed": failed,
        "total": total,
        "entities": imported[:50],
        "updated_entities": updated[:50],
    }


def _vanna_entity_filter(
    *,
    entity_type: str | None = None,
    table_column: str | None = None,
    table_name: str | None = None,
) -> str | None:
    filters: list[str] = []
    if entity_type:
        filters.append(f'entity_type == "{_milvus_string(entity_type)}"')
    if table_column:
        filters.append(f'table_column == "{_milvus_string(table_column)}"')
    elif table_name:
        schema, table = _split_table_name(table_name)
        schema = schema or "public"
        if table:
            filters.append(f'table_column like "{_milvus_string(f"{schema}.{table}.")}%"')
    return " and ".join(filters) or None


def _vanna_entity_base_filter(
    *,
    table_column: str | None = None,
    table_name: str | None = None,
) -> str | None:
    return _vanna_entity_filter(table_column=table_column, table_name=table_name)


def _entity_alias_values(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(item) for item in value if str(item or "").strip()]
    return [str(value)]


def _entity_matches_search(row: dict[str, Any], search: str | None) -> bool:
    needle = str(search or "").strip().lower()
    if not needle:
        return True
    haystack_values = [
        row.get("entity_type"),
        row.get("canonical_name"),
        row.get("table_column"),
        *_entity_alias_values(row.get("aliases")),
    ]
    return any(needle in str(value or "").lower() for value in haystack_values)


def list_vanna_entities(
    *,
    entity_type: str | None = None,
    table_column: str | None = None,
    table_name: str | None = None,
    search: str | None = None,
    offset: int = 0,
    limit: int = 50,
) -> dict[str, Any]:
    vn = build_vanna_client_from_app_config()
    try:
        if not vn.milvus_client.has_collection(collection_name=vn.entity_collection):
            return {"entities": [], "count": 0, "limited": False, "type_counts": {}, "offset": 0, "limit": 0}
    except Exception:
        return {"entities": [], "count": 0, "limited": False, "type_counts": {}, "offset": 0, "limit": 0}

    # Count all entity types for the current table scope first. The frontend
    # must not infer available types from a truncated page, otherwise a later
    # imported type (e.g. brand) is invisible after 10k rows of another type.
    base_filter_expr = _vanna_entity_base_filter(table_column=table_column, table_name=table_name)
    filter_expr = _vanna_entity_filter(entity_type=entity_type, table_column=table_column, table_name=table_name)
    safe_offset = max(0, int(offset or 0))
    safe_limit = max(1, min(int(limit or 50), 200))
    records: list[dict[str, Any]] = []
    type_counts: dict[str, int] = {}
    matched_total = 0
    iterator = None
    try:
        iterator = vn.milvus_client.query_iterator(
            collection_name=vn.entity_collection,
            filter=base_filter_expr,
            output_fields=["pk", "entity_type", "canonical_name", "aliases", "table_column"],
            batch_size=1000,
        )
        while True:
            batch = iterator.next()
            if not batch:
                break
            for row in batch:
                type_key = str(row.get("entity_type") or "").strip()
                if type_key:
                    type_counts[type_key] = type_counts.get(type_key, 0) + 1
    finally:
        if iterator is not None:
            iterator.close()

    iterator = None
    try:
        iterator = vn.milvus_client.query_iterator(
            collection_name=vn.entity_collection,
            filter=filter_expr,
            output_fields=["pk", "entity_type", "canonical_name", "aliases", "table_column"],
            batch_size=1000,
        )
        while True:
            batch = iterator.next()
            if not batch:
                break
            for row in batch:
                row_dict = dict(row)
                if not _entity_matches_search(row_dict, search):
                    continue
                current_index = matched_total
                matched_total += 1
                if current_index < safe_offset:
                    continue
                if len(records) < safe_limit:
                    records.append(row_dict)
    finally:
        if iterator is not None:
            iterator.close()

    return {
        "entities": records,
        "count": matched_total,
        "limited": matched_total > safe_offset + len(records),
        "type_counts": type_counts,
        "offset": safe_offset,
        "limit": safe_limit,
    }


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
    "sync_curated_semantic_entities",
]
