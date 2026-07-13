"""HITL tool for logical dataset concat strategy selection."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Type

from langchain_core.tools import BaseTool
from langgraph.types import interrupt
from pydantic import BaseModel, Field

from analytics.table_catalog import TableAssetCatalog, TableCatalogError
from db import get_sessionmaker
from graph.logical_dataset_resume import logical_dataset_resume_registry


def _logical_dataset_business_fields(asset: dict[str, Any]) -> list[str]:
    """Return the logical schema, excluding runtime row-lineage fields."""

    logical = asset.get("logical_dataset")
    if isinstance(logical, dict):
        canonical = logical.get("canonical_columns") or (logical.get("schema") or {}).get("fields")
        if isinstance(canonical, list) and canonical:
            return [str(field) for field in canonical]
    return [
        str(field)
        for field in asset.get("columns") or []
        if not str(field).startswith("_pc_source_")
    ]


class LogicalDatasetCandidate(BaseModel):
    asset_id: str
    display_name: str
    fields: list[str] = Field(min_length=1)
    rows: int | None = None
    sheet_name: str | None = None
    source_type: str | None = None


class RequestLogicalDatasetRuleInput(BaseModel):
    title: str = "确认逻辑数据集规则"
    reason: str = "需要确认待合并表、基准表和字段差异处理方式。"
    suggested_name: str = ""
    operation: str = Field(default="create", pattern="^(create|append)$")
    target_asset_id: str = ""
    candidates: list[LogicalDatasetCandidate] = Field(min_length=1)


class RequestLogicalDatasetRuleTool(BaseTool):
    name: str = "request_logical_dataset_rule"
    description: str = (
        "Request a user-confirmed logical dataset rule after inspecting candidate table assets. "
        "For create, the HITL card chooses at least two raw source tables and one baseline table. "
        "For append, target_asset_id is the existing logical dataset baseline and candidates must be new raw sources only. "
        "The HITL card chooses the new sources and schema-drift strategy. "
        "Do not create or append a logical dataset until this tool resumes with dataset_rule."
    )
    args_schema: Type[BaseModel] = RequestLogicalDatasetRuleInput
    risk_level: str = "safe"
    session_id: str = ""
    query_id: str = ""

    class Config:
        arbitrary_types_allowed = True

    def _run(self, **kwargs: Any) -> str:
        raise RuntimeError("Use async execution for logical dataset HITL")

    async def _arun(self, **kwargs: Any) -> str:
        if not self.session_id:
            return "❌ 无法创建逻辑数据集确认：缺少会话标识。"
        tool_call_id = str(kwargs.pop("tool_call_id", "") or "")
        candidates = [item.model_dump() if isinstance(item, BaseModel) else dict(item) for item in kwargs["candidates"]]
        operation = str(kwargs["operation"])
        target_asset_id = str(kwargs["target_asset_id"] or "")
        target = next((item for item in candidates if str(item.get("asset_id")) == target_asset_id), None)
        if operation == "append":
            if not target_asset_id:
                return "❌ 追加逻辑数据集时必须提供 target_asset_id。"
            try:
                async with get_sessionmaker()() as session:
                    asset = await TableAssetCatalog(Path(__file__).resolve().parents[1]).get_asset(
                        session,
                        target_asset_id,
                        include_profile=False,
                    )
                if str(asset.get("source_type") or "") != "logical_concat":
                    return "❌ 追加目标必须是已有的逻辑数据集。"
                # Always resolve target metadata server-side. The Agent may know
                # only the stable asset ID, but the HITL card must show a human
                # readable dataset name and its actual schema baseline.
                target = {
                    "asset_id": target_asset_id,
                    "display_name": str(asset.get("file_name") or target_asset_id),
                    "fields": _logical_dataset_business_fields(asset),
                    "rows": asset.get("rows"),
                    "sheet_name": asset.get("sheet_name"),
                    "source_type": asset.get("source_type"),
                }
                # Appending never creates a new business definition. Preserve the
                # target dataset's name so the HITL card has no empty metadata form.
                if not str(kwargs.get("suggested_name") or "").strip():
                    kwargs["suggested_name"] = str(asset.get("file_name") or "")
            except TableCatalogError as exc:
                return f"❌ 无法读取追加目标：{exc}"
            # Older callers may include the target in candidates. Keep its display
            # metadata for the card, but never let it become an append source.
            candidates = [item for item in candidates if str(item.get("asset_id")) != target_asset_id]
            if not candidates:
                return "❌ 请至少提供一张要追加的原始表资产；目标逻辑数据集不能作为来源。"
        request = logical_dataset_resume_registry.create(
            session_id=self.session_id,
            query_id=self.query_id,
            tool_call_id=tool_call_id,
            payload={
                "title": kwargs["title"],
                "reason": kwargs["reason"],
                "suggested_name": kwargs["suggested_name"],
                "operation": operation,
                "target_asset_id": target_asset_id,
                "target": target,
                "candidates": candidates,
            },
        )
        decision = interrupt({"type": "logical_dataset_rule_request", "request": request, "decisions": [{"action": "confirm"}, {"action": "cancel"}]})
        if not isinstance(decision, dict) or decision.get("action") != "confirm":
            return "🧩 用户取消了逻辑数据集规则确认，未执行合并。"
        rule = decision.get("dataset_rule")
        if not isinstance(rule, dict):
            return "❌ 逻辑数据集确认恢复失败：缺少已验证规则。"
        return "🧩 已确认逻辑数据集规则\n" + json.dumps({"request_id": request["id"], "dataset_rule": rule}, ensure_ascii=False, indent=2)


def create_request_logical_dataset_rule_tool() -> RequestLogicalDatasetRuleTool:
    return RequestLogicalDatasetRuleTool()
