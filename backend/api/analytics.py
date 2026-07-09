"""Analytics workbench API."""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile
from fastapi.responses import Response
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from analytics.semantic_assets import SemanticAssetError, get_semantic_asset_registry
from analytics.nl2sql.entity_candidates import recommend_entity_candidates
from analytics.nl2sql.result_store import (
    QueryResultStoreError,
    export_query_result_csv,
    get_query_result_page,
    get_query_result_summary,
    list_query_results,
)
from analytics.table_catalog import TableAssetCatalog, TableCatalogError
from config import get_database_qa_config
from db import get_db_session

router = APIRouter(prefix="/analytics", tags=["analytics"])
BASE_DIR = Path(__file__).resolve().parent.parent


class SemanticAssetCreateRequest(BaseModel):
    name: str
    type: str = Field(pattern="^(measure|dimension|grain)$")
    description: str = ""
    aliases: list[str] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)
    version: str = "0.1.0"
    slug: str | None = None


@router.get("/semantic-assets")
async def list_semantic_assets():
    try:
        return get_semantic_asset_registry(BASE_DIR).list_assets()
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"Failed to list semantic assets: {exc}") from exc


@router.post("/semantic-assets/refresh")
async def refresh_semantic_assets():
    try:
        return get_semantic_asset_registry(BASE_DIR).refresh()
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"Failed to refresh semantic assets: {exc}") from exc


@router.post("/semantic-assets")
async def create_semantic_asset(request: SemanticAssetCreateRequest):
    try:
        asset = get_semantic_asset_registry(BASE_DIR).create_asset(
            name=request.name,
            asset_type=request.type,
            description=request.description,
            aliases=request.aliases,
            tags=request.tags,
            version=request.version,
            slug=request.slug,
        )
        return {"asset": asset, "status": "created"}
    except SemanticAssetError as exc:
        message = str(exc)
        status_code = 409 if "already exists" in message else 400
        raise HTTPException(status_code=status_code, detail=message) from exc
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"Failed to create semantic asset: {exc}") from exc


@router.post("/semantic-assets/import")
async def import_semantic_assets(files: list[UploadFile] = File(...)):
    if not files:
        raise HTTPException(status_code=400, detail="No files uploaded")
    registry = get_semantic_asset_registry(BASE_DIR)
    try:
        first_filename = files[0].filename or ""
        if len(files) == 1 and first_filename.lower().endswith(".zip"):
            return registry.import_zip(files[0].file)
        payload = []
        for uploaded in files:
            payload.append((uploaded.filename or "uploaded", await uploaded.read()))
        return registry.import_files(payload)
    except SemanticAssetError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"Failed to import semantic assets: {exc}") from exc


@router.get("/semantic-assets/{asset_id:path}")
async def get_semantic_asset(asset_id: str):
    try:
        return {"asset": get_semantic_asset_registry(BASE_DIR).get_asset(asset_id)}
    except SemanticAssetError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"Failed to load semantic asset: {exc}") from exc


@router.get("/query-results")
async def list_database_query_results(
    limit: int = Query(default=50, ge=1, le=200),
    include_expired: bool = Query(default=True),
    include_profile: bool = Query(default=False),
    session: AsyncSession = Depends(get_db_session),
):
    try:
        return await list_query_results(
            session,
            limit=limit,
            include_expired=include_expired,
            include_profile=include_profile,
        )
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"Failed to list query results: {exc}") from exc


@router.get("/query-results/{result_id}/summary")
async def get_database_query_result_summary(
    result_id: str,
    session: AsyncSession = Depends(get_db_session),
):
    try:
        return await get_query_result_summary(session, result_id)
    except QueryResultStoreError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"Failed to load query result: {exc}") from exc


@router.get("/query-results/{result_id}")
async def get_database_query_result_page(
    result_id: str,
    page: int = Query(default=1, ge=1),
    page_size: int | None = Query(default=None, ge=1),
    session: AsyncSession = Depends(get_db_session),
):
    try:
        return await get_query_result_page(session, result_id, page=page, page_size=page_size)
    except QueryResultStoreError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"Failed to load query result page: {exc}") from exc


@router.get("/query-results/{result_id}/export.csv")
async def export_database_query_result_csv(
    result_id: str,
    session: AsyncSession = Depends(get_db_session),
):
    if not get_database_qa_config().get("export_enabled", False):
        raise HTTPException(status_code=403, detail="CSV 导出已在智能问数设置中关闭。")
    try:
        filename, content = await export_query_result_csv(session, result_id)
        return Response(
            content=content,
            media_type="text/csv; charset=utf-8",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )
    except QueryResultStoreError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"Failed to export query result: {exc}") from exc


@router.get("/table-assets")
async def list_table_assets(
    include_profile: bool = Query(default=False),
    limit: int = Query(default=500, ge=1, le=2000),
    session: AsyncSession = Depends(get_db_session),
):
    catalog = TableAssetCatalog(BASE_DIR)
    try:
        assets = await catalog.list_assets(session, include_profile=include_profile, limit=limit)
        return {"assets": assets, "count": len(assets)}
    except TableCatalogError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"Failed to list table assets: {exc}") from exc


@router.post("/table-assets/refresh-profiles")
async def refresh_table_asset_profiles(
    limit: int = Query(default=200, ge=1, le=1000),
    session: AsyncSession = Depends(get_db_session),
):
    catalog = TableAssetCatalog(BASE_DIR)
    try:
        return await catalog.refresh_profiles(session, limit=limit)
    except TableCatalogError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"Failed to refresh table profiles: {exc}") from exc


@router.get("/table-assets/{asset_id}")
async def get_table_asset(
    asset_id: str,
    session: AsyncSession = Depends(get_db_session),
):
    catalog = TableAssetCatalog(BASE_DIR)
    try:
        return {"asset": await catalog.get_asset(session, asset_id, include_profile=True)}
    except TableCatalogError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"Failed to load table asset: {exc}") from exc


@router.post("/table-assets/{asset_id}/profile")
async def generate_table_asset_profile(
    asset_id: str,
    session: AsyncSession = Depends(get_db_session),
):
    catalog = TableAssetCatalog(BASE_DIR)
    try:
        return {"asset": await catalog.generate_profile(session, asset_id)}
    except TableCatalogError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"Failed to generate table profile: {exc}") from exc


@router.get("/table-assets/{asset_id}/entity-candidates")
async def get_table_asset_entity_candidates(
    asset_id: str,
    limit: int = Query(default=12, ge=1, le=50),
    session: AsyncSession = Depends(get_db_session),
):
    catalog = TableAssetCatalog(BASE_DIR)
    try:
        asset = await catalog.get_asset(session, asset_id, include_profile=True)
        profile = asset.get("profile") or {}
        candidates = recommend_entity_candidates(
            profile,
            table_name=asset.get("sheet_name") or asset.get("file_name"),
            max_candidates=limit,
        )
        return {"asset_id": asset_id, "candidates": candidates, "count": len(candidates)}
    except TableCatalogError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"Failed to recommend entity candidates: {exc}") from exc
