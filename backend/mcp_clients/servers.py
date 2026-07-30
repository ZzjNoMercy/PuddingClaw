"""MCP Servers Registry — 全局 MCP 服务器配置.

所有敏感信息（API Key）均从环境变量读取，不在此文件硬编码.
"""

import os
import shutil
from pathlib import Path
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
    "zhihuiya_patents": {
        "transport": "streamable-http",
        "url": "https://connect.zhihuiya.com/1458a4/mcp",
        "headers": {
            "Authorization": f"Bearer {_get_env('ZHIHUIYA_MCP_API_KEY')}",
        },
        "timeout": 60,
    },
    # Enabled only when PUDDINGCLAW_GBRAIN_HOME points at the dedicated,
    # PostgreSQL-configured brain. Never fall back to the user's personal brain.
    "gbrain": {
        "transport": "stdio",
        "command": "gbrain",
        "args": ["serve"],
    },
}

# gbrain publishes a broad admin/write surface over MCP. PuddingClaw's Agent
# receives only the read-only retrieval and graph inspection subset below.
_GBRAIN_ALLOWED_TOOLS = frozenset(
    {
        "get_page",
        "list_pages",
        "search",
        "query",
        "get_links",
        "get_backlinks",
        "traverse_graph",
        "get_timeline",
        "get_stats",
        "get_health",
        "resolve_slugs",
        "get_chunks",
        "get_active_schema_pack",
        "schema_stats",
        "schema_graph",
        "schema_explain_type",
    }
)

# 服务器中文显示名映射（供前端看板使用）
_SERVER_DISPLAY_NAMES: dict[str, str] = {
    # "technical_qa": "技术研发问答",
    "zhihuiya_patents": "智慧芽专利检索",
    "gbrain": "LLM Wiki · gbrain（只读）",
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


def gbrain_runtime_status() -> dict[str, Any]:
    """Cheap, secret-free readiness gate for the dedicated gbrain runtime.

    This deliberately checks durable prerequisites only. Starting ``gbrain
    serve`` here would add seconds to every Agent construction and create a
    second MCP process before the real client does discovery.
    """

    configured_home = _get_env("PUDDINGCLAW_GBRAIN_HOME").strip()
    binary_name = _get_env("PUDDINGCLAW_GBRAIN_BIN", "gbrain").strip() or "gbrain"
    binary = shutil.which(binary_name)
    if not configured_home:
        return {
            "configured": False,
            "ready": False,
            "reason": "PUDDINGCLAW_GBRAIN_HOME is not configured",
            "home": "",
            "binary": binary or "",
            "config_exists": False,
            "pack_exists": False,
        }

    home = Path(configured_home).expanduser().resolve()
    config_exists = (home / ".gbrain" / "config.json").is_file()
    pack_exists = (home / ".gbrain" / "schema-packs" / "puddingclaw-wiki" / "pack.yaml").is_file()
    reasons: list[str] = []
    if not binary:
        reasons.append("gbrain CLI is not installed")
    if not config_exists:
        reasons.append("dedicated gbrain home is not initialized")
    if not pack_exists:
        reasons.append("puddingclaw-wiki schema pack is not compiled")
    return {
        "configured": True,
        "ready": not reasons,
        "reason": "; ".join(reasons),
        "home": str(home),
        "binary": binary or "",
        "config_exists": config_exists,
        "pack_exists": pack_exists,
    }


def effective_mcp_server_names(
    enabled_names: list[str] | None,
    *,
    auto_enable_gbrain: bool = False,
) -> list[str]:
    """Return the effective server set after runtime-readiness policy."""

    result = list(dict.fromkeys(str(name) for name in (enabled_names or []) if str(name)))
    status = gbrain_runtime_status()
    if auto_enable_gbrain and status["ready"] and "gbrain" not in result:
        result.append("gbrain")
    if not status["ready"]:
        result = [name for name in result if name != "gbrain"]
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
    gbrain_status = gbrain_runtime_status()
    gbrain_home = str(gbrain_status["home"])
    if not gbrain_status["ready"]:
        registry.pop("gbrain", None)
    elif "gbrain" in registry:
        registry["gbrain"]["command"] = _get_env("PUDDINGCLAW_GBRAIN_BIN", "gbrain")
        registry["gbrain"]["env"] = {
            "GBRAIN_HOME": gbrain_home,
            "GBRAIN_SCHEMA_PACK": "puddingclaw-wiki",
        }

    if enabled_names is not None:
        return {k: v for k, v in registry.items() if k in enabled_names}

    return registry


def allowed_mcp_tool_names(server_name: str) -> frozenset[str] | None:
    """Return a hard allowlist for a server, or None when no filter is declared."""

    if server_name == "gbrain":
        return _GBRAIN_ALLOWED_TOOLS
    return None


def filter_mcp_tools(server_name: str, tools: list[Any]) -> list[Any]:
    """Filter tools immediately after MCP discovery, before Agent construction."""

    allowed = allowed_mcp_tool_names(server_name)
    if allowed is None:
        return list(tools)
    prefix = f"{server_name}_"
    filtered: list[Any] = []
    for tool in tools:
        name = str(getattr(tool, "name", ""))
        upstream_name = name[len(prefix) :] if name.startswith(prefix) else name
        if upstream_name in allowed:
            filtered.append(tool)
    return filtered
