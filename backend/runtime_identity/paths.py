"""Stable host paths for user-owned PuddingClaw runtime state."""

from __future__ import annotations

import os
import platform
import re
from dataclasses import dataclass
from pathlib import Path

_SAFE_ID = re.compile(r"^[A-Za-z0-9_.-]{1,128}$")


def resolve_puddingclaw_home() -> Path:
    """Resolve the host-side PuddingClaw data root.

    ``PUDDINGCLAW_HOME`` is intentionally interpreted by the Backend host
    process, never by an Agent command or a sandbox container.
    """

    configured = os.environ.get("PUDDINGCLAW_HOME", "").strip()
    candidate = Path(configured).expanduser() if configured else Path.home() / ".puddingclaw"
    if not candidate.is_absolute():
        raise ValueError("PUDDINGCLAW_HOME must be an absolute host path")
    return candidate.resolve()


def trusted_owner_user_id() -> str:
    """Return the Backend-owned credential principal for this deployment.

    PuddingClaw currently runs as a single-user desktop service.  The value is
    therefore deployment configuration, not the caller-controlled ``user_id``
    field present in legacy request bodies.
    """

    return safe_identity_component(
        os.environ.get("PUDDINGCLAW_OWNER_USER_ID", "local").strip() or "local",
        field="owner_user_id",
    )


def safe_identity_component(value: str, *, field: str) -> str:
    normalized = str(value or "").strip()
    if not _SAFE_ID.fullmatch(normalized) or normalized in {".", ".."}:
        raise ValueError(f"{field} contains unsupported characters")
    return normalized


def runtime_arch() -> str:
    value = re.sub(r"[^A-Za-z0-9_.-]+", "-", platform.machine().lower()).strip("-")
    return value or "unknown"


@dataclass(frozen=True)
class PuddingClawPaths:
    """Typed path projection rooted in the host user's PuddingClaw home."""

    root: Path

    @classmethod
    def from_environment(cls) -> PuddingClawPaths:
        return cls(resolve_puddingclaw_home())

    def node_toolchain(self, runtime_contract: str) -> Path:
        contract = re.sub(r"[^A-Za-z0-9_.+-]+", "-", runtime_contract).strip("-")
        if not contract:
            raise ValueError("runtime_contract must be non-empty")
        return self.root / "runtime" / "toolchains" / "node" / f"{contract}-{runtime_arch()}"

    def credentials_root(self, owner_user_id: str) -> Path:
        owner = safe_identity_component(owner_user_id, field="owner_user_id")
        return self.root / "users" / owner / "credentials"

    def provider_profile(self, owner_user_id: str, provider: str, profile_id: str) -> Path:
        provider_name = safe_identity_component(provider, field="provider")
        profile = safe_identity_component(profile_id, field="profile_id")
        return self.credentials_root(owner_user_id) / provider_name / profile
