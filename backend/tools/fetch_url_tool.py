"""Public-web fetch Tool with redirect-safe SSRF protection."""

from __future__ import annotations

import ipaddress
import os
import re
import socket
import ssl
import subprocess
import sys
import threading
import time
from collections.abc import Mapping
from dataclasses import dataclass
from urllib.parse import urljoin, urlsplit, urlunsplit

import certifi
import html2text
import urllib3
from charset_normalizer import from_bytes
from langchain_core.tools import BaseTool
from pydantic import BaseModel, Field
from urllib3.connectionpool import HTTPConnectionPool, HTTPSConnectionPool
from urllib3.poolmanager import ProxyManager
from urllib3.util import Timeout

from utils.network_safety import (
    TRUSTED_HTTPS_FAKE_IP_NETWORKS,
    is_public_or_trusted_https_fake_ip,
    normalized_ip,
)

MAX_REDIRECTS = 5
MAX_RESPONSE_BYTES = 5 * 1024 * 1024
READ_CHUNK_BYTES = 64 * 1024
TRANSPORT_ATTEMPTS = 3
TRANSPORT_RETRY_DELAYS = (0.2, 0.6)
PROXY_DISCOVERY_CACHE_TTL_SECONDS = 5.0
_PROXY_ENV_KEYS = (
    "PUDDINGCLAW_HTTPS_PROXY",
    "HTTPS_PROXY",
    "https_proxy",
    "ALL_PROXY",
    "all_proxy",
)
_PROXY_PREFERRED_HOST_SUFFIXES = (
    "mp.weixin.qq.com",
    "mmbiz.qpic.cn",
    "twimg.com",
)
_proxy_discovery_lock = threading.Lock()
_proxy_discovery_value = ""
_proxy_discovery_expires_at = 0.0


class UnsafePublicURL(ValueError):
    """Raised before a request can reach a non-public network target."""


@dataclass(frozen=True)
class _FetchedResponse:
    status: int
    headers: Mapping[str, str]
    body: bytes


class FetchURLInput(BaseModel):
    url: str = Field(description="The public HTTP(S) URL to fetch")


def _public_ip(
    value: str,
    *,
    scheme: str = "",
    hostname: str = "",
) -> ipaddress.IPv4Address | ipaddress.IPv6Address:
    address = normalized_ip(value)
    if not is_public_or_trusted_https_fake_ip(
        str(address),
        scheme=scheme,
        hostname=hostname,
    ):
        raise UnsafePublicURL(f"target resolves to non-public address {address}")
    return address


def _validated_url(url: str) -> tuple[str, str, int, str]:
    try:
        parsed = urlsplit(url.strip())
        scheme = parsed.scheme.lower()
        hostname = parsed.hostname or ""
        port = parsed.port
    except ValueError as exc:
        raise UnsafePublicURL("malformed URL") from exc
    if scheme not in {"http", "https"} or not hostname:
        raise UnsafePublicURL("only public http:// and https:// URLs are allowed")
    if parsed.username or parsed.password:
        raise UnsafePublicURL("URL user information is not allowed")
    expected_port = 80 if scheme == "http" else 443
    if port not in {None, expected_port}:
        raise UnsafePublicURL(f"non-standard {scheme.upper()} port {port} is not allowed")
    try:
        ascii_hostname = hostname.encode("idna").decode("ascii")
    except UnicodeError as exc:
        raise UnsafePublicURL("invalid hostname") from exc
    try:
        literal = ipaddress.ip_address(ascii_hostname)
    except ValueError:
        literal = None
    if literal is not None:
        _public_ip(str(literal))
    path = urlunsplit(("", "", parsed.path or "/", parsed.query, ""))
    return scheme, ascii_hostname, expected_port, path


def _resolve_public_addresses(hostname: str, port: int, *, scheme: str = "https") -> list[str]:
    try:
        records = socket.getaddrinfo(
            hostname,
            port,
            family=socket.AF_UNSPEC,
            type=socket.SOCK_STREAM,
        )
    except socket.gaierror as exc:
        raise UnsafePublicURL(f"hostname could not be resolved: {hostname}") from exc
    addresses: list[str] = []
    for record in records:
        raw = str(record[4][0])
        address = str(_public_ip(raw, scheme=scheme, hostname=hostname))
        if address not in addresses:
            addresses.append(address)
    if not addresses:
        raise UnsafePublicURL(f"hostname has no public address: {hostname}")
    return addresses


def _uses_trusted_https_fake_ip(addresses: list[str], *, scheme: str) -> bool:
    if scheme != "https" or not addresses:
        return False
    return all(
        any(normalized_ip(address) in network for network in TRUSTED_HTTPS_FAKE_IP_NETWORKS)
        for address in addresses
    )


def _prefers_configured_proxy(hostname: str, *, scheme: str) -> bool:
    """Route known proxy-sensitive public CDNs through the user's HTTPS proxy."""

    if scheme != "https":
        return False
    normalized = hostname.rstrip(".").lower()
    return any(
        normalized == suffix or normalized.endswith(f".{suffix}")
        for suffix in _PROXY_PREFERRED_HOST_SUFFIXES
    )


def _is_retryable_transport_error(exc: Exception) -> bool:
    """Classify transient connection failures without weakening TLS validation."""

    current: BaseException | None = exc
    seen: set[int] = set()
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        message = str(current).lower()
        if isinstance(current, ssl.SSLCertVerificationError) or "certificate verify failed" in message:
            return False
        if isinstance(
            current,
            (
                ssl.SSLError,
                TimeoutError,
                ConnectionError,
                OSError,
                urllib3.exceptions.HTTPError,
            ),
        ):
            return True
        current = current.__cause__ or current.__context__
    return False


def _friendly_transport_error(exc: Exception) -> ConnectionError:
    message = str(exc)
    if "unexpected_eof" in message.lower() or "eof occurred in violation of protocol" in message.lower():
        return ConnectionError(
            f"TLS 连接被代理或远端服务器提前关闭，已自动重试 {TRANSPORT_ATTEMPTS} 次；请稍后重新解析"
        )
    return ConnectionError(
        f"网络连接失败，已自动重试 {TRANSPORT_ATTEMPTS} 次：{type(exc).__name__}: {message}"
    )


def _normalized_proxy_url(value: str) -> str:
    candidate = value.strip()
    if not candidate:
        return ""
    try:
        parsed = urlsplit(candidate)
        if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
            return ""
        if parsed.path not in {"", "/"} or parsed.query or parsed.fragment:
            return ""
        _ = parsed.port
    except ValueError:
        return ""
    return candidate


def _parse_macos_https_proxy(output: str) -> str:
    values = {
        match.group("key"): match.group("value").strip()
        for match in re.finditer(
            r"^\s*(?P<key>HTTPSEnable|HTTPSProxy|HTTPSPort)\s*:\s*(?P<value>.+?)\s*$",
            output,
            flags=re.MULTILINE,
        )
    }
    if values.get("HTTPSEnable") != "1":
        return ""
    host = values.get("HTTPSProxy", "").strip()
    try:
        port = int(values.get("HTTPSPort", "0"))
    except ValueError:
        return ""
    if not host or not 1 <= port <= 65535:
        return ""
    return _normalized_proxy_url(f"http://{host}:{port}")


def _configured_https_proxy_url() -> str:
    """Resolve the active HTTPS proxy while allowing VPN hot switching.

    Environment overrides are checked on every call. macOS system-proxy
    discovery is cached only briefly because VPN clients can enable, disable or
    replace the local proxy while the long-lived backend process keeps running.
    """

    for key in _PROXY_ENV_KEYS:
        proxy_url = _normalized_proxy_url(os.getenv(key, ""))
        if proxy_url:
            return proxy_url
    if sys.platform != "darwin":
        return ""

    global _proxy_discovery_expires_at, _proxy_discovery_value
    now = time.monotonic()
    with _proxy_discovery_lock:
        if now < _proxy_discovery_expires_at:
            return _proxy_discovery_value
        proxy_url = _discover_macos_https_proxy_url()
        _proxy_discovery_value = proxy_url
        _proxy_discovery_expires_at = now + PROXY_DISCOVERY_CACHE_TTL_SECONDS
        return proxy_url


def _discover_macos_https_proxy_url() -> str:
    """Read the current macOS HTTPS proxy without applying process-lifetime caching."""

    try:
        completed = subprocess.run(
            ["/usr/sbin/scutil", "--proxy"],
            check=False,
            capture_output=True,
            text=True,
            timeout=2,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    if completed.returncode != 0:
        return ""
    return _parse_macos_https_proxy(completed.stdout)


def _invalidate_https_proxy_cache() -> None:
    """Force the next transport attempt to rediscover the macOS proxy."""

    global _proxy_discovery_expires_at, _proxy_discovery_value
    with _proxy_discovery_lock:
        _proxy_discovery_value = ""
        _proxy_discovery_expires_at = 0.0


class FetchURLTool(BaseTool):
    name: str = "fetch_url"
    description: str = (
        "Fetch one public web page and return cleaned Markdown or JSON. "
        "Only HTTP(S) public-network URLs on their standard ports are accepted."
    )
    args_schema: type[BaseModel] = FetchURLInput
    risk_level: str = "safe"

    @staticmethod
    def _headers(hostname: str) -> dict[str, str]:
        return {
            "Host": hostname,
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36"
            ),
            "Accept": "text/html,application/json,text/plain,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.1",
            "Accept-Encoding": "gzip, deflate",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            "Connection": "close",
        }

    @staticmethod
    def _read_response(response: urllib3.HTTPResponse) -> _FetchedResponse:
        content_length = response.headers.get("content-length")
        if content_length and int(content_length) > MAX_RESPONSE_BYTES:
            raise ValueError("response exceeds the 5 MiB safety limit")
        body = bytearray()
        for chunk in response.stream(READ_CHUNK_BYTES, decode_content=True):
            body.extend(chunk)
            if len(body) > MAX_RESPONSE_BYTES:
                raise ValueError("response exceeds the 5 MiB safety limit")
        return _FetchedResponse(
            status=int(response.status),
            headers={str(key).lower(): str(value) for key, value in response.headers.items()},
            body=bytes(body),
        )

    @classmethod
    def _request_via_proxy(cls, url: str, *, hostname: str, proxy_url: str) -> _FetchedResponse:
        manager = ProxyManager(
            proxy_url,
            cert_reqs=ssl.CERT_REQUIRED,
            ca_certs=certifi.where(),
            timeout=Timeout(connect=5, read=15),
            retries=False,
        )
        response: urllib3.HTTPResponse | None = None
        try:
            response = manager.urlopen(
                "GET",
                url,
                headers=cls._headers(hostname),
                redirect=False,
                preload_content=False,
                decode_content=False,
            )
            return cls._read_response(response)
        finally:
            if response is not None:
                response.release_conn()
            manager.clear()

    @staticmethod
    def _pool(
        *,
        scheme: str,
        address: str,
        port: int,
        hostname: str,
    ) -> HTTPConnectionPool | HTTPSConnectionPool:
        common = {
            "host": address,
            "port": port,
            "maxsize": 1,
            "block": True,
            "timeout": Timeout(connect=5, read=15),
            "retries": False,
        }
        if scheme == "https":
            return HTTPSConnectionPool(
                **common,
                cert_reqs=ssl.CERT_REQUIRED,
                ca_certs=certifi.where(),
                assert_hostname=hostname,
                server_hostname=hostname,
            )
        return HTTPConnectionPool(**common)

    @classmethod
    def _request_once(cls, url: str) -> _FetchedResponse:
        scheme, hostname, port, path = _validated_url(url)
        last_error: Exception | None = None
        for attempt in range(TRANSPORT_ATTEMPTS):
            # DNS and proxy state may both change when a VPN is toggled. Resolve
            # them again for each bounded retry instead of pinning the first
            # observation for the lifetime of this request (or backend).
            addresses = _resolve_public_addresses(hostname, port, scheme=scheme)
            proxy_url = _configured_https_proxy_url()
            use_proxy = bool(
                proxy_url
                and (
                    _uses_trusted_https_fake_ip(addresses, scheme=scheme)
                    or _prefers_configured_proxy(hostname, scheme=scheme)
                )
            )
            if use_proxy:
                try:
                    return cls._request_via_proxy(url, hostname=hostname, proxy_url=proxy_url)
                except Exception as exc:  # noqa: BLE001 - classify below
                    if not _is_retryable_transport_error(exc):
                        raise
                    last_error = exc
            else:
                for address in addresses:
                    pool = cls._pool(
                        scheme=scheme,
                        address=address,
                        port=port,
                        hostname=hostname,
                    )
                    response: urllib3.HTTPResponse | None = None
                    try:
                        response = pool.urlopen(
                            "GET",
                            path,
                            headers=cls._headers(hostname),
                            redirect=False,
                            preload_content=False,
                            decode_content=False,
                            release_conn=False,
                        )
                        connection = response.connection
                        peer = connection.sock.getpeername()[0] if connection and connection.sock else ""
                        if not peer or str(_public_ip(str(peer), scheme=scheme, hostname=hostname)) != str(
                            _public_ip(address, scheme=scheme, hostname=hostname)
                        ):
                            raise UnsafePublicURL("connected peer does not match the validated address")

                        return cls._read_response(response)
                    except (UnsafePublicURL, ValueError):
                        raise
                    except Exception as exc:  # noqa: BLE001 - try another validated address
                        if not _is_retryable_transport_error(exc):
                            raise
                        last_error = exc
                    finally:
                        if response is not None:
                            response.release_conn()
                        pool.close()
            if attempt < TRANSPORT_ATTEMPTS - 1:
                # A cached empty/stale proxy is a common consequence of
                # enabling or switching a VPN after backend startup.
                _invalidate_https_proxy_cache()
                time.sleep(TRANSPORT_RETRY_DELAYS[attempt])
        if last_error is not None:
            raise _friendly_transport_error(last_error) from last_error
        raise UnsafePublicURL("no validated public address could be reached")

    @staticmethod
    def _decode(body: bytes, content_type: str) -> str:
        charset = ""
        for part in content_type.split(";")[1:]:
            key, separator, value = part.strip().partition("=")
            if separator and key.lower() == "charset":
                charset = value.strip(" \"'")
                break
        if not charset:
            best = from_bytes(body).best()
            charset = str(best.encoding or "utf-8") if best is not None else "utf-8"
        try:
            return body.decode(charset, errors="replace")
        except LookupError:
            return body.decode("utf-8", errors="replace")

    def _run(self, url: str) -> str:
        current = url.strip()
        try:
            for redirect_count in range(MAX_REDIRECTS + 1):
                response = self._request_once(current)
                if response.status in {301, 302, 303, 307, 308}:
                    location = response.headers.get("location", "").strip()
                    if not location:
                        return f"❌ Fetch error: redirect {response.status} has no Location"
                    if redirect_count >= MAX_REDIRECTS:
                        return f"❌ Fetch error: more than {MAX_REDIRECTS} redirects"
                    current = urljoin(current, location)
                    # Validate every redirect before the next request; DNS and
                    # the connected peer are revalidated in _request_once.
                    _validated_url(current)
                    continue
                if not 200 <= response.status < 300:
                    return f"❌ Fetch error: HTTP {response.status}"

                content_type = response.headers.get("content-type", "").lower()
                media_type = content_type.split(";", 1)[0].strip()
                allowed = (
                    not media_type
                    or media_type.startswith("text/")
                    or media_type
                    in {
                        "application/json",
                        "application/xml",
                        "application/xhtml+xml",
                    }
                    or media_type.endswith("+json")
                    or media_type.endswith("+xml")
                )
                if not allowed:
                    return f"❌ Fetch blocked: unsupported content type {media_type}"
                text = self._decode(response.body, content_type)
                if media_type == "application/json" or media_type.endswith("+json"):
                    return text

                converter = html2text.HTML2Text()
                converter.ignore_links = False
                converter.ignore_images = True
                converter.body_width = 0
                return converter.handle(text)
        except UnsafePublicURL as exc:
            return f"❌ Fetch blocked: {exc}"
        except (TimeoutError, urllib3.exceptions.TimeoutError):
            return "❌ Request timed out (15s limit)"
        except ValueError as exc:
            return f"❌ Fetch blocked: {exc}"
        except Exception as exc:  # noqa: BLE001 - Tool errors are model-visible
            return f"❌ Fetch error: {type(exc).__name__}: {exc}"
        return "❌ Fetch error: redirect handling did not produce a response"


def create_fetch_url_tool() -> FetchURLTool:
    return FetchURLTool()
