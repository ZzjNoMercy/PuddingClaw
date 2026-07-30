from __future__ import annotations

import asyncio
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
from knowledge.import_jobs import LLM_WIKI_INGEST_KIND, create_llm_wiki_ingest_job, job_to_list_dict
from knowledge.llm_wiki import LlmWikiError, LlmWikiService
from knowledge.llm_wiki_compiler_agent import LlmWikiCompilerAgent
from knowledge.models import Base
from tools.llm_wiki_tools import LlmWikiQueryTool


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
        await engine.dispose()

    asyncio.run(run())


def test_dedicated_compiler_agent_runs_only_required_tools_in_order(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    messages = [HumanMessage(content="compile")]

    def ai_call(name: str, call_id: str, args: dict[str, object]) -> AIMessage:
        return AIMessage(
            content="",
            tool_calls=[{"name": name, "args": args, "id": call_id, "type": "tool_call"}],
        )

    states = []
    for message in (
        ai_call("llm_wiki_context", "context-1", {"operation": "ingest", "raw_paths": ["one.md"]}),
        ToolMessage(content=json.dumps({"schema_bundle": {"bundle_hash": "a" * 64}}), name="llm_wiki_context", tool_call_id="context-1"),
        ai_call("llm_wiki_publish", "publish-1", {"pages": [], "raw_paths": ["one.md"]}),
        ToolMessage(content=json.dumps({"ok": True, "published_pages": ["frameworks/one"]}), name="llm_wiki_publish", tool_call_id="publish-1"),
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
    assert wiki_env.workspace_status()["raw"][0]["compiled"] is False


def test_destructive_schema_upgrade_fails_before_activation(wiki_env: LlmWikiService) -> None:
    raw = wiki_env.snapshot_raw(source_id="kb", asset_id="destructive", title="Destructive", content="# D\n")
    bundle = wiki_env.schema.bundle()
    assert wiki_env.publish(
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
    )["published"] is True

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
    assert "schema_version: 0.1.0" in (wiki_env.wiki_dir / "systems" / "system-page.md").read_text(
        encoding="utf-8"
    )


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
