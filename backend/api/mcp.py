"""MCP Servers API — list configured/enabled MCP servers."""

from fastapi import APIRouter

from config import load_config

router = APIRouter()

# Mirror of mcp_clients.servers._SERVER_DISPLAY_NAMES to avoid importing
# the full mcp_clients package (which pulls in optional langchain deps).
_MCP_DISPLAY_NAMES: dict[str, str] = {
    "zhihuiya_patents": "智慧芽专利检索",
}


@router.get("/mcp/servers")
async def list_mcp_servers():
    """List enabled MCP servers for frontend panel display."""
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

    return {"servers": servers, "gbrain": gbrain}
