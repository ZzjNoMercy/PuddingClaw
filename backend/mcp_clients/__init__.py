"""MCP Client Factory."""

from langchain_core.tools import BaseTool
from langchain_mcp_adapters.client import MultiServerMCPClient

from mcp_clients.servers import build_mcp_servers_config, filter_mcp_tools


def create_mcp_client(enabled_names: list[str] | None = None) -> MultiServerMCPClient:
    """根据启用的服务器列表创建 MCP 客户端."""
    cfg = build_mcp_servers_config(enabled_names)
    if not cfg:
        raise ValueError("No MCP servers enabled or configured")
    return MultiServerMCPClient(cfg)


async def load_filtered_mcp_tools(enabled_names: list[str]) -> list[BaseTool]:
    """Load prefixed, allowlisted MCP tools for the DeepAgents runtime.

    The adapter-backed tools create a fresh MCP session per invocation, so no
    session stack has to outlive Agent construction.
    """

    cfg = build_mcp_servers_config(enabled_names)
    if not cfg:
        return []
    client = MultiServerMCPClient(cfg, tool_name_prefix=True)
    result: list[BaseTool] = []
    for server_name in cfg:
        discovered = await client.get_tools(server_name=server_name)
        result.extend(filter_mcp_tools(server_name, discovered))
    return result
