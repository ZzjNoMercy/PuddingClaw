"""Shared network-address classification for SSRF-safe HTTPS clients."""

from __future__ import annotations

import ipaddress

# RFC 2544 benchmarking space. Clash and sing-box commonly use this reserved,
# non-routable block for synthetic DNS answers in Fake-IP mode. It is only
# trusted for hostname-based HTTPS: callers must still perform normal TLS
# certificate and hostname verification before sending an HTTP request.
TRUSTED_HTTPS_FAKE_IP_NETWORKS = (ipaddress.ip_network("198.18.0.0/15"),)


def normalized_ip(value: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address:
    """Parse an address and collapse IPv4-mapped IPv6 values."""

    address = ipaddress.ip_address(value.split("%", 1)[0])
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped:
        return address.ipv4_mapped
    return address


def is_public_or_trusted_https_fake_ip(
    value: str,
    *,
    scheme: str,
    hostname: str,
) -> bool:
    """Allow public IPs plus the narrow, TLS-protected Fake-IP exception.

    Fake-IP is never accepted for an IP-literal URL or plain HTTP. This keeps
    localhost, RFC1918, link-local and metadata-service targets blocked while
    allowing HTTPS domain requests to traverse a local Clash/sing-box TUN.
    """

    address = normalized_ip(value)
    if address.is_global:
        return True
    if scheme.lower() != "https":
        return False
    try:
        ipaddress.ip_address(hostname)
    except ValueError:
        pass
    else:
        return False
    return any(address in network for network in TRUSTED_HTTPS_FAKE_IP_NETWORKS)
