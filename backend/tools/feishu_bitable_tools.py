"""Read-only Agent tools for live queries against registered Feishu Bitable sources."""

from __future__ import annotations

import asyncio
import json
from typing import Any

from langchain_core.tools import BaseTool
from pydantic import BaseModel, Field
from sqlalchemy import select

from db import get_sessionmaker
from knowledge.connectors.feishu import FeishuConnectorError, FeishuOpenApi
from knowledge.models import KnowledgeSourceConnection, KnowledgeSourceItem


class FeishuBitableDescribeInput(BaseModel):
    source_id: str = Field(description="Registered Feishu knowledge Source ID.")
    source_item_id: str = Field(default="", description="Optional linked Wiki Bitable Source Item ID.")
    table_id: str = Field(default="", description="Optional table ID when the registered locator contains multiple tables.")


class FeishuBitableListSourcesInput(BaseModel):
    include_disabled: bool = Field(default=False, description="Include disabled sources for diagnosis only.")


class FeishuBitableQueryInput(FeishuBitableDescribeInput):
    view_id: str = Field(default="", description="Optional registered or explicitly selected Feishu view ID.")
    field_names: list[str] = Field(default_factory=list, max_length=100, description="Optional exact visible field names.")
    page_size: int = Field(default=50, ge=1, le=100, description="Maximum records in this live page.")
    page_token: str = Field(default="", max_length=500, description="Opaque next-page token from the previous call.")


async def _registered_locator(
    source_id: str,
    source_item_id: str,
    requested_table_id: str,
    *,
    api: FeishuOpenApi,
) -> tuple[KnowledgeSourceConnection, dict[str, str], list[dict[str, Any]], Any]:
    sessionmaker = get_sessionmaker()
    session = sessionmaker()
    try:
        source = await session.get(KnowledgeSourceConnection, source_id)
        if source is None or source.connector_key != "feishu_wiki" or source.status == "disabled":
            raise FeishuConnectorError("已登记的飞书 Source 不存在或已停用。")
        raw: dict[str, Any] = dict(source.config_json or {})
        if source_item_id:
            item = await session.get(KnowledgeSourceItem, source_item_id)
            if item is None or item.source_connection_id != source.id or item.external_type != "bitable":
                raise FeishuConnectorError("该 Bitable 条目不属于指定 Source。")
            raw = {**raw, **dict(item.metadata_json or {})}
        app_token = str(raw.get("app_token") or "").strip()
        if not app_token:
            raise FeishuConnectorError("该 Source 尚未登记可实时查询的 Bitable 定位。")
        tables = await api.list_bitable_tables(session, source, app_token=app_token)
        visible = {str(item.get("table_id") or ""): item for item in tables}
        table_id = requested_table_id.strip() or str(raw.get("table_id") or "").strip()
        if not table_id and len(visible) == 1:
            table_id = next(iter(visible))
        if not table_id:
            raise FeishuConnectorError("该 Bitable 包含多个数据表，请从返回候选中明确 table_id。")
        if table_id not in visible:
            raise FeishuConnectorError("请求的数据表不在已登记 Source 的当前可见范围内。")
        locator = {
            "app_token": app_token,
            "table_id": table_id,
            "table_name": str(visible[table_id].get("name") or raw.get("table_name") or table_id),
            "view_id": str(raw.get("view_id") or ""),
            "source_url": str(raw.get("source_url") or ""),
        }
        return source, locator, tables, session
    except Exception:
        await session.close()
        raise


class FeishuBitableDescribeTool(BaseTool):
    name: str = "feishu_bitable_describe"
    description: str = (
        "Inspect table candidates and field schema for an already registered Feishu Bitable Source. "
        "Use this before querying unfamiliar fields. It reads live Feishu metadata and never stores record values."
    )
    args_schema: type[BaseModel] = FeishuBitableDescribeInput
    risk_level: str = "safe"
    api: FeishuOpenApi = FeishuOpenApi()

    class Config:
        arbitrary_types_allowed = True

    def _run(self, **kwargs: Any) -> str:
        return asyncio.run(self._arun(**kwargs))

    async def _arun(self, source_id: str, source_item_id: str = "", table_id: str = "") -> str:
        session = None
        try:
            _source, locator, tables, session = await _registered_locator(
                source_id, source_item_id, table_id, api=self.api
            )
            fields = await self.api.list_bitable_fields(
                session,
                _source,
                app_token=locator["app_token"],
                table_id=locator["table_id"],
            )
            return json.dumps(
                {
                    "ok": True,
                    "live": True,
                    "row_storage": False,
                    "source_id": source_id,
                    "locator": {key: value for key, value in locator.items() if key != "app_token"},
                    "tables": tables,
                    "fields": fields,
                },
                ensure_ascii=False,
            )
        except Exception as exc:  # noqa: BLE001 - tool errors are model-visible
            return json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False)
        finally:
            if session is not None:
                await session.close()


class FeishuBitableListSourcesTool(BaseTool):
    name: str = "feishu_bitable_list_sources"
    description: str = (
        "List registered Feishu Bitable live sources and linked Wiki Bitable items. Use this first when the user "
        "did not provide an exact source_id. It returns locators only and never reads record values."
    )
    args_schema: type[BaseModel] = FeishuBitableListSourcesInput
    risk_level: str = "safe"

    def _run(self, **kwargs: Any) -> str:
        return asyncio.run(self._arun(**kwargs))

    async def _arun(self, include_disabled: bool = False) -> str:
        try:
            async with get_sessionmaker()() as session:
                sources = (
                    await session.execute(
                        select(KnowledgeSourceConnection).where(
                            KnowledgeSourceConnection.connector_key == "feishu_wiki"
                        )
                    )
                ).scalars().all()
                source_by_id = {source.id: source for source in sources}
                items = (
                    await session.execute(
                        select(KnowledgeSourceItem).where(KnowledgeSourceItem.external_type == "bitable")
                    )
                ).scalars().all()
                entries: list[dict[str, Any]] = []
                represented: set[str] = set()
                for item in items:
                    source = source_by_id.get(item.source_connection_id)
                    if source is None or (source.status == "disabled" and not include_disabled):
                        continue
                    metadata = dict(item.metadata_json or {})
                    entries.append(
                        {
                            "source_id": source.id,
                            "source_name": source.name,
                            "source_status": source.status,
                            "source_item_id": item.id,
                            "title": item.title,
                            "table_id": str(metadata.get("table_id") or ""),
                            "view_id": str(metadata.get("view_id") or ""),
                            "source_url": item.source_url,
                            "entry_kind": str(metadata.get("entry_kind") or "wiki_bitable"),
                        }
                    )
                    represented.add(source.id)
                for source in sources:
                    config = dict(source.config_json or {})
                    if str(config.get("source_mode") or "") != "bitable" or source.id in represented:
                        continue
                    if source.status == "disabled" and not include_disabled:
                        continue
                    entries.append(
                        {
                            "source_id": source.id,
                            "source_name": source.name,
                            "source_status": source.status,
                            "source_item_id": "",
                            "title": str(config.get("table_name") or config.get("table_id") or source.name),
                            "table_id": str(config.get("table_id") or ""),
                            "view_id": str(config.get("view_id") or ""),
                            "source_url": str(config.get("source_url") or ""),
                            "entry_kind": str(config.get("entry_kind") or "direct_bitable"),
                        }
                    )
            return json.dumps({"ok": True, "row_storage": False, "sources": entries}, ensure_ascii=False)
        except Exception as exc:  # noqa: BLE001 - tool errors are model-visible
            return json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False)


class FeishuBitableQueryTool(BaseTool):
    name: str = "feishu_bitable_query"
    description: str = (
        "Read one bounded live page from an already registered Feishu Bitable Source. Use exact source_id and "
        "field names; call feishu_bitable_describe first when schema is unknown. Returned private records enter "
        "the current Agent/model context but are not persisted by this tool."
    )
    args_schema: type[BaseModel] = FeishuBitableQueryInput
    risk_level: str = "safe"
    api: FeishuOpenApi = FeishuOpenApi()

    class Config:
        arbitrary_types_allowed = True

    def _run(self, **kwargs: Any) -> str:
        return asyncio.run(self._arun(**kwargs))

    async def _arun(self, **kwargs: Any) -> str:
        session = None
        try:
            source, locator, _tables, session = await _registered_locator(
                str(kwargs.get("source_id") or ""),
                str(kwargs.get("source_item_id") or ""),
                str(kwargs.get("table_id") or ""),
                api=self.api,
            )
            result = await self.api.list_bitable_records_page(
                session,
                source,
                app_token=locator["app_token"],
                table_id=locator["table_id"],
                view_id=str(kwargs.get("view_id") or "") or locator["view_id"],
                field_names=list(kwargs.get("field_names") or []),
                page_size=int(kwargs.get("page_size") or 50),
                page_token=str(kwargs.get("page_token") or ""),
            )
            return json.dumps(
                {
                    "ok": True,
                    "live": True,
                    "row_storage": False,
                    "privacy": "records were read live and are present in the current Agent/model context",
                    "source_id": source.id,
                    "table_id": locator["table_id"],
                    **result,
                },
                ensure_ascii=False,
            )
        except Exception as exc:  # noqa: BLE001 - tool errors are model-visible
            return json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False)
        finally:
            if session is not None:
                await session.close()


def create_feishu_bitable_tools() -> list[BaseTool]:
    api = FeishuOpenApi()
    return [FeishuBitableListSourcesTool(), FeishuBitableDescribeTool(api=api), FeishuBitableQueryTool(api=api)]
