import asyncio
from pathlib import Path
import sys

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from knowledge.models import Base
from knowledge.mineru_client import MinerUParseResult
from knowledge.paths import get_knowledge_root
from knowledge.service import KnowledgeService
from tools.search_knowledge_tool import SearchKnowledgeBaseTool


class FakeMinerUClient:
    async def parse_pdf_bytes(self, *, filename: str, content: bytes, assets_dir: Path | None = None) -> MinerUParseResult:
        assert filename.endswith(".pdf")
        assert content.startswith(b"%PDF")
        assets: list[dict] = []
        if assets_dir is not None:
            assets_dir.mkdir(parents=True, exist_ok=True)
            image_path = assets_dir / "page_1_img_1.png"
            image_path.write_bytes(b"fake image")
            assets.append({"name": image_path.name, "path": str(image_path), "relative_path": image_path.name})
        return MinerUParseResult(
            markdown="# Parsed PDF\n\nMinerU extracted a table and conclusion.\n",
            raw_response={"endpoint": "/parse", "fake": True},
            assets=assets,
        )


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

    tool = SearchKnowledgeBaseTool(base_dir=str(tmp_path))
    before = tool._compute_knowledge_signature()

    imported_dir = knowledge_dir / "imported" / "20260702"
    imported_dir.mkdir(parents=True)
    (imported_dir / "b.md").write_text("second", encoding="utf-8")

    after = tool._compute_knowledge_signature()
    assert before != after


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
        assert Path(document.source_path).exists()
        assert Path(document.storage_path).exists()
        assert Path(document.storage_path).read_text(encoding="utf-8").startswith("# Parsed PDF")
        assert document.doc_metadata["parser"] == "mineru"
        assert document.doc_metadata["original_filename"] == "report.pdf"
        assert document.doc_metadata["multimodal"]["image_asset_count"] == 1
        assert Path(document.doc_metadata["assets"][0]["path"]).exists()
        assert ingest["deduplicated"] is False
        assert ingest["vector_index"]["refreshed"] is False

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
        assert ingest["deduplicated"] is False
        assert ingest["vector_index"]["refreshed"] is False

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
