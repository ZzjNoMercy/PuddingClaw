from __future__ import annotations

import asyncio

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from knowledge.connectors.feishu import FeishuHttpClient, FeishuOpenApi
from knowledge.connectors.feishu_blocks import convert_feishu_blocks_to_markdown
from knowledge.connectors.feishu_sync import process_feishu_sync_run
from knowledge.models import Base, FeishuAppCredential, KnowledgeDocument, KnowledgeSourceItem
from knowledge.sources import create_source_connection, create_sync_run


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
