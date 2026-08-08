"""Control-plane API for managed web-search providers."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

from web_search.models import WebSearchError
from web_search.registry import get_web_search_registry
from web_search.service import get_web_search_service

router = APIRouter(prefix="/config/web-search", tags=["web-search-config"])


class CredentialRequest(BaseModel):
    api_key: str = Field(min_length=1, max_length=4096)


class RoutingRequest(BaseModel):
    default_scope: str | None = None
    domestic: list[str] | None = None
    global_: list[str] | None = Field(default=None, alias="global")
    fallback_enabled: bool | None = None
    max_provider_attempts: int | None = Field(default=None, ge=1, le=3)
    cross_check_enabled: bool | None = None

    model_config = {"populate_by_name": True}


class ProviderOptionsRequest(BaseModel):
    options: dict[str, Any]


@router.get("")
async def get_web_search_config():
    return get_web_search_registry().display()


@router.put("/routing")
async def update_web_search_routing(request: RoutingRequest):
    try:
        update = request.model_dump(exclude_none=True, by_alias=True)
        return get_web_search_registry().update_routing(update)
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.patch("/providers/{provider_id}/options")
async def update_web_search_provider_options(provider_id: str, request: ProviderOptionsRequest):
    try:
        return get_web_search_registry().update_provider_options(provider_id, request.options)
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.put("/providers/{provider_id}/credential")
async def put_web_search_credential(provider_id: str, request: CredentialRequest):
    try:
        return get_web_search_registry().save_credential(provider_id, request.api_key)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.delete("/providers/{provider_id}/credential")
async def delete_web_search_credential(provider_id: str):
    try:
        return get_web_search_registry().delete_credential(provider_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/providers/{provider_id}/prepare")
async def prepare_web_search_provider(provider_id: str):
    try:
        return get_web_search_registry().prepare(provider_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/providers/{provider_id}/test")
async def test_web_search_provider(provider_id: str):
    try:
        return await run_in_threadpool(get_web_search_service().test_provider, provider_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except WebSearchError as exc:
        status = 401 if exc.category == "authentication" else 502
        raise HTTPException(status_code=status, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"连接测试失败：{exc}") from exc


@router.post("/providers/{provider_id}/enable")
async def enable_web_search_provider(provider_id: str):
    try:
        return await run_in_threadpool(get_web_search_service().enable_provider, provider_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except WebSearchError as exc:
        status = 401 if exc.category == "authentication" else 502
        raise HTTPException(status_code=status, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"启用失败：{exc}") from exc


@router.post("/providers/{provider_id}/disable")
async def disable_web_search_provider(provider_id: str):
    try:
        return get_web_search_registry().set_enabled(provider_id, False)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
