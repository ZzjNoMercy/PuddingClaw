"""MCP Client Factory.

MCP discovery can be noticeably more expensive than constructing the local
Agent tool list, especially for stdio servers such as ``gbrain serve``.  The
returned LangChain tools open a fresh MCP session when they are invoked, so
their discovery metadata is safe to reuse across Agent instances in the same
backend process.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import threading
from collections import OrderedDict
from pathlib import Path
from typing import Any

from langchain_core.tools import BaseTool
from langchain_mcp_adapters.client import MultiServerMCPClient

from mcp_clients.servers import build_mcp_servers_config, filter_mcp_tools

_MAX_DISCOVERY_CACHE_ENTRIES = 8
_discovery_cache: OrderedDict[tuple[int, str], tuple[BaseTool, ...]] = OrderedDict()
_discovery_inflight: dict[tuple[int, int, str], asyncio.Task[tuple[BaseTool, ...]]] = {}
_discovery_cache_generation = 0
_discovery_cache_lock = threading.Lock()


def create_mcp_client(enabled_names: list[str] | None = None) -> MultiServerMCPClient:
    """根据启用的服务器列表创建 MCP 客户端."""
    cfg = build_mcp_servers_config(enabled_names)
    if not cfg:
        raise ValueError("No MCP servers enabled or configured")
    return MultiServerMCPClient(cfg)


def _file_signature(path: Path) -> dict[str, Any]:
    """Return cheap metadata that invalidates discovery after runtime changes."""

    try:
        stat = path.stat()
    except OSError:
        return {"path": str(path), "exists": False}
    return {
        "path": str(path),
        "exists": True,
        "mtime_ns": stat.st_mtime_ns,
        "size": stat.st_size,
    }


def _runtime_signatures(cfg: dict[str, Any]) -> list[dict[str, Any]]:
    """Capture files whose contents affect the exposed MCP tool schema."""

    signatures: list[dict[str, Any]] = []
    for server_name, server in cfg.items():
        if not isinstance(server, dict):
            continue
        command = str(server.get("command") or "").strip()
        if command and Path(command).is_absolute():
            signatures.append(
                {
                    "server": server_name,
                    "kind": "command",
                    **_file_signature(Path(command)),
                }
            )
        environment = server.get("env")
        if not isinstance(environment, dict):
            continue
        # The active gbrain configuration and Schema Pack determine the MCP
        # tool descriptions. Include their file metadata so saving either one
        # automatically forces a fresh discovery without restarting backend.
        gbrain_home = str(environment.get("GBRAIN_HOME") or "").strip()
        if gbrain_home:
            home = Path(gbrain_home).expanduser()
            pack_name = str(environment.get("GBRAIN_SCHEMA_PACK") or "puddingclaw-wiki").strip()
            for kind, path in (
                ("gbrain_config", home / ".gbrain" / "config.json"),
                ("gbrain_schema_pack", home / ".gbrain" / "schema-packs" / pack_name / "pack.yaml"),
            ):
                signatures.append(
                    {
                        "server": server_name,
                        "kind": kind,
                        **_file_signature(path),
                    }
                )
    return signatures


def _discovery_fingerprint(cfg: dict[str, Any]) -> str:
    """Hash effective connection data without retaining or logging secrets."""

    payload = {
        "servers": cfg,
        "runtime_files": _runtime_signatures(cfg),
    }
    canonical = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def invalidate_mcp_tool_cache() -> None:
    """Invalidate cached discovery metadata for subsequent Agent builds."""

    global _discovery_cache_generation
    with _discovery_cache_lock:
        _discovery_cache_generation += 1
        _discovery_cache.clear()


async def _discover_filtered_tools(
    cfg: dict[str, Any],
    *,
    cache_key: tuple[int, str],
    inflight_key: tuple[int, int, str],
) -> tuple[BaseTool, ...]:
    """Run one MCP discovery and publish its immutable result to the cache."""

    current_task = asyncio.current_task()
    try:
        client = MultiServerMCPClient(cfg, tool_name_prefix=True)
        result: list[BaseTool] = []
        for server_name in cfg:
            discovered = await client.get_tools(server_name=server_name)
            result.extend(filter_mcp_tools(server_name, discovered))
        cached = tuple(result)
        with _discovery_cache_lock:
            _discovery_cache[cache_key] = cached
            _discovery_cache.move_to_end(cache_key)
            while len(_discovery_cache) > _MAX_DISCOVERY_CACHE_ENTRIES:
                _discovery_cache.popitem(last=False)
        return cached
    finally:
        with _discovery_cache_lock:
            if _discovery_inflight.get(inflight_key) is current_task:
                _discovery_inflight.pop(inflight_key, None)


async def load_filtered_mcp_tools(
    enabled_names: list[str],
    *,
    force_refresh: bool = False,
) -> list[BaseTool]:
    """Load prefixed, allowlisted MCP tools for the DeepAgents runtime.

    The adapter-backed tools create a fresh MCP session per invocation, so no
    session stack has to outlive Agent construction. Discovery metadata is
    cached per effective server configuration. Concurrent Agent builds share
    one in-flight discovery instead of spawning duplicate stdio servers.
    """

    cfg = build_mcp_servers_config(enabled_names)
    if not cfg:
        return []
    fingerprint = _discovery_fingerprint(cfg)
    loop = asyncio.get_running_loop()
    with _discovery_cache_lock:
        generation = _discovery_cache_generation
        cache_key = (generation, fingerprint)
        if not force_refresh:
            cached = _discovery_cache.get(cache_key)
            if cached is not None:
                _discovery_cache.move_to_end(cache_key)
                return list(cached)
        inflight_key = (id(loop), generation, fingerprint)
        task = _discovery_inflight.get(inflight_key)
        if task is None or task.done():
            task = loop.create_task(
                _discover_filtered_tools(
                    cfg,
                    cache_key=cache_key,
                    inflight_key=inflight_key,
                ),
                name=f"mcp-discovery-{fingerprint[:12]}",
            )
            _discovery_inflight[inflight_key] = task
    # One cancelled request must not cancel discovery awaited by another Agent
    # build or the startup warm-up task.
    return list(await asyncio.shield(task))
