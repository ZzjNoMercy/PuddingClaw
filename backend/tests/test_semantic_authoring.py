import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from langchain.agents.middleware.types import ToolCallRequest

from analytics.semantic_authoring.contracts import inspect_frontmatter_contract
from analytics.semantic_authoring.discovery import discover_semantic_definitions
from analytics.semantic_authoring.service import (
    SemanticAuthoringError,
    publish_semantic_markdown,
)
from analytics.semantic_authoring.service import (
    prepare_semantic_markdown as _prepare_semantic_markdown,
)
from harness.tool_execution import PolicyDecision, ToolExecutionPipeline
from runtime_identity.paths import PuddingClawPaths
from tools.semantic_steward_tool import (
    DiscoverSemanticDefinitionsTool,
    PrepareSemanticMarkdownTool,
    create_semantic_steward_tools,
)
from tools.toolsets import BUSINESS_TOOLSETS
from tools.write_file_tool import create_write_file_tool

MEASURE_BODY = """# 成交均价

## 业务含义

成交均价表示每个售出单位对应的净成交金额。

## 计算口径与颗粒度

先汇总成交金额和销量，再计算 `SUM(成交金额) / SUM(销量)`。默认按车系颗粒度展示。

## 数据来源

成交金额和销量来自已确认的销售事实语义输入。

## 业务规则

销量为零时返回空值，退货按负数计入。币种与时间口径沿用输入数据，不跨币种或周期混算。
重复事实按来源唯一键去重。

## 验收案例

- 正常示例：总成交金额除以总销量。
- 边界示例：净销量为零时返回空值。
- 反例：不得对行级价格直接取平均。
"""

MEASURE_BRIEF = {
    "kind": "measure",
    "goal": "定义成交均价",
    "observed": ["销售事实提供成交金额和销量语义输入"],
    "confirmed": ["先分别汇总成交金额和销量再相除", "销量为零返回空值"],
    "unresolved": [],
    "evidence": ["semantic-input:sales-facts"],
    "reviewed_topics": [
        "business_meaning",
        "sources",
        "calculation",
        "grain",
        "rules",
        "unit_and_time",
        "duplicates",
        "examples",
    ],
    "body_outline": ["业务含义", "数据来源", "计算口径与颗粒度", "业务规则", "验收案例"],
}

GRAIN_MARKDOWN = """---
name: 订单
description: 每个已确认订单对应一个业务对象
aliases: [销售订单]
tags: [销售]
---

# 订单颗粒度

## 业务对象与身份

业务对象是一张已确认订单，唯一键为 `order_id`。

## 去重规则

重复到达时按 `order_id` 去重，并保留最新确认版本。

## 上卷规则

可以上卷到客户或自然日；不得把订单行直接视为订单。

## 验收示例

- 同一 `order_id` 的重复记录只计一个订单。
- 两个不同订单必须计为两个业务对象。
"""

DIMENSION_MARKDOWN = """---
name: 订单渠道
description: 订单成交时采用的渠道分类
aliases: [成交渠道]
tags: [销售]
resolution_mode: source_field
resolution:
  mode: source_field
  bindings:
    - asset_ref: warehouse.orders
      display_name: 订单事实
      fields:
        value: channel_name
---

# 订单渠道

## 业务含义

成员表示订单成交时的渠道分类。

## 解析与映射

采用 `source_field` 解析，从数据资产 `warehouse.orders`（订单事实）的 `channel_name` 映射成员。

## 未知值

空值保留为“未知渠道”，不得自动归入其他成员。

## 验收示例

- `直营` 保持为直营。
- 空值显示为未知渠道。
"""

RELATION_MARKDOWN = """---
name: 订单到客户
description: 订单事实通过客户标识连接客户主数据
tags: [销售]
relation_type: direct_join
relation:
  type: direct_join
  left:
    ref: warehouse.orders
    key_fields: [customer_id]
  right:
    ref: warehouse.customers
    key_fields: [customer_id]
  field_mapping:
    left: [customer_id]
    right: [customer_id]
  cardinality: many_to_one
  join_type: left
  rules: [客户缺失时保留订单]
---

# 订单到客户关系

## 关联对象与两端

来源资产是 `warehouse.orders` 订单事实，目标资产是 `warehouse.customers` 客户主数据，关系类型为 `direct_join`。

## 关联键与字段映射

两端都使用 `customer_id` 作为连接键。

## 基数

基数是多对一（`many_to_one`），一个客户可以对应多张订单。

## 风险与覆盖率

采用 `left` 左连接；规则“客户缺失时保留订单”，禁止因连接造成订单重复计数，并监控空键覆盖率。
"""

DIMENSION_BINDING_RELATION_MARKDOWN = """---
name: 订单渠道绑定
description: 订单事实绑定到订单渠道维度
relation_type: dimension_binding
relation:
  type: dimension_binding
  asset:
    ref: warehouse.orders
    key_fields: [channel_name]
  dimension:
    ref: dimension:order-channel
    output_key: channel_name
  cardinality: many_to_one
---

# 订单渠道绑定关系

## 关联对象与两端

来源资产是 `warehouse.orders` 订单事实，目标资产是 `dimension:order-channel`，关系类型为 `dimension_binding`。

## 关联键与字段映射

订单通过 `channel_name` 连接到维度成员。

## 基数

基数是多对一（`many_to_one`），每张订单最多匹配一个渠道成员。

## 风险与覆盖率

空键或未匹配渠道必须保留为未知，禁止重复计数，并监控未匹配覆盖率。
"""

MODEL_MARKDOWN = """---
name: 订单经营分析
description: 用订单事实回答经营规模与渠道分布问题
tags: [销售]
data_assets:
  tables: [warehouse.orders]
  table_aliases: {}
semantic_assets:
  measures: []
  dimensions: []
  grains: []
asset_relations: []
guardrails: []
templates: {}
default_template: ''
---

# 订单经营分析

## 模型目标与业务目标

模型目标是用 `warehouse.orders` 回答订单经营规模与渠道分布问题。

## 适用问题与典型问题

- 各渠道订单趋势如何？

## 依赖与语义资产

当前只依赖数据资产 `warehouse.orders`，暂不选择语义资产、关系或 Guardrail。

## 适用范围与限制

默认范围为已确认订单；时间过滤由用户问题明确给出。

## 输出要求

输出核心结论、统计范围和异常说明。

## 验收示例

- 回答必须注明订单时间范围。
- 不得把未确认记录纳入结果。
"""


def _brief(kind: str, topics: list[str]) -> dict:
    return {
        "kind": kind,
        "goal": f"定义 {kind}",
        "observed": ["已读取现有定义和数据证据"],
        "confirmed": ["正文中的业务规则已经确认"],
        "unresolved": [],
        "evidence": ["user-confirmation:current-turn"],
        "reviewed_topics": topics,
        "body_outline": ["业务说明", "规则", "验收"],
    }


def prepare_semantic_markdown(**kwargs):
    logical_path = str(kwargs["logical_path"])
    if logical_path.startswith("analytics-models/"):
        kind = "analytics_model"
    else:
        kind = {
            "measures": "measure",
            "dimensions": "dimension",
            "grains": "grain",
            "relations": "relation",
        }[logical_path.split("/")[1]]
    target_slug = Path(logical_path).parent.name
    target_id = target_slug if kind == "analytics_model" else f"{kind}:{target_slug}"
    receipt = discover_semantic_definitions(
        query=target_id,
        kinds=[kind],
        session_id=str(kwargs.get("session_id") or ""),
        paths=kwargs.get("paths"),
    )
    return _prepare_semantic_markdown(
        **kwargs,
        discovery_receipt_id=receipt["receipt_id"],
    )


def _candidate(*, description: str = "每个售出单位对应的净成交金额") -> str:
    return f"""---
name: 成交均价
description: {description}
aliases: [平均成交价, ASP]
tags: [销售]
version: 0.1.0
---

{MEASURE_BODY}"""


def _write_definition(tmp_path: Path, logical_path: str, content: str) -> Path:
    target = tmp_path / "definitions" / logical_path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    return target


def test_discover_lists_searches_and_paginates_measures_with_backlinks(tmp_path) -> None:
    paths = PuddingClawPaths(tmp_path)
    _write_definition(
        tmp_path,
        "semantic-assets/measures/average-price/measure.md",
        _candidate(),
    )
    _write_definition(
        tmp_path,
        "semantic-assets/measures/net-sales/measure.md",
        _candidate(description="扣除退款后的净销售额")
        .replace("name: 成交均价", "name: 净销售额")
        .replace("aliases: [平均成交价, ASP]", "aliases: [净营收]")
        .replace("# 成交均价", "# 净销售额"),
    )
    _write_definition(
        tmp_path,
        "analytics-models/sales/model.md",
        """---
formatter: analytics-model
id: sales
name: 销售分析
type: analysis_model
description: 销售分析模型
semantic_assets:
  measures: [measure:average-price]
  dimensions: []
  grains: []
asset_relations: []
---

# 销售分析
""",
    )

    first_page = discover_semantic_definitions(
        query="",
        kinds=["measure"],
        limit=1,
        session_id="session-a",
        paths=paths,
    )
    second_page = discover_semantic_definitions(
        query="",
        kinds=["measure"],
        cursor=str(first_page["next_cursor"]),
        limit=1,
        session_id="session-a",
        paths=paths,
    )
    targeted = discover_semantic_definitions(
        query="ASP",
        kinds=["measure"],
        session_id="session-a",
        paths=paths,
    )

    assert first_page["catalog_count"] == 2
    assert first_page["match_count"] == 2
    assert first_page["next_cursor"]
    assert len(first_page["candidates"] + second_page["candidates"]) == 2
    assert targeted["candidates"][0]["id"] == "measure:average-price"
    assert targeted["candidates"][0]["match_reasons"] == ["exact_alias"]
    assert targeted["candidates"][0]["referenced_by"][0]["id"] == "sales"
    assert str(tmp_path) not in json.dumps(targeted, ensure_ascii=False)


def test_prepare_requires_targeted_session_bound_current_discovery(tmp_path) -> None:
    paths = PuddingClawPaths(tmp_path)
    common = {
        "logical_path": "semantic-assets/measures/average-price/measure.md",
        "candidate_markdown": _candidate(),
        "session_id": "session-a",
        "brief": MEASURE_BRIEF,
        "paths": paths,
    }

    with pytest.raises(SemanticAuthoringError) as missing:
        _prepare_semantic_markdown(**common)
    assert missing.value.code == "discovery_required"

    inventory = discover_semantic_definitions(
        query="",
        kinds=["measure"],
        session_id="session-a",
        paths=paths,
    )
    with pytest.raises(SemanticAuthoringError) as inventory_only:
        _prepare_semantic_markdown(**common, discovery_receipt_id=inventory["receipt_id"])
    assert inventory_only.value.code == "targeted_discovery_required"

    other_session = discover_semantic_definitions(
        query="成交均价",
        kinds=["measure"],
        session_id="session-b",
        paths=paths,
    )
    with pytest.raises(SemanticAuthoringError) as wrong_session:
        _prepare_semantic_markdown(**common, discovery_receipt_id=other_session["receipt_id"])
    assert wrong_session.value.code == "discovery_session_mismatch"

    targeted = discover_semantic_definitions(
        query="成交均价",
        kinds=["measure"],
        session_id="session-a",
        paths=paths,
    )
    _write_definition(
        tmp_path,
        "semantic-assets/measures/net-sales/measure.md",
        _candidate().replace("成交均价", "净销售额"),
    )
    with pytest.raises(SemanticAuthoringError) as stale:
        _prepare_semantic_markdown(**common, discovery_receipt_id=targeted["receipt_id"])
    assert stale.value.code == "discovery_stale"


def test_prepare_treats_semantically_empty_query_as_inventory(tmp_path) -> None:
    paths = PuddingClawPaths(tmp_path)
    discovery = discover_semantic_definitions(
        query="!!!",
        kinds=["measure"],
        session_id="session-a",
        paths=paths,
    )

    with pytest.raises(SemanticAuthoringError) as exc_info:
        _prepare_semantic_markdown(
            logical_path="semantic-assets/measures/average-price/measure.md",
            candidate_markdown=_candidate(),
            discovery_receipt_id=discovery["receipt_id"],
            session_id="session-a",
            brief=MEASURE_BRIEF,
            paths=paths,
        )

    assert discovery["mode"] == "inventory"
    assert discovery["decision_required"] is False
    assert exc_info.value.code == "targeted_discovery_required"


@pytest.mark.parametrize("field", ["query", "mode", "session_id", "expires_at", "complete", "kinds"])
def test_prepare_rejects_tampered_discovery_receipt(tmp_path, field: str) -> None:
    paths = PuddingClawPaths(tmp_path)
    discovery = discover_semantic_definitions(
        query="成交均价",
        kinds=["measure"],
        session_id="session-a",
        paths=paths,
    )
    receipt_path = (
        tmp_path
        / "state"
        / "semantic-steward"
        / "discoveries"
        / discovery["receipt_id"]
        / "receipt.json"
    )
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    replacements = {
        "query": "净销售额",
        "mode": "inventory",
        "session_id": "session-b",
        "expires_at": receipt["expires_at"] + 3600,
        "complete": not receipt["complete"],
        "kinds": ["measure", "dimension"],
    }
    receipt[field] = replacements[field]
    receipt_path.write_text(json.dumps(receipt, ensure_ascii=False), encoding="utf-8")

    with pytest.raises(SemanticAuthoringError) as exc_info:
        _prepare_semantic_markdown(
            logical_path="semantic-assets/measures/average-price/measure.md",
            candidate_markdown=_candidate(),
            discovery_receipt_id=discovery["receipt_id"],
            session_id="session-a",
            brief=MEASURE_BRIEF,
            paths=paths,
        )

    assert exc_info.value.code == "discovery_receipt_integrity_mismatch"


def test_prepare_rejects_incomplete_targeted_discovery(tmp_path) -> None:
    paths = PuddingClawPaths(tmp_path)
    _write_definition(
        tmp_path,
        "semantic-assets/measures/average-price/measure.md",
        _candidate(),
    )
    _write_definition(
        tmp_path,
        "semantic-assets/measures/average-price-copy/measure.md",
        _candidate().replace("name: 成交均价", "name: 成交均价副本"),
    )
    discovery = discover_semantic_definitions(
        query="成交均价",
        kinds=["measure"],
        limit=1,
        session_id="session-a",
        paths=paths,
    )

    with pytest.raises(SemanticAuthoringError) as exc_info:
        _prepare_semantic_markdown(
            logical_path="semantic-assets/measures/new-average-price/measure.md",
            candidate_markdown=_candidate(),
            discovery_receipt_id=discovery["receipt_id"],
            session_id="session-a",
            brief=MEASURE_BRIEF,
            paths=paths,
        )

    assert discovery["complete"] is False
    assert exc_info.value.code == "discovery_incomplete"


def test_prepare_existing_definition_requires_target_in_discovery_results(tmp_path) -> None:
    paths = PuddingClawPaths(tmp_path)
    _write_definition(
        tmp_path,
        "semantic-assets/measures/average-price/measure.md",
        _candidate(description="旧口径"),
    )
    discovery = discover_semantic_definitions(
        query="不存在的其他指标",
        kinds=["measure"],
        session_id="session-a",
        paths=paths,
    )

    with pytest.raises(SemanticAuthoringError) as exc_info:
        _prepare_semantic_markdown(
            logical_path="semantic-assets/measures/average-price/measure.md",
            candidate_markdown=_candidate(description="新口径"),
            discovery_receipt_id=discovery["receipt_id"],
            session_id="session-a",
            brief=MEASURE_BRIEF,
            paths=paths,
        )

    assert discovery["candidates"] == []
    assert exc_info.value.code == "discovery_target_not_returned"


def test_publish_rejects_catalog_change_after_preparation(tmp_path) -> None:
    paths = PuddingClawPaths(tmp_path)
    plan = prepare_semantic_markdown(
        logical_path="semantic-assets/measures/average-price/measure.md",
        candidate_markdown=_candidate(),
        session_id="session-a",
        brief=MEASURE_BRIEF,
        paths=paths,
    )
    _write_definition(
        tmp_path,
        "semantic-assets/measures/net-sales/measure.md",
        _candidate().replace("成交均价", "净销售额"),
    )

    with pytest.raises(SemanticAuthoringError) as exc_info:
        publish_semantic_markdown(
            plan_id=plan["plan_id"],
            plan_digest=plan["plan_digest"],
            session_id="session-a",
            paths=paths,
        )

    assert exc_info.value.code == "discovery_stale"
    assert not (
        tmp_path / "definitions" / "semantic-assets" / "measures" / "average-price" / "measure.md"
    ).exists()


def test_prepare_measure_repairs_only_target_derived_frontmatter(tmp_path) -> None:
    paths = PuddingClawPaths(tmp_path)

    plan = prepare_semantic_markdown(
        logical_path="semantic-assets/measures/average-price/measure.md",
        candidate_markdown=_candidate(),
        baseline_digest="absent",
        session_id="session-a",
        brief=MEASURE_BRIEF,
        paths=paths,
    )

    assert plan["status"] == "prepared"
    assert plan["baseline_digest"] is None
    assert "formatter: semantic-asset" in plan["technical_diff"]
    assert "type: measure" in plan["technical_diff"]
    assert "authoring_schema" not in plan["technical_diff"]
    assert plan["body_preview"] == MEASURE_BODY.rstrip() + "\n"
    assert any("别名" in item for item in plan["machine_effect_summary"])
    assert not (tmp_path / "definitions" / "semantic-assets" / "measures" / "average-price" / "measure.md").exists()


def test_prepare_rejects_unresolved_brief(tmp_path) -> None:
    with pytest.raises(SemanticAuthoringError) as exc_info:
        prepare_semantic_markdown(
            logical_path="semantic-assets/measures/average-price/measure.md",
            candidate_markdown=_candidate(),
            brief={**MEASURE_BRIEF, "unresolved": ["退货是否计入"]},
            session_id="session-a",
            paths=PuddingClawPaths(tmp_path),
        )

    assert exc_info.value.code == "candidate_invalid"


def test_prepare_rejects_placeholder_business_body(tmp_path) -> None:
    with pytest.raises(SemanticAuthoringError) as exc_info:
        prepare_semantic_markdown(
            logical_path="semantic-assets/measures/average-price/measure.md",
            candidate_markdown=_candidate().replace("成交均价表示", "待补充：成交均价表示"),
            brief=MEASURE_BRIEF,
            session_id="session-a",
            paths=PuddingClawPaths(tmp_path),
        )

    assert exc_info.value.code == "candidate_invalid"


def test_publish_measure_uses_baseline_cas_and_loads_registry(tmp_path) -> None:
    paths = PuddingClawPaths(tmp_path)
    plan = prepare_semantic_markdown(
        logical_path="semantic-assets/measures/average-price/measure.md",
        candidate_markdown=_candidate(),
        session_id="session-a",
        brief=MEASURE_BRIEF,
        paths=paths,
    )

    receipt = publish_semantic_markdown(
        plan_id=plan["plan_id"],
        plan_digest=plan["plan_digest"],
        session_id="session-a",
        paths=paths,
    )

    target = tmp_path / "definitions" / "semantic-assets" / "measures" / "average-price" / "measure.md"
    assert receipt["ok"] is True
    assert receipt["published_digest"] == plan["candidate_digest"]
    assert target.is_file()
    assert MEASURE_BODY in target.read_text(encoding="utf-8")


@pytest.mark.parametrize(
    ("logical_path", "kind", "candidate", "topics"),
    [
        (
            "semantic-assets/grains/order/grain.md",
            "grain",
            GRAIN_MARKDOWN,
            ["business_object", "identity", "deduplication", "rollup", "examples"],
        ),
        (
            "semantic-assets/dimensions/order-channel/dimension.md",
            "dimension",
            DIMENSION_MARKDOWN,
            ["business_meaning", "resolution", "unknowns", "examples"],
        ),
        (
            "semantic-assets/relations/order-customer/relation.md",
            "relation",
            RELATION_MARKDOWN,
            ["endpoints", "keys", "cardinality", "risk"],
        ),
        (
            "analytics-models/order-operations/model.md",
            "analytics_model",
            MODEL_MARKDOWN,
            ["goal", "questions", "dependencies", "scope", "output", "acceptance"],
        ),
    ],
)
def test_prepare_and_publish_supported_definition_kinds(
    tmp_path,
    logical_path: str,
    kind: str,
    candidate: str,
    topics: list[str],
) -> None:
    paths = PuddingClawPaths(tmp_path)

    plan = prepare_semantic_markdown(
        logical_path=logical_path,
        candidate_markdown=candidate,
        baseline_digest="absent",
        session_id="session-a",
        brief=_brief(kind, topics),
        paths=paths,
    )
    receipt = publish_semantic_markdown(
        plan_id=plan["plan_id"],
        plan_digest=plan["plan_digest"],
        session_id="session-a",
        paths=paths,
    )

    assert plan["kind"] == kind
    assert plan["status"] == "prepared"
    assert receipt["ok"] is True
    assert (tmp_path / "definitions" / logical_path).is_file()
    if kind == "analytics_model":
        assert "id: order-operations" in plan["technical_diff"]


def test_prepare_routes_entity_lookup_dimension_to_dedicated_builder(tmp_path) -> None:
    candidate = DIMENSION_MARKDOWN.replace("source_field", "entity_lookup").replace(
        "  bindings:\n", "  canonical:\n    key: entity_key\n    fields: [name]\n  reference_path: refs/entities.csv\n  bindings:\n"
    )

    with pytest.raises(SemanticAuthoringError) as exc_info:
        prepare_semantic_markdown(
            logical_path="semantic-assets/dimensions/order-channel/dimension.md",
            candidate_markdown=candidate,
            session_id="session-a",
            brief=_brief(
                "dimension",
                ["business_meaning", "resolution", "unknowns", "examples"],
            ),
            paths=PuddingClawPaths(tmp_path),
        )

    assert exc_info.value.code == "candidate_invalid"
    assert "entity_lookup_requires_dimension_builder" in str(exc_info.value)


def test_prepare_dimension_requires_concrete_mapping_in_body(tmp_path) -> None:
    candidate = DIMENSION_MARKDOWN.replace("warehouse.orders", "warehouse.secret").replace(
        "`channel_name`", "`secret_column`", 1
    )

    with pytest.raises(SemanticAuthoringError) as exc_info:
        prepare_semantic_markdown(
            logical_path="semantic-assets/dimensions/order-channel/dimension.md",
            candidate_markdown=candidate,
            session_id="session-a",
            brief=_brief(
                "dimension",
                ["business_meaning", "resolution", "unknowns", "examples"],
            ),
            paths=PuddingClawPaths(tmp_path),
        )

    assert exc_info.value.code == "candidate_invalid"
    assert "business_frontmatter_not_auditable_in_body" in str(exc_info.value)


def test_prepare_relation_requires_concrete_runtime_values_in_body(tmp_path) -> None:
    candidate = RELATION_MARKDOWN.replace("warehouse.orders", "warehouse.secret_orders", 1).replace(
        "customer_id", "secret_join_key", 1
    )

    with pytest.raises(SemanticAuthoringError) as exc_info:
        prepare_semantic_markdown(
            logical_path="semantic-assets/relations/order-customer/relation.md",
            candidate_markdown=candidate,
            session_id="session-a",
            brief=_brief("relation", ["endpoints", "keys", "cardinality", "risk"]),
            paths=PuddingClawPaths(tmp_path),
        )

    assert exc_info.value.code == "candidate_invalid"
    assert "business_frontmatter_not_auditable_in_body" in str(exc_info.value)


def test_prepare_relation_rejects_mapping_outside_endpoint_keys(tmp_path) -> None:
    candidate = RELATION_MARKDOWN.replace("left: [customer_id]", "left: [different_left_key]").replace(
        "两端都使用 `customer_id`", "左侧映射 `different_left_key`，右侧使用 `customer_id`"
    )

    with pytest.raises(SemanticAuthoringError) as exc_info:
        prepare_semantic_markdown(
            logical_path="semantic-assets/relations/order-customer/relation.md",
            candidate_markdown=candidate,
            session_id="session-a",
            brief=_brief("relation", ["endpoints", "keys", "cardinality", "risk"]),
            paths=PuddingClawPaths(tmp_path),
        )

    assert exc_info.value.code == "candidate_invalid"
    assert "relation_key_mapping_conflict" in str(exc_info.value)


def test_prepare_model_rejects_missing_package_template(tmp_path) -> None:
    candidate = MODEL_MARKDOWN.replace(
        "templates: {}\ndefault_template: ''",
        "templates:\n  executive: templates/executive.md\ndefault_template: executive",
    ).replace(
        "暂不选择语义资产、关系或 Guardrail。",
        "暂不选择语义资产、关系或 Guardrail；输出模板为 `templates/executive.md`。",
    )

    with pytest.raises(SemanticAuthoringError) as exc_info:
        prepare_semantic_markdown(
            logical_path="analytics-models/order-operations/model.md",
            candidate_markdown=candidate,
            session_id="session-a",
            brief=_brief(
                "analytics_model",
                ["goal", "questions", "dependencies", "scope", "output", "acceptance"],
            ),
            paths=PuddingClawPaths(tmp_path),
        )

    assert exc_info.value.code == "candidate_invalid"
    assert "missing_model_resource" in str(exc_info.value)


def test_publish_revalidates_model_package_dependencies(tmp_path) -> None:
    paths = PuddingClawPaths(tmp_path)
    template = tmp_path / "definitions" / "analytics-models" / "order-operations" / "templates" / "executive.md"
    template.parent.mkdir(parents=True)
    template.write_text("# Executive template\n", encoding="utf-8")
    candidate = MODEL_MARKDOWN.replace(
        "templates: {}\ndefault_template: ''",
        "templates:\n  executive: templates/executive.md\ndefault_template: executive",
    ).replace(
        "暂不选择语义资产、关系或 Guardrail。",
        "暂不选择语义资产、关系或 Guardrail；输出模板为 `templates/executive.md`。",
    )
    plan = prepare_semantic_markdown(
        logical_path="analytics-models/order-operations/model.md",
        candidate_markdown=candidate,
        session_id="session-a",
        brief=_brief(
            "analytics_model",
            ["goal", "questions", "dependencies", "scope", "output", "acceptance"],
        ),
        paths=paths,
    )
    template.unlink()

    with pytest.raises(SemanticAuthoringError) as exc_info:
        publish_semantic_markdown(
            plan_id=plan["plan_id"],
            plan_digest=plan["plan_digest"],
            session_id="session-a",
            paths=paths,
        )

    assert exc_info.value.code == "definition_dependencies_changed"
    assert not (tmp_path / "definitions" / "analytics-models" / "order-operations" / "model.md").exists()


def test_prepare_model_rejects_untyped_semantic_reference(tmp_path) -> None:
    candidate = MODEL_MARKDOWN.replace("  measures: []", "  measures: [net-sales]").replace(
        "暂不选择语义资产、关系或 Guardrail。",
        "选择 Measure `net-sales`，暂不选择关系或 Guardrail。",
    )

    with pytest.raises(SemanticAuthoringError) as exc_info:
        prepare_semantic_markdown(
            logical_path="analytics-models/order-operations/model.md",
            candidate_markdown=candidate,
            session_id="session-a",
            brief=_brief(
                "analytics_model",
                ["goal", "questions", "dependencies", "scope", "output", "acceptance"],
            ),
            paths=PuddingClawPaths(tmp_path),
        )

    assert exc_info.value.code == "candidate_invalid"
    assert "noncanonical_model_reference" in str(exc_info.value)


def test_prepare_model_rejects_missing_table_asset(tmp_path) -> None:
    candidate = MODEL_MARKDOWN.replace("warehouse.orders", "table_asset:ghost")

    with pytest.raises(SemanticAuthoringError) as exc_info:
        prepare_semantic_markdown(
            logical_path="analytics-models/order-operations/model.md",
            candidate_markdown=candidate,
            session_id="session-a",
            brief=_brief(
                "analytics_model",
                ["goal", "questions", "dependencies", "scope", "output", "acceptance"],
            ),
            paths=PuddingClawPaths(tmp_path),
        )

    assert exc_info.value.code == "candidate_invalid"
    assert "missing_model_data_asset" in str(exc_info.value)


def test_prepare_model_rejects_relation_with_unselected_endpoint(tmp_path) -> None:
    paths = PuddingClawPaths(tmp_path)
    relation_plan = prepare_semantic_markdown(
        logical_path="semantic-assets/relations/order-customer/relation.md",
        candidate_markdown=RELATION_MARKDOWN,
        session_id="session-a",
        brief=_brief("relation", ["endpoints", "keys", "cardinality", "risk"]),
        paths=paths,
    )
    publish_semantic_markdown(
        plan_id=relation_plan["plan_id"],
        plan_digest=relation_plan["plan_digest"],
        session_id="session-a",
        paths=paths,
    )
    candidate = MODEL_MARKDOWN.replace(
        "asset_relations: []",
        "asset_relations: [relation:order-customer]",
    ).replace(
        "暂不选择语义资产、关系或 Guardrail。",
        "选择关系 `relation:order-customer`，但暂不选择语义资产或 Guardrail。",
    )

    with pytest.raises(SemanticAuthoringError) as exc_info:
        prepare_semantic_markdown(
            logical_path="analytics-models/order-operations/model.md",
            candidate_markdown=candidate,
            session_id="session-a",
            brief=_brief(
                "analytics_model",
                ["goal", "questions", "dependencies", "scope", "output", "acceptance"],
            ),
            paths=paths,
        )

    assert exc_info.value.code == "candidate_invalid"
    assert "invalid_model_dependency_graph" in str(exc_info.value)


def test_publish_revalidates_relation_dependency_after_registry_cache(tmp_path) -> None:
    paths = PuddingClawPaths(tmp_path)
    dimension_plan = prepare_semantic_markdown(
        logical_path="semantic-assets/dimensions/order-channel/dimension.md",
        candidate_markdown=DIMENSION_MARKDOWN,
        session_id="session-a",
        brief=_brief(
            "dimension",
            ["business_meaning", "resolution", "unknowns", "examples"],
        ),
        paths=paths,
    )
    publish_semantic_markdown(
        plan_id=dimension_plan["plan_id"],
        plan_digest=dimension_plan["plan_digest"],
        session_id="session-a",
        paths=paths,
    )
    relation_plan = prepare_semantic_markdown(
        logical_path="semantic-assets/relations/order-channel/relation.md",
        candidate_markdown=DIMENSION_BINDING_RELATION_MARKDOWN,
        session_id="session-a",
        brief=_brief("relation", ["endpoints", "keys", "cardinality", "risk"]),
        paths=paths,
    )
    dimension_path = (
        tmp_path / "definitions" / "semantic-assets" / "dimensions" / "order-channel" / "dimension.md"
    )
    dimension_path.unlink()

    with pytest.raises(SemanticAuthoringError) as exc_info:
        publish_semantic_markdown(
            plan_id=relation_plan["plan_id"],
            plan_digest=relation_plan["plan_digest"],
            session_id="session-a",
            paths=paths,
        )

    assert exc_info.value.code == "definition_dependencies_changed"


def test_publish_rejects_stale_manual_edit(tmp_path) -> None:
    paths = PuddingClawPaths(tmp_path)
    target = tmp_path / "definitions" / "semantic-assets" / "measures" / "average-price" / "measure.md"
    target.parent.mkdir(parents=True)
    target.write_text(_candidate(description="旧口径"), encoding="utf-8")
    plan = prepare_semantic_markdown(
        logical_path="semantic-assets/measures/average-price/measure.md",
        candidate_markdown=_candidate(description="新口径"),
        session_id="session-a",
        brief=MEASURE_BRIEF,
        paths=paths,
    )
    target.write_text(_candidate(description="人工修改后的口径"), encoding="utf-8")

    with pytest.raises(SemanticAuthoringError) as exc_info:
        publish_semantic_markdown(
            plan_id=plan["plan_id"],
            plan_digest=plan["plan_digest"],
            session_id="session-a",
            paths=paths,
        )

    assert exc_info.value.code == "baseline_changed"
    assert "人工修改后的口径" in target.read_text(encoding="utf-8")


def test_publish_rejects_tampered_prepared_plan(tmp_path) -> None:
    paths = PuddingClawPaths(tmp_path)
    plan = prepare_semantic_markdown(
        logical_path="semantic-assets/measures/average-price/measure.md",
        candidate_markdown=_candidate(),
        session_id="session-a",
        brief=MEASURE_BRIEF,
        paths=paths,
    )
    plan_path = tmp_path / "state" / "semantic-steward" / "plans" / plan["plan_id"] / "plan.json"
    stored = json.loads(plan_path.read_text(encoding="utf-8"))
    stored["candidate_markdown"] = stored["candidate_markdown"].replace("成交均价", "被篡改的定义")
    plan_path.write_text(json.dumps(stored, ensure_ascii=False), encoding="utf-8")

    with pytest.raises(SemanticAuthoringError) as exc_info:
        publish_semantic_markdown(
            plan_id=plan["plan_id"],
            plan_digest=plan["plan_digest"],
            session_id="session-a",
            paths=paths,
        )

    assert exc_info.value.code == "plan_integrity_mismatch"


def test_measure_effect_contract_does_not_treat_type_as_formatting_only() -> None:
    effects = {item["field"]: item for item in inspect_frontmatter_contract("measure")}

    assert effects["formatter"]["safe_auto_repair"] is True
    assert effects["type"]["safe_auto_repair"] is False
    assert "routing" in effects["type"]["effect"]
    assert effects["aliases"]["safe_auto_repair"] is False
    assert effects["description"]["safe_auto_repair"] is False


def test_semantic_steward_tool_factory_exposes_discovery_prepare_and_publish() -> None:
    tools = create_semantic_steward_tools()

    assert {tool.name for tool in tools} == {
        "discover_semantic_definitions",
        "prepare_semantic_markdown",
        "publish_semantic_markdown",
    }


def test_prepare_tool_returns_digest_bound_plan(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("PUDDINGCLAW_HOME", str(tmp_path))
    discovery_tool = DiscoverSemanticDefinitionsTool(session_id="session-a")
    prepare_tool = PrepareSemanticMarkdownTool(session_id="session-a")
    discovery = json.loads(discovery_tool.invoke({"query": "成交均价", "kinds": ["measure"]}))

    prepared = prepare_tool.invoke(
        {
            "logical_path": "semantic-assets/measures/average-price/measure.md",
            "candidate_markdown": _candidate(),
            "baseline_digest": "absent",
            "discovery_receipt_id": discovery["receipt_id"],
            "authoring_brief": MEASURE_BRIEF,
        }
    )
    prepared_payload = json.loads(prepared)
    assert prepared_payload["ok"] is True
    assert prepared_payload["status"] == "prepared"


def test_publish_tool_requires_one_call_harness_approval(tmp_path) -> None:
    pipeline = ToolExecutionPipeline(
        known_tools={"publish_semantic_markdown"},
        backend_mode="spawn",
    )
    request = ToolCallRequest(
        tool_call={
            "id": "publish-call",
            "name": "publish_semantic_markdown",
            "args": {"plan_id": "semantic-plan-1", "plan_digest": "sha256:approved"},
        },
        tool=None,
        state={},
        runtime=SimpleNamespace(context={"workspace_path": str(tmp_path)}),
    )
    changed_request = ToolCallRequest(
        tool_call={
            "id": "publish-call-2",
            "name": "publish_semantic_markdown",
            "args": {"plan_id": "semantic-plan-1", "plan_digest": "sha256:changed"},
        },
        tool=None,
        state={},
        runtime=SimpleNamespace(context={"workspace_path": str(tmp_path)}),
    )
    result = pipeline._preflight(request)
    fingerprint = pipeline._permission_fingerprint_command(request, pipeline._action_preview(request))
    changed_fingerprint = pipeline._permission_fingerprint_command(
        changed_request,
        pipeline._action_preview(changed_request),
    )

    assert result.decision is PolicyDecision.ASK
    assert result.reason == "digest_bound_semantic_definition_publish"
    assert fingerprint != changed_fingerprint


def test_prepare_tool_redacts_host_paths_from_unexpected_errors(monkeypatch) -> None:
    forbidden_home = "/System/Library/puddingclaw-semantic-test"
    monkeypatch.setenv("PUDDINGCLAW_HOME", forbidden_home)
    tool = PrepareSemanticMarkdownTool(session_id="session-a")

    payload = json.loads(
        tool.invoke(
            {
                "logical_path": "semantic-assets/measures/average-price/measure.md",
                "candidate_markdown": _candidate(),
                "baseline_digest": "absent",
                "discovery_receipt_id": "semantic-discovery-missing",
                "authoring_brief": MEASURE_BRIEF,
            }
        )
    )

    assert payload["ok"] is False
    assert forbidden_home not in payload["message"]


def test_legacy_chat_excludes_semantic_publication_tools() -> None:
    assert BUSINESS_TOOLSETS["semantic_steward"] == {
        "discover_semantic_definitions",
        "prepare_semantic_markdown",
        "publish_semantic_markdown",
    }


def test_legacy_write_file_cannot_modify_semantic_definitions(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("PUDDINGCLAW_HOME", str(tmp_path))
    tool = create_write_file_tool(tmp_path)

    result = tool.invoke(
        {
            "file_path": "semantic-assets/measures/sales/measure.md",
            "content": _candidate(),
        }
    )

    assert "Access denied" in result
    assert "Semantic Steward" in result
    assert not (tmp_path / "definitions" / "semantic-assets" / "measures" / "sales" / "measure.md").exists()
