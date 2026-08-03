"""Read-only SQL execution for database knowledge queries."""

from __future__ import annotations

import re
from copy import deepcopy
from typing import Any

import sqlglot
from sqlalchemy import exc as sa_exc
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from sqlglot import exp
from sqlglot.errors import ParseError
from sqlglot.optimizer.scope import Scope, build_scope

from analytics.nl2sql.schemas import SqlExecutionResult
from config import get_database_qa_config
from knowledge.database_sources import database_source_url
from knowledge.models import KnowledgeDatabaseSource


class SqlRunnerError(RuntimeError):
    """Raised when generated SQL is unsafe or cannot be executed."""

    def __init__(self, message: str, *, sql: str | None = None) -> None:
        super().__init__(message)
        self.sql = sql


_SQL_FENCE_RE = re.compile(r"```(?:sql)?\s*(.*?)```", re.IGNORECASE | re.DOTALL)
_LEADING_SQL_RE = re.compile(r"\b(with|select)\b", re.IGNORECASE)
_DANGEROUS_RE = re.compile(
    r"\b(insert|update|delete|drop|alter|truncate|create|grant|revoke|copy|call|execute|merge|vacuum|analyze|refresh|set|reset)\b",
    re.IGNORECASE,
)
_SCALAR_SUBQUERY_LIST_RE = re.compile(
    r"\)\s+as\s+(?:\"[^\"]+\"|[a-zA-Z_][\w]*)\s*,\s*\(\s*select\b",
    re.IGNORECASE | re.DOTALL,
)
_FORBIDDEN_FUNCTION_NAMES = {
    "cursor_to_xml",
    "database_to_xml",
    "database_to_xml_and_xmlschema",
    "database_to_xmlschema",
    "lo_export",
    "lo_get",
    "lo_import",
    "loread",
    "query_to_xml",
    "schema_to_xml",
    "schema_to_xml_and_xmlschema",
    "schema_to_xmlschema",
    "table_to_xml",
    "table_to_xml_and_xmlschema",
    "table_to_xmlschema",
}
_FORBIDDEN_FUNCTION_PREFIXES = ("dblink", "pg_ls_", "pg_read_")
_ALLOWED_ANONYMOUS_FUNCTION_NAMES = {
    # sqlglot does not model these PostgreSQL built-ins with a dedicated
    # expression class. Unknown scalar functions fail closed because a
    # SECURITY DEFINER function can read relations absent from the query AST.
    "json_agg",
    "json_build_array",
    "json_build_object",
    "json_object_agg",
    "jsonb_agg",
    "jsonb_build_array",
    "jsonb_build_object",
    "jsonb_object_agg",
    "make_date",
    "make_interval",
    "width_bucket",
}
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
    return _repair_scalar_subquery_list(value)


def _paren_balance(sql: str) -> int:
    balance = 0
    in_single = False
    in_double = False
    index = 0
    while index < len(sql):
        char = sql[index]
        if in_single:
            if char == "'" and index + 1 < len(sql) and sql[index + 1] == "'":
                index += 2
                continue
            if char == "'":
                in_single = False
        elif in_double:
            if char == '"' and index + 1 < len(sql) and sql[index + 1] == '"':
                index += 2
                continue
            if char == '"':
                in_double = False
        else:
            if char == "'":
                in_single = True
            elif char == '"':
                in_double = True
            elif char == "(":
                balance += 1
            elif char == ")":
                balance -= 1
        index += 1
    return balance


def _repair_scalar_subquery_list(sql: str) -> str:
    """Repair a common LLM shape: ``(SELECT ...) AS a, (SELECT ...) AS b``.

    ``extract_sql`` intentionally starts at the first SELECT token. If the LLM
    omits the outer ``SELECT`` and returns a list of scalar subqueries, that
    strips the leading ``(`` and leaves invalid SQL. Only repair this narrow
    shape when the parenthesis balance proves exactly one leading ``(`` is
    missing.
    """

    stripped = sql.strip()
    lowered = stripped.lower()
    if not lowered.startswith("select "):
        return sql
    if _paren_balance(stripped) != -1:
        return sql
    if not _SCALAR_SUBQUERY_LIST_RE.search(stripped):
        return sql
    return "SELECT (" + stripped


def _normalize_identifier(value: str) -> str:
    return ".".join(part.strip().strip('"').lower() for part in value.split(".") if part.strip())


def _strip_sql_comments(sql: str) -> str:
    """Remove SQL comments while preserving quoted strings and line layout."""

    output: list[str] = []
    index = 0
    in_single = False
    in_double = False
    while index < len(sql):
        char = sql[index]
        next_char = sql[index + 1] if index + 1 < len(sql) else ""
        if in_single:
            output.append(char)
            if char == "'" and next_char == "'":
                output.append(next_char)
                index += 2
                continue
            if char == "'":
                in_single = False
            index += 1
            continue
        if in_double:
            output.append(char)
            if char == '"' and next_char == '"':
                output.append(next_char)
                index += 2
                continue
            if char == '"':
                in_double = False
            index += 1
            continue
        if char == "'":
            in_single = True
            output.append(char)
            index += 1
            continue
        if char == '"':
            in_double = True
            output.append(char)
            index += 1
            continue
        if char == "-" and next_char == "-":
            output.extend((" ", " "))
            index += 2
            while index < len(sql) and sql[index] not in "\r\n":
                output.append(" ")
                index += 1
            continue
        if char == "/" and next_char == "*":
            output.extend((" ", " "))
            index += 2
            while index < len(sql):
                if sql[index] == "*" and index + 1 < len(sql) and sql[index + 1] == "/":
                    output.extend((" ", " "))
                    index += 2
                    break
                output.append("\n" if sql[index] == "\n" else " ")
                index += 1
            continue
        output.append(char)
        index += 1
    return "".join(output)


def _table_aliases(table_name: str) -> set[str]:
    normalized = _normalize_identifier(table_name)
    if not normalized:
        return set()
    if "." in normalized:
        schema, table = normalized.split(".", 1)
        return {normalized, table, f"public.{table}" if schema == "public" else normalized}
    return {normalized, f"public.{normalized}"}


def _referenced_tables(sql: str) -> set[str]:
    """Return physical PostgreSQL tables referenced by a read-only query.

    Table scope is an authorization boundary, so derive it from SQL structure
    rather than keyword-shaped text. A regex cannot distinguish a real FROM
    clause from PostgreSQL expressions such as ``IS NOT DISTINCT FROM`` or
    ``EXTRACT(... FROM ...)``. Scope sources do make that distinction: CTEs
    and subqueries resolve to nested scopes, while physical relations remain
    ``Table`` expressions.
    """

    parse_sql = _strip_sql_comments(sql)
    try:
        tree = sqlglot.parse_one(parse_sql, dialect="postgres")
        root_scope = build_scope(tree)
    except ParseError as exc:
        raise SqlRunnerError(f"SQL 解析失败：{exc}", sql=sql) from exc
    if root_scope is None:
        raise SqlRunnerError("SQL 解析失败：无法建立查询作用域。", sql=sql)

    for function in tree.find_all(exp.Func):
        name = (
            str(function.name or "") if isinstance(function, exp.Anonymous) else str(function.sql_name() or "")
        ).lower()
        if isinstance(function.parent, exp.Dot):
            qualifier = function.parent.this
            schema_name = qualifier.name.lower() if isinstance(qualifier, exp.Identifier) else ""
            if schema_name != "pg_catalog":
                raise SqlRunnerError(f"SQL 包含未授权的 schema 限定函数：{name}", sql=sql)
        if name in _FORBIDDEN_FUNCTION_NAMES or name.startswith(_FORBIDDEN_FUNCTION_PREFIXES):
            raise SqlRunnerError(f"SQL 包含未授权的关系或系统读取函数：{name}", sql=sql)
        if isinstance(function, exp.Anonymous) and name not in _ALLOWED_ANONYMOUS_FUNCTION_NAMES:
            raise SqlRunnerError(f"SQL 包含未授权的自定义或未登记函数：{name}", sql=sql)

    tables: set[str] = set()
    for scope in root_scope.traverse():
        for source in scope.sources.values():
            if isinstance(source, Scope) and isinstance(source.expression, exp.UDTF):
                raise SqlRunnerError(
                    f"SQL FROM/JOIN 包含未授权的表函数：{source.expression.sql(dialect='postgres')}",
                    sql=sql,
                )
            if not isinstance(source, exp.Table):
                continue
            if not isinstance(source.this, exp.Identifier):
                raise SqlRunnerError(
                    f"SQL FROM/JOIN 包含未授权的表函数：{source.this.sql(dialect='postgres')}",
                    sql=sql,
                )
            parts = [source.catalog, source.db, source.name]
            normalized = _normalize_identifier(".".join(part for part in parts if part))
            if normalized:
                tables.add(normalized)
    return tables


def validate_readonly_sql(sql: str, *, allowed_tables: list[str]) -> str:
    """Validate SQL safety and table scope before execution."""

    clean_sql = extract_sql(sql)
    lowered = clean_sql.lower().strip()
    if not (lowered.startswith("select") or lowered.startswith("with")):
        raise SqlRunnerError("只允许执行 SELECT / WITH 查询。")
    balance = _paren_balance(clean_sql)
    if balance != 0:
        direction = "缺少右括号" if balance > 0 else "缺少左括号或 WITH/SELECT 起始结构"
        raise SqlRunnerError(
            f"SQL 结构不完整：括号不平衡（balance={balance}，{direction}）。",
            sql=clean_sql,
        )
    if ";" in clean_sql:
        raise SqlRunnerError("不允许执行多条 SQL。")
    if _DANGEROUS_RE.search(clean_sql):
        raise SqlRunnerError("SQL 包含非只读关键字，已拦截。")

    allowed_aliases: set[str] = set()
    for table_name in allowed_tables:
        allowed_aliases |= _table_aliases(table_name)

    referenced = _referenced_tables(clean_sql)
    blocked = sorted(ref for ref in referenced if ref not in allowed_aliases)
    if blocked:
        raise SqlRunnerError(f"SQL 引用了未授权数据表：{', '.join(blocked)}", sql=clean_sql)
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
    return [{column: _compact_cell(row.get(column), max_chars=max_cell_chars) for column in columns} for row in rows]


def _trim_profile_to_token_budget(
    profile: dict[str, Any],
    *,
    token_budget: int,
) -> dict[str, Any]:
    """Keep Profile evidence within its configured model-context budget."""

    budget = max(1, int(token_budget))
    if _estimate_tokens(profile) <= budget:
        return profile

    trimmed: dict[str, Any] = {}

    # Distribution evidence is the main defense against treating preview rows
    # as the full population, so retain its highest-frequency values first.
    group_counts = profile.get("group_counts")
    if isinstance(group_counts, dict):
        for column, raw_counts in group_counts.items():
            if not isinstance(raw_counts, dict):
                continue
            for value, count in raw_counts.items():
                candidate = deepcopy(trimmed)
                candidate.setdefault("group_counts", {}).setdefault(column, {})[value] = count
                if _estimate_tokens(candidate) > budget:
                    break
                trimmed = candidate

    for section in ("date_ranges", "numeric_ranges"):
        values = profile.get(section)
        if not isinstance(values, dict):
            continue
        for column, range_info in values.items():
            candidate = deepcopy(trimmed)
            candidate.setdefault(section, {})[column] = range_info
            if _estimate_tokens(candidate) <= budget:
                trimmed = candidate

    return trimmed


def _is_statement_timeout(exc: BaseException) -> bool:
    text_value = str(exc).lower()
    return "statement timeout" in text_value or "querycancelederror" in text_value or "query canceled" in text_value


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
                profile["group_counts"][column] = dict(
                    sorted(counts.items(), key=lambda item: item[1], reverse=True)[:100]
                )
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
    timeout_ms: int | None = None,
) -> SqlExecutionResult:
    """Execute validated SQL and return a completeness-aware result contract."""

    config = get_database_qa_config()
    full_row_cap = int(config.get("full_rows_hard_row_cap") or 200)
    requested_page_limit = max(
        1,
        min(
            int(limit or config.get("default_page_size") or 100),
            int(config.get("max_page_size") or 500),
        ),
    )
    materialize_limit = max(requested_page_limit, full_row_cap)
    if config.get("result_store_enabled", True):
        materialize_limit = max(
            materialize_limit,
            int(config.get("result_materialization_row_cap") or 5000),
        )
    safe_limit = min(requested_page_limit, materialize_limit)
    max_cell_chars = int(config.get("max_cell_chars_for_llm") or 500)
    full_column_cap = int(config.get("full_rows_hard_column_cap") or 20)
    full_token_budget = int(config.get("full_rows_token_budget") or 10000)
    effective_timeout_ms = int(timeout_ms or config.get("query_timeout_ms") or 30000)
    clean_sql = validate_readonly_sql(sql, allowed_tables=allowed_tables)
    engine = create_async_engine(database_source_url(source), pool_pre_ping=True)
    try:
        async with engine.connect() as conn:
            async with conn.begin():
                await conn.execute(text("SET TRANSACTION READ ONLY"))
                await conn.execute(text(f"SET LOCAL statement_timeout = '{effective_timeout_ms}ms'"))
                result = await conn.execute(
                    text(f"SELECT * FROM ({clean_sql}) AS puddingclaw_result LIMIT :limit_value"),
                    {"limit_value": materialize_limit + 1},
                )
                fetched_rows = [dict(row) for row in result.mappings().all()]
                rows = fetched_rows[:materialize_limit]
                columns = list(rows[0].keys()) if rows else list(result.keys())
                string_columns = [str(column) for column in columns]
                compact_all_rows = _compact_rows(rows, string_columns, max_cell_chars=max_cell_chars)
                estimated_tokens = _estimate_tokens({"columns": string_columns, "rows": compact_all_rows})
                materialized_all = len(fetched_rows) <= materialize_limit
                if materialized_all:
                    total_row_count = len(rows)
                else:
                    count_result = await conn.execute(
                        text(f"SELECT COUNT(*) AS count FROM ({clean_sql}) AS puddingclaw_count")
                    )
                    total_row_count = int(count_result.scalar_one() or 0)
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
                if not can_include_full and config.get("profile_enabled", True):
                    if materialized_all:
                        profile = _profile_from_rows(rows, string_columns)
                    else:
                        profile = await _profile_with_sql(conn, clean_sql, string_columns)
                    profile = _trim_profile_to_token_budget(
                        profile,
                        token_budget=int(config.get("profile_token_budget") or 3000),
                    )
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
    except sa_exc.DBAPIError as exc:
        if _is_statement_timeout(exc):
            raise SqlRunnerError(
                "SQL 执行超时：数据库在 "
                f"{effective_timeout_ms}ms 内没有返回结果。"
                "这通常表示生成 SQL 需要全表扫描、COUNT(DISTINCT)、正则/substring 计算或缺少索引；"
                "分页不会解决聚合计算超时，需要优化 SQL、增加索引/物化视图，或先缩小过滤范围。",
                sql=clean_sql,
            ) from exc
        raise SqlRunnerError(f"{type(exc).__name__}: {exc}", sql=clean_sql) from exc
    finally:
        await engine.dispose()
