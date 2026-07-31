from __future__ import annotations

import asyncio
from types import SimpleNamespace

import mcp_clients


def _install_fake_discovery(monkeypatch, *, delay: float = 0) -> type:
    class FakeMCPClient:
        calls = 0

        def __init__(self, _cfg, *, tool_name_prefix=False):
            assert tool_name_prefix is True

        async def get_tools(self, *, server_name: str):
            type(self).calls += 1
            if delay:
                await asyncio.sleep(delay)
            return [SimpleNamespace(name=f"{server_name}_query")]

    monkeypatch.setattr(mcp_clients, "MultiServerMCPClient", FakeMCPClient)
    monkeypatch.setattr(
        mcp_clients,
        "build_mcp_servers_config",
        lambda _enabled: {
            "gbrain": {
                "transport": "stdio",
                "command": "gbrain",
                "args": ["serve"],
            }
        },
    )
    monkeypatch.setattr(mcp_clients, "filter_mcp_tools", lambda _server, tools: list(tools))
    mcp_clients.invalidate_mcp_tool_cache()
    return FakeMCPClient


def test_mcp_discovery_is_reused_across_agent_builds(monkeypatch) -> None:
    client_type = _install_fake_discovery(monkeypatch)

    async def run() -> None:
        first = await mcp_clients.load_filtered_mcp_tools(["gbrain"])
        second = await mcp_clients.load_filtered_mcp_tools(["gbrain"])
        assert first is not second
        assert [tool.name for tool in first] == ["gbrain_query"]
        assert [tool.name for tool in second] == ["gbrain_query"]

    asyncio.run(run())
    assert client_type.calls == 1


def test_concurrent_agent_builds_share_one_mcp_discovery(monkeypatch) -> None:
    client_type = _install_fake_discovery(monkeypatch, delay=0.02)

    async def run() -> None:
        results = await asyncio.gather(*(mcp_clients.load_filtered_mcp_tools(["gbrain"]) for _ in range(5)))
        assert all([tool.name for tool in tools] == ["gbrain_query"] for tools in results)

    asyncio.run(run())
    assert client_type.calls == 1


def test_gbrain_runtime_file_change_invalidates_discovery(monkeypatch, tmp_path) -> None:
    client_type = _install_fake_discovery(monkeypatch)
    config_path = tmp_path / ".gbrain" / "config.json"
    pack_path = tmp_path / ".gbrain" / "schema-packs" / "puddingclaw-wiki" / "pack.yaml"
    pack_path.parent.mkdir(parents=True)
    config_path.write_text("{}", encoding="utf-8")
    pack_path.write_text("version: 1\n", encoding="utf-8")
    monkeypatch.setattr(
        mcp_clients,
        "build_mcp_servers_config",
        lambda _enabled: {
            "gbrain": {
                "transport": "stdio",
                "command": "gbrain",
                "args": ["serve"],
                "env": {
                    "GBRAIN_HOME": str(tmp_path),
                    "GBRAIN_SCHEMA_PACK": "puddingclaw-wiki",
                },
            }
        },
    )

    async def run() -> None:
        await mcp_clients.load_filtered_mcp_tools(["gbrain"])
        pack_path.write_text("version: 2 with a different size\n", encoding="utf-8")
        await mcp_clients.load_filtered_mcp_tools(["gbrain"])

    asyncio.run(run())
    assert client_type.calls == 2


def test_failed_mcp_discovery_is_not_cached(monkeypatch) -> None:
    class FlakyMCPClient:
        calls = 0

        def __init__(self, _cfg, *, tool_name_prefix=False):
            assert tool_name_prefix is True

        async def get_tools(self, *, server_name: str):
            type(self).calls += 1
            if type(self).calls == 1:
                raise RuntimeError("temporary startup failure")
            return [SimpleNamespace(name=f"{server_name}_query")]

    monkeypatch.setattr(mcp_clients, "MultiServerMCPClient", FlakyMCPClient)
    monkeypatch.setattr(
        mcp_clients,
        "build_mcp_servers_config",
        lambda _enabled: {
            "gbrain": {
                "transport": "stdio",
                "command": "gbrain",
                "args": ["serve"],
            }
        },
    )
    monkeypatch.setattr(mcp_clients, "filter_mcp_tools", lambda _server, tools: list(tools))
    mcp_clients.invalidate_mcp_tool_cache()

    async def run() -> None:
        try:
            await mcp_clients.load_filtered_mcp_tools(["gbrain"])
        except RuntimeError:
            pass
        else:
            raise AssertionError("first discovery should fail")
        tools = await mcp_clients.load_filtered_mcp_tools(["gbrain"])
        assert [tool.name for tool in tools] == ["gbrain_query"]

    asyncio.run(run())
    assert FlakyMCPClient.calls == 2
