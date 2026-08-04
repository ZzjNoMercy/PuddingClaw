from __future__ import annotations

import asyncio
import hashlib
import json
import shutil
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from api.llm_wiki import router
from graph.citations import parse_tool_result
from knowledge.brain_schema import BrainSchemaError, BrainSchemaService
from knowledge.import_jobs import (
    LLM_WIKI_INGEST_KIND,
    create_import_job,
    create_llm_wiki_ingest_job,
    job_to_list_dict,
    process_import_job,
)
from knowledge.llm_wiki import LlmWikiError, LlmWikiService
from knowledge.llm_wiki_compiler_agent import COMPILER_SYSTEM_PROMPT, LlmWikiCompilerAgent
from knowledge.llm_wiki_job_runner import BACKGROUND_INGEST_GROUNDING_RULES, process_llm_wiki_ingest_job
from knowledge.models import Base
from tools.llm_wiki_tools import (
    LlmWikiCreateRawTool,
    LlmWikiQueryTool,
    LlmWikiRetirePagesTool,
    LlmWikiStartIngestTool,
    WikiStartIngestInput,
)


@pytest.fixture()
def wiki_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> LlmWikiService:
    monkeypatch.setenv("PUDDINGCLAW_KNOWLEDGE_DIR", str(tmp_path / "knowledge"))
    monkeypatch.delenv("PUDDINGCLAW_GBRAIN_HOME", raising=False)
    schema = BrainSchemaService(Path(__file__).resolve().parent.parent)
    try:
        schema.initialize()
    except BrainSchemaError:
        pytest.skip("installed or source-tree gbrain schema catalog is unavailable")
    return LlmWikiService(Path(__file__).resolve().parent.parent)


def _page(title: str, page_type: str, source: str, link: str) -> str:
    return (
        "---\n"
        f"title: {title}\n"
        f"type: {page_type}\n"
        "sources:\n"
        f"  - {source}\n"
        "created: '2026-07-30'\n"
        "updated: '2026-07-30'\n"
        "schema_version: 0.1.0\n"
        "---\n\n"
        f"# {title}\n\n"
        f"这是 {title} 的稳定知识摘要，参见 [[{link}]]。\n"
    )


def test_raw_snapshot_is_immutable_and_context_is_bounded(wiki_env: LlmWikiService) -> None:
    agents = wiki_env.workspace_status()["agents"]
    assert agents["content"].startswith("# LLM Wiki Agent 操作契约")
    assert len(agents["sha256"]) == 64
    first = wiki_env.snapshot_raw(
        source_id="existing-kb",
        asset_id="doc-1",
        title="Source One",
        content="# Source One\n\nEvidence.",
        source_path="/knowledge/imported/source-one.md",
    )
    second = wiki_env.snapshot_raw(
        source_id="existing-kb",
        asset_id="doc-1",
        title="Source One",
        content="# Source One\n\nEvidence.",
        source_path="/knowledge/imported/source-one.md",
    )
    assert first == second
    snapshot = wiki_env.raw_dir / first["snapshot_path"]
    assert snapshot.read_text(encoding="utf-8").endswith("\n")

    ingest = wiki_env.operation_context("ingest", raw_paths=[first["snapshot_path"]])
    assert set(ingest["raw_files"]) == {first["snapshot_path"]}
    assert "Ingest" in ingest["agents_md"]
    query = wiki_env.operation_context("query")
    assert "raw_files" not in query
    assert "raw_manifest" not in query
    assert wiki_env.operation_context("ingest")["raw_files"] == {}


def test_llm_wiki_skill_falls_back_to_markdown_after_irrelevant_gbrain_results() -> None:
    skill = (Path(__file__).resolve().parent.parent / "skills" / "llm-wiki" / "SKILL.md").read_text(
        encoding="utf-8"
    )
    normalized = " ".join(skill.split())

    assert "published Markdown LLM Wiki as the complete source of truth" in normalized
    assert "does not directly match the entity/topic asked by the user" in normalized
    assert "call `llm_wiki_query` before" in normalized
    assert "when gbrain already returns a direct, relevant answer" in normalized


def test_markdown_file_snapshot_preserves_final_bytes_and_detects_changes(
    wiki_env: LlmWikiService,
    tmp_path: Path,
) -> None:
    source = tmp_path / "uploaded.md"
    original = "# 原始 Markdown\n\n没有强制补换行"
    source.write_bytes(original.encode("utf-8"))
    record = wiki_env.snapshot_raw_file(
        source_id="knowledge-upload",
        asset_id="doc-final",
        title="最终文件",
        path=source,
        source_path="/knowledge/imported/uploaded.md",
    )
    assert (wiki_env.raw_dir / record["snapshot_path"]).read_bytes() == original.encode("utf-8")
    status = wiki_env.raw_status_for_source(
        source_path="/knowledge/imported/uploaded.md",
        content_sha256=record["sha256"],
    )
    assert status["available"] is True
    assert status["changed_since_snapshot"] is False

    source.write_text(f"{original}\n\n已修改", encoding="utf-8")
    changed_hash = hashlib.sha256(source.read_bytes()).hexdigest()
    changed_status = wiki_env.raw_status_for_source(
        source_path="/knowledge/imported/uploaded.md",
        content_sha256=changed_hash,
    )
    assert changed_status["available"] is False
    assert changed_status["changed_since_snapshot"] is True


def test_main_agent_raw_tool_uses_bound_current_message_without_content_argument(
    wiki_env: LlmWikiService,
) -> None:
    tool = LlmWikiCreateRawTool(
        base_dir=wiki_env.base_dir,
        session_id="session-one",
        query_id="query-one",
        current_message="# 粘贴内容\n\n请把这段内容整理成 Wiki。",
        current_attachments=[],
    )

    result = asyncio.run(tool._arun(source="current_message", title="聊天材料"))
    payload = json.loads(result)
    assert payload["ok"] is True
    assert payload["next_tool"] == "llm_wiki_start_ingest"
    assert len(payload["intake_id"]) == 64
    assert len(payload["raw_paths"]) == 1
    snapshot = wiki_env.raw_dir / payload["raw_paths"][0]
    assert snapshot.read_text(encoding="utf-8") == "# 粘贴内容\n\n请把这段内容整理成 Wiki。\n"


def test_main_agent_ingest_tool_rejects_raw_paths_without_current_intake(
    wiki_env: LlmWikiService,
) -> None:
    tool = LlmWikiStartIngestTool(
        base_dir=wiki_env.base_dir,
        session_id="session-one",
        query_id="query-one",
    )
    result = asyncio.run(
        tool._arun(
            raw_paths=["historical/source.md"],
            intake_id="0" * 64,
        )
    )
    payload = json.loads(result)
    assert payload["ok"] is False
    assert "当前消息" in payload["error"]


def test_main_agent_ingest_defaults_to_wiki_only() -> None:
    payload = WikiStartIngestInput.model_validate(
        {
            "raw_paths": ["chat-session/source.md"],
            "intake_id": "0" * 64,
        }
    )
    assert payload.import_gbrain is False


def test_empty_attachment_selection_snapshots_only_current_markdown_attachments(
    wiki_env: LlmWikiService,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    markdown = tmp_path / "source.md"
    image = tmp_path / "image.png"
    markdown.write_text("# Markdown attachment\n", encoding="utf-8")
    image.write_bytes(b"\x89PNG")
    stored = {
        "md": {"id": "md", "name": "source.md", "path": str(markdown), "mime_type": "text/markdown"},
        "image": {"id": "image", "name": "image.png", "path": str(image), "mime_type": "image/png"},
    }
    monkeypatch.setattr(
        "tools.llm_wiki_tools.attachment_store.get",
        lambda _session_id, attachment_id: stored.get(attachment_id),
    )
    tool = LlmWikiCreateRawTool(
        base_dir=wiki_env.base_dir,
        session_id="session-one",
        query_id="query-one",
        current_attachments=[{"id": "md"}, {"id": "image"}],
    )
    payload = json.loads(asyncio.run(tool._arun(source="attachments")))
    assert payload["ok"] is True
    assert len(payload["raw_paths"]) == 1
    assert payload["snapshots"][0]["asset_id"] == "md"


def test_knowledge_file_raw_tool_canonicalizes_virtual_path(
    wiki_env: LlmWikiService,
) -> None:
    imported = wiki_env.root.parent / "imported"
    imported.mkdir(parents=True, exist_ok=True)
    source = imported / "canonical.md"
    source.write_text("# Canonical\n", encoding="utf-8")
    tool = LlmWikiCreateRawTool(
        base_dir=wiki_env.base_dir,
        session_id="session-one",
        query_id="query-one",
    )
    payload = json.loads(
        asyncio.run(
            tool._arun(
                source="knowledge_file",
                virtual_path="/knowledge/imported/folder/../canonical.md",
            )
        )
    )
    assert payload["ok"] is True
    assert payload["snapshots"][0]["source_path"] == "/knowledge/imported/canonical.md"
    assert payload["snapshots"][0]["asset_id"] == "/knowledge/imported/canonical.md"


def test_markdown_import_job_copies_final_imported_file_to_raw_once(
    wiki_env: LlmWikiService,
    tmp_path: Path,
) -> None:
    content = "# 上传文件\n\n服务端只能读取最终 imported 文件"
    task_source = tmp_path / "task" / "source" / "upload.md"
    task_source.parent.mkdir(parents=True)
    task_source.write_bytes(content.encode("utf-8"))

    async def run() -> None:
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'upload-jobs.db'}")
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with factory() as session:
            job = await create_import_job(
                session,
                base_dir=wiki_env.base_dir,
                filename="upload.md",
                source_path=task_source,
                file_size=len(content.encode("utf-8")),
                source_sha256=hashlib.sha256(content.encode("utf-8")).hexdigest(),
                publish_targets=["local_markdown", "llm_wiki_raw"],
            )
            result = await process_import_job(session, base_dir=wiki_env.base_dir, job=job)
            imported_path = Path(result.job_metadata["document_virtual_path"].replace("/knowledge/", "", 1))
            imported_path = wiki_env.root.parent / imported_path
            raw = result.job_metadata["ingestion"]["llm_wiki_raw"]
            assert Path(result.source_path) == task_source
            assert imported_path.read_bytes() == content.encode("utf-8")
            assert (wiki_env.raw_dir / raw["snapshot_path"]).read_bytes() == imported_path.read_bytes()
        await engine.dispose()

    asyncio.run(run())


def test_raw_addon_failure_does_not_fail_original_markdown_import(
    wiki_env: LlmWikiService,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    content = "# 原上传链路\n"
    task_source = tmp_path / "failed-raw-task" / "source" / "upload.md"
    task_source.parent.mkdir(parents=True)
    task_source.write_bytes(content.encode("utf-8"))

    class BrokenWiki:
        def snapshot_raw_file(self, **_kwargs):
            raise LlmWikiError("Schema 尚未初始化")

    monkeypatch.setattr("knowledge.llm_wiki.get_llm_wiki_service", lambda _base: BrokenWiki())

    async def run() -> None:
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'failed-raw-jobs.db'}")
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with factory() as session:
            job = await create_import_job(
                session,
                base_dir=wiki_env.base_dir,
                filename="upload.md",
                source_path=task_source,
                file_size=len(content.encode("utf-8")),
                source_sha256=hashlib.sha256(content.encode("utf-8")).hexdigest(),
                publish_targets=["local_markdown", "llm_wiki_raw"],
            )
            result = await process_import_job(session, base_dir=wiki_env.base_dir, job=job)
            assert result.status == "succeeded"
            assert result.document_id
            assert "llm_wiki_raw" not in result.publish_targets
            assert result.job_metadata["ingestion"]["llm_wiki_raw"]["ok"] is False
        await engine.dispose()

    asyncio.run(run())


def test_llm_wiki_retry_resumes_at_gbrain_without_rerunning_compiler(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    raw_path = "chat-session/source.md"
    raw_hash = "b" * 64
    bundle_hash = "a" * 64
    compile_calls: list[bool] = []

    class FakeWiki:
        def freeze_ingest_inputs(self, raw_paths: list[str]):
            return {
                "schema_bundle": {"bundle_hash": bundle_hash},
                "raw_manifest": [{"snapshot_path": raw_path, "sha256": raw_hash}],
            }

        def compile_gbrain(self, *, import_pages: bool):
            compile_calls.append(import_pages)
            return {"ok": True, "phase": "import"}

    class FakeSession:
        def __init__(self):
            self.events: list[object] = []

        def add(self, value):
            self.events.append(value)

        async def commit(self):
            return None

        async def refresh(self, _value):
            return None

    class ForbiddenCompiler:
        def __init__(self, **_kwargs):
            raise AssertionError("checkpoint retry must not rebuild the Wiki")

    monkeypatch.setattr("knowledge.llm_wiki_job_runner.get_llm_wiki_service", lambda _base: FakeWiki())
    monkeypatch.setattr("knowledge.llm_wiki_job_runner.LlmWikiCompilerAgent", ForbiddenCompiler)
    job = SimpleNamespace(
        id="job-retry",
        status="running",
        current_step="starting",
        progress=5,
        finished_at=None,
        error_message=None,
        job_metadata={
            "raw_paths": [raw_path],
            "raw_hashes": {raw_path: raw_hash},
            "bundle_hash": bundle_hash,
            "compiler_model_id": "compiler",
            "wiki_stage_complete": True,
            "import_gbrain": True,
            "published_pages": ["concepts/example"],
            "publish_result": {"published_pages": ["concepts/example"]},
            "lint_result": {"ok": True},
            "run_outcome": {"outcome": "completed"},
        },
    )
    session = FakeSession()
    result = asyncio.run(
        process_llm_wiki_ingest_job(
            session,  # type: ignore[arg-type]
            base_dir=tmp_path,
            job=job,  # type: ignore[arg-type]
        )
    )
    assert result.status == "succeeded"
    assert result.job_metadata["gbrain_import_ok"] is True
    assert compile_calls == [True]


def test_wiki_only_checkpoint_does_not_import_or_report_gbrain_success(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    raw_path = "chat-session/wiki-only.md"
    raw_hash = "c" * 64
    bundle_hash = "d" * 64

    class FakeWiki:
        def freeze_ingest_inputs(self, raw_paths: list[str]):
            return {
                "schema_bundle": {"bundle_hash": bundle_hash},
                "raw_manifest": [{"snapshot_path": raw_path, "sha256": raw_hash}],
            }

        def compile_gbrain(self, *, import_pages: bool):
            raise AssertionError(f"Wiki-only task must not import gbrain: {import_pages}")

    class FakeSession:
        def __init__(self):
            self.events: list[object] = []

        def add(self, value):
            self.events.append(value)

        async def commit(self):
            return None

        async def refresh(self, _value):
            return None

    class ForbiddenCompiler:
        def __init__(self, **_kwargs):
            raise AssertionError("checkpoint retry must not rebuild the Wiki")

    monkeypatch.setattr("knowledge.llm_wiki_job_runner.get_llm_wiki_service", lambda _base: FakeWiki())
    monkeypatch.setattr("knowledge.llm_wiki_job_runner.LlmWikiCompilerAgent", ForbiddenCompiler)
    job = SimpleNamespace(
        id="job-wiki-only",
        status="running",
        current_step="starting",
        progress=5,
        finished_at=None,
        error_message=None,
        job_metadata={
            "raw_paths": [raw_path],
            "raw_hashes": {raw_path: raw_hash},
            "bundle_hash": bundle_hash,
            "compiler_model_id": "compiler",
            "wiki_stage_complete": True,
            "import_gbrain": False,
            "published_pages": ["concepts/example"],
            "publish_result": {"published_pages": ["concepts/example"]},
            "lint_result": {"ok": True},
            "run_outcome": {"outcome": "completed"},
        },
    )
    session = FakeSession()
    result = asyncio.run(
        process_llm_wiki_ingest_job(
            session,  # type: ignore[arg-type]
            base_dir=tmp_path,
            job=job,  # type: ignore[arg-type]
        )
    )
    assert result.status == "succeeded"
    assert result.job_metadata["gbrain_import_ok"] is None
    final_event = session.events[-1]
    assert final_event.event_metadata["gbrain_import_ok"] is None
    assert "gbrain" not in final_event.message.lower()


def test_llm_wiki_ingest_job_freezes_raw_schema_and_compiler_model(
    wiki_env: LlmWikiService,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "config.get_llm_wiki_compiler_agent_config",
        lambda: {
            "model_id": "provider:endpoint:wiki-model",
            "model": "wiki-model",
            "provider": "provider",
        },
    )
    raw = wiki_env.snapshot_raw(
        source_id="existing-kb",
        asset_id="queued-doc",
        title="Queued source",
        content="# Queued source\n\nEvidence.",
    )

    async def run() -> None:
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'jobs.db'}")
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with factory() as session:
            job = await create_llm_wiki_ingest_job(
                session,
                base_dir=Path(__file__).resolve().parent.parent,
                raw_paths=[raw["snapshot_path"]],
            )
            payload = job_to_list_dict(job)
            assert payload["metadata"]["kind"] == LLM_WIKI_INGEST_KIND
            assert payload["metadata"]["raw_paths"] == [raw["snapshot_path"]]
            assert payload["metadata"]["raw_count"] == 1
            assert len(str(payload["metadata"]["bundle_hash"])) == 64
            assert len(str(payload["metadata"]["agents_sha256"])) == 64
            assert payload["metadata"]["compiler_model_id"] == "provider:endpoint:wiki-model"
            assert payload["metadata"]["compiler_model"] == "wiki-model"
            assert payload["metadata"]["compiler_runtime"] == "llm_wiki_compiler_agent"
            assert payload["metadata"]["import_gbrain"] is False
            assert payload["publish_targets"] == ["llm_wiki"]
            assert "GBrain" not in payload["title"]
            duplicate = await create_llm_wiki_ingest_job(
                session,
                base_dir=Path(__file__).resolve().parent.parent,
                raw_paths=[raw["snapshot_path"]],
            )
            assert duplicate.id == job.id
            gbrain_job = await create_llm_wiki_ingest_job(
                session,
                base_dir=Path(__file__).resolve().parent.parent,
                raw_paths=[raw["snapshot_path"]],
                import_gbrain=True,
            )
            gbrain_payload = job_to_list_dict(gbrain_job)
            assert gbrain_job.id != job.id
            assert gbrain_payload["metadata"]["import_gbrain"] is True
            assert gbrain_payload["publish_targets"] == ["llm_wiki", "gbrain"]
            assert "GBrain" in gbrain_payload["title"]
        await engine.dispose()

    asyncio.run(run())


def test_dedicated_compiler_agent_runs_only_required_tools_in_order(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert "source_refs、文件路径、URL 和引用字段不是已授权内容" in COMPILER_SYSTEM_PROMPT
    assert "index_md 只用于解析已有 slug，不是事实证据" in COMPILER_SYSTEM_PROMPT
    assert "不得为了避免孤立页面而强行互链" in COMPILER_SYSTEM_PROMPT
    assert "严格保留专有名词与主客体" in COMPILER_SYSTEM_PROMPT
    assert "先规划 Raw 明确支持的实体、主题、页面类型和关系" in BACKGROUND_INGEST_GROUNDING_RULES
    assert "Index 只用于解析 slug，不是事实证据" in BACKGROUND_INGEST_GROUNDING_RULES
    messages = [HumanMessage(content="compile")]

    def ai_call(name: str, call_id: str, args: dict[str, object]) -> AIMessage:
        return AIMessage(
            content="",
            tool_calls=[{"name": name, "args": args, "id": call_id, "type": "tool_call"}],
        )

    states = []
    for message in (
        ai_call("llm_wiki_context", "context-1", {"operation": "ingest", "raw_paths": ["one.md"]}),
        ToolMessage(
            content=json.dumps({"schema_bundle": {"bundle_hash": "a" * 64}}),
            name="llm_wiki_context",
            tool_call_id="context-1",
        ),
        ai_call("llm_wiki_publish", "publish-1", {"pages": [], "raw_paths": ["one.md"]}),
        ToolMessage(
            content=json.dumps({"published": True, "pages": ["frameworks/one"]}),
            name="llm_wiki_publish",
            tool_call_id="publish-1",
        ),
        ai_call("llm_wiki_lint", "lint-1", {}),
        ToolMessage(content=json.dumps({"ok": True, "errors": []}), name="llm_wiki_lint", tool_call_id="lint-1"),
        AIMessage(content="编译完成。"),
    ):
        messages = [*messages, message]
        states.append({"messages": messages})

    stream_config: dict[str, object] = {}

    class FakeAgent:
        async def astream(self, *_args, **kwargs):
            stream_config.update(kwargs.get("config") or {})
            for state in states:
                yield state

    compiler = LlmWikiCompilerAgent(base_dir=tmp_path, model_id="wiki-model-id")
    monkeypatch.setattr(compiler, "_build", lambda: FakeAgent())
    events: list[tuple[str, str]] = []

    async def run() -> dict[str, object]:
        return await compiler.run(
            "compile",
            job_id="job-1",
            raw_paths=["one.md"],
            on_tool_event=lambda phase, name, _payload: events.append((phase, name)),
        )

    result = asyncio.run(run())
    assert list(result["called"]) == ["llm_wiki_context", "llm_wiki_publish", "llm_wiki_lint"]
    assert events == [
        ("start", "llm_wiki_context"),
        ("end", "llm_wiki_context"),
        ("start", "llm_wiki_publish"),
        ("end", "llm_wiki_publish"),
        ("start", "llm_wiki_lint"),
        ("end", "llm_wiki_lint"),
    ]
    assert result["final_text"] == "编译完成。"
    assert "configurable" not in stream_config
    assert stream_config["metadata"] == {
        "runtime": "llm_wiki_compiler_agent",
        "job_id": "job-1",
    }


def test_dedicated_compiler_agent_retries_rejected_publish(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    messages = [HumanMessage(content="compile")]

    def ai_call(name: str, call_id: str, args: dict[str, object]) -> AIMessage:
        return AIMessage(content="", tool_calls=[{"name": name, "args": args, "id": call_id, "type": "tool_call"}])

    failed = {
        "published": False,
        "lint": {
            "ok": False,
            "errors": [
                {"code": "schema_drift", "path": "wiki/frameworks/one.md", "message": "expected schema_version '0.4.0'"}
            ],
        },
    }
    states = []
    for message in (
        ai_call("llm_wiki_context", "context-1", {"operation": "ingest", "raw_paths": ["one.md"]}),
        ToolMessage(
            content=json.dumps({"schema_bundle": {"bundle_hash": "a" * 64}}),
            name="llm_wiki_context",
            tool_call_id="context-1",
        ),
        ai_call("llm_wiki_publish", "publish-1", {"pages": [], "raw_paths": ["one.md"]}),
        ToolMessage(content=json.dumps(failed), name="llm_wiki_publish", tool_call_id="publish-1"),
        ai_call("llm_wiki_publish", "publish-2", {"pages": [], "raw_paths": ["one.md"]}),
        ToolMessage(
            content=json.dumps({"published": True, "pages": ["frameworks/one"]}),
            name="llm_wiki_publish",
            tool_call_id="publish-2",
        ),
        ai_call("llm_wiki_lint", "lint-1", {}),
        ToolMessage(content=json.dumps({"ok": True, "errors": []}), name="llm_wiki_lint", tool_call_id="lint-1"),
    ):
        messages = [*messages, message]
        states.append({"messages": messages})

    class FakeAgent:
        async def astream(self, *_args, **_kwargs):
            for state in states:
                yield state

    compiler = LlmWikiCompilerAgent(base_dir=tmp_path)
    monkeypatch.setattr(compiler, "_build", lambda: FakeAgent())
    events: list[tuple[str, str]] = []
    result = asyncio.run(
        compiler.run(
            "compile",
            job_id="job-retry",
            raw_paths=["one.md"],
            on_tool_event=lambda phase, name, _payload: events.append((phase, name)),
        )
    )

    assert result["called"]["llm_wiki_publish"]["published"] is True
    assert events.count(("start", "llm_wiki_publish")) == 2
    assert events.count(("end", "llm_wiki_publish")) == 2


def test_initialize_dedicated_gbrain_runtime_uses_existing_postgres(
    wiki_env: LlmWikiService,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    def fake_run(command, **kwargs):
        captured["command"] = command
        environment = kwargs["env"]
        runtime_home = Path(environment["GBRAIN_HOME"])
        config_path = runtime_home / ".gbrain" / "config.json"
        config_path.parent.mkdir(parents=True, exist_ok=True)
        config_path.write_text("{}\n", encoding="utf-8")
        return SimpleNamespace(returncode=0, stdout="{}", stderr="")

    monkeypatch.setattr("knowledge.llm_wiki.resolve_gbrain_binary", lambda: "/usr/bin/true")
    monkeypatch.setattr(
        "knowledge.llm_wiki.inspect_pgvector_dsn_sync",
        lambda _url: {
            "required": True,
            "available": True,
            "installed": False,
            "version": "0.8.5",
            "server_major": 16,
            "install_command": "",
        },
    )
    ai_runtime = {
        "embedding_model": "dashscope:text-embedding-v4",
        "embedding_dimensions": 1024,
        "chat_model": "deepseek:deepseek-v4-pro",
        "environment": {},
        "provider_base_urls": {
            "dashscope": "https://dashscope.aliyuncs.com/compatible-mode/v1",
            "deepseek": "https://api.deepseek.com",
        },
        "embedding": {"name": "text-embedding-v4", "provider": "dashscope", "dimension": 1024},
        "think": {"name": "deepseek-v4-pro", "provider": "deepseek"},
    }

    def fake_ai_environment(base):
        return dict(base), ai_runtime

    monkeypatch.setattr("knowledge.llm_wiki.apply_gbrain_ai_environment", fake_ai_environment)
    monkeypatch.setattr("knowledge.llm_wiki.subprocess.run", fake_run)
    result = wiki_env.initialize_gbrain_runtime("postgresql://user:secret@127.0.0.1:5432/gbrain")
    assert result["ok"] is True
    assert result["pgvector"]["available"] is True
    assert captured["command"][1:3] == ["init", "--url"]
    assert "--no-embedding" not in captured["command"]
    assert captured["command"][captured["command"].index("--embedding-model") + 1] == "dashscope:text-embedding-v4"
    assert captured["command"][captured["command"].index("--chat-model") + 1] == "deepseek:deepseek-v4-pro"
    assert (wiki_env.gbrain_runtime_home / ".gbrain" / "schema-packs" / "puddingclaw-wiki" / "pack.yaml").is_file()
    runtime_config = json.loads((wiki_env.gbrain_runtime_home / ".gbrain" / "config.json").read_text(encoding="utf-8"))
    assert runtime_config["provider_base_urls"]["dashscope"].endswith("/compatible-mode/v1")
    assert wiki_env.workspace_status()["gbrain"]["postgres_configured"] is True


def test_initialize_gbrain_stops_before_cli_when_pgvector_is_missing(
    wiki_env: LlmWikiService,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("knowledge.llm_wiki.resolve_gbrain_binary", lambda: "/usr/bin/true")
    monkeypatch.setattr(
        "knowledge.llm_wiki.inspect_pgvector_dsn_sync",
        lambda _url: {
            "required": True,
            "available": False,
            "installed": False,
            "version": "",
            "server_major": 16,
            "install_command": "./scripts/start-local-infra.sh",
        },
    )

    def unexpected_run(*_args, **_kwargs):
        raise AssertionError("gbrain CLI must not run without pgvector")

    monkeypatch.setattr("knowledge.llm_wiki.subprocess.run", unexpected_run)
    with pytest.raises(LlmWikiError, match=r"\./scripts/start-local-infra\.sh"):
        wiki_env.initialize_gbrain_runtime("postgresql://pet@127.0.0.1:5432/llm_wiki")


def test_workspace_status_includes_persistent_gbrain_import_history(
    wiki_env: LlmWikiService,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = wiki_env.gbrain_runtime_home / ".gbrain" / "config.json"
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(
        json.dumps({"database_url": "postgresql://user:secret@127.0.0.1:5432/llm_wiki"}),
        encoding="utf-8",
    )

    postgres = wiki_env.workspace_status()["gbrain"]["postgres"]
    assert postgres == {
        "configured": True,
        "host": "127.0.0.1",
        "port": 5432,
        "database": "llm_wiki",
        "username": "user",
    }

    async def fake_read(database_url: str, *, limit: int = 10) -> dict[str, object]:
        assert database_url.endswith("/llm_wiki")
        assert limit == 10
        return {
            "available": True,
            "counts": {"pages": 3, "links": 2, "chunks": 8, "imports": 1},
            "records": [
                {
                    "id": 1,
                    "source_id": "default",
                    "source_type": "directory",
                    "pages_updated": ["frameworks/gbrain", "concepts/compiled-rag"],
                    "summary": "Imported 3 pages, 0 skipped, 8 chunks",
                    "created_at": "2026-07-31T00:15:39+08:00",
                }
            ],
        }

    monkeypatch.setattr("knowledge.llm_wiki._read_gbrain_import_status", fake_read)
    imports = wiki_env.workspace_status()["gbrain"]["imports"]
    assert imports["available"] is True
    assert imports["counts"] == {"pages": 3, "links": 2, "chunks": 8, "imports": 1}
    assert imports["records"][0]["pages_updated"] == ["frameworks/gbrain", "concepts/compiled-rag"]
    assert "database_url" not in imports
    assert "secret" not in json.dumps(imports)


def test_publish_validates_schema_rebuilds_index_and_appends_log(wiki_env: LlmWikiService) -> None:
    raw = wiki_env.snapshot_raw(
        source_id="existing-kb",
        asset_id="doc-1",
        title="Source One",
        content="# Source One\n\nEvidence.\n",
    )
    bundle = wiki_env.schema.bundle()
    result = wiki_env.publish(
        pages=[
            {
                "slug": "concepts/compiled-rag",
                "content": _page("编译式 RAG", "concept", raw["snapshot_path"], "systems/gbrain"),
            },
            {
                "slug": "systems/gbrain",
                "content": _page("GBrain", "system", raw["snapshot_path"], "concepts/compiled-rag"),
            },
        ],
        expected_bundle_hash=bundle["bundle_hash"],
        summary="初版编译",
        model="test:model",
        raw_paths=[raw["snapshot_path"]],
    )
    assert result["published"] is True
    assert result["lint"]["ok"] is True
    raw_status = wiki_env.workspace_status()["raw"][0]
    assert raw_status["compiled"] is True
    assert raw_status["compiled_pages"] == ["concepts/compiled-rag", "systems/gbrain"]
    assert raw_status["compiled_job_ids"] == [result["job_id"]]

    changed = wiki_env.snapshot_raw(
        source_id="existing-kb",
        asset_id="doc-1",
        title="Source One",
        content="# Source One\n\nChanged evidence.\n",
    )
    version_status = {item["snapshot_path"]: item for item in wiki_env.workspace_status()["raw"]}
    assert version_status[raw["snapshot_path"]]["compiled"] is True
    assert version_status[changed["snapshot_path"]]["compiled"] is False

    (wiki_env.raw_dir / raw["snapshot_path"]).write_text("tampered\n", encoding="utf-8")
    compromised = {item["snapshot_path"]: item for item in wiki_env.workspace_status()["raw"]}
    assert compromised[raw["snapshot_path"]]["compiled"] is False
    assert compromised[raw["snapshot_path"]]["integrity"].startswith("mismatch:")
    index = (wiki_env.wiki_dir / "index.md").read_text(encoding="utf-8")
    assert "[[concepts/compiled-rag|编译式 RAG]]" in index
    assert "[[systems/gbrain|GBrain]]" in index
    log = (wiki_env.wiki_dir / "log.md").read_text(encoding="utf-8")
    assert "append-only" not in log or "#" in log
    assert result["job_id"] in log

    query = wiki_env.query("GBrain 编译", limit=1)
    assert query["source_policy"] == "wiki-only"
    assert len(query["pages"]) == 1
    assert query["references"] == [
        {
            "slug": "concepts/compiled-rag",
            "title": "编译式 RAG",
            "type": "concept",
            "uri": "/knowledge/llm-wiki/wiki/concepts/compiled-rag.md",
            "score": query["references"][0]["score"],
            "excerpt": query["references"][0]["excerpt"],
            "sources": [raw["snapshot_path"]],
        }
    ]
    assert query["knowledge_gap"] is False

    answer_context, structured_sources = parse_tool_result(
        LlmWikiQueryTool(base_dir=wiki_env.base_dir)._run("GBrain 编译", limit=1),
        "call-wiki-query",
    )
    encoded_query = json.loads(answer_context)
    assert encoded_query["references"][0]["slug"] == "concepts/compiled-rag"
    assert structured_sources[0]["source_type"] == "llm_wiki"
    assert structured_sources[0]["uri"] == "/knowledge/llm-wiki/wiki/concepts/compiled-rag.md"
    assert structured_sources[0]["metadata"]["raw_sources"] == [raw["snapshot_path"]]


def test_publish_normalizes_legacy_raw_prefix_to_manifest_snapshot_path(wiki_env: LlmWikiService) -> None:
    raw = wiki_env.snapshot_raw(
        source_id="existing-kb",
        asset_id="legacy-source-prefix",
        title="Legacy source prefix",
        content="# Source\n\nEvidence.\n",
    )
    bundle = wiki_env.schema.bundle()
    result = wiki_env.publish(
        pages=[
            {
                "slug": "concepts/source-prefix",
                "content": _page(
                    "Source Prefix",
                    "concept",
                    f"raw/{raw['snapshot_path']}",
                    "concepts/source-prefix",
                ),
            }
        ],
        expected_bundle_hash=bundle["bundle_hash"],
        summary="Normalize source prefix",
        model="test:model",
        raw_paths=[f"raw/{raw['snapshot_path']}"],
    )

    assert result["published"] is True
    published = (wiki_env.wiki_dir / "concepts" / "source-prefix.md").read_text(encoding="utf-8")
    assert f"- {raw['snapshot_path']}" in published
    assert f"raw/{raw['snapshot_path']}" not in published
    assert result["lint"]["ok"] is True


def test_publish_normalizes_schema_identity_to_bundle_version(wiki_env: LlmWikiService) -> None:
    raw = wiki_env.snapshot_raw(
        source_id="existing-kb",
        asset_id="schema-identity",
        title="Schema identity",
        content="# Source\n\nEvidence.\n",
    )
    bundle = wiki_env.schema.bundle()
    document = bundle["brain_schema"]["document"]
    page = _page("Schema Identity", "concept", raw["snapshot_path"], "concepts/schema-identity")
    page = page.replace(
        "schema_version: 0.1.0",
        f"schema_version: {document['schema_id']}@{document['bundle_version']}",
    )

    result = wiki_env.publish(
        pages=[{"slug": "concepts/schema-identity", "content": page}],
        expected_bundle_hash=bundle["bundle_hash"],
        summary="Normalize schema identity",
        model="test:model",
        raw_paths=[raw["snapshot_path"]],
    )

    assert result["published"] is True
    published = (wiki_env.wiki_dir / "concepts" / "schema-identity.md").read_text(encoding="utf-8")
    assert f"schema_version: {document['bundle_version']}" in published
    assert f"{document['schema_id']}@{document['bundle_version']}" not in published


def test_publish_preserves_type_directories_for_gbrain(wiki_env: LlmWikiService) -> None:
    raw = wiki_env.snapshot_raw(
        source_id="existing-kb",
        asset_id="typed-paths",
        title="Typed Paths",
        content="# Typed Paths\n\nEvidence.\n",
    )
    bundle = wiki_env.schema.bundle()
    result = wiki_env.publish(
        pages=[
            {
                "slug": "papers/compiled-rag",
                "content": _page(
                    "编译式 RAG",
                    "research_paper",
                    raw["snapshot_path"],
                    "systems/gbrain",
                ),
            },
            {
                "slug": "systems/gbrain",
                "content": _page(
                    "GBrain",
                    "system",
                    raw["snapshot_path"],
                    "papers/compiled-rag",
                ),
            },
        ],
        expected_bundle_hash=bundle["bundle_hash"],
        summary="类型目录编译",
        model="test:model",
        raw_paths=[raw["snapshot_path"]],
    )
    assert result["published"] is True
    assert (wiki_env.wiki_dir / "papers" / "compiled-rag.md").is_file()
    assert (wiki_env.wiki_dir / "systems" / "gbrain.md").is_file()
    index = (wiki_env.wiki_dir / "index.md").read_text(encoding="utf-8")
    assert "[[papers/compiled-rag|编译式 RAG]]" in index
    assert "[[systems/gbrain|GBrain]]" in index
    assert wiki_env.query("GBrain", limit=2)["pages"][0]["slug"] == "systems/gbrain"
    source_dir = wiki_env._gbrain_source_dir()
    assert source_dir is not None
    assert (source_dir / "papers" / "compiled-rag.md").is_file()


def test_retire_pages_rewrites_links_rebuilds_index_and_is_idempotent(wiki_env: LlmWikiService) -> None:
    raw = wiki_env.snapshot_raw(
        source_id="existing-kb",
        asset_id="retirement-source",
        title="Retirement Source",
        content="# Retirement Source\n\nEvidence.\n",
    )
    bundle = wiki_env.schema.bundle()
    published = wiki_env.publish(
        pages=[
            {
                "slug": "concepts/duplicate-practice",
                "content": _page(
                    "Duplicate Practice",
                    "concept",
                    raw["snapshot_path"],
                    "practices/canonical-practice",
                ),
            },
            {
                "slug": "practices/canonical-practice",
                "content": _page(
                    "Canonical Practice",
                    "engineering_practice",
                    raw["snapshot_path"],
                    "practices/canonical-practice",
                ),
            },
            {
                "slug": "systems/example",
                "content": _page(
                    "Example System",
                    "system",
                    raw["snapshot_path"],
                    "concepts/duplicate-practice",
                ).replace(
                    "[[concepts/duplicate-practice]]",
                    "[[concepts/duplicate-practice|旧实践]]",
                ),
            },
        ],
        expected_bundle_hash=bundle["bundle_hash"],
        summary="Publish duplicate",
        model="test:model",
        raw_paths=[raw["snapshot_path"]],
    )
    assert published["published"] is True

    result = wiki_env.retire_pages(
        retirements=[
            {
                "slug": "concepts/duplicate-practice",
                "replacement": "practices/canonical-practice",
            }
        ],
        summary="Remove duplicate classification",
    )

    assert result["retired"] is True
    assert result["already_retired"] is False
    assert not (wiki_env.wiki_dir / "concepts" / "duplicate-practice.md").exists()
    assert (wiki_env.wiki_dir / "practices" / "canonical-practice.md").is_file()
    consumer = (wiki_env.wiki_dir / "systems" / "example.md").read_text(encoding="utf-8")
    assert "[[practices/canonical-practice|旧实践]]" in consumer
    assert "[[concepts/duplicate-practice" not in consumer
    index = (wiki_env.wiki_dir / "index.md").read_text(encoding="utf-8")
    assert "[[concepts/duplicate-practice" not in index
    assert "[[practices/canonical-practice|Canonical Practice]]" in index
    log = (wiki_env.wiki_dir / "log.md").read_text(encoding="utf-8")
    assert "## [" in log and "] retire | Remove duplicate classification" in log
    assert "[[concepts/duplicate-practice]] -> [[practices/canonical-practice]]" in log
    archived = wiki_env.root / result["archive_dir"] / "concepts" / "duplicate-practice.md"
    assert archived.is_file()
    assert result["lint"]["ok"] is True

    raw_status = {item["snapshot_path"]: item for item in wiki_env.workspace_status()["raw"]}
    assert raw_status[raw["snapshot_path"]]["compiled_pages"] == [
        "practices/canonical-practice",
        "systems/example",
    ]

    repeated = wiki_env.retire_pages(
        retirements=[
            {
                "slug": "concepts/duplicate-practice",
                "replacement": "practices/canonical-practice",
            }
        ],
        summary="Repeat safely",
    )
    assert repeated["retired"] is False
    assert repeated["already_retired"] is True
    assert repeated["job_id"] == result["job_id"]


def test_retired_page_source_raw_does_not_reenter_pending_queue(wiki_env: LlmWikiService) -> None:
    obsolete_raw = wiki_env.snapshot_raw(
        source_id="existing-kb",
        asset_id="obsolete-source",
        title="Obsolete Source",
        content="# Obsolete Source\n",
    )
    replacement_raw = wiki_env.snapshot_raw(
        source_id="existing-kb",
        asset_id="replacement-source",
        title="Replacement Source",
        content="# Replacement Source\n",
    )
    bundle = wiki_env.schema.bundle()
    assert wiki_env.publish(
        pages=[
            {
                "slug": "concepts/obsolete",
                "content": _page(
                    "Obsolete",
                    "concept",
                    obsolete_raw["snapshot_path"],
                    "practices/replacement",
                ),
            },
            {
                "slug": "practices/replacement",
                "content": _page(
                    "Replacement",
                    "engineering_practice",
                    replacement_raw["snapshot_path"],
                    "practices/replacement",
                ),
            },
        ],
        expected_bundle_hash=bundle["bundle_hash"],
        summary="Publish replacement pair",
        model="test:model",
        raw_paths=[obsolete_raw["snapshot_path"], replacement_raw["snapshot_path"]],
    )["published"]

    wiki_env.retire_pages(
        retirements=[
            {
                "slug": "concepts/obsolete",
                "replacement": "practices/replacement",
            }
        ],
        summary="Retire obsolete output",
    )

    raw_status = {item["snapshot_path"]: item for item in wiki_env.workspace_status()["raw"]}
    assert raw_status[obsolete_raw["snapshot_path"]]["compiled"] is True
    assert raw_status[obsolete_raw["snapshot_path"]]["compiled_pages"] == []
    assert raw_status[replacement_raw["snapshot_path"]]["compiled"] is True


def test_retire_pages_rejects_missing_replacement_without_mutation(wiki_env: LlmWikiService) -> None:
    raw = wiki_env.snapshot_raw(
        source_id="existing-kb",
        asset_id="retirement-invalid",
        title="Retirement Invalid",
        content="# Retirement Invalid\n",
    )
    bundle = wiki_env.schema.bundle()
    published = wiki_env.publish(
        pages=[
            {
                "slug": "concepts/keep-me",
                "content": _page("Keep Me", "concept", raw["snapshot_path"], "concepts/keep-me"),
            }
        ],
        expected_bundle_hash=bundle["bundle_hash"],
        summary="Publish retained page",
        model="test:model",
        raw_paths=[raw["snapshot_path"]],
    )
    assert published["published"] is True
    before_index = (wiki_env.wiki_dir / "index.md").read_text(encoding="utf-8")
    before_log = (wiki_env.wiki_dir / "log.md").read_text(encoding="utf-8")

    with pytest.raises(LlmWikiError, match="replacement Wiki pages do not exist"):
        wiki_env.retire_pages(
            retirements=[{"slug": "concepts/keep-me", "replacement": "concepts/missing"}],
            summary="Must fail",
        )

    assert (wiki_env.wiki_dir / "concepts" / "keep-me.md").is_file()
    assert (wiki_env.wiki_dir / "index.md").read_text(encoding="utf-8") == before_index
    assert (wiki_env.wiki_dir / "log.md").read_text(encoding="utf-8") == before_log


def test_retire_pages_tool_optionally_soft_deletes_gbrain(
    wiki_env: LlmWikiService,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    raw = wiki_env.snapshot_raw(
        source_id="existing-kb",
        asset_id="retirement-tool",
        title="Retirement Tool",
        content="# Retirement Tool\n",
    )
    bundle = wiki_env.schema.bundle()
    assert (
        wiki_env.publish(
            pages=[
                {
                    "slug": "concepts/tool-old",
                    "content": _page("Tool Old", "concept", raw["snapshot_path"], "concepts/tool-new"),
                },
                {
                    "slug": "concepts/tool-new",
                    "content": _page("Tool New", "concept", raw["snapshot_path"], "concepts/tool-new"),
                },
            ],
            expected_bundle_hash=bundle["bundle_hash"],
            summary="Publish tool pages",
            model="test:model",
            raw_paths=[raw["snapshot_path"]],
        )["published"]
        is True
    )
    calls: list[list[str]] = []

    def fake_retire_gbrain(_service: LlmWikiService, slugs: list[str]) -> dict[str, object]:
        calls.append(slugs)
        return {"ok": True, "soft_deleted": slugs}

    monkeypatch.setattr(LlmWikiService, "retire_gbrain_pages", fake_retire_gbrain)
    payload = json.loads(
        LlmWikiRetirePagesTool(base_dir=wiki_env.base_dir)._run(
            retirements=[{"slug": "concepts/tool-old", "replacement": "concepts/tool-new"}],
            summary="Tool retirement",
            sync_gbrain=True,
        )
    )

    assert payload["ok"] is True
    assert payload["wiki"]["retired"] is True
    assert payload["gbrain"]["soft_deleted"] == ["concepts/tool-old"]
    assert calls == [["concepts/tool-old"]]


def test_retire_gbrain_pages_syncs_current_wiki_before_soft_delete(
    wiki_env: LlmWikiService,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    order: list[object] = []

    def fake_compile(*, import_pages: bool) -> dict[str, object]:
        order.append(("import", import_pages))
        return {"ok": True, "phase": "import"}

    def fake_run(command: list[str], *, environment: dict[str, str], timeout: int = 60) -> dict[str, object]:
        order.append(("run", command, environment, timeout))
        return {"ok": True, "command": command, "exit_code": 0, "stdout": "", "stderr": ""}

    monkeypatch.setattr(wiki_env, "_compile_gbrain_unlocked", fake_compile)
    monkeypatch.setattr("knowledge.llm_wiki.resolve_gbrain_binary", lambda: "/fake/gbrain")
    monkeypatch.setattr(wiki_env, "_production_gbrain_env", lambda **_kwargs: ({"ENV": "test"}, Path("/runtime")))
    monkeypatch.setattr("knowledge.llm_wiki.gbrain_subprocess_environment", lambda _binary, env: env)
    monkeypatch.setattr(wiki_env, "_run", fake_run)

    result = wiki_env.retire_gbrain_pages(["concepts/old"])

    assert result["ok"] is True
    assert result["soft_deleted"] == ["concepts/old"]
    assert order[0] == ("import", True)
    assert order[1][0:2] == ("run", ["/fake/gbrain", "delete", "concepts/old"])


def test_invalid_candidate_is_not_published(wiki_env: LlmWikiService) -> None:
    raw = wiki_env.snapshot_raw(
        source_id="existing-kb",
        asset_id="doc-2",
        title="Source Two",
        content="# Source Two\n",
    )
    bundle = wiki_env.schema.bundle()
    result = wiki_env.publish(
        pages=[
            {
                "slug": "concepts/broken-page",
                "content": _page("Broken", "not-in-schema", raw["snapshot_path"], "concepts/missing-page"),
            }
        ],
        expected_bundle_hash=bundle["bundle_hash"],
        summary="invalid",
        model="test:model",
        raw_paths=[raw["snapshot_path"]],
    )
    assert result["published"] is False
    codes = {item["code"] for item in result["lint"]["errors"]}
    assert {"unknown_page_type", "broken_wikilink"}.issubset(codes)
    assert not (wiki_env.wiki_dir / "concepts" / "broken-page.md").exists()
    assert wiki_env.workspace_status()["raw"][0]["compiled"] is False


def test_wikilink_requires_the_gbrain_directory_prefix(wiki_env: LlmWikiService) -> None:
    raw = wiki_env.snapshot_raw(source_id="kb", asset_id="prefix", title="Prefix", content="# Prefix\n")
    bundle = wiki_env.schema.bundle()
    page = _page("Prefix", "concept", raw["snapshot_path"], "concepts/prefix-page").replace(
        "[[concepts/prefix-page]]",
        "[[prefix-page]]",
    )
    result = wiki_env.publish(
        pages=[{"slug": "concepts/prefix-page", "content": page}],
        expected_bundle_hash=bundle["bundle_hash"],
        summary="missing wikilink prefix",
        model="test:model",
        raw_paths=[raw["snapshot_path"]],
    )
    assert result["published"] is False
    assert "invalid_wikilink" in {item["code"] for item in result["lint"]["errors"]}


def test_lint_requires_source_to_link_back_to_sourced_media(wiki_env: LlmWikiService) -> None:
    raw = wiki_env.snapshot_raw(source_id="kb", asset_id="source-backlink", title="Article", content="# Article\n")
    bundle = wiki_env.schema.bundle()
    media = _page("Article", "media", raw["snapshot_path"], "sources/publisher").replace(
        "参见 [[sources/publisher]]",
        "本文章 sourced_from [[sources/publisher|Publisher]]",
    )
    source = _page("Publisher", "source", raw["snapshot_path"], "sources/publisher").replace(
        "这是 Publisher 的稳定知识摘要，参见 [[sources/publisher]]。",
        "## 已收录内容\n\n- Article（详见 media 页面）",
    )

    result = wiki_env.publish(
        pages=[
            {"slug": "media/article", "content": media},
            {"slug": "sources/publisher", "content": source},
        ],
        expected_bundle_hash=bundle["bundle_hash"],
        summary="missing source backlink",
        model="test:model",
        raw_paths=[raw["snapshot_path"]],
    )

    assert result["published"] is False
    assert "missing_source_backlink" in {item["code"] for item in result["lint"]["errors"]}


def test_lint_requires_collected_media_to_declare_sourced_from(wiki_env: LlmWikiService) -> None:
    raw = wiki_env.snapshot_raw(source_id="kb", asset_id="media-source", title="Article", content="# Article\n")
    bundle = wiki_env.schema.bundle()
    media = _page("Article", "media", raw["snapshot_path"], "media/article")
    source = _page("Publisher", "source", raw["snapshot_path"], "media/article").replace(
        "这是 Publisher 的稳定知识摘要，参见 [[media/article]]。",
        "## 已收录内容\n\n- [[media/article|Article]]",
    )

    result = wiki_env.publish(
        pages=[
            {"slug": "media/article", "content": media},
            {"slug": "sources/publisher", "content": source},
        ],
        expected_bundle_hash=bundle["bundle_hash"],
        summary="missing media source relation",
        model="test:model",
        raw_paths=[raw["snapshot_path"]],
    )

    assert result["published"] is False
    assert "missing_media_source_relation" in {item["code"] for item in result["lint"]["errors"]}


def test_lint_accepts_bidirectional_source_collection_links(wiki_env: LlmWikiService) -> None:
    raw = wiki_env.snapshot_raw(source_id="kb", asset_id="source-pair", title="Article", content="# Article\n")
    bundle = wiki_env.schema.bundle()
    media = _page("Article", "media", raw["snapshot_path"], "sources/publisher").replace(
        "参见 [[sources/publisher]]",
        "本文章 sourced_from [[sources/publisher|Publisher]]",
    )
    source = _page("Publisher", "source", raw["snapshot_path"], "media/article").replace(
        "这是 Publisher 的稳定知识摘要，参见 [[media/article]]。",
        "## 已收录内容\n\n- [[media/article|Article]]",
    )

    result = wiki_env.publish(
        pages=[
            {"slug": "media/article", "content": media},
            {"slug": "sources/publisher", "content": source},
        ],
        expected_bundle_hash=bundle["bundle_hash"],
        summary="complete source pair",
        model="test:model",
        raw_paths=[raw["snapshot_path"]],
    )

    assert result["published"] is True


def test_publish_rejects_a_duplicated_workspace_wiki_prefix(wiki_env: LlmWikiService) -> None:
    raw = wiki_env.snapshot_raw(source_id="kb", asset_id="duplicate-root", title="Duplicate", content="# D\n")
    bundle = wiki_env.schema.bundle()
    result = wiki_env.publish(
        pages=[
            {
                "slug": "wiki/concepts/duplicate-root",
                "content": _page(
                    "Duplicate Root",
                    "concept",
                    raw["snapshot_path"],
                    "wiki/concepts/duplicate-root",
                ),
            }
        ],
        expected_bundle_hash=bundle["bundle_hash"],
        summary="duplicate wiki root",
        model="test:model",
        raw_paths=[raw["snapshot_path"]],
    )
    assert result["published"] is False
    codes = {item["code"] for item in result["lint"]["errors"]}
    assert {"duplicate_wiki_root", "duplicate_wiki_root_link", "page_path_type_mismatch"}.issubset(codes)


def test_migrate_legacy_workspace_wiki_prefix_preserves_log_prefix(wiki_env: LlmWikiService) -> None:
    raw = wiki_env.snapshot_raw(source_id="kb", asset_id="migration", title="Migration", content="# Migration\n")
    legacy_page = _page(
        "Compiled RAG",
        "concept",
        raw["snapshot_path"],
        "frameworks/gbrain",
    )
    framework_page = _page(
        "GBrain",
        "software_framework",
        raw["snapshot_path"],
        "wiki/concepts/compiled-rag",
    )
    legacy_path = wiki_env.wiki_dir / "wiki" / "concepts" / "compiled-rag.md"
    legacy_path.parent.mkdir(parents=True)
    legacy_path.write_text(legacy_page, encoding="utf-8")
    framework_path = wiki_env.wiki_dir / "frameworks" / "gbrain.md"
    framework_path.parent.mkdir(parents=True)
    framework_path.write_text(framework_page, encoding="utf-8")
    existing_pages = {
        "wiki/concepts/compiled-rag": legacy_page,
        "frameworks/gbrain": framework_page,
    }
    (wiki_env.wiki_dir / "index.md").write_text(wiki_env._build_index(existing_pages), encoding="utf-8")
    prior_log = "# Wiki Ingest Log\n\n## historical\n\n- page: [[wiki/concepts/compiled-rag]]\n"
    (wiki_env.wiki_dir / "log.md").write_text(prior_log, encoding="utf-8")

    result = wiki_env.migrate_legacy_wiki_prefixes()

    assert result["migrated"] is True
    assert result["moved"] == {"wiki/concepts/compiled-rag": "concepts/compiled-rag"}
    assert not legacy_path.exists()
    assert (wiki_env.wiki_dir / "concepts" / "compiled-rag.md").is_file()
    assert "[[concepts/compiled-rag]]" in framework_path.read_text(encoding="utf-8")
    assert "[[wiki/concepts/compiled-rag]]" not in framework_path.read_text(encoding="utf-8")
    assert (wiki_env.wiki_dir / "log.md").read_text(encoding="utf-8").startswith(prior_log)
    assert wiki_env.lint()["ok"] is True
    assert all(item["compiled"] for item in wiki_env.workspace_status()["raw"])

    revalidated = wiki_env.migrate_legacy_wiki_prefixes()
    assert revalidated["migrated"] is False
    assert revalidated["status"] == "published"
    assert all(item["compiled"] for item in wiki_env.workspace_status()["raw"])


def test_publish_requires_explicit_raw_authority_and_bound_sources(wiki_env: LlmWikiService) -> None:
    raw = wiki_env.snapshot_raw(
        source_id="private",
        asset_id="doc-private",
        title="Private Source",
        content="# Private\n",
    )
    bundle = wiki_env.schema.bundle()
    page = _page("Private", "concept", "totally-unknown", "concepts/private-page")
    with pytest.raises(Exception, match="explicit immutable raw_paths"):
        wiki_env.publish(
            pages=[{"slug": "concepts/private-page", "content": page}],
            expected_bundle_hash=bundle["bundle_hash"],
            summary="unauthorized",
            model="test:model",
            raw_paths=[],
        )
    with pytest.raises(Exception, match="not authorized"):
        wiki_env.publish(
            pages=[{"slug": "concepts/private-page", "content": page}],
            expected_bundle_hash=bundle["bundle_hash"],
            summary="unauthorized",
            model="test:model",
            raw_paths=[raw["snapshot_path"]],
        )
    assert not (wiki_env.wiki_dir / "concepts" / "private-page.md").exists()


def test_update_must_consume_a_raw_selected_for_this_ingest(wiki_env: LlmWikiService) -> None:
    first = wiki_env.snapshot_raw(source_id="kb", asset_id="a", title="A", content="# A\n")
    second = wiki_env.snapshot_raw(source_id="kb", asset_id="b", title="B", content="# B\n")
    bundle = wiki_env.schema.bundle()
    initial = wiki_env.publish(
        pages=[
            {
                "slug": "concepts/page",
                "content": _page("Page", "concept", first["snapshot_path"], "concepts/page"),
            }
        ],
        expected_bundle_hash=bundle["bundle_hash"],
        summary="initial",
        model="test:model",
        raw_paths=[first["snapshot_path"]],
    )
    assert initial["published"] is True
    with pytest.raises(LlmWikiError, match="must cite at least one raw selected"):
        wiki_env.publish(
            pages=[
                {
                    "slug": "concepts/page",
                    "content": _page("Rewritten", "concept", first["snapshot_path"], "concepts/page"),
                }
            ],
            expected_bundle_hash=bundle["bundle_hash"],
            summary="rewrite",
            model="test:model",
            raw_paths=[second["snapshot_path"]],
        )


def test_raw_identity_and_context_path_are_unambiguous(wiki_env: LlmWikiService, tmp_path: Path) -> None:
    slash = wiki_env.snapshot_raw(source_id="a/b", asset_id="doc", title="Slash", content="# same\n")
    space = wiki_env.snapshot_raw(source_id="a b", asset_id="doc", title="Space", content="# same\n")
    assert slash["snapshot_path"] != space["snapshot_path"]

    outside = tmp_path / "outside.md"
    outside.write_text("secret", encoding="utf-8")
    polluted = {
        "source_id": "bad",
        "asset_id": "bad",
        "snapshot_path": "../../outside.md",
        "sha256": "0" * 64,
    }
    with wiki_env.raw_manifest_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(polluted) + "\n")
    with pytest.raises(LlmWikiError, match="escapes raw"):
        wiki_env.operation_context("ingest", raw_paths=["../../outside.md"])


def test_real_gbrain_validates_published_wiki(wiki_env: LlmWikiService) -> None:
    if not shutil.which("gbrain"):
        pytest.skip("gbrain CLI is unavailable")
    raw = wiki_env.snapshot_raw(
        source_id="existing-kb",
        asset_id="doc-3",
        title="Source Three",
        content="# Source Three\n",
    )
    bundle = wiki_env.schema.bundle()
    published = wiki_env.publish(
        pages=[
            {
                "slug": "concepts/single-page",
                "content": _page("Single", "concept", raw["snapshot_path"], "concepts/single-page"),
            }
        ],
        expected_bundle_hash=bundle["bundle_hash"],
        summary="gbrain validation",
        model="test:model",
        raw_paths=[raw["snapshot_path"]],
    )
    assert published["published"] is True
    compiled = wiki_env.compile_gbrain()
    assert compiled["ok"] is True, compiled
    assert all(check["ok"] for check in compiled["checks"])
    assert "0 issue(s)" in compiled["checks"][-1]["stdout"]


def test_validate_only_compile_does_not_touch_configured_gbrain_home(
    wiki_env: LlmWikiService,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    if not shutil.which("gbrain"):
        pytest.skip("gbrain CLI is unavailable")
    raw = wiki_env.snapshot_raw(source_id="kb", asset_id="compile", title="Compile", content="# Compile\n")
    bundle = wiki_env.schema.bundle()
    wiki_env.publish(
        pages=[
            {
                "slug": "concepts/compile",
                "content": _page("Compile", "concept", raw["snapshot_path"], "concepts/compile"),
            }
        ],
        expected_bundle_hash=bundle["bundle_hash"],
        summary="compile",
        model="test:model",
        raw_paths=[raw["snapshot_path"]],
    )
    configured_home = tmp_path / "production-gbrain"
    pack_path = configured_home / ".gbrain" / "schema-packs" / "puddingclaw-wiki" / "pack.yaml"
    pack_path.parent.mkdir(parents=True)
    pack_path.write_text("sentinel\n", encoding="utf-8")
    monkeypatch.setenv("PUDDINGCLAW_GBRAIN_HOME", str(configured_home))
    result = wiki_env.compile_gbrain(import_pages=False)
    assert result["ok"] is True
    assert result["runtime_home"] == "isolated-temporary-home"
    assert pack_path.read_text(encoding="utf-8") == "sentinel\n"


def test_compatible_schema_upgrade_migrates_the_whole_wiki(wiki_env: LlmWikiService) -> None:
    raw = wiki_env.snapshot_raw(source_id="kb", asset_id="migration", title="Migration", content="# Migration\n")
    bundle = wiki_env.schema.bundle()
    published = wiki_env.publish(
        pages=[
            {
                "slug": "concepts/one",
                "content": _page("One", "concept", raw["snapshot_path"], "systems/two"),
            },
            {
                "slug": "systems/two",
                "content": _page("Two", "system", raw["snapshot_path"], "concepts/one"),
            },
        ],
        expected_bundle_hash=bundle["bundle_hash"],
        summary="before schema migration",
        model="test:model",
        raw_paths=[raw["snapshot_path"]],
    )
    assert published["published"] is True

    manifest = deepcopy(bundle["custom"]["manifest"])
    manifest["version"] = "0.2.0"
    manifest["link_types"].append({"name": "decides", "inverse": "decided_by"})
    upgraded = wiki_env.schema.save_custom(
        manifest,
        expected_sha256=bundle["custom"]["manifest_sha256"],
        expected_bundle_hash=bundle["bundle_hash"],
    )
    assert upgraded["schema_migration"]["migrated_pages"] == ["concepts/one", "systems/two"]
    assert "schema_version: 0.2.0" in (wiki_env.wiki_dir / "concepts" / "one.md").read_text(encoding="utf-8")
    assert "schema_version: 0.2.0" in (wiki_env.wiki_dir / "systems" / "two.md").read_text(encoding="utf-8")
    assert "schema-migrate" in (wiki_env.wiki_dir / "log.md").read_text(encoding="utf-8")
    assert wiki_env.lint()["ok"] is True
    assert wiki_env.workspace_status()["raw"][0]["compiled"] is True


def test_destructive_schema_upgrade_fails_before_activation(wiki_env: LlmWikiService) -> None:
    raw = wiki_env.snapshot_raw(source_id="kb", asset_id="destructive", title="Destructive", content="# D\n")
    bundle = wiki_env.schema.bundle()
    assert (
        wiki_env.publish(
            pages=[
                {
                    "slug": "systems/system-page",
                    "content": _page("System", "system", raw["snapshot_path"], "systems/system-page"),
                }
            ],
            expected_bundle_hash=bundle["bundle_hash"],
            summary="system page",
            model="test:model",
            raw_paths=[raw["snapshot_path"]],
        )["published"]
        is True
    )

    manifest = deepcopy(bundle["custom"]["manifest"])
    manifest["version"] = "0.2.0"
    manifest["page_types"] = [item for item in manifest["page_types"] if item["name"] != "system"]
    with pytest.raises(BrainSchemaError, match="uses removed type 'system'"):
        wiki_env.schema.save_custom(
            manifest,
            expected_sha256=bundle["custom"]["manifest_sha256"],
            expected_bundle_hash=bundle["bundle_hash"],
        )
    assert wiki_env.schema.bundle()["bundle_hash"] == bundle["bundle_hash"]
    assert "schema_version: 0.1.0" in (wiki_env.wiki_dir / "systems" / "system-page.md").read_text(encoding="utf-8")


def test_llm_wiki_api_vertical_slice(wiki_env: LlmWikiService) -> None:
    app = FastAPI()
    app.include_router(router, prefix="/api")
    client = TestClient(app)

    raw_response = client.post(
        "/api/knowledge/brain/wiki/raw",
        json={
            "source_id": "api",
            "asset_id": "asset",
            "title": "API source",
            "content": "# API source\n",
        },
    )
    assert raw_response.status_code == 200
    raw = raw_response.json()
    status_response = client.get("/api/knowledge/brain/wiki/status")
    assert status_response.status_code == 200
    status = status_response.json()
    assert status["raw"][0]["snapshot_path"] == raw["snapshot_path"]
    assert status["raw"][0]["integrity"] == "ok"
    assert status["gbrain"]["cli_installed"] is True
    context_response = client.get("/api/knowledge/brain/wiki/context/ingest", params={"raw_path": raw["snapshot_path"]})
    assert context_response.status_code == 200
    bundle_hash = context_response.json()["schema_bundle"]["bundle_hash"]
    publish_response = client.post(
        "/api/knowledge/brain/wiki/publish",
        json={
            "pages": [
                {
                    "slug": "concepts/api-page",
                    "content": _page("API Page", "concept", raw["snapshot_path"], "concepts/api-page"),
                }
            ],
            "expected_bundle_hash": bundle_hash,
            "summary": "API publish",
            "model": "test:model",
            "raw_paths": [raw["snapshot_path"]],
        },
    )
    assert publish_response.status_code == 200
    assert publish_response.json()["published"] is True
    assert client.get("/api/knowledge/brain/wiki/lint").json()["ok"] is True
    assert client.post("/api/knowledge/brain/wiki/query", json={"question": "API"}).json()["pages"]
