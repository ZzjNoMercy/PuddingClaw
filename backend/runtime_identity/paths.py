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

    def shared_node_runtime(self, runtime_contract: str) -> Path:
        """Return the user-global declarative Node runtime root.

        The runtime contract and architecture are part of the physical
        identity because native addons built for one image/architecture must
        never be consumed by another one.
        """

        contract = re.sub(r"[^A-Za-z0-9_.+-]+", "-", runtime_contract).strip("-")
        if not contract:
            raise ValueError("runtime_contract must be non-empty")
        return self.root / "runtime" / "node" / f"{contract}-{runtime_arch()}"

    def python_skill_runtime(
        self,
        runtime_contract: str,
        skill_id: str,
        skill_version: str,
    ) -> Path:
        """Return one Skill-version-isolated Python runtime root."""

        contract = re.sub(r"[^A-Za-z0-9_.+-]+", "-", runtime_contract).strip("-")
        if not contract:
            raise ValueError("runtime_contract must be non-empty")
        skill = safe_identity_component(skill_id, field="skill_id")
        version = safe_identity_component(skill_version, field="skill_version")
        return (
            self.root
            / "runtime"
            / "python"
            / "skills"
            / skill
            / version
            / f"{contract}-{runtime_arch()}"
        )

    def python_environment_runtime(self, runtime_contract: str) -> Path:
        """Return the dependency-hash-addressed Python environment store."""

        contract = re.sub(r"[^A-Za-z0-9_.+-]+", "-", runtime_contract).strip("-")
        if not contract:
            raise ValueError("runtime_contract must be non-empty")
        return self.root / "runtime" / "python" / "environments" / f"{contract}-{runtime_arch()}"

    def python_uv_cache(self) -> Path:
        return self.root / "runtime" / "python" / "uv-cache"

    def credentials_root(self, owner_user_id: str) -> Path:
        owner = safe_identity_component(owner_user_id, field="owner_user_id")
        return self.root / "users" / owner / "credentials"

    def skill_secret_registry(self, owner_user_id: str) -> Path:
        owner = safe_identity_component(owner_user_id, field="owner_user_id")
        return self.root / "users" / owner / "skill-secrets" / "registry.enc"

    def skill_runtime_bindings(self) -> Path:
        return self.root / "runtime" / "skill-runtime-bindings.json"

    def provider_profile(self, owner_user_id: str, provider: str, profile_id: str) -> Path:
        provider_name = safe_identity_component(provider, field="provider")
        profile = safe_identity_component(profile_id, field="profile_id")
        return self.credentials_root(owner_user_id) / provider_name / profile
