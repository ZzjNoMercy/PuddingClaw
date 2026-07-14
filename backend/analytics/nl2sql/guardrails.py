"""Configurable SQL guardrails for NL2SQL execution."""

from __future__ import annotations

import re
import shutil
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, Field


BASE_DIR = Path(__file__).resolve().parents[2]
GUARDRAILS_ROOT = BASE_DIR / "sql-guardrails"
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
            "document_path": path.relative_to(BASE_DIR).as_posix(),
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
                diagnostics.append({"path": path.relative_to(BASE_DIR).as_posix(), "error": f"duplicate id: {rule.id}"})
                continue
            seen_ids.add(rule.id)
            rules.append(rule)
        except Exception as exc:
            diagnostics.append({"path": path.relative_to(BASE_DIR).as_posix(), "error": str(exc)})
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
    scope = rule.scope
    route_tables = _route_table_names(route)
    asset_ids = semantic_asset_ids(semantic_trace)

    table_values = set(scope.table_scope.values)
    if table_values:
        if scope.table_scope.mode == "all":
            if not table_values.issubset(route_tables):
                return False
        elif not table_values.intersection(route_tables):
            return False
    if scope.semantic_assets and not set(scope.semantic_assets).issubset(asset_ids):
        return False
    if scope.intent_any:
        normalized_question = str(question or "").lower()
        if not any(str(intent).strip().lower() in normalized_question for intent in scope.intent_any if str(intent).strip()):
            return False
    return True


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
    pattern = str(rule.params.get("pattern") or "")
    if not pattern:
        return None
    flags = re.IGNORECASE
    configured_flags = {item.lower() for item in _as_list(rule.params.get("flags"))}
    if "case_sensitive" in configured_flags:
        flags = 0
    if not re.search(pattern, sql, flags):
        return None
    unless_contains = str(rule.params.get("unless_contains") or "")
    if unless_contains and _sql_contains(sql, unless_contains):
        return None
    unless_pattern = str(rule.params.get("unless_pattern") or "")
    if unless_pattern and re.search(unless_pattern, sql, flags):
        return None
    return _conflict(rule, f"SQL 命中禁止模式：{pattern}")


def _detect_require_sql_contains(sql: str, rule: GuardrailRule) -> GuardrailConflict | None:
    contains = str(rule.params.get("contains") or "")
    if not contains:
        return None
    triggers = _as_list(rule.params.get("when_contains_any"))
    if triggers and not any(_sql_contains(sql, item) for item in triggers):
        return None
    if _sql_contains(sql, contains):
        return None
    return _conflict(rule, f"SQL 必须包含：{contains}")


def _detect_require_table_when_available(sql: str, rule: GuardrailRule) -> GuardrailConflict | None:
    required_table = str(rule.params.get("required_table") or "")
    fallback_table = str(rule.params.get("fallback_table") or "")
    if not required_table:
        return None
    if _uses_table(sql, required_table):
        return None
    if fallback_table and not _uses_table(sql, fallback_table):
        return None
    return _conflict(rule, f"SQL 必须使用表：{required_table}")


def _detect_require_group_by(sql: str, rule: GuardrailRule) -> GuardrailConflict | None:
    group_by_columns = set(_extract_group_by_columns(sql))
    if not group_by_columns:
        return None
    require_columns = {_normalize_identifier(item) for item in _as_list(rule.params.get("require_columns"))}
    forbidden_only = {_normalize_identifier(item) for item in _as_list(rule.params.get("forbidden_columns_only"))}
    if forbidden_only and group_by_columns == forbidden_only:
        return _conflict(rule, "GROUP BY 字段不符合规则。")
    if require_columns and not require_columns.issubset(group_by_columns):
        return _conflict(rule, "GROUP BY 缺少必需字段。")
    return None


def _detect_forbid_exists_distinct_pattern(sql: str, rule: GuardrailRule) -> GuardrailConflict | None:
    lowered = " ".join(sql.split()).lower()
    table = str(rule.params.get("table") or "")
    distinct_column = str(rule.params.get("distinct_column") or "")
    min_exists_count = int(rule.params.get("min_exists_count") or 2)
    if table and not _uses_table(lowered, table):
        return None
    exists_count = len(re.findall(r"\b(?:not\s+)?exists\s*\(", lowered))
    if exists_count < min_exists_count:
        return None
    if "count(distinct" not in lowered:
        return None
    if distinct_column and re.search(rf"\bselect\s+distinct\s+[\w.]*{re.escape(distinct_column.lower())}\b", lowered) is None:
        return None
    return _conflict(rule, "SQL 命中 EXISTS + DISTINCT 慢查询模式。")


DETECTORS = {
    "forbid_sql_pattern": _detect_forbid_sql_pattern,
    "require_sql_contains": _detect_require_sql_contains,
    "require_table_when_available": _detect_require_table_when_available,
    "require_group_by": _detect_require_group_by,
    "forbid_exists_distinct_pattern": _detect_forbid_exists_distinct_pattern,
}


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
        conflict = detector(sql, rule)
        if conflict is not None:
            conflicts.append(conflict)
    return conflicts


def conflicts_to_messages(conflicts: list[GuardrailConflict]) -> list[str]:
    return [f"{conflict.rule_id}：{conflict.message}" for conflict in conflicts]
