from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import httpx
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from api import knowledge_sources as sources_api
from knowledge.models import Base, KnowledgeSourceConnection, KnowledgeSyncRun
from knowledge.service import KnowledgeService
from knowledge.sources import (
    create_source_connection,
    enqueue_due_feishu_sync_runs,
    ensure_builtin_source_connections,
    upsert_source_item,
)


def test_source_identity_allows_equal_content_from_different_sources(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("PUDDINGCLAW_KNOWLEDGE_DIR", str(tmp_path / "knowledge"))

    async def run() -> None:
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'sources.db'}")
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        async with sessions() as session:
            first_source = await create_source_connection(
                session,
                connector_key="feishu_wiki",
                name="产品 Wiki",
                auth_type="tenant",
            )
            second_source = await create_source_connection(
                session,
                connector_key="feishu_wiki",
                name="研发 Wiki",
                auth_type="tenant",
            )
            first_item = await upsert_source_item(
                session,
                source=first_source,
                external_id="wiki-node-a",
                external_type="docx",
                title="共同内容 A",
            )
            second_item = await upsert_source_item(
                session,
                source=second_source,
                external_id="wiki-node-b",
                external_type="docx",
                title="共同内容 B",
            )
            await session.commit()

            service = KnowledgeService(tmp_path)
            content = b"# Shared body\n\nThe content hash is intentionally identical.\n"
            first_document, _ = await service.ingest_markdown_upload(
                session,
                filename="a.md",
                content=content,
                source_connection_id=first_source.id,
                source_item_id=first_item.id,
                source_revision="1",
                publish_vector_now=False,
            )
            second_document, _ = await service.ingest_markdown_upload(
                session,
                filename="b.md",
                content=content,
                source_connection_id=second_source.id,
                source_item_id=second_item.id,
                source_revision="7",
                publish_vector_now=False,
            )
            assert first_document.id != second_document.id
            assert first_document.content_sha256 == second_document.content_sha256
            assert first_document.source_item_id == first_item.id
            assert second_document.source_item_id == second_item.id
        await engine.dispose()

    asyncio.run(run())


def test_knowledge_sources_api_bootstraps_builtins_and_rejects_secrets(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'sources-api.db'}")
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        sessions = async_sessionmaker(engine, expire_on_commit=False)

        async def session_override():
            async with sessions() as session:
                yield session

        app = FastAPI()
        app.include_router(sources_api.router, prefix="/api")
        app.dependency_overrides[sources_api.get_db_session] = session_override
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            listed = await client.get("/api/knowledge/sources")
            assert listed.status_code == 200
            assert [source["connector_key"] for source in listed.json()["sources"]] == [
                "local_upload",
                "web_capture",
            ]

            rejected = await client.post(
                "/api/knowledge/sources",
                json={
                    "connector_key": "feishu_wiki",
                    "name": "unsafe",
                    "auth_type": "tenant",
                    "config": {"app_secret": "must-not-enter-json"},
                },
            )
            assert rejected.status_code == 400
            assert "凭据接口" in rejected.json()["detail"]

            rejected_binding = await client.post(
                "/api/knowledge/sources",
                json={
                    "connector_key": "feishu_wiki",
                    "name": "unsafe binding",
                    "auth_type": "user",
                    "config": {"user_grant_id": "fgrant_attacker"},
                },
            )
            assert rejected_binding.status_code == 400
            assert "专用授权接口" in rejected_binding.json()["detail"]

            created = await client.post(
                "/api/knowledge/sources",
                json={
                    "connector_key": "feishu_wiki",
                    "name": "产品知识空间",
                    "auth_type": "user",
                    "config": {"root_node_token": "wikcn-safe-metadata"},
                },
            )
            assert created.status_code == 201
            assert created.json()["source"]["status"] == "pending_auth"
            assert created.json()["source"]["credential_configured"] is False

            remote_source_id = created.json()["source"]["id"]
            protected = await client.patch(
                f"/api/knowledge/sources/{remote_source_id}",
                json={"config": {"app_credential_id": "fapp_attacker"}},
            )
            assert protected.status_code == 400
            assert "专用授权接口" in protected.json()["detail"]
            async with sessions() as session:
                remote_source = await session.get(KnowledgeSourceConnection, remote_source_id)
                assert remote_source is not None
                remote_source.status = "ready"
                remote_source.config_json = {"space_id": "space-test"}
                await session.commit()
            first_remote_sync = await client.post(
                f"/api/knowledge/sources/{remote_source_id}/sync",
                json={"mode": "incremental"},
            )
            assert first_remote_sync.status_code == 202
            duplicate_remote_sync = await client.post(
                f"/api/knowledge/sources/{remote_source_id}/sync",
                json={"mode": "incremental"},
            )
            assert duplicate_remote_sync.status_code == 409
            run_id = first_remote_sync.json()["run"]["id"]
            cancelled = await client.post(
                f"/api/knowledge/sources/{remote_source_id}/runs/{run_id}/cancel"
            )
            assert cancelled.status_code == 200
            assert cancelled.json()["run"]["status"] == "cancelled"

            local_source = listed.json()["sources"][0]
            synced = await client.post(
                f"/api/knowledge/sources/{local_source['id']}/sync",
                json={"mode": "incremental"},
            )
            assert synced.status_code == 202
            assert synced.json()["run"]["status"] == "succeeded"
        await engine.dispose()

    asyncio.run(run())


def test_builtin_source_ids_are_idempotent(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'builtins.db'}")
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        async with sessions() as session:
            first = await ensure_builtin_source_connections(session)
            await session.commit()
            second = await ensure_builtin_source_connections(session)
            assert {key: value.id for key, value in first.items()} == {key: value.id for key, value in second.items()}
        await engine.dispose()

    asyncio.run(run())


def test_scheduled_feishu_sync_is_due_once_and_manual_sources_are_skipped(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'scheduled.db'}")
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        now = datetime.now(timezone.utc)
        async with sessions() as session:
            due = await create_source_connection(
                session,
                connector_key="feishu_wiki",
                name="每小时同步",
                auth_type="tenant",
                config={"space_id": "space-due"},
                schedule={"interval_minutes": 60},
            )
            due.status = "ready"
            due.last_synced_at = now - timedelta(hours=2)
            manual = await create_source_connection(
                session,
                connector_key="feishu_wiki",
                name="仅手动",
                auth_type="tenant",
                config={"space_id": "space-manual"},
                schedule={"interval_minutes": 0},
            )
            manual.status = "ready"
            await session.commit()

            first = await enqueue_due_feishu_sync_runs(session, now=now)
            await session.commit()
            second = await enqueue_due_feishu_sync_runs(session, now=now)
            assert len(first) == 1
            assert first[0].source_connection_id == due.id
            assert second == []
            assert await session.get(KnowledgeSyncRun, first[0].id) is not None
            assert manual.status == "ready"
        await engine.dispose()

    asyncio.run(run())
