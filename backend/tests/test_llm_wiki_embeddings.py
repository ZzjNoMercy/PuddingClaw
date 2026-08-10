from __future__ import annotations

from pathlib import Path

from knowledge.llm_wiki_embeddings import LlmWikiEmbeddingService


def _profile_config() -> dict[str, object]:
    return {
        "provider": "dashscope",
        "model": "text-embedding-v4",
        "model_id": "embedding-model",
        "dimension": 1024,
    }


def _index_config() -> dict[str, object]:
    return {
        "enabled": True,
        "vector_store": "milvus",
        "milvus_uri": "http://localhost:19530",
        "text_collection": "puddingclaw_knowledge_text",
    }


def test_embedding_manifest_reuses_page_hash_and_skips_unchanged_page(
    tmp_path: Path,
    monkeypatch,
) -> None:
    knowledge = tmp_path / "knowledge"
    wiki_root = knowledge / "llm-wiki"
    page = wiki_root / "wiki" / "concepts" / "compiled-rag.md"
    page.parent.mkdir(parents=True)
    page.write_text("# Compiled RAG\n\nStable page.\n", encoding="utf-8")
    calls: list[Path] = []

    monkeypatch.setattr("knowledge.llm_wiki_embeddings.get_fallback_embedding_config", _profile_config)
    monkeypatch.setattr("knowledge.llm_wiki_embeddings.get_knowledge_multimodal_index_config", _index_config)
    monkeypatch.setattr("knowledge.llm_wiki_embeddings.get_llm_wiki_retrieval_config", lambda: {"hybrid_enabled": True})

    def fake_refresh(_base_dir: Path, path: Path, *, include_linked_images: bool = True):
        assert include_linked_images is False
        calls.append(path)
        return {"refreshed": True, "multimodal": {"text_count": 2}}

    monkeypatch.setattr("knowledge.llm_wiki_embeddings.refresh_document_knowledge_index", fake_refresh)
    service = LlmWikiEmbeddingService(tmp_path, wiki_root)

    first = service.sync()
    assert first["ok"] is True
    assert first["updated"] == ["concepts/compiled-rag"]
    assert service.status()["counts"] == {
        "total": 1,
        "indexed": 1,
        "pending": 0,
        "outdated": 0,
        "failed": 0,
        "chunks": 2,
        "stale": 0,
    }

    second = service.sync()
    assert second["updated"] == []
    assert second["skipped"] == ["concepts/compiled-rag"]
    assert len(calls) == 1

    page.write_text("# Compiled RAG\n\nChanged page.\n", encoding="utf-8")
    assert service.status()["pages"][0]["state"] == "outdated"
    third = service.sync()
    assert third["updated"] == ["concepts/compiled-rag"]
    assert len(calls) == 2


def test_embedding_profile_change_rebuilds_all_pages(
    tmp_path: Path,
    monkeypatch,
) -> None:
    wiki_root = tmp_path / "knowledge" / "llm-wiki"
    for slug in ("concepts/one", "concepts/two"):
        path = wiki_root / "wiki" / f"{slug}.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"# {slug}\n", encoding="utf-8")
    profile = _profile_config()
    calls: list[str] = []
    monkeypatch.setattr("knowledge.llm_wiki_embeddings.get_fallback_embedding_config", lambda: dict(profile))
    monkeypatch.setattr("knowledge.llm_wiki_embeddings.get_knowledge_multimodal_index_config", _index_config)
    monkeypatch.setattr("knowledge.llm_wiki_embeddings.get_llm_wiki_retrieval_config", lambda: {"hybrid_enabled": True})
    monkeypatch.setattr(
        "knowledge.llm_wiki_embeddings.refresh_document_knowledge_index",
        lambda _base, path, **_kwargs: calls.append(path.stem) or {"refreshed": True, "multimodal": {"text_count": 1}},
    )
    service = LlmWikiEmbeddingService(tmp_path, wiki_root)
    assert service.sync()["ok"] is True
    calls.clear()

    profile["model"] = "text-embedding-v5"
    profile["model_id"] = "embedding-model-v5"
    result = service.sync(slugs=["concepts/one"])

    assert result["profile_changed"] is True
    assert sorted(result["updated"]) == ["concepts/one", "concepts/two"]
    assert sorted(calls) == ["one", "two"]
