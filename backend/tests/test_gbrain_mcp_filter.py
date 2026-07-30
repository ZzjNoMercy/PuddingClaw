from __future__ import annotations

import sys
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

from api.mcp import router
from config import _DEFAULT_CONFIG
from mcp_clients.servers import (
    allowed_mcp_tool_names,
    build_mcp_servers_config,
    effective_mcp_server_names,
    filter_mcp_tools,
    gbrain_runtime_status,
)
from tools.toolsets import tools_for_toolsets


def test_default_config_auto_enables_only_ready_gbrain_runtime() -> None:
    assert _DEFAULT_CONFIG["mcp"] == {"enabled": [], "auto_enable_gbrain": True}


def test_gbrain_server_requires_ready_dedicated_home(monkeypatch, tmp_path) -> None:
    monkeypatch.delenv("PUDDINGCLAW_GBRAIN_HOME", raising=False)
    monkeypatch.setenv("PUDDINGCLAW_KNOWLEDGE_DIR", str(tmp_path / "knowledge"))
    assert build_mcp_servers_config(["gbrain"]) == {}
    monkeypatch.setenv("PUDDINGCLAW_GBRAIN_HOME", str(tmp_path))
    monkeypatch.setenv("PUDDINGCLAW_GBRAIN_BIN", sys.executable)
    assert build_mcp_servers_config(["gbrain"]) == {}
    (tmp_path / ".gbrain" / "schema-packs" / "puddingclaw-wiki").mkdir(parents=True)
    (tmp_path / ".gbrain" / "config.json").write_text("{}", encoding="utf-8")
    (tmp_path / ".gbrain" / "schema-packs" / "puddingclaw-wiki" / "pack.yaml").write_text("api_version: gbrain-schema-pack-v1\n", encoding="utf-8")
    runtime = {
        "embedding": {"name": "embed", "provider": "dashscope", "dimension": 1024},
        "think": {"name": "think", "provider": "deepseek"},
    }
    monkeypatch.setattr("mcp_clients.servers.resolve_gbrain_ai_runtime", lambda: runtime)
    monkeypatch.setattr(
        "mcp_clients.servers.apply_gbrain_ai_environment",
        lambda base: ({**base, "DASHSCOPE_API_KEY": "test"}, runtime),
    )
    config = build_mcp_servers_config(["gbrain"])["gbrain"]
    assert config["transport"] == "stdio"
    assert config["env"]["GBRAIN_HOME"] == str(tmp_path)
    assert config["env"]["GBRAIN_SCHEMA_PACK"] == "puddingclaw-wiki"
    assert gbrain_runtime_status()["ready"] is True
    assert effective_mcp_server_names([], auto_enable_gbrain=True) == ["gbrain"]


def test_unready_gbrain_is_removed_even_when_explicitly_enabled(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("PUDDINGCLAW_GBRAIN_HOME", str(tmp_path))
    monkeypatch.setenv("PUDDINGCLAW_GBRAIN_BIN", sys.executable)
    assert effective_mcp_server_names(["gbrain", "zhihuiya_patents"], auto_enable_gbrain=True) == ["zhihuiya_patents"]
    status = gbrain_runtime_status()
    assert status["configured"] is True
    assert status["ready"] is False
    assert "not initialized" in status["reason"]


def test_gbrain_mcp_filter_is_read_only_and_fail_closed() -> None:
    discovered = [
        SimpleNamespace(name="query"),
        SimpleNamespace(name="think"),
        SimpleNamespace(name="get_page"),
        SimpleNamespace(name="put_page"),
        SimpleNamespace(name="delete_page"),
        SimpleNamespace(name="schema_apply_mutations"),
        SimpleNamespace(name="sync_brain"),
    ]
    filtered = filter_mcp_tools("gbrain", discovered)
    assert {tool.name for tool in filtered} == {"query", "think", "get_page"}
    allowed = allowed_mcp_tool_names("gbrain")
    assert allowed is not None
    assert not {"put_page", "delete_page", "schema_apply_mutations", "sync_brain"} & allowed
    assert "gbrain_think" in tools_for_toolsets({"gbrain_query"})

    prefixed = [SimpleNamespace(name=f"gbrain_{tool.name}") for tool in discovered]
    assert {tool.name for tool in filter_mcp_tools("gbrain", prefixed)} == {
        "gbrain_query",
        "gbrain_think",
        "gbrain_get_page",
    }


def test_other_mcp_servers_are_unchanged() -> None:
    tools = [SimpleNamespace(name="search_patents")]
    assert filter_mcp_tools("zhihuiya_patents", tools) == tools


def test_mcp_catalog_reports_filtered_tools_after_live_probe(monkeypatch) -> None:
    monkeypatch.setattr(
        "api.mcp.load_config",
        lambda: {"mcp": {"enabled": [], "auto_enable_gbrain": True}},
    )
    monkeypatch.setattr(
        "mcp_clients.servers.gbrain_runtime_status",
        lambda: {
            "configured": True,
            "ready": True,
            "reason": "",
            "models": {
                "embedding": {"name": "embed", "provider": "dashscope", "dimension": 1024},
                "think": {"name": "think", "provider": "deepseek"},
            },
        },
    )
    monkeypatch.setattr(
        "mcp_clients.servers.effective_mcp_server_names",
        lambda _enabled, *, auto_enable_gbrain: ["gbrain"],
    )

    async def fake_load(_enabled):
        return [SimpleNamespace(name="gbrain_query"), SimpleNamespace(name="gbrain_think")]

    monkeypatch.setattr("mcp_clients.load_filtered_mcp_tools", fake_load)
    app = FastAPI()
    app.include_router(router)
    payload = TestClient(app).get("/mcp/servers?probe=true").json()
    assert payload["catalog"][0]["status"] == "loaded"
    assert payload["catalog"][0]["auto_enabled"] is True
    assert payload["catalog"][0]["tools"] == ["gbrain_query", "gbrain_think"]
