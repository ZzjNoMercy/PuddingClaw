"""Adversarial public-network tests for FetchURLTool."""

from __future__ import annotations

import socket
import ssl

import pytest

from tools.fetch_url_tool import (
    FetchURLTool,
    UnsafePublicURL,
    _configured_https_proxy_url,
    _FetchedResponse,
    _invalidate_https_proxy_cache,
    _parse_macos_https_proxy,
    _resolve_public_addresses,
    _validated_url,
)


def test_fetch_url_returns_complete_json_for_filesystem_middleware(monkeypatch) -> None:
    payload = '{"items":[' + ('{"title":"完整结果"},' * 1000) + "{}]}"
    monkeypatch.setattr(
        FetchURLTool,
        "_request_once",
        staticmethod(
            lambda _url: _FetchedResponse(
                status=200,
                headers={"content-type": "application/json; charset=utf-8"},
                body=payload.encode(),
            )
        ),
    )

    result = FetchURLTool()._run("https://example.com/api")

    assert result == payload
    assert "...[truncated]" not in result


def test_fetch_url_returns_complete_html_markdown(monkeypatch) -> None:
    body = "<html><body><p>" + ("完整正文" * 2000) + "</p></body></html>"
    monkeypatch.setattr(
        FetchURLTool,
        "_request_once",
        staticmethod(
            lambda _url: _FetchedResponse(
                status=200,
                headers={"content-type": "text/html; charset=utf-8"},
                body=body.encode(),
            )
        ),
    )

    result = FetchURLTool()._run("https://example.com/page")

    assert result.count("完整正文") == 2000
    assert "...[truncated]" not in result


@pytest.mark.parametrize(
    "address",
    [
        "127.0.0.1",
        "10.0.0.1",
        "169.254.169.254",
        "::1",
        "::ffff:127.0.0.1",
        "64:ff9b::7f00:1",
        "64:ff9b::a9fe:a9fe",
    ],
)
def test_dns_resolution_rejects_any_non_public_address(monkeypatch, address) -> None:
    family = socket.AF_INET6 if ":" in address else socket.AF_INET
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *_args, **_kwargs: [(family, socket.SOCK_STREAM, 6, "", (address, 443))],
    )

    with pytest.raises(UnsafePublicURL, match="non-public"):
        _resolve_public_addresses("attacker.example", 443)


def test_mixed_public_and_private_dns_fails_closed(monkeypatch) -> None:
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *_args, **_kwargs: [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443)),
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 443)),
        ],
    )

    with pytest.raises(UnsafePublicURL, match="non-public"):
        _resolve_public_addresses("rebind.example", 443)


def test_https_hostname_accepts_standard_fake_ip_but_http_does_not(monkeypatch) -> None:
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *_args, **_kwargs: [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("198.18.0.118", 443)),
        ],
    )

    assert _resolve_public_addresses("public.example", 443, scheme="https") == ["198.18.0.118"]
    with pytest.raises(UnsafePublicURL, match="non-public"):
        _resolve_public_addresses("public.example", 80, scheme="http")


def test_macos_https_proxy_parser_requires_enabled_valid_proxy() -> None:
    output = """
    <dictionary> {
      HTTPEnable : 1
      HTTPSEnable : 1
      HTTPSPort : 27890
      HTTPSProxy : 127.0.0.1
    }
    """

    assert _parse_macos_https_proxy(output) == "http://127.0.0.1:27890"
    assert _parse_macos_https_proxy(output.replace("HTTPSEnable : 1", "HTTPSEnable : 0")) == ""
    assert _parse_macos_https_proxy(output.replace("HTTPSPort : 27890", "HTTPSPort : invalid")) == ""


def test_macos_proxy_discovery_cache_expires_when_vpn_is_enabled(monkeypatch) -> None:
    from tools import fetch_url_tool

    for key in fetch_url_tool._PROXY_ENV_KEYS:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(fetch_url_tool.sys, "platform", "darwin")
    discoveries = iter(["", "http://127.0.0.1:27890"])
    monkeypatch.setattr(
        fetch_url_tool,
        "_discover_macos_https_proxy_url",
        lambda: next(discoveries),
    )
    monkeypatch.setattr(fetch_url_tool, "_proxy_url_is_available", lambda _url: True)
    times = iter([100.0, 101.0, 106.0])
    monkeypatch.setattr(fetch_url_tool.time, "monotonic", lambda: next(times))
    _invalidate_https_proxy_cache()

    assert _configured_https_proxy_url() == ""
    assert _configured_https_proxy_url() == ""
    assert _configured_https_proxy_url() == "http://127.0.0.1:27890"

    _invalidate_https_proxy_cache()


def test_stale_explicit_local_proxy_falls_back_to_live_system_proxy(monkeypatch) -> None:
    from tools import fetch_url_tool

    for key in fetch_url_tool._PROXY_ENV_KEYS:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("PUDDINGCLAW_HTTPS_PROXY", "http://127.0.0.1:7897")
    monkeypatch.setattr(fetch_url_tool.sys, "platform", "darwin")
    monkeypatch.setattr(
        fetch_url_tool,
        "_cached_macos_https_proxy_url",
        lambda: "http://127.0.0.1:27890",
    )
    checked: list[str] = []

    def available(url: str) -> bool:
        checked.append(url)
        return url.endswith(":27890")

    monkeypatch.setattr(fetch_url_tool, "_proxy_url_is_available", available)

    assert _configured_https_proxy_url() == "http://127.0.0.1:27890"
    assert checked == ["http://127.0.0.1:7897", "http://127.0.0.1:27890"]


def test_fake_ip_https_request_uses_configured_proxy(monkeypatch) -> None:
    class FakeResponse:
        status = 200
        headers = {"content-type": "image/jpeg", "content-length": "300"}

        @staticmethod
        def stream(_chunk_size, *, decode_content):
            assert decode_content is True
            return iter([b"x" * 300])

        @staticmethod
        def release_conn():
            return None

    class FakeProxyManager:
        def __init__(self, proxy_url, **kwargs):
            assert proxy_url == "http://127.0.0.1:27890"
            assert kwargs["retries"] is False

        def urlopen(self, method, url, **kwargs):
            assert method == "GET"
            assert url == "https://cdn.example/image.jpg"
            assert kwargs["headers"]["Host"] == "cdn.example"
            assert kwargs["redirect"] is False
            return FakeResponse()

        @staticmethod
        def clear():
            return None

    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *_args, **_kwargs: [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("198.18.1.63", 443)),
        ],
    )
    monkeypatch.setattr(
        "tools.fetch_url_tool._configured_https_proxy_url",
        lambda **_kwargs: "http://127.0.0.1:27890",
    )
    monkeypatch.setattr("tools.fetch_url_tool.ProxyManager", FakeProxyManager)
    monkeypatch.setattr(
        FetchURLTool,
        "_pool",
        staticmethod(lambda **_kwargs: pytest.fail("Fake-IP request must not use direct IP pool")),
    )

    response = FetchURLTool._request_once("https://cdn.example/image.jpg")

    assert response.status == 200
    assert response.headers["content-type"] == "image/jpeg"
    assert response.body == b"x" * 300


def test_fake_ip_tls_retry_rediscovers_proxy_after_vpn_switch(monkeypatch) -> None:
    class FailingDirectPool:
        @staticmethod
        def urlopen(*_args, **_kwargs):
            raise ssl.SSLError("[SSL: UNEXPECTED_EOF_WHILE_READING]")

        @staticmethod
        def close():
            return None

    proxy_lookups = iter(["", "http://127.0.0.1:27890"])
    direct_attempts = 0
    proxy_attempts = 0

    def direct_pool(**_kwargs):
        nonlocal direct_attempts
        direct_attempts += 1
        return FailingDirectPool()

    def proxy_request(cls, url: str, *, hostname: str, proxy_url: str) -> _FetchedResponse:
        nonlocal proxy_attempts
        proxy_attempts += 1
        assert cls is FetchURLTool
        assert url == "https://wttr.in/Ningbo?format=3"
        assert hostname == "wttr.in"
        assert proxy_url == "http://127.0.0.1:27890"
        return _FetchedResponse(200, {"content-type": "text/plain"}, b"Ningbo: 28 C")

    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *_args, **_kwargs: [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("198.18.0.104", 443)),
        ],
    )
    monkeypatch.setattr(
        "tools.fetch_url_tool._configured_https_proxy_url",
        lambda **_kwargs: next(proxy_lookups),
    )
    monkeypatch.setattr("tools.fetch_url_tool.time.sleep", lambda _delay: None)
    monkeypatch.setattr(FetchURLTool, "_pool", staticmethod(direct_pool))
    monkeypatch.setattr(FetchURLTool, "_request_via_proxy", classmethod(proxy_request))

    response = FetchURLTool._request_once("https://wttr.in/Ningbo?format=3")

    assert response.status == 200
    assert response.body == b"Ningbo: 28 C"
    assert direct_attempts == 1
    assert proxy_attempts == 1


def test_wechat_request_uses_configured_proxy_and_retries_transient_tls_eof(monkeypatch) -> None:
    attempts = 0

    def flaky_proxy_request(cls, url: str, *, hostname: str, proxy_url: str) -> _FetchedResponse:
        nonlocal attempts
        attempts += 1
        assert cls is FetchURLTool
        assert url == "https://mp.weixin.qq.com/s/example"
        assert hostname == "mp.weixin.qq.com"
        assert proxy_url == "http://127.0.0.1:27890"
        if attempts < 3:
            raise ssl.SSLError("[SSL: UNEXPECTED_EOF_WHILE_READING] EOF occurred in violation of protocol")
        return _FetchedResponse(200, {"content-type": "text/html"}, b"wechat article")

    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *_args, **_kwargs: [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("101.32.104.4", 443)),
        ],
    )
    monkeypatch.setattr(
        "tools.fetch_url_tool._configured_https_proxy_url",
        lambda **_kwargs: "http://127.0.0.1:27890",
    )
    monkeypatch.setattr("tools.fetch_url_tool.time.sleep", lambda _delay: None)
    monkeypatch.setattr(FetchURLTool, "_request_via_proxy", classmethod(flaky_proxy_request))
    monkeypatch.setattr(
        FetchURLTool,
        "_pool",
        staticmethod(lambda **_kwargs: pytest.fail("WeChat request should honor the configured HTTPS proxy")),
    )

    response = FetchURLTool._request_once("https://mp.weixin.qq.com/s/example")

    assert attempts == 3
    assert response.status == 200
    assert response.body == b"wechat article"


def test_retryable_tls_failure_switches_to_next_proxy_candidate(monkeypatch) -> None:
    attempts: list[str] = []

    def configured_proxy(*, excluded: frozenset[str] = frozenset()) -> str:
        candidates = ["http://127.0.0.1:7897", "http://127.0.0.1:27890"]
        return next((candidate for candidate in candidates if candidate not in excluded), candidates[0])

    def proxy_request(cls, _url: str, *, hostname: str, proxy_url: str) -> _FetchedResponse:
        assert cls is FetchURLTool
        assert hostname == "mp.weixin.qq.com"
        attempts.append(proxy_url)
        if proxy_url.endswith(":7897"):
            raise ssl.SSLError("[SSL: UNEXPECTED_EOF_WHILE_READING]")
        return _FetchedResponse(200, {"content-type": "text/html"}, b"wechat article")

    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *_args, **_kwargs: [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("101.32.104.4", 443)),
        ],
    )
    monkeypatch.setattr("tools.fetch_url_tool._configured_https_proxy_url", configured_proxy)
    monkeypatch.setattr("tools.fetch_url_tool.time.sleep", lambda _delay: None)
    monkeypatch.setattr(FetchURLTool, "_request_via_proxy", classmethod(proxy_request))

    response = FetchURLTool._request_once("https://mp.weixin.qq.com/s/example")

    assert attempts == ["http://127.0.0.1:7897", "http://127.0.0.1:27890"]
    assert response.body == b"wechat article"


def test_tls_certificate_verification_failure_is_not_retried(monkeypatch) -> None:
    attempts = 0

    def invalid_certificate(cls, _url: str, *, hostname: str, proxy_url: str) -> _FetchedResponse:
        nonlocal attempts
        attempts += 1
        assert cls is FetchURLTool
        assert hostname == "mp.weixin.qq.com"
        assert proxy_url
        raise ssl.SSLCertVerificationError(1, "certificate verify failed")

    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *_args, **_kwargs: [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("101.32.104.4", 443)),
        ],
    )
    monkeypatch.setattr(
        "tools.fetch_url_tool._configured_https_proxy_url",
        lambda **_kwargs: "http://127.0.0.1:27890",
    )
    monkeypatch.setattr(FetchURLTool, "_request_via_proxy", classmethod(invalid_certificate))

    with pytest.raises(ssl.SSLCertVerificationError):
        FetchURLTool._request_once("https://mp.weixin.qq.com/s/example")

    assert attempts == 1


def test_redirect_is_revalidated_before_following(monkeypatch) -> None:
    requested: list[str] = []

    def request(url: str) -> _FetchedResponse:
        requested.append(url)
        if len(requested) == 1:
            return _FetchedResponse(
                status=302,
                headers={"location": "http://127.0.0.1/admin"},
                body=b"",
            )
        raise AssertionError("private redirect must never be requested")

    monkeypatch.setattr(FetchURLTool, "_request_once", staticmethod(request))

    result = FetchURLTool()._run("https://public.example/start")

    assert result.startswith("❌ Fetch blocked:")
    assert requested == ["https://public.example/start"]


@pytest.mark.parametrize(
    "url",
    [
        "ftp://example.com/file",
        "https://user:secret@example.com/",
        "http://example.com:8080/",
        "https://example.com:8443/",
        "https://198.18.0.118/",
        "https://[64:ff9b::7f00:1]/",
        "https://[64:ff9b::a9fe:a9fe]/",
    ],
)
def test_unsafe_url_shapes_are_blocked_without_request(url) -> None:
    with pytest.raises(UnsafePublicURL):
        _validated_url(url)
