"""Configuration API — settings management + connection testing."""

import os
import asyncio
from typing import Any, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from config import (
    get_fallback_embedding_config,
    get_fallback_llm_config,
    get_rag_mode,
    set_rag_mode,
    get_settings_for_display,
    update_settings,
)

router = APIRouter()


# ── RAG mode (existing, unchanged) ────────────────────────


class RagModeRequest(BaseModel):
    enabled: bool


@router.get("/config/rag-mode")
async def get_rag_mode_endpoint():
    return {"rag_mode": get_rag_mode()}


@router.put("/config/rag-mode")
async def set_rag_mode_endpoint(request: RagModeRequest):
    set_rag_mode(request.enabled)
    return {"rag_mode": request.enabled}


# ── Settings CRUD ──────────────────────────────────────────


class SettingsUpdateRequest(BaseModel):
    thinking_mode: Optional[bool] = None
    ai_gateway: Optional[dict[str, Any]] = None
    gateway_llm: Optional[dict[str, Any]] = None
    fallback_llm: Optional[dict[str, Any]] = None
    fallback_embedding: Optional[dict[str, Any]] = None
    rag: Optional[dict[str, Any]] = None
    compression: Optional[dict[str, Any]] = None


@router.get("/settings")
async def get_settings():
    """Get current settings with masked API keys."""
    return get_settings_for_display()


@router.put("/settings")
async def put_settings(request: SettingsUpdateRequest):
    """Update settings (partial update supported)."""
    try:
        updates = request.model_dump(exclude_none=True)
        update_settings(updates)
        import capabilities
        capabilities.invalidate_capabilities()
        return {"success": True, "message": "Settings saved"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to save settings: {e}")


# ── Connection testing ─────────────────────────────────────


class TestConnectionRequest(BaseModel):
    type: str  # "gateway", "llm" or "embedding"
    provider: str = ""
    model: str = ""
    base_url: str
    api_key: str = ""
    health_path: str = "/health"


@router.post("/settings/test-connection")
async def test_connection(request: TestConnectionRequest):
    """Test API key connectivity with a lightweight request."""
    import time

    start = time.time()

    try:
        if request.type == "gateway":
            result = await _test_gateway_connection(
                request.base_url,
                request.health_path,
            )
        elif request.type == "llm":
            llm = get_fallback_llm_config()
            result = await _test_llm_connection(
                request.provider,
                request.model,
                request.base_url,
                request.api_key or llm.get("api_key", ""),
            )
        elif request.type == "embedding":
            embedding = get_fallback_embedding_config()
            result = await _test_embedding_connection(
                request.provider,
                request.model,
                request.base_url,
                request.api_key or embedding.get("api_key", ""),
            )
        else:
            raise HTTPException(status_code=400, detail="type must be 'gateway', 'llm' or 'embedding'")

        latency_ms = int((time.time() - start) * 1000)
        return {"success": True, "model": request.model, "latency_ms": latency_ms, **result}

    except HTTPException:
        raise
    except asyncio.TimeoutError:
        raise HTTPException(status_code=408, detail="Connection timeout (10s)")
    except Exception as e:
        error_msg = str(e)
        if "401" in error_msg or "Unauthorized" in error_msg:
            raise HTTPException(status_code=401, detail="Invalid API key")
        if "403" in error_msg or "Forbidden" in error_msg:
            raise HTTPException(status_code=403, detail="Access forbidden — check API key permissions")
        raise HTTPException(status_code=502, detail=f"Connection failed: {error_msg}")


async def _test_llm_connection(provider: str, model: str, base_url: str, api_key: str) -> dict:
    """Test LLM connection with a minimal chat completion request."""
    from openai import AsyncOpenAI

    client = AsyncOpenAI(base_url=base_url, api_key=api_key, timeout=10.0)
    response = await client.chat.completions.create(
        model=model,
        messages=[{"role": "user", "content": "Hi"}],
        max_tokens=5,
    )
    return {"response_model": response.model or model}


def _gateway_health_url(base_url: str, health_path: str) -> str:
    """将 OpenAI `/v1` 入口转换为网关进程的健康检查地址。"""
    normalized = base_url.rstrip("/")
    if normalized.endswith("/v1"):
        normalized = normalized[:-3]
    return normalized + "/" + health_path.lstrip("/")


async def _test_gateway_connection(base_url: str, health_path: str) -> dict:
    import httpx

    health_url = _gateway_health_url(base_url, health_path)
    async with httpx.AsyncClient(timeout=10.0) as client:
        response = await client.get(health_url)
    if not 200 <= response.status_code < 400:
        raise HTTPException(status_code=502, detail=f"Gateway health check returned HTTP {response.status_code}")
    return {"health_url": health_url, "status_code": response.status_code}


async def _test_embedding_connection(provider: str, model: str, base_url: str, api_key: str) -> dict:
    """Test embedding connection with a minimal embedding request."""
    from openai import AsyncOpenAI

    client = AsyncOpenAI(base_url=base_url, api_key=api_key, timeout=10.0)
    response = await client.embeddings.create(
        model=model,
        input="test",
    )
    dim = len(response.data[0].embedding) if response.data else 0
    return {"dimensions": dim}
