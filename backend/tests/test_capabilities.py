"""capabilities 模块单元测试。"""

import os
from unittest import mock

import httpx
import pytest

from capabilities import (
    Capabilities,
    CapabilityStatus,
    detect_capabilities,
    invalidate_capabilities,
)


# 当测试未显式配置 AI_GATEWAY_URL 时，自动探测会请求这个地址
_DEFAULT_HIGRESS_HEALTH = "http://higress:8080/health"


@pytest.fixture(autouse=True)
def _clear_cache_and_env():
    """每个测试前清除缓存和相关环境变量。"""
    invalidate_capabilities()
    for key in ("AI_GATEWAY_URL", "MILVUS_URL", "MINERU_URL"):
        os.environ.pop(key, None)
    yield
    invalidate_capabilities()


def _mock_default_higress_unavailable(httpx_mock):
    """未显式配置 gateway URL 时，默认 Higress 探测为不可用。"""
    httpx_mock.add_exception(
        httpx.ConnectError("Connection refused"),
        url=_DEFAULT_HIGRESS_HEALTH,
    )


@pytest.fixture(autouse=True)
def _mock_milvus_unavailable():
    """默认将 Milvus 探测 mock 为不可用，避免测试机本地 Milvus 干扰。"""
    with mock.patch("capabilities._check_milvus") as mock_check:
        mock_check.return_value = CapabilityStatus(available=False, reason="mocked unavailable")
        yield


@pytest.mark.asyncio
async def test_detect_capabilities_no_services(httpx_mock):
    """无服务配置时，所有能力应为不可用。"""
    _mock_default_higress_unavailable(httpx_mock)
    httpx_mock.add_exception(httpx.ConnectError("Connection refused"), url="http://localhost:8002/health")
    caps = await detect_capabilities(force=True)
    assert isinstance(caps, Capabilities)
    assert caps.ai_gateway.available is False
    assert caps.milvus.available is False
    assert caps.mineru.available is False


@pytest.mark.asyncio
async def test_detect_capabilities_mineru_available(httpx_mock):
    """MinerU /health 返回 200 时标记为可用。"""
    _mock_default_higress_unavailable(httpx_mock)
    httpx_mock.add_response(url="http://localhost:8002/health", status_code=200)
    caps = await detect_capabilities(force=True)
    assert caps.mineru == CapabilityStatus(available=True)


@pytest.mark.asyncio
async def test_detect_capabilities_gateway_5xx(httpx_mock):
    """Higress 返回 500 时标记为不可用并带原因。"""
    os.environ["AI_GATEWAY_URL"] = "http://gateway:8080"
    httpx_mock.add_response(url="http://gateway:8080/health", status_code=500)
    httpx_mock.add_exception(httpx.ConnectError("Connection refused"), url="http://localhost:8002/health")
    caps = await detect_capabilities(force=True)
    assert caps.ai_gateway.available is False
    assert "500" in (caps.ai_gateway.reason or "")


@pytest.mark.asyncio
async def test_detect_capabilities_gateway_404_is_unavailable(httpx_mock):
    """错误健康检查路径的 404 不能误判为网关可用。"""
    os.environ["AI_GATEWAY_URL"] = "http://gateway:8080/v1"
    httpx_mock.add_response(url="http://gateway:8080/health", status_code=404)
    httpx_mock.add_exception(httpx.ConnectError("Connection refused"), url="http://localhost:8002/health")
    caps = await detect_capabilities(force=True)
    assert caps.ai_gateway.available is False
    assert "404" in (caps.ai_gateway.reason or "")


@pytest.mark.asyncio
async def test_detect_capabilities_cache(httpx_mock):
    """第二次调用应使用缓存，除非 force=True。"""
    _mock_default_higress_unavailable(httpx_mock)
    httpx_mock.add_response(url="http://localhost:8002/health", status_code=200)
    first = await detect_capabilities(force=True)
    assert first.mineru.available is True

    # 不添加新的 mock，如果缓存生效不会触发新的 HTTP 请求
    second = await detect_capabilities()
    assert second.mineru.available is True

    # force=True 会重新探测
    _mock_default_higress_unavailable(httpx_mock)
    httpx_mock.add_response(url="http://localhost:8002/health", status_code=503)
    third = await detect_capabilities(force=True)
    assert third.mineru.available is False


@pytest.mark.asyncio
async def test_capability_status_to_dict():
    """CapabilityStatus.to_dict 输出正确。"""
    status = CapabilityStatus(available=False, reason="timeout")
    assert status.to_dict() == {"available": False, "reason": "timeout"}


@pytest.mark.asyncio
async def test_capabilities_to_dict():
    """Capabilities.to_dict 输出正确。"""
    caps = Capabilities(
        ai_gateway=CapabilityStatus(available=True),
        milvus=CapabilityStatus(available=False, reason="refused"),
        mineru=CapabilityStatus(available=True),
    )
    assert caps.to_dict() == {
        "ai_gateway": {"available": True, "reason": None},
        "milvus": {"available": False, "reason": "refused"},
        "mineru": {"available": True, "reason": None},
    }


@pytest.mark.asyncio
async def test_detect_capabilities_custom_urls(httpx_mock):
    """显式传入 URL 应覆盖环境变量和默认值。"""
    _mock_default_higress_unavailable(httpx_mock)
    httpx_mock.add_response(url="http://custom-mineru:9000/health", status_code=200)
    caps = await detect_capabilities(
        force=True,
        mineru_url="http://custom-mineru:9000",
    )
    assert caps.mineru.available is True
    assert caps.ai_gateway.available is False


@pytest.mark.asyncio
async def test_detect_capabilities_auto_gateway(httpx_mock):
    """未配置 AI_GATEWAY_URL 时，自动探测默认 Docker 内网 Higress。"""
    httpx_mock.add_response(url=_DEFAULT_HIGRESS_HEALTH, status_code=200)
    httpx_mock.add_exception(httpx.ConnectError("Connection refused"), url="http://localhost:8002/health")
    caps = await detect_capabilities(force=True)
    assert caps.ai_gateway.available is True
