"""Analytics workbench API."""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession

from analytics.nl2sql.entity_candidates import recommend_entity_candidates
from analytics.table_catalog import TableAssetCatalog, TableCatalogError
from db import get_db_session

router = APIRouter(prefix="/analytics", tags=["analytics"])
BASE_DIR = Path(__file__).resolve().parent.parent


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
