"""MCP Servers Registry — 全局 MCP 服务器配置.

所有敏感信息（API Key）均从环境变量读取，不在此文件硬编码。
"""

import os
from typing import Any


def _get_env(name: str, default: str = "") -> str:
    """读取环境变量."""
    return os.getenv(name, default)


# ===== 全局 MCP 服务器注册表 =====
# 新增服务器只需在此注册，然后在 config.json 的 mcp.enabled 中启用
_REGISTRY: dict[str, Any] = {
    # 示例：技术研发问答（按需修改为你的 MCP Server）
    # "technical_qa": {
    #     "transport": "streamable-http",
    #     "url": "https://your-mcp-server.com/mcp",
    #     "headers": {
    #         "Authorization": f"Bearer {_get_env('MCP_API_KEY')}"
    #     },
    #     "timeout": 60,
    # },
}

# 服务器中文显示名映射（供前端看板使用）
_SERVER_DISPLAY_NAMES: dict[str, str] = {
    # "technical_qa": "技术研发问答",
}


def get_mcp_server_display_info(enabled_names: list[str]) -> list[dict[str, str]]:
    """返回供前端展示的 MCP 服务器信息（不含敏感 headers）."""
    result = []
    for name in enabled_names:
        cfg = _REGISTRY.get(name)
        if not cfg:
            continue
        result.append({
            "key": name,
            "name": _SERVER_DISPLAY_NAMES.get(name, name),
            "url": cfg.get("url", ""),
            "transport": cfg.get("transport", ""),
        })
    return result


def build_mcp_servers_config(enabled_names: list[str] | None = None) -> dict[str, Any]:
    """构建 MCP 服务器配置，供 MultiServerMCPClient 使用.

    Args:
        enabled_names: 指定的启用列表。None 时返回所有已定义服务器。

    环境变量规范：
        MCP_API_KEY 或各服务特定 Key — API Key
    """
    import copy
    registry = copy.deepcopy(_REGISTRY)

    if enabled_names is not None:
        return {k: v for k, v in registry.items() if k in enabled_names}

    return registry
