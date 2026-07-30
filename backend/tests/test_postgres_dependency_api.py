from __future__ import annotations

import pytest

from api.config_api import DatabaseConnectionRequest, _test_database_connection


@pytest.mark.asyncio
async def test_database_connection_reports_missing_pgvector(monkeypatch) -> None:
    import asyncpg

    class FakeConnection:
        async def fetchval(self, _query):
            return "PostgreSQL 16.13"

        async def fetchrow(self, _query):
            return {
                "server_version_num": 160013,
                "available": False,
                "default_version": None,
                "installed_version": None,
            }

        async def close(self):
            return None

    async def fake_connect(**_kwargs):
        return FakeConnection()

    monkeypatch.setattr(asyncpg, "connect", fake_connect)
    result = await _test_database_connection(
        DatabaseConnectionRequest(
            host="127.0.0.1",
            port=5432,
            database="llm_wiki",
            username="pet",
        )
    )
    assert result["success"] is True
    assert result["pgvector"]["available"] is False
    assert result["pgvector"]["install_command"] == "./scripts/start-local-infra.sh"
