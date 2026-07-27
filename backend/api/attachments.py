"""Attachment upload API for Agent mode."""

from __future__ import annotations

from pathlib import Path
from urllib.parse import quote

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse
from starlette.datastructures import UploadFile
from starlette.formparsers import MultiPartException, MultiPartParser

from graph.attachment_store import attachment_store
from graph.session_manager import session_manager

router = APIRouter()
MAX_ATTACHMENT_REQUEST_BYTES = 100 * 1024 * 1024


@router.post("/attachments")
async def upload_attachments(
    request: Request,
):
    content_length = request.headers.get("content-length")
    if content_length and content_length.isdigit() and int(content_length) > MAX_ATTACHMENT_REQUEST_BYTES:
        raise HTTPException(status_code=413, detail="Attachment request exceeds the 100MB limit")

    async def limited_stream():
        consumed = 0
        async for chunk in request.stream():
            consumed += len(chunk)
            if consumed > MAX_ATTACHMENT_REQUEST_BYTES:
                raise MultiPartException("Attachment request exceeded the 100MB limit")
            yield chunk

    try:
        form = await MultiPartParser(
            request.headers,
            limited_stream(),
            max_files=8,
            max_fields=4,
            max_part_size=64 * 1024,
        ).parse()
    except MultiPartException as exc:
        raise HTTPException(status_code=413, detail=exc.message) from exc

    session_id = str(form.get("session_id") or "")
    source = str(form.get("source") or "upload")
    if source not in {"upload", "paste"}:
        raise HTTPException(status_code=422, detail="Attachment source must be upload or paste")
    if not session_manager.session_exists(session_id):
        raise HTTPException(status_code=404, detail="Session not found")
    files = [item for item in form.getlist("files") if isinstance(item, UploadFile)]
    if not files:
        raise HTTPException(status_code=422, detail="At least one attachment file is required")
    items = []
    for file in files:
        try:
            items.append(
                attachment_store.save(
                    session_id=session_id,
                    filename=file.filename or "attachment",
                    mime_type=file.content_type or "application/octet-stream",
                    source=source,
                    stream=file.file,
                )
            )
        except ValueError as exc:
            raise HTTPException(status_code=413, detail=str(exc)) from exc
    return {"attachments": items}


@router.get("/attachments/{attachment_id}/download")
async def download_attachment(attachment_id: str, session_id: str):
    """Download one attachment only through its owning Session boundary."""

    if not session_manager.session_exists(session_id):
        raise HTTPException(status_code=404, detail="Session not found")
    item = attachment_store.get(session_id, attachment_id)
    if item is None:
        raise HTTPException(status_code=404, detail="Attachment not found")
    return FileResponse(
        path=str(item["path"]),
        media_type=str(item.get("mime_type") or "application/octet-stream"),
        filename=str(item.get("name") or "attachment"),
    )


@router.get("/attachments/{attachment_id}/preview")
async def preview_attachment(attachment_id: str, session_id: str):
    """Render one immutable raster image inside its owning Session boundary."""

    if not session_manager.session_exists(session_id):
        raise HTTPException(status_code=404, detail="Session not found")
    item = attachment_store.get(session_id, attachment_id)
    if item is None:
        raise HTTPException(status_code=404, detail="Attachment not found")

    preview = attachment_store.preview_info(item)
    if preview is None:
        # SVG is deliberately excluded: it is active document content rather
        # than an inert screenshot/QR bitmap and should remain downloadable.
        raise HTTPException(status_code=415, detail="Attachment is not a previewable image")

    filename = Path(str(item.get("name") or "image")).name
    headers = {
        "Cache-Control": "private, no-store",
        "Content-Disposition": f"inline; filename*=UTF-8''{quote(filename, safe='')}",
        "Content-Security-Policy": "default-src 'none'; sandbox",
        "Cross-Origin-Resource-Policy": "same-origin",
        "X-Content-Type-Options": "nosniff",
    }
    content_sha256 = str(item.get("sha256") or "")
    if content_sha256:
        headers["ETag"] = f'"{content_sha256}"'
    return FileResponse(
        path=str(item["path"]),
        media_type=str(preview["mime_type"]),
        headers=headers,
    )
