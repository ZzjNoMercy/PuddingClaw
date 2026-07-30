from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from knowledge.brain_schema import BrainSchemaService
from knowledge.llm_wiki import LlmWikiService
from mcp_clients import load_filtered_mcp_tools


def _wiki_page(raw_path: str) -> str:
    return (
        "---\n"
        "title: PostgreSQL MCP E2E\n"
        "type: concept\n"
        "sources:\n"
        f"  - {raw_path}\n"
        "created: '2026-07-30'\n"
        "updated: '2026-07-30'\n"
        "schema_version: 0.1.0\n"
        "---\n\n"
        "# PostgreSQL MCP E2E\n\n"
        "PuddingClaw 把 raw 编译成 Wiki，并由 gbrain 提供筛选后的只读查询。\n"
    )


@pytest.mark.asyncio
async def test_raw_to_wiki_to_postgres_to_filtered_mcp(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Opt-in real E2E; requires an isolated pgvector database URL."""

    database_url = os.getenv("PUDDINGCLAW_GBRAIN_E2E_DATABASE_URL", "").strip()
    binary = shutil.which(os.getenv("PUDDINGCLAW_GBRAIN_BIN", "gbrain"))
    if not database_url or not binary:
        pytest.skip("set PUDDINGCLAW_GBRAIN_E2E_DATABASE_URL and install gbrain")

    runtime_home = tmp_path / "gbrain-home"
    environment = os.environ.copy()
    environment.pop("DATABASE_URL", None)
    environment.pop("GBRAIN_DATABASE_URL", None)
    environment["GBRAIN_HOME"] = str(runtime_home)
    initialized = subprocess.run(
        [
            binary,
            "init",
            "--url",
            database_url,
            "--non-interactive",
            "--no-embedding",
            "--skip-embed-check",
            "--schema-pack",
            "gbrain-base-v2",
            "--json",
        ],
        capture_output=True,
        text=True,
        env=environment,
        timeout=180,
        check=False,
    )
    assert initialized.returncode == 0, initialized.stderr[-4000:]

    monkeypatch.setenv("PUDDINGCLAW_KNOWLEDGE_DIR", str(tmp_path / "knowledge"))
    monkeypatch.setenv("PUDDINGCLAW_GBRAIN_HOME", str(runtime_home))
    monkeypatch.setenv("PUDDINGCLAW_GBRAIN_BIN", binary)

    base_dir = Path(__file__).resolve().parents[2]
    schema = BrainSchemaService(base_dir)
    schema.initialize()
    wiki = LlmWikiService(base_dir)
    raw = wiki.snapshot_raw(
        source_id="e2e",
        asset_id="postgres-mcp",
        title="PostgreSQL MCP E2E source",
        content="# Source\n\nPostgreSQL + gbrain MCP E2E evidence.\n",
    )
    wiki.publish(
        pages=[{"slug": "postgres-mcp-e2e", "content": _wiki_page(raw["snapshot_path"])}],
        expected_bundle_hash=schema.bundle()["bundle_hash"],
        summary="real PostgreSQL and MCP E2E",
        model="pytest:e2e",
        raw_paths=[raw["snapshot_path"]],
    )
    compiled = wiki.compile_gbrain(import_pages=True)
    assert compiled["ok"] is True, compiled

    tools = await load_filtered_mcp_tools(["gbrain"])
    tool_names = {tool.name for tool in tools}
    assert len(tool_names) == 16
    assert "gbrain_get_active_schema_pack" in tool_names
    assert "gbrain_search" in tool_names
    assert not {"gbrain_put_page", "gbrain_delete_page", "gbrain_schema_apply_mutations"} & tool_names

    active_tool = next(tool for tool in tools if tool.name == "gbrain_get_active_schema_pack")
    active = await active_tool.ainvoke({})
    assert "puddingclaw-wiki" in str(active)

    search_tool = next(tool for tool in tools if tool.name == "gbrain_search")
    result = await search_tool.ainvoke({"query": "PostgreSQL MCP E2E"})
    assert "postgres-mcp-e2e" in str(result)
