"""Analytics workbench API."""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, HTTPException, Query

from analytics.table_catalog import TableAssetCatalog, TableCatalogError

router = APIRouter(prefix="/analytics", tags=["analytics"])
BASE_DIR = Path(__file__).resolve().parent.parent


@router.get("/table-assets")
async def list_table_assets(
    include_profile: bool = Query(default=False),
    limit: int = Query(default=500, ge=1, le=2000),
):
    catalog = TableAssetCatalog(BASE_DIR)
    try:
        assets = catalog.list_assets(include_profile=include_profile, limit=limit)
        return {"assets": assets, "count": len(assets)}
    except TableCatalogError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"Failed to list table assets: {exc}") from exc


@router.post("/table-assets/refresh-profiles")
async def refresh_table_asset_profiles(limit: int = Query(default=200, ge=1, le=1000)):
    catalog = TableAssetCatalog(BASE_DIR)
    try:
        return catalog.refresh_profiles(limit=limit)
    except TableCatalogError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"Failed to refresh table profiles: {exc}") from exc


@router.get("/table-assets/{asset_id}")
async def get_table_asset(asset_id: str):
    catalog = TableAssetCatalog(BASE_DIR)
    try:
        return {"asset": catalog.get_asset(asset_id, include_profile=True)}
    except TableCatalogError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"Failed to load table asset: {exc}") from exc


@router.post("/table-assets/{asset_id}/profile")
async def generate_table_asset_profile(asset_id: str):
    catalog = TableAssetCatalog(BASE_DIR)
    try:
        return {"asset": catalog.generate_profile(asset_id)}
    except TableCatalogError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"Failed to generate table profile: {exc}") from exc


