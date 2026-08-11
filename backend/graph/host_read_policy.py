"""Shared product policy for ordinary versus sensitive host reads."""

from __future__ import annotations

from pathlib import Path

SENSITIVE_HOST_READ_DIRECTORIES = frozenset(
    {".ssh", ".gnupg", ".aws", ".azure", ".kube", ".docker"}
)
SENSITIVE_HOST_READ_NAMES = frozenset(
    {
        ".env",
        ".netrc",
        ".npmrc",
        ".pypirc",
        "credentials",
        "credentials.json",
        "id_dsa",
        "id_ecdsa",
        "id_ed25519",
        "id_rsa",
        "service-account.json",
        "service_account.json",
    }
)
SENSITIVE_HOST_READ_SUFFIXES = (".key", ".p12", ".pem", ".pfx")


def is_sensitive_host_read_path(path: str | Path) -> bool:
    """Keep credential-shaped paths outside Smart's ordinary-read fast path."""

    candidate = Path(path).expanduser()
    parts = tuple(part.lower() for part in candidate.parts)
    name = candidate.name.lower()
    return (
        any(part in SENSITIVE_HOST_READ_DIRECTORIES for part in parts)
        or name in SENSITIVE_HOST_READ_NAMES
        or name.startswith(".env.")
        or name.endswith(SENSITIVE_HOST_READ_SUFFIXES)
    )
