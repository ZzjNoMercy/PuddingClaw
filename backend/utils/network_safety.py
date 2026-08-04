"""Shared network-address classification for SSRF-safe HTTPS clients."""

from __future__ import annotations

import ipaddress

# RFC 2544 benchmarking space. Clash and sing-box commonly use this reserved,
# non-routable block for synthetic DNS answers in Fake-IP mode. It is only
# trusted for hostname-based HTTPS: callers must still perform normal TLS
# certificate and hostname verification before sending an HTTP request.
TRUSTED_HTTPS_FAKE_IP_NETWORKS = (ipaddress.ip_network("198.18.0.0/15"),)

# NAT64 prefixes can make an embedded private IPv4 address look like a globally
# routable IPv6 address. Normalize the embedded address before applying the
# public-network policy so targets such as 64:ff9b::7f00:1 cannot disguise
# 127.0.0.1.
NAT64_IPV4_PREFIXES = (
    ipaddress.ip_network("64:ff9b::/96"),
    ipaddress.ip_network("64:ff9b:1::/48"),
)


def normalized_ip(value: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address:
    """Parse an address and collapse IPv4-mapped or NAT64 IPv6 values."""

    address = ipaddress.ip_address(value.split("%", 1)[0])
    if isinstance(address, ipaddress.IPv6Address):
        if address.ipv4_mapped:
            return address.ipv4_mapped
        if any(address in prefix for prefix in NAT64_IPV4_PREFIXES):
            return ipaddress.IPv4Address(address.packed[-4:])
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
