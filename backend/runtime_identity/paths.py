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
    resolved = candidate.resolve(strict=False)
    if resolved.exists() and not resolved.is_dir():
        raise ValueError("PUDDINGCLAW_HOME must point to a directory, not a file")
    return resolved


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

    def ensure_root(self) -> Path:
        """Create and validate the user-owned root without touching package files."""

        self.root.mkdir(parents=True, exist_ok=True)
        if not self.root.is_dir():
            raise NotADirectoryError(f"PuddingClaw home is not a directory: {self.root}")
        return self.root

    def sessions(self) -> Path:
        return self.root / "sessions"

    def session_traces(self) -> Path:
        return self.sessions() / "traces"

    def session_archive(self) -> Path:
        return self.sessions() / "archive"

    def user_skills(self) -> Path:
        return self.root / "skills"

    def config(self) -> Path:
        return self.root / "config"

    def provider_registry(self) -> Path:
        return self.config() / "providers.json"

    def mcp_registry(self) -> Path:
        return self.config() / "mcp.json"

    def web_search_registry(self) -> Path:
        return self.config() / "web-search.json"

    def evaluation_settings(self) -> Path:
        return self.config() / "evaluation.json"

    def profile(self) -> Path:
        return self.root / "profile"

    def memory(self) -> Path:
        return self.root / "memory"

    def projects(self) -> Path:
        return self.root / "projects"

    def project_registry(self) -> Path:
        return self.projects() / "registry.json"

    def user_definitions(self) -> Path:
        return self.root / "definitions"

    def knowledge(self) -> Path:
        return self.root / "knowledge"

    def databases(self) -> Path:
        return self.root / "db"

    def data(self) -> Path:
        return self.root / "data"

    def usage(self) -> Path:
        return self.data() / "usage"

    def query_results(self) -> Path:
        return self.data() / "query-results"

    def agent_workspaces(self) -> Path:
        return self.data() / "agent-workspaces"

    def state(self) -> Path:
        return self.root / "state"

    def knowledge_index(self) -> Path:
        return self.state() / "knowledge_index"

    def knowledge_search(self) -> Path:
        return self.state() / "knowledge-search"

    def memory_index(self) -> Path:
        return self.state() / "memory_index"

    def skill_management(self) -> Path:
        return self.data() / "skill-management"

    def skill_evals(self) -> Path:
        return self.data() / "skill-evals"

    def cache(self) -> Path:
        return self.root / "cache"

    def logs(self) -> Path:
        return self.root / "logs"

    def temporary(self) -> Path:
        return self.root / "tmp"

    def owner_access(self, owner_user_id: str) -> Path:
        owner = safe_identity_component(owner_user_id, field="owner_user_id")
        return self.root / "users" / owner / "access"

    def infrastructure(self) -> Path:
        return self.root / "infrastructure"

    def migrations(self) -> Path:
        return self.root / "migrations"

    def credentials_root(self, owner_user_id: str) -> Path:
        owner = safe_identity_component(owner_user_id, field="owner_user_id")
        return self.root / "users" / owner / "credentials"

    def integration_root(self, owner_user_id: str, integration: str) -> Path:
        """Return persistent host state for one user-owned CLI integration.

        Provider-native CLIs own the files below this directory.  PuddingClaw
        supplies an isolated directory per local user/profile but does not
        serialize the provider's state through the Agent workspace.
        """

        owner = safe_identity_component(owner_user_id, field="owner_user_id")
        name = safe_identity_component(integration, field="integration")
        return self.root / "users" / owner / "integrations" / name

    def lark_cli_profile_root(self, owner_user_id: str, profile_id: str) -> Path:
        profile = safe_identity_component(profile_id, field="profile_id")
        return self.integration_root(owner_user_id, "lark-cli") / "profiles" / profile

    def lark_cli_config_dir(self, owner_user_id: str, profile_id: str) -> Path:
        return self.lark_cli_profile_root(owner_user_id, profile_id) / "config"

    def skill_secret_registry(self, owner_user_id: str) -> Path:
        owner = safe_identity_component(owner_user_id, field="owner_user_id")
        return self.root / "users" / owner / "skill-secrets" / "registry.enc"

    def skill_runtime_bindings(self) -> Path:
        return self.root / "runtime" / "skill-runtime-bindings.json"

    def shared_node_runtime(self, runtime_contract: str) -> Path:
        """Return the user-owned shared Node runtime for one contract and architecture."""

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
        """Return one user-owned, Skill-version-isolated Python runtime root."""

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
        """Return the user-owned dependency-hash-addressed Python environment store."""

        contract = re.sub(r"[^A-Za-z0-9_.+-]+", "-", runtime_contract).strip("-")
        if not contract:
            raise ValueError("runtime_contract must be non-empty")
        return self.root / "runtime" / "python" / "environments" / f"{contract}-{runtime_arch()}"

    def python_uv_cache(self) -> Path:
        return self.root / "runtime" / "python" / "uv-cache"

    def provider_profile(self, owner_user_id: str, provider: str, profile_id: str) -> Path:
        provider_name = safe_identity_component(provider, field="provider")
        profile = safe_identity_component(profile_id, field="profile_id")
        return self.credentials_root(owner_user_id) / provider_name / profile

    def ensure_layout(self) -> None:
        """Create only user-state directories; bundled assets remain untouched."""

        self.ensure_root()
        for path in (
            self.sessions(), self.session_traces(), self.session_archive(),
            self.user_skills(), self.config(), self.profile(), self.memory() / "global",
            self.memory() / "projects", self.projects(), self.user_definitions(),
            self.user_definitions() / "semantic-assets", self.user_definitions() / "analytics-models",
            self.user_definitions() / "sql-guardrails", self.knowledge(), self.databases(),
            self.data(), self.usage(), self.query_results(), self.agent_workspaces(),
            self.state(), self.skill_management(), self.skill_evals(), self.cache(),
            self.logs(), self.temporary(), self.infrastructure(), self.migrations(),
        ):
            path.mkdir(parents=True, exist_ok=True)


@dataclass(frozen=True)
class PuddingClawPackagePaths:
    """Read-only assets shipped with the application package."""

    root: Path

    @property
    def bundled_skills(self) -> Path:
        return self.root / "skills"

    @property
    def prompts(self) -> Path:
        return self.root / "prompts"
