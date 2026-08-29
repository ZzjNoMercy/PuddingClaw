from __future__ import annotations

import hashlib
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from knowledge.import_jobs import (
    cleanup_expired_staged_import_jobs,
    cleanup_succeeded_task_source,
    cleanup_succeeded_task_sources,
    commit_staged_import_job,
    create_import_job,
    list_import_jobs,
    task_source_path,
)
from knowledge.mineru_client import MinerUParseResult
from knowledge.models import Base, KnowledgeBase, KnowledgeImportJob
from knowledge.parsers.contracts import ParseResult
from knowledge.parsers.registry import DocumentParserRegistry
from knowledge.service import KnowledgeService, KnowledgeServiceError


@pytest.mark.asyncio
async def test_parser_probe_uses_draft_connection_values_without_saving(monkeypatch):
    registry = DocumentParserRegistry()
    saved_config = {
        "mineru_cloud_precise": {
            "enabled": False,
            "priority": 40,
            "base_url": "https://saved.example",
            "credential_ref": "",
        }
    }
    captured: dict[str, str] = {}

    class _DraftMinerU:
        parser_id = "mineru_cloud_precise"

        def __init__(self, *, base_url: str, api_key: str) -> None:
            captured.update(base_url=base_url, api_key=api_key)

        async def health(self) -> tuple[bool, str]:
            return True, "draft ok"

    monkeypatch.setattr(registry, "configuration", lambda: saved_config)
    monkeypatch.setattr("knowledge.parsers.registry.MinerUCloudPreciseParser", _DraftMinerU)

    ok, message = await registry.probe(
        "mineru_cloud_precise",
        base_url="https://draft.example",
        api_key="draft-token",
    )

    assert ok is True
    assert message == "draft ok"
    assert captured == {"base_url": "https://draft.example", "api_key": "draft-token"}
    assert saved_config["mineru_cloud_precise"]["base_url"] == "https://saved.example"
    assert saved_config["mineru_cloud_precise"]["credential_ref"] == ""


@pytest.mark.asyncio
async def test_light_cloud_parser_is_rejected_before_upload_when_source_exceeds_limits(monkeypatch):
    registry = DocumentParserRegistry()
    monkeypatch.setattr(
        registry,
        "configuration",
        lambda: {
            "mineru_cloud_light": {
                "enabled": True,
                "priority": 10,
                "base_url": "https://mineru.net",
            }
        },
    )
    by_size = await registry.catalog(filename="large.pdf", file_size=10 * 1024 * 1024 + 1, page_count=2)
    assert by_size[0]["selectable"] is False
    assert "10 MB" in by_size[0]["reason"]

    by_pages = await registry.catalog(filename="long.pdf", file_size=1024, page_count=21)
    assert by_pages[0]["selectable"] is False
    assert "20 页" in by_pages[0]["reason"]

    eligible = await registry.catalog(filename="small.pdf", file_size=1024, page_count=2)
    assert eligible[0]["selectable"] is True
    assert eligible[0]["recommended"] is True
    assert eligible[0]["reason"] == "当前首选的可用解析器"


@pytest.mark.asyncio
async def test_parser_catalog_survives_unreadable_vault_credential(monkeypatch):
    registry = DocumentParserRegistry()
    monkeypatch.setattr(
        registry,
        "configuration",
        lambda: {
            "llama_parse_cloud": {
                "enabled": True,
                "priority": 10,
                "base_url": "https://api.cloud.llamaindex.ai",
                "credential_ref": "vault://users/local/credentials/parser-llama",
            }
        },
    )
    monkeypatch.setattr(
        "knowledge.parsers.registry._credential_status",
        lambda _reference: {
            "configured": True,
            "readable": False,
            "value": "",
            "error": "vault mismatch",
        },
    )

    rows = await registry.catalog(filename="report.pdf")

    assert rows[0]["credential_configured"] is True
    assert rows[0]["credential_readable"] is False
    assert rows[0]["available"] is False
    assert rows[0]["selectable"] is False
    assert "重新录入" in rows[0]["health_message"]


class _Registry:
    async def catalog(self, *, filename: str = "", file_size: int | None = None, page_count: int | None = None):
        return [
            {
                "id": "mineru_local",
                "name": "MinerU 本地解析",
                "selectable": True,
                "location": "local",
                "version": "test-v1",
                "reason": "ok",
            },
            {
                "id": "llama_parse_cloud",
                "name": "LlamaParse 云端 PDF 解析",
                "selectable": True,
                "location": "cloud",
                "version": "test-v1",
                "reason": "ok",
            },
        ]


class _FakeMinerU:
    def __init__(self, markdown: str) -> None:
        self.markdown = markdown
        self.calls = 0

    async def parse_pdf_bytes(self, *, filename: str, content: bytes, assets_dir: Path):
        self.calls += 1
        return MinerUParseResult(markdown=self.markdown, raw_response={"test": True}, assets=[])


class _StructuredParserRegistry:
    async def parse(self, parser_id, request, on_progress=None):
        assert parser_id == "mineru_cloud_precise"
        return ParseResult(
            markdown="# Precise\n",
            parser_id=parser_id,
            parser_version="test-v1",
            structured_blocks=({"type": "text", "text": "Precise"},),
            parser_metadata={"remote_task_id": "batch-1"},
        )


@pytest.mark.asyncio
async def test_precise_cloud_parser_persists_structured_blocks_on_document(tmp_path: Path, monkeypatch):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'catalog.db'}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr("knowledge.service.get_document_parser_registry", lambda: _StructuredParserRegistry())

    async with sessions() as session:
        document, _ = await KnowledgeService(tmp_path).ingest_pdf_upload(
            session,
            filename="report.pdf",
            content=b"%PDF precise",
            publish_targets=["local_markdown"],
            parser_id="mineru_cloud_precise",
            publish_vector_now=False,
        )

    assert document.doc_metadata["structured_blocks"] == [{"type": "text", "text": "Precise"}]
    assert document.doc_metadata["parser_trace"]["metadata"]["remote_task_id"] == "batch-1"
    await engine.dispose()


def test_completed_task_cleanup_only_removes_its_owned_staging_copy(tmp_path: Path, monkeypatch):
    knowledge_root = tmp_path / "knowledge"
    monkeypatch.setattr("knowledge.import_jobs.get_knowledge_root", lambda _base_dir: knowledge_root)
    source = task_source_path(tmp_path, job_id="job-success", filename="report.pdf")
    source.parent.mkdir(parents=True)
    source.write_bytes(b"temporary upload")
    job = KnowledgeImportJob(
        id="job-success",
        knowledge_base_id="kb-1",
        status="succeeded",
        file_name="report.pdf",
        source_path=str(source),
    )

    assert cleanup_succeeded_task_source(tmp_path, job) is True
    assert not source.exists()
    assert not source.parent.parent.exists()
    assert (knowledge_root / ".tasks").exists()

    retry_source = task_source_path(tmp_path, job_id="job-failed", filename="retry.pdf")
    retry_source.parent.mkdir(parents=True)
    retry_source.write_bytes(b"retain for retry")
    failed_job = KnowledgeImportJob(
        id="job-failed",
        knowledge_base_id="kb-1",
        status="failed",
        file_name="retry.pdf",
        source_path=str(retry_source),
    )
    assert cleanup_succeeded_task_source(tmp_path, failed_job) is False
    assert retry_source.exists()

    external = tmp_path / "mounted" / "report.pdf"
    external.parent.mkdir(parents=True)
    external.write_bytes(b"never delete mounted source")
    external_job = KnowledgeImportJob(
        id="job-external",
        knowledge_base_id="kb-1",
        status="succeeded",
        file_name="report.pdf",
        source_path=str(external),
    )
    assert cleanup_succeeded_task_source(tmp_path, external_job) is False
    assert external.exists()


@pytest.mark.asyncio
async def test_worker_recovery_sweeps_only_successful_task_sources(tmp_path: Path, monkeypatch):
    knowledge_root = tmp_path / "knowledge"
    monkeypatch.setattr("knowledge.import_jobs.get_knowledge_root", lambda _base_dir: knowledge_root)
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'catalog.db'}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    succeeded_source = task_source_path(tmp_path, job_id="job-sweep-success", filename="done.md")
    failed_source = task_source_path(tmp_path, job_id="job-sweep-failed", filename="retry.md")
    succeeded_source.parent.mkdir(parents=True)
    failed_source.parent.mkdir(parents=True)
    succeeded_source.write_text("done", encoding="utf-8")
    failed_source.write_text("retry", encoding="utf-8")

    async with sessions() as session:
        session.add(KnowledgeBase(id="kb-1", name="测试知识库"))
        session.add_all(
            [
                KnowledgeImportJob(
                    id="job-sweep-success",
                    knowledge_base_id="kb-1",
                    status="succeeded",
                    file_name="done.md",
                    source_path=str(succeeded_source),
                ),
                KnowledgeImportJob(
                    id="job-sweep-failed",
                    knowledge_base_id="kb-1",
                    status="failed",
                    file_name="retry.md",
                    source_path=str(failed_source),
                ),
            ]
        )
        await session.commit()

        assert await cleanup_succeeded_task_sources(session, base_dir=tmp_path) == 1
        assert not succeeded_source.exists()
        assert failed_source.exists()
    await engine.dispose()


@pytest.mark.asyncio
async def test_staged_source_is_not_queued_until_one_parser_is_committed(tmp_path: Path, monkeypatch):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'catalog.db'}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    source = tmp_path / "knowledge" / ".tasks" / "job-stage" / "source" / "report.pdf"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"%PDF-1.7 test")

    async with sessions() as session:
        job = await create_import_job(
            session,
            base_dir=tmp_path,
            filename="report.pdf",
            source_path=source,
            file_size=source.stat().st_size,
            source_sha256=hashlib.sha256(source.read_bytes()).hexdigest(),
            status="staged",
        )
        assert job.status == "staged"
        pending_jobs = await list_import_jobs(session)
        assert [item.id for item in pending_jobs] == [job.id]

        monkeypatch.setattr("knowledge.parsers.get_document_parser_registry", lambda: _Registry())
        committed = await commit_staged_import_job(
            session,
            job_id=job.id,
            parser_id="mineru_local",
            publish_targets=["local_markdown"],
        )
        assert committed.status == "queued"
        assert committed.job_metadata["parser"]["resolved_id"] == "mineru_local"

        same = await commit_staged_import_job(session, job_id=job.id, parser_id="mineru_local")
        assert same.id == committed.id
        with pytest.raises(KnowledgeServiceError, match="不能重复提交"):
            await commit_staged_import_job(session, job_id=job.id, parser_id="llama_parse_cloud", allow_cloud=True)


@pytest.mark.asyncio
async def test_cloud_parser_requires_explicit_upload_authorization(tmp_path: Path, monkeypatch):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'catalog.db'}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    source = tmp_path / "knowledge" / ".tasks" / "job-cloud" / "source" / "report.pdf"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"%PDF cloud")

    async with sessions() as session:
        job = await create_import_job(
            session,
            base_dir=tmp_path,
            filename="report.pdf",
            source_path=source,
            file_size=source.stat().st_size,
            source_sha256=hashlib.sha256(source.read_bytes()).hexdigest(),
            status="staged",
        )
        monkeypatch.setattr("knowledge.parsers.get_document_parser_registry", lambda: _Registry())
        with pytest.raises(KnowledgeServiceError, match="明确允许"):
            await commit_staged_import_job(
                session,
                job_id=job.id,
                parser_id="llama_parse_cloud",
                allow_cloud=False,
            )


@pytest.mark.asyncio
async def test_expired_staged_source_is_removed_without_becoming_task_history(tmp_path: Path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'catalog.db'}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    source = tmp_path / "knowledge" / ".tasks" / "job-expired" / "source" / "report.pdf"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"%PDF expired")

    async with sessions() as session:
        job = await create_import_job(
            session,
            base_dir=tmp_path,
            filename="report.pdf",
            source_path=source,
            file_size=source.stat().st_size,
            source_sha256=hashlib.sha256(source.read_bytes()).hexdigest(),
            status="staged",
        )
        job.job_metadata = {
            **job.job_metadata,
            "source": {
                **job.job_metadata["source"],
                "expires_at": (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat(),
            },
        }
        await session.commit()

        assert await cleanup_expired_staged_import_jobs(session) == 1
        assert await session.get(type(job), job.id) is None
        assert not source.exists()


@pytest.mark.asyncio
async def test_same_file_is_idempotent_per_parser_fingerprint_but_reparses_with_another_parser(tmp_path: Path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'catalog.db'}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    service = KnowledgeService(tmp_path)
    content = b"%PDF same source"
    first_parser = _FakeMinerU("# First parser")
    unused_parser = _FakeMinerU("# Must not run")
    second_parser = _FakeMinerU("# Second parser")

    async with sessions() as session:
        first, _ = await service.ingest_pdf_upload(
            session,
            filename="report.pdf",
            content=content,
            publish_targets=["local_markdown"],
            source_item_id="source-item",
            parser_id="mineru_local",
            mineru_client=first_parser,
        )
        same, same_result = await service.ingest_pdf_upload(
            session,
            filename="report.pdf",
            content=content,
            publish_targets=["local_markdown"],
            source_item_id="source-item",
            parser_id="mineru_local",
            mineru_client=unused_parser,
        )
        changed, changed_result = await service.ingest_pdf_upload(
            session,
            filename="report.pdf",
            content=content,
            publish_targets=["local_markdown"],
            source_item_id="source-item",
            parser_id="llama_parse_cloud",
            mineru_client=second_parser,
        )

    assert first.id == same.id == changed.id
    assert same_result["deduplicated"] is True
    assert unused_parser.calls == 0
    assert changed_result["deduplicated"] is False
    assert second_parser.calls == 1
    assert Path(changed.storage_path).read_text(encoding="utf-8").startswith("# Second parser")
    assert changed.doc_metadata["parser_trace"]["id"] == "llama_parse_cloud"
