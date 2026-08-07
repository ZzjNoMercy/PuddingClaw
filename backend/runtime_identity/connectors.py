"""Product-level connector registry and safe status projections."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from runtime_identity.authorization import AuthorizationFlowStore
from runtime_identity.paths import PuddingClawPaths, trusted_owner_user_id
from runtime_identity.profiles import CredentialProfileStore


@dataclass(frozen=True)
class ConnectorDefinition:
    connector_id: str
    provider: str
    adapter_id: str
    display_name: str
    description: str
    driver_kind: str
    executable: str
    package: str
    capabilities: tuple[str, ...]


LARK_CONNECTOR = ConnectorDefinition(
    connector_id="lark",
    provider="lark",
    adapter_id="lark-cli",
    display_name="飞书",
    description="消息、文档、云盘、日历、多维表格等飞书能力",
    driver_kind="managed_cli",
    executable="lark-cli",
    package="@larksuite/cli",
    capabilities=("消息", "文档", "云盘", "日历", "多维表格", "审批", "任务", "知识库"),
)


class ConnectorRegistry:
    """Expose provider-neutral connector metadata without secret state."""

    def __init__(
        self,
        paths: PuddingClawPaths,
        runtime_contract: str,
        *,
        owner_user_id: str | None = None,
    ) -> None:
        self.paths = paths
        self.runtime_contract = str(runtime_contract).strip()
        if not self.runtime_contract:
            raise ValueError("connector registry requires a runtime contract")
        self.owner_user_id = owner_user_id or trusted_owner_user_id()
        self.definitions = {LARK_CONNECTOR.connector_id: LARK_CONNECTOR}

    def list(self) -> list[dict[str, Any]]:
        return [self.get(connector_id) for connector_id in self.definitions]

    def get(self, connector_id: str) -> dict[str, Any]:
        definition = self.definitions.get(str(connector_id))
        if definition is None:
            raise KeyError("connector does not exist")
        environment = self._lark_environment(definition)
        profile_store = CredentialProfileStore(self.paths, self.owner_user_id)
        profile = profile_store.resolve(definition.provider, create_default=False)
        active_flow: dict[str, Any] | None = None
        if profile is not None:
            flow_store = AuthorizationFlowStore(
                self.paths,
                self.owner_user_id,
                vault=profile_store.vault,
            )
            profile_id = str(profile["profile_id"])
            with profile_store.profile_lock(definition.provider, profile_id):
                profile = profile_store.resolve(
                    definition.provider,
                    explicit_profile_id=profile_id,
                    create_default=False,
                )
                active = flow_store.reconcile_recovery(
                    definition.provider,
                    profile_id,
                    runner_lease_present=bool((profile or {}).get("browser_job_id")),
                )
            if active is not None:
                active_flow = AuthorizationFlowStore.projection(active)
        profile_projection = self._profile_projection(profile)
        status = self._aggregate_status(environment, profile_projection, active_flow)
        return {
            "connector_id": definition.connector_id,
            "provider": definition.provider,
            "adapter_id": definition.adapter_id,
            "display_name": definition.display_name,
            "description": definition.description,
            "driver_kind": definition.driver_kind,
            "environment": environment,
            "profile": profile_projection,
            "active_flow": active_flow,
            "status": status,
            "capabilities": list(definition.capabilities),
            "installed_skill_count": self._lark_skill_count(),
        }

    def _lark_environment(self, definition: ConnectorDefinition) -> dict[str, Any]:
        root = self.paths.node_toolchain(self.runtime_contract)
        current = root / "current"
        executable = current / "bin" / definition.executable
        resolved: Path | None = None
        try:
            resolved = current.resolve(strict=True)
        except FileNotFoundError:
            pass
        available = executable.is_file()
        version: str | None = None
        revision: str | None = resolved.name if resolved is not None else None
        if resolved is not None:
            package_json = resolved / "lib" / "node_modules" / "@larksuite" / "cli" / "package.json"
            try:
                package = json.loads(package_json.read_text(encoding="utf-8"))
                raw_version = package.get("version") if isinstance(package, dict) else None
                if isinstance(raw_version, str) and re.fullmatch(r"[0-9A-Za-z.+_-]+", raw_version):
                    version = raw_version
            except (FileNotFoundError, json.JSONDecodeError, OSError):
                pass
        return {
            "health": "available" if available else "unavailable",
            "runtime": "node",
            "executable": definition.executable,
            "package": definition.package,
            "version": version,
            "availability_scope": "all_projects",
            "toolchain_revision": revision,
        }

    @staticmethod
    def _profile_projection(profile: dict[str, Any] | None) -> dict[str, Any] | None:
        if profile is None:
            return None
        identities = profile.get("identities") if isinstance(profile.get("identities"), dict) else {}

        def identity(name: str) -> dict[str, Any]:
            raw = identities.get(name) if isinstance(identities, dict) else None
            return {
                key: raw.get(key)
                for key in ("status", "reason", "verified", "token_status", "updated_at")
                if isinstance(raw, dict) and raw.get(key) is not None
            }

        return {
            "id": profile.get("profile_id"),
            "label": profile.get("label"),
            "health": profile.get("status"),
            "sharing_policy": profile.get("sharing_policy"),
            "app_identity": identity("bot"),
            "user_identity": identity("user"),
            "last_updated_at": profile.get("updated_at"),
        }

    @staticmethod
    def _aggregate_status(
        environment: dict[str, Any],
        profile: dict[str, Any] | None,
        active_flow: dict[str, Any] | None,
    ) -> str:
        if environment.get("health") != "available":
            return "environment_unavailable"
        if active_flow is not None:
            return "authorizing"
        if profile is None:
            return "unconfigured"
        health = str(profile.get("health") or "")
        if health == "revoked":
            return "revoked"
        if health in {"repair_required", "expired"}:
            return "repair_required"
        app_status = str((profile.get("app_identity") or {}).get("status") or "")
        user_status = str((profile.get("user_identity") or {}).get("status") or "")
        if app_status in {"ready", "active"} and user_status in {"ready", "active"}:
            return "connected"
        if app_status in {"ready", "active"} or health == "active":
            return "authorization_required"
        return "unconfigured"

    @staticmethod
    def _lark_skill_count() -> int:
        skills_root = Path(__file__).resolve().parents[1] / "skills"
        try:
            return sum(
                1
                for child in skills_root.iterdir()
                if child.is_dir() and child.name.startswith("lark-") and (child / "SKILL.md").is_file()
            )
        except OSError:
            return 0
