"""Product-level connector catalog and managed authorization actions.

This module is also inside the development server's watched API surface, so
router, connector-projection, and authorization-service changes are picked up
without requiring a manual Backend restart.
"""

from __future__ import annotations

import asyncio
from typing import Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from config import load_config
from harness.workspace_backends import ProjectSandboxManager
from runtime_identity.adapters import ManagedCliRegistry
from runtime_identity.connectors import ConnectorRegistry
from runtime_identity.paths import PuddingClawPaths
from runtime_identity.service import ManagedCliService

router = APIRouter(prefix="/connectors", tags=["connectors"])


class AuthorizeConnectorRequest(BaseModel):
    mode: Literal["user_reauthorize", "full_replace"] = "user_reauthorize"


class RevokeConnectorRequest(BaseModel):
    confirmed: bool = False


def _sandbox_manager(*, require_enabled: bool) -> ProjectSandboxManager:
    terminal = load_config().get("harness", {}).get("terminal", {})
    if require_enabled and not bool(terminal.get("docker_enabled", False)):
        raise HTTPException(status_code=503, detail="托管连接器需要启用 Docker runtime。")
    return ProjectSandboxManager(dict(terminal.get("docker", {}) or {}))


def _connector_registry() -> ConnectorRegistry:
    manager = _sandbox_manager(require_enabled=False)
    return ConnectorRegistry(PuddingClawPaths.from_environment(), manager.runtime_contract)


def _managed_service() -> ManagedCliService:
    return ManagedCliService(_sandbox_manager(require_enabled=True))


def _require_lark(connector_id: str) -> None:
    if connector_id != "lark":
        raise HTTPException(status_code=404, detail="连接器不存在。")


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
    _require_lark(connector_id)
    command = (
        "lark-cli auth login --domain all --no-wait --json"
        if request.mode == "user_reauthorize"
        else "lark-cli config init --new"
    )

    def execute():
        service = _managed_service()
        match = ManagedCliRegistry().match(command)
        assert match is not None
        return service.execute(service.plan(match, {}), {})

    return _result_or_error(await asyncio.to_thread(execute))


@router.post("/{connector_id}/resume")
async def resume_connector_authorization(connector_id: str):
    _require_lark(connector_id)

    def execute():
        service = _managed_service()
        match = ManagedCliRegistry().match("lark-cli auth resume")
        assert match is not None
        return service.execute(service.plan(match, {}), {})

    return _result_or_error(await asyncio.to_thread(execute))


@router.post("/{connector_id}/revoke")
async def revoke_connector(connector_id: str, request: RevokeConnectorRequest):
    _require_lark(connector_id)
    if not request.confirmed:
        raise HTTPException(status_code=409, detail="断开连接是破坏性操作，需要明确确认。")

    def execute():
        service = _managed_service()
        match = ManagedCliRegistry().match("lark-cli config remove")
        assert match is not None
        plan = service.plan(match, {})
        return service.execute(
            plan,
            {"_managed_cli_destructive_approval": plan.destructive_approval_binding()},
        )

    return _result_or_error(await asyncio.to_thread(execute))
