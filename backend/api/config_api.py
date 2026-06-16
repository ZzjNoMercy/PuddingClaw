"""Configuration API — settings management + connection testing."""

import os
import asyncio
from typing import Any, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from config import (
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
    llm: Optional[dict[str, Any]] = None
    embedding: Optional[dict[str, Any]] = None
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
        return {"success": True, "message": "Settings saved"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to save settings: {e}")


# ── Connection testing ─────────────────────────────────────


class TestConnectionRequest(BaseModel):
    type: str  # "llm" or "embedding"
    provider: str
    model: str
    base_url: str
    api_key: str


@router.post("/settings/test-connection")
async def test_connection(request: TestConnectionRequest):
    """Test API key connectivity with a lightweight request."""
    import time

    start = time.time()

    try:
        if request.type == "llm":
            result = await _test_llm_connection(
                request.provider, request.model, request.base_url, request.api_key
            )
        elif request.type == "embedding":
            result = await _test_embedding_connection(
                request.provider, request.model, request.base_url, request.api_key
            )
        else:
            raise HTTPException(status_code=400, detail="type must be 'llm' or 'embedding'")

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
