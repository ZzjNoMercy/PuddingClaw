import asyncio
from pathlib import Path
import sys

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config
from knowledge.models import Base
from knowledge.import_jobs import clear_import_jobs, create_import_job, delete_import_job, job_to_dict, process_import_job, task_source_path
from knowledge.indexer import _build_multimodal_nodes
from knowledge.mineru_client import MinerUClient, MinerUParseResult
from knowledge.paths import get_knowledge_root
from knowledge.service import KnowledgeService, KnowledgeServiceError, _slugify
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
    knowledge_dir = tmp_path / "knowledge" / "imported" / "20260702"
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
        imported_files = list((backend_dir / "knowledge" / "imported").rglob("*.md"))
        assert len(imported_files) == 1
        assert imported_files[0].name == "中文笔记.md"
        assert imported_files[0].read_text(encoding="utf-8") == content.decode()

    asyncio.run(run())


def test_import_local_markdown_copies_into_deepagents_knowledge_backend(tmp_path: Path):
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
        assert document.storage_path.startswith(str(backend_dir / "knowledge" / "imported"))
        assert Path(document.storage_path).read_text(encoding="utf-8") == source.read_text(encoding="utf-8")
        assert documents[0].id == document.id

    asyncio.run(run())


def test_search_knowledge_tool_signature_changes_when_markdown_is_imported(tmp_path: Path):
    knowledge_dir = tmp_path / "knowledge"
    knowledge_dir.mkdir()
    (knowledge_dir / "a.md").write_text("first", encoding="utf-8")

    tool = LlamaIndexKnowledgeQueryTool(base_dir=str(tmp_path))
    before = tool._compute_knowledge_signature()

    imported_dir = knowledge_dir / "imported" / "20260702"
    imported_dir.mkdir(parents=True)
    (imported_dir / "b.md").write_text("second", encoding="utf-8")

    after = tool._compute_knowledge_signature()
    assert before != after


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
    knowledge_dir = tmp_path / "knowledge" / "imported" / "20260702"
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
    knowledge_dir = tmp_path / "knowledge"
    knowledge_dir.mkdir()
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
