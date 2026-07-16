"""Configuration API — settings management + connection testing."""

import asyncio
import re
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
    multimodal_embedding: Optional[dict[str, Any]] = None
    rag: Optional[dict[str, Any]] = None
    vanna: Optional[dict[str, Any]] = None
    analytics: Optional[dict[str, Any]] = None
    database: Optional[dict[str, Any]] = None
    knowledge: Optional[dict[str, Any]] = None
    compression: Optional[dict[str, Any]] = None
    harness: Optional[dict[str, Any]] = None
    subagents: Optional[dict[str, Any]] = None
    subagent: Optional[dict[str, Any]] = None


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
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
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


class DatabaseConnectionRequest(BaseModel):
    mode: str = "external"
    host: str = "127.0.0.1"
    port: int = 5432
    database: str = "puddingclaw"
    username: str = "puddingclaw"
    password: str = ""
    create_if_missing: bool = False


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


@router.post("/settings/database/test")
async def test_database_connection(request: DatabaseConnectionRequest):
    """Test PostgreSQL connectivity; optionally create the database if missing."""

    result = await _test_database_connection(request)
    return result


def _validate_pg_identifier(value: str, label: str) -> str:
    cleaned = (value or "").strip()
    if not cleaned:
        raise HTTPException(status_code=400, detail=f"{label} cannot be empty")
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,62}", cleaned):
        raise HTTPException(
            status_code=400,
            detail=f"{label} must start with a letter/underscore and contain only letters, numbers or underscores",
        )
    return cleaned


async def _test_database_connection(request: DatabaseConnectionRequest) -> dict[str, Any]:
    import time
    from urllib.parse import quote

    import asyncpg

    start = time.time()
    host = (request.host or "127.0.0.1").strip()
    port = int(request.port or 5432)
    database = _validate_pg_identifier(request.database, "database")
    username = _validate_pg_identifier(request.username, "username")
    password = request.password or ""

    try:
        conn = await asyncpg.connect(
            host=host,
            port=port,
            user=username,
            password=password,
            database=database,
            timeout=5,
        )
        try:
            server_version = await conn.fetchval("select version()")
        finally:
            await conn.close()
        latency_ms = int((time.time() - start) * 1000)
        return {
            "success": True,
            "created": False,
            "database_missing": False,
            "can_create": False,
            "latency_ms": latency_ms,
            "message": "PostgreSQL connection ok",
            "server_version": server_version,
        }
    except asyncpg.InvalidCatalogNameError:
        if not request.create_if_missing:
            return {
                "success": False,
                "created": False,
                "database_missing": True,
                "can_create": True,
                "latency_ms": int((time.time() - start) * 1000),
                "message": f"Database '{database}' does not exist",
            }

        maintenance_db = "postgres" if database != "postgres" else "template1"
        conn = await asyncpg.connect(
            host=host,
            port=port,
            user=username,
            password=password,
            database=maintenance_db,
            timeout=5,
        )
        try:
            quoted_db = '"' + database.replace('"', '""') + '"'
            await conn.execute(f"CREATE DATABASE {quoted_db}")
        finally:
            await conn.close()

        # Verify the newly created database is reachable.
        verify_conn = await asyncpg.connect(
            host=host,
            port=port,
            user=username,
            password=password,
            database=database,
            timeout=5,
        )
        try:
            server_version = await verify_conn.fetchval("select version()")
        finally:
            await verify_conn.close()

        latency_ms = int((time.time() - start) * 1000)
        safe_url = f"postgresql+asyncpg://{quote(username)}:***@{host}:{port}/{quote(database)}"
        return {
            "success": True,
            "created": True,
            "database_missing": False,
            "can_create": False,
            "latency_ms": latency_ms,
            "message": f"Database '{database}' created and connection ok",
            "server_version": server_version,
            "safe_url": safe_url,
        }
    except asyncpg.InvalidPasswordError as exc:
        raise HTTPException(status_code=401, detail="Invalid PostgreSQL username or password") from exc
    except asyncpg.InvalidAuthorizationSpecificationError as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc
    except (OSError, TimeoutError, asyncio.TimeoutError) as exc:
        raise HTTPException(status_code=408, detail=f"PostgreSQL connection timeout/refused: {exc}") from exc
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"PostgreSQL connection failed: {exc}") from exc


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
