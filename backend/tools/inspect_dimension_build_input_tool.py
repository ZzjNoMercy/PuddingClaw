"""Inspect a candidate input without importing it as a long-lived data asset."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Type

import pandas as pd
from langchain_core.tools import BaseTool
from pydantic import BaseModel, Field

from analytics.table_catalog import TableAssetCatalog
from db import get_sessionmaker
from graph.attachment_store import attachment_store
from knowledge.database_sources import get_database_source, list_database_sources, list_database_table_columns


class InspectDimensionBuildInput(BaseModel):
    kind: str = Field(description="attachment, table_asset, or database_table")
    attachment_id: str | None = None
    asset_id: str | None = None
    source_id: str | None = None
    table: str | None = None


class InspectDimensionBuildInputTool(BaseTool):
    name: str = "inspect_dimension_build_input"
    description: str = (
        "Inspect candidate fields for a semantic-dimension build. Use this for an uploaded spreadsheet attachment "
        "without importing it into the knowledge base, an existing table asset, or a registered database table. "
        "Return the exact input object and field names to pass into request_dimension_build_rule."
    )
    args_schema: Type[BaseModel] = InspectDimensionBuildInput
    risk_level: str = "safe"
    session_id: str = ""

    class Config:
        arbitrary_types_allowed = True

    def _run(self, **kwargs: Any) -> str:
        raise RuntimeError("Use async execution for dimension input inspection")

    async def _arun(self, **kwargs: Any) -> str:
        kind = str(kwargs.get("kind") or "")
        payload: dict[str, Any]
        if kind == "attachment":
            attachment_id = str(kwargs.get("attachment_id") or "")
            item = attachment_store.get(self.session_id, attachment_id)
            if not item or str(item.get("type")) != "spreadsheet":
                return "❌ 未找到可用于维度构建的表格附件。"
            path = Path(str(item.get("path") or ""))
            if not path.is_file():
                return "❌ 表格附件文件已不存在。"
            try:
                if path.suffix.lower() in {".xlsx", ".xls"}:
                    fields = [str(value) for value in pd.read_excel(path, nrows=0).columns]
                else:
                    fields = [str(value) for value in pd.read_csv(path, nrows=0).columns]
            except Exception as exc:
                return f"❌ 无法读取附件字段：{exc}"
            payload = {
                "input": {"kind": "attachment", "attachment_id": attachment_id},
                "display_name": str(item.get("name") or attachment_id),
                "fields": fields,
                "temporary": True,
                "note": "该附件仅作为本次构建输入，不会自动进入数据资产。",
            }
        elif kind == "table_asset":
            asset_id = str(kwargs.get("asset_id") or "")
            async with get_sessionmaker()() as session:
                asset = await TableAssetCatalog(Path(__file__).resolve().parents[1]).get_asset(session, asset_id, include_profile=False)
            payload = {
                "input": {"kind": "table_asset", "asset_id": asset_id},
                "display_name": f"{asset['file_name']} · {asset.get('sheet_name') or '工作表'}",
                "fields": asset.get("columns") or [],
                "temporary": False,
            }
        elif kind == "database_table":
            source_id = str(kwargs.get("source_id") or "")
            table = str(kwargs.get("table") or "")
            async with get_sessionmaker()() as session:
                if not source_id:
                    sources = await list_database_sources(session)
                    matches = [
                        source for source in sources
                        if table and table in {str(item) for item in source.get("selected_tables") or []}
                    ]
                    if len(matches) == 1:
                        source_id = str(matches[0].get("id") or "")
                    elif len(matches) > 1:
                        return "🧩 数据库表存在于多个数据源，请从以下候选中选择 source_id 后重试：\n" + json.dumps(
                            [
                                {"source_id": item.get("id"), "name": item.get("name"), "database": item.get("database")}
                                for item in matches
                            ],
                            ensure_ascii=False,
                        )
                    else:
                        return f"❌ 未在已选数据库表中找到 {table}。请先在智能问数的数据资产中选择该表。"
                source = await get_database_source(session, source_id)
            fields = await list_database_table_columns(source, table)
            payload = {
                "input": {"kind": "database_table", "source_id": source_id, "table": table},
                "display_name": f"{source.get('name') if isinstance(source, dict) else source.name} · {table}",
                "fields": fields,
                "temporary": False,
            }
        else:
            return "❌ kind 必须是 attachment、table_asset 或 database_table。"
        return "🧩 维度构建输入检查\n" + json.dumps(payload, ensure_ascii=False, indent=2)


def create_inspect_dimension_build_input_tool() -> InspectDimensionBuildInputTool:
    return InspectDimensionBuildInputTool()
