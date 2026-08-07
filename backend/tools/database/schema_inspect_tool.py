"""Schema inspection tool for database Agent workflows."""

from __future__ import annotations

import asyncio
import hashlib
import json
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from langchain.tools import ToolRuntime
from langchain_core.tools import BaseTool
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from analytics.nl2sql.eav_evidence import extract_eav_type_names
from analytics.nl2sql.sql_runner import run_readonly_sql
from graph.database_schema_evidence import (
    database_schema_discovery_coordinator,
    database_schema_evidence_registry,
)
from graph.database_sql_revision_resume import database_sql_revision_resume_registry
from knowledge.database_sources import database_source_url

from .formatting import markdown_table
from .models import DatabaseSchemaInspectInput
from .scope import normalize_table_name_for_match, quote_table_identifier, resolve_database_source_scope, table_in_scope
from .spans import emit_database_span, preview_rows


def _json_safe(value: Any) -> Any:
    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral_value() else float(value)
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, set):
        return sorted((_json_safe(item) for item in value), key=lambda item: str(item))
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    return value


def _value_profile(rows: list[dict[str, object]], *, conflict_model_count: int) -> dict[str, object]:
    total = int(rows[0].get("total_row_count") or 0) if rows else 0
    distinct_count = int(rows[0].get("distinct_count") or 0) if rows else 0
    null_or_blank = 0
    for row in rows:
        value = str(row.get("type_value") or "")
        count = int(row.get("count") or 0)
        if value == "<NULL_OR_BLANK>":
            null_or_blank += count
    return {
        "total_rows": total,
        "distinct_count": distinct_count,
        "null_or_blank_rows": null_or_blank,
        "top_values": [
            {"value": row.get("type_value"), "count": int(row.get("count") or 0)}
            for row in rows
        ],
        "conflict_model_count": int(conflict_model_count),
    }


class DatabaseSchemaInspectTool(BaseTool):
    name: str = "database_schema_inspect"
    description: str = (
        "Inspect configured database metadata without Vanna: list selected tables, columns, EAV type_name values, "
        "exact type_value distributions/value profiles, or sample rows. Evidence modes return a server-owned "
        "Discovery Receipt without a parent generation; when diagnosing an "
        "existing SQL generation, pass parent_generation_id and use the receipt for an automatic physical repair. "
        "Use for schema/debug questions, not for answering business metrics directly."
    )
    args_schema: type[BaseModel] = DatabaseSchemaInspectInput
    risk_level: str = "safe"
    session_id: str = ""
    query_id: str = ""

    class Config:
        arbitrary_types_allowed = True

    async def _arun(
        self,
        mode: str = "tables",
        database_source_id: str | None = None,
        table_name: str | None = None,
        search: str | None = None,
        type_name: str | None = None,
        limit: int = 100,
        parent_generation_id: str | None = None,
        runtime: ToolRuntime | None = None,
    ) -> str:
        database_schema_discovery_coordinator.begin(
            session_id=self.session_id,
            query_id=self.query_id,
        )
        try:
            source, public_source, allowed_tables = await resolve_database_source_scope(database_source_id, [])
            normalized_mode = str(mode or "tables").strip().lower()
            if normalized_mode == "tables":
                rows = [{"table": table, "source": public_source.get("name")} for table in allowed_tables]
                emit_database_span(
                    "schema_inspect",
                    {
                        "mode": normalized_mode,
                        "source": public_source,
                        "allowed_tables": allowed_tables,
                        "rows_preview": rows[:20],
                    },
                    metadata={"database_source_id": public_source.get("id")},
                )
                return "\n".join(
                    [
                        "🧮 数据库结构",
                        f"- 数据源：{public_source.get('name')} ({public_source.get('id')})",
                        "",
                        *markdown_table(rows, ["table", "source"], max_rows=len(rows) or 20),
                    ]
                )
            if not table_name:
                raise RuntimeError(f"{normalized_mode} 模式需要 table_name。")
            if not table_in_scope(table_name, allowed_tables):
                raise RuntimeError(f"表 {table_name} 不在授权表范围内：{', '.join(allowed_tables)}")

            if normalized_mode == "sample":
                quoted_table = quote_table_identifier(table_name)
                execution = await run_readonly_sql(
                    source,
                    f"SELECT * FROM {quoted_table}",
                    allowed_tables=allowed_tables,
                    limit=limit,
                )
                emit_database_span(
                    "schema_inspect",
                    {
                        "mode": normalized_mode,
                        "source": public_source,
                        "table": table_name,
                        "columns": execution.columns,
                        "row_count": execution.row_count,
                        "rows_preview": preview_rows(execution.rows, limit=20),
                    },
                    metadata={"database_source_id": public_source.get("id")},
                )
                return "\n".join(
                    [
                        "🧮 表样例",
                        f"- 数据源：{public_source.get('name')} ({public_source.get('id')})",
                        f"- 表：{table_name}",
                        "",
                        *markdown_table(execution.rows, execution.columns, max_rows=len(execution.rows) or 20),
                    ]
                )

            engine = create_async_engine(database_source_url(source), pool_pre_ping=True)
            try:
                async with engine.connect() as conn:
                    table_only = normalize_table_name_for_match(table_name)
                    table_parts = [
                        part.strip().strip('"')
                        for part in str(table_name).split(".")
                        if part.strip()
                    ]
                    table_schema = table_parts[-2] if len(table_parts) >= 2 else "public"
                    if normalized_mode == "columns":
                        result = await conn.execute(
                            text(
                                """
                                SELECT column_name, data_type, is_nullable
                                FROM information_schema.columns
                                WHERE table_schema = :table_schema
                                  AND table_name = :table_name
                                ORDER BY ordinal_position
                                """
                            ),
                            {"table_schema": table_schema, "table_name": table_only},
                        )
                        rows = [_json_safe(dict(row)) for row in result.mappings().all()]
                        emit_database_span(
                            "schema_inspect",
                            {
                                "mode": normalized_mode,
                                "source": public_source,
                                "table": table_name,
                                "columns": ["column_name", "data_type", "is_nullable"],
                                "row_count": len(rows),
                                "rows_preview": rows[:20],
                            },
                            metadata={"database_source_id": public_source.get("id")},
                        )
                        return "\n".join(
                            [
                                "🧮 表字段",
                                f"- 数据源：{public_source.get('name')} ({public_source.get('id')})",
                                f"- 表：{table_name}",
                                "",
                                *markdown_table(rows, ["column_name", "data_type", "is_nullable"], max_rows=len(rows) or 20),
                            ]
                        )
                    if normalized_mode in {"type_names", "type_values", "value_profile"}:
                        if normalized_mode in {"type_values", "value_profile"} and not str(type_name or "").strip():
                            raise RuntimeError(f"{normalized_mode} 模式需要精确 type_name。")
                        quoted_table = quote_table_identifier(table_name)
                        parameters = {
                            "search_text": str(search or ""),
                            "type_name_value": str(type_name or "").strip(),
                            "limit_value": max(1, min(int(limit or 100), 200)),
                        }
                        if normalized_mode == "type_names":
                            statement = text(
                                f"""
                                SELECT type_name, COUNT(*) AS count
                                FROM {quoted_table}
                                WHERE type_name IS NOT NULL
                                  AND (:search_text = '' OR type_name ILIKE '%' || :search_text || '%')
                                GROUP BY type_name
                                ORDER BY COUNT(*) DESC, type_name
                                LIMIT :limit_value
                                """
                            )
                            columns = ["type_name", "count"]
                        else:
                            statement = text(
                                f"""
                                SELECT
                                    COALESCE(NULLIF(btrim(type_value), ''), '<NULL_OR_BLANK>') AS type_value,
                                    COUNT(*) AS count,
                                    COUNT(DISTINCT (brand, serial_name, car_name)) AS model_count,
                                    SUM(COUNT(*)) OVER () AS total_row_count,
                                    COUNT(*) OVER () AS distinct_count
                                FROM {quoted_table}
                                WHERE type_name = :type_name_value
                                  AND (
                                    :search_text = ''
                                    OR COALESCE(type_value, '') ILIKE '%' || :search_text || '%'
                                  )
                                GROUP BY COALESCE(NULLIF(btrim(type_value), ''), '<NULL_OR_BLANK>')
                                ORDER BY COUNT(*) DESC, type_value
                                LIMIT :limit_value
                                """
                            )
                            columns = ["type_value", "count", "model_count"]
                        result = await conn.execute(
                            statement,
                            parameters,
                        )
                        rows = [_json_safe(dict(row)) for row in result.mappings().all()]
                        runtime_context = getattr(runtime, "context", None)
                        context = runtime_context if isinstance(runtime_context, dict) else {}
                        parent = None
                        parent_type_names: list[str] = []
                        if parent_generation_id:
                            parent = database_sql_revision_resume_registry.get_generation(
                                parent_generation_id,
                                session_id=self.session_id,
                                run_id=str(context.get("run_id") or ""),
                                goal_id=str(context.get("goal_id") or ""),
                                goal_revision=context.get("goal_revision"),
                            )
                            if parent is None:
                                raise RuntimeError(
                                    "parent_generation_id 不存在或不属于当前 Session/Run/Goal。"
                                )
                            if str(parent.query_id or "") != str(self.query_id or ""):
                                raise RuntimeError("父 generation 与本次 schema 检查不属于同一 Query。")
                            if str(parent.result.source.get("id") or "") != str(public_source.get("id") or ""):
                                raise RuntimeError("父 generation 与本次 schema 检查不属于同一数据源。")
                            if not table_in_scope(table_name, list(parent.result.route.table_names)):
                                raise RuntimeError("父 generation 未授权本次检查的表。")
                            parent_type_names = sorted(extract_eav_type_names(parent.result.sql))
                            if not parent_type_names:
                                raise RuntimeError("父 SQL 没有可诊断的 vehicle_params.type_name 字面量。")
                        conflict_model_count = 0
                        profile: dict[str, object] = {}
                        if normalized_mode == "value_profile":
                            conflict_result = await conn.execute(
                                text(
                                    f"""
                                    SELECT
                                        COUNT(*) FILTER (WHERE valid_value_count > 1) AS conflict_model_count,
                                        COUNT(*) AS total_model_count
                                    FROM (
                                        SELECT brand, serial_name, car_name,
                                               COUNT(DISTINCT NULLIF(btrim(type_value), '')) AS valid_value_count
                                        FROM {quoted_table}
                                        WHERE type_name = :type_name_value
                                        GROUP BY brand, serial_name, car_name
                                    ) AS conflicts
                                    """
                                ),
                                {"type_name_value": str(type_name or "").strip()},
                            )
                            conflict_stats = _json_safe(dict(conflict_result.mappings().one()))
                            conflict_model_count = int(conflict_stats.get("conflict_model_count") or 0)
                            total_model_count = int(conflict_stats.get("total_model_count") or 0)
                            profile = _value_profile(rows, conflict_model_count=conflict_model_count)
                            conflict_examples_result = await conn.execute(
                                text(
                                    f"""
                                    SELECT brand, serial_name, car_name,
                                           array_agg(DISTINCT btrim(type_value) ORDER BY btrim(type_value)) AS values
                                    FROM {quoted_table}
                                    WHERE type_name = :type_name_value
                                      AND NULLIF(btrim(type_value), '') IS NOT NULL
                                    GROUP BY brand, serial_name, car_name
                                    HAVING COUNT(DISTINCT btrim(type_value)) > 1
                                    ORDER BY brand, serial_name, car_name
                                    LIMIT 10
                                    """
                                ),
                                {"type_name_value": str(type_name or "").strip()},
                            )
                            conflict_examples = []
                            for row in conflict_examples_result.mappings().all():
                                item = _json_safe(dict(row))
                                item["values"] = list(item.get("values") or [])
                                conflict_examples.append(item)
                            profile["conflict_examples"] = conflict_examples
                            profile["total_model_count"] = total_model_count
                            profile["conflict_rate"] = round(conflict_model_count / max(1, total_model_count), 6)
                        revision_payload = {
                            "source": str(public_source.get("id") or ""),
                            "table": normalize_table_name_for_match(table_name),
                            "mode": normalized_mode,
                            "type_name": str(type_name or ""),
                            "rows": rows,
                            "profile": profile,
                        }
                        profile_revision = "sha256:" + hashlib.sha256(
                            json.dumps(
                                revision_payload,
                                ensure_ascii=False,
                                sort_keys=True,
                                separators=(",", ":"),
                            ).encode("utf-8")
                        ).hexdigest()
                        receipt = database_schema_evidence_registry.register(
                            session_id=self.session_id,
                            query_id=self.query_id,
                            run_id=str(context.get("run_id") or ""),
                            goal_id=str(context.get("goal_id") or ""),
                            goal_revision=context.get("goal_revision"),
                            parent_generation_id=str(parent_generation_id or ""),
                            database_source_id=str(public_source.get("id") or ""),
                            table_name=str(table_name),
                            mode=normalized_mode,
                            search=str(search or ""),
                            type_name=str(type_name or ""),
                            rows=rows,
                            profile=profile,
                            profile_revision=profile_revision,
                            parent_sql_sha256=parent.sql_sha256 if parent is not None else "",
                            parent_type_names=parent_type_names,
                        )
                        emit_database_span(
                            "schema_inspect",
                            {
                                "mode": normalized_mode,
                                "source": public_source,
                                "table": table_name,
                                "search": search or "",
                                "type_name": str(type_name or ""),
                                "columns": columns,
                                "row_count": len(rows),
                                "rows_preview": rows[:20],
                                "profile": profile,
                                "receipt_kind": receipt.get("receipt_kind"),
                                "profile_revision": profile_revision,
                            },
                            metadata={"database_source_id": public_source.get("id")},
                        )
                        title = {
                            "type_names": "🧮 EAV type_name 枚举",
                            "type_values": "🧮 EAV type_value 枚举",
                            "value_profile": "🧮 EAV value profile",
                        }[normalized_mode]
                        receipt_lines = [
                            f"- schema_evidence_receipt_id：{receipt['id']}",
                            f"- receipt_kind：{receipt['receipt_kind']}",
                            f"- profile_revision：{profile_revision}",
                            f"- evidence_sha256：{receipt['sha256']}",
                        ]
                        profile_lines = (
                            ["", "```json", json.dumps(profile, ensure_ascii=False, indent=2), "```"]
                            if profile
                            else []
                        )
                        return "\n".join(
                            [
                                title,
                                f"- 数据源：{public_source.get('name')} ({public_source.get('id')})",
                                f"- 表：{table_name}",
                                *([f"- type_name：{type_name}"] if type_name else []),
                                f"- 搜索：{search or '<empty>'}",
                                *receipt_lines,
                                "",
                                *markdown_table(rows, columns, max_rows=len(rows) or 20),
                                *profile_lines,
                            ]
                        )
            finally:
                await engine.dispose()
            raise RuntimeError(f"不支持的 mode：{mode}")
        except Exception as exc:
            protocol = {
                "status": "error",
                "error_code": "schema_inspection_failed",
                "stage": "schema_inspection",
                "recoverable": False,
                "next_action": "check_scope_or_parameters",
                "message": f"{type(exc).__name__}: {exc}",
            }
            return (
                f"🧮 数据库结构检查失败：{type(exc).__name__}: {exc}\n\n"
                "```json\n"
                + json.dumps(protocol, ensure_ascii=False, indent=2)
                + "\n```"
            )
        finally:
            database_schema_discovery_coordinator.finish(
                session_id=self.session_id,
                query_id=self.query_id,
            )

    def _run(
        self,
        mode: str = "tables",
        database_source_id: str | None = None,
        table_name: str | None = None,
        search: str | None = None,
        type_name: str | None = None,
        limit: int = 100,
        parent_generation_id: str | None = None,
    ) -> str:
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(
                self._arun(
                    mode=mode,
                    database_source_id=database_source_id,
                    table_name=table_name,
                    search=search,
                    type_name=type_name,
                    limit=limit,
                    parent_generation_id=parent_generation_id,
                )
            )
        return "🧮 数据库结构检查失败：当前运行环境不支持同步调用，请使用异步工具调用。"
