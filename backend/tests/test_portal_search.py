import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from knowledge.portal_search import (
    DEFAULT_SEARCH_DIRECTORIES,
    KnowledgePortalSearchService,
    PortalSearchConfigError,
    build_catalog,
    normalize_search_config,
)


def _config() -> dict:
    return normalize_search_config({
        "directories": [
            {"id": "assets", "path": "assets", "enabled": True, "recursive": True, "content_types": ["image"], "referenced_images_only": True},
            {"id": "imported", "path": "imported", "enabled": True, "recursive": True, "content_types": ["markdown"]},
            {"id": "wiki", "path": "llm-wiki/wiki", "enabled": True, "recursive": True, "content_types": ["markdown"]},
        ],
        "exclude": ["llm-wiki/wiki/index.md", "llm-wiki/raw/**"],
    })


def test_default_search_scope_is_explicit_and_raw_is_excluded():
    config = normalize_search_config(None)
    assert config["enabled"] is True
    assert [item["id"] for item in config["directories"]] == [item["id"] for item in DEFAULT_SEARCH_DIRECTORIES]
    assert "llm-wiki/raw/**" in config["exclude"]
    assert "llm-wiki/wiki/index.md" in config["exclude"]


def test_legacy_disabled_flag_cannot_disable_core_search():
    assert normalize_search_config({"enabled": False})["enabled"] is True


def test_search_directory_rejects_absolute_and_parent_paths():
    with pytest.raises(PortalSearchConfigError):
        normalize_search_config({"directories": [{"id": "bad", "path": "../outside", "content_types": ["markdown"]}]})
    with pytest.raises(PortalSearchConfigError):
        normalize_search_config({"directories": [{"id": "bad", "path": "/tmp/outside", "content_types": ["markdown"]}]})


def test_catalog_only_indexes_referenced_images_and_excludes_navigation(tmp_path: Path):
    root = tmp_path / "knowledge"
    (root / "imported").mkdir(parents=True)
    (root / "assets").mkdir(parents=True)
    (root / "llm-wiki/wiki").mkdir(parents=True)
    (root / "llm-wiki/raw").mkdir(parents=True)
    (root / "imported/article.md").write_text("# DeepSeek V4\n\n![架构图](../assets/diagram.png)\n配置说明", encoding="utf-8")
    (root / "assets/diagram.png").write_bytes(b"diagram")
    (root / "assets/unreferenced.png").write_bytes(b"noise")
    (root / "llm-wiki/wiki/index.md").write_text("# navigation", encoding="utf-8")
    (root / "llm-wiki/wiki/page.md").write_text("---\ntitle: Wiki Page\n---\n正文", encoding="utf-8")
    (root / "llm-wiki/raw/raw.md").write_text("# raw", encoding="utf-8")

    catalog = build_catalog(root, _config())
    paths = {record["path"] for record in catalog["records"]}
    assert "imported/article.md" in paths
    assert "assets/diagram.png" in paths
    assert "assets/unreferenced.png" not in paths
    assert "llm-wiki/wiki/index.md" not in paths
    assert "llm-wiki/raw/raw.md" not in paths


def test_portal_search_ranks_title_and_deduplicates_same_content(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    root = tmp_path / "knowledge"
    (root / "imported").mkdir(parents=True)
    (root / "llm-wiki/wiki").mkdir(parents=True)
    content = "# OpenCode DeepSeek V4\n\n在模型列表中选择 DeepSeek V4。"
    (root / "imported/opencode.md").write_text(content, encoding="utf-8")
    (root / "llm-wiki/wiki/opencode.md").write_text(content, encoding="utf-8")

    import knowledge.portal_search as module

    monkeypatch.setattr(module, "get_knowledge_root", lambda _base: root)
    monkeypatch.setattr(module.KnowledgePortalSearchService, "config", property(lambda _self: _config()))
    monkeypatch.setattr(module, "_semantic_retrieval", lambda *_args, **_kwargs: {"enabled": False, "channels": {}, "errors": {}})
    service = KnowledgePortalSearchService(tmp_path)
    result = service.search("OpenCode DeepSeek V4")
    assert result["total"] == 1
    assert result["hits"][0]["result_type"] == "wiki"
    assert result["hits"][0]["source_group"]["versions"] == ["/knowledge/imported/opencode.md"]
    assert "title" in result["hits"][0]["matched_by"]


def test_catalog_incrementally_updates_changed_and_deleted_files(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    root = tmp_path / "knowledge"
    first = root / "imported" / "first.md"
    second = root / "imported" / "second.md"
    first.parent.mkdir(parents=True)
    first.write_text("# First\n\nold body", encoding="utf-8")
    second.write_text("# Second\n\nkeep body", encoding="utf-8")

    import knowledge.portal_search as module

    monkeypatch.setattr(module, "get_knowledge_root", lambda _base: root)
    monkeypatch.setattr(module.KnowledgePortalSearchService, "config", property(lambda _self: _config()))
    service = KnowledgePortalSearchService(tmp_path)
    service.refresh()
    before = module.load_catalog(tmp_path)
    second_before = next(record for record in before["records"] if record["path"] == "imported/second.md")

    first.write_text("# First\n\nnew body", encoding="utf-8")
    service.refresh_paths([first])
    updated = module.load_catalog(tmp_path)
    first_after = next(record for record in updated["records"] if record["path"] == "imported/first.md")
    second_after = next(record for record in updated["records"] if record["path"] == "imported/second.md")
    assert "new body" in first_after["text"]
    assert second_after["content_sha256"] == second_before["content_sha256"]

    second.unlink()
    service.refresh_paths([second])
    deleted = module.load_catalog(tmp_path)
    assert "imported/second.md" not in {record["path"] for record in deleted["records"]}


def test_portal_search_returns_semantic_hit_without_keyword_overlap(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    root = tmp_path / "knowledge"
    (root / "imported").mkdir(parents=True)
    (root / "imported/first-principles.md").write_text(
        "# 第一性原理\n\n从最基础事实重新推导解决方案。",
        encoding="utf-8",
    )

    import knowledge.portal_search as module

    monkeypatch.setattr(module, "get_knowledge_root", lambda _base: root)
    monkeypatch.setattr(module.KnowledgePortalSearchService, "config", property(lambda _self: _config()))

    def semantic(_query: str, *, records: list[dict], **_kwargs):
        record = next(item for item in records if item["path"] == "imported/first-principles.md")
        return {
            "enabled": True,
            "channels": {
                "text_vector": [{
                    "record": record,
                    "record_id": record["id"],
                    "quote": "从最基础事实重新推导解决方案。",
                    "raw_score": 0.88,
                    "metadata": {},
                }],
            },
            "errors": {},
        }

    monkeypatch.setattr(module, "_semantic_retrieval", semantic)
    result = KnowledgePortalSearchService(tmp_path).search("不要照搬已有答案，重新推演")

    assert result["total"] == 1
    assert result["hits"][0]["title"] == "第一性原理"
    assert "text_vector" in result["hits"][0]["matched_by"]
    assert result["retrieval"]["hybrid_enabled"] is True


def test_portal_service_has_no_agent_tool_dependency():
    source = Path(__file__).parents[1].joinpath("api/knowledge.py").read_text(encoding="utf-8")
    assert "LlamaIndexKnowledgeQueryTool" not in source
    assert "KnowledgePortalSearchService" in source


def test_semantic_retrieval_queries_shared_text_and_image_collections(tmp_path: Path, monkeypatch):
    root = tmp_path / "knowledge"
    (root / "imported").mkdir(parents=True)
    (root / "assets").mkdir(parents=True)
    markdown = root / "imported" / "article.md"
    image = root / "assets" / "diagram.png"
    markdown.write_text("# Architecture\n\n![架构图](../assets/diagram.png)", encoding="utf-8")
    image.write_bytes(b"image")
    records = build_catalog(root, _config())["records"]

    import pymilvus

    import knowledge.portal_search as module
    import llm.embed_client as embed_client
    import llm.multimodal_embedding as multimodal_embedding

    monkeypatch.setattr(module, "get_knowledge_multimodal_index_config", lambda: {
        "enabled": True,
        "vector_store": "milvus",
        "milvus_uri": "http://milvus.test:19530",
        "text_collection": "text",
        "image_collection": "image",
        "bm25_enabled": True,
    })
    monkeypatch.setattr(module, "get_rag_hybrid_config", lambda: {"enabled": True, "candidate_top_k": 10})
    monkeypatch.setattr(embed_client, "get_embedding_model", lambda: SimpleNamespace(get_query_embedding=lambda _query: [0.1, 0.2]))
    monkeypatch.setattr(multimodal_embedding, "get_multimodal_embedding_model", lambda: SimpleNamespace(get_query_embedding=lambda _query: [0.3, 0.4]))

    class FakeMilvusClient:
        def __init__(self, **_kwargs):
            pass

        def has_collection(self, _name):
            return True

        def search(self, *, collection_name, anns_field, **_kwargs):
            if collection_name == "image":
                metadata = {"virtual_path": "/knowledge/assets/diagram.png", "context": {"caption": "架构图"}}
                text = ""
            else:
                metadata = {"virtual_path": "/knowledge/imported/article.md"}
                text = "semantic architecture"
            return [[{
                "id": f"{collection_name}-{anns_field}",
                "distance": 0.9,
                "entity": {"text": text, "_node_content": json.dumps({"metadata": metadata})},
            }]]

    monkeypatch.setattr(pymilvus, "MilvusClient", FakeMilvusClient)
    result = module._semantic_retrieval(
        "系统如何组织",
        root=root,
        records=records,
        limit=5,
        include_images=True,
    )

    assert result["enabled"] is True
    assert set(result["channels"]) == {"text_vector", "image_vector"}
    assert result["channels"]["text_vector"][0]["record"]["path"] == "imported/article.md"
    assert result["channels"]["image_vector"][0]["record"]["path"] == "assets/diagram.png"


def test_fused_portal_candidates_are_reranked_without_agent_runtime(monkeypatch):
    import knowledge.portal_search as module
    import llm.rerank_client as rerank_client

    records = [
        {"id": "one", "title": "One", "path": "imported/one.md", "text": "first"},
        {"id": "two", "title": "Two", "path": "imported/two.md", "text": "second"},
    ]
    monkeypatch.setattr(module, "get_rag_rerank_config", lambda: {
        "enabled": True,
        "candidate_top_k": 10,
        "top_n": 10,
    })
    monkeypatch.setattr(
        rerank_client,
        "rerank_documents",
        lambda **_kwargs: [SimpleNamespace(index=1, score=0.95), SimpleNamespace(index=0, score=0.5)],
    )

    reranked, enabled, error = module._rerank_scored_records(
        "second",
        [(0.9, records[0]), (0.8, records[1])],
        semantic_hits={},
        requested_limit=10,
    )

    assert enabled is True
    assert error is None
    assert [record["id"] for _, record in reranked] == ["two", "one"]
