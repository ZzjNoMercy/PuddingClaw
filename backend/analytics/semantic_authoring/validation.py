"""Deterministic checks around LLM-authored semantic Markdown."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from analytics.models import AnalyticsModelError, get_analytics_model_registry
from analytics.models.registry import _normalize_semantic_assets, canonical_model_resource_path
from analytics.semantic_assets import SemanticAssetError, get_semantic_asset_registry
from analytics.semantic_assets.registry import _normalize_dimension_definition, _normalize_relation_definition
from knowledge.paths import get_knowledge_root

from .contracts import (
    KIND_EFFECTS,
    AuthoringBrief,
    DefinitionKind,
    kind_from_logical_path,
)
from .documents import MarkdownDocument, MarkdownDocumentError, parse_markdown_document

_TOPICS: dict[DefinitionKind, dict[str, tuple[str, ...]]] = {
    "measure": {
        "business_meaning": ("业务含义", "业务口径", "定义"),
        "sources": ("数据来源", "来源字段", "语义输入", "输入"),
        "calculation": ("计算", "分子", "分母", "聚合"),
        "grain": ("颗粒度", "粒度"),
        "rules": ("业务规则", "边界", "空值", "零"),
        "unit_and_time": ("单位", "币种", "时间口径", "时间字段", "不适用"),
        "duplicates": ("去重", "重复", "唯一键", "不适用"),
        "examples": ("验收", "示例", "反例"),
    },
    "dimension": {
        "business_meaning": ("业务含义", "业务口径", "成员"),
        "resolution": ("解析", "映射", "来源", "创建方式"),
        "unknowns": ("未知", "空值", "其他值", "未匹配"),
        "examples": ("验收", "示例", "反例"),
    },
    "grain": {
        "business_object": ("业务对象", "统计对象", "业务含义"),
        "identity": ("唯一键", "组合键", "身份"),
        "deduplication": ("去重", "重复"),
        "rollup": ("上卷", "汇总", "父级"),
        "examples": ("验收", "示例", "反例"),
    },
    "relation": {
        "endpoints": ("两端", "来源资产", "目标资产", "关联对象"),
        "keys": ("字段映射", "关联键", "join key", "连接键"),
        "cardinality": ("基数", "一对一", "一对多", "多对一", "多对多"),
        "risk": ("重复计数", "未匹配", "空键", "覆盖率"),
    },
    "analytics_model": {
        "goal": ("模型目标", "业务目标", "目标问题"),
        "questions": ("适用问题", "典型问题", "用户问题"),
        "dependencies": ("依赖", "数据资产", "语义资产", "关系"),
        "scope": ("适用范围", "默认范围", "过滤", "限制"),
        "output": ("输出要求", "结果要求", "交付"),
        "acceptance": ("验收", "示例", "反例"),
    },
}

_REQUIRED_BRIEF_TOPICS: dict[DefinitionKind, frozenset[str]] = {
    kind: frozenset(topics) for kind, topics in _TOPICS.items()
}

_PLACEHOLDER_PATTERNS = (
    r"\bTODO\b",
    r"\bTBD\b",
    r"在这里描述",
    r"待补充",
    r"待确认",
)


def _validate_common_frontmatter(
    document: MarkdownDocument,
    kind: DefinitionKind,
) -> list[dict[str, str]]:
    diagnostics: list[dict[str, str]] = []
    expected_formatter = "analytics-model" if kind == "analytics_model" else (
        "asset-relation" if kind == "relation" else "semantic-asset"
    )
    expected_type = "analysis_model" if kind == "analytics_model" else kind
    required_strings = {
        "formatter": expected_formatter,
        "type": expected_type,
        "name": None,
        "description": None,
    }
    for field, expected in required_strings.items():
        value = document.frontmatter.get(field)
        if not isinstance(value, str) or not value.strip():
            diagnostics.append(
                {
                    "code": "missing_required_frontmatter",
                    "severity": "error",
                    "message": f"frontmatter field '{field}' must be a non-empty string",
                }
            )
        elif expected is not None and value.strip() != expected:
            diagnostics.append(
                {
                    "code": "frontmatter_path_conflict",
                    "severity": "error",
                    "message": f"frontmatter field '{field}' must be '{expected}' for this target path",
                }
            )
    for field in ("aliases", "tags"):
        value = document.frontmatter.get(field)
        if value is not None and (
            not isinstance(value, list)
            or any(not isinstance(item, str) or not item.strip() for item in value)
        ):
            diagnostics.append(
                {
                    "code": "invalid_frontmatter_list",
                    "severity": "error",
                    "message": f"frontmatter field '{field}' must be a list of non-empty strings",
                }
            )
    return diagnostics


def _business_tokens(value: Any) -> list[str]:
    if isinstance(value, str):
        text = value.strip().lower()
        return [text] if text else []
    if isinstance(value, list):
        return [token for item in value for token in _business_tokens(item)]
    if isinstance(value, dict):
        return [token for nested in value.values() for token in _business_tokens(nested)]
    return []


def _body_mentions(value: Any, body_lower: str) -> bool:
    return all(token in body_lower for token in _business_tokens(value))


def _validate_business_frontmatter(
    document: MarkdownDocument,
    kind: DefinitionKind,
) -> list[dict[str, str]]:
    body_lower = document.body.lower()
    diagnostics: list[dict[str, str]] = []
    for effect in KIND_EFFECTS[kind]:
        if effect.body_projection != "required" or effect.field not in document.frontmatter:
            continue
        if not _body_mentions(document.frontmatter[effect.field], body_lower):
            diagnostics.append(
                {
                    "code": "business_frontmatter_not_auditable_in_body",
                    "severity": "error",
                    "message": f"frontmatter field '{effect.field}' affects runtime but is not auditable in the body",
                }
            )
    return diagnostics


def _error(code: str, message: str) -> dict[str, str]:
    return {"code": code, "severity": "error", "message": message}


def _json_mapping(path: Path) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _validate_kind_contract(
    document: MarkdownDocument,
    kind: DefinitionKind,
    *,
    logical_path: str,
    definitions_root: Path | None,
) -> list[dict[str, str]]:
    """Validate existing runtime contracts without inferring business values."""

    meta = document.frontmatter
    diagnostics: list[dict[str, str]] = []
    if kind == "dimension":
        resolution_mode = str(meta.get("resolution_mode") or "").strip()
        resolution = meta.get("resolution")
        if not resolution_mode or not isinstance(resolution, dict):
            return [_error("invalid_dimension_contract", "dimension requires resolution_mode and resolution")]
        embedded_mode = str(resolution.get("mode") or resolution.get("resolution_mode") or "").strip()
        if embedded_mode and embedded_mode != resolution_mode:
            return [_error("dimension_mode_conflict", "resolution mode conflicts with resolution_mode")]
        try:
            normalized_mode, normalized = _normalize_dimension_definition({**resolution, "mode": resolution_mode})
        except SemanticAssetError as exc:
            return [_error("invalid_dimension_contract", str(exc))]
        if normalized_mode == "entity_lookup":
            return [
                _error(
                    "entity_lookup_requires_dimension_builder",
                    "entity_lookup Dimensions require the dedicated build-semantic-dimension workflow",
                )
            ]
        bindings = normalized.get("bindings") or []
        if normalized_mode in {"source_field", "derived", "calendar_lookup"}:
            if not bindings or any(
                not str(item.get("asset_ref") or "").strip() or not (item.get("fields") or {})
                for item in bindings
            ):
                diagnostics.append(
                    _error(
                        "invalid_dimension_binding",
                        "ordinary Dimension resolution requires bindings with asset_ref and fields",
                    )
                )
        if normalized_mode == "derived" and (
            not normalized.get("source_fields") or not str(normalized.get("expression") or "").strip()
        ):
            diagnostics.append(
                _error("invalid_derived_dimension", "derived Dimension requires source_fields and expression")
            )
        if normalized_mode == "calendar_lookup" and not str(normalized.get("date_field") or "").strip():
            diagnostics.append(
                _error("invalid_calendar_dimension", "calendar_lookup Dimension requires date_field")
            )
    elif kind == "relation":
        relation_type = str(meta.get("relation_type") or "").strip()
        relation = meta.get("relation")
        if not relation_type or not isinstance(relation, dict):
            return [_error("invalid_relation_contract", "relation requires relation_type and relation")]
        embedded_type = str(relation.get("type") or relation.get("relation_type") or "").strip()
        if embedded_type and embedded_type != relation_type:
            return [_error("relation_type_conflict", "relation type conflicts with relation_type")]
        try:
            normalized_type, normalized = _normalize_relation_definition({**relation, "type": relation_type})
        except SemanticAssetError as exc:
            return [_error("invalid_relation_contract", str(exc))]
        if definitions_root is not None and normalized_type == "dimension_binding":
            dimension_ref = str((normalized.get("dimension") or {}).get("ref") or "")
            try:
                assets = get_semantic_asset_registry(definitions_root)
                assets.refresh()
                asset = assets.get_asset(dimension_ref)
                if asset.get("type") != "dimension":
                    raise SemanticAssetError("referenced asset is not a Dimension")
            except Exception:
                diagnostics.append(
                    _error("missing_relation_dependency", f"referenced Dimension does not exist: {dimension_ref}")
                )
        if normalized_type == "direct_join":
            mapping = normalized.get("field_mapping") or {}
            for side in ("left", "right"):
                endpoint_fields = set((normalized.get(side) or {}).get("key_fields") or [])
                mapped_fields = set(mapping.get(side) or [])
                if not mapped_fields.issubset(endpoint_fields):
                    diagnostics.append(
                        _error(
                            "relation_key_mapping_conflict",
                            f"relation field_mapping.{side} must use fields declared in {side}.key_fields",
                        )
                    )
    elif kind == "analytics_model":
        required_mappings = ("data_assets", "semantic_assets", "templates")
        required_lists = ("asset_relations", "guardrails")
        for field in required_mappings:
            if not isinstance(meta.get(field), dict):
                diagnostics.append(_error("invalid_model_contract", f"model field '{field}' must be a mapping"))
        for field in required_lists:
            value = meta.get(field)
            if not isinstance(value, list) or any(not isinstance(item, str) or not item.strip() for item in value):
                diagnostics.append(_error("invalid_model_contract", f"model field '{field}' must be a string list"))
        default_template = meta.get("default_template")
        if not isinstance(default_template, str):
            diagnostics.append(_error("invalid_model_contract", "model field 'default_template' must be a string"))
        if diagnostics:
            return diagnostics
        data_assets = meta["data_assets"]
        tables = data_assets.get("tables")
        table_aliases = data_assets.get("table_aliases", {})
        if not isinstance(tables, list) or any(not isinstance(item, str) or not item.strip() for item in tables):
            diagnostics.append(_error("invalid_model_contract", "data_assets.tables must be a string list"))
        if not isinstance(table_aliases, dict):
            diagnostics.append(_error("invalid_model_contract", "data_assets.table_aliases must be a mapping"))
        raw_semantic_assets = meta["semantic_assets"]
        for group in ("measures", "dimensions", "grains"):
            values = raw_semantic_assets.get(group, [])
            if not isinstance(values, list) or any(not isinstance(item, str) or not item.strip() for item in values):
                diagnostics.append(
                    _error("invalid_model_contract", f"semantic_assets.{group} must be a string list")
                )
        if diagnostics:
            return diagnostics
        semantic_assets = _normalize_semantic_assets(meta["semantic_assets"])
        for group in ("measures", "dimensions", "grains"):
            if semantic_assets[group] != raw_semantic_assets.get(group, []):
                diagnostics.append(
                    _error(
                        "noncanonical_model_reference",
                        f"semantic_assets.{group} must use unique typed ids such as '{group[:-1]}:<id>'",
                    )
                )
        if diagnostics:
            return diagnostics
        asset_relations = meta["asset_relations"]
        try:
            registry = get_analytics_model_registry(definitions_root) if definitions_root is not None else None
            if registry is not None:
                assets = get_semantic_asset_registry(definitions_root)
                assets.refresh()
                registry._validate_asset_graph(  # noqa: SLF001 - validate the canonical Registry contract
                    data_assets=data_assets,
                    semantic_assets=semantic_assets,
                    asset_relations=asset_relations,
                )
                for group in ("measures", "dimensions", "grains"):
                    for asset_id in semantic_assets[group]:
                        asset = assets.get_asset(asset_id)
                        if asset.get("type") != group[:-1]:
                            raise AnalyticsModelError(f"semantic asset type mismatch: {asset_id}")
                for relation_id in asset_relations:
                    relation_asset = assets.get_asset(relation_id)
                    if relation_asset.get("type") != "relation":
                        raise AnalyticsModelError(f"not a Relation: {relation_id}")
                    relation_meta = relation_asset.get("frontmatter") or {}
                    relation_definition = relation_meta.get("relation") or {}
                    relation_type = str(relation_meta.get("relation_type") or "")
                    selected_tables = set(tables)
                    selected_dimensions = set(semantic_assets["dimensions"])
                    if relation_type == "dimension_binding":
                        asset_ref = str((relation_definition.get("asset") or {}).get("ref") or "")
                        dimension_ref = str((relation_definition.get("dimension") or {}).get("ref") or "")
                        if asset_ref not in selected_tables or dimension_ref not in selected_dimensions:
                            raise AnalyticsModelError(
                                f"Relation endpoints must be selected by the model: {relation_id}"
                            )
                    elif relation_type == "direct_join":
                        left_ref = str((relation_definition.get("left") or {}).get("ref") or "")
                        right_ref = str((relation_definition.get("right") or {}).get("ref") or "")
                        if left_ref not in selected_tables or right_ref not in selected_tables:
                            raise AnalyticsModelError(
                                f"Relation endpoints must be selected by the model: {relation_id}"
                            )
        except (AnalyticsModelError, SemanticAssetError, KeyError, ValueError) as exc:
            diagnostics.append(_error("invalid_model_dependency_graph", str(exc)))
        templates = meta["templates"]
        if default_template and default_template not in templates:
            diagnostics.append(
                _error("invalid_default_template", "default_template must name an entry in templates")
            )
        if definitions_root is not None:
            model_dir = definitions_root / Path(*Path(logical_path).parent.parts)
            for table_ref in tables:
                if not table_ref.startswith("table_asset:"):
                    continue
                asset_id = table_ref.removeprefix("table_asset:").strip()
                logical_dataset = (
                    definitions_root.parent / "data" / "analytics-concat-datasets" / asset_id / "dataset.json"
                )
                profile = get_knowledge_root(definitions_root) / ".puddingclaw" / "table_profiles" / f"{asset_id}.profile.json"
                logical_definition = _json_mapping(logical_dataset)
                profile_definition = _json_mapping(profile)
                logical_exists = bool(
                    logical_definition and logical_definition.get("formatter") == "logical-data-asset"
                )
                profile_exists = profile_definition is not None
                if not asset_id or "/" in asset_id or "\\" in asset_id or not (logical_exists or profile_exists):
                    diagnostics.append(
                        _error("missing_model_data_asset", f"selected table asset does not exist: {table_ref}")
                    )
            requested_guardrails = set(meta["guardrails"])
            if requested_guardrails:
                available_guardrails: set[str] = set()
                for rule_path in (definitions_root / "sql-guardrails" / "rules").glob("**/guardrail.md"):
                    try:
                        rule_document = parse_markdown_document(rule_path.read_text(encoding="utf-8"))
                        rule_id = str(rule_document.frontmatter.get("id") or "").strip()
                        if rule_id:
                            available_guardrails.add(rule_id)
                    except (OSError, MarkdownDocumentError):
                        continue
                for guardrail_id in sorted(requested_guardrails - available_guardrails):
                    diagnostics.append(
                        _error("missing_model_guardrail", f"selected Guardrail does not exist: {guardrail_id}")
                    )
            for template_id, raw_definition in templates.items():
                definition = raw_definition if isinstance(raw_definition, dict) else {"path": raw_definition}
                if "semantic_scope" in definition:
                    diagnostics.append(
                        _error(
                            "invalid_model_template",
                            f"template {template_id} semantic_scope belongs in its guide frontmatter",
                        )
                    )
                declared_paths: list[tuple[object, str]] = [
                    (definition.get("path"), "templates"),
                    (definition.get("guide"), "templates"),
                ]
                raw_assets = definition.get("assets") or []
                if not isinstance(raw_assets, list):
                    diagnostics.append(
                        _error("invalid_model_template", f"template {template_id} assets must be a list")
                    )
                    continue
                declared_paths.extend((item, "templates") for item in raw_assets)
                for raw_path, root in declared_paths:
                    if not str(raw_path or "").strip():
                        continue
                    try:
                        relative = canonical_model_resource_path(raw_path, root=root)
                    except AnalyticsModelError as exc:
                        diagnostics.append(_error("invalid_model_resource_path", str(exc)))
                        continue
                    if not (model_dir / relative).is_file():
                        diagnostics.append(
                            _error(
                                "missing_model_resource",
                                f"template {template_id} references a missing package file: {relative}",
                            )
                        )
            references = meta.get("references", {})
            if not isinstance(references, dict):
                diagnostics.append(_error("invalid_model_contract", "model field 'references' must be a mapping"))
            else:
                for reference_id, raw_definition in references.items():
                    definition = raw_definition if isinstance(raw_definition, dict) else {"path": raw_definition}
                    raw_path = definition.get("path")
                    if not str(raw_path or "").strip():
                        diagnostics.append(
                            _error("invalid_model_reference", f"reference {reference_id} requires a path")
                        )
                        continue
                    try:
                        relative = canonical_model_resource_path(raw_path, root="references")
                    except AnalyticsModelError as exc:
                        diagnostics.append(_error("invalid_model_resource_path", str(exc)))
                        continue
                    if not (model_dir / relative).is_file():
                        diagnostics.append(
                            _error(
                                "missing_model_resource",
                                f"reference {reference_id} points to a missing package file: {relative}",
                            )
                        )
    return diagnostics


def validate_markdown_definition(
    content: str,
    *,
    logical_path: str,
    brief: AuthoringBrief | None = None,
    definitions_root: Path | None = None,
) -> dict[str, Any]:
    try:
        kind = kind_from_logical_path(logical_path)
        document = parse_markdown_document(content)
    except (ValueError, MarkdownDocumentError) as exc:
        return {
            "valid": False,
            "kind": None,
            "diagnostics": [{"code": "invalid_document", "severity": "error", "message": str(exc)}],
        }
    diagnostics: list[dict[str, str]] = []
    if not document.frontmatter:
        diagnostics.append({"code": "missing_frontmatter", "severity": "error", "message": "frontmatter is required"})
    else:
        diagnostics.extend(_validate_common_frontmatter(document, kind))
    if not re.search(r"(?m)^#\s+\S", document.body):
        diagnostics.append({"code": "missing_title", "severity": "error", "message": "body needs a readable H1 title"})
    for pattern in _PLACEHOLDER_PATTERNS:
        if re.search(pattern, document.body, flags=re.IGNORECASE):
            diagnostics.append(
                {
                    "code": "placeholder_business_content",
                    "severity": "error",
                    "message": f"body still contains unresolved placeholder text matching '{pattern}'",
                }
            )
    body_lower = document.body.lower()
    for topic, terms in _TOPICS[kind].items():
        if not any(term.lower() in body_lower for term in terms):
            diagnostics.append(
                {
                    "code": "missing_authoring_topic",
                    "severity": "error",
                    "message": f"body may not explain required topic: {topic}",
                }
            )
    diagnostics.extend(_validate_business_frontmatter(document, kind))
    diagnostics.extend(
        _validate_kind_contract(
            document,
            kind,
            logical_path=logical_path,
            definitions_root=definitions_root,
        )
    )
    if brief is not None:
        if brief.kind != kind:
            diagnostics.append({"code": "brief_kind_mismatch", "severity": "error", "message": "brief kind does not match path"})
        if brief.unresolved:
            diagnostics.append(
                {
                    "code": "unresolved_business_decisions",
                    "severity": "error",
                    "message": f"{len(brief.unresolved)} business decisions are unresolved",
                }
            )
        if not brief.goal or not brief.confirmed or not brief.evidence:
            diagnostics.append(
                {
                    "code": "brief_missing_control_evidence",
                    "severity": "error",
                    "message": "brief requires a goal, confirmed decisions, and evidence references",
                }
            )
        missing_reviewed = sorted(_REQUIRED_BRIEF_TOPICS[kind] - set(brief.reviewed_topics))
        if missing_reviewed:
            diagnostics.append(
                {
                    "code": "brief_topics_not_reviewed",
                    "severity": "error",
                    "message": f"brief has not reviewed required topics: {', '.join(missing_reviewed)}",
                }
            )
        if brief.confirmed:
            diagnostics.append(
                {
                    "code": "confirmed_decisions_require_llm_review",
                    "severity": "info",
                    "message": "Agent must confirm that every confirmed decision is faithfully expressed in the body",
                }
            )
    else:
        diagnostics.append(
            {
                "code": "missing_authoring_brief",
                "severity": "error",
                "message": "a control-only Authoring Brief is required before preparation",
            }
        )
    return {
        "valid": not any(item["severity"] == "error" for item in diagnostics),
        "kind": kind,
        "diagnostics": diagnostics,
        "frontmatter": document.frontmatter,
        "body_chars": len(document.body),
    }
