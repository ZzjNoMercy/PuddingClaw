"""MCP Servers API — list and optionally probe configured MCP servers."""

import asyncio
import copy
import re
from typing import Any

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from config import CONFIG_FILE, load_config, save_config

router = APIRouter()

# Mirror of mcp_clients.servers._SERVER_DISPLAY_NAMES to avoid importing
# the full mcp_clients package (which pulls in optional langchain deps).
_MCP_DISPLAY_NAMES: dict[str, str] = {
    "zhihuiya_patents": "智慧芽专利检索",
}

_SERVER_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
_SUPPORTED_TRANSPORTS = {"stdio", "sse", "streamable-http"}
_MASKED_SECRET = "***"


def _mask_secret(value: Any) -> Any:
    """Keep environment references readable while masking literal secrets."""

    if isinstance(value, str) and value.startswith("${") and value.endswith("}"):
        return value
    return _MASKED_SECRET


def _safe_mcp_config(config: dict[str, Any]) -> dict[str, Any]:
    """Return MCP config suitable for the browser, masking header values."""

    result = copy.deepcopy(config)
    servers = result.get("servers")
    if isinstance(servers, dict):
        for server in servers.values():
            if not isinstance(server, dict):
                continue
            headers = server.get("headers")
            if isinstance(headers, dict):
                server["headers"] = {str(key): _mask_secret(value) for key, value in headers.items()}
            environment = server.get("env")
            if isinstance(environment, dict):
                server["env"] = {str(key): _mask_secret(value) for key, value in environment.items()}
    return result


class McpConfigRequest(BaseModel):
    config: dict[str, Any] = Field(default_factory=dict)


def _validate_mcp_config(raw: dict[str, Any], current: dict[str, Any]) -> dict[str, Any]:
    """Validate the browser-editable MCP section and preserve masked secrets."""

    enabled = raw.get("enabled", [])
    if not isinstance(enabled, list) or any(not isinstance(name, str) or not name.strip() for name in enabled):
        raise ValueError("mcp.enabled must be an array of server names")
    if len(set(enabled)) != len(enabled):
        raise ValueError("mcp.enabled contains duplicate server names")

    auto_enable_gbrain = raw.get("auto_enable_gbrain", False)
    if not isinstance(auto_enable_gbrain, bool):
        raise ValueError("mcp.auto_enable_gbrain must be a boolean")

    servers = raw.get("servers", {})
    if not isinstance(servers, dict):
        raise ValueError("mcp.servers must be an object")
    clean_servers: dict[str, Any] = {}
    current_servers = current.get("servers", {}) if isinstance(current.get("servers"), dict) else {}
    for name, value in servers.items():
        if not isinstance(name, str) or not _SERVER_NAME_RE.fullmatch(name):
            raise ValueError(f"Invalid MCP server name: {name}")
        if not isinstance(value, dict):
            raise ValueError(f"MCP server {name} must be an object")
        transport = value.get("transport")
        if transport not in _SUPPORTED_TRANSPORTS:
            raise ValueError(f"MCP server {name} has unsupported transport: {transport}")
        if transport in {"sse", "streamable-http"} and not str(value.get("url") or "").strip():
            raise ValueError(f"MCP server {name} requires url")
        if transport == "stdio" and not str(value.get("command") or "").strip():
            raise ValueError(f"MCP server {name} requires command")
        item = copy.deepcopy(value)
        headers = item.get("headers")
        if isinstance(headers, dict):
            previous = current_servers.get(name, {})
            previous_headers = previous.get("headers", {}) if isinstance(previous, dict) else {}
            item["headers"] = {
                str(key): previous_headers[key] if secret == _MASKED_SECRET and key in previous_headers else secret
                for key, secret in headers.items()
                if str(key).strip() and isinstance(secret, str) and secret.strip()
            }
        environment = item.get("env")
        if isinstance(environment, dict):
            previous = current_servers.get(name, {})
            previous_environment = previous.get("env", {}) if isinstance(previous, dict) else {}
            item["env"] = {
                str(key): previous_environment[key] if secret == _MASKED_SECRET and key in previous_environment else secret
                for key, secret in environment.items()
                if str(key).strip() and isinstance(secret, str) and secret.strip()
            }
        clean_servers[name] = item

    result = {
        "enabled": list(dict.fromkeys(enabled)),
        "auto_enable_gbrain": auto_enable_gbrain,
        "servers": clean_servers,
    }
    return result


@router.get("/mcp/config")
async def get_mcp_config():
    """Return the persisted MCP section and its local source path."""

    config = load_config().get("mcp", {})
    return {
        "path": str(CONFIG_FILE),
        "config": _safe_mcp_config(config if isinstance(config, dict) else {}),
    }


@router.put("/mcp/config")
async def put_mcp_config(request: McpConfigRequest):
    """Persist MCP enablement and user-defined server definitions."""

    current_config = load_config()
    current_mcp = current_config.get("mcp", {})
    try:
        next_mcp = _validate_mcp_config(
            request.config,
            current_mcp if isinstance(current_mcp, dict) else {},
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    current_config["mcp"] = next_mcp
    save_config(current_config)
    try:
        from mcp_clients import invalidate_mcp_tool_cache

        invalidate_mcp_tool_cache()
    except Exception:
        pass
    return {"path": str(CONFIG_FILE), "config": _safe_mcp_config(next_mcp), "status": "saved"}


@router.get("/mcp/servers")
async def list_mcp_servers(probe: bool = Query(False)):
    """List effective servers and secret-free discovery/load status."""
    cfg = load_config()
    mcp_config = cfg.get("mcp", {})
    custom_servers = mcp_config.get("servers", {}) if isinstance(mcp_config, dict) else {}

    # Import server registry lazily to avoid heavy deps at module load time.
    try:
        from mcp_clients.servers import (
            effective_mcp_server_names,
            gbrain_runtime_status,
            get_mcp_server_display_info,
        )
        enabled = effective_mcp_server_names(
            mcp_config.get("enabled", []),
            auto_enable_gbrain=bool(mcp_config.get("auto_enable_gbrain", False)),
        )
        from mcp_clients.servers import _server_registry

        registry_names = list(_server_registry(custom_servers).keys())
        servers = get_mcp_server_display_info(enabled, custom_servers)
        gbrain = gbrain_runtime_status()
        catalog_names = list(dict.fromkeys([*registry_names, *enabled]))
        catalog = get_mcp_server_display_info(catalog_names, custom_servers)
        enabled_set = set(enabled)
        explicitly_enabled = set(mcp_config.get("enabled", []))
        for item in catalog:
            key = item["key"]
            item["enabled"] = key in enabled_set
            item["auto_enabled"] = key == "gbrain" and key in enabled_set and key not in explicitly_enabled
            item["ready"] = bool(gbrain.get("ready")) if key == "gbrain" else True
            item["status"] = "ready" if item["enabled"] and item["ready"] else "not_ready"
            item["reason"] = str(gbrain.get("reason") or "") if key == "gbrain" else ""
            item["loaded"] = False
            item["tools"] = []
            item["tool_count"] = 0

        if probe:
            from mcp_clients import load_filtered_mcp_tools

            async def probe_server(item: dict[str, Any]) -> None:
                if not item["enabled"] or not item["ready"]:
                    return
                try:
                    tools = await asyncio.wait_for(
                        load_filtered_mcp_tools([item["key"]]),
                        timeout=20,
                    )
                    names = [str(getattr(tool, "name", "")) for tool in tools]
                    item["loaded"] = True
                    item["status"] = "loaded"
                    item["tools"] = names
                    item["tool_count"] = len(names)
                except Exception as exc:
                    item["status"] = "error"
                    item["reason"] = str(exc)[:500]

            await asyncio.gather(*(probe_server(item) for item in catalog))
    except Exception:
        # Fallback: return minimal info from config when MCP client deps are missing.
        servers = [
            {
                "key": name,
                "name": _MCP_DISPLAY_NAMES.get(name, name),
                "url": "",
                "transport": "",
            }
            for name in mcp_config.get("enabled", [])
        ]
        gbrain = {"configured": False, "ready": False, "reason": "runtime status unavailable"}
        catalog = [
            {
                **server,
                "enabled": True,
                "auto_enabled": False,
                "ready": False,
                "loaded": False,
                "status": "error",
                "reason": "runtime status unavailable",
                "tools": [],
                "tool_count": 0,
            }
            for server in servers
        ]

    return {"servers": servers, "catalog": catalog, "gbrain": gbrain}
