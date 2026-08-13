import json
from types import SimpleNamespace

import pytest
from langchain.agents.middleware.types import ToolCallRequest

from analytics.semantic_authoring.contracts import inspect_frontmatter_contract
from analytics.semantic_authoring.service import (
    SemanticAuthoringError,
    prepare_semantic_markdown,
    publish_semantic_markdown,
)
from harness.tool_execution import PolicyDecision, ToolExecutionPipeline
from runtime_identity.paths import PuddingClawPaths
from tools.semantic_steward_tool import (
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


def _candidate(*, description: str = "每个售出单位对应的净成交金额") -> str:
    return f"""---
name: 成交均价
description: {description}
aliases: [平均成交价, ASP]
tags: [销售]
version: 0.1.0
---

{MEASURE_BODY}"""


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


def test_measure_effect_contract_does_not_treat_type_as_formatting_only() -> None:
    effects = {item["field"]: item for item in inspect_frontmatter_contract("measure")}

    assert effects["formatter"]["safe_auto_repair"] is True
    assert effects["type"]["safe_auto_repair"] is False
    assert "routing" in effects["type"]["effect"]
    assert effects["aliases"]["safe_auto_repair"] is False
    assert effects["description"]["safe_auto_repair"] is False


def test_semantic_steward_tool_factory_exposes_only_prepare_and_publish() -> None:
    tools = create_semantic_steward_tools()

    assert {tool.name for tool in tools} == {
        "prepare_semantic_markdown",
        "publish_semantic_markdown",
    }


def test_prepare_tool_returns_digest_bound_plan(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("PUDDINGCLAW_HOME", str(tmp_path))
    prepare_tool = PrepareSemanticMarkdownTool(session_id="session-a")

    prepared = prepare_tool.invoke(
        {
            "logical_path": "semantic-assets/measures/average-price/measure.md",
            "candidate_markdown": _candidate(),
            "baseline_digest": "absent",
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
                "authoring_brief": MEASURE_BRIEF,
            }
        )
    )

    assert payload["ok"] is False
    assert forbidden_home not in payload["message"]


def test_legacy_chat_excludes_semantic_publication_tools() -> None:
    assert BUSINESS_TOOLSETS["semantic_steward"] == {
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
