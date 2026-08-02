from __future__ import annotations

import asyncio
import inspect
import subprocess
import sys
from builtins import ExceptionGroup
from pathlib import Path

import app


def test_app_imports_in_fresh_python_process() -> None:
    backend_dir = Path(__file__).resolve().parents[1]

    result = subprocess.run(
        [sys.executable, "-c", "import app; print('APP_IMPORT_OK')"],
        cwd=backend_dir,
        capture_output=True,
        check=False,
        text=True,
        timeout=30,
    )

    assert result.returncode == 0, result.stderr
    assert "APP_IMPORT_OK" in result.stdout


def test_mcp_warmup_flattens_taskgroup_and_disambiguates_ready_gbrain(
    monkeypatch,
    capsys,
) -> None:
    import config
    import mcp_clients
    from mcp_clients import servers

    monkeypatch.setattr(
        config,
        "load_config",
        lambda: {"mcp": {"enabled": [], "auto_enable_gbrain": True}},
    )
    monkeypatch.setattr(
        servers,
        "effective_mcp_server_names",
        lambda *_args, **_kwargs: ["gbrain"],
    )
    monkeypatch.setattr(
        servers,
        "gbrain_runtime_status",
        lambda: {"ready": True},
    )

    async def failed_discovery(_enabled):
        raise ExceptionGroup(
            "adapter task group",
            [ExceptionGroup("stdio task group", [RuntimeError("connection closed")])],
        )

    monkeypatch.setattr(mcp_clients, "load_filtered_mcp_tools", failed_discovery)

    asyncio.run(app._warm_mcp_discovery())

    output = capsys.readouterr().out
    assert "RuntimeError: connection closed" in output
    assert "first use will retry" in output
    assert "do not run `gbrain init`" in output
    assert "unhandled errors in a TaskGroup" not in output


def test_mcp_warmup_retries_one_transient_stdio_failure(
    monkeypatch,
    capsys,
) -> None:
    import config
    import mcp_clients
    from mcp_clients import servers

    monkeypatch.setattr(
        config,
        "load_config",
        lambda: {"mcp": {"enabled": [], "auto_enable_gbrain": True}},
    )
    monkeypatch.setattr(
        servers,
        "effective_mcp_server_names",
        lambda *_args, **_kwargs: ["gbrain"],
    )
    calls = 0

    async def transient_discovery(_enabled):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("connection closed")
        return [object()] * 17

    monkeypatch.setattr(mcp_clients, "load_filtered_mcp_tools", transient_discovery)

    asyncio.run(app._warm_mcp_discovery(retry_delay_seconds=0))

    output = capsys.readouterr().out
    assert calls == 2
    assert "MCP discovery warmed after one cold-start retry: 17 filtered tools" in output
    assert "warm-up did not complete" not in output
    assert "gbrain init" not in output


def test_lifespan_warms_mcp_after_database_and_before_capabilities() -> None:
    source = inspect.getsource(app.lifespan)

    assert source.index("db_ready = await init_database()") < source.index("await _warm_mcp_discovery()")
    assert source.index("await _warm_mcp_discovery()") < source.index(
        "await capabilities.detect_capabilities(force=True)"
    )
