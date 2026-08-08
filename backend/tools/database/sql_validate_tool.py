"""Validator for Agent-authored SQL submissions."""

from __future__ import annotations

import asyncio
import hashlib
import json
from typing import Any

import sqlglot
from langchain.tools import ToolRuntime
from langchain_core.tools import BaseTool
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from sqlglot import exp

from analytics.nl2sql.guardrails import detect_guardrail_conflicts
from analytics.nl2sql.schemas import DatabaseQueryRequest
from analytics.nl2sql.sql_runner import SqlRunnerError, unregistered_function_names, validate_readonly_sql
from analytics.nl2sql.table_router import route_database_tables, summarize_table_route
from analytics.semantic_runtime import compile_semantic_query_context, normalize_selected_semantic_asset_ids
from db import get_sessionmaker
from graph.database_evidence import database_evidence_registry
from graph.database_schema_evidence import database_schema_evidence_registry
from graph.database_sql_revision_resume import database_sql_revision_resume_registry
from graph.session_manager import session_manager
from knowledge.database_sources import database_source_url, get_database_source

from .models import DatabaseSqlValidateInput
from .scope import normalize_table_scope
from .spans import emit_database_span
from .sql_generate_tool import _trusted_user_scope_text


def _hash(value: Any) -> str:
    return "sha256:" + hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str).encode()
    ).hexdigest()


def _runtime_context(runtime: Any) -> dict[str, Any]:
    value = getattr(runtime, "context", None)
    return value if isinstance(value, dict) else {}


def _runtime_state(runtime: Any) -> dict[str, Any]:
    value = getattr(runtime, "state", None)
    return value if isinstance(value, dict) else {}


def _trusted_question(runtime: Any, context: dict[str, Any], state: dict[str, Any]) -> str:
    # Reuse the legacy path's server-owned provenance rules.  In particular,
    # delegated HumanMessages and internal rubric/completion prompts cannot
    # become trusted user scope merely because they are the latest message.
    return _trusted_user_scope_text(runtime).strip()


async def _load_columns(source: Any, table_names: list[str]) -> dict[str, set[str]]:
    engine = create_async_engine(database_source_url(source), pool_pre_ping=True)
    columns: dict[str, set[str]] = {}
    try:
        async with engine.connect() as connection:
            for raw_table in table_names:
                parts = [part.strip().strip('"') for part in str(raw_table).split(".") if part.strip()]
                schema, table = (parts[-2], parts[-1]) if len(parts) > 1 else ("public", parts[-1])
                result = await connection.execute(
                    text(
                        "SELECT column_name FROM information_schema.columns "
                        "WHERE table_schema = :schema AND table_name = :table"
                    ),
                    {"schema": schema, "table": table},
                )
                columns[normalize_table_scope(raw_table)] = {str(row.column_name).lower() for row in result}
    finally:
        await engine.dispose()
    return columns


async def _validate_pg_catalog_functions(sql: str, source: Any) -> None:
    """Authorize flexible PostgreSQL built-ins without trusting user UDFs."""

    names = unregistered_function_names(sql)
    if not names:
        return
    engine = create_async_engine(database_source_url(source), pool_pre_ping=True)
    authorized: set[str] = set()
    try:
        async with engine.connect() as connection:
            for name in sorted(names):
                result = await connection.execute(
                    text(
                        "SELECT EXISTS ("
                        "SELECT 1 FROM pg_catalog.pg_proc p "
                        "JOIN pg_catalog.pg_namespace n ON n.oid = p.pronamespace "
                        "WHERE n.nspname = 'pg_catalog' AND p.proname = :name"
                        ")"
                    ),
                    {"name": name},
                )
                if bool(result.scalar()):
                    authorized.add(name)
    finally:
        await engine.dispose()
    blocked = sorted(names - authorized)
    if blocked:
        raise SqlRunnerError(
            "SQL 包含未授权的用户函数或未知函数：" + ", ".join(blocked),
            sql=sql,
            error_code="sql_function_not_authorized",
        )


def _database_plan_error_detail(exc: BaseException) -> str:
    original = getattr(exc, "orig", None) or exc
    detail = str(original).strip().split("\n[SQL:", 1)[0].strip()
    return detail[:1200] or type(exc).__name__


async def _validate_postgres_plan(sql: str, source: Any) -> None:
    """Ask PostgreSQL to parse, bind and type-check SQL without executing it."""

    engine = create_async_engine(database_source_url(source), pool_pre_ping=True)
    try:
        async with engine.connect() as connection:
            await connection.execute(text("SET TRANSACTION READ ONLY"))
            await connection.execute(text("SET LOCAL statement_timeout = '5000ms'"))
            await asyncio.wait_for(
                connection.execute(text(f"EXPLAIN (FORMAT JSON) {sql}")),
                timeout=6.0,
            )
    except Exception as exc:
        raise SqlRunnerError(
            "PostgreSQL 无法规划该 SQL：" + _database_plan_error_detail(exc),
            sql=sql,
            error_code="sql_dialect_validation_failed",
        ) from exc
    finally:
        await engine.dispose()


def _cte_output_names(tree: exp.Expression) -> set[str]:
    names: set[str] = set()
    for cte in tree.find_all(exp.CTE):
        alias = cte.alias_or_name
        query = cte.this
        if isinstance(query, exp.Subquery):
            query = query.this
        if isinstance(query, exp.Expression):
            for projection in query.args.get("expressions") or []:
                if isinstance(projection, exp.Alias):
                    names.add(f"{alias}.{projection.alias}".lower())
                elif isinstance(projection, exp.Column):
                    names.add(f"{alias}.{projection.name}".lower())
    return names


def _is_descendant_of(node: exp.Expression, ancestor: exp.Expression) -> bool:
    current: exp.Expression | None = node
    while current is not None:
        if current is ancestor:
            return True
        current = current.parent
    return False


def _is_select_output_alias_reference(column: exp.Column) -> bool:
    """Return whether PostgreSQL resolves this column as a SELECT alias.

    Output aliases are valid in ORDER BY and GROUP BY, but not in WHERE or
    HAVING. Keeping this clause-aware avoids turning every Agent alias into a
    globally trusted physical column.
    """

    if column.table:
        return False
    select: exp.Select | None = None
    current = column.parent
    while current is not None:
        if isinstance(current, exp.Select):
            select = current
            break
        current = current.parent
    if select is None:
        return False
    aliases = {
        str(projection.alias or "").strip().lower()
        for projection in select.expressions
        if str(projection.alias or "").strip()
    }
    if str(column.name or "").strip().lower() not in aliases:
        return False
    return any(
        isinstance(clause, exp.Expression) and _is_descendant_of(column, clause)
        for clause in (select.args.get("order"), select.args.get("group"))
    )


async def _validate_real_columns(sql: str, source: Any, allowed_tables: list[str]) -> None:
    tree = sqlglot.parse_one(sql, read="postgres")
    columns = await _load_columns(source, allowed_tables)
    alias_to_table: dict[str, str] = {}
    derived_aliases = {item.alias_or_name.lower() for item in tree.find_all(exp.Subquery) if item.alias_or_name}
    cte_aliases = {item.alias_or_name.lower() for item in tree.find_all(exp.CTE) if item.alias_or_name}
    for table in tree.find_all(exp.Table):
        raw = ".".join(part for part in (table.db, table.name) if part)
        normalized = normalize_table_scope(raw)
        alias_to_table[table.alias_or_name.lower()] = normalized
        alias_to_table[table.name.lower()] = normalized
    known_unqualified = set().union(*columns.values()) if columns else set()
    cte_outputs = _cte_output_names(tree)
    for column in tree.find_all(exp.Column):
        name = str(column.name or "").strip().lower()
        if not name or name == "*":
            continue
        qualifier = str(column.table or "").strip().lower()
        if qualifier in derived_aliases or qualifier in cte_aliases:
            if f"{qualifier}.{name}" in cte_outputs:
                continue
            # A derived projection may be a wildcard or a database expression;
            # the database itself remains the authority for that output scope.
            continue
        if qualifier:
            table = alias_to_table.get(qualifier)
            if table is None:
                raise SqlRunnerError(f"SQL 使用了未解析的表别名或作用域：{qualifier}", sql=sql, error_code="column_scope_unresolved")
            if name not in columns.get(table, set()):
                raise SqlRunnerError(f"SQL 引用了不存在的列：{qualifier}.{name}", sql=sql, error_code="column_not_found")
        elif (
            name not in known_unqualified
            and not any(item.endswith(f".{name}") for item in cte_outputs)
            and not _is_select_output_alias_reference(column)
        ):
            raise SqlRunnerError(f"SQL 引用了不存在或无法解析的列：{name}", sql=sql, error_code="column_not_found")


def _literal_values(node: exp.Expression) -> list[str] | None:
    if isinstance(node, exp.Literal):
        return [str(node.this)]
    if isinstance(node, exp.In):
        values = [item for item in node.expressions if isinstance(item, exp.Literal)]
        return [str(item.this) for item in values] if len(values) == len(node.expressions) else None
    return None


def _column_predicate(node: exp.Expression, name: str) -> tuple[str, list[str] | None] | None:
    if isinstance(node, exp.EQ):
        pairs = ((node.this, node.expression), (node.expression, node.this))
        for left, right in pairs:
            if isinstance(left, exp.Column) and left.name.lower() == name:
                return str(left.table or "").lower(), _literal_values(right)
    if isinstance(node, exp.In) and isinstance(node.this, exp.Column) and node.this.name.lower() == name:
        return str(node.this.table or "").lower(), _literal_values(node)
    return None


def _is_negated(node: exp.Expression) -> bool:
    current = node.parent
    while current is not None:
        if isinstance(current, exp.Not):
            return True
        current = current.parent
    return False


def _eav_bindings(sql: str) -> tuple[list[dict[str, str]], list[str]]:
    tree = sqlglot.parse_one(sql, read="postgres")
    predicate_types = (exp.EQ, exp.In, exp.Like, exp.ILike, exp.RegexpLike, exp.GT, exp.GTE, exp.LT, exp.LTE, exp.Between)
    value_nodes = [
        node
        for node in tree.walk()
        if isinstance(node, predicate_types)
        and not _is_negated(node)
        and any(isinstance(item, exp.Column) and item.name.lower() == "type_value" for item in node.walk())
    ]
    bindings: list[dict[str, str]] = []
    unprovable: list[str] = []
    for value_node in value_nodes:
        value_alias, values = _column_predicate(value_node, "type_value") or ("", None)
        if values is None or not values:
            unprovable.append(value_node.sql(dialect="postgres"))
            continue
        parent: exp.Expression | None = value_node.parent
        while parent is not None and not isinstance(parent, exp.And):
            parent = parent.parent
        container = parent or tree
        type_candidates: list[str] = []
        for node in container.walk():
            predicate = _column_predicate(node, "type_name")
            if predicate is None:
                continue
            alias, names = predicate
            if alias == value_alias and names:
                type_candidates.extend(names)
        if len(set(type_candidates)) != 1:
            unprovable.append(value_node.sql(dialect="postgres"))
            continue
        for value in values:
            bindings.append({"alias": value_alias, "type_name": type_candidates[0], "type_value": value})
    return bindings, sorted(set(unprovable))


def _observed_eav_values(
    *,
    evidence_payload: dict[str, Any] | None,
    schema_payloads: list[dict[str, Any]],
) -> tuple[dict[tuple[str, str], dict[str, Any]], set[tuple[str, str]], list[str]]:
    profiles: dict[tuple[str, str], dict[str, Any]] = {}
    type_names: set[tuple[str, str]] = set()
    revisions: list[str] = []
    for item in (evidence_payload or {}).get("observations") or []:
        if not isinstance(item, dict):
            continue
        name = str(item.get("type_name") or "")
        table = normalize_table_scope(str(item.get("table") or ""))
        if name and table:
            key = (table, name)
            type_names.add(key)
            profiles[key] = dict(item)
            if item.get("profile_revision"):
                revisions.append(str(item["profile_revision"]))
    for receipt in schema_payloads:
        evidence = receipt.get("evidence") if isinstance(receipt.get("evidence"), dict) else {}
        mode = str(evidence.get("mode") or "")
        name = str(evidence.get("type_name") or "")
        table = normalize_table_scope(str(evidence.get("table_name") or receipt.get("table_name") or ""))
        rows = [item for item in evidence.get("rows") or [] if isinstance(item, dict)]
        if mode == "type_names":
            type_names.update(
                (table, str(item.get("type_name") or ""))
                for item in rows
                if table and str(item.get("type_name") or "")
            )
        if name and table:
            key = (table, name)
            type_names.add(key)
            values = [str(item.get("type_value") or "") for item in rows if str(item.get("type_value") or "")]
            profile = evidence.get("profile") if isinstance(evidence.get("profile"), dict) else {}
            current = profiles.setdefault(key, {"table": table, "type_name": name, "values": [], "complete": False})
            current["values"] = sorted(set(current.get("values") or []) | set(values))
            current["complete"] = bool(mode == "value_profile" and int(profile.get("distinct_count") or 0) <= len(current["values"]))
            if receipt.get("sha256"):
                revisions.append(str(receipt["sha256"]))
    return profiles, type_names, sorted(set(revisions))


def _eav_alias_tables(sql: str) -> dict[str, str]:
    tree = sqlglot.parse_one(sql, read="postgres")
    aliases: dict[str, str] = {}
    for table in tree.find_all(exp.Table):
        raw = ".".join(part for part in (table.db, table.name) if part)
        normalized = normalize_table_scope(raw)
        aliases[table.alias_or_name.lower()] = normalized
        aliases[table.name.lower()] = normalized
    return aliases


class DatabaseSqlValidateTool(BaseTool):
    name: str = "database_sql_validate"
    description: str = (
        "Validate and register untrusted Agent-authored SQL. Hard rejection covers authorization, dangerous operations, "
        "and database-native parse/bind/type errors that make execution impossible. Business-semantic, EAV, Evidence, "
        "and Guardrail findings remain advisory warnings for the Agent. This tool never calls an LLM or rewrites SQL. "
        "Execute a successful submission with its paired Receipt."
    )
    args_schema: type[BaseModel] = DatabaseSqlValidateInput
    risk_level: str = "safe"
    session_id: str = ""
    query_id: str = ""

    class Config:
        arbitrary_types_allowed = True

    async def _arun(
        self,
        sql: str,
        database_source_id: str | None = None,
        table_names: list[str] | None = None,
        selected_semantic_asset_ids: list[str] | None = None,
        evidence_search_id: str = "",
        schema_evidence_receipt_ids: list[str] | None = None,
        runtime: ToolRuntime | None = None,
    ) -> str:
        context = _runtime_context(runtime)
        state = _runtime_state(runtime)
        run_id = str(context.get("run_id") or "")
        goal_id = str(context.get("goal_id") or "")
        goal_revision = context.get("goal_revision")
        trusted_question = _trusted_question(runtime, context, state)
        if not self.session_id or (not run_id and not goal_id) or not trusted_question:
            return json.dumps({"status": "rejected", "code": "semantic_context_unavailable", "recoverable": False, "stage": "trusted_runtime"}, ensure_ascii=False)
        model_id = str(state.get("analytics_model_id") or "").strip() or None
        selected_ids = list(selected_semantic_asset_ids or [])
        try:
            warnings: list[dict[str, Any]] = []
            normalized_ids = selected_ids
            allowed_ids = {str(item).strip() for item in state.get("allowed_semantic_asset_ids") or [] if str(item).strip()}
            if selected_ids and "allowed_semantic_asset_ids" not in state:
                warnings.append({
                    "code": "semantic_assets_unavailable",
                    "message": "当前分析模型的语义资产目录不可用；已忽略 Agent 选择的语义资产。",
                })
                normalized_ids = []
            elif allowed_ids:
                normalized_ids, error = normalize_selected_semantic_asset_ids(selected_ids, allowed_ids)
                if error:
                    warnings.append({"code": "semantic_assets_unresolved", "message": error})
                    normalized_ids = []
            request = DatabaseQueryRequest(
                question=trusted_question,
                database_source_id=database_source_id,
                table_names=list(table_names or []),
                model_id=model_id,
                measure_ids=normalized_ids,
            )
            sessionmaker = get_sessionmaker()
            async with sessionmaker() as session:
                route = await route_database_tables(session, request)
                source = await get_database_source(session, route.database_source_id)
            evidence_item = database_evidence_registry.get(
                evidence_search_id,
                session_id=self.session_id,
                query_id=self.query_id,
                run_id=run_id,
                goal_id=goal_id,
                goal_revision=goal_revision,
                database_source_id=route.database_source_id,
                allowed_tables=route.table_names,
                trusted_question_sha256=_hash(trusted_question),
                analytics_model_id=model_id or "",
            ) if evidence_search_id else None
            if evidence_search_id and evidence_item is None:
                warnings.append({
                    "code": "evidence_unavailable",
                    "message": "Evidence 已过期或上下文不匹配；SQL 仍按当前权限边界校验。",
                })
            semantic_hash = ""
            model_revision = ""
            semantic_trace: dict[str, Any] = {}
            resolved_asset_ids: list[str] = []
            try:
                semantic_context = compile_semantic_query_context(
                    question=trusted_question,
                    model_id=model_id,
                    selected_semantic_asset_ids=normalized_ids,
                    strict_selected_ids=bool(normalized_ids),
                )
                semantic_hash = semantic_context.semantic_hash
                model_revision = str(semantic_context.model_version or "")
                semantic_trace = semantic_context.trace
                resolved_asset_ids = list(semantic_context.semantic_asset_ids)
            except Exception as exc:
                warnings.append({
                    "code": "semantic_context_unavailable",
                    "message": f"语义上下文不可用：{type(exc).__name__}: {exc}",
                })
            clean_sql = validate_readonly_sql(
                sql,
                allowed_tables=route.table_names,
                require_schema_qualified=True,
                allow_unregistered_functions=True,
            )
            await _validate_pg_catalog_functions(clean_sql, source)
            native_plan_validated = "postgres" in str(route.dialect or "").lower()
            if native_plan_validated:
                await _validate_postgres_plan(clean_sql, source)
            else:
                try:
                    await _validate_real_columns(clean_sql, source, route.table_names)
                except Exception as exc:
                    warnings.append({
                        "code": getattr(exc, "error_code", "column_diagnostic_unavailable"),
                        "message": str(exc),
                    })
            bindings, unprovable = _eav_bindings(clean_sql)
            if unprovable:
                warnings.append({
                    "code": "eav_binding_unproven",
                    "message": "EAV 条件未静态绑定到唯一 type_name：" + ", ".join(unprovable),
                })
            schema_payloads: list[dict[str, Any]] = []
            for receipt_id in schema_evidence_receipt_ids or []:
                receipt = database_schema_evidence_registry.get_discovery(
                    receipt_id,
                    session_id=self.session_id,
                    query_id=self.query_id,
                    run_id=run_id,
                    goal_id=goal_id,
                    goal_revision=goal_revision,
                )
                if receipt is None:
                    warnings.append({
                        "code": "schema_evidence_unavailable",
                        "message": f"Schema Evidence 不可用：{receipt_id}",
                    })
                    continue
                evidence = receipt.get("evidence") or {}
                if str(evidence.get("database_source_id") or "") != route.database_source_id or normalize_table_scope(str(evidence.get("table_name") or "")) not in {normalize_table_scope(item) for item in route.table_names}:
                    warnings.append({
                        "code": "schema_evidence_scope_mismatch",
                        "message": f"Schema Evidence 不属于当前数据源/授权表：{receipt_id}",
                    })
                    continue
                schema_payloads.append(receipt)
            profiles, known_type_names, profile_revisions = _observed_eav_values(
                evidence_payload=(evidence_item or {}).get("payload") if evidence_item else None,
                schema_payloads=schema_payloads,
            )
            alias_tables = _eav_alias_tables(clean_sql)
            route_scopes = {normalize_table_scope(item) for item in route.table_names}
            for binding in bindings:
                table_scope = alias_tables.get(binding["alias"])
                if not table_scope or table_scope not in route_scopes:
                    warnings.append({
                        "code": "eav_table_binding_unproven",
                        "message": "EAV 条件未静态绑定到当前授权物理表。",
                    })
                    continue
                type_name = binding["type_name"]
                value = binding["type_value"]
                evidence_key = (table_scope, type_name)
                if evidence_key not in known_type_names:
                    warnings.append({
                        "code": "eav_type_name_unobserved",
                        "message": f"当前证据未观测到 EAV type_name：{table_scope}.{type_name}",
                    })
                    continue
                profile = profiles.get(evidence_key)
                values = set(str(item) for item in (profile or {}).get("values") or [])
                if value in values:
                    continue
                if profile and bool(profile.get("complete")):
                    warnings.append({
                        "code": "eav_literal_unobserved",
                        "message": f"当前 revision 未观测到 EAV 字面量：{type_name}={value}",
                    })
                    continue
                if value in trusted_question:
                    continue
                warnings.append({
                    "code": "eav_literal_unproven",
                    "message": f"当前证据未证明 EAV 字面量：{type_name}={value}",
                })
            conflicts = []
            try:
                conflicts = detect_guardrail_conflicts(
                    clean_sql,
                    source_name=route.source_name,
                    route=route,
                    semantic_trace=semantic_trace,
                    question=trusted_question,
                )
                warnings.extend(
                    {
                        "code": "semantic_guardrail_advisory",
                        "rule_id": item.rule_id,
                        "legacy_action": item.action,
                        "message": item.message,
                    }
                    for item in conflicts
                )
            except Exception as exc:
                warnings.append({
                    "code": "semantic_guardrail_unavailable",
                    "message": f"语义诊断不可用：{type(exc).__name__}: {exc}",
                })
            if not getattr(session_manager, "is_initialized", False):
                raise PermissionError("当前 permission policy 不可用，拒绝签发 Agent Receipt")
            try:
                permission_epoch = int(session_manager.get_permission_policy(self.session_id)["policy_epoch"])
            except (FileNotFoundError, KeyError, RuntimeError, TypeError, ValueError) as exc:
                raise PermissionError("当前 permission policy 不可用，拒绝签发 Agent Receipt") from exc
            route_hash = _hash(summarize_table_route(route))
            question_hash = _hash(trusted_question)
            submission = database_sql_revision_resume_registry.register_submission(
                session_id=self.session_id,
                query_id=self.query_id,
                run_id=run_id,
                goal_id=goal_id,
                goal_revision=goal_revision,
                sql=clean_sql,
                request={
                    "database_source_id": route.database_source_id,
                    "table_names": list(route.table_names),
                    "trusted_question_sha256": question_hash,
                    "trusted_question": trusted_question,
                    "analytics_model_id": model_id or "",
                    "analytics_model_revision": model_revision,
                    "selected_semantic_asset_ids": normalized_ids,
                    "resolved_semantic_asset_ids": resolved_asset_ids,
                    "semantic_context_hash": semantic_hash,
                    "route_hash": route_hash,
                    "evidence_search_id": evidence_search_id,
                    "schema_evidence_receipt_ids": list(schema_evidence_receipt_ids or []),
                    "profile_revisions": profile_revisions,
                    "permission_epoch": permission_epoch,
                    "provenance": "agent_authored",
                },
            )
            receipt = database_sql_revision_resume_registry.register_agent_validation_receipt(
                submission=submission,
                database_source_id=route.database_source_id,
                allowed_tables=route.table_names,
                metadata={
                    "trusted_question_sha256": question_hash,
                    "analytics_model_id": model_id or "",
                    "analytics_model_revision": model_revision,
                    "semantic_context_hash": semantic_hash,
                    "route_hash": route_hash,
                    "evidence_search_id": evidence_search_id,
                    "schema_evidence_receipt_ids": list(schema_evidence_receipt_ids or []),
                    "profile_revisions": profile_revisions,
                    "permission_epoch": permission_epoch,
                    "semantic_validation_status": "advisory",
                    "semantic_guardrail_ids": sorted({item.rule_id for item in conflicts}),
                    "semantic_evidence_refs": sorted(set(resolved_asset_ids + ([evidence_search_id] if evidence_search_id else []) + list(schema_evidence_receipt_ids or []))),
                },
            )
            warnings = [{**item, "blocking": False} for item in warnings]
            result = {
                "status": "passed",
                "valid": True,
                "execution_allowed": True,
                "sql_submission_id": submission.id,
                "validation_receipt_id": receipt.id,
                "sql_sha256": submission.sql_sha256,
                "database_source_id": route.database_source_id,
                "allowed_tables": route.table_names,
                "semantic_context_hash": semantic_hash,
                "profile_revisions": profile_revisions,
                "semantic_guardrail_ids": receipt.semantic_guardrail_ids,
                "warnings": warnings,
                "warning_policy": "advisory_only_agent_decides",
                "provenance": "agent_authored",
            }
            emit_database_span(
                "sql_validate",
                result,
                metadata={"database_source_id": route.database_source_id, "agent_path": True},
            )
            return json.dumps(result, ensure_ascii=False, indent=2)
        except SqlRunnerError as exc:
            return json.dumps({"status": "rejected", "code": exc.error_code, "recoverable": exc.error_code in {"eav_evidence_required", "column_not_found", "column_scope_unresolved", "sql_dialect_validation_failed"}, "stage": "physical_evidence" if exc.error_code.startswith("eav_") else "sql_validation", "message": str(exc)}, ensure_ascii=False, indent=2)
        except PermissionError as exc:
            return json.dumps({"status": "rejected", "code": "authorization_context_unavailable", "recoverable": False, "stage": "authorization", "message": str(exc)}, ensure_ascii=False, indent=2)
        except Exception as exc:
            return json.dumps({"status": "rejected", "code": "sql_validation_failed", "recoverable": False, "stage": "authorization_or_syntax", "message": str(exc)}, ensure_ascii=False, indent=2)

    def _run(self, **kwargs: object) -> str:
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(self._arun(**kwargs))  # type: ignore[arg-type]
        return json.dumps({"status": "rejected", "code": "async_only"}, ensure_ascii=False)
