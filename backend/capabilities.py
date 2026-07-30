"""基础设施能力探测。

在 core/full 混合部署下，backend 启动时异步检测 PostgreSQL、pgvector、Docker、Milvus、MinerU 是否可用。
模型请求由内部网关统一路由，不属于外部基础设施健康探测。
"""

from __future__ import annotations

import asyncio
import logging
import os
import socket
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import urlparse

import httpx
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from config import get_database_config, get_knowledge_mineru_config, load_config
from postgres_dependencies import PGVECTOR_STATUS_SQL, normalize_pgvector_status

logger = logging.getLogger(__name__)

# 缓存探测结果，避免每次请求都重复检测
_CAPABILITIES_CACHE: Capabilities | None = None
_CAPABILITIES_CACHED_AT: datetime | None = None
_CACHE_TTL = timedelta(seconds=60)

DEFAULT_MILVUS_URL = "http://localhost:19530"
DEFAULT_MINERU_URL = "http://localhost:8002"
DEFAULT_POSTGRES_URL = ""

@dataclass
class CapabilityStatus:
    available: bool
    reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {"available": self.available, "reason": self.reason}


@dataclass
class Capabilities:
    database: CapabilityStatus
    pgvector: CapabilityStatus
    docker: CapabilityStatus
    milvus: CapabilityStatus
    mineru: CapabilityStatus

    def to_dict(self) -> dict[str, Any]:
        return {
            "database": self.database.to_dict(),
            "pgvector": self.pgvector.to_dict(),
            "docker": self.docker.to_dict(),
            "milvus": self.milvus.to_dict(),
            "mineru": self.mineru.to_dict(),
        }


async def _check_http_get(url: str, path: str, timeout: float = 3.0) -> CapabilityStatus:
    """对指定 URL 发送 HTTP GET 健康检查。"""
    if not url:
        return CapabilityStatus(available=False, reason="URL not configured")

    target = url.rstrip("/") + path
    try:
        async with httpx.AsyncClient(timeout=timeout, trust_env=False) as client:
            response = await client.get(target)
            if 200 <= response.status_code < 400:
                return CapabilityStatus(available=True)
            return CapabilityStatus(
                available=False,
                reason=f"HTTP {response.status_code}",
            )
    except httpx.ConnectError as exc:
        return CapabilityStatus(available=False, reason=f"Connection refused: {exc}")
    except httpx.TimeoutException:
        return CapabilityStatus(available=False, reason="Timeout")
    except Exception as exc:  # noqa: BLE001
        return CapabilityStatus(available=False, reason=f"{type(exc).__name__}: {exc}")


async def _check_milvus(url: str | None) -> CapabilityStatus:
    """尝试连接 Milvus。"""
    target = url or os.getenv("MILVUS_URL") or DEFAULT_MILVUS_URL
    if not target:
        return CapabilityStatus(available=False, reason="URL not configured")

    try:
        # 延迟导入，避免 core 模式无 Milvus 时启动失败
        from pymilvus import MilvusClient

        client = MilvusClient(uri=target, timeout=3.0)
        # 简单调用验证连接
        client.list_collections()
        return CapabilityStatus(available=True)
    except Exception as exc:  # noqa: BLE001
        return CapabilityStatus(available=False, reason=f"{type(exc).__name__}: {exc}")


def _docker_config() -> dict[str, Any]:
    config = load_config()
    docker = config.get("harness", {}).get("terminal", {}).get("docker", {})
    return dict(docker) if isinstance(docker, dict) else {}


def _check_docker_sync() -> CapabilityStatus:
    """复用沙箱设置页的 Docker daemon 探测，不创建容器。"""
    try:
        from harness.workspace_backends import ProjectSandboxManager

        available, detail = ProjectSandboxManager(_docker_config()).probe()
        return CapabilityStatus(
            available=available,
            reason=None if available else (detail or "Docker daemon unavailable"),
        )
    except Exception as exc:  # noqa: BLE001
        return CapabilityStatus(available=False, reason=f"{type(exc).__name__}: {exc}")


async def _check_docker() -> CapabilityStatus:
    return await asyncio.to_thread(_check_docker_sync)


def _resolve_postgres_url(explicit_url: str | None = None) -> str:
    database_config = get_database_config()
    return (
        explicit_url
        or str(database_config.get("url") or "")
        or DEFAULT_POSTGRES_URL
    )


def _normalize_async_postgres_url(url: str) -> str:
    if url.startswith("postgresql://"):
        return url.replace("postgresql://", "postgresql+asyncpg://", 1)
    return url


def _is_postgres_url(url: str) -> bool:
    return url.startswith("postgresql://") or url.startswith("postgresql+")


async def _check_postgres(url: str | None) -> CapabilityStatus:
    target = _resolve_postgres_url(url)
    if not _is_postgres_url(target):
        return CapabilityStatus(available=False, reason="PostgreSQL URL not configured")

    engine = None
    try:
        engine = create_async_engine(_normalize_async_postgres_url(target), pool_pre_ping=True)
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
        return CapabilityStatus(available=True)
    except Exception as exc:  # noqa: BLE001
        return CapabilityStatus(available=False, reason=f"{type(exc).__name__}: {exc}")
    finally:
        if engine is not None:
            await engine.dispose()


async def _check_pgvector(url: str | None) -> CapabilityStatus:
    """Check server-side pgvector availability independently of DB health."""

    target = _resolve_postgres_url(url)
    if not _is_postgres_url(target):
        return CapabilityStatus(available=False, reason="PostgreSQL URL not configured")
    engine = None
    try:
        engine = create_async_engine(_normalize_async_postgres_url(target), pool_pre_ping=True)
        async with engine.connect() as conn:
            row = (await conn.execute(text(PGVECTOR_STATUS_SQL))).mappings().one()
        status = normalize_pgvector_status(row)
        if status["available"]:
            version = status["version"] or "available"
            return CapabilityStatus(available=True, reason=f"pgvector {version}")
        return CapabilityStatus(
            available=False,
            reason=f"Required PostgreSQL extension is missing. Install: {status['install_command']}",
        )
    except Exception as exc:  # noqa: BLE001
        return CapabilityStatus(available=False, reason=f"{type(exc).__name__}: {exc}")
    finally:
        if engine is not None:
            await engine.dispose()


async def detect_capabilities(
    *,
    force: bool = False,
    postgres_url: str | None = None,
    milvus_url: str | None = None,
    mineru_url: str | None = None,
) -> Capabilities:
    """探测可选基础设施可用性。

    Args:
        force: 是否强制重新探测，忽略缓存。
        postgres_url: 显式指定 PostgreSQL URL；默认读 Settings 中的数据库配置。
        milvus_url: 显式指定 Milvus URL；默认读 MILVUS_URL 环境变量。
        mineru_url: 显式指定 MinerU URL；默认读 config.json 的 knowledge.mineru.base_url。

    Returns:
        Capabilities 探测结果。
    """
    global _CAPABILITIES_CACHE, _CAPABILITIES_CACHED_AT

    if not force and _CAPABILITIES_CACHE is not None and _CAPABILITIES_CACHED_AT is not None:
        if datetime.now(timezone.utc) - _CAPABILITIES_CACHED_AT < _CACHE_TTL:
            return _CAPABILITIES_CACHE

    postgres_target = _resolve_postgres_url(postgres_url)
    milvus_target = milvus_url or os.getenv("MILVUS_URL") or DEFAULT_MILVUS_URL
    mineru_config = get_knowledge_mineru_config()
    mineru_target = mineru_url or str(mineru_config.get("base_url") or DEFAULT_MINERU_URL)

    results = await asyncio.gather(
        _check_postgres(postgres_target),
        _check_pgvector(postgres_target),
        _check_docker(),
        _check_milvus(milvus_target),
        _check_http_get(mineru_target, "/health"),
    )

    caps = Capabilities(
        database=results[0],
        pgvector=results[1],
        docker=results[2],
        milvus=results[3],
        mineru=results[4],
    )

    _CAPABILITIES_CACHE = caps
    _CAPABILITIES_CACHED_AT = datetime.now(timezone.utc)
    logger.debug("Capabilities detected: %s", caps.to_dict())
    return caps


def detect_capabilities_sync(
    *,
    force: bool = False,
    postgres_url: str | None = None,
    milvus_url: str | None = None,
    mineru_url: str | None = None,
) -> Capabilities:
    """detect_capabilities 的同步包装，供同步代码（如 ModelClient.get_chat_model）使用。"""
    if not force and _CAPABILITIES_CACHE is not None and _CAPABILITIES_CACHED_AT is not None:
        if datetime.now(timezone.utc) - _CAPABILITIES_CACHED_AT < _CACHE_TTL:
            return _CAPABILITIES_CACHE
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        pass
    else:
        # We are already inside an event loop. Calling asyncio.run() here would
        # create a coroutine and then fail, leaving an unawaited-coroutine
        # warning. Use the blocking sync probes instead.
        logger.debug("[capabilities] running loop detected, using sync checks")
        return _detect_capabilities_sync_fallback(
            force=force,
            postgres_url=postgres_url,
            milvus_url=milvus_url,
            mineru_url=mineru_url,
        )
    try:
        return asyncio.run(
            detect_capabilities(
                force=force,
                postgres_url=postgres_url,
                milvus_url=milvus_url,
                mineru_url=mineru_url,
            )
        )
    except RuntimeError as exc:
        # 如果当前线程已有事件循环（如在异步 FastAPI 中同步调用），回退到同步探测
        logger.debug("[capabilities] asyncio.run failed (%s), falling back to sync checks", exc)
        return _detect_capabilities_sync_fallback(
            force=force,
            postgres_url=postgres_url,
            milvus_url=milvus_url,
            mineru_url=mineru_url,
        )


def _detect_capabilities_sync_fallback(
    *,
    force: bool = False,
    postgres_url: str | None = None,
    milvus_url: str | None = None,
    mineru_url: str | None = None,
) -> Capabilities:
    """Synchronous capability probing used when async probing is not available."""
    global _CAPABILITIES_CACHE, _CAPABILITIES_CACHED_AT

    if not force and _CAPABILITIES_CACHE is not None and _CAPABILITIES_CACHED_AT is not None:
        if datetime.now(timezone.utc) - _CAPABILITIES_CACHED_AT < _CACHE_TTL:
            return _CAPABILITIES_CACHE

    postgres_target = _resolve_postgres_url(postgres_url)
    milvus_target = milvus_url or os.getenv("MILVUS_URL") or DEFAULT_MILVUS_URL
    mineru_config = get_knowledge_mineru_config()
    mineru_target = mineru_url or str(mineru_config.get("base_url") or DEFAULT_MINERU_URL)

    caps = Capabilities(
        database=_check_postgres_sync(postgres_target),
        pgvector=CapabilityStatus(
            available=False,
            reason="pgvector status is verified by the asynchronous infrastructure probe",
        ),
        docker=_check_docker_sync(),
        milvus=_check_milvus_sync(milvus_target),
        mineru=_check_http_get_sync(mineru_target, "/health"),
    )
    _CAPABILITIES_CACHE = caps
    _CAPABILITIES_CACHED_AT = datetime.now(timezone.utc)
    logger.debug("Capabilities detected synchronously: %s", caps.to_dict())
    return caps


def _check_http_get_sync(url: str | None, path: str, timeout: float = 3.0) -> CapabilityStatus:
    """_check_http_get 的同步版本。"""
    if not url:
        return CapabilityStatus(available=False, reason="URL not configured")
    target = url.rstrip("/") + path
    try:
        import httpx

        response = httpx.get(target, timeout=timeout, trust_env=False)
        if 200 <= response.status_code < 400:
            return CapabilityStatus(available=True)
        return CapabilityStatus(available=False, reason=f"HTTP {response.status_code}")
    except httpx.ConnectError as exc:
        return CapabilityStatus(available=False, reason=f"Connection refused: {exc}")
    except httpx.TimeoutException:
        return CapabilityStatus(available=False, reason="Timeout")
    except Exception as exc:  # noqa: BLE001
        return CapabilityStatus(available=False, reason=f"{type(exc).__name__}: {exc}")


def _check_milvus_sync(url: str | None) -> CapabilityStatus:
    """_check_milvus 的同步版本。"""
    target = url or os.getenv("MILVUS_URL") or DEFAULT_MILVUS_URL
    if not target:
        return CapabilityStatus(available=False, reason="URL not configured")
    try:
        from pymilvus import MilvusClient

        client = MilvusClient(uri=target, timeout=3.0)
        client.list_collections()
        return CapabilityStatus(available=True)
    except Exception as exc:  # noqa: BLE001
        return CapabilityStatus(available=False, reason=f"{type(exc).__name__}: {exc}")


def _check_postgres_sync(url: str | None) -> CapabilityStatus:
    """同步路径只做 TCP 探测，避免在已有事件循环里阻塞 asyncpg。"""
    target = _resolve_postgres_url(url)
    if not _is_postgres_url(target):
        return CapabilityStatus(available=False, reason="PostgreSQL URL not configured")

    parsed = urlparse(target.replace("postgresql+asyncpg://", "postgresql://", 1))
    host = parsed.hostname or "localhost"
    port = parsed.port or 5432
    try:
        with socket.create_connection((host, port), timeout=3.0):
            return CapabilityStatus(available=True)
    except Exception as exc:  # noqa: BLE001
        return CapabilityStatus(available=False, reason=f"{type(exc).__name__}: {exc}")


def invalidate_capabilities() -> None:
    """清除能力探测缓存，主要用于测试。"""
    global _CAPABILITIES_CACHE, _CAPABILITIES_CACHED_AT
    _CAPABILITIES_CACHE = None
    _CAPABILITIES_CACHED_AT = None
