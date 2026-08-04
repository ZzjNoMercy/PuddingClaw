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


@pytest.mark.parametrize(
    ("address", "embedded_ipv4"),
    [
        ("64:ff9b::7f00:1", "127.0.0.1"),
        ("64:ff9b::a9fe:a9fe", "169.254.169.254"),
        ("64:ff9b::5db8:d822", "93.184.216.34"),
        ("64:ff9b:1::a00:8", "10.0.0.8"),
    ],
)
def test_nat64_ipv6_is_normalized_to_embedded_ipv4(address: str, embedded_ipv4: str) -> None:
    assert str(normalized_ip(address)) == embedded_ipv4


@pytest.mark.parametrize("address", ["64:ff9b::7f00:1", "64:ff9b::a9fe:a9fe", "64:ff9b:1::a00:8"])
def test_nat64_private_and_metadata_destinations_stay_blocked(address: str) -> None:
    assert not is_public_or_trusted_https_fake_ip(
        address,
        scheme="https",
        hostname="public.example",
    )
