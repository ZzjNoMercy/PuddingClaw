"""MCP Servers Registry — 全局 MCP 服务器配置.

所有敏感信息（API Key）均从环境变量读取，不在此文件硬编码.
"""

import os
import copy
import re
from pathlib import Path
from typing import Any

from gbrain_runtime import (
    apply_gbrain_ai_environment,
    gbrain_subprocess_environment,
    resolve_gbrain_ai_runtime,
    resolve_gbrain_binary,
)


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
    # Enabled only when the active knowledge base owns an initialized,
    # PostgreSQL-configured brain. Never fall back to a separate personal brain.
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
        "think",
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
    "gbrain": "gbrain",
}


_CUSTOM_SERVER_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
_SUPPORTED_TRANSPORTS = {"stdio", "sse", "streamable-http"}


def _configured_servers() -> dict[str, Any]:
    """Load user-defined servers without importing config at module load time."""

    try:
        from config import load_config

        servers = load_config().get("mcp", {}).get("servers", {})
    except Exception:
        return {}
    return servers if isinstance(servers, dict) else {}


def _server_registry(custom_servers: dict[str, Any] | None = None) -> dict[str, Any]:
    """Combine code-owned built-ins with user-defined MCP servers."""

    registry = copy.deepcopy(_REGISTRY)
    for name, value in (custom_servers if custom_servers is not None else _configured_servers()).items():
        if not isinstance(name, str) or not _CUSTOM_SERVER_NAME_RE.fullmatch(name):
            continue
        if not isinstance(value, dict) or value.get("transport") not in _SUPPORTED_TRANSPORTS:
            continue
        if value.get("transport") in {"sse", "streamable-http"} and not str(value.get("url") or "").strip():
            continue
        if value.get("transport") == "stdio" and not str(value.get("command") or "").strip():
            continue
        registry[name] = _resolve_environment_values(copy.deepcopy(value))
    return registry


def _resolve_environment_values(value: Any) -> Any:
    """Resolve ${ENV_NAME} references in the single MCP config file."""

    if isinstance(value, dict):
        return {key: _resolve_environment_values(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_resolve_environment_values(item) for item in value]
    if isinstance(value, str) and value.startswith("vault://"):
        from provider_registry import LocalCredentialStore

        return LocalCredentialStore().get(value)
    if isinstance(value, str) and value.startswith("${") and value.endswith("}"):
        return _get_env(value[2:-1])
    return value


def get_mcp_server_display_info(
    enabled_names: list[str],
    custom_servers: dict[str, Any] | None = None,
) -> list[dict[str, str]]:
    """返回供前端展示的 MCP 服务器信息（不含敏感 headers）."""
    result = []
    registry = _server_registry(custom_servers)
    for name in enabled_names:
        cfg = registry.get(name)
        if not cfg:
            continue
        result.append({
            "key": name,
            "name": cfg.get("name") or _SERVER_DISPLAY_NAMES.get(name, name),
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

    binary = resolve_gbrain_binary()
    from knowledge.paths import get_gbrain_runtime_home

    backend_root = Path(__file__).resolve().parent.parent
    home = get_gbrain_runtime_home(backend_root)
    config_exists = (home / ".gbrain" / "config.json").is_file()
    if not config_exists:
        return {
            "configured": False,
            "ready": False,
            "reason": "gbrain 专用运行目录尚未初始化",
            "home": str(home),
            "binary": binary or "",
            "config_exists": False,
            "pack_exists": False,
        }

    pack_exists = (home / ".gbrain" / "schema-packs" / "puddingclaw-wiki" / "pack.yaml").is_file()
    reasons: list[str] = []
    models: dict[str, Any] | None = None
    if not binary:
        reasons.append("gbrain CLI is not installed")
    if not config_exists:
        reasons.append("gbrain 专用运行目录尚未初始化")
    if not pack_exists:
        reasons.append("puddingclaw-wiki schema pack is not compiled")
    try:
        ai_runtime = resolve_gbrain_ai_runtime()
        models = {
            "embedding": ai_runtime["embedding"],
            "think": ai_runtime["think"],
        }
    except (OSError, ValueError) as exc:
        reasons.append(str(exc))
    return {
        "configured": True,
        "ready": not reasons,
        "reason": "; ".join(reasons),
        "home": str(home),
        "binary": binary or "",
        "config_exists": config_exists,
        "pack_exists": pack_exists,
        "models": models,
    }


def effective_mcp_server_names(
    enabled_names: list[str] | None,
) -> list[str]:
    """Return user-enabled servers plus the mandatory ready gbrain runtime."""

    result = list(
        dict.fromkeys(
            str(name)
            for name in (enabled_names or [])
            if str(name) and str(name) != "gbrain"
        )
    )
    status = gbrain_runtime_status()
    if status["ready"]:
        result.append("gbrain")
    return result


def build_mcp_servers_config(
    enabled_names: list[str] | None = None,
    custom_servers: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """构建 MCP 服务器配置，供 MultiServerMCPClient 使用.

    Args:
        enabled_names: 指定的启用列表。None 时返回所有已定义服务器。

    环境变量规范：
        MCP_API_KEY 或各服务特定 Key — API Key
    """
    registry = _server_registry(custom_servers)
    gbrain_status = gbrain_runtime_status()
    gbrain_home = str(gbrain_status["home"])
    if not gbrain_status["ready"]:
        registry.pop("gbrain", None)
    elif "gbrain" in registry:
        registry["gbrain"]["command"] = str(gbrain_status["binary"])
        # GBrain normally honors GBRAIN_HOME, but an explicit working
        # directory also prevents cold-start discovery from falling back to a
        # user's personal/default brain when PuddingClaw is launched from a
        # reloader or package runner with a different cwd.
        registry["gbrain"]["cwd"] = gbrain_home
        environment = {
            "GBRAIN_HOME": gbrain_home,
            "GBRAIN_SCHEMA_PACK": "puddingclaw-wiki",
            "PATH": gbrain_subprocess_environment(str(gbrain_status["binary"]))["PATH"],
        }
        try:
            environment, _runtime = apply_gbrain_ai_environment(environment)
            registry["gbrain"]["env"] = environment
        except ValueError:
            registry.pop("gbrain", None)

    if enabled_names is not None:
        return {
            k: {field: value for field, value in v.items() if field != "name"}
            for k, v in registry.items()
            if k in enabled_names
        }

    return {
        k: {field: value for field, value in v.items() if field != "name"}
        for k, v in registry.items()
    }


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
