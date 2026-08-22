"""Product-level connector registry and safe status projections."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from runtime_identity.adapters import ManagedCliRegistry
from runtime_identity.authorization import AuthorizationFlowStore
from runtime_identity.authorization_drivers import AuthorizationDriverRegistry
from runtime_identity.host_lark_cli import HostLarkCliRuntime
from runtime_identity.paths import PuddingClawPaths, trusted_owner_user_id
from runtime_identity.profiles import CredentialProfileStore
from runtime_identity.toolchains import ToolchainManager
from tools.skills_scanner import scan_skill_registry


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


KIMI_WEBBRIDGE_CONNECTOR = ConnectorDefinition(
    connector_id="kimi-webbridge",
    provider="kimi-webbridge",
    adapter_id="kimi-webbridge-localhost-v1",
    display_name="Kimi WebBridge",
    description="控制本机浏览器并复用现有登录态；公开资料请优先使用联网搜索",
    driver_kind="managed_local_daemon",
    executable="kimi-webbridge",
    package="Kimi WebBridge",
    capabilities=("标签页", "导航", "页面快照", "点击与填写", "截图/PDF"),
)


class ConnectorRegistry:
    """Expose provider-neutral connector metadata without secret state."""

    def __init__(
        self,
        paths: PuddingClawPaths,
        runtime_contract: str,
        *,
        owner_user_id: str | None = None,
        managed_registry: ManagedCliRegistry | None = None,
        authorization_drivers: AuthorizationDriverRegistry | None = None,
        runtime_image_digest: str | None = None,
    ) -> None:
        self.paths = paths
        self.runtime_contract = str(runtime_contract).strip()
        if not self.runtime_contract:
            raise ValueError("connector registry requires a runtime contract")
        self.owner_user_id = owner_user_id or trusted_owner_user_id()
        self.managed_registry = managed_registry or ManagedCliRegistry()
        self.authorization_drivers = authorization_drivers or AuthorizationDriverRegistry()
        self.runtime_image_digest = runtime_image_digest
        self.toolchains = ToolchainManager(paths, self.runtime_contract)
        self.definitions: dict[str, ConnectorDefinition] = {}
        for adapter in self.managed_registry.adapters():
            metadata = getattr(adapter, "connector", None)
            if metadata is None:
                continue
            if metadata.connector_id == KIMI_WEBBRIDGE_CONNECTOR.connector_id:
                raise ValueError("managed Connector id conflicts with another Connector driver")
            self.definitions[metadata.connector_id] = ConnectorDefinition(
                connector_id=metadata.connector_id,
                provider=adapter.provider,
                adapter_id=adapter.adapter_id,
                display_name=metadata.display_name,
                description=metadata.description,
                driver_kind="managed_cli",
                executable=adapter.toolchain_package.executable,
                package=adapter.toolchain_package.package,
                capabilities=metadata.capabilities,
            )
        self.definitions[KIMI_WEBBRIDGE_CONNECTOR.connector_id] = KIMI_WEBBRIDGE_CONNECTOR

    def list(self) -> list[dict[str, Any]]:
        return [self.get(connector_id) for connector_id in self.definitions]

    def get(self, connector_id: str) -> dict[str, Any]:
        definition = self.definitions.get(str(connector_id))
        if definition is None:
            raise KeyError("connector does not exist")
        if definition.connector_id == KIMI_WEBBRIDGE_CONNECTOR.connector_id:
            return self._webbridge_projection(definition)
        environment = self._managed_environment(definition)
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
        driver = self.authorization_drivers.for_adapter(definition.adapter_id, required=False)
        status = self._aggregate_status(environment, profile_projection, active_flow, driver=driver)
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
            "installed_skill_count": self._installed_skill_count(definition),
            "authorization_supported": self.authorization_drivers.for_adapter(
                definition.adapter_id,
                required=False,
            )
            is not None,
        }

    def _managed_environment(self, definition: ConnectorDefinition) -> dict[str, Any]:
        adapter = self.managed_registry.adapter(definition.adapter_id)
        if definition.adapter_id == "lark-cli":
            resolution = HostLarkCliRuntime(self.paths).resolve()
            return {
                "health": "available" if resolution.available else "unavailable",
                "runtime": "host",
                "executable": (
                    str(resolution.executable) if resolution.executable is not None else definition.executable
                ),
                "package": definition.package,
                "version": resolution.version,
                "availability_scope": "all_projects",
                "toolchain_revision": None,
                "state_model": "provider_native_profile_dirs",
            }
        adapter_fingerprint = self.managed_registry.adapter_contract_fingerprint(adapter.adapter_id)
        driver = self.authorization_drivers.for_adapter(adapter.adapter_id, required=False)
        if driver is not None:
            adapter_fingerprint = hashlib.sha256(
                f"{adapter_fingerprint}\0{driver.contract_fingerprint}".encode()
            ).hexdigest()
        try:
            installed = self.toolchains.inspect_current(
                adapter_id=adapter.adapter_id,
                spec=adapter.toolchain_package,
                adapter_contract_fingerprint=adapter_fingerprint,
                credential_state_fingerprint=adapter.credential_state.fingerprint,
                runtime_image_digest=self.runtime_image_digest,
            )
            health = "available" if installed is not None else "unavailable"
        except (OSError, ValueError):
            installed = None
            health = "repair_required"
        return {
            "health": health,
            "runtime": "node",
            "executable": definition.executable,
            "package": definition.package,
            "version": installed.get("version") if installed is not None else None,
            "availability_scope": "all_projects",
            "toolchain_revision": installed.get("revision") if installed is not None else None,
        }

    def _webbridge_projection(self, definition: ConnectorDefinition) -> dict[str, Any]:
        from connectors.kimi_webbridge.lifecycle import KimiWebBridgeLifecycle

        lifecycle = KimiWebBridgeLifecycle(self.paths)
        state = lifecycle.probe()
        if not state.installed:
            status = "environment_unavailable"
        elif not state.enabled:
            status = "unconfigured"
        elif state.ready:
            status = "connected"
        else:
            status = "repair_required"
        return {
            "connector_id": definition.connector_id,
            "provider": definition.provider,
            "adapter_id": definition.adapter_id,
            "display_name": definition.display_name,
            "description": definition.description,
            "driver_kind": definition.driver_kind,
            "environment": {
                "health": "available" if state.installed else "unavailable",
                "runtime": "host",
                "executable": str(lifecycle.daemon_path),
                "package": definition.package,
                "version": state.version,
                "availability_scope": "local_user",
                "daemon_running": state.daemon_running,
                "extension_connected": state.extension_connected,
                "enabled": state.enabled,
                "ready": state.ready,
                "version_compatible": state.version_compatible,
                "extension_version": state.extension_version,
                "error": state.error,
            },
            "profile": None,
            "active_flow": None,
            "status": status,
            "capabilities": list(definition.capabilities),
            "installed_skill_count": 0,
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

        safe_identities = {
            name: identity(name)
            for name in identities
            if isinstance(name, str) and isinstance(identities.get(name), dict)
        }

        return {
            "id": profile.get("profile_id"),
            "label": profile.get("label"),
            "health": profile.get("status"),
            "sharing_policy": profile.get("sharing_policy"),
            "app_identity": identity("bot"),
            "user_identity": identity("user"),
            "identities": safe_identities,
            "last_updated_at": profile.get("updated_at"),
        }

    def _aggregate_status(
        self,
        environment: dict[str, Any],
        profile: dict[str, Any] | None,
        active_flow: dict[str, Any] | None,
        *,
        driver: Any | None,
    ) -> str:
        if environment.get("health") == "repair_required":
            return "repair_required"
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
        identities = profile.get("identities") if isinstance(profile.get("identities"), dict) else {}
        if driver is not None and driver.durable_profile_ready(identities):
            return "connected"
        app_status = str((profile.get("app_identity") or {}).get("status") or "")
        user_status = str((profile.get("user_identity") or {}).get("status") or "")
        app_identity = profile.get("app_identity") or {}
        user_identity = profile.get("user_identity") or {}
        if driver is None and (
            app_status in {"ready", "active"}
            and app_identity.get("verified") is True
            and user_status in {"ready", "active"}
            and user_identity.get("verified") is True
            and user_identity.get("token_status") == "valid"
        ):
            return "connected"
        if app_status in {"ready", "active"} or health == "active":
            return "authorization_required"
        return "unconfigured"

    def _installed_skill_count(self, definition: ConnectorDefinition) -> int:
        try:
            adapter = self.managed_registry.adapter(definition.adapter_id)
        except ValueError:
            return 0
        metadata = getattr(adapter, "connector", None)
        prefix = str(getattr(metadata, "skill_prefix", "") or "")
        if not prefix:
            return 0
        try:
            return sum(
                1
                for item in scan_skill_registry(
                    Path(__file__).resolve().parents[1],
                    user_root=self.paths.user_skills(),
                )
                if item.get("effective") and str(item["skill_id"]).startswith(prefix)
            )
        except OSError:
            return 0
