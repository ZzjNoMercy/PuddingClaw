"""Adversarial public-network tests for FetchURLTool."""

from __future__ import annotations

import socket

import pytest

from tools.fetch_url_tool import (
    FetchURLTool,
    UnsafePublicURL,
    _FetchedResponse,
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
        lambda: "http://127.0.0.1:27890",
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
