"""MCP Servers API — list and optionally probe configured MCP servers."""

import asyncio

from fastapi import APIRouter, Query

from config import load_config

router = APIRouter()

# Mirror of mcp_clients.servers._SERVER_DISPLAY_NAMES to avoid importing
# the full mcp_clients package (which pulls in optional langchain deps).
_MCP_DISPLAY_NAMES: dict[str, str] = {
    "zhihuiya_patents": "智慧芽专利检索",
}


@router.get("/mcp/servers")
async def list_mcp_servers(probe: bool = Query(False)):
    """List effective servers and secret-free discovery/load status."""
    cfg = load_config()
    mcp_config = cfg.get("mcp", {})

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
        servers = get_mcp_server_display_info(enabled)
        gbrain = gbrain_runtime_status()
        catalog_names = list(dict.fromkeys([*enabled, "gbrain"]))
        catalog = get_mcp_server_display_info(catalog_names)
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

            for item in catalog:
                if not item["enabled"] or not item["ready"]:
                    continue
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
