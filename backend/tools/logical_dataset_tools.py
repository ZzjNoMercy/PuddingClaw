"""Agent tools for inspecting, creating and extending logical table datasets."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any, Type

from langchain_core.tools import BaseTool
from pydantic import BaseModel, Field

from analytics.table_catalog import TableAssetCatalog, TableCatalogError
from db import get_sessionmaker
from graph.attachment_store import attachment_store
from knowledge.service import DEFAULT_KNOWLEDGE_BASE_ID, KnowledgeService, KnowledgeServiceError


class ListLogicalDatasetCandidatesInput(BaseModel):
    limit: int = Field(default=100, ge=1, le=500)


class EnsureAttachmentTableAssetInput(BaseModel):
    attachment_id: str = Field(description="Spreadsheet attachment ID, such as att_xxx, to register as a reusable knowledge table asset.")
    title: str | None = Field(default=None, description="Optional display title after importing into the knowledge base.")


class EnsureAttachmentTableAssetTool(BaseTool):
    name: str = "ensure_attachment_table_asset"
    description: str = (
        "Check whether an uploaded spreadsheet attachment is already available as a knowledge-base table asset. "
        "If not, import it into the knowledge base and register its sheet/table assets. "
        "Use this before logical-dataset field inspection or append when the user supplied a new attachment."
    )
    args_schema: Type[BaseModel] = EnsureAttachmentTableAssetInput
    risk_level: str = "moderate"
    session_id: str = ""

    def _run(self, **kwargs: Any) -> str:
        raise RuntimeError("Use async execution for attachment import")

    async def _arun(self, **kwargs: Any) -> str:
        attachment_id = str(kwargs.get("attachment_id") or "").strip()
        item = attachment_store.get(self.session_id, attachment_id)
        if item is None:
            return "❌ 未找到该会话附件。请重新上传后再追加到逻辑数据集。"
        if str(item.get("type") or "") != "spreadsheet":
            return "❌ 逻辑数据集只支持 Excel、CSV 或 TSV 表格附件。"
        attachment_path = Path(str(item.get("path") or ""))
        if not attachment_path.is_file():
            return "❌ 附件文件已不存在，无法导入知识库。"

        base_dir = Path(__file__).resolve().parents[1]
        try:
            async with get_sessionmaker()() as session:
                service = KnowledgeService(base_dir)
                document, ingestion = await service.ingest_generic_upload(
                    session,
                    filename=str(item.get("name") or attachment_path.name),
                    content=await asyncio.to_thread(attachment_path.read_bytes),
                    title=str(kwargs.get("title") or "").strip() or None,
                    knowledge_base_id=DEFAULT_KNOWLEDGE_BASE_ID,
                    publish_targets=["local_file"],
                )
                catalog = TableAssetCatalog(base_dir)
                assets = await catalog.register_path(
                    session,
                    Path(document.storage_path),
                    virtual_path=document.virtual_path,
                    knowledge_base_id=document.knowledge_base_id,
                    document_id=document.id,
                )
                # Field selection is the immediate next step. Generate the lightweight
                # table profile here so the Agent never needs to infer headers from an
                # ephemeral attachment path.
                profiled_assets = [
                    await catalog.generate_profile(session, asset.asset_id, include_profile=False)
                    for asset in assets
                ]
        except (KnowledgeServiceError, TableCatalogError) as exc:
            return f"❌ 导入表格附件失败：{exc}"
        except Exception as exc:
            return f"❌ 导入表格附件失败：{exc}"

        payload = {
            "attachment_id": attachment_id,
            "document_id": document.id,
            "deduplicated": bool(ingestion.get("deduplicated")) if isinstance(ingestion, dict) else False,
            "assets": [
                {
                    "asset_id": asset["asset_id"],
                    "display_name": asset["file_name"],
                    "sheet_name": asset.get("sheet_name"),
                    "virtual_path": asset["virtual_path"],
                    "fields": asset.get("columns") or [],
                }
                for asset in profiled_assets
            ],
        }
        return "✅ 表格附件已登记为知识库数据资产\n" + json.dumps(payload, ensure_ascii=False, indent=2)


class ListLogicalDatasetCandidatesTool(BaseTool):
    name: str = "list_logical_dataset_candidates"
    description: str = "List reusable table assets with IDs, fields and row counts before proposing a logical dataset merge."
    args_schema: Type[BaseModel] = ListLogicalDatasetCandidatesInput
    risk_level: str = "safe"

    def _run(self, **kwargs: Any) -> str:
        raise RuntimeError("Use async execution for logical dataset candidates")

    async def _arun(self, **kwargs: Any) -> str:
        async with get_sessionmaker()() as session:
            assets = await TableAssetCatalog(Path(__file__).resolve().parents[1]).list_assets(
                session, include_profile=False, limit=int(kwargs.get("limit") or 100)
            )
        candidates = [
            {
                "asset_id": asset["asset_id"],
                "display_name": asset["file_name"],
                "fields": asset.get("columns") or [],
                "rows": asset.get("rows"),
                "sheet_name": asset.get("sheet_name"),
                "source_type": asset.get("source_type"),
            }
            for asset in assets
            if asset.get("source_type") != "derived_concat"
        ]
        return "🧩 可用于逻辑合并的表格资产\n" + json.dumps(candidates, ensure_ascii=False, indent=2)


class ApplyLogicalDatasetRuleInput(BaseModel):
    name: str = Field(min_length=1, max_length=160)
    description: str = ""
    tags: list[str] = Field(default_factory=list)
    source_asset_ids: list[str] = Field(min_length=1, max_length=120)
    schema_mode: str = Field(pattern="^(strict|baseline_fill_missing|union_fill_missing)$")
    target_asset_id: str = ""
    preferred_intents: list[str] = Field(default_factory=list)
    direct_source_allowed: bool = True


class ApplyLogicalDatasetRuleTool(BaseTool):
    name: str = "apply_logical_dataset_rule"
    description: str = (
        "Create a logical dataset or append sources only after request_logical_dataset_rule returned a confirmed dataset_rule. "
        "For append, source_asset_ids contains only new raw sources; target_asset_id is never a source."
    )
    args_schema: Type[BaseModel] = ApplyLogicalDatasetRuleInput
    risk_level: str = "safe"

    def _run(self, **kwargs: Any) -> str:
        raise RuntimeError("Use async execution for logical dataset creation")

    async def _arun(self, **kwargs: Any) -> str:
        catalog = TableAssetCatalog(Path(__file__).resolve().parents[1])
        try:
            async with get_sessionmaker()() as session:
                target_asset_id = str(kwargs.get("target_asset_id") or "")
                if target_asset_id:
                    requested_ids = list(kwargs["source_asset_ids"])
                    if target_asset_id in requested_ids:
                        return "❌ 追加失败：目标逻辑数据集不能作为来源，请只传入新的原始表资产。"
                    current = await catalog.get_asset(session, target_asset_id, include_profile=False)
                    existing_ids = (current.get("logical_dataset") or {}).get("source_asset_ids") or []
                    append_ids = [item for item in requested_ids if item not in existing_ids]
                    asset = await catalog.append_concat_dataset_sources(
                        session,
                        asset_id=target_asset_id,
                        source_asset_ids=append_ids,
                        schema_mode=str(kwargs["schema_mode"]),
                    )
                    action = "已追加来源"
                else:
                    asset = await catalog.create_concat_dataset(
                        session,
                        name=str(kwargs["name"]),
                        description=str(kwargs.get("description") or ""),
                        tags=list(kwargs.get("tags") or []),
                        source_asset_ids=list(kwargs["source_asset_ids"]),
                        schema_mode=str(kwargs["schema_mode"]),
                        routing={"preferred_intents": list(kwargs.get("preferred_intents") or []), "direct_source_allowed": bool(kwargs.get("direct_source_allowed", True))},
                    )
                    action = "已创建"
        except TableCatalogError as exc:
            return f"❌ 逻辑数据集操作失败：{exc}"
        return "🧩 " + action + "逻辑数据集\n" + json.dumps(
            {
                "asset_id": asset["asset_id"],
                "name": asset["file_name"],
                "rows": asset.get("rows"),
                "columns": asset.get("columns"),
                "logical_dataset": asset.get("logical_dataset"),
            },
            ensure_ascii=False,
            indent=2,
        )


def create_logical_dataset_tools() -> list[BaseTool]:
    return [EnsureAttachmentTableAssetTool(), ListLogicalDatasetCandidatesTool(), ApplyLogicalDatasetRuleTool()]
