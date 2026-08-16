"""基础设施能力探测。

数据库能力拆分为三个独立状态，避免"一个 postgres 状态代表所有"：
- ``core_database``（scope=core）：Core Catalog，默认本地 SQLite，PostgreSQL 为服务端可选；
- ``pgvector``（scope=gbrain）：gbrain / LLM Wiki 向量运行时，独立于 Core 数据库；
- ``external_datasources``（scope=datasource）：Analytics / 知识数据源连接外部业务数据库的能力。

其余条目（Docker、Milvus、MinerU）为可选基础设施探测。
模型请求由内部网关统一路由，不属于外部基础设施健康探测。
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import socket
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from cli_runtime import current_cli_runtime_status
from config import get_database_config, get_knowledge_mineru_config, load_config
from extensions import extension_enabled
from postgres_dependencies import PGVECTOR_STATUS_SQL, normalize_pgvector_status

logger = logging.getLogger(__name__)

# 缓存探测结果，避免每次请求都重复检测
_CAPABILITIES_CACHE: Capabilities | None = None
_CAPABILITIES_CACHED_AT: datetime | None = None
_CACHE_TTL = timedelta(seconds=60)

DEFAULT_MILVUS_URL = "http://localhost:19530"
DEFAULT_MINERU_URL = "http://localhost:8002"
DEFAULT_POSTGRES_URL = ""


def _profile_disabled(name: str, *, scope: str | None = None) -> CapabilityStatus:
    details = {"scope": scope} if scope else None
    return CapabilityStatus(
        available=False,
        reason=f"{name} is disabled by the current Runtime Profile",
        details=details,
    )


def _asyncpg_missing_status(*, scope: str, usage: str) -> CapabilityStatus:
    """asyncpg 是可选依赖；缺失时返回降级状态而不是抛错。"""
    return CapabilityStatus(
        available=False,
        reason="未安装 asyncpg（pip install puddingclaw-backend[postgres]）",
        details={"scope": scope, "driver": "missing", "用途": usage},
    )

@dataclass
class CapabilityStatus:
    available: bool
    reason: str | None = None
    details: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {"available": self.available, "reason": self.reason}
        if self.details:
            result["details"] = self.details
        return result


@dataclass
class Capabilities:
    core_database: CapabilityStatus
    pgvector: CapabilityStatus
    docker: CapabilityStatus
    milvus: CapabilityStatus
    mineru: CapabilityStatus
    external_datasources: CapabilityStatus
    cli: CapabilityStatus | None = None

    def to_dict(self) -> dict[str, Any]:
        result = {
            "core_database": self.core_database.to_dict(),
            "pgvector": self.pgvector.to_dict(),
            "external_datasources": self.external_datasources.to_dict(),
            "docker": self.docker.to_dict(),
            "milvus": self.milvus.to_dict(),
            "mineru": self.mineru.to_dict(),
            # Deprecated alias of core_database, kept for one version so older
            # frontends reading "database" do not break.
            "database": self.core_database.to_dict(),
        }
        if self.cli is not None:
            result["cli"] = self.cli.to_dict()
        return result


def _check_cli() -> CapabilityStatus:
    status = current_cli_runtime_status(Path(__file__).resolve().parent)
    installed = bool(status.get("installed"))
    details = {
        "安装状态": "已安装" if installed else "未安装",
        "版本": str(status.get("version") or "未检测到"),
        "Node.js": str((status.get("node") or {}).get("version") or "未检测到"),
        "npm": str((status.get("npm") or {}).get("version") or "未检测到"),
        "安装策略": str(status.get("install_policy") or "未配置"),
    }
    message = str(status.get("install_message") or "").strip()
    if message:
        details["检测说明"] = message
    return CapabilityStatus(
        available=installed,
        reason=None if installed else (message or "PuddingClaw CLI 尚未安装"),
        details=details,
    )


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
    """Core Catalog 数据库探测（scope=core）：SQLite 直接可用，PostgreSQL 探测连通性。"""
    target = _resolve_postgres_url(url)
    if not _is_postgres_url(target):
        if get_database_config().get("mode") == "sqlite":
            return CapabilityStatus(
                available=True,
                reason="SQLite catalog in PuddingClaw Home",
                details={"mode": "sqlite", "scope": "core"},
            )
        return CapabilityStatus(
            available=False,
            reason="PostgreSQL URL not configured",
            details={"mode": "postgresql", "scope": "core"},
        )

    try:
        import asyncpg  # noqa: F401
    except ImportError:
        return _asyncpg_missing_status(scope="core", usage="Core Catalog（PostgreSQL 模式）")

    engine = None
    try:
        engine = create_async_engine(_normalize_async_postgres_url(target), pool_pre_ping=True)
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
        return CapabilityStatus(available=True, details={"mode": "postgresql", "scope": "core"})
    except Exception as exc:  # noqa: BLE001
        return CapabilityStatus(
            available=False,
            reason=f"{type(exc).__name__}: {exc}",
            details={"mode": "postgresql", "scope": "core"},
        )
    finally:
        if engine is not None:
            await engine.dispose()


def _read_gbrain_database_url() -> str:
    """读取 gbrain 自己的 PostgreSQL DSN（独立于 Core 数据库配置）。

    gbrain 的连接信息位于 ``<gbrain_runtime_home>/.gbrain/config.json``
    （database_url / url 字段），与 Core Catalog 的 database 配置无关。
    读不到或格式非法时返回空串。
    """
    try:
        from knowledge.paths import get_gbrain_runtime_home

        config_path = (
            get_gbrain_runtime_home(Path(__file__).resolve().parent)
            / ".gbrain"
            / "config.json"
        )
        config = json.loads(config_path.read_text(encoding="utf-8"))
        return str(config.get("database_url") or config.get("url") or "").strip()
    except Exception:  # noqa: BLE001 - 探测代码不得因配置缺失而抛错
        return ""


async def _check_pgvector(url: str | None = None) -> CapabilityStatus:
    """Check gbrain/pgvector availability independently of Core DB health (scope=gbrain).

    只探测 gbrain 自己配置的 PostgreSQL（<gbrain_runtime_home>/.gbrain/config.json），
    不再回退 Core 数据库 URL——两者是独立的部署边界。gbrain 未配置时返回明确的
    “可选能力未配置”状态。
    """

    target = (url or "").strip() or _read_gbrain_database_url()
    if not target:
        return CapabilityStatus(
            available=False,
            reason="gbrain 未配置（可选能力）",
            details={"scope": "gbrain", "归属": "gbrain / LLM Wiki 向量运行时"},
        )
    if not _is_postgres_url(target):
        return CapabilityStatus(
            available=False,
            reason="gbrain 配置的数据库 URL 不是 PostgreSQL DSN",
            details={"scope": "gbrain", "归属": "gbrain / LLM Wiki 向量运行时"},
        )
    try:
        import asyncpg  # noqa: F401
    except ImportError:
        return _asyncpg_missing_status(scope="gbrain", usage="gbrain / LLM Wiki 向量运行时")
    engine = None
    try:
        engine = create_async_engine(_normalize_async_postgres_url(target), pool_pre_ping=True)
        async with engine.connect() as conn:
            row = (await conn.execute(text(PGVECTOR_STATUS_SQL))).mappings().one()
        status = normalize_pgvector_status(row)
        if status["available"]:
            version = status["version"] or "available"
            return CapabilityStatus(
                available=True,
                reason=f"pgvector {version}",
                details={"scope": "gbrain", "归属": "gbrain / LLM Wiki 向量运行时", "version": version},
            )
        return CapabilityStatus(
            available=False,
            reason=f"Required PostgreSQL extension is missing. Install: {status['install_command']}",
            details={"scope": "gbrain", "归属": "gbrain / LLM Wiki 向量运行时"},
        )
    except Exception as exc:  # noqa: BLE001
        return CapabilityStatus(
            available=False,
            reason=f"{type(exc).__name__}: {exc}",
            details={"scope": "gbrain", "归属": "gbrain / LLM Wiki 向量运行时"},
        )
    finally:
        if engine is not None:
            await engine.dispose()


def _check_external_datasources() -> CapabilityStatus:
    """连接外部业务数据库数据源的能力（scope=datasource）。

    只反映驱动能力（asyncpg 是否可用），供 Analytics / 知识数据库数据源使用；
    不探测用户已配置数据源的实际连通性。
    """
    usage = "Analytics / 知识数据库数据源的外部 PostgreSQL 连接"
    try:
        import asyncpg  # noqa: F401
    except ImportError:
        return _asyncpg_missing_status(scope="datasource", usage=usage)
    return CapabilityStatus(
        available=True,
        reason="asyncpg 驱动可用",
        details={
            "scope": "datasource",
            "用途": usage,
            "说明": "仅反映驱动能力，不探测已配置数据源的连通性",
        },
    )


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

    knowledge_enabled = extension_enabled("knowledge")
    knowledge_results = await asyncio.gather(
        _check_pgvector(),
        _check_milvus(milvus_target),
        _check_http_get(mineru_target, "/health"),
    ) if knowledge_enabled else (
        _profile_disabled("pgvector", scope="gbrain"),
        _profile_disabled("Milvus"),
        _profile_disabled("MinerU"),
    )
    core_results = await asyncio.gather(
        _check_postgres(postgres_target),
        _check_docker(),
    )

    caps = Capabilities(
        core_database=core_results[0],
        pgvector=knowledge_results[0],
        docker=core_results[1],
        milvus=knowledge_results[1],
        mineru=knowledge_results[2],
        external_datasources=_check_external_datasources(),
        cli=_check_cli(),
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

    knowledge_enabled = extension_enabled("knowledge")
    caps = Capabilities(
        core_database=_check_postgres_sync(postgres_target),
        pgvector=CapabilityStatus(
            available=False,
            reason="pgvector status is verified by the asynchronous infrastructure probe",
            details={"scope": "gbrain"},
        ) if knowledge_enabled else _profile_disabled("pgvector", scope="gbrain"),
        docker=_check_docker_sync(),
        milvus=_check_milvus_sync(milvus_target) if knowledge_enabled else _profile_disabled("Milvus"),
        mineru=_check_http_get_sync(mineru_target, "/health") if knowledge_enabled else _profile_disabled("MinerU"),
        external_datasources=_check_external_datasources(),
        cli=_check_cli(),
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
        if get_database_config().get("mode") == "sqlite":
            return CapabilityStatus(
                available=True,
                reason="SQLite catalog in PuddingClaw Home",
                details={"mode": "sqlite", "scope": "core"},
            )
        return CapabilityStatus(
            available=False,
            reason="PostgreSQL URL not configured",
            details={"mode": "postgresql", "scope": "core"},
        )

    parsed = urlparse(target.replace("postgresql+asyncpg://", "postgresql://", 1))
    host = parsed.hostname or "localhost"
    port = parsed.port or 5432
    try:
        with socket.create_connection((host, port), timeout=3.0):
            return CapabilityStatus(available=True, details={"mode": "postgresql", "scope": "core"})
    except Exception as exc:  # noqa: BLE001
        return CapabilityStatus(
            available=False,
            reason=f"{type(exc).__name__}: {exc}",
            details={"mode": "postgresql", "scope": "core"},
        )


def invalidate_capabilities() -> None:
    """清除能力探测缓存，主要用于测试。"""
    global _CAPABILITIES_CACHE, _CAPABILITIES_CACHED_AT
    _CAPABILITIES_CACHE = None
    _CAPABILITIES_CACHED_AT = None
