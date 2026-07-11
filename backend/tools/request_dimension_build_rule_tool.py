"""HITL tool for selecting a semantic-dimension build rule."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Type

from langchain_core.tools import BaseTool
from langgraph.types import interrupt
from pydantic import BaseModel, Field

from graph.dimension_build_resume import dimension_build_resume_registry
from knowledge.semantic_dimension_crosswalk import list_registered_sources


class DimensionBuildCandidateInput(BaseModel):
    kind: str = Field(description="attachment, table_asset, database_table, or active_crosswalk")
    attachment_id: str | None = None
    asset_id: str | None = None
    source_id: str | None = None
    table: str | None = None
    dimension_id: str | None = None


class DimensionBuildCandidate(BaseModel):
    id: str = Field(description="Stable candidate id within this HITL request.")
    display_name: str
    input: DimensionBuildCandidateInput
    fields: list[str] = Field(min_length=1)
    suggested_key_fields: list[str] = Field(default_factory=list)
    suggested_output_fields: list[str] = Field(default_factory=list)
    suggested_source_id: str = ""
    suggested_source_name: str = ""


class RequestDimensionBuildRuleInput(BaseModel):
    dimension_id: str
    title: str = "确认维度构建规则"
    reason: str = "需要确认输入表、基准表和键字段。"
    operation: str = Field(default="refresh", description="refresh rebuilds the canonical baseline; append_source keeps the published canonical baseline and only adds a source column.")
    candidates: list[DimensionBuildCandidate] = Field(min_length=1)
    adapter: str = "entity_crosswalk_v1"
    reference_path: str = "references/active_crosswalk.json"


class RequestDimensionBuildRuleTool(BaseTool):
    name: str = "request_dimension_build_rule"
    description: str = (
        "Request a user-confirmed, structured semantic-dimension build rule. "
        "Use after inspecting all candidate inputs and fields, before enqueue_semantic_dimension_build. "
        "The user selects the canonical input, source input and keys in a HITL card; do not enqueue until this tool resumes with a confirmed build_rule."
    )
    args_schema: Type[BaseModel] = RequestDimensionBuildRuleInput
    risk_level: str = "safe"
    session_id: str = ""
    query_id: str = ""

    class Config:
        arbitrary_types_allowed = True

    def _run(self, **kwargs: Any) -> str:
        raise RuntimeError("Use async execution for dimension build HITL")

    async def _arun(self, **kwargs: Any) -> str:
        if not self.session_id:
            return "❌ 无法创建维度构建确认：缺少会话标识。"
        tool_call_id = str(kwargs.pop("tool_call_id", "") or "")
        base_dir = Path(__file__).resolve().parents[1]
        dimension_id = str(kwargs["dimension_id"])
        operation = str(kwargs["operation"] or "refresh")
        candidates = [item.model_dump() if isinstance(item, BaseModel) else dict(item) for item in kwargs["candidates"]]
        locked_canonical_candidate_id = ""
        if operation == "append_source":
            active_path = base_dir / "semantic-assets" / "dimensions" / dimension_id / "references" / "active_crosswalk.json"
            if not active_path.is_file():
                return "❌ 无法追加来源：当前维度尚未发布 active_crosswalk.json。请先完成一次规范基准构建。"
            try:
                active = json.loads(active_path.read_text(encoding="utf-8"))
                records = [item for item in active.get("records") or [] if isinstance(item, dict) and isinstance(item.get("entity"), dict)]
                fields = list((active.get("canonical_key") or {}).get("fields") or [])
                if not fields and records:
                    fields = [key for key in records[0]["entity"].keys() if key != "entity_key"]
                if not records or not fields or any(any(field not in item["entity"] for field in fields) for item in records):
                    raise ValueError("active Crosswalk 缺少可复用的规范字段")
            except (ValueError, json.JSONDecodeError) as exc:
                return f"❌ 无法读取当前规范基准：{exc}"
            locked_canonical_candidate_id = "__active_canonical__"
            candidates.insert(0, {
                "id": locked_canonical_candidate_id,
                "display_name": f"当前规范基准 · {dimension_id} {active.get('version') or ''}（{len(records)} 条）",
                "input": {"kind": "active_crosswalk", "dimension_id": dimension_id},
                "fields": fields,
                "suggested_key_fields": fields,
                "suggested_output_fields": fields,
            })
        request = dimension_build_resume_registry.create(
            session_id=self.session_id,
            query_id=self.query_id,
            tool_call_id=tool_call_id,
            payload={
                "dimension_id": dimension_id,
                "title": kwargs["title"],
                "reason": kwargs["reason"],
                "operation": operation,
                "candidates": candidates,
                "locked_canonical_candidate_id": locked_canonical_candidate_id,
                "registered_sources": list_registered_sources(base_dir, dimension_id),
                "rule_template": {
                    "dimension_id": kwargs["dimension_id"],
                    "adapter": kwargs["adapter"],
                    "reference_path": kwargs["reference_path"],
                },
            },
        )
        decision = interrupt(
            {
                "type": "dimension_build_rule_request",
                "request": request,
                "decisions": [{"action": "confirm"}, {"action": "cancel"}],
            }
        )
        if not isinstance(decision, dict) or decision.get("action") != "confirm":
            return "🧩 用户取消了语义维度构建规则确认，未创建 Job。"
        rule = decision.get("build_rule")
        if not isinstance(rule, dict):
            return "❌ 维度构建确认恢复失败：缺少已验证规则。"
        return "🧩 已确认语义维度构建规则\n" + json.dumps(
            {"request_id": request["id"], "build_rule": rule}, ensure_ascii=False, indent=2
        )


def create_request_dimension_build_rule_tool() -> RequestDimensionBuildRuleTool:
    return RequestDimensionBuildRuleTool()
