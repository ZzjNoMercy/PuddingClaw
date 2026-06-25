"""可选基础设施能力探测。

在 core/full 混合部署下，backend 启动时异步检测 Higress、Milvus、MinerU 是否可用，
业务代码通过 detect_capabilities() 获取结果并自动 fallback。
"""

from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx

from config import get_gateway_config

logger = logging.getLogger(__name__)

# 缓存探测结果，避免每次请求都重复检测
_CAPABILITIES_CACHE: Capabilities | None = None
_CAPABILITIES_CACHED_AT: datetime | None = None
_CACHE_TTL = timedelta(seconds=60)

DEFAULT_MILVUS_URL = "http://localhost:19530"
DEFAULT_MINERU_URL = "http://localhost:8002"

# 当 AI_GATEWAY_URL 未配置时，自动探测这些地址
# Higress 统一走 Docker full profile，backend 与 higress 在同一个 compose 网络内
_DEFAULT_GATEWAY_URLS = [
    "http://higress:8080/v1",
]

# 最后一次成功探测到的 Higress URL，供 ModelClient 等业务代码使用
_EFFECTIVE_GATEWAY_URL: str | None = None


def get_effective_gateway_url() -> str | None:
    """返回当前生效的 Higress URL。

    优先级：
    1. 最近一次成功探测到的 URL（含自动探测）。
    2. 显式配置的 URL（config.json / AI_GATEWAY_URL 环境变量）。
    """
    if _EFFECTIVE_GATEWAY_URL:
        return _EFFECTIVE_GATEWAY_URL
    return _resolve_explicit_gateway_url()


@dataclass
class CapabilityStatus:
    available: bool
    reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {"available": self.available, "reason": self.reason}


@dataclass
class Capabilities:
    ai_gateway: CapabilityStatus
    milvus: CapabilityStatus
    mineru: CapabilityStatus

    def to_dict(self) -> dict[str, Any]:
        return {
            "ai_gateway": self.ai_gateway.to_dict(),
            "milvus": self.milvus.to_dict(),
            "mineru": self.mineru.to_dict(),
        }


async def _check_http_get(url: str, path: str, timeout: float = 3.0) -> CapabilityStatus:
    """对指定 URL 发送 HTTP GET 健康检查。"""
    if not url:
        return CapabilityStatus(available=False, reason="URL not configured")

    target = url.rstrip("/") + path
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
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


def _normalize_gateway_url(url: str) -> str:
    """把以 /v1 结尾的 gateway URL 归一化为 base URL（供 health check 使用）。"""
    url = url.rstrip("/")
    if url.endswith("/v1"):
        return url[:-3]
    return url


def _resolve_explicit_gateway_url() -> str | None:
    """返回用户显式配置的 Higress URL（参数 / config.json / 环境变量），未配置则返回 None。"""
    gateway_config = get_gateway_config()
    return gateway_config.get("base_url") or os.getenv("AI_GATEWAY_URL")


def _build_gateway_urls(explicit_url: str | None = None) -> list[str]:
    """构建待探测的 Higress URL 列表。

    如果用户显式配置了 URL（参数 / config.json / 环境变量），只探测该 URL；
    未配置时才 fallback 到默认 URL 列表。
    """
    urls: list[str] = []
    if explicit_url:
        urls.append(explicit_url)
    explicit_config = _resolve_explicit_gateway_url()
    if explicit_config and explicit_config not in urls:
        urls.append(explicit_config)
    # 只有完全没有显式配置时，才尝试自动探测默认地址
    if not urls:
        urls.extend(_DEFAULT_GATEWAY_URLS)
    return urls



async def _check_gateway_urls(health_path: str, explicit_url: str | None = None) -> CapabilityStatus:
    """按优先级探测 Higress URL。"""
    global _EFFECTIVE_GATEWAY_URL
    urls_to_try = _build_gateway_urls(explicit_url)

    if not urls_to_try:
        return CapabilityStatus(available=False, reason="URL not configured")

    last_status: CapabilityStatus | None = None
    for url in urls_to_try:
        base_url = _normalize_gateway_url(url)
        # Higress all-in-one 启动初期可能响应较慢，给 5 秒
        last_status = await _check_http_get(base_url, health_path, timeout=5.0)
        if last_status.available:
            _EFFECTIVE_GATEWAY_URL = url
            logger.info("[capabilities] Higress detected at %s", url)
            return CapabilityStatus(available=True)
    _EFFECTIVE_GATEWAY_URL = None
    return last_status or CapabilityStatus(available=False, reason="URL not configured")


def _check_gateway_urls_sync(health_path: str, explicit_url: str | None = None) -> CapabilityStatus:
    """_check_gateway_urls 的同步版本。"""
    global _EFFECTIVE_GATEWAY_URL
    urls_to_try = _build_gateway_urls(explicit_url)

    if not urls_to_try:
        return CapabilityStatus(available=False, reason="URL not configured")

    last_status: CapabilityStatus | None = None
    for url in urls_to_try:
        base_url = _normalize_gateway_url(url)
        last_status = _check_http_get_sync(base_url, health_path, timeout=5.0)
        if last_status.available:
            _EFFECTIVE_GATEWAY_URL = url
            logger.info("[capabilities] Higress detected at %s", url)
            return CapabilityStatus(available=True)
    _EFFECTIVE_GATEWAY_URL = None
    return last_status or CapabilityStatus(available=False, reason="URL not configured")


async def detect_capabilities(
    *,
    force: bool = False,
    ai_gateway_url: str | None = None,
    milvus_url: str | None = None,
    mineru_url: str | None = None,
) -> Capabilities:
    """探测可选基础设施可用性。

    Args:
        force: 是否强制重新探测，忽略缓存。
        ai_gateway_url: 显式指定 Higress URL；默认读 AI_GATEWAY_URL 环境变量。
        milvus_url: 显式指定 Milvus URL；默认读 MILVUS_URL 环境变量。
        mineru_url: 显式指定 MinerU URL；默认读 MINERU_URL 环境变量。

    Returns:
        Capabilities 探测结果。
    """
    global _CAPABILITIES_CACHE, _CAPABILITIES_CACHED_AT

    if not force and _CAPABILITIES_CACHE is not None and _CAPABILITIES_CACHED_AT is not None:
        if datetime.now(timezone.utc) - _CAPABILITIES_CACHED_AT < _CACHE_TTL:
            return _CAPABILITIES_CACHE

    gateway_config = get_gateway_config()
    gateway_health_path = str(gateway_config.get("health_path", "/health"))
    milvus_target = milvus_url or os.getenv("MILVUS_URL") or DEFAULT_MILVUS_URL
    mineru_target = mineru_url or os.getenv("MINERU_URL") or DEFAULT_MINERU_URL

    results = await asyncio.gather(
        _check_gateway_urls(gateway_health_path, explicit_url=ai_gateway_url),
        _check_milvus(milvus_target),
        _check_http_get(mineru_target, "/health"),
    )

    caps = Capabilities(
        ai_gateway=results[0],
        milvus=results[1],
        mineru=results[2],
    )

    _CAPABILITIES_CACHE = caps
    _CAPABILITIES_CACHED_AT = datetime.now(timezone.utc)
    logger.debug("Capabilities detected: %s", caps.to_dict())
    return caps


def detect_capabilities_sync(
    *,
    force: bool = False,
    ai_gateway_url: str | None = None,
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
            ai_gateway_url=ai_gateway_url,
            milvus_url=milvus_url,
            mineru_url=mineru_url,
        )
    try:
        return asyncio.run(
            detect_capabilities(
                force=force,
                ai_gateway_url=ai_gateway_url,
                milvus_url=milvus_url,
                mineru_url=mineru_url,
            )
        )
    except RuntimeError as exc:
        # 如果当前线程已有事件循环（如在异步 FastAPI 中同步调用），回退到同步探测
        logger.debug("[capabilities] asyncio.run failed (%s), falling back to sync checks", exc)
        return _detect_capabilities_sync_fallback(
            force=force,
            ai_gateway_url=ai_gateway_url,
            milvus_url=milvus_url,
            mineru_url=mineru_url,
        )


def _detect_capabilities_sync_fallback(
    *,
    force: bool = False,
    ai_gateway_url: str | None = None,
    milvus_url: str | None = None,
    mineru_url: str | None = None,
) -> Capabilities:
    """Synchronous capability probing used when async probing is not available."""
    global _CAPABILITIES_CACHE, _CAPABILITIES_CACHED_AT

    if not force and _CAPABILITIES_CACHE is not None and _CAPABILITIES_CACHED_AT is not None:
        if datetime.now(timezone.utc) - _CAPABILITIES_CACHED_AT < _CACHE_TTL:
            return _CAPABILITIES_CACHE

    gateway_config = get_gateway_config()
    gateway_health_path = str(gateway_config.get("health_path", "/health"))
    milvus_target = milvus_url or os.getenv("MILVUS_URL") or DEFAULT_MILVUS_URL
    mineru_target = mineru_url or os.getenv("MINERU_URL") or DEFAULT_MINERU_URL

    caps = Capabilities(
        ai_gateway=_check_gateway_urls_sync(gateway_health_path, explicit_url=ai_gateway_url),
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

        response = httpx.get(target, timeout=timeout)
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


def invalidate_capabilities() -> None:
    """清除能力探测缓存，主要用于测试。"""
    global _CAPABILITIES_CACHE, _CAPABILITIES_CACHED_AT
    _CAPABILITIES_CACHE = None
    _CAPABILITIES_CACHED_AT = None
