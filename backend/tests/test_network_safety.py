from __future__ import annotations

import pytest

from utils.network_safety import is_public_or_trusted_https_fake_ip, normalized_ip


@pytest.mark.parametrize("address", ["127.0.0.1", "10.0.0.8", "169.254.169.254", "::1"])
def test_private_and_metadata_addresses_stay_blocked(address: str) -> None:
    assert not is_public_or_trusted_https_fake_ip(
        address,
        scheme="https",
        hostname="public.example",
    )


def test_standard_fake_ip_requires_https_domain_and_public_ip_is_unchanged() -> None:
    assert is_public_or_trusted_https_fake_ip(
        "198.18.0.118",
        scheme="https",
        hostname="aihot.virxact.com",
    )
    assert not is_public_or_trusted_https_fake_ip(
        "198.18.0.118",
        scheme="http",
        hostname="aihot.virxact.com",
    )
    assert not is_public_or_trusted_https_fake_ip(
        "198.18.0.118",
        scheme="https",
        hostname="198.18.0.118",
    )
    assert is_public_or_trusted_https_fake_ip(
        "93.184.216.34",
        scheme="https",
        hostname="example.com",
    )


def test_ipv4_mapped_ipv6_is_normalized_before_policy() -> None:
    assert str(normalized_ip("::ffff:127.0.0.1")) == "127.0.0.1"
