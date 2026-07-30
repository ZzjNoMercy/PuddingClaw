from __future__ import annotations

import json
import shutil
from copy import deepcopy
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from api.llm_wiki import router
from knowledge.brain_schema import BrainSchemaError, BrainSchemaService
from knowledge.llm_wiki import LlmWikiError, LlmWikiService


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
            {"slug": "compiled-rag", "content": _page("编译式 RAG", "concept", raw["snapshot_path"], "gbrain")},
            {"slug": "gbrain", "content": _page("GBrain", "system", raw["snapshot_path"], "compiled-rag")},
        ],
        expected_bundle_hash=bundle["bundle_hash"],
        summary="初版编译",
        model="test:model",
        raw_paths=[raw["snapshot_path"]],
    )
    assert result["published"] is True
    assert result["lint"]["ok"] is True
    index = (wiki_env.wiki_dir / "index.md").read_text(encoding="utf-8")
    assert "[[compiled-rag|编译式 RAG]]" in index
    assert "[[gbrain|GBrain]]" in index
    log = (wiki_env.wiki_dir / "log.md").read_text(encoding="utf-8")
    assert "append-only" not in log or "#" in log
    assert result["job_id"] in log

    query = wiki_env.query("GBrain 编译", limit=1)
    assert query["source_policy"] == "wiki-only"
    assert len(query["pages"]) == 1
    assert query["knowledge_gap"] is False


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
                "slug": "broken-page",
                "content": _page("Broken", "not-in-schema", raw["snapshot_path"], "missing-page"),
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
    assert not (wiki_env.wiki_dir / "broken-page.md").exists()


def test_publish_requires_explicit_raw_authority_and_bound_sources(wiki_env: LlmWikiService) -> None:
    raw = wiki_env.snapshot_raw(
        source_id="private",
        asset_id="doc-private",
        title="Private Source",
        content="# Private\n",
    )
    bundle = wiki_env.schema.bundle()
    page = _page("Private", "concept", "totally-unknown", "private-page")
    with pytest.raises(Exception, match="explicit immutable raw_paths"):
        wiki_env.publish(
            pages=[{"slug": "private-page", "content": page}],
            expected_bundle_hash=bundle["bundle_hash"],
            summary="unauthorized",
            model="test:model",
            raw_paths=[],
        )
    with pytest.raises(Exception, match="not authorized"):
        wiki_env.publish(
            pages=[{"slug": "private-page", "content": page}],
            expected_bundle_hash=bundle["bundle_hash"],
            summary="unauthorized",
            model="test:model",
            raw_paths=[raw["snapshot_path"]],
        )
    assert not (wiki_env.wiki_dir / "private-page.md").exists()


def test_update_must_consume_a_raw_selected_for_this_ingest(wiki_env: LlmWikiService) -> None:
    first = wiki_env.snapshot_raw(source_id="kb", asset_id="a", title="A", content="# A\n")
    second = wiki_env.snapshot_raw(source_id="kb", asset_id="b", title="B", content="# B\n")
    bundle = wiki_env.schema.bundle()
    initial = wiki_env.publish(
        pages=[{"slug": "page", "content": _page("Page", "concept", first["snapshot_path"], "page")}],
        expected_bundle_hash=bundle["bundle_hash"],
        summary="initial",
        model="test:model",
        raw_paths=[first["snapshot_path"]],
    )
    assert initial["published"] is True
    with pytest.raises(LlmWikiError, match="must cite at least one raw selected"):
        wiki_env.publish(
            pages=[{"slug": "page", "content": _page("Rewritten", "concept", first["snapshot_path"], "page")}],
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
        pages=[{"slug": "single-page", "content": _page("Single", "concept", raw["snapshot_path"], "single-page")}],
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
        pages=[{"slug": "compile", "content": _page("Compile", "concept", raw["snapshot_path"], "compile")}],
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
            {"slug": "one", "content": _page("One", "concept", raw["snapshot_path"], "two")},
            {"slug": "two", "content": _page("Two", "system", raw["snapshot_path"], "one")},
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
    assert upgraded["schema_migration"]["migrated_pages"] == ["one", "two"]
    assert "schema_version: 0.2.0" in (wiki_env.wiki_dir / "one.md").read_text(encoding="utf-8")
    assert "schema_version: 0.2.0" in (wiki_env.wiki_dir / "two.md").read_text(encoding="utf-8")
    assert "schema-migrate" in (wiki_env.wiki_dir / "log.md").read_text(encoding="utf-8")
    assert wiki_env.lint()["ok"] is True


def test_destructive_schema_upgrade_fails_before_activation(wiki_env: LlmWikiService) -> None:
    raw = wiki_env.snapshot_raw(source_id="kb", asset_id="destructive", title="Destructive", content="# D\n")
    bundle = wiki_env.schema.bundle()
    assert wiki_env.publish(
        pages=[{"slug": "system-page", "content": _page("System", "system", raw["snapshot_path"], "system-page")}],
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
    assert "schema_version: 0.1.0" in (wiki_env.wiki_dir / "system-page.md").read_text(encoding="utf-8")


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
    context_response = client.get("/api/knowledge/brain/wiki/context/ingest", params={"raw_path": raw["snapshot_path"]})
    assert context_response.status_code == 200
    bundle_hash = context_response.json()["schema_bundle"]["bundle_hash"]
    publish_response = client.post(
        "/api/knowledge/brain/wiki/publish",
        json={
            "pages": [{"slug": "api-page", "content": _page("API Page", "concept", raw["snapshot_path"], "api-page")}],
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
