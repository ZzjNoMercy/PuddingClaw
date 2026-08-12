import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config
import knowledge.import_jobs as import_jobs_module
import knowledge.indexer as indexer_module
import tools.search_knowledge_tool as search_knowledge_module
from knowledge.import_jobs import (
    clear_import_jobs,
    create_document_vector_publish_job,
    create_import_job,
    delete_import_job,
    job_to_dict,
    process_import_job,
    process_vector_publish_job,
    task_source_path,
)
from knowledge.indexer import _build_milvus_storage_context, _build_multimodal_nodes
from knowledge.mineru_client import MinerUClient, MinerUParseResult
from knowledge.models import Base
from knowledge.paths import get_knowledge_root
from knowledge.service import KnowledgeService, KnowledgeServiceError, _slugify
from llm.multimodal_embedding import DashScopeMultiModalEmbedding
from tools.search_knowledge_tool import LlamaIndexKnowledgeQueryTool


@pytest.fixture(autouse=True)
def _isolate_knowledge_root(tmp_path: Path, monkeypatch):
    monkeypatch.delenv("PUDDINGCLAW_KNOWLEDGE_DIR", raising=False)
    monkeypatch.setattr(config, "CONFIG_FILE", tmp_path / "config.json")


class FakeMinerUClient:
    async def parse_pdf_bytes(self, *, filename: str, content: bytes, assets_dir: Path | None = None) -> MinerUParseResult:
        assert filename.endswith(".pdf")
        assert content.startswith(b"%PDF")
        assets: list[dict] = []
        if assets_dir is not None:
            assets_dir.mkdir(parents=True, exist_ok=True)
            image_path = assets_dir / "report" / "auto" / "images" / "page_1_img_1.png"
            image_path.parent.mkdir(parents=True, exist_ok=True)
            image_path.write_bytes(b"fake image")
            html_image_path = assets_dir / "report" / "auto" / "images" / "page_2_img_1.png"
            html_image_path.write_bytes(b"fake image 2")
            assets.append({"name": image_path.name, "path": str(image_path), "relative_path": "report/auto/images/page_1_img_1.png"})
            assets.append({"name": html_image_path.name, "path": str(html_image_path), "relative_path": "report/auto/images/page_2_img_1.png"})
        return MinerUParseResult(
            markdown=(
                "# Parsed PDF\n\n"
                "MinerU extracted a table and conclusion.\n\n"
                "![](images/page_1_img_1.png)\n\n"
                '<img src="images/page_2_img_1.png"/>\n'
            ),
            raw_response={"endpoint": "/parse", "fake": True},
            assets=assets,
        )


def test_slugify_keeps_chinese_desktop_filenames():
    assert _slugify("AI-图文混排 PDF 检索实战.pdf") == "AI-图文混排 PDF 检索实战.pdf"
    assert _slugify("../../AI-图文混排.pdf") == "AI-图文混排.pdf"
    assert _slugify("a/bad:name?.md") == "bad-name-.md"


def test_mineru_client_uses_configured_long_read_timeout(tmp_path: Path):
    config.CONFIG_FILE.write_text(
        """
{
  "knowledge": {
    "mineru": {
      "base_url": "http://mineru.local:8002",
      "connect_timeout_seconds": 3,
      "read_timeout_seconds": 1234
    }
  }
}
""",
        encoding="utf-8",
    )

    client = MinerUClient()

    assert client.base_url == "http://mineru.local:8002"
    assert client.timeout.connect == 3
    assert client.timeout.read == 1234


def test_preview_markdown_keeps_utf8_when_chunk_ends_mid_character(tmp_path: Path):
    knowledge_dir = get_knowledge_root(tmp_path) / "imported" / "20260702"
    knowledge_dir.mkdir(parents=True)
    markdown = knowledge_dir / "中文.md"
    markdown.write_text("## 中文标题\n" + "内容" * 100, encoding="utf-8")

    service = KnowledgeService(tmp_path)
    preview = service.preview_file(virtual_path="/knowledge/imported/20260702/中文.md", max_bytes=14)

    assert preview["truncated"] is True
    assert preview["content"].startswith("## 中文标")
    assert "ä¸" not in preview["content"]


def test_import_job_processes_markdown_upload(tmp_path: Path):
    async def run() -> None:
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'test.db'}")
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        sessionmaker = async_sessionmaker(engine, expire_on_commit=False)

        backend_dir = tmp_path / "backend"
        job_id = "job_testmarkdown"
        source_path = task_source_path(backend_dir, job_id=job_id, filename="AI-中文笔记.md")
        source_path.parent.mkdir(parents=True, exist_ok=True)
        content = b"# Notes\n\nhello queued import\n"
        source_path.write_bytes(content)

        async with sessionmaker() as session:
            job = await create_import_job(
                session,
                base_dir=backend_dir,
                filename="AI-中文笔记.md",
                source_path=source_path,
                file_size=len(content),
                source_sha256="",
                title="中文笔记",
                publish_targets=["local_markdown"],
            )
            assert job.id == job_id
            assert job_to_dict(job)["status"] == "queued"
            processed = await process_import_job(session, base_dir=backend_dir, job=job)

        assert processed.status == "succeeded"
        assert processed.progress == 100
        assert processed.document_id
        imported_files = list((get_knowledge_root(backend_dir) / "imported").rglob("*.md"))
        assert len(imported_files) == 1
        assert imported_files[0].name == "中文笔记.md"
        assert imported_files[0].read_text(encoding="utf-8") == content.decode()

    asyncio.run(run())


def test_document_vector_job_deduplicates_and_rebuilds_only_its_document(tmp_path: Path, monkeypatch):
    async def run() -> None:
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'test.db'}")
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        sessionmaker = async_sessionmaker(engine, expire_on_commit=False)

        backend_dir = tmp_path / "backend"
        service = KnowledgeService(backend_dir)
        async with sessionmaker() as session:
            document, _ = await service.ingest_markdown_upload(
                session,
                filename="report.md",
                content=b"# Report\n\nselected document\n",
                title="Report",
                publish_targets=["local_markdown"],
            )
            job = await create_document_vector_publish_job(
                session,
                base_dir=backend_dir,
                document=document,
            )
            duplicate = await create_document_vector_publish_job(
                session,
                base_dir=backend_dir,
                document=document,
            )
            assert duplicate.id == job.id

            captured: dict[str, Path] = {}

            def fake_refresh(base_dir: Path, document_path: Path, progress_callback=None):
                captured["base_dir"] = base_dir
                captured["document_path"] = document_path
                if progress_callback:
                    progress_callback({"stage": "done", "text_total": 1, "image_total": 0})
                return {
                    "refreshed": True,
                    "generated_at": "2026-08-03T00:00:00Z",
                    "document_virtual_path": document.virtual_path,
                }

            monkeypatch.setattr(import_jobs_module, "refresh_document_knowledge_index", fake_refresh)
            processed = await process_vector_publish_job(session, base_dir=backend_dir, job=job)

        assert processed.status == "succeeded"
        assert captured["base_dir"] == backend_dir
        assert captured["document_path"] == Path(document.storage_path)

    asyncio.run(run())


def test_refresh_document_index_selects_complete_linked_pdf_image_directory(tmp_path: Path, monkeypatch):
    backend_dir = tmp_path / "backend"
    knowledge_dir = tmp_path / "knowledge"
    markdown_path = knowledge_dir / "imported" / "20260803" / "report.md"
    linked_image = knowledge_dir / "assets" / "report" / "figure.png"
    unrelated_image = knowledge_dir / "assets" / "other" / "other.png"
    markdown_path.parent.mkdir(parents=True)
    linked_image.parent.mkdir(parents=True)
    unrelated_image.parent.mkdir(parents=True)
    markdown_path.write_text("# Report\n\n![](../../../assets/report/figure.png)\n", encoding="utf-8")
    linked_image.write_bytes(b"linked")
    sibling_images = [linked_image.parent / f"unreferenced-{index:02d}.png" for index in range(25)]
    for image_path in sibling_images:
        image_path.write_bytes(b"sibling")
    unrelated_image.write_bytes(b"unrelated")

    monkeypatch.setattr(indexer_module, "get_knowledge_root", lambda _base_dir: knowledge_dir)
    monkeypatch.setattr(
        indexer_module,
        "get_knowledge_multimodal_index_config",
        lambda: {"enabled": True, "vector_store": "milvus"},
    )
    captured: dict[str, object] = {}

    def fake_build(**kwargs):
        captured.update(kwargs)
        return {"enabled": True, "document_count": 2}

    monkeypatch.setattr(indexer_module, "_try_build_multimodal_index", fake_build)

    result = indexer_module.refresh_document_knowledge_index(backend_dir, markdown_path)

    assert result["refreshed"] is True
    assert captured["markdown_files"] == [markdown_path]
    assert captured["image_files"] == sorted([linked_image, *sibling_images])
    assert captured["replace_document_virtual_path"] == "/knowledge/imported/20260803/report.md"


def test_document_rebuild_deletes_only_stale_milvus_nodes():
    class FakeClient:
        def __init__(self, rows: list[dict[str, str]]):
            self.rows = rows
            self.deletes: list[str] = []

        def query(self, **_kwargs):
            return self.rows

        def delete(self, *, filter: str, **_kwargs):
            self.deletes.append(filter)

    class FakeStore:
        def __init__(self, rows: list[dict[str, str]]):
            self.collection_name = "collection"
            self.client = FakeClient(rows)

    text_store = FakeStore([{"id": "text-current"}, {"id": "text-stale"}])
    image_store = FakeStore([{"id": "image-stale"}])
    storage_context = SimpleNamespace(vector_stores={"default": text_store, "image": image_store})
    existing = indexer_module._document_node_ids(storage_context, "/knowledge/imported/report.md")
    nodes = [SimpleNamespace(node_id="text-current", metadata={"modality": "text"})]

    indexer_module._delete_stale_document_nodes(storage_context, existing, nodes)

    assert text_store.client.deletes == ['id in ["text-stale"]']
    assert image_store.client.deletes == ['id in ["image-stale"]']


def test_import_local_markdown_copies_into_puddingclaw_home(tmp_path: Path):
    async def run() -> None:
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'test.db'}")
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        sessionmaker = async_sessionmaker(engine, expire_on_commit=False)

        source = tmp_path / "source.md"
        source.write_text("# Source\n\nhello knowledge\n", encoding="utf-8")
        backend_dir = tmp_path / "backend"

        service = KnowledgeService(backend_dir)
        async with sessionmaker() as session:
            document = await service.import_local_markdown(session, source_path=str(source))
            documents = await service.list_documents(session)

        assert document.virtual_path.startswith("/knowledge/imported/")
        assert document.storage_path.startswith(str(get_knowledge_root(backend_dir) / "imported"))
        assert Path(document.storage_path).read_text(encoding="utf-8") == source.read_text(encoding="utf-8")
        assert documents[0].id == document.id

    asyncio.run(run())


def test_search_knowledge_tool_has_no_query_time_index_builder(tmp_path: Path):
    tool = LlamaIndexKnowledgeQueryTool(base_dir=str(tmp_path))

    assert not hasattr(tool, "_build_index")
    assert not hasattr(tool, "_compute_knowledge_signature")


def test_rag_trace_candidate_summary_includes_complete_content_preview():
    text_candidate = {
        "title": "whitepaper.md",
        "quote": "AI native applications combine models, agents, retrieval, memory, and tools. " * 4,
        "modality": "text",
        "retrieval_channel": "text_vector",
        "retrieval_rank": 1,
        "source": {
            "chunk_id": "text-1",
            "metadata": {"chunk_title": "Core architecture"},
        },
    }
    image_candidate = {
        "title": "architecture.png",
        "quote": "image path only",
        "modality": "image",
        "retrieval_channel": "image_vector",
        "retrieval_rank": 1,
        "source": {"chunk_id": "image-1", "metadata": {}},
        "image_hit": {"context": {"snippet": "Architecture diagram with model, agent, RAG, and tools."}},
    }

    summaries = LlamaIndexKnowledgeQueryTool._rag_candidate_summary(
        [text_candidate, image_candidate],
        include_preview=True,
    )

    assert summaries[0]["section"] == "Core architecture"
    assert summaries[0]["preview"].startswith("AI native applications")
    assert summaries[0]["preview"] == (
        "AI native applications combine models, agents, retrieval, memory, and tools. " * 4
    ).strip()
    assert summaries[1]["preview"] == "Architecture diagram with model, agent, RAG, and tools."
    compact = LlamaIndexKnowledgeQueryTool._rag_candidate_summary([text_candidate])
    assert "preview" not in compact[0]


def test_knowledge_milvus_defaults_to_stable_collection_names():
    index_config = config.get_knowledge_multimodal_index_config()

    assert index_config["text_collection"] == "puddingclaw_knowledge_text"
    assert index_config["image_collection"] == "puddingclaw_knowledge_image"
    assert "legacy_text_collection" not in index_config
    assert index_config["bm25_enabled"] is True


def test_milvus_text_store_enables_builtin_bm25_with_chinese_analyzer(monkeypatch):
    stores: list[dict] = []
    functions: list[dict] = []

    class FakeMilvusVectorStore:
        def __init__(self, **kwargs):
            stores.append(kwargs)
            self.collection_name = kwargs["collection_name"]
            self.client = SimpleNamespace(delete=lambda **_kwargs: None)

    class FakeBM25BuiltInFunction:
        def __init__(self, **kwargs):
            functions.append(kwargs)

    class FakeStorageContext:
        def __init__(self, vector_store):
            self.vector_stores = {"default": vector_store}

    monkeypatch.setattr("llama_index.vector_stores.milvus.MilvusVectorStore", FakeMilvusVectorStore)
    monkeypatch.setattr(
        "llama_index.vector_stores.milvus.utils.BM25BuiltInFunction",
        FakeBM25BuiltInFunction,
    )
    monkeypatch.setattr(
        "llama_index.core.StorageContext.from_defaults",
        lambda **kwargs: FakeStorageContext(kwargs["vector_store"]),
    )
    monkeypatch.setattr(config, "get_fallback_embedding_config", lambda: {"dimension": 1024})
    monkeypatch.setattr(config, "get_multimodal_embedding_config", lambda: {"dimension": 1024})

    _storage_context, details = _build_milvus_storage_context({
        "milvus_uri": "http://milvus.test:19530",
        "text_collection": "puddingclaw_knowledge_text",
        "image_collection": "puddingclaw_knowledge_image",
        "bm25_enabled": True,
    })

    assert stores[0]["collection_name"] == "puddingclaw_knowledge_text"
    assert stores[0]["enable_sparse"] is True
    assert stores[0]["sparse_embedding_field"] == "sparse_embedding"
    assert stores[1]["enable_sparse"] is False
    assert functions == [{
        "input_field_names": "text",
        "output_field_names": "sparse_embedding",
        "function_name": "puddingclaw_knowledge_bm25",
        "analyzer_params": {
            "tokenizer": "jieba",
            "filter": ["lowercase"],
        },
    }]
    assert details["bm25_enabled"] is True
    assert details["bm25_analyzer"] == "jieba"


def test_search_knowledge_tool_never_falls_back_from_milvus_to_local_index(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(
        search_knowledge_module,
        "get_knowledge_multimodal_index_config",
        lambda: {"enabled": True, "vector_store": "milvus"},
    )

    def fail_milvus(_self, _query: str, *, top_k: int = 3):
        raise RuntimeError("milvus unavailable")

    def fail_if_local_index_is_loaded(_self):
        raise AssertionError("Milvus query failure must not load or rebuild a local index")

    monkeypatch.setattr(LlamaIndexKnowledgeQueryTool, "_query_milvus_multimodal", fail_milvus)
    monkeypatch.setattr(LlamaIndexKnowledgeQueryTool, "_load_local_index", fail_if_local_index_is_loaded)

    result = LlamaIndexKnowledgeQueryTool(base_dir=str(tmp_path))._run("AI 原生应用")

    assert "Milvus knowledge query failed without modifying the index" in result


def test_image_embedding_failure_keeps_text_milvus_retrieval(tmp_path: Path, monkeypatch):
    class FakeTextEmbedding:
        def get_query_embedding(self, _query: str):
            return [0.1, 0.2]

    class FailingImageEmbedding:
        def get_query_embedding(self, _query: str):
            raise RuntimeError("image endpoint rejected request")

    class FakeMilvusClient:
        searches: list[str] = []

        def __init__(self, **_kwargs):
            pass

        def has_collection(self, collection: str) -> bool:
            return collection == "kb_text"

        def search(self, *, collection_name: str, **_kwargs):
            self.searches.append(collection_name)
            return [[{
                "id": "chunk-1",
                "distance": 0.9,
                "entity": {
                    "text": "AI 原生应用需要模型、Agent、RAG 与评估体系。",
                    "metadata": {
                        "file_path": "/knowledge/imported/ai-native.md",
                        "document_id": "doc-1",
                        "chunk_id": "chunk-1",
                    },
                },
            }]]

    monkeypatch.setattr(
        search_knowledge_module,
        "get_knowledge_multimodal_index_config",
        lambda: {
            "enabled": True,
            "vector_store": "milvus",
            "milvus_uri": "http://milvus.test:19530",
            "text_collection": "kb_text",
            "image_collection": "kb_image",
        },
    )
    monkeypatch.setattr(
        search_knowledge_module,
        "get_rag_hybrid_config",
        lambda: {
            "enabled": False,
            "candidate_top_k": 3,
            "image_vector_weight": 0.35,
        },
    )
    monkeypatch.setattr(config, "get_rag_rerank_config", lambda: {"enabled": False})
    monkeypatch.setattr("llm.embed_client.get_embedding_model", lambda: FakeTextEmbedding())
    monkeypatch.setattr("llm.multimodal_embedding.get_multimodal_embedding_model", lambda: FailingImageEmbedding())
    monkeypatch.setattr("pymilvus.MilvusClient", FakeMilvusClient)

    payload = LlamaIndexKnowledgeQueryTool(base_dir=str(tmp_path))._query_milvus_multimodal_hits(
        "AI 原生应用",
        top_k=3,
    )

    assert payload is not None
    assert payload["retrieval"]["text_vector"] == 1
    assert payload["retrieval"]["image_vector"] == 0
    assert payload["chunks"] == ["AI 原生应用需要模型、Agent、RAG 与评估体系。"]
    assert FakeMilvusClient.searches == ["kb_text"]


def test_milvus_bm25_retrieval_uses_raw_query_without_embedding(tmp_path: Path, monkeypatch):
    class FailingEmbedding:
        def get_query_embedding(self, _query: str):
            raise RuntimeError("embedding unavailable")

    class FakeMilvusClient:
        searches: list[tuple[str, object]] = []

        def __init__(self, **_kwargs):
            pass

        def has_collection(self, collection: str) -> bool:
            return collection == "puddingclaw_knowledge_text"

        def search(self, *, anns_field: str, data: list, **_kwargs):
            self.searches.append((anns_field, data[0]))
            assert anns_field == "sparse_embedding"
            return [[{
                "id": "chunk-bm25",
                "distance": 8.2,
                "entity": {
                    "text": "马赫 M100 智驾算法支持年度改款车型。",
                    "metadata": {
                        "file_path": "/knowledge/imported/l6.md",
                        "document_id": "doc-l6",
                        "chunk_id": "chunk-bm25",
                    },
                },
            }]]

    monkeypatch.setattr(
        search_knowledge_module,
        "get_knowledge_multimodal_index_config",
        lambda: {
            "enabled": True,
            "vector_store": "milvus",
            "milvus_uri": "http://milvus.test:19530",
            "text_collection": "puddingclaw_knowledge_text",
            "image_collection": "puddingclaw_knowledge_image",
            "bm25_enabled": True,
        },
    )
    monkeypatch.setattr(
        search_knowledge_module,
        "get_rag_hybrid_config",
        lambda: {
            "enabled": True,
            "candidate_top_k": 5,
            "text_vector_weight": 0.7,
            "bm25_weight": 0.3,
            "image_vector_weight": 0.4,
        },
    )
    monkeypatch.setattr(config, "get_rag_rerank_config", lambda: {"enabled": False})
    monkeypatch.setattr("llm.embed_client.get_embedding_model", lambda: FailingEmbedding())
    monkeypatch.setattr("llm.multimodal_embedding.get_multimodal_embedding_model", lambda: FailingEmbedding())
    monkeypatch.setattr("pymilvus.MilvusClient", FakeMilvusClient)

    payload = LlamaIndexKnowledgeQueryTool(base_dir=str(tmp_path))._query_milvus_multimodal_hits(
        "马赫 M100",
        top_k=3,
    )

    assert payload is not None
    assert FakeMilvusClient.searches == [("sparse_embedding", "马赫 M100")]
    assert payload["retrieval"]["text_vector"] == 0
    assert payload["retrieval"]["bm25"] == 1
    assert payload["chunks"] == ["马赫 M100 智驾算法支持年度改款车型。"]


def test_missing_configured_collection_does_not_fallback_to_another_collection(tmp_path: Path, monkeypatch):
    class FakeEmbedding:
        def get_query_embedding(self, _query: str):
            return [0.1, 0.2]

    class FailingImageEmbedding:
        def get_query_embedding(self, _query: str):
            raise RuntimeError("image unavailable")

    class FakeMilvusClient:
        fields: list[str] = []

        def __init__(self, **_kwargs):
            pass

        def has_collection(self, collection: str) -> bool:
            return collection == "some_old_collection"

        def search(self, *, anns_field: str, **_kwargs):
            self.fields.append(anns_field)
            return [[]]

    monkeypatch.setattr(
        search_knowledge_module,
        "get_knowledge_multimodal_index_config",
        lambda: {
            "enabled": True,
            "vector_store": "milvus",
            "text_collection": "puddingclaw_knowledge_text",
            "image_collection": "puddingclaw_knowledge_image",
            "bm25_enabled": True,
        },
    )
    monkeypatch.setattr(
        search_knowledge_module,
        "get_rag_hybrid_config",
        lambda: {
            "enabled": True,
            "candidate_top_k": 3,
            "text_vector_weight": 0.7,
            "bm25_weight": 0.3,
            "image_vector_weight": 0.4,
        },
    )
    monkeypatch.setattr(config, "get_rag_rerank_config", lambda: {"enabled": False})
    monkeypatch.setattr("llm.embed_client.get_embedding_model", lambda: FakeEmbedding())
    monkeypatch.setattr("llm.multimodal_embedding.get_multimodal_embedding_model", lambda: FailingImageEmbedding())
    monkeypatch.setattr("pymilvus.MilvusClient", FakeMilvusClient)

    payload = LlamaIndexKnowledgeQueryTool(base_dir=str(tmp_path))._query_milvus_multimodal_hits(
        "AI 原生应用",
        top_k=3,
    )

    assert payload is not None
    assert FakeMilvusClient.fields == []
    assert payload["retrieval"]["text_collection"] == "puddingclaw_knowledge_text"
    assert "legacy_text_fallback" not in payload["retrieval"]
    assert payload["retrieval"]["bm25"] == 0


def test_dashscope_multimodal_http_uses_contents_envelope(monkeypatch):
    captured: dict[str, object] = {}

    class FakeResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {"output": {"embeddings": [{"embedding": [0.1, 0.2]}]}}

    class FakeHttpClient:
        def __init__(self, **_kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def post(self, url: str, *, headers: dict, json: dict):
            captured.update({"url": url, "headers": headers, "json": json})
            return FakeResponse()

    monkeypatch.setattr("llm.multimodal_embedding.httpx.Client", FakeHttpClient)
    model = DashScopeMultiModalEmbedding(
        api_key="test-key",
        model_name="qwen2.5-vl-embedding",
        dimension=1024,
        base_url="https://dashscope.example",
    )

    result = model._call_http_api([{"text": "AI 原生应用"}])

    assert result == [[0.1, 0.2]]
    assert captured["json"] == {
        "model": "qwen2.5-vl-embedding",
        "input": {"contents": [{"text": "AI 原生应用"}]},
        "parameters": {"dimension": 1024},
    }


def test_qwen3_multimodal_http_requests_independent_vectors_and_restores_index_order(monkeypatch):
    captured: dict[str, object] = {}

    class FakeResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {
                "output": {
                    "embeddings": [
                        {"index": 1, "embedding": [0.3, 0.4]},
                        {"index": 0, "embedding": [0.1, 0.2]},
                    ]
                }
            }

    class FakeHttpClient:
        def __init__(self, **_kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def post(self, url: str, *, headers: dict, json: dict):
            captured.update({"url": url, "headers": headers, "json": json})
            return FakeResponse()

    monkeypatch.setattr("llm.multimodal_embedding.httpx.Client", FakeHttpClient)
    model = DashScopeMultiModalEmbedding(
        api_key="test-key",
        model_name="qwen3-vl-embedding",
        dimension=1024,
        base_url="https://dashscope.example",
    )

    result = model._call_http_api([{"text": "first"}, {"text": "second"}])

    assert result == [[0.1, 0.2], [0.3, 0.4]]
    assert captured["json"] == {
        "model": "qwen3-vl-embedding",
        "input": {"contents": [{"text": "first"}, {"text": "second"}]},
        "parameters": {"dimension": 1024, "enable_fusion": False},
    }


def test_qwen3_multimodal_batches_independent_inputs_up_to_configured_size(monkeypatch):
    calls: list[list[dict[str, str]]] = []

    def fake_call_api(_self, input_data: list[dict[str, str]]) -> list[list[float]]:
        calls.append(input_data)
        return [[float(index)] for index, _item in enumerate(input_data)]

    monkeypatch.setattr(DashScopeMultiModalEmbedding, "_call_api", fake_call_api)
    model = DashScopeMultiModalEmbedding(
        api_key="test-key",
        model_name="qwen3-vl-embedding",
        dimension=1024,
        embed_batch_size=10,
        base_url="https://dashscope.example",
    )

    result = model._get_text_embeddings([f"chunk-{index}" for index in range(23)])

    assert [len(batch) for batch in calls] == [10, 10, 3]
    assert len(result) == 23


def test_qwen3_multimodal_caps_image_batches_at_ten(monkeypatch):
    calls: list[list[dict[str, str]]] = []

    def fake_call_api(_self, input_data: list[dict[str, str]]) -> list[list[float]]:
        calls.append(input_data)
        return [[0.1] for _item in input_data]

    monkeypatch.setattr(DashScopeMultiModalEmbedding, "_call_api", fake_call_api)
    model = DashScopeMultiModalEmbedding(
        api_key="test-key",
        model_name="qwen3-vl-embedding",
        dimension=1024,
        embed_batch_size=20,
        base_url="https://dashscope.example",
    )

    model._call_items_in_batches([{"image": f"image-{index}"} for index in range(23)], modality="image")

    assert [len(batch) for batch in calls] == [10, 10, 3]


def test_qwen25_multimodal_keeps_single_item_requests(monkeypatch):
    calls: list[list[dict[str, str]]] = []

    def fake_call_api(_self, input_data: list[dict[str, str]]) -> list[list[float]]:
        calls.append(input_data)
        return [[0.1] for _item in input_data]

    monkeypatch.setattr(DashScopeMultiModalEmbedding, "_call_api", fake_call_api)
    model = DashScopeMultiModalEmbedding(
        api_key="test-key",
        model_name="qwen2.5-vl-embedding",
        dimension=1024,
        embed_batch_size=10,
        base_url="https://dashscope.example",
    )

    model._get_text_embeddings(["first", "second", "third"])

    assert [len(batch) for batch in calls] == [1, 1, 1]


def test_multimodal_index_builds_markdown_parser_nodes_with_image_context(tmp_path: Path):
    knowledge_dir = tmp_path / "knowledge"
    markdown_dir = knowledge_dir / "imported" / "20260702"
    image_dir = knowledge_dir / "assets" / "20260702" / "pdf_demo" / "images"
    markdown_dir.mkdir(parents=True)
    image_dir.mkdir(parents=True)
    image_path = image_dir / "figure.png"
    from PIL import Image

    Image.new("RGB", (1, 1), color=(255, 255, 255)).save(image_path)
    markdown_path = markdown_dir / "报告.md"
    markdown_path.write_text(
        "# 总览\n\n"
        "这是报告的开头。\n\n"
        "## 架构图\n\n"
        "下面这张图解释系统关系。\n\n"
        f"![](../../assets/20260702/pdf_demo/images/{image_path.name})\n\n"
        "图后面还有补充说明。\n",
        encoding="utf-8",
    )

    nodes, manifest = _build_multimodal_nodes(knowledge_dir, [markdown_path], [image_path])

    text_chunks = manifest["chunks"]
    images = manifest["images"]
    assert len(nodes) == len(text_chunks) + len(images)
    assert len(text_chunks) >= 2
    assert text_chunks[0]["level"].startswith("H")
    assert text_chunks[0]["virtual_path"] == "/knowledge/imported/20260702/报告.md"
    assert images[0]["context"]["heading"] == "架构图"
    assert "下面这张图解释系统关系" in images[0]["context"]["snippet"]
    assert images[0]["linked_markdown_virtual_path"] == "/knowledge/imported/20260702/报告.md"


def test_ingest_pdf_upload_stores_original_markdown_and_catalog_record(tmp_path: Path):
    async def run() -> None:
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'test.db'}")
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        sessionmaker = async_sessionmaker(engine, expire_on_commit=False)

        backend_dir = tmp_path / "backend"
        service = KnowledgeService(backend_dir)
        async with sessionmaker() as session:
            document, ingest = await service.ingest_pdf_upload(
                session,
                filename="report.pdf",
                content=b"%PDF-1.7 fake",
                title="Report",
                publish_targets=["local_markdown"],
                mineru_client=FakeMinerUClient(),
            )

        assert document.title == "Report"
        assert document.source_type == "pdf_mineru"
        assert document.virtual_path.startswith("/knowledge/imported/")
        assert Path(document.storage_path).name == "Report.md"
        assert "pdf_" not in Path(document.storage_path).name
        assert Path(document.source_path).exists()
        assert Path(document.storage_path).exists()
        stored_markdown = Path(document.storage_path).read_text(encoding="utf-8")
        assert stored_markdown.startswith("# Parsed PDF")
        assert "](../../assets/" in stored_markdown
        assert 'src="../../assets/' in stored_markdown
        assert "](/knowledge/assets/" not in stored_markdown
        assert "](images/page_1_img_1.png)" not in stored_markdown
        assert 'src="images/page_2_img_1.png"' not in stored_markdown
        preview = service.preview_file(virtual_path=document.virtual_path)
        assert "](/api/knowledge/file/raw?virtual_path=" in preview["content"]
        assert document.doc_metadata["parser"] == "mineru"
        assert document.doc_metadata["original_filename"] == "report.pdf"
        assert document.doc_metadata["multimodal"]["image_asset_count"] == 2
        assert Path(document.doc_metadata["assets"][0]["path"]).exists()
        assert document.doc_metadata["assets"][0]["virtual_path"].startswith("/knowledge/assets/")
        assert document.doc_metadata["assets"][0]["relative_path"].startswith("images/")
        assert "/auto/" not in document.doc_metadata["assets"][0]["virtual_path"]
        image_context = document.doc_metadata["assets"][0]["context"]
        assert image_context["heading"] == "Parsed PDF"
        assert "MinerU extracted" in image_context["snippet"]
        assert image_context["line_number"] == 5
        chunk_manifest = document.doc_metadata["llamaindex_chunks"]
        assert chunk_manifest["parser"] == "MarkdownNodeParser"
        assert chunk_manifest["chunk_count"] >= 1
        assert chunk_manifest["chunks"][0]["virtual_path"] == document.virtual_path
        assert ingest["deduplicated"] is False
        assert ingest["vector_index"]["refreshed"] is False

    asyncio.run(run())


def test_ingest_pdf_upload_repairs_existing_record_when_markdown_file_is_missing(tmp_path: Path):
    async def run() -> None:
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'test.db'}")
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        sessionmaker = async_sessionmaker(engine, expire_on_commit=False)

        backend_dir = tmp_path / "backend"
        service = KnowledgeService(backend_dir)
        async with sessionmaker() as session:
            first, _ = await service.ingest_pdf_upload(
                session,
                filename="report.pdf",
                content=b"%PDF-1.7 fake",
                title="Report",
                publish_targets=["local_markdown"],
                mineru_client=FakeMinerUClient(),
            )
            Path(first.storage_path).unlink()

            second, ingest = await service.ingest_pdf_upload(
                session,
                filename="report.pdf",
                content=b"%PDF-1.7 fake",
                title="Report",
                publish_targets=["local_markdown"],
                mineru_client=FakeMinerUClient(),
            )

        assert second.id == first.id
        assert Path(second.storage_path).exists()
        assert ingest["deduplicated"] is False

    asyncio.run(run())


def test_repair_document_metadata_backfills_image_context_without_reparse(tmp_path: Path):
    async def run() -> None:
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'test.db'}")
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        sessionmaker = async_sessionmaker(engine, expire_on_commit=False)

        backend_dir = tmp_path / "backend"
        service = KnowledgeService(backend_dir)
        async with sessionmaker() as session:
            document, _ = await service.ingest_pdf_upload(
                session,
                filename="report.pdf",
                content=b"%PDF-1.7 fake",
                title="Report",
                publish_targets=["local_markdown"],
                mineru_client=FakeMinerUClient(),
            )
            metadata = dict(document.doc_metadata)
            metadata["assets"] = [{key: value for key, value in asset.items() if key != "context"} for asset in metadata["assets"]]
            metadata.pop("llamaindex_chunks", None)
            document.doc_metadata = metadata

            repaired = service.repair_document_metadata(document)

        assert repaired is True
        assert document.doc_metadata["assets"][0]["context"]["heading"] == "Parsed PDF"
        assert "MinerU extracted" in document.doc_metadata["assets"][0]["context"]["snippet"]
        assert document.doc_metadata["llamaindex_chunks"]["chunk_count"] >= 1

    asyncio.run(run())


def test_ingest_markdown_upload_stores_markdown_and_catalog_record(tmp_path: Path):
    async def run() -> None:
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'test.db'}")
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        sessionmaker = async_sessionmaker(engine, expire_on_commit=False)

        backend_dir = tmp_path / "backend"
        service = KnowledgeService(backend_dir)
        async with sessionmaker() as session:
            document, ingest = await service.ingest_markdown_upload(
                session,
                filename="notes.md",
                content=b"# Notes\n\nhello markdown\n",
                title="Notes",
                publish_targets=["local_markdown"],
            )

        assert document.title == "Notes"
        assert document.source_type == "markdown_upload"
        assert document.virtual_path.startswith("/knowledge/imported/")
        assert Path(document.storage_path).read_text(encoding="utf-8") == "# Notes\n\nhello markdown\n"
        assert document.doc_metadata["llamaindex_chunks"]["parser"] == "MarkdownNodeParser"
        assert document.doc_metadata["llamaindex_chunks"]["chunk_count"] >= 1
        assert ingest["deduplicated"] is False
        assert ingest["vector_index"]["refreshed"] is False

    asyncio.run(run())


def test_import_job_delete_and_clear_remove_task_records_only(tmp_path: Path):
    async def run() -> None:
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'test.db'}")
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        sessionmaker = async_sessionmaker(engine, expire_on_commit=False)

        backend_dir = tmp_path / "backend"
        source_a = task_source_path(backend_dir, job_id="job_delete_a", filename="a.md")
        source_a.parent.mkdir(parents=True, exist_ok=True)
        source_a.write_text("# a\n", encoding="utf-8")
        source_b = task_source_path(backend_dir, job_id="job_delete_b", filename="b.md")
        source_b.parent.mkdir(parents=True, exist_ok=True)
        source_b.write_text("# b\n", encoding="utf-8")

        async with sessionmaker() as session:
            job_a = await create_import_job(
                session,
                base_dir=backend_dir,
                filename="a.md",
                source_path=source_a,
                file_size=source_a.stat().st_size,
                source_sha256="",
            )
            await create_import_job(
                session,
                base_dir=backend_dir,
                filename="b.md",
                source_path=source_b,
                file_size=source_b.stat().st_size,
                source_sha256="",
            )
            await delete_import_job(session, job_a.id)
            assert await session.get(type(job_a), job_a.id) is None
            deleted = await clear_import_jobs(session)
            assert deleted == 1

        assert source_a.exists()
        assert source_b.exists()

    asyncio.run(run())


def test_uploaded_markdown_filename_uses_title_and_deduplicates(tmp_path: Path):
    async def run() -> None:
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'test.db'}")
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        sessionmaker = async_sessionmaker(engine, expire_on_commit=False)

        backend_dir = tmp_path / "backend"
        service = KnowledgeService(backend_dir)
        async with sessionmaker() as session:
            first, _ = await service.ingest_markdown_upload(
                session,
                filename="notes.md",
                content=b"# first\n",
                title="产品说明",
                publish_targets=["local_markdown"],
            )
            second, _ = await service.ingest_markdown_upload(
                session,
                filename="notes.md",
                content=b"# second\n",
                title="产品说明",
                publish_targets=["local_markdown"],
            )

        assert Path(first.storage_path).name == "产品说明.md"
        assert Path(second.storage_path).name == "产品说明-2.md"

    asyncio.run(run())


def test_markdown_glob_and_grep_include_imported_pdf_markdown(tmp_path: Path):
    knowledge_dir = get_knowledge_root(tmp_path) / "imported" / "20260702"
    knowledge_dir.mkdir(parents=True)
    (knowledge_dir / "pdf-report.md").write_text("# Report\n\nMinerU conclusion\n", encoding="utf-8")
    (knowledge_dir / "notes.markdown").write_text("other content\n", encoding="utf-8")

    service = KnowledgeService(tmp_path)
    files = service.glob_markdown_files(pattern="imported/**/*.md")
    matches = service.grep_markdown_files(query="conclusion", pattern="imported/**/*.md")

    assert [item["virtual_path"] for item in files] == ["/knowledge/imported/20260702/pdf-report.md"]
    assert matches[0]["virtual_path"] == "/knowledge/imported/20260702/pdf-report.md"
    assert matches[0]["line_number"] == 3


def test_knowledge_root_can_be_configured_by_user_directory(tmp_path: Path, monkeypatch):
    custom_root = tmp_path / "user-knowledge"
    monkeypatch.setenv("PUDDINGCLAW_KNOWLEDGE_DIR", str(custom_root))

    service = KnowledgeService(tmp_path / "backend")

    assert get_knowledge_root(tmp_path / "backend") == custom_root.resolve()
    assert service.knowledge_dir == custom_root.resolve()
    assert service.imported_dir == custom_root.resolve() / "imported"
    assert service.originals_dir == custom_root.resolve() / "originals"


def test_directory_listing_reports_unreadable_root(tmp_path: Path, monkeypatch):
    knowledge_dir = get_knowledge_root(tmp_path)
    knowledge_dir.mkdir(parents=True)
    service = KnowledgeService(tmp_path)
    original_iterdir = Path.iterdir

    def denied_iterdir(path: Path):
        if path == knowledge_dir:
            raise PermissionError("permission denied")
        return original_iterdir(path)

    monkeypatch.setattr(Path, "iterdir", denied_iterdir)

    with pytest.raises(KnowledgeServiceError, match="无法读取知识库目录"):
        service.list_directory_files()
    with pytest.raises(KnowledgeServiceError, match="无法读取知识库目录"):
        service.list_directory_tree()
