"""Attachment upload API for Agent mode."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, File, Form, UploadFile

from graph.attachment_store import attachment_store

router = APIRouter()


@router.post("/attachments")
async def upload_attachments(
    session_id: Annotated[str, Form()],
    files: Annotated[list[UploadFile], File()],
    source: Annotated[str, Form()] = "upload",
):
    items = []
    for file in files[:8]:
        items.append(
            attachment_store.save(
                session_id=session_id,
                filename=file.filename or "attachment",
                mime_type=file.content_type or "application/octet-stream",
                source=source,
                stream=file.file,
            )
        )
    return {"attachments": items}
