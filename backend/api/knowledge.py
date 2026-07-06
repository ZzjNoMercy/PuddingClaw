"""Knowledge base management API."""

from __future__ import annotations

import hashlib
import logging
import mimetypes
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from config import get_knowledge_multimodal_index_config, get_knowledge_root_config
from db import get_database_status, get_db_session
from knowledge.database_sources import (
    KnowledgeDatabaseSourceError,
    delete_database_source,
    get_database_source,
    list_database_sources,
    list_database_tables,
    test_database_source,
    upsert_database_source,
)
from knowledge.import_jobs import (
    create_import_job,
    create_vector_publish_job,
    clear_import_jobs,
    delete_import_job,
    event_to_dict,
    get_import_job,
    job_to_dict,
    list_related_import_events,
    list_import_jobs,
    retry_import_job,
    task_source_path,
)
from knowledge.indexer import reset_multimodal_collections
from knowledge.paths import get_knowledge_originals_dir, get_knowledge_root
from knowledge.models import new_id
from knowledge.models import KnowledgeDocument
from knowledge.service import DEFAULT_KNOWLEDGE_BASE_ID, KnowledgeService, KnowledgeServiceError, _slugify, document_to_dict
from tools.pandas_knowledge_tool import PandasKnowledgeQueryTool
from tools.search_knowledge_tool import LlamaIndexKnowledgeQueryTool

router = APIRouter(prefix="/knowledge", tags=["knowledge"])
BASE_DIR = Path(__file__).resolve().parent.parent
logger = logging.getLogger(__name__)


class ImportLocalMarkdownRequest(BaseModel):
    source_path: str = Field(description="Absolute or home-relative path to a local Markdown file.")
    title: str | None = Field(default=None, description="Optional display title.")
    knowledge_base_id: str = Field(default=DEFAULT_KNOWLEDGE_BASE_ID)


class MarkdownGrepRequest(BaseModel):
    query: str = Field(min_length=1, description="Plain-text keyword to search in Markdown artifacts.")
    pattern: str = Field(default="**/*.md", description="Glob pattern under /knowledge/, e.g. imported/**/*.md.")
    case_sensitive: bool = False
    context_lines: int = Field(default=1, ge=0, le=5)
    max_matches: int = Field(default=50, ge=1, le=500)


class KnowledgeSearchRequest(BaseModel):
    query: str = Field(min_length=1, description="Semantic query for the LlamaIndex knowledge index.")
    top_k: int | None = Field(default=None, ge=1, le=20, description="Final number of hits returned to the Agent/LLM.")


class KnowledgeTableQueryRequest(BaseModel):
    query: str = Field(min_length=1, description="Natural-language table question for imported Excel/CSV/TSV files.")
    file_hint: str | None = Field(default=None, description="Optional file name, title, or /knowledge/... path hint.")
    sheet_name: str | None = Field(default=None, description="Optional Excel sheet name.")
    preview_rows: int = Field(default=5, ge=1, le=20)


class KnowledgeDatabaseSourceRequest(BaseModel):
    id: str | None = None
    type: str = Field(default="postgresql")
    name: str = Field(default="PostgreSQL 数据源")
    description: str = ""
    host: str = Field(default="127.0.0.1")
    port: int = Field(default=5432, ge=1, le=65535)
    database: str = Field(default="puddingclaw")
    username: str = Field(default="puddingclaw")
    password: str = ""
    selected_tables: list[str] = Field(default_factory=list)
    knowledge_base_id: str = Field(default=DEFAULT_KNOWLEDGE_BASE_ID)


async def _save_upload_to_task_source(file: UploadFile, *, job_id: str, filename: str) -> tuple[Path, int, str]:
    target = task_source_path(BASE_DIR, job_id=job_id, filename=filename)
    target.parent.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256()
    size = 0
    with target.open("wb") as handle:
        while True:
            chunk = await file.read(1024 * 1024)
            if not chunk:
                break
            size += len(chunk)
            digest.update(chunk)
            handle.write(chunk)
    if size <= 0:
        raise KnowledgeServiceError("Uploaded file is empty.")
    return target, size, digest.hexdigest()


@router.get("/status")
async def knowledge_status():
    knowledge_dir = get_knowledge_root(BASE_DIR)
    multimodal_index = get_knowledge_multimodal_index_config()
    root_config = get_knowledge_root_config()
    return {
        "enabled": True,
        "database": get_database_status(),
        "local_markdown": {
            "enabled": True,
            "physical_path": str(knowledge_dir),
            "originals_path": str(get_knowledge_originals_dir(BASE_DIR, knowledge_dir)),
            "configured_by": root_config["configured_by"],
            "environment_override": root_config["environment_override"],
            "deepagents_virtual_path": "/knowledge/",
        },
        "vector": {
            "enabled": True,
            "provider": "llamaindex",
            "note": "PDF/MinerU and local Markdown artifacts are published through LlamaIndex; vector publishing defaults to unified multimodal Milvus indexing via config.json knowledge.multimodal_index.",
            "multimodal": {
                "enabled": multimodal_index["enabled"],
                "vector_store": multimodal_index["vector_store"],
                "milvus_uri": multimodal_index["milvus_uri"],
                "text_collection": multimodal_index["text_collection"],
                "image_collection": multimodal_index["image_collection"],
            },
        },
        "parser": {
            "mineru_optional": True,
            "note": "PDF upload uses MinerU first, then stores Markdown artifacts under /knowledge/imported/ for glob/grep and vector indexing.",
        },
        "markdown_search": {
            "enabled": True,
            "glob_endpoint": "/api/knowledge/markdown/glob",
            "grep_endpoint": "/api/knowledge/markdown/grep",
            "deepagents_virtual_path": "/knowledge/",
        },
    }


@router.get("/database-sources")
async def list_knowledge_database_sources(
    knowledge_base_id: str = DEFAULT_KNOWLEDGE_BASE_ID,
    session: AsyncSession = Depends(get_db_session),
):
    try:
        sources = await list_database_sources(session, knowledge_base_id=knowledge_base_id)
        return {"sources": sources, "count": len(sources)}
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"Database sources unavailable: {exc}") from exc


@router.post("/database-sources")
async def save_knowledge_database_source(
    request: KnowledgeDatabaseSourceRequest,
    session: AsyncSession = Depends(get_db_session),
):
    try:
        source = await upsert_database_source(
            session,
            request.model_dump(exclude={"knowledge_base_id"}),
            knowledge_base_id=request.knowledge_base_id,
        )
        return {"source": source}
    except KnowledgeDatabaseSourceError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"Failed to save database source: {exc}") from exc


@router.post("/database-sources/test")
async def test_knowledge_database_source(
    request: KnowledgeDatabaseSourceRequest,
    session: AsyncSession = Depends(get_db_session),
):
    try:
        payload = request.model_dump(exclude={"knowledge_base_id"})
        if payload.get("id") and not payload.get("password"):
            source = await get_database_source(session, payload["id"], knowledge_base_id=request.knowledge_base_id)
            return await test_database_source(source)
        return await test_database_source(payload)
    except KnowledgeDatabaseSourceError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"Failed to test database source: {exc}") from exc


@router.get("/database-sources/{source_id}/tables")
async def list_knowledge_database_source_tables(
    source_id: str,
    knowledge_base_id: str = DEFAULT_KNOWLEDGE_BASE_ID,
    session: AsyncSession = Depends(get_db_session),
):
    try:
        source = await get_database_source(session, source_id, knowledge_base_id=knowledge_base_id)
        tables = await list_database_tables(source)
        return {"tables": tables, "count": len(tables)}
    except KnowledgeDatabaseSourceError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"Failed to list database tables: {exc}") from exc


@router.delete("/database-sources/{source_id}")
async def delete_knowledge_database_source(
    source_id: str,
    knowledge_base_id: str = DEFAULT_KNOWLEDGE_BASE_ID,
    session: AsyncSession = Depends(get_db_session),
):
    try:
        await delete_database_source(session, source_id, knowledge_base_id=knowledge_base_id)
        return {"ok": True}
    except KnowledgeDatabaseSourceError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"Failed to delete database source: {exc}") from exc


@router.get("/documents")
async def list_knowledge_documents(
    knowledge_base_id: str = DEFAULT_KNOWLEDGE_BASE_ID,
    session: AsyncSession = Depends(get_db_session),
):
    service = KnowledgeService(BASE_DIR)
    try:
        documents = await service.list_documents(session, knowledge_base_id=knowledge_base_id)
        return {"documents": [document_to_dict(document) for document in documents]}
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"Knowledge database unavailable: {exc}") from exc


@router.get("/import-jobs")
async def list_knowledge_import_jobs(
    knowledge_base_id: str = DEFAULT_KNOWLEDGE_BASE_ID,
    limit: int = 50,
    session: AsyncSession = Depends(get_db_session),
):
    try:
        jobs = await list_import_jobs(session, knowledge_base_id=knowledge_base_id, limit=limit)
        return {"jobs": [job_to_dict(job) for job in jobs]}
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"Knowledge import jobs unavailable: {exc}") from exc


@router.delete("/import-jobs")
async def clear_knowledge_import_jobs(
    knowledge_base_id: str = DEFAULT_KNOWLEDGE_BASE_ID,
    session: AsyncSession = Depends(get_db_session),
):
    try:
        deleted_count = await clear_import_jobs(session, knowledge_base_id=knowledge_base_id)
        return {"deleted_count": deleted_count}
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"Failed to clear import jobs: {exc}") from exc


@router.get("/import-jobs/{job_id}")
async def get_knowledge_import_job(
    job_id: str,
    include_events: bool = True,
    session: AsyncSession = Depends(get_db_session),
):
    job = await get_import_job(session, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"Import job not found: {job_id}")
    payload = {"job": job_to_dict(job), "document": None}
    if job.document_id:
        document = await session.get(KnowledgeDocument, job.document_id)
        if document is not None:
            if job.status not in {"queued", "running"}:
                service = KnowledgeService(BASE_DIR)
                if service.repair_document_metadata(document):
                    await session.commit()
                    await session.refresh(document)
            payload["document"] = document_to_dict(document)
    if include_events:
        payload["events"] = [event_to_dict(event) for event in await list_related_import_events(session, job)]
    return payload


@router.delete("/import-jobs/{job_id}")
async def delete_knowledge_import_job(
    job_id: str,
    session: AsyncSession = Depends(get_db_session),
):
    try:
        await delete_import_job(session, job_id)
        return {"ok": True}
    except KnowledgeServiceError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"Failed to delete import job: {exc}") from exc


@router.post("/import-jobs/{job_id}/retry")
async def retry_knowledge_import_job(
    job_id: str,
    session: AsyncSession = Depends(get_db_session),
):
    try:
        job = await retry_import_job(session, job_id)
        return {"job": job_to_dict(job)}
    except KnowledgeServiceError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"Failed to retry import job: {exc}") from exc


@router.post("/import-jobs/{job_id}/publish-vector")
async def publish_import_job_vector(
    job_id: str,
    session: AsyncSession = Depends(get_db_session),
):
    job = await get_import_job(session, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"Import job not found: {job_id}")
    if not job.document_id:
        raise HTTPException(status_code=400, detail="任务还没有生成知识库文档，暂时不能导入向量。")

    try:
        vector_job = await create_vector_publish_job(session, base_dir=BASE_DIR, source_job=job)
        return {"job": job_to_dict(vector_job), "queued": True, "source_job_id": job.id}
    except KnowledgeServiceError as exc:
        await session.rollback()
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        await session.rollback()
        raise HTTPException(status_code=503, detail=f"Failed to create vector import job: {exc}") from exc


@router.post("/import-jobs")
async def create_knowledge_import_job(
    file: UploadFile = File(...),
    title: str | None = Form(default=None),
    knowledge_base_id: str = Form(default=DEFAULT_KNOWLEDGE_BASE_ID),
    publish_targets: str = Form(default="local_markdown"),
    session: AsyncSession = Depends(get_db_session),
):
    filename = _slugify(file.filename or "document")
    targets = [item.strip() for item in publish_targets.split(",") if item.strip()]
    job_id = new_id("job")
    try:
        logger.info("[knowledge-import-job] receiving filename=%s job_id=%s", filename, job_id)
        source_path, file_size, source_sha256 = await _save_upload_to_task_source(file, job_id=job_id, filename=filename)
        job = await create_import_job(
            session,
            base_dir=BASE_DIR,
            filename=filename,
            source_path=source_path,
            file_size=file_size,
            source_sha256=source_sha256,
            title=title,
            knowledge_base_id=knowledge_base_id,
            publish_targets=targets,
        )
        return {"job": job_to_dict(job)}
    except KnowledgeServiceError as exc:
        logger.warning("[knowledge-import-job] rejected filename=%s: %s", filename, exc)
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        logger.exception("[knowledge-import-job] failed filename=%s", filename)
        raise HTTPException(status_code=503, detail=f"Failed to create import job: {exc}") from exc


@router.get("/files")
async def list_knowledge_directory_files(limit: int = 200):
    service = KnowledgeService(BASE_DIR)
    try:
        return {"files": service.list_directory_files(limit=limit)}
    except KnowledgeServiceError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/tree")
async def get_knowledge_directory_tree(max_depth: int = 5, limit: int = 500):
    service = KnowledgeService(BASE_DIR)
    try:
        return {"tree": service.list_directory_tree(max_depth=max_depth, max_entries=limit)}
    except KnowledgeServiceError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/file/preview")
async def preview_knowledge_file(virtual_path: str):
    service = KnowledgeService(BASE_DIR)
    try:
        return {"file": service.preview_file(virtual_path=virtual_path)}
    except KnowledgeServiceError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/file/raw")
async def raw_knowledge_file(virtual_path: str):
    service = KnowledgeService(BASE_DIR)
    try:
        path = service.resolve_raw_file(virtual_path=virtual_path)
        media_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        return FileResponse(path, media_type=media_type, filename=path.name)
    except KnowledgeServiceError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/vector/reset")
async def reset_knowledge_vector_collections():
    """Immediately drop configured vector collections.

    This is a user-triggered maintenance operation. It is intentionally not tied
    to document import, so normal ingestion never rebuilds collections.
    """

    try:
        return reset_multimodal_collections(get_knowledge_multimodal_index_config())
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"Failed to reset vector collections: {exc}") from exc


@router.get("/markdown/glob")
async def glob_markdown_files(
    pattern: str = "**/*.md",
    limit: int = 200,
):
    service = KnowledgeService(BASE_DIR)
    try:
        return {"files": service.glob_markdown_files(pattern=pattern, limit=limit)}
    except KnowledgeServiceError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/markdown/grep")
async def grep_markdown_files(request: MarkdownGrepRequest):
    service = KnowledgeService(BASE_DIR)
    try:
        matches = service.grep_markdown_files(
            query=request.query,
            pattern=request.pattern,
            case_sensitive=request.case_sensitive,
            context_lines=request.context_lines,
            max_matches=request.max_matches,
        )
        return {"matches": matches}
    except KnowledgeServiceError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/search")
async def search_knowledge(request: KnowledgeSearchRequest):
    try:
        tool = LlamaIndexKnowledgeQueryTool(base_dir=str(BASE_DIR))
        return tool.query_structured(request.query, top_k=request.top_k)
    except Exception as exc:
        logger.exception("[knowledge-search] failed query=%s", request.query)
        raise HTTPException(status_code=503, detail=f"Knowledge search failed: {exc}") from exc


@router.post("/tables/query")
async def query_knowledge_table(request: KnowledgeTableQueryRequest):
    try:
        tool = PandasKnowledgeQueryTool(base_dir=str(BASE_DIR))
        return tool.query_structured(
            request.query,
            file_hint=request.file_hint,
            sheet_name=request.sheet_name,
            preview_rows=request.preview_rows,
        )
    except Exception as exc:
        logger.exception("[knowledge-table-query] failed query=%s", request.query)
        raise HTTPException(status_code=503, detail=f"Knowledge table query failed: {exc}") from exc


@router.post("/documents/import-local-md")
async def import_local_markdown(
    request: ImportLocalMarkdownRequest,
    session: AsyncSession = Depends(get_db_session),
):
    service = KnowledgeService(BASE_DIR)
    try:
        document = await service.import_local_markdown(
            session,
            source_path=request.source_path,
            title=request.title,
            knowledge_base_id=request.knowledge_base_id,
        )
        return {"document": document_to_dict(document)}
    except KnowledgeServiceError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"Knowledge database unavailable: {exc}") from exc


@router.post("/documents/import")
async def import_knowledge_document(
    file: UploadFile = File(...),
    title: str | None = Form(default=None),
    knowledge_base_id: str = Form(default=DEFAULT_KNOWLEDGE_BASE_ID),
    publish_targets: str = Form(default="local_markdown,vector"),
    session: AsyncSession = Depends(get_db_session),
):
    service = KnowledgeService(BASE_DIR)
    targets = [item.strip() for item in publish_targets.split(",") if item.strip()]
    filename = file.filename or "document"
    suffix = Path(filename).suffix.lower()
    try:
        logger.info("[knowledge-import] receiving file filename=%s suffix=%s", filename, suffix)
        content = await file.read()
        logger.info("[knowledge-import] received file filename=%s size_bytes=%s", filename, len(content))
        if suffix == ".pdf":
            document, ingest = await service.ingest_pdf_upload(
                session,
                filename=filename,
                content=content,
                title=title,
                knowledge_base_id=knowledge_base_id,
                publish_targets=targets,
            )
        elif suffix in {".md", ".markdown"}:
            document, ingest = await service.ingest_markdown_upload(
                session,
                filename=filename,
                content=content,
                title=title,
                knowledge_base_id=knowledge_base_id,
                publish_targets=targets,
            )
        elif suffix in {".xlsx", ".xls", ".csv", ".tsv", ".txt", ".docx"}:
            document, ingest = await service.ingest_generic_upload(
                session,
                filename=filename,
                content=content,
                title=title,
                knowledge_base_id=knowledge_base_id,
                publish_targets=targets,
            )
        else:
            raise KnowledgeServiceError(
                f"Unsupported knowledge file type: {suffix or 'unknown'}. Supported: .pdf, .md, .markdown, .xlsx, .xls, .csv, .tsv, .txt, .docx"
            )
        return {
            "document": document_to_dict(document),
            "ingestion": ingest,
            "detected_type": suffix.removeprefix(".") or "unknown",
        }
    except KnowledgeServiceError as exc:
        logger.warning("[knowledge-import] rejected filename=%s: %s", filename, exc)
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        logger.exception("[knowledge-import] failed filename=%s", filename)
        raise HTTPException(status_code=503, detail=f"Knowledge import failed: {exc}") from exc


@router.post("/documents/upload-pdf")
async def upload_pdf_document(
    file: UploadFile = File(...),
    title: str | None = Form(default=None),
    knowledge_base_id: str = Form(default=DEFAULT_KNOWLEDGE_BASE_ID),
    publish_targets: str = Form(default="local_markdown,vector"),
    session: AsyncSession = Depends(get_db_session),
):
    service = KnowledgeService(BASE_DIR)
    targets = [item.strip() for item in publish_targets.split(",") if item.strip()]
    filename = file.filename or "document.pdf"
    try:
        logger.info("[knowledge-upload-pdf] receiving file filename=%s", filename)
        content = await file.read()
        logger.info("[knowledge-upload-pdf] received file filename=%s size_bytes=%s", filename, len(content))
        document, ingest = await service.ingest_pdf_upload(
            session,
            filename=filename,
            content=content,
            title=title,
            knowledge_base_id=knowledge_base_id,
            publish_targets=targets,
        )
        return {
            "document": document_to_dict(document),
            "ingestion": ingest,
        }
    except KnowledgeServiceError as exc:
        logger.warning("[knowledge-upload-pdf] rejected filename=%s: %s", filename, exc)
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        logger.exception("[knowledge-upload-pdf] failed filename=%s", filename)
        raise HTTPException(status_code=503, detail=f"PDF ingestion failed: {exc}") from exc
