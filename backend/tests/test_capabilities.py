"""capabilities 模块单元测试。"""

import os
import sys
import warnings
from pathlib import Path
from unittest import mock

import httpx
import pytest

pytestmark = pytest.mark.httpx_mock(assert_all_responses_were_requested=False)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from capabilities import (  # noqa: E402
    Capabilities,
    CapabilityStatus,
    _check_external_datasources,
    _check_http_get,
    _check_http_get_sync,
    _check_pgvector,
    _check_postgres_sync,
    detect_capabilities,
    detect_capabilities_sync,
    invalidate_capabilities,
)


@pytest.fixture(autouse=True)
def _clear_cache_and_env():
    """每个测试前清除缓存和相关环境变量。"""
    invalidate_capabilities()
    for key in (
        "DATABASE_URL",
        "PUDDINGCLAW_DATABASE_URL",
        "PUDDINGCLAW_DATABASE_MODE",
        "PUDDINGCLAW_DATABASE_SOURCE",
        "POSTGRES_URL",
        "MILVUS_URL",
        "MINERU_URL",
    ):
        os.environ.pop(key, None)
    yield
    invalidate_capabilities()


@pytest.fixture(autouse=True)
def _mock_postgres_unavailable():
    """默认将 PostgreSQL 探测 mock 为不可用，避免测试机本地数据库干扰。"""
    with mock.patch("capabilities._check_postgres") as mock_check:
        mock_check.return_value = CapabilityStatus(available=False, reason="mocked unavailable")
        yield


@pytest.fixture(autouse=True)
def _mock_pgvector_unavailable():
    """默认将 pgvector 探测 mock 为不可用，避免连接测试机数据库。"""
    with mock.patch("capabilities._check_pgvector") as mock_check:
        mock_check.return_value = CapabilityStatus(available=False, reason="mocked unavailable")
        yield


@pytest.fixture(autouse=True)
def _mock_milvus_unavailable():
    """默认将 Milvus 探测 mock 为不可用，避免测试机本地 Milvus 干扰。"""
    with mock.patch("capabilities._check_milvus") as mock_check:
        mock_check.return_value = CapabilityStatus(available=False, reason="mocked unavailable")
        yield


@pytest.fixture(autouse=True)
def _mock_docker_unavailable():
    """默认将 Docker 探测 mock 为不可用，避免依赖测试机 daemon 状态。"""
    with mock.patch("capabilities._check_docker") as mock_check:
        mock_check.return_value = CapabilityStatus(available=False, reason="mocked unavailable")
        yield


@pytest.mark.asyncio
async def test_detect_capabilities_no_services(httpx_mock):
    """无服务配置时，所有能力应为不可用。"""
    httpx_mock.add_exception(httpx.ConnectError("Connection refused"), url="http://localhost:8002/health")
    caps = await detect_capabilities(force=True)
    assert isinstance(caps, Capabilities)
    assert caps.core_database.available is False
    assert caps.pgvector.available is False
    assert caps.docker.available is False
    assert caps.milvus.available is False
    assert caps.mineru.available is False


@pytest.mark.asyncio
async def test_harness_profile_does_not_probe_knowledge_infrastructure(monkeypatch):
    monkeypatch.setenv("PUDDINGCLAW_EXTENSION_KNOWLEDGE", "0")
    with (
        mock.patch("capabilities._check_pgvector") as pgvector,
        mock.patch("capabilities._check_milvus") as milvus,
        mock.patch("capabilities._check_http_get") as mineru,
    ):
        caps = await detect_capabilities(force=True)

    pgvector.assert_not_awaited()
    milvus.assert_not_awaited()
    mineru.assert_not_awaited()
    assert caps.pgvector.reason == "pgvector is disabled by the current Runtime Profile"
    assert caps.milvus.reason == "Milvus is disabled by the current Runtime Profile"
    assert caps.mineru.reason == "MinerU is disabled by the current Runtime Profile"


@pytest.mark.asyncio
async def test_detect_capabilities_mineru_available(httpx_mock):
    """MinerU /health 返回 200 时标记为可用。"""
    httpx_mock.add_response(url="http://localhost:8002/health", status_code=200)
    caps = await detect_capabilities(force=True)
    assert caps.mineru == CapabilityStatus(available=True)


@pytest.mark.asyncio
@pytest.mark.asyncio
async def test_detect_capabilities_cache(httpx_mock):
    """第二次调用应使用缓存，除非 force=True。"""
    httpx_mock.add_response(url="http://localhost:8002/health", status_code=200)
    first = await detect_capabilities(force=True)
    assert first.mineru.available is True

    # 不添加新的 mock，如果缓存生效不会触发新的 HTTP 请求
    second = await detect_capabilities()
    assert second.mineru.available is True

    # force=True 会重新探测
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
        core_database=CapabilityStatus(available=True, details={"mode": "sqlite", "scope": "core"}),
        pgvector=CapabilityStatus(available=True, reason="pgvector 0.8.5", details={"scope": "gbrain"}),
        docker=CapabilityStatus(available=True),
        milvus=CapabilityStatus(available=False, reason="refused"),
        mineru=CapabilityStatus(available=True),
        external_datasources=CapabilityStatus(available=True, details={"scope": "datasource"}),
    )
    assert caps.to_dict() == {
        "core_database": {"available": True, "reason": None, "details": {"mode": "sqlite", "scope": "core"}},
        "pgvector": {"available": True, "reason": "pgvector 0.8.5", "details": {"scope": "gbrain"}},
        "external_datasources": {"available": True, "reason": None, "details": {"scope": "datasource"}},
        "docker": {"available": True, "reason": None},
        "milvus": {"available": False, "reason": "refused"},
        "mineru": {"available": True, "reason": None},
        # Deprecated alias of core_database, kept for one version.
        "database": {"available": True, "reason": None, "details": {"mode": "sqlite", "scope": "core"}},
    }


@pytest.mark.asyncio
async def test_detect_capabilities_custom_urls(httpx_mock):
    """显式传入 URL 应覆盖环境变量和默认值。"""
    httpx_mock.add_response(url="http://custom-mineru:9000/health", status_code=200)
    caps = await detect_capabilities(
        force=True,
        mineru_url="http://custom-mineru:9000",
    )
    assert caps.mineru.available is True


@pytest.mark.asyncio
@pytest.mark.asyncio
async def test_detect_capabilities_sync_inside_event_loop_does_not_leak_coroutine_warning():
    """同步包装在 async 环境里应直接走同步探测，不创建未 await 的 coroutine。"""

    with (
        mock.patch(
            "capabilities._check_postgres_sync",
            return_value=CapabilityStatus(available=False, reason="mock postgres"),
        ),
        mock.patch(
            "capabilities._check_milvus_sync",
            return_value=CapabilityStatus(available=False, reason="mock milvus"),
        ),
        mock.patch(
            "capabilities._check_docker_sync",
            return_value=CapabilityStatus(available=False, reason="mock docker"),
        ),
        mock.patch(
            "capabilities._check_http_get_sync",
            return_value=CapabilityStatus(available=False, reason="mock mineru"),
        ),
        warnings.catch_warnings(record=True) as caught,
    ):
        warnings.simplefilter("always")
        caps = detect_capabilities_sync(force=True)

    assert caps.core_database.reason == "mock postgres"
    assert caps.docker.reason == "mock docker"
    assert not [
        warning for warning in caught
        if "coroutine 'detect_capabilities' was never awaited" in str(warning.message)
    ]


@pytest.mark.asyncio
async def test_http_health_check_ignores_environment_proxy():
    """本机/内网健康检查不应被 HTTP_PROXY 等环境代理劫持。"""

    with mock.patch("capabilities.httpx.AsyncClient") as mock_client:
        mock_response = mock.Mock(status_code=200)
        mock_client.return_value.__aenter__.return_value.get.return_value = mock_response

        status = await _check_http_get("http://localhost:8080", "/health")

    assert status == CapabilityStatus(available=True)
    mock_client.assert_called_once_with(timeout=3.0, trust_env=False)


def test_core_database_sqlite_reports_core_scope(monkeypatch):
    """SQLite 模式下 core_database 可用且 scope=core。"""
    monkeypatch.setenv("PUDDINGCLAW_DATABASE_MODE", "sqlite")
    status = _check_postgres_sync(None)
    assert status.available is True
    assert status.details == {"mode": "sqlite", "scope": "core"}


def test_external_datasources_reports_scope():
    """外部数据源能力条目标注 scope=datasource。"""
    status = _check_external_datasources()
    assert status.details is not None
    assert status.details["scope"] == "datasource"


def test_external_datasources_missing_asyncpg_degrades_gracefully():
    """asyncpg 未安装时返回降级状态与安装提示，而不是抛错。"""
    with mock.patch.dict(sys.modules, {"asyncpg": None}):
        status = _check_external_datasources()
    assert status.available is False
    assert "pip install puddingclaw-backend[postgres]" in (status.reason or "")
    assert status.details is not None
    assert status.details["driver"] == "missing"
    assert status.details["scope"] == "datasource"


def test_sync_http_health_check_ignores_environment_proxy():
    """同步健康检查同样不能走环境代理。"""

    with mock.patch("capabilities.httpx.get", return_value=mock.Mock(status_code=200)) as mock_get:
        status = _check_http_get_sync("http://localhost:8080", "/health")

    assert status == CapabilityStatus(available=True)
    mock_get.assert_called_once_with(
        "http://localhost:8080/health",
        timeout=3.0,
        trust_env=False,
    )


@pytest.mark.asyncio
async def test_pgvector_without_gbrain_config_reports_optional_capability(tmp_path, monkeypatch):
    """gbrain 未配置时 pgvector 探测返回明确的可选能力状态，不回退 Core URL。"""
    monkeypatch.setenv("PUDDINGCLAW_KNOWLEDGE_DIR", str(tmp_path))
    monkeypatch.setenv("PUDDINGCLAW_DATABASE_URL", "postgresql+asyncpg://u:p@127.0.0.1:5432/core")
    status = await _check_pgvector()
    assert status.available is False
    assert status.reason == "gbrain 未配置（可选能力）"
    assert status.details is not None
    assert status.details["scope"] == "gbrain"


@pytest.mark.asyncio
async def test_pgvector_rejects_non_postgres_gbrain_dsn(tmp_path, monkeypatch):
    """gbrain 配置里的非 PostgreSQL DSN 不应被当作可探测目标。"""
    gbrain_home = tmp_path / "llm-wiki" / ".puddingclaw" / "gbrain-home" / ".gbrain"
    gbrain_home.mkdir(parents=True)
    (gbrain_home / "config.json").write_text('{"database_url": "sqlite:///x.db"}', encoding="utf-8")
    monkeypatch.setenv("PUDDINGCLAW_KNOWLEDGE_DIR", str(tmp_path))
    status = await _check_pgvector()
    assert status.available is False
    assert status.reason == "gbrain 配置的数据库 URL 不是 PostgreSQL DSN"
    assert status.details is not None
    assert status.details["scope"] == "gbrain"
