"""Read-later capture, reading state, and Wiki promotion API."""

from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from db import get_db_session
from knowledge.import_jobs import job_to_dict
from knowledge.models import ReadLaterItem
from knowledge.read_later import (
    create_read_later_item,
    list_read_later_items,
    promote_read_later_to_wiki,
    read_later_to_dict,
    retry_read_later_item,
)
from knowledge.service import (
    DEFAULT_KNOWLEDGE_BASE_ID,
    KnowledgeServiceError,
    _rewrite_markdown_links_for_preview,
)
from tools.fetch_url_tool import UnsafePublicURL

router = APIRouter(prefix="/read-later", tags=["read-later"])
BASE_DIR = Path(__file__).resolve().parent.parent


class ReadLaterCreateRequest(BaseModel):
    url: str = Field(min_length=4, max_length=1800)
    title: str = Field(default="", max_length=500)
    note: str = Field(default="", max_length=4000)
    tags: list[str] = Field(default_factory=list, max_length=30)
    knowledge_base_id: str = Field(default=DEFAULT_KNOWLEDGE_BASE_ID)


class ReadLaterUpdateRequest(BaseModel):
    reading_status: Literal["unread", "read", "archived"] | None = None
    title: str | None = Field(default=None, max_length=500)
    note: str | None = Field(default=None, max_length=4000)
    tags: list[str] | None = Field(default=None, max_length=30)


class ReadLaterCompileRequest(BaseModel):
    item_ids: list[str] = Field(min_length=1, max_length=100)
    import_gbrain: bool = False


@router.post("")
async def save_read_later(request: ReadLaterCreateRequest, session: AsyncSession = Depends(get_db_session)):
    try:
        item, job, deduplicated = await create_read_later_item(
            session,
            base_dir=BASE_DIR,
            url=request.url,
            title=request.title,
            note=request.note,
            tags=request.tags,
            knowledge_base_id=request.knowledge_base_id,
        )
        return {
            "item": read_later_to_dict(item),
            "job": job_to_dict(job) if job else None,
            "deduplicated": deduplicated,
        }
    except UnsafePublicURL as exc:
        raise HTTPException(status_code=400, detail=f"链接不安全或不可访问：{exc}") from exc
    except KnowledgeServiceError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("")
async def get_read_later_items(
    reading_status: str = "all",
    parse_status: str = "all",
    search: str = "",
    limit: int = 200,
    knowledge_base_id: str = DEFAULT_KNOWLEDGE_BASE_ID,
    session: AsyncSession = Depends(get_db_session),
):
    items = await list_read_later_items(
        session,
        knowledge_base_id=knowledge_base_id,
        reading_status=reading_status,
        parse_status=parse_status,
        search=search,
        limit=limit,
    )
    return {"items": [read_later_to_dict(item) for item in items]}


@router.get("/{item_id}")
async def get_read_later_item(item_id: str, session: AsyncSession = Depends(get_db_session)):
    item = await session.get(ReadLaterItem, item_id)
    if item is None:
        raise HTTPException(status_code=404, detail="稍后读记录不存在")
    content = ""
    if item.storage_path:
        try:
            content = Path(item.storage_path).read_text(encoding="utf-8", errors="replace")
            content = re.sub(r"\A---\s*\n.*?\n---\s*\n", "", content, count=1, flags=re.DOTALL)
            content = _rewrite_markdown_links_for_preview(content, base_virtual_path=item.virtual_path)
        except OSError:
            content = ""
    return {"item": read_later_to_dict(item, content=content)}


@router.patch("/{item_id}")
async def update_read_later_item(
    item_id: str, request: ReadLaterUpdateRequest, session: AsyncSession = Depends(get_db_session)
):
    item = await session.get(ReadLaterItem, item_id)
    if item is None:
        raise HTTPException(status_code=404, detail="稍后读记录不存在")
    if request.reading_status is not None:
        item.reading_status = request.reading_status
        item.read_at = datetime.now(timezone.utc) if request.reading_status == "read" else None
    if request.title is not None:
        item.title = request.title.strip()
    if request.note is not None:
        item.note = request.note.strip()
    if request.tags is not None:
        item.tags = list(dict.fromkeys(tag.strip() for tag in request.tags if tag.strip()))
    await session.commit()
    await session.refresh(item)
    return {"item": read_later_to_dict(item)}


@router.post("/{item_id}/retry")
async def retry_read_later_capture(item_id: str, session: AsyncSession = Depends(get_db_session)):
    item = await session.get(ReadLaterItem, item_id)
    if item is None:
        raise HTTPException(status_code=404, detail="稍后读记录不存在")
    try:
        job = await retry_read_later_item(session, item=item)
        return {"item": read_later_to_dict(item), "job": job_to_dict(job)}
    except KnowledgeServiceError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/compile")
async def compile_read_later_items(
    request: ReadLaterCompileRequest, session: AsyncSession = Depends(get_db_session)
):
    try:
        job = await promote_read_later_to_wiki(
            session,
            base_dir=BASE_DIR,
            item_ids=request.item_ids,
            import_gbrain=request.import_gbrain,
        )
        return {"job": job_to_dict(job)}
    except KnowledgeServiceError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
