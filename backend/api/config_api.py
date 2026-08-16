"""Configuration API — settings management + connection testing."""

import asyncio
import logging
import re
from typing import Any

import httpx
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict
from starlette.concurrency import run_in_threadpool

import runtime_control
from config import get_settings_for_display, update_settings
from postgres_dependencies import PGVECTOR_STATUS_SQL, normalize_pgvector_status
from provider_registry import get_provider_registry

router = APIRouter()
logger = logging.getLogger(__name__)


async def _assert_database_settings_write_allowed() -> None:
    """维护期（draining/maintenance）禁止改 settings 的 database 段。

    容错：DB 未初始化或 runtime_control 表不存在等探测失败时放行，不阻塞
    正常服务；命中维护期时抛 MaintenanceModeError，由全局 handler 映射 503。
    """

    try:
        from db import get_sessionmaker

        async with get_sessionmaker()() as session:
            await runtime_control.assert_writes_allowed(session)
    except runtime_control.MaintenanceModeError:
        raise
    except Exception:  # noqa: BLE001 - 探测失败不阻塞正常服务
        logger.warning("[settings] runtime_control 写入门控探测失败，放行 database 段更新", exc_info=True)


# ── Settings CRUD ──────────────────────────────────────────


class SettingsUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    rag: dict[str, Any] | None = None
    vanna: dict[str, Any] | None = None
    analytics: dict[str, Any] | None = None
    database: dict[str, Any] | None = None
    knowledge: dict[str, Any] | None = None
    compression: dict[str, Any] | None = None
    harness: dict[str, Any] | None = None
    subagents: dict[str, Any] | None = None


class ProviderUpdateRequest(BaseModel):
    name: str | None = None
    enabled: bool | None = None
    endpoints: list[dict[str, Any]] = []
    credentials: list[dict[str, Any]] = []


class ProviderBindingRequest(BaseModel):
    model_id: str


class ProviderModelRequest(BaseModel):
    endpoint_id: str
    capability: str
    name: str
    categories: list[str] | None = None
    dimension: int | None = None
    batch_size: int | None = None
    concurrency: int | None = None


class ProviderConnectionTestRequest(BaseModel):
    base_url: str = ""
    api_key: str = ""
    credential_name: str | None = None


@router.get("/settings")
async def get_settings():
    """Get current settings with masked API keys."""
    return get_settings_for_display()


@router.put("/settings")
async def put_settings(request: SettingsUpdateRequest):
    """Update settings (partial update supported)."""
    try:
        updates = request.model_dump(exclude_none=True)
        if "database" in updates:
            await _assert_database_settings_write_allowed()
        extra = update_settings(updates)
        import capabilities
        capabilities.invalidate_capabilities()
        return {"success": True, "message": "Settings saved", **(extra or {})}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except runtime_control.MaintenanceModeError:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to save settings: {e}")


# ── Direct Provider Registry ───────────────────────────────


@router.get("/providers")
async def get_providers():
    """Provider/endpoint/model metadata only; credentials are always masked."""
    return get_provider_registry().display()


@router.patch("/providers/{provider_id}")
async def patch_provider(provider_id: str, request: ProviderUpdateRequest):
    try:
        return get_provider_registry().update_provider(
            provider_id,
            request.model_dump(exclude_none=True),
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.put("/providers/bindings/{binding}")
async def put_provider_binding(binding: str, request: ProviderBindingRequest):
    try:
        get_provider_registry().set_binding(binding, request.model_id)
        return {"success": True, "binding": binding, "model_id": request.model_id}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/providers/{provider_id}/models")
async def upsert_provider_model(provider_id: str, request: ProviderModelRequest):
    try:
        model = get_provider_registry().upsert_model(provider_id, request.model_dump(exclude_none=True))
        return {"model": model}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/providers/{provider_id}/endpoints/{endpoint_id}/discover-models")
async def discover_provider_models(provider_id: str, endpoint_id: str):
    """Discover models from this specific Provider endpoint, never a fallback binding."""
    try:
        models = await run_in_threadpool(get_provider_registry().discover_models, provider_id, endpoint_id)
        return {"models": models}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail=f"Provider model discovery failed: {exc}") from exc


@router.post("/providers/{provider_id}/endpoints/{endpoint_id}/test-connection")
async def test_provider_connection(
    provider_id: str,
    endpoint_id: str,
    request: ProviderConnectionTestRequest,
):
    """Test endpoint reachability/authentication; model discovery is separate."""
    import time

    started_at = time.time()
    try:
        probe_kwargs: dict[str, Any] = {
            "base_url": request.base_url,
            "api_key": request.api_key,
        }
        if request.credential_name:
            probe_kwargs["credential_name"] = request.credential_name
        result = await run_in_threadpool(
            get_provider_registry().test_endpoint,
            provider_id,
            endpoint_id,
            **probe_kwargs,
        )
        return {
            "success": True,
            "latency_ms": int((time.time() - started_at) * 1000),
            **result,
        }
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except httpx.HTTPStatusError as exc:
        status_code = exc.response.status_code
        if status_code in {401, 403}:
            raise HTTPException(status_code=status_code, detail="Provider authentication failed") from exc
        raise HTTPException(status_code=502, detail=f"Provider connectivity failed: HTTP {status_code}") from exc
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail=f"Provider connectivity failed: {exc}") from exc


# ── Connection testing ─────────────────────────────────────


class DatabaseConnectionRequest(BaseModel):
    mode: str = "external"
    host: str = "127.0.0.1"
    port: int = 5432
    database: str = "puddingclaw"
    username: str = "puddingclaw"
    password: str = ""
    create_if_missing: bool = False


class DockerProbeRequest(BaseModel):
    connection: str = ""
    context: str = ""


@router.post("/settings/harness/docker/probe")
async def probe_harness_docker(request: DockerProbeRequest):
    """Probe the user-configured Docker daemon without creating a container."""

    from harness.workspace_backends import ProjectSandboxManager

    manager = ProjectSandboxManager(request.model_dump())
    available, detail = await run_in_threadpool(manager.probe)
    return {
        "available": available,
        "detail": detail,
        "connection": request.connection,
        "context": request.context,
    }


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

    try:
        import asyncpg
    except ImportError as exc:
        raise HTTPException(
            status_code=400,
            detail=(
                "asyncpg 未安装：PostgreSQL 连接测试需要可选依赖，"
                "请安装 postgres extra（pip install 'puddingclaw-backend[postgres]' 或 uv sync --extra postgres）。"
            ),
        ) from exc

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
            pgvector = normalize_pgvector_status(dict(await conn.fetchrow(PGVECTOR_STATUS_SQL) or {}))
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
            "pgvector": pgvector,
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
            pgvector = normalize_pgvector_status(dict(await verify_conn.fetchrow(PGVECTOR_STATUS_SQL) or {}))
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
            "pgvector": pgvector,
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
