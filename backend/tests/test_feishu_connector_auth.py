from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from api import feishu_connector as feishu_api
from knowledge.connectors import feishu
from knowledge.connectors.feishu_bitable import FeishuBitableReference
from knowledge.models import Base, FeishuAppCredential, KnowledgeSourceConnection
from knowledge.sources import create_source_connection, upsert_source_item
from tools import feishu_bitable_tools


def test_feishu_app_secret_stays_in_vault_and_tenant_token_is_singleflight(tmp_path) -> None:
    async def run() -> None:
        requests: list[dict[str, object]] = []

        async def handler(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content)
            requests.append(body)
            assert body == {"app_id": "cli_test_app_123", "app_secret": "super-secret-value"}
            return httpx.Response(
                200,
                json={"code": 0, "tenant_access_token": "tenant-token-must-not-leak", "expire": 7200},
            )

        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'feishu.db'}")
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        broker = feishu.TenantTokenBroker(
            feishu.FeishuHttpClient(transport=httpx.MockTransport(handler))
        )
        async with sessions() as session:
            app = await feishu.save_feishu_app(
                session,
                app_id="cli_test_app_123",
                app_secret="super-secret-value",
                app_name="测试应用",
            )
            await session.commit()
            stored = await session.get(FeishuAppCredential, app.id)
            assert stored is not None
            assert stored.app_id_masked != "cli_test_app_123"
            assert stored.credential_ref.startswith("vault://")
            assert "secret" not in stored.credential_ref

            first, second = await asyncio.gather(
                broker.get(session, stored),
                broker.get(session, stored),
            )
            assert first == second == "tenant-token-must-not-leak"
            assert len(requests) == 1
            public = feishu.feishu_app_to_dict(stored)
            assert "tenant-token-must-not-leak" not in json.dumps(public)
            assert "super-secret-value" not in json.dumps(public)
        await engine.dispose()

    asyncio.run(run())


def test_feishu_app_api_never_echoes_secret_or_token(tmp_path, monkeypatch) -> None:
    async def run() -> None:
        async def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={"code": 0, "tenant_access_token": "t-ultra-secret", "expire": 7200},
            )

        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'feishu-api.db'}")
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        sessions = async_sessionmaker(engine, expire_on_commit=False)

        async def session_override():
            async with sessions() as session:
                yield session

        broker = feishu.TenantTokenBroker(feishu.FeishuHttpClient(transport=httpx.MockTransport(handler)))
        monkeypatch.setattr(feishu_api, "tenant_token_broker", broker)
        app = FastAPI()
        app.include_router(feishu_api.router, prefix="/api")
        app.dependency_overrides[feishu_api.get_db_session] = session_override
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            created = await client.post(
                "/api/knowledge/feishu/apps",
                json={
                    "app_id": "cli_test_app_456",
                    "app_secret": "never-echo-this-secret",
                    "app_name": "产品知识应用",
                },
            )
            assert created.status_code == 201
            body = created.text
            assert "never-echo-this-secret" not in body
            app_id = created.json()["app"]["id"]

            tested = await client.post(f"/api/knowledge/feishu/apps/{app_id}/test")
            assert tested.status_code == 200
            assert "t-ultra-secret" not in tested.text
            assert tested.json()["app"]["status"] == "ready"

            async with sessions() as session:
                row = (await session.execute(select(FeishuAppCredential))).scalar_one()
                serialized = json.dumps(
                    {column.name: getattr(row, column.name) for column in FeishuAppCredential.__table__.columns},
                    default=str,
                )
                assert "never-echo-this-secret" not in serialized
                assert "t-ultra-secret" not in serialized
        await engine.dispose()

    asyncio.run(run())


def test_feishu_api_base_rejects_credential_exfiltration_hosts() -> None:
    for value in ("http://open.feishu.cn", "https://evil.example", "https://user:pass@open.feishu.cn"):
        try:
            feishu.normalize_api_base(value)
        except feishu.FeishuConnectorError:
            pass
        else:
            raise AssertionError(f"unsafe Feishu API base accepted: {value}")

    try:
        feishu._validate_redirect_uri("https://attacker.example/oauth/callback")
    except feishu.FeishuConnectorError as exc:
        assert "允许列表" in str(exc)
    else:
        raise AssertionError("unregistered OAuth redirect URI accepted")


def test_user_oauth_state_pkce_and_refresh_rotation_are_vault_only(tmp_path) -> None:
    async def run() -> None:
        calls: list[tuple[str, dict[str, object]]] = []

        async def handler(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content) if request.content else {}
            calls.append((request.url.path, body))
            if request.url.path.endswith("/oauth/token"):
                if body.get("grant_type") == "authorization_code":
                    assert body.get("code_verifier")
                    return httpx.Response(
                        200,
                        json={
                            "code": 0,
                            "access_token": "user-access-initial",
                            "refresh_token": "user-refresh-initial",
                            "expires_in": 60,
                            "refresh_token_expires_in": 3600,
                            "scope": "wiki:wiki:readonly offline_access",
                        },
                    )
                assert body.get("refresh_token") == "user-refresh-initial"
                return httpx.Response(
                    200,
                    json={
                        "code": 0,
                        "access_token": "user-access-rotated",
                        "refresh_token": "user-refresh-rotated",
                        "expires_in": 7200,
                        "refresh_token_expires_in": 7200,
                        "scope": "wiki:wiki:readonly offline_access",
                    },
                )
            if request.url.path.endswith("/user_info"):
                assert request.headers["authorization"] == "Bearer user-access-initial"
                return httpx.Response(
                    200,
                    json={
                        "code": 0,
                        "data": {"open_id": "ou_sensitive_identity", "union_id": "on_union", "tenant_key": "tenant-a"},
                    },
                )
            raise AssertionError(f"unexpected request: {request.url}")

        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'oauth.db'}")
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        client = feishu.FeishuHttpClient(transport=httpx.MockTransport(handler))
        async with sessions() as session:
            app = await feishu.save_feishu_app(
                session,
                app_id="cli_oauth_app_123",
                app_secret="oauth-app-secret",
            )
            source = await create_source_connection(
                session,
                connector_key="feishu_wiki",
                name="用户知识空间",
                auth_type="user",
            )
            started = await feishu.start_user_oauth(
                session,
                app=app,
                source=source,
                redirect_uri="http://127.0.0.1:3000/knowledge/feishu/oauth/callback",
                principal_id="browser:test-binding",
            )
            await session.commit()
            query = parse_qs(urlparse(started["authorization_url"]).query)
            state = query["state"][0]
            assert query["code_challenge_method"] == ["S256"]
            assert "offline_access" in query["scope"][0]

            oauth_row = (await session.execute(select(feishu.FeishuOAuthSession))).scalar_one()
            assert oauth_row.state_hash != state
            assert oauth_row.verifier_credential_ref.startswith("vault://")

            try:
                await feishu.complete_user_oauth(
                    session,
                    state=state,
                    code="stolen-auth-code",
                    expected_principal_id="browser:different-binding",
                    http_client=client,
                )
            except feishu.FeishuConnectorError as exc:
                assert "浏览器会话" in str(exc)
            else:
                raise AssertionError("OAuth state must be bound to the initiating browser")

            grant, bound_source, cleanup_refs = await feishu.complete_user_oauth(
                session,
                state=state,
                code="single-use-auth-code",
                expected_principal_id="browser:test-binding",
                http_client=client,
            )
            await session.commit()
            feishu.cleanup_oauth_credentials(cleanup_refs)
            assert bound_source.status == "ready"
            assert grant.token_credential_ref.startswith("vault://")
            public = json.dumps(feishu.feishu_user_grant_to_dict(grant))
            assert "user-access-initial" not in public
            assert "user-refresh-initial" not in public

            try:
                await feishu.complete_user_oauth(
                    session,
                    state=state,
                    code="replayed-code",
                    http_client=client,
                )
            except feishu.FeishuConnectorError as exc:
                assert "已经使用" in str(exc)
            else:
                raise AssertionError("OAuth state replay must fail")

            grant.access_expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
            await session.commit()
            old_reference = grant.token_credential_ref
            broker = feishu.UserTokenBroker(client)
            token = await broker.get(session, grant)
            assert token == "user-access-rotated"
            assert grant.token_credential_ref != old_reference
            assert feishu._credential_store().get(old_reference) == ""
            second = await broker.get(session, grant)
            assert second == "user-access-rotated"
            assert sum(1 for path, body in calls if path.endswith("/oauth/token") and body.get("grant_type") == "refresh_token") == 1

            serialized = json.dumps(
                {column.name: getattr(grant, column.name) for column in grant.__table__.columns},
                default=str,
            )
            assert "user-access" not in serialized
            assert "user-refresh" not in serialized
        await engine.dispose()

    asyncio.run(run())


def test_editing_source_can_switch_auth_identity(tmp_path, monkeypatch) -> None:
    async def run() -> None:
        async def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={"code": 0, "tenant_access_token": "t-switch-secret", "expire": 7200},
            )

        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'feishu-switch.db'}")
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        sessions = async_sessionmaker(engine, expire_on_commit=False)

        async def session_override():
            async with sessions() as session:
                yield session

        broker = feishu.TenantTokenBroker(feishu.FeishuHttpClient(transport=httpx.MockTransport(handler)))
        monkeypatch.setattr(feishu_api, "tenant_token_broker", broker)
        app = FastAPI()
        app.include_router(feishu_api.router, prefix="/api")
        app.dependency_overrides[feishu_api.get_db_session] = session_override

        async with sessions() as session:
            feishu_app = await feishu.save_feishu_app(
                session,
                app_id="cli_switch_app_123",
                app_secret="switch-app-secret",
            )
            source = await create_source_connection(
                session,
                connector_key="feishu_wiki",
                name="可切换身份的知识空间",
                auth_type="user",
            )
            grant = feishu.FeishuUserGrant(
                id="fgrant_switch_test",
                app_credential_id=feishu_app.id,
                source_connection_id=source.id,
                principal_id="local",
                token_credential_ref="vault://unused",
            )
            session.add(grant)
            source.config_json = {
                **(source.config_json or {}),
                "app_credential_id": feishu_app.id,
                "user_grant_id": grant.id,
            }
            source.credential_ref = f"feishu-user-grant:{grant.id}"
            source.status = "ready"
            await session.commit()
            app_credential_id = feishu_app.id
            source_id = source.id
            grant_id = grant.id

        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            # user -> tenant: bind must succeed, detach the grant, mark it stale.
            bound = await client.post(
                f"/api/knowledge/feishu/sources/{source_id}/tenant-auth",
                json={"app_credential_id": app_credential_id},
            )
            assert bound.status_code == 200
            assert bound.json()["source"]["auth_type"] == "tenant"

            async with sessions() as session:
                switched = await session.get(KnowledgeSourceConnection, source_id)
                assert switched is not None
                assert switched.auth_type == "tenant"
                assert switched.status == "ready"
                assert switched.credential_ref == f"feishu-app:{app_credential_id}"
                assert "user_grant_id" not in (switched.config_json or {})
                stale_grant = await session.get(feishu.FeishuUserGrant, grant_id)
                assert stale_grant is not None
                assert stale_grant.status == "needs_reauth"

            # tenant -> user: oauth/start must flip the source to pending_auth.
            started = await client.post(
                f"/api/knowledge/feishu/sources/{source_id}/oauth/start",
                json={
                    "app_credential_id": app_credential_id,
                    "redirect_uri": "http://127.0.0.1:3000/knowledge/feishu/oauth/callback",
                },
            )
            assert started.status_code == 200
            assert "authorization_url" in started.json()

            async with sessions() as session:
                switched_back = await session.get(KnowledgeSourceConnection, source_id)
                assert switched_back is not None
                assert switched_back.auth_type == "user"
                assert switched_back.status == "pending_auth"
                assert switched_back.credential_ref == ""
        await engine.dispose()

    asyncio.run(run())


def test_bitable_relation_api_uses_stable_schema_ids_and_rejects_duplicates(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'bitable-relations.db'}")
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        sessions = async_sessionmaker(engine, expire_on_commit=False)

        async def session_override():
            async with sessions() as session:
                yield session

        app = FastAPI()
        app.include_router(feishu_api.router, prefix="/api")
        app.dependency_overrides[feishu_api.get_db_session] = session_override

        async with sessions() as session:
            source = await create_source_connection(
                session,
                connector_key="feishu_wiki",
                name="社区数据",
                auth_type="tenant",
                config={
                    "source_mode": "bitable",
                    "app_token": "base-token",
                    "table_ids": ["members", "content"],
                },
            )
            source.status = "ready"
            await upsert_source_item(
                session,
                source=source,
                external_id="bitable:base-token:members",
                external_type="bitable",
                title="社区成员",
                status="linked",
                metadata={
                    "app_token": "base-token",
                    "table_id": "members",
                    "row_storage": False,
                    "fields": [{"field_id": "member-id", "field_name": "member_id", "type": 1, "is_primary": True}],
                },
            )
            await upsert_source_item(
                session,
                source=source,
                external_id="bitable:base-token:content",
                external_type="bitable",
                title="内容发布",
                status="linked",
                metadata={
                    "app_token": "base-token",
                    "table_id": "content",
                    "row_storage": False,
                    "fields": [{"field_id": "author-id", "field_name": "author_id", "type": 1}],
                },
            )
            await session.commit()
            source_id = source.id

        payload = {
            "name": "内容作者",
            "source_table_id": "content",
            "source_field_id": "author-id",
            "target_table_id": "members",
            "target_field_id": "member-id",
            "cardinality": "many_to_one",
            "on_target_delete": "retain_orphans",
        }
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            created = await client.post(f"/api/knowledge/feishu/sources/{source_id}/bitable/relations", json=payload)
            assert created.status_code == 201
            relation = created.json()["relation"]
            assert relation["source_table_name"] == "内容发布"
            assert relation["target_field_name"] == "member_id"
            assert relation["validation_status"] == "schema_valid"
            assert relation["validation_scope"] == "schema_only"
            assert relation["row_values_stored"] is False

            duplicate = await client.post(
                f"/api/knowledge/feishu/sources/{source_id}/bitable/relations",
                json={
                    **payload,
                    "source_table_id": "members",
                    "source_field_id": "member-id",
                    "target_table_id": "content",
                    "target_field_id": "author-id",
                },
            )
            assert duplicate.status_code == 409

            listed = await client.get(f"/api/knowledge/feishu/sources/{source_id}/bitable/relations")
            assert [item["id"] for item in listed.json()["relations"]] == [relation["id"]]

            deleted = await client.delete(
                f"/api/knowledge/feishu/sources/{source_id}/bitable/relations/{relation['id']}"
            )
            assert deleted.status_code == 200
            listed_after = await client.get(f"/api/knowledge/feishu/sources/{source_id}/bitable/relations")
            assert listed_after.json()["relations"] == []
        await engine.dispose()

    asyncio.run(run())


def test_bitable_scope_explicit_empty_selection_is_preserved(tmp_path, monkeypatch) -> None:
    async def run() -> None:
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'bitable-empty-scope-api.db'}")
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        sessions = async_sessionmaker(engine, expire_on_commit=False)

        async def session_override():
            async with sessions() as session:
                yield session

        async def fake_resolve(_session, _source, *, url, api):
            return (
                FeishuBitableReference(
                    original_url=url,
                    entry_kind="direct_bitable",
                    app_token="base-token",
                    table_id="members",
                ),
                [
                    {"table_id": "members", "name": "社区成员"},
                    {"table_id": "content", "name": "内容发布"},
                ],
            )

        monkeypatch.setattr(feishu_api, "resolve_feishu_bitable_reference", fake_resolve)
        app = FastAPI()
        app.include_router(feishu_api.router, prefix="/api")
        app.dependency_overrides[feishu_api.get_db_session] = session_override

        async with sessions() as session:
            source = await create_source_connection(
                session,
                connector_key="feishu_wiki",
                name="社区数据",
                auth_type="tenant",
                config={
                    "source_mode": "bitable",
                    "app_token": "base-token",
                    "table_id": "members",
                    "table_ids": ["members", "content"],
                },
            )
            source.status = "ready"
            await session.commit()
            source_id = source.id

        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.put(
                f"/api/knowledge/feishu/sources/{source_id}/bitable/scope",
                json={
                    "url": "https://example.feishu.cn/base/base-token?table=members",
                    "table_id": "members",
                    "table_ids": [],
                },
            )
            assert response.status_code == 200
            config = response.json()["source"]["config"]
            assert config["table_ids"] == []
            assert config["tables"] == []
            assert config["table_id"] == ""
            assert config["table_name"] == ""
        await engine.dispose()

    asyncio.run(run())


def test_agent_rejects_visible_but_unselected_bitable_sheet(tmp_path, monkeypatch) -> None:
    async def run() -> None:
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'bitable-agent-scope.db'}")
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        monkeypatch.setattr(feishu_bitable_tools, "get_sessionmaker", lambda: sessions)

        class FakeApi:
            async def list_bitable_tables(self, _session, _source, *, app_token):
                assert app_token == "base-token"
                return [
                    {"table_id": "members", "name": "社区成员"},
                    {"table_id": "content", "name": "内容发布"},
                ]

        async with sessions() as session:
            source = await create_source_connection(
                session,
                connector_key="feishu_wiki",
                name="社区数据",
                auth_type="tenant",
                config={
                    "source_mode": "bitable",
                    "app_token": "base-token",
                    "table_id": "members",
                    "table_ids": ["members"],
                },
            )
            source.status = "ready"
            await session.commit()
            source_id = source.id

        with pytest.raises(feishu.FeishuConnectorError, match="当前可见范围"):
            await feishu_bitable_tools._registered_locator(
                source_id,
                "",
                "content",
                api=FakeApi(),
            )

        source, locator, candidates, opened_session = await feishu_bitable_tools._registered_locator(
            source_id,
            "",
            "members",
            api=FakeApi(),
        )
        try:
            assert source.id == source_id
            assert locator["table_id"] == "members"
            assert [table["table_id"] for table in candidates] == ["members"]
        finally:
            await opened_session.close()
        await engine.dispose()

    asyncio.run(run())
