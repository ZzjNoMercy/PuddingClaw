"""Knowledge base management API."""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from config import get_knowledge_multimodal_index_config, get_knowledge_root_config
from db import get_database_status, get_db_session
from knowledge.indexer import reset_multimodal_collections
from knowledge.paths import get_knowledge_originals_dir, get_knowledge_root
from knowledge.service import DEFAULT_KNOWLEDGE_BASE_ID, KnowledgeService, KnowledgeServiceError, document_to_dict

router = APIRouter(prefix="/knowledge", tags=["knowledge"])
BASE_DIR = Path(__file__).resolve().parent.parent


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
        content = await file.read()
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
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
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
    try:
        content = await file.read()
        document, ingest = await service.ingest_pdf_upload(
            session,
            filename=file.filename or "document.pdf",
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
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"PDF ingestion failed: {exc}") from exc
