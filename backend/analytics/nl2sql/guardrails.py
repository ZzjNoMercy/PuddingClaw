"""Configurable SQL guardrails for NL2SQL execution."""

from __future__ import annotations

import re
import shutil
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

import sqlglot
import yaml
from pydantic import BaseModel, Field
from sqlglot import exp

from analytics.nl2sql.guardrail_runtime import detector_failed, scope_status

from runtime_identity.paths import PuddingClawPaths

GUARDRAILS_ROOT = PuddingClawPaths.from_environment().user_definitions() / "sql-guardrails"
GUARDRAILS_RULES_DIR = GUARDRAILS_ROOT / "rules"
GUARDRAILS_DRAFTS_DIR = GUARDRAILS_ROOT / "drafts"
GUARDRAIL_FILENAME = "guardrail.md"


ActionType = Literal["rewrite", "block", "warn"]
TableScopeMode = Literal["any", "all"]


class GuardrailTableScope(BaseModel):
    mode: TableScopeMode = "any"
    values: list[str] = Field(default_factory=list)


class GuardrailScope(BaseModel):
    table_scope: GuardrailTableScope = Field(default_factory=GuardrailTableScope)
    semantic_assets: list[str] = Field(default_factory=list)
    intent_any: list[str] = Field(default_factory=list)


class GuardrailAction(BaseModel):
    type: ActionType = "rewrite"
    message: str = ""


class GuardrailRule(BaseModel):
    id: str
    name: str
    enabled: bool = True
    type: str
    scope: GuardrailScope = Field(default_factory=GuardrailScope)
    params: dict[str, Any] = Field(default_factory=dict)
    action: GuardrailAction = Field(default_factory=GuardrailAction)


class GuardrailConflict(BaseModel):
    rule_id: str
    rule_name: str
    rule_type: str
    action: ActionType
    message: str


class GuardrailRuleSet(BaseModel):
    guardrails: list[GuardrailRule] = Field(default_factory=list)
    diagnostics: list[dict[str, str]] = Field(default_factory=list)


RULE_TYPE_DEFINITIONS: dict[str, dict[str, Any]] = {
    "forbid_sql_pattern": {
        "label": "禁止 SQL 模式",
        "description": "正则命中 SQL 后触发，可配置例外片段。",
        "fields": [
            {"path": "params.pattern", "label": "禁止 SQL 正则", "type": "string", "required": True},
            {"path": "params.unless_contains", "label": "例外包含文本", "type": "string", "required": False},
            {"path": "params.unless_pattern", "label": "例外 SQL 正则", "type": "string", "required": False},
            {"path": "params.flags", "label": "正则 flags", "type": "string_array", "required": False},
        ],
    },
    "require_sql_contains": {
        "label": "要求 SQL 片段",
        "description": "命中 scope 后要求 SQL 包含指定文本。",
        "fields": [
            {"path": "params.contains", "label": "必须包含", "type": "string", "required": True},
            {"path": "params.when_contains_any", "label": "触发关键词", "type": "string_array", "required": False},
        ],
    },
    "require_table_when_available": {
        "label": "路由有表时必须使用",
        "description": "当路由包含某表时，生成 SQL 必须使用该表。",
        "fields": [
            {"path": "params.required_table", "label": "必须使用表", "type": "string", "required": True},
            {"path": "params.fallback_table", "label": "fallback 表", "type": "string", "required": False},
        ],
    },
    "require_group_by": {
        "label": "要求 GROUP BY 字段",
        "description": "用于确保聚合粒度符合业务口径。",
        "fields": [
            {"path": "params.require_columns", "label": "必须包含字段", "type": "string_array", "required": False},
            {"path": "params.forbidden_columns_only", "label": "禁止仅按字段分组", "type": "string_array", "required": False},
        ],
    },
    "forbid_exists_distinct_pattern": {
        "label": "禁止 EXISTS + DISTINCT 慢查询",
        "description": "拦截 EAV 表上多层 EXISTS 与 COUNT(DISTINCT) 组合。",
        "fields": [
            {"path": "params.table", "label": "表名", "type": "string", "required": True},
            {"path": "params.distinct_column", "label": "distinct 字段", "type": "string", "required": True},
            {"path": "params.min_exists_count", "label": "EXISTS 最小数量", "type": "number", "required": False},
        ],
    },
}


DEFAULT_GUARDRAILS = GuardrailRuleSet(
    guardrails=[
        GuardrailRule(
            id="launch_time_no_car_name_year",
            name="上市时间不能从款型名推断",
            type="forbid_sql_pattern",
            scope=GuardrailScope(semantic_assets=["dimension:launch_time"]),
            params={
                "pattern": r"\bcar_name\b\s+(?:LIKE|ILIKE)\s+['\"]\d{2}款%",
                "unless_contains": "type_name = '上市时间'",
            },
            action=GuardrailAction(
                type="rewrite",
                message="命中语义资产“上市时间”，必须改用 type_name = '上市时间' 的 type_value 过滤真实上市日期。",
            ),
        ),
        GuardrailRule(
            id="air_suspension_reference_type_value",
            name="空气悬架使用可调悬架种类字段",
            type="require_sql_contains",
            scope=GuardrailScope(semantic_assets=["measure:config_rate:references/air_suspension"]),
            params={
                "contains": "type_name = '可调悬架种类'",
                "when_contains_any": ["空气悬架", "空气悬挂"],
            },
            action=GuardrailAction(
                type="rewrite",
                message="空气悬架必须使用 type_name = '可调悬架种类' 且 type_value 包含 '空气悬架'。",
            ),
        ),
        GuardrailRule(
            id="voltage_platform_400v_physical_value",
            name="400V 平台使用真实枚举值",
            type="forbid_sql_pattern",
            scope=GuardrailScope(
                semantic_assets=["measure:config_rate"],
                intent_any=["高压平台", "电压平台", "400V"],
            ),
            params={"pattern": r"['\"]400V['\"]"},
            action=GuardrailAction(
                type="rewrite",
                message="高压平台物理枚举必须使用 '400V平台'，不能精确匹配不存在的 '400V'。",
            ),
        ),
        GuardrailRule(
            id="voltage_platform_800v_physical_value",
            name="800V 平台使用真实枚举值",
            type="forbid_sql_pattern",
            scope=GuardrailScope(
                semantic_assets=["measure:config_rate"],
                intent_any=["高压平台", "电压平台", "800V"],
            ),
            params={"pattern": r"['\"]800V['\"]"},
            action=GuardrailAction(
                type="rewrite",
                message="高压平台物理枚举必须使用 '800V平台'，不能精确匹配不存在的 '800V'。",
            ),
        ),
        GuardrailRule(
            id="rear_screen_physical_type_name",
            name="后排屏使用真实物理字段",
            type="require_sql_contains",
            scope=GuardrailScope(
                semantic_assets=["measure:config_rate"],
                intent_any=["后排多媒体屏", "后排屏"],
            ),
            params={"contains": "type_name = '后排多媒体屏幕数量'"},
            action=GuardrailAction(
                type="rewrite",
                message="后排多媒体屏必须使用真实字段 type_name = '后排多媒体屏幕数量'。",
            ),
        ),
        GuardrailRule(
            id="config_rate_use_model_base_denominator",
            name="配置率优先使用款型基础表分母",
            type="require_table_when_available",
            scope=GuardrailScope(
                table_scope=GuardrailTableScope(mode="all", values=["vehicle_params", "vehicle_model_base"]),
                semantic_assets=["measure:config_rate"],
                intent_any=["配置率", "搭载率", "渗透率", "配备率", "占比"],
            ),
            params={"required_table": "vehicle_model_base", "fallback_table": "vehicle_params"},
            action=GuardrailAction(
                type="rewrite",
                message="配置率必须使用 vehicle_model_base 先筛选分母款型，再 JOIN vehicle_params 判断配置明细。",
            ),
        ),
        GuardrailRule(
            id="config_rate_model_key_group",
            name="配置率款型颗粒度分组",
            type="require_group_by",
            scope=GuardrailScope(
                table_scope=GuardrailTableScope(mode="any", values=["vehicle_params"]),
                semantic_assets=["measure:config_rate"],
                intent_any=["配置率", "搭载率", "渗透率", "配备率", "占比"],
            ),
            params={
                "forbidden_columns_only": ["car_name"],
            },
            action=GuardrailAction(
                type="rewrite",
                message="默认款型颗粒度必须按 brand + serial_name + car_name 分组。",
            ),
        ),
        GuardrailRule(
            id="config_rate_no_exists_distinct",
            name="配置率禁止多层 EXISTS DISTINCT 慢查询",
            type="forbid_exists_distinct_pattern",
            scope=GuardrailScope(
                table_scope=GuardrailTableScope(mode="any", values=["vehicle_params"]),
                semantic_assets=["measure:config_rate"],
                intent_any=["配置率", "搭载率", "渗透率", "配备率", "占比"],
            ),
            params={"table": "vehicle_params", "distinct_column": "car_name", "min_exists_count": 2},
            action=GuardrailAction(
                type="rewrite",
                message=(
                    "配置率不要使用 DISTINCT car_name + 多层 EXISTS/NOT EXISTS 自关联。"
                    "请一次扫描相关 type_name，并按 brand, serial_name, car_name 聚合 BOOL_OR flags。"
                ),
            ),
        ),
        GuardrailRule(
            id="postgres_count_distinct_nullable_tuple_after_left_join",
            name="PostgreSQL LEFT JOIN 后禁止直接 COUNT DISTINCT 右表 nullable tuple",
            type="forbid_sql_pattern",
            scope=GuardrailScope(),
            params={
                "pattern": (
                    r"(?=[\s\S]*\bLEFT\s+JOIN\b)"
                    r"(?=[\s\S]*\bCOUNT\s*\(\s*DISTINCT\s*\([^)]*\.[^)]*,[^)]*\)\s*\))"
                ),
                "unless_pattern": (
                    r"\bCOUNT\s*\(\s*DISTINCT\s*\([^)]*\.[^)]*,[^)]*\)\s*\)\s*"
                    r"FILTER\s*\(\s*WHERE\s+[^)]*\.[A-Za-z_][\w]*\s+IS\s+NOT\s+NULL\s*\)"
                ),
            },
            action=GuardrailAction(
                type="rewrite",
                message=(
                    "PostgreSQL 会把 ROW(NULL, NULL, ...) 当作一个 distinct tuple。"
                    "LEFT JOIN 后不要直接 COUNT(DISTINCT (right.col1, right.col2...))，否则未命中行可能多算 1。"
                    "请改用 FILTER (WHERE right.key IS NOT NULL)、COUNT(right.key)，或先在子查询/CTE 中过滤非空后再计数。"
                ),
            ),
        ),
    ]
)


def _normalize_identifier(value: str) -> str:
    return str(value or "").strip().strip('"').strip("() \n\t;").split(".")[-1].strip('"').lower()


def _as_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    return [str(value).strip()] if str(value).strip() else []


def _route_table_names(route: Any) -> set[str]:
    names: set[str] = set()
    for table_name in getattr(route, "table_names", []) or []:
        value = str(table_name).strip().strip('"')
        if not value:
            continue
        names.add(value)
        names.add(value.split(".")[-1])
    return names


def semantic_asset_ids(semantic_trace: dict[str, Any]) -> set[str]:
    ids: set[str] = set()
    if not isinstance(semantic_trace, dict):
        return ids
    for key in ("matched", "references"):
        items = semantic_trace.get(key)
        if not isinstance(items, list):
            continue
        for item in items:
            if isinstance(item, dict) and item.get("id"):
                ids.add(str(item["id"]))
    return ids


def _parse_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    if not text.startswith("---"):
        return {}, text
    parts = text.split("---", 2)
    if len(parts) < 3:
        return {}, text
    raw_meta = yaml.safe_load(parts[1]) or {}
    if not isinstance(raw_meta, dict):
        raw_meta = {}
    return raw_meta, parts[2].lstrip("\n")


def _rule_doc_path(rule_id: str) -> Path:
    safe_id = re.sub(r"[^0-9A-Za-z_\-]+", "_", rule_id.strip()).strip("_") or "guardrail"
    return GUARDRAILS_RULES_DIR / safe_id / GUARDRAIL_FILENAME


def _rule_from_markdown(path: Path) -> GuardrailRule:
    rule, _body, _content = _read_rule_markdown(path)
    return rule


def _read_rule_markdown(path: Path) -> tuple[GuardrailRule, str, str]:
    text = path.read_text(encoding="utf-8")
    meta, body = _parse_frontmatter(text)
    if meta.get("formatter") != "sql-guardrail":
        raise ValueError("formatter must be sql-guardrail")
    payload = {
        "id": meta.get("id"),
        "name": meta.get("name"),
        "enabled": meta.get("enabled", True),
        "type": meta.get("type"),
        "scope": meta.get("scope") or {},
        "params": meta.get("params") or {},
        "action": meta.get("action") or {},
    }
    return GuardrailRule.model_validate(payload), body, text


def _rule_document_payload(rule: GuardrailRule, path: Path, body: str, content: str) -> dict[str, Any]:
    data = rule.model_dump(mode="json")
    data.update(
        {
            "document_path": path.relative_to(GUARDRAILS_ROOT.parent).as_posix(),
            "document_body": body,
            "document_content": content,
        }
    )
    return data


def _guardrail_body(rule: GuardrailRule) -> str:
    table_values = rule.scope.table_scope.values
    semantic_assets = rule.scope.semantic_assets
    intent_any = rule.scope.intent_any
    params_preview = yaml.safe_dump(rule.params, allow_unicode=True, sort_keys=False).strip() or "{}"
    return (
        f"# {rule.name}\n\n"
        "## 业务约束\n\n"
        f"{rule.action.message or '描述这条 SQL 守卫要保护的业务口径。'}\n\n"
        "## 命中范围\n\n"
        f"- 表范围：{rule.scope.table_scope.mode} / {', '.join(table_values) if table_values else '不限制'}\n"
        f"- 语义资产：{', '.join(semantic_assets) if semantic_assets else '不限制'}\n\n"
        f"- 问题意图：{' / '.join(intent_any) if intent_any else '不限制'}\n\n"
        "## Detector 参数\n\n"
        "```yaml\n"
        f"{params_preview}\n"
        "```\n\n"
        "## 推荐处理\n\n"
        f"{rule.action.message or '命中后按 action 配置处理。'}\n\n"
        "## 风险说明\n\n"
        "- frontmatter 是机器执行配置，正文只用于人工审核和 LLM 理解。\n"
        "- 修改正文不会改变执行逻辑；需要同步修改 frontmatter。\n"
    )


def _rule_to_markdown(rule: GuardrailRule, body: str | None = None) -> str:
    now_text = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    metadata = {
        "formatter": "sql-guardrail",
        "id": rule.id,
        "name": rule.name,
        "enabled": rule.enabled,
        "version": "0.1.0",
        "type": rule.type,
        "scope": rule.scope.model_dump(mode="json"),
        "params": rule.params,
        "action": rule.action.model_dump(mode="json"),
        "updated_at": now_text,
    }
    frontmatter = yaml.safe_dump(metadata, allow_unicode=True, sort_keys=False).strip()
    return f"---\n{frontmatter}\n---\n\n{body if body is not None else _guardrail_body(rule)}"


def _write_rule_markdown(rule: GuardrailRule, body: str | None = None) -> Path:
    target = _rule_doc_path(rule.id)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(_rule_to_markdown(rule, body=body), encoding="utf-8")
    return target


def _write_raw_rule_markdown(content: str, *, previous_id: str | None = None) -> dict[str, Any]:
    temp_rule, body, _raw = _read_rule_markdown_from_text(content)
    if previous_id and previous_id != temp_rule.id:
        previous_path = _rule_doc_path(previous_id)
        if previous_path.exists():
            previous_path.unlink()
            try:
                previous_path.parent.rmdir()
            except OSError:
                pass
    target = _rule_doc_path(temp_rule.id)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    return _rule_document_payload(temp_rule, target, body, content)


def _read_rule_markdown_from_text(content: str) -> tuple[GuardrailRule, str, str]:
    meta, body = _parse_frontmatter(content)
    if meta.get("formatter") != "sql-guardrail":
        raise ValueError("formatter must be sql-guardrail")
    payload = {
        "id": meta.get("id"),
        "name": meta.get("name"),
        "enabled": meta.get("enabled", True),
        "type": meta.get("type"),
        "scope": meta.get("scope") or {},
        "params": meta.get("params") or {},
        "action": meta.get("action") or {},
    }
    return GuardrailRule.model_validate(payload), body, content


def _ensure_default_rule_docs() -> None:
    GUARDRAILS_RULES_DIR.mkdir(parents=True, exist_ok=True)
    GUARDRAILS_DRAFTS_DIR.mkdir(parents=True, exist_ok=True)
    if list(GUARDRAILS_RULES_DIR.glob(f"**/{GUARDRAIL_FILENAME}")):
        return
    for rule in DEFAULT_GUARDRAILS.guardrails:
        _write_rule_markdown(rule)


def _load_markdown_guardrails() -> GuardrailRuleSet:
    _ensure_default_rule_docs()
    rules: list[GuardrailRule] = []
    diagnostics: list[dict[str, str]] = []
    seen_ids: set[str] = set()
    for path in sorted(GUARDRAILS_RULES_DIR.glob(f"**/{GUARDRAIL_FILENAME}")):
        try:
            rule = _rule_from_markdown(path)
            if rule.id in seen_ids:
                diagnostics.append({"path": path.relative_to(GUARDRAILS_ROOT.parent).as_posix(), "error": f"duplicate id: {rule.id}"})
                continue
            seen_ids.add(rule.id)
            rules.append(rule)
        except Exception as exc:
            diagnostics.append({"path": path.relative_to(GUARDRAILS_ROOT.parent).as_posix(), "error": str(exc)})
    return GuardrailRuleSet(guardrails=rules, diagnostics=diagnostics)


def load_guardrail_rules() -> GuardrailRuleSet:
    return _load_markdown_guardrails()


def save_guardrail_rules(rule_set: GuardrailRuleSet) -> GuardrailRuleSet:
    GUARDRAILS_RULES_DIR.mkdir(parents=True, exist_ok=True)
    for rule in rule_set.guardrails:
        _write_rule_markdown(rule)
    return rule_set


def list_guardrail_rules() -> dict[str, Any]:
    rule_set = load_guardrail_rules()
    items: list[dict[str, Any]] = []
    for rule in rule_set.guardrails:
        path = _rule_doc_path(rule.id)
        if path.exists():
            try:
                parsed_rule, body, content = _read_rule_markdown(path)
                items.append(_rule_document_payload(parsed_rule, path, body, content))
                continue
            except Exception:
                pass
        items.append(rule.model_dump(mode="json"))
    return {"guardrails": items, "diagnostics": rule_set.diagnostics}


def replace_guardrail_rules(rules: list[dict[str, Any]]) -> dict[str, Any]:
    if GUARDRAILS_RULES_DIR.exists():
        shutil.rmtree(GUARDRAILS_RULES_DIR)
    for rule_payload in rules:
        upsert_guardrail_rule(rule_payload)
    return list_guardrail_rules()


def upsert_guardrail_rule(rule_payload: dict[str, Any]) -> dict[str, Any]:
    document_content = str(rule_payload.get("document_content") or "")
    if document_content:
        return _write_raw_rule_markdown(document_content, previous_id=str(rule_payload.get("id") or "") or None)
    rule = GuardrailRule.model_validate(rule_payload)
    document_body = rule_payload.get("document_body")
    path = _write_rule_markdown(rule, body=str(document_body) if document_body is not None else None)
    parsed_rule, body, content = _read_rule_markdown(path)
    return _rule_document_payload(parsed_rule, path, body, content)


def delete_guardrail_rule(rule_id: str) -> bool:
    found = False
    for path in sorted(GUARDRAILS_RULES_DIR.glob(f"**/{GUARDRAIL_FILENAME}")):
        try:
            rule = _rule_from_markdown(path)
        except Exception:
            continue
        if rule.id != rule_id:
            continue
        found = True
        path.unlink()
        try:
            path.parent.rmdir()
        except OSError:
            pass
    if not found:
        return False
    return True


def reset_guardrail_rules() -> dict[str, Any]:
    if GUARDRAILS_RULES_DIR.exists():
        shutil.rmtree(GUARDRAILS_RULES_DIR)
    save_guardrail_rules(DEFAULT_GUARDRAILS)
    return list_guardrail_rules()


def scope_matches(
    rule: GuardrailRule,
    *,
    source_name: str,
    route: Any,
    semantic_trace: dict[str, Any],
    question: str = "",
) -> bool:
    return scope_status(
        rule.model_dump(mode="json"),
        {
            "available_tables": sorted(_route_table_names(route)),
            "semantic_asset_ids": sorted(semantic_asset_ids(semantic_trace)),
            "question": question,
        },
    ) == "passed"


def _sql_contains(sql: str, needle: str) -> bool:
    return str(needle or "").lower() in sql.lower()


def _extract_group_by_columns(sql: str) -> list[str]:
    match = re.search(
        r"\bgroup\s+by\s+(?P<columns>.*?)(?:\border\s+by\b|\blimit\b|\bhaving\b|\bwhere\b|\bselect\b|$)",
        sql,
        re.IGNORECASE | re.DOTALL,
    )
    if not match:
        return []
    raw_columns = match.group("columns")
    return [_normalize_identifier(column) for column in raw_columns.split(",") if column.strip()]


def _uses_table(sql: str, table_name: str) -> bool:
    table = re.escape(str(table_name).strip())
    return re.search(rf"\b(?:from|join)\s+{table}\b", sql, re.IGNORECASE) is not None


def _conflict(rule: GuardrailRule, fallback_message: str) -> GuardrailConflict:
    return GuardrailConflict(
        rule_id=rule.id,
        rule_name=rule.name,
        rule_type=rule.type,
        action=rule.action.type,
        message=rule.action.message or fallback_message,
    )


def _detect_forbid_sql_pattern(sql: str, rule: GuardrailRule) -> GuardrailConflict | None:
    if not detector_failed(sql, rule.model_dump(mode="json")):
        return None
    if (
        rule.id == "postgres_count_distinct_nullable_tuple_after_left_join"
        and not _counts_distinct_tuple_from_nullable_join_side(sql)
    ):
        return None
    return _conflict(rule, f"SQL 命中禁止模式：{rule.params.get('pattern') or ''}")


def _counts_distinct_tuple_from_nullable_join_side(sql: str) -> bool:
    """Return whether a DISTINCT tuple reads a nullable LEFT JOIN side."""

    try:
        tree = sqlglot.parse_one(sql, read="postgres")
    except Exception:
        # The native database planner/Validator owns syntax validity. Preserve
        # the configured regex diagnostic when AST refinement is unavailable.
        return True
    nullable_aliases = {
        str(join.this.alias_or_name or "").strip().lower()
        for join in tree.find_all(exp.Join)
        if str(join.side or "").strip().upper() == "LEFT"
        and str(join.this.alias_or_name or "").strip()
    }
    if not nullable_aliases:
        return False
    for count in tree.find_all(exp.Count):
        distinct = count.this
        if not isinstance(distinct, exp.Distinct):
            continue
        tuples = list(distinct.find_all(exp.Tuple))
        if not tuples:
            continue
        counted_aliases = {
            str(column.table or "").strip().lower()
            for tuple_expression in tuples
            for column in tuple_expression.find_all(exp.Column)
            if str(column.table or "").strip()
        }
        if counted_aliases & nullable_aliases:
            return True
    return False


def _detect_require_sql_contains(sql: str, rule: GuardrailRule) -> GuardrailConflict | None:
    if not detector_failed(sql, rule.model_dump(mode="json")):
        return None
    return _conflict(rule, f"SQL 必须包含：{rule.params.get('contains') or ''}")


def _detect_require_table_when_available(sql: str, rule: GuardrailRule) -> GuardrailConflict | None:
    if not detector_failed(sql, rule.model_dump(mode="json")):
        return None
    return _conflict(rule, f"SQL 必须使用表：{rule.params.get('required_table') or ''}")


def _detect_require_group_by(sql: str, rule: GuardrailRule) -> GuardrailConflict | None:
    return _conflict(rule, "GROUP BY 字段不符合规则。") if detector_failed(
        sql, rule.model_dump(mode="json")
    ) else None


def _detect_forbid_exists_distinct_pattern(sql: str, rule: GuardrailRule) -> GuardrailConflict | None:
    return _conflict(rule, "SQL 命中 EXISTS + DISTINCT 慢查询模式。") if detector_failed(
        sql, rule.model_dump(mode="json")
    ) else None


def _governed_surface(frontmatter: dict[str, Any]) -> tuple[set[str], set[str]]:
    """Return (columns, eav_type_names) declared by one semantic asset."""

    governed = frontmatter.get("governed") if isinstance(frontmatter.get("governed"), dict) else {}
    columns = {str(item).strip().lower() for item in governed.get("columns") or [] if str(item).strip()}
    eav_names = {str(item).strip() for item in governed.get("eav_type_names") or [] if str(item).strip()}
    if columns or eav_names:
        return columns, eav_names
    # Derive from resolution.bindings when no explicit governed block exists.
    resolution = frontmatter.get("resolution") if isinstance(frontmatter.get("resolution"), dict) else {}
    for binding in resolution.get("bindings") or []:
        fields = binding.get("fields") if isinstance(binding, dict) else None
        value = str((fields or {}).get("value") or "").strip()
        if value:
            columns.add(value.lower())
    return columns, eav_names


def _declaration_bearing_assets(semantic_trace: dict[str, Any], sql: str) -> list[dict[str, Any]]:
    """Assets whose frontmatter declares enum rules and that apply to this SQL.

    Primary set: assets resolved for the current generation. Fallback (when the
    outer agent forgot to select the asset): declaration-bearing assets whose
    governed column or EAV type_name literally appears in the SQL. Both sets
    are driven by the assets themselves; nothing is hardcoded per dimension.
    """

    from analytics.semantic_assets.registry import get_semantic_asset_registry

    registry = get_semantic_asset_registry()
    snapshot = registry.list_assets()
    declared: list[dict[str, Any]] = []
    for item in snapshot.get("assets") or []:
        asset_id = str(item.get("id") or "")
        if not asset_id:
            continue
        try:
            detail = registry.get_asset(asset_id)
        except Exception:
            continue
        frontmatter = detail.get("frontmatter") if isinstance(detail, dict) else None
        if not isinstance(frontmatter, dict):
            continue
        if not (
            frontmatter.get("enum_universe")
            or frontmatter.get("classifications")
            or frontmatter.get("forbidden_patterns")
        ):
            continue
        detail["_frontmatter"] = frontmatter
        declared.append(detail)

    resolved_ids = semantic_asset_ids(semantic_trace)
    if resolved_ids:
        chosen = [asset for asset in declared if str(asset.get("id") or "") in resolved_ids]
        if chosen:
            return chosen
    lowered = sql.lower()
    fallback: list[dict[str, Any]] = []
    for asset in declared:
        columns, eav_names = _governed_surface(asset["_frontmatter"])
        if any(column in lowered for column in columns) or any(
            f"'{name.lower()}'" in lowered for name in eav_names
        ):
            fallback.append(asset)
    return fallback


def _detect_semantic_enum_consistency(
    sql: str,
    rule: GuardrailRule,
    *,
    semantic_trace: dict[str, Any] | None = None,
    question: str = "",
) -> GuardrailConflict | None:
    import re as _re

    from analytics.nl2sql.sql_enum_extract import extract_enum_usage

    issues: list[str] = []
    for asset in _declaration_bearing_assets(semantic_trace or {}, sql):
        frontmatter = asset["_frontmatter"]
        asset_id = str(asset.get("id") or "semantic_asset")
        universe = {str(item) for item in frontmatter.get("enum_universe") or []}
        classifications_raw = frontmatter.get("classifications") or {}
        classifications = {
            str(label).strip(): {str(item) for item in values}
            for label, values in classifications_raw.items()
            if isinstance(values, list)
        }
        forbidden = frontmatter.get("forbidden_patterns") or []
        columns, eav_names = _governed_surface(frontmatter)
        if not (columns or eav_names):
            continue
        try:
            usage = extract_enum_usage(sql, columns, eav_names)
        except Exception:
            # Unparseable SQL must not fail closed here; the read-only safety
            # check upstream remains authoritative for syntax-level problems.
            continue

        all_literals: set[str] = set()
        for literals in usage.column_literals.values():
            all_literals.update(literals)

        if universe:
            for key, literals in sorted(usage.column_literals.items()):
                unknown = sorted(literals - universe)
                if unknown:
                    issues.append(
                        f"[{asset_id}] {key} 出现未登记取值：{'、'.join(unknown)}"
                        f"（enum_universe 共 {len(universe)} 个合法值）"
                    )
        for case_label in usage.case_labels:
            label = case_label.label.strip()
            expected_set = classifications.get(label)
            if not expected_set:
                continue
            if case_label.is_else:
                if case_label.governed:
                    issues.append(
                        f"[{asset_id}] 分类「{label}」由 ELSE 归入，"
                        "未映射取值会被一并并入；必须显式枚举"
                    )
                continue
            # Narrowing to a subset of the declared mapping is a legitimate
            # filter, not an override; only added values are violations.
            # Labels whose condition never touched a governed column
            # (e.g. serial_name) are not judged against this asset.
            if not case_label.governed:
                continue
            extras = sorted(case_label.literals - expected_set)
            if extras:
                issues.append(
                    f"[{asset_id}] 分类「{label}」多出的取值：{'、'.join(extras)}"
                    f"（声明映射 {len(expected_set)} 个值）"
                )
        # The question text is the agent's declared intent: literals on this
        # asset's governed columns must stay inside the union of the mappings
        # for every classification the question names (a comparison query
        # naming both 传统能源 and 新能源 allows their union). This closes the
        # plain-WHERE bypass that skips CASE labels entirely.
        named_mappings = [
            (label, expected_set)
            for label, expected_set in classifications.items()
            if label in question
        ]
        if named_mappings:
            allowed: set[str] = set()
            for _label, expected_set in named_mappings:
                allowed.update(expected_set)
            extras = sorted(all_literals - allowed)
            if extras:
                labels_text = "、".join(label for label, _ in named_mappings)
                issues.append(
                    f"[{asset_id}] 问题指定「{labels_text}」，但 SQL 使用了映射外取值："
                    f"{'、'.join(extras)}（声明映射共 {len(allowed)} 个值）"
                )
            # A question naming classifications must materialize the mapping
            # inside the SQL (a governed CASE arm, or a filter over mapped
            # values). Otherwise the classification happens in the agent's
            # unchecked reasoning layer — the 2026-07-25 dodge: ask for a raw
            # enum breakdown, then map labels mentally (diesel came back).
            named_labels = {label for label, _ in named_mappings}
            materialized = any(
                case_label.governed and case_label.label.strip() in named_labels
                for case_label in usage.case_labels
            ) or any(literal in allowed for literal in all_literals)
            if not materialized:
                labels_text = "、".join(label for label, _ in named_mappings)
                issues.append(
                    f"[{asset_id}] 问题指定「{labels_text}」，但 SQL 未物化任何分类结构"
                    "（CASE 映射或映射值过滤）；分类口径将落入无校验层，"
                    "必须以 CASE 显式物化，或从问题中移除分类表述"
                )
        for item in forbidden:
            if not isinstance(item, dict):
                continue
            pattern = str(item.get("pattern") or "")
            if not pattern:
                continue
            # Match declared patterns only against LIKE clauses on governed
            # columns — never against raw SQL text (car_name LIKE '%纯电%'
            # is legitimate).
            try:
                matched = any(
                    _re.search(pattern, f"LIKE '{like}'", _re.IGNORECASE)
                    for like in usage.like_patterns
                )
            except _re.error:
                # A broken pattern in an asset must not fail validation of
                # unrelated SQL.
                continue
            if matched:
                issues.append(f"[{asset_id}] 命中禁止模式：{item.get('message') or pattern}")

    if not issues:
        return None
    return GuardrailConflict(
        rule_id=rule.id,
        rule_name=rule.name,
        rule_type=rule.type,
        action=rule.action.type,
        message="；".join(issues),
    )


def collect_applied_semantic_rules(
    sql: str,
    semantic_trace: dict[str, Any] | None,
) -> list[dict[str, Any]]:
    """Deterministically derive which asset declarations governed this SQL.

    P2b: the generator never self-reports its basis (an LLM confession is not
    trustworthy); the same declarations the detector enforces are re-derived
    here and recorded into the generation trace for audit and acceptance
    reconciliation.
    """

    from analytics.nl2sql.sql_enum_extract import extract_enum_usage

    applied: list[dict[str, Any]] = []
    for asset in _declaration_bearing_assets(semantic_trace or {}, sql):
        frontmatter = asset["_frontmatter"]
        columns, eav_names = _governed_surface(frontmatter)
        if not (columns or eav_names):
            continue
        declarations = [
            name
            for name in ("classifications", "enum_universe", "forbidden_patterns")
            if frontmatter.get(name)
        ]
        try:
            usage = extract_enum_usage(sql, columns, eav_names)
        except Exception:
            continue
        literals: set[str] = set()
        for values in usage.column_literals.values():
            literals.update(values)
        applied.append(
            {
                "asset_id": str(asset.get("id") or "semantic_asset"),
                "declarations": declarations,
                "governed_columns": sorted(columns),
                "governed_eav_type_names": sorted(eav_names),
                "literals": sorted(literals),
                "case_labels": [
                    item.label for item in usage.case_labels if item.governed
                ],
            }
        )
    return applied


DETECTORS = {
    "forbid_sql_pattern": _detect_forbid_sql_pattern,
    "require_sql_contains": _detect_require_sql_contains,
    "require_table_when_available": _detect_require_table_when_available,
    "require_group_by": _detect_require_group_by,
    "forbid_exists_distinct_pattern": _detect_forbid_exists_distinct_pattern,
    "semantic_enum_consistency": _detect_semantic_enum_consistency,
}

_SEMANTIC_TRACE_DETECTOR_TYPES = {"semantic_enum_consistency"}


def detect_guardrail_conflicts(
    sql: str,
    *,
    source_name: str,
    route: Any,
    semantic_trace: dict[str, Any],
    question: str = "",
    rules: list[GuardrailRule] | None = None,
) -> list[GuardrailConflict]:
    active_rules = rules if rules is not None else load_guardrail_rules().guardrails
    conflicts: list[GuardrailConflict] = []
    for rule in active_rules:
        if not rule.enabled:
            continue
        if not scope_matches(
            rule,
            source_name=source_name,
            route=route,
            semantic_trace=semantic_trace,
            question=question,
        ):
            continue
        detector = DETECTORS.get(rule.type)
        if detector is None:
            continue
        if rule.type in _SEMANTIC_TRACE_DETECTOR_TYPES:
            conflict = detector(sql, rule, semantic_trace=semantic_trace, question=question)
        else:
            conflict = detector(sql, rule)
        if conflict is not None:
            conflicts.append(conflict)
    return conflicts


def conflicts_to_messages(conflicts: list[GuardrailConflict]) -> list[str]:
    return [f"{conflict.rule_id}：{conflict.message}" for conflict in conflicts]
