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

logger = logging.getLogger(__name__)

# 缓存探测结果，避免每次请求都重复检测
_CAPABILITIES_CACHE: Capabilities | None = None
_CAPABILITIES_CACHED_AT: datetime | None = None
_CACHE_TTL = timedelta(seconds=60)

DEFAULT_MILVUS_URL = "http://localhost:19530"
DEFAULT_MINERU_URL = "http://localhost:8002"


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
            if response.status_code < 500:
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

    gateway_url = ai_gateway_url or os.getenv("AI_GATEWAY_URL")
    milvus_target = milvus_url or os.getenv("MILVUS_URL") or DEFAULT_MILVUS_URL
    mineru_target = mineru_url or os.getenv("MINERU_URL") or DEFAULT_MINERU_URL

    results = await asyncio.gather(
        _check_http_get(gateway_url, "/health"),
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
        gateway_url = ai_gateway_url or os.getenv("AI_GATEWAY_URL")
        milvus_target = milvus_url or os.getenv("MILVUS_URL") or DEFAULT_MILVUS_URL
        mineru_target = mineru_url or os.getenv("MINERU_URL") or DEFAULT_MINERU_URL

        return Capabilities(
            ai_gateway=_check_http_get_sync(gateway_url, "/health"),
            milvus=_check_milvus_sync(milvus_target),
            mineru=_check_http_get_sync(mineru_target, "/health"),
        )


def _check_http_get_sync(url: str | None, path: str, timeout: float = 3.0) -> CapabilityStatus:
    """_check_http_get 的同步版本。"""
    if not url:
        return CapabilityStatus(available=False, reason="URL not configured")
    target = url.rstrip("/") + path
    try:
        import httpx

        response = httpx.get(target, timeout=timeout)
        if response.status_code < 500:
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
