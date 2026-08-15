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
SENSITIVE_HOST_WRITE_NAMES = frozenset(
    {
        ".bash_profile",
        ".bashrc",
        ".profile",
        ".zshrc",
        "authorized_keys",
        "crontab",
    }
)
SENSITIVE_HOST_WRITE_DIRECTORIES = frozenset(
    {
        ".ssh",
        ".gnupg",
        ".aws",
        ".azure",
        ".kube",
        ".docker",
        "autostart",
        "launchagents",
        "launchdaemons",
    }
)


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


def is_sensitive_host_write_path(path: str | Path) -> bool:
    """Identify credential or persistence targets that remain effect-gated."""

    candidate = Path(path).expanduser()
    parts = tuple(part.lower() for part in candidate.parts)
    name = candidate.name.lower()
    return (
        any(part in SENSITIVE_HOST_WRITE_DIRECTORIES for part in parts)
        or name in SENSITIVE_HOST_WRITE_NAMES
        or name in SENSITIVE_HOST_READ_NAMES
        or name.startswith(".env.")
        or name.endswith(SENSITIVE_HOST_READ_SUFFIXES)
    )
