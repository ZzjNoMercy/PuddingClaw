"""Read-only SQL execution for database knowledge queries."""

from __future__ import annotations

import re
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from analytics.nl2sql.schemas import SqlExecutionResult
from config import get_database_qa_config
from knowledge.database_sources import database_source_url
from knowledge.models import KnowledgeDatabaseSource


class SqlRunnerError(RuntimeError):
    """Raised when generated SQL is unsafe or cannot be executed."""


_SQL_FENCE_RE = re.compile(r"```(?:sql)?\s*(.*?)```", re.IGNORECASE | re.DOTALL)
_LEADING_SQL_RE = re.compile(r"\b(with|select)\b", re.IGNORECASE)
_DANGEROUS_RE = re.compile(
    r"\b(insert|update|delete|drop|alter|truncate|create|grant|revoke|copy|call|execute|merge|vacuum|analyze|refresh|set|reset)\b",
    re.IGNORECASE,
)
_TABLE_REF_RE = re.compile(
    r"\b(?:from|join)\s+((?:\"[^\"]+\"|[a-zA-Z_][\w]*)(?:\.(?:\"[^\"]+\"|[a-zA-Z_][\w]*))?)",
    re.IGNORECASE,
)
_CTE_NAME_RE = re.compile(
    r"(?:\bwith|,)\s+((?:\"[^\"]+\"|[a-zA-Z_][\w]*))\s+as\s*\(",
    re.IGNORECASE,
)

_DIMENSION_HINTS = ("品牌", "brand", "车系", "serial", "车型", "name", "分类", "category", "类型", "type")
_DATE_HINTS = ("日期", "时间", "date", "time", "created_at", "updated_at")
_NUMERIC_HINTS = ("价格", "金额", "销量", "数量", "price", "amount", "count", "qty", "num")


def extract_sql(raw_sql: str) -> str:
    """Extract the first SELECT/WITH SQL statement from an LLM response."""

    value = str(raw_sql or "").strip()
    if not value:
        raise SqlRunnerError("Vanna 没有生成 SQL。")

    fence_match = _SQL_FENCE_RE.search(value)
    if fence_match:
        value = fence_match.group(1).strip()
    else:
        leading_match = _LEADING_SQL_RE.search(value)
        if leading_match:
            value = value[leading_match.start() :].strip()

    value = value.strip().rstrip(";").strip()
    if not value:
        raise SqlRunnerError("Vanna 生成的 SQL 为空。")
    return value


def _normalize_identifier(value: str) -> str:
    return ".".join(part.strip().strip('"').lower() for part in value.split(".") if part.strip())


def _table_aliases(table_name: str) -> set[str]:
    normalized = _normalize_identifier(table_name)
    if not normalized:
        return set()
    if "." in normalized:
        schema, table = normalized.split(".", 1)
        return {normalized, table, f"public.{table}" if schema == "public" else normalized}
    return {normalized, f"public.{normalized}"}


def _referenced_tables(sql: str) -> set[str]:
    tables: set[str] = set()
    for match in _TABLE_REF_RE.finditer(sql):
        ref = match.group(1).strip()
        if ref.startswith("("):
            continue
        tables.add(_normalize_identifier(ref))
    return tables


def _cte_names(sql: str) -> set[str]:
    if not sql.lstrip().lower().startswith("with"):
        return set()
    prefix = sql[:4000]
    return {_normalize_identifier(match.group(1)) for match in _CTE_NAME_RE.finditer(prefix)}


def validate_readonly_sql(sql: str, *, allowed_tables: list[str]) -> str:
    """Validate SQL safety and table scope before execution."""

    clean_sql = extract_sql(sql)
    lowered = clean_sql.lower().strip()
    if not (lowered.startswith("select") or lowered.startswith("with")):
        raise SqlRunnerError("只允许执行 SELECT / WITH 查询。")
    if ";" in clean_sql:
        raise SqlRunnerError("不允许执行多条 SQL。")
    if _DANGEROUS_RE.search(clean_sql):
        raise SqlRunnerError("SQL 包含非只读关键字，已拦截。")

    allowed_aliases: set[str] = set()
    for table_name in allowed_tables:
        allowed_aliases |= _table_aliases(table_name)

    cte_names = _cte_names(clean_sql)
    referenced = _referenced_tables(clean_sql)
    blocked = sorted(ref for ref in referenced if ref not in allowed_aliases and ref not in cte_names)
    if blocked:
        raise SqlRunnerError(f"SQL 引用了未授权数据表：{', '.join(blocked)}")
    return clean_sql


def _estimate_tokens(value: Any) -> int:
    return max(1, (len(str(value)) + 2) // 3)


def _compact_cell(value: Any, *, max_chars: int) -> Any:
    if value is None or isinstance(value, (bool, int, float)):
        return value
    text_value = str(value).replace("\n", " ")
    if len(text_value) <= max_chars:
        return text_value
    return text_value[:max_chars] + "..."


def _compact_rows(rows: list[dict[str, Any]], columns: list[str], *, max_cell_chars: int) -> list[dict[str, Any]]:
    return [
        {column: _compact_cell(row.get(column), max_chars=max_cell_chars) for column in columns}
        for row in rows
    ]


def _profile_from_rows(rows: list[dict[str, Any]], columns: list[str]) -> dict[str, Any]:
    profile: dict[str, Any] = {"group_counts": {}, "date_ranges": {}, "numeric_ranges": {}}
    for column in columns:
        values = [row.get(column) for row in rows if row.get(column) not in (None, "")]
        if not values:
            continue
        lower = column.lower()
        if any(hint in column or hint in lower for hint in _DIMENSION_HINTS):
            counts: dict[str, int] = {}
            for value in values:
                key = str(value)
                counts[key] = counts.get(key, 0) + 1
            if 0 < len(counts) <= 100:
                profile["group_counts"][column] = dict(sorted(counts.items(), key=lambda item: item[1], reverse=True)[:100])
        if any(hint in column or hint in lower for hint in _DATE_HINTS):
            as_text = [str(value) for value in values]
            profile["date_ranges"][column] = {"min": min(as_text), "max": max(as_text)}
        numeric_values: list[float] = []
        for value in values:
            try:
                numeric_values.append(float(value))
            except Exception:
                continue
        if numeric_values and any(hint in column or hint in lower for hint in _NUMERIC_HINTS):
            profile["numeric_ranges"][column] = {"min": min(numeric_values), "max": max(numeric_values)}
    return {key: value for key, value in profile.items() if value}


async def _profile_with_sql(conn: Any, clean_sql: str, columns: list[str]) -> dict[str, Any]:
    profile: dict[str, Any] = {"group_counts": {}, "date_ranges": {}, "numeric_ranges": {}}
    for column in columns[:20]:
        safe_column = '"' + column.replace('"', '""') + '"'
        lower = column.lower()
        try:
            if any(hint in column or hint in lower for hint in _DIMENSION_HINTS):
                result = await conn.execute(
                    text(
                        f"SELECT {safe_column} AS value, COUNT(*) AS count "
                        f"FROM ({clean_sql}) AS puddingclaw_profile "
                        f"WHERE {safe_column} IS NOT NULL "
                        f"GROUP BY {safe_column} "
                        f"ORDER BY COUNT(*) DESC "
                        f"LIMIT 100"
                    )
                )
                counts = {str(row["value"]): int(row["count"]) for row in result.mappings().all()}
                if counts:
                    profile["group_counts"][column] = counts
            if any(hint in column or hint in lower for hint in _DATE_HINTS):
                result = await conn.execute(
                    text(
                        f"SELECT MIN({safe_column}) AS min_value, MAX({safe_column}) AS max_value "
                        f"FROM ({clean_sql}) AS puddingclaw_profile"
                    )
                )
                row = result.mappings().first()
                if row and row.get("min_value") is not None:
                    profile["date_ranges"][column] = {"min": str(row["min_value"]), "max": str(row["max_value"])}
            if any(hint in column or hint in lower for hint in _NUMERIC_HINTS):
                result = await conn.execute(
                    text(
                        f"SELECT MIN(({safe_column})::text::numeric) AS min_value, MAX(({safe_column})::text::numeric) AS max_value "
                        f"FROM ({clean_sql}) AS puddingclaw_profile "
                        f"WHERE {safe_column} IS NOT NULL AND ({safe_column})::text ~ '^-?[0-9]+(\\.[0-9]+)?$'"
                    )
                )
                row = result.mappings().first()
                if row and row.get("min_value") is not None:
                    profile["numeric_ranges"][column] = {
                        "min": float(row["min_value"]),
                        "max": float(row["max_value"]),
                    }
        except Exception:
            continue
    return {key: value for key, value in profile.items() if value}


async def run_readonly_sql(
    source: KnowledgeDatabaseSource | dict[str, Any],
    sql: str,
    *,
    allowed_tables: list[str],
    limit: int = 100,
    timeout_ms: int = 15000,
) -> SqlExecutionResult:
    """Execute validated SQL and return a completeness-aware result contract."""

    config = get_database_qa_config()
    safe_limit = max(1, min(int(limit or config.get("default_page_size") or 100), int(config.get("max_page_size") or 500)))
    max_cell_chars = int(config.get("max_cell_chars_for_llm") or 500)
    full_row_cap = int(config.get("full_rows_hard_row_cap") or 200)
    full_column_cap = int(config.get("full_rows_hard_column_cap") or 20)
    full_token_budget = int(config.get("full_rows_token_budget") or 10000)
    materialize_limit = max(5000, safe_limit, full_row_cap)
    clean_sql = validate_readonly_sql(sql, allowed_tables=allowed_tables)
    engine = create_async_engine(database_source_url(source), pool_pre_ping=True)
    try:
        async with engine.connect() as conn:
            async with conn.begin():
                await conn.execute(text(f"SET LOCAL statement_timeout = '{int(timeout_ms)}ms'"))
                count_result = await conn.execute(text(f"SELECT COUNT(*) AS count FROM ({clean_sql}) AS puddingclaw_count"))
                total_row_count = int(count_result.scalar_one() or 0)
                result = await conn.execute(
                    text(f"SELECT * FROM ({clean_sql}) AS puddingclaw_result LIMIT :limit_value"),
                    {"limit_value": min(total_row_count, materialize_limit)},
                )
                rows = [dict(row) for row in result.mappings().all()]
                columns = list(rows[0].keys()) if rows else list(result.keys())
                string_columns = [str(column) for column in columns]
                compact_all_rows = _compact_rows(rows, string_columns, max_cell_chars=max_cell_chars)
                estimated_tokens = _estimate_tokens({"columns": string_columns, "rows": compact_all_rows})
                materialized_all = len(rows) == total_row_count
                can_include_full = (
                    materialized_all
                    and total_row_count <= full_row_cap
                    and len(string_columns) <= full_column_cap
                    and estimated_tokens <= full_token_budget
                )
                if can_include_full:
                    model_rows = compact_all_rows
                else:
                    preview_rows = compact_all_rows[:safe_limit]
                    preview_tokens = _estimate_tokens({"columns": string_columns, "rows": preview_rows})
                    while preview_rows and preview_tokens > int(config.get("preview_rows_token_budget") or 3000):
                        preview_rows = preview_rows[:-1]
                        preview_tokens = _estimate_tokens({"columns": string_columns, "rows": preview_rows})
                    model_rows = preview_rows
                profile: dict[str, Any] = {}
                if config.get("profile_enabled", True):
                    if materialized_all:
                        profile = _profile_from_rows(rows, string_columns)
                    else:
                        profile = await _profile_with_sql(conn, clean_sql, string_columns)
                omitted_count = max(0, total_row_count - len(model_rows))
                limited = not can_include_full
                return SqlExecutionResult(
                    columns=string_columns,
                    rows=model_rows,
                    row_count=total_row_count,
                    limited=limited,
                    total_row_count=total_row_count,
                    preview_count=len(model_rows),
                    omitted_count=omitted_count,
                    is_complete=can_include_full,
                    estimated_tokens=estimated_tokens,
                    profile=profile,
                    llm_guardrail=(
                        "preview_rows are samples for display only. Do not infer that omitted groups do not exist."
                        if limited
                        else ""
                    ),
                    materialized_rows=rows,
                    materialized_all=materialized_all,
                )
    finally:
        await engine.dispose()
