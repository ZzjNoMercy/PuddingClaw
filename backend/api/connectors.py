"""Product-level connector catalog and managed authorization actions.

This module is also inside the development server's watched API surface, so
router, connector-projection, and authorization-service changes are picked up
without requiring a manual Backend restart.
"""

from __future__ import annotations

import asyncio
import shlex
from typing import Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from config import load_config
from connectors.kimi_webbridge.lifecycle import KimiWebBridgeLifecycle
from harness.workspace_backends import ProjectSandboxManager
from runtime_identity.adapters import ManagedCliRegistry
from runtime_identity.authorization_drivers import AuthorizationDriverRegistry
from runtime_identity.connectors import ConnectorRegistry
from runtime_identity.paths import PuddingClawPaths
from runtime_identity.service import ManagedCliService

router = APIRouter(prefix="/connectors", tags=["connectors"])
_MANAGED_REGISTRY = ManagedCliRegistry()
_AUTHORIZATION_DRIVERS = AuthorizationDriverRegistry()


class AuthorizeConnectorRequest(BaseModel):
    mode: Literal["user_reauthorize", "full_replace"] = "user_reauthorize"


class RevokeConnectorRequest(BaseModel):
    confirmed: bool = False


def _sandbox_manager() -> ProjectSandboxManager:
    terminal = load_config().get("harness", {}).get("terminal", {})
    # Connector managed-runtime Docker is an internal compatibility path,
    # not a user-selectable Agent execution mode. Availability is checked by
    # the manager when the connector operation actually needs it.
    return ProjectSandboxManager(dict(terminal.get("docker", {}) or {}))


def _connector_registry() -> ConnectorRegistry:
    manager = _sandbox_manager()
    return ConnectorRegistry(
        PuddingClawPaths.from_environment(),
        manager.runtime_contract,
        managed_registry=_MANAGED_REGISTRY,
        authorization_drivers=_AUTHORIZATION_DRIVERS,
        runtime_image_digest=manager.inspect_managed_runtime_image_digest(),
    )


def _managed_service() -> ManagedCliService:
    return ManagedCliService(
        _sandbox_manager(),
        registry=_MANAGED_REGISTRY,
        authorization_drivers=_AUTHORIZATION_DRIVERS,
    )


def _managed_connector(connector_id: str):
    registry = _MANAGED_REGISTRY
    adapter = next(
        (
            item
            for item in registry.adapters()
            if getattr(getattr(item, "connector", None), "connector_id", None) == connector_id
        ),
        None,
    )
    if adapter is None:
        raise HTTPException(status_code=404, detail="连接器不存在。")
    driver = _AUTHORIZATION_DRIVERS.for_adapter(adapter.adapter_id, required=False)
    if driver is None:
        raise HTTPException(status_code=409, detail="该连接器没有可用的授权 Driver。")
    return registry, driver


def _webbridge_lifecycle() -> KimiWebBridgeLifecycle:
    return KimiWebBridgeLifecycle(PuddingClawPaths.from_environment())


def _result_or_error(result) -> dict:
    payload = dict(result.payload)
    if result.exit_code == 0 or payload.get("status") in {
        "awaiting_user_browser",
        "confirmation_required",
    }:
        return payload
    error = str(payload.get("error") or payload.get("status") or "connector_operation_failed")
    status_code = 409 if error in {
        "authorization_flow_missing",
        "authorization_profile_conflict",
        "authorization_prerequisite_failed",
        "credential_profile_incomplete",
    } else 502
    raise HTTPException(status_code=status_code, detail=payload)


@router.get("")
async def list_connectors():
    return {"connectors": await asyncio.to_thread(_connector_registry().list)}


@router.get("/{connector_id}")
async def get_connector(connector_id: str):
    try:
        connector = await asyncio.to_thread(_connector_registry().get, connector_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="连接器不存在。") from exc
    return {"connector": connector}


@router.post("/{connector_id}/authorize")
async def authorize_connector(connector_id: str, request: AuthorizeConnectorRequest):
    registry, driver = _managed_connector(connector_id)
    argv = driver.user_login_argv if request.mode == "user_reauthorize" else driver.app_configuration_argv

    def execute():
        service = _managed_service()
        match = registry.match(shlex.join(argv))
        assert match is not None
        return service.execute(service.plan(match, {}), {})

    return _result_or_error(await asyncio.to_thread(execute))


@router.post("/{connector_id}/resume")
async def resume_connector_authorization(connector_id: str):
    registry, driver = _managed_connector(connector_id)

    def execute():
        service = _managed_service()
        match = registry.match(shlex.join(driver.resume_argv))
        assert match is not None
        return service.execute(service.plan(match, {}), {})

    return _result_or_error(await asyncio.to_thread(execute))


@router.post("/{connector_id}/revoke")
async def revoke_connector(connector_id: str, request: RevokeConnectorRequest):
    registry, driver = _managed_connector(connector_id)
    if not request.confirmed:
        raise HTTPException(status_code=409, detail="断开连接是破坏性操作，需要明确确认。")

    def execute():
        service = _managed_service()
        match = registry.match(shlex.join(driver.revoke_argv))
        assert match is not None
        plan = service.plan(match, {})
        return service.execute(
            plan,
            {"_managed_cli_destructive_approval": plan.destructive_approval_binding()},
        )

    return _result_or_error(await asyncio.to_thread(execute))


@router.post("/kimi-webbridge/install")
async def install_kimi_webbridge():
    """Install/repair only through the vendor binary; never run a remote shell script."""

    lifecycle = _webbridge_lifecycle()
    state = await asyncio.to_thread(lifecycle.probe)
    if state.installed:
        if state.extension_version and not state.version_compatible:
            return {
                "status": "extension_update_required",
                "state": state.as_dict(),
                "extension_update_url": "https://www.kimi.com/zh-cn/features/webbridge",
                "message": "浏览器扩展版本不匹配，请先按官方说明更新 Chrome/Edge 扩展，再重新检测。",
            }
        return {"status": "already_installed", "state": state.as_dict()}
    return {
        "status": "manual_install_required",
        "state": state.as_dict(),
        "required_path": str(lifecycle.daemon_path),
        "message": "请按 Kimi WebBridge 官方安装流程完成本地组件和浏览器扩展安装，然后重新检测。",
    }


@router.post("/kimi-webbridge/enable")
async def enable_kimi_webbridge():
    lifecycle = _webbridge_lifecycle()
    state = await asyncio.to_thread(lifecycle.probe)
    if not state.installed:
        raise HTTPException(status_code=409, detail={"status": "not_installed", "state": state.as_dict()})
    await asyncio.to_thread(lifecycle.set_enabled, True)
    return {"status": "enabled", "state": (await asyncio.to_thread(lifecycle.probe)).as_dict()}


@router.post("/kimi-webbridge/disable")
async def disable_kimi_webbridge():
    lifecycle = _webbridge_lifecycle()
    await asyncio.to_thread(lifecycle.set_enabled, False)
    return {"status": "disabled", "state": (await asyncio.to_thread(lifecycle.probe)).as_dict()}


@router.post("/kimi-webbridge/probe")
async def probe_kimi_webbridge():
    return {"status": "probed", "state": (await asyncio.to_thread(_webbridge_lifecycle().probe)).as_dict()}
