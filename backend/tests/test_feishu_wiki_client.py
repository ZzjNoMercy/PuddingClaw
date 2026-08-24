from __future__ import annotations

import asyncio

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from knowledge.connectors.feishu import FeishuConnectorError, FeishuHttpClient, FeishuOpenApi
from knowledge.connectors.feishu_bitable import parse_feishu_bitable_url
from knowledge.connectors.feishu_blocks import convert_feishu_blocks_to_markdown
from knowledge.connectors.feishu_sync import process_feishu_sync_run
from knowledge.models import Base, FeishuAppCredential, KnowledgeDocument, KnowledgeSourceItem
from knowledge.sources import create_source_connection, create_sync_run


def test_bitable_links_normalize_direct_and_wiki_entries() -> None:
    direct = parse_feishu_bitable_url(
        "https://example.feishu.cn/base/bascnExample123?table=tblExample123&view=vewExample123"
    )
    assert direct.entry_kind == "direct_bitable"
    assert direct.app_token == "bascnExample123"
    assert direct.table_id == "tblExample123"
    assert direct.view_id == "vewExample123"

    wiki = parse_feishu_bitable_url("https://example.feishu.cn/wiki/wikcnExample123?table=tblExample123")
    assert wiki.entry_kind == "wiki_bitable"
    assert wiki.node_token == "wikcnExample123"
    assert wiki.app_token == ""

    with pytest.raises(FeishuConnectorError, match="官方租户域名"):
        parse_feishu_bitable_url("https://feishu.cn.attacker.invalid/base/bascnExample123")


def test_bitable_open_api_reads_one_bounded_record_page(tmp_path) -> None:
    async def run() -> None:
        observed: dict[str, str] = {}

        async def handler(request: httpx.Request) -> httpx.Response:
            observed.update(dict(request.url.params))
            return httpx.Response(
                200,
                json={"code": 0, "data": {"items": [{"record_id": "rec1", "fields": {"名称": "示例"}}], "has_more": True, "page_token": "next", "total": 2}},
            )

        class StubTenantBroker:
            async def get(self, _session, _app, *, force_refresh: bool = False):
                return "token"

        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'bitable-api.db'}")
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        api = FeishuOpenApi(
            http_client=FeishuHttpClient(transport=httpx.MockTransport(handler)),
            tenant_broker=StubTenantBroker(),
        )
        async with sessions() as session:
            app = FeishuAppCredential(id="fapp-bitable", app_id_masked="cli_••••test", credential_ref="unused", api_base_url="https://open.feishu.cn")
            session.add(app)
            source = await create_source_connection(session, connector_key="feishu_wiki", name="Base", auth_type="tenant", config={"app_credential_id": app.id})
            source.status = "ready"
            await session.commit()
            result = await api.list_bitable_records_page(
                session,
                source,
                app_token="bascnExample123",
                table_id="tblExample123",
                view_id="vewExample123",
                page_size=500,
                field_names=["名称"],
            )
            assert result["items"][0]["record_id"] == "rec1"
            assert result["page_token"] == "next"
            assert observed["page_size"] == "100"
            assert observed["view_id"] == "vewExample123"
        await engine.dispose()

    asyncio.run(run())


def test_wiki_bitable_node_is_registered_as_live_link_not_document(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("PUDDINGCLAW_KNOWLEDGE_DIR", str(tmp_path / "knowledge"))

    async def run() -> None:
        class FakeApi:
            async def list_nodes(self, _session, _source, *, space_id, parent_node_token=None):
                return [{"space_id": space_id, "node_token": "wiki-base", "obj_token": "base-token", "obj_type": "bitable", "title": "项目台账", "has_child": False, "obj_edit_time": "1700000000"}]

        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'bitable-sync.db'}")
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        async with sessions() as session:
            source = await create_source_connection(session, connector_key="feishu_wiki", name="Wiki", auth_type="tenant", config={"space_id": "space-1", "publish_vector": False})
            source.status = "ready"
            run_ = await create_sync_run(session, source=source, mode="incremental")
            await session.commit()
            await process_feishu_sync_run(session, base_dir=tmp_path, run=run_, api=FakeApi())
            item = (await session.execute(select(KnowledgeSourceItem))).scalar_one()
            assert item.status == "linked"
            assert item.external_type == "bitable"
            assert item.metadata_json["app_token"] == "base-token"
            assert run_.stats_json["linked"] == 1
            assert (await session.execute(select(KnowledgeDocument))).scalars().all() == []
        await engine.dispose()

    asyncio.run(run())


def test_direct_bitable_sync_refreshes_schema_without_reading_records(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("PUDDINGCLAW_KNOWLEDGE_DIR", str(tmp_path / "knowledge"))

    async def run() -> None:
        class FakeApi:
            record_calls = 0

            async def list_bitable_fields(self, _session, _source, *, app_token, table_id):
                assert app_token == "base-token"
                assert table_id == "table-token"
                return [{"field_id": "fld1", "field_name": "项目", "type": 1}]

            async def list_bitable_records_page(self, *_args, **_kwargs):
                self.record_calls += 1
                raise AssertionError("schema refresh must not read records")

        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'bitable-direct-sync.db'}")
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        api = FakeApi()
        async with sessions() as session:
            source = await create_source_connection(
                session,
                connector_key="feishu_wiki",
                name="项目台账",
                auth_type="tenant",
                config={
                    "source_mode": "bitable",
                    "app_token": "base-token",
                    "table_id": "table-token",
                    "table_name": "项目",
                    "source_url": "https://example.feishu.cn/base/base-token?table=table-token",
                },
            )
            source.status = "ready"
            run_ = await create_sync_run(session, source=source, mode="incremental")
            await session.commit()
            await process_feishu_sync_run(session, base_dir=tmp_path, run=run_, api=api)
            item = (await session.execute(select(KnowledgeSourceItem))).scalar_one()
            assert item.status == "linked"
            assert item.metadata_json["row_storage"] is False
            assert item.metadata_json["fields"][0]["field_name"] == "项目"
            assert run_.stats_json["schema_changed"] == 1
            assert api.record_calls == 0
        await engine.dispose()

    asyncio.run(run())


def test_feishu_open_api_refreshes_once_and_paginates_empty_filtered_pages(tmp_path) -> None:
    async def run() -> None:
        requests = 0

        async def handler(request: httpx.Request) -> httpx.Response:
            nonlocal requests
            requests += 1
            if request.headers["authorization"] == "Bearer expired-token":
                return httpx.Response(401, json={"code": 99991663, "msg": "invalid token"})
            page_token = request.url.params.get("page_token")
            if not page_token:
                # Feishu explicitly permits an empty permission-filtered page
                # while has_more remains true.
                return httpx.Response(
                    200,
                    json={"code": 0, "data": {"items": [], "has_more": True, "page_token": "next"}},
                )
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "data": {
                        "items": [{"space_id": "space-1", "name": "产品知识库"}],
                        "has_more": False,
                    },
                },
            )

        class StubTenantBroker:
            def __init__(self) -> None:
                self.calls: list[bool] = []
                self.refreshed = False

            async def get(self, _session, _app, *, force_refresh: bool = False):
                self.calls.append(force_refresh)
                if force_refresh:
                    self.refreshed = True
                return "fresh-token" if self.refreshed else "expired-token"

        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'wiki.db'}")
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        broker = StubTenantBroker()
        api = FeishuOpenApi(
            http_client=FeishuHttpClient(transport=httpx.MockTransport(handler)),
            tenant_broker=broker,
        )
        async with sessions() as session:
            app = FeishuAppCredential(
                id="fapp-test",
                app_id_masked="cli_••••test",
                credential_ref="vault://unused-in-stub",
                api_base_url="https://open.feishu.cn",
            )
            session.add(app)
            source = await create_source_connection(
                session,
                connector_key="feishu_wiki",
                name="产品知识库",
                auth_type="tenant",
                config={"app_credential_id": app.id},
            )
            source.status = "ready"
            await session.commit()
            spaces = await api.list_spaces(session, source)
            assert spaces == [{"space_id": "space-1", "name": "产品知识库"}]
            assert broker.calls == [False, True, False]
            assert requests == 3
        await engine.dispose()

    asyncio.run(run())


def test_docx_block_tree_converts_structure_and_surfaces_assets() -> None:
    blocks = [
        {"block_id": "page", "block_type": 1, "children": ["h1", "p1", "todo", "img", "table"]},
        {
            "block_id": "h1",
            "block_type": 3,
            "heading1": {"elements": [{"text_run": {"content": "产品手册"}}]},
        },
        {
            "block_id": "p1",
            "block_type": 2,
            "text": {
                "elements": [
                    {"text_run": {"content": "查看 ", "text_element_style": {}}},
                    {
                        "text_run": {
                            "content": "官方说明",
                            "text_element_style": {"bold": True, "link": {"url": "https://example.com"}},
                        }
                    },
                ]
            },
        },
        {
            "block_id": "todo",
            "block_type": 17,
            "todo": {
                "elements": [{"text_run": {"content": "完成同步"}}],
                "style": {"done": True},
            },
        },
        {"block_id": "img", "block_type": 27, "image": {"token": "img-token"}},
        {
            "block_id": "table",
            "block_type": 31,
            "children": ["cell1", "cell2"],
            "table": {"property": {"row_size": 1, "column_size": 2}},
        },
        {"block_id": "cell1", "block_type": 32, "children": ["cell1text"]},
        {"block_id": "cell2", "block_type": 32, "children": ["cell2text"]},
        {
            "block_id": "cell1text",
            "block_type": 2,
            "text": {"elements": [{"text_run": {"content": "字段"}}]},
        },
        {
            "block_id": "cell2text",
            "block_type": 2,
            "text": {"elements": [{"text_run": {"content": "说明"}}]},
        },
    ]
    result = convert_feishu_blocks_to_markdown(blocks)
    assert "# 产品手册" in result.markdown
    assert "[**官方说明**](https://example.com)" in result.markdown
    assert "- [x] 完成同步" in result.markdown
    assert "| 字段 | 说明 |" in result.markdown
    assert result.assets == [
        {
            "type": "image",
            "token": "img-token",
            "block_id": "img",
            "filename": "feishu-image-img.bin",
        }
    ]


def test_feishu_incremental_revision_hash_and_full_scan_deletion(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("PUDDINGCLAW_KNOWLEDGE_DIR", str(tmp_path / "knowledge"))

    async def run() -> None:
        class FakeApi:
            def __init__(self) -> None:
                self.revision = 1
                self.body = "第一版正文"
                self.nodes = [
                    {
                        "space_id": "space-1",
                        "node_token": "wiki-node-1",
                        "obj_token": "docx-1",
                        "obj_type": "docx",
                        "title": "产品手册",
                        "has_child": False,
                        "obj_edit_time": "1700000000",
                    }
                ]
                self.block_calls = 0

            async def list_nodes(self, _session, _source, *, space_id, parent_node_token=None):
                assert space_id == "space-1"
                assert parent_node_token is None
                return list(self.nodes)

            async def get_node(self, *_args, **_kwargs):
                raise AssertionError("root node lookup not expected")

            async def get_docx_document(self, _session, _source, *, document_id):
                assert document_id == "docx-1"
                return {"title": "产品手册", "revision_id": self.revision}

            async def list_docx_blocks(self, _session, _source, *, document_id, document_revision_id):
                self.block_calls += 1
                assert document_id == "docx-1"
                assert document_revision_id == self.revision
                return [
                    {"block_id": "page", "block_type": 1, "children": ["text"]},
                    {
                        "block_id": "text",
                        "block_type": 2,
                        "text": {"elements": [{"text_run": {"content": self.body}}]},
                    },
                ]

            async def get_doc_meta(self, _session, _source, *, doc_token, doc_type="docx"):
                return {}

            async def get_user_display_names(self, _session, _source, *, open_ids):
                return {}

        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'sync.db'}")
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        api = FakeApi()
        async with sessions() as session:
            source = await create_source_connection(
                session,
                connector_key="feishu_wiki",
                name="产品知识库",
                auth_type="tenant",
                config={"space_id": "space-1", "publish_vector": False},
            )
            source.status = "ready"
            first = await create_sync_run(session, source=source, mode="incremental")
            await session.commit()
            await process_feishu_sync_run(session, base_dir=tmp_path, run=first, api=api)
            assert first.status == "succeeded"
            assert first.stats_json["changed"] == 1
            item = (await session.execute(select(KnowledgeSourceItem))).scalar_one()
            document = (await session.execute(select(KnowledgeDocument))).scalar_one()
            first_storage_path = Path(document.storage_path)
            assert item.revision == "1"
            assert document.source_item_id == item.id
            assert document.source_type == "feishu_docx"
            assert api.block_calls == 1

            api.nodes[0]["title"] = "产品手册（新位置）"
            second = await create_sync_run(session, source=source, mode="incremental")
            await session.commit()
            await process_feishu_sync_run(session, base_dir=tmp_path, run=second, api=api)
            assert second.stats_json["unchanged"] == 1
            assert api.block_calls == 1  # revision fast path avoided block fetch
            assert document.doc_metadata["feishu"]["wiki_path"] == ["产品手册（新位置）"]

            api.revision = 2
            third = await create_sync_run(session, source=source, mode="incremental")
            await session.commit()
            await process_feishu_sync_run(session, base_dir=tmp_path, run=third, api=api)
            assert third.stats_json["unchanged"] == 1  # revision changed, normalized body hash did not
            assert item.revision == "2"
            assert api.block_calls == 2

            api.revision = 3
            api.body = "第二版正文"
            fourth = await create_sync_run(session, source=source, mode="incremental")
            await session.commit()
            await process_feishu_sync_run(session, base_dir=tmp_path, run=fourth, api=api)
            assert fourth.stats_json["changed"] == 1
            assert item.document_id == document.id
            assert Path(document.storage_path).exists()
            assert Path(document.storage_path) != first_storage_path
            assert not first_storage_path.exists()

            monkeypatch.setattr(
                "knowledge.connectors.feishu_sync.refresh_local_knowledge_index",
                lambda _base_dir: {"refreshed": True},
            )
            calls_before_reindex = api.block_calls
            reindex = await create_sync_run(session, source=source, mode="reindex")
            await session.commit()
            await process_feishu_sync_run(session, base_dir=tmp_path, run=reindex, api=api)
            assert reindex.status == "succeeded"
            assert reindex.stats_json["vector_index"]["refreshed"] is True
            assert api.block_calls == calls_before_reindex  # reindex is purely local

            api.nodes = []
            full = await create_sync_run(session, source=source, mode="full_scan")
            await session.commit()
            await process_feishu_sync_run(session, base_dir=tmp_path, run=full, api=api)
            assert full.stats_json["deleted"] == 1
            assert item.status == "deleted"
            assert document.status == "deleted"
            assert not Path(document.storage_path).exists()
            assert Path(document.doc_metadata["tombstone_path"]).exists()
        await engine.dispose()

    from pathlib import Path

    asyncio.run(run())


def test_feishu_http_client_retries_rate_limit() -> None:
    async def run() -> None:
        calls = 0

        async def handler(_request: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            if calls == 1:
                return httpx.Response(429, headers={"retry-after": "0"}, json={"code": 99991400, "msg": "rate"})
            return httpx.Response(200, json={"code": 0, "data": {"ok": True}})

        client = FeishuHttpClient(transport=httpx.MockTransport(handler))
        payload = await client.request_json("GET", api_base_url="https://open.feishu.cn", path="/retry")
        assert payload["data"]["ok"] is True
        assert calls == 2

    asyncio.run(run())


def test_feishu_empty_body_is_not_imported(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("PUDDINGCLAW_KNOWLEDGE_DIR", str(tmp_path / "knowledge"))

    async def run() -> None:
        from pathlib import Path

        class FakeApi:
            def __init__(self) -> None:
                self.revision = 1
                self.body = ""
                self.nodes = [
                    {
                        "space_id": "space-1",
                        "node_token": "wiki-node-empty",
                        "obj_token": "docx-empty",
                        "obj_type": "docx",
                        "title": "目录页",
                        "has_child": False,
                        "obj_edit_time": "1700000000",
                    }
                ]

            async def list_nodes(self, _session, _source, *, space_id, parent_node_token=None):
                return list(self.nodes)

            async def get_node(self, *_args, **_kwargs):
                raise AssertionError("root node lookup not expected")

            async def get_docx_document(self, _session, _source, *, document_id):
                return {"title": "目录页", "revision_id": self.revision}

            async def list_docx_blocks(self, _session, _source, *, document_id, document_revision_id):
                blocks = [{"block_id": "page", "block_type": 1, "children": ["text"]}]
                if self.body:
                    blocks.append(
                        {
                            "block_id": "text",
                            "block_type": 2,
                            "text": {"elements": [{"text_run": {"content": self.body}}]},
                        }
                    )
                return blocks

            async def get_doc_meta(self, _session, _source, *, doc_token, doc_type="docx"):
                return {}

            async def get_user_display_names(self, _session, _source, *, open_ids):
                return {}

        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'sync.db'}")
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        api = FakeApi()
        async with sessions() as session:
            source = await create_source_connection(
                session,
                connector_key="feishu_wiki",
                name="产品知识库",
                auth_type="tenant",
                config={"space_id": "space-1", "publish_vector": False},
            )
            source.status = "ready"

            first = await create_sync_run(session, source=source, mode="incremental")
            await session.commit()
            await process_feishu_sync_run(session, base_dir=tmp_path, run=first, api=api)
            assert first.status == "succeeded"
            assert first.stats_json["empty"] == 1
            assert first.stats_json["changed"] == 0
            item = (await session.execute(select(KnowledgeSourceItem))).scalar_one()
            assert item.status == "empty"
            assert item.document_id is None
            assert (await session.execute(select(KnowledgeDocument))).scalar_one_or_none() is None

            api.revision = 2
            api.body = "正文内容"
            second = await create_sync_run(session, source=source, mode="incremental")
            await session.commit()
            await process_feishu_sync_run(session, base_dir=tmp_path, run=second, api=api)
            assert second.stats_json["changed"] == 1
            assert second.stats_json["empty"] == 0
            document = (await session.execute(select(KnowledgeDocument))).scalar_one()
            assert item.status == "ready"
            assert item.document_id == document.id
            assert Path(document.storage_path).exists()

            api.revision = 3
            api.body = ""
            third = await create_sync_run(session, source=source, mode="incremental")
            await session.commit()
            await process_feishu_sync_run(session, base_dir=tmp_path, run=third, api=api)
            assert third.stats_json["changed"] == 1  # previously imported doc was tombstoned
            assert item.status == "empty"
            assert item.document_id is None
            assert document.status == "deleted"
            assert not Path(document.storage_path).exists()
            assert Path(document.doc_metadata["tombstone_path"]).exists()
        await engine.dispose()

    asyncio.run(run())


def test_feishu_sync_persists_document_card_metadata(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("PUDDINGCLAW_KNOWLEDGE_DIR", str(tmp_path / "knowledge"))

    async def run() -> None:
        from pathlib import Path

        class FakeApi:
            def __init__(self) -> None:
                self.nodes = [
                    {
                        "space_id": "space-1",
                        "node_token": "wiki-node-meta",
                        "obj_token": "docx-meta",
                        "obj_type": "docx",
                        "title": "产品手册",
                        "has_child": False,
                        "creator": "ou_creator",
                        "obj_create_time": "1700000000",
                        "obj_edit_time": "1700001000",
                    }
                ]

            async def list_nodes(self, _session, _source, *, space_id, parent_node_token=None):
                return list(self.nodes)

            async def get_node(self, *_args, **_kwargs):
                raise AssertionError("root node lookup not expected")

            async def get_docx_document(self, _session, _source, *, document_id):
                return {"title": "产品手册", "revision_id": 7}

            async def list_docx_blocks(self, _session, _source, *, document_id, document_revision_id):
                return [
                    {"block_id": "page", "block_type": 1, "children": ["text", "img"]},
                    {
                        "block_id": "text",
                        "block_type": 2,
                        "text": {"elements": [{"text_run": {"content": "正文"}}]},
                    },
                    {"block_id": "img", "block_type": 27, "image": {"token": "img-token"}},
                ]

            async def download_media_assets(self, _session, _source, *, file_tokens, max_bytes_each=None):
                return {"img-token": (b"png-bytes", "image/png", 'inline; filename="cover.png"')}

            async def get_doc_meta(self, _session, _source, *, doc_token, doc_type="docx"):
                assert doc_token == "docx-meta"
                return {
                    "doc_token": "docx-meta",
                    "doc_type": "docx",
                    "title": "产品手册",
                    "owner_id": "ou_owner",
                    "create_time": "1700000100",
                    "latest_modify_user": "ou_owner",
                    "latest_modify_time": "1700002000",
                    "url": "https://sample.feishu.cn/docx/docx-meta",
                }

            async def get_user_display_names(self, _session, _source, *, open_ids):
                return {"ou_owner": "张三"}

        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'sync.db'}")
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        api = FakeApi()
        async with sessions() as session:
            source = await create_source_connection(
                session,
                connector_key="feishu_wiki",
                name="产品知识库",
                auth_type="tenant",
                config={"space_id": "space-1", "publish_vector": False},
            )
            source.status = "ready"
            run_ = await create_sync_run(session, source=source, mode="incremental")
            await session.commit()
            await process_feishu_sync_run(session, base_dir=tmp_path, run=run_, api=api)
            assert run_.status == "succeeded"

            document = (await session.execute(select(KnowledgeDocument))).scalar_one()
            feishu_meta = document.doc_metadata["feishu"]
            # drive metadata wins over wiki node fields where both exist
            assert feishu_meta["author_id"] == "ou_owner"
            assert feishu_meta["author_name"] == "张三"
            assert feishu_meta["published_at"] == "2023-11-14T22:15:00+00:00"
            assert feishu_meta["updated_at"] == "2023-11-14T22:46:40+00:00"
            assert document.origin_url == "https://sample.feishu.cn/docx/docx-meta"
            assert "cover" not in feishu_meta

            content = Path(document.storage_path).read_text(encoding="utf-8")
            assert "author: 张三" in content
            assert "published_at: '2023-11-14T22:15:00+00:00'" in content
            assert "source_url: https://sample.feishu.cn/docx/docx-meta" in content
        await engine.dispose()

    asyncio.run(run())
