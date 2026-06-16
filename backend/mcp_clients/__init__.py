"""MCP Client Factory."""

from langchain_mcp_adapters.client import MultiServerMCPClient
from mcp_clients.servers import build_mcp_servers_config


def create_mcp_client(enabled_names: list[str] | None = None) -> MultiServerMCPClient:
    """根据启用的服务器列表创建 MCP 客户端."""
    cfg = build_mcp_servers_config(enabled_names)
    if not cfg:
        raise ValueError("No MCP servers enabled or configured")
    return MultiServerMCPClient(cfg)
