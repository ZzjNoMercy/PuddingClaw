"""Rebuildable LlamaIndex/Milvus projection for published LLM Wiki pages.

The Markdown Wiki remains the source of truth.  This module only records which
page bytes and indexing profile were last projected into the shared knowledge
text collection.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import logging
import os
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from config import (
    get_fallback_embedding_config,
    get_knowledge_multimodal_index_config,
    get_llm_wiki_retrieval_config,
)

logger = logging.getLogger(__name__)

MANIFEST_VERSION = 1
PARSER_NAME = "MarkdownNodeParser"
PARSER_VERSION = 1
WIKI_VIRTUAL_PREFIX = "/knowledge/llm-wiki/wiki/"


def refresh_document_knowledge_index(*args: Any, **kwargs: Any) -> dict[str, Any]:
    """Load the heavyweight indexing pipeline only for an actual sync."""

    from knowledge.indexer import refresh_document_knowledge_index as refresh

    return refresh(*args, **kwargs)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _utcnow() -> str:
    return datetime.now(UTC).isoformat()


def _atomic_json_write(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


@contextmanager
def _exclusive_lock(path: Path) -> Iterator[None]:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _decode_entity(result: Any) -> tuple[str, dict[str, Any], dict[str, Any]]:
    entity = result.get("entity", {}) if isinstance(result, dict) else getattr(result, "entity", {})
    if not isinstance(entity, dict):
        entity = {}
    payload: dict[str, Any] = {}
    raw = entity.get("_node_content")
    if isinstance(raw, str):
        try:
            decoded = json.loads(raw)
            if isinstance(decoded, dict):
                payload = decoded
        except json.JSONDecodeError:
            pass
    text = str(payload.get("text") or entity.get("text") or "")
    metadata = payload.get("metadata") if isinstance(payload.get("metadata"), dict) else {}
    return text, dict(metadata), entity


class LlmWikiEmbeddingService:
    """Synchronize and query the Wiki subset of the shared knowledge index."""

    def __init__(self, base_dir: Path, wiki_root: Path):
        self.base_dir = base_dir.resolve()
        self.wiki_root = wiki_root.resolve()
        self.wiki_dir = self.wiki_root / "wiki"
        state_dir = self.wiki_root / ".puddingclaw"
        self.manifest_path = state_dir / "embedding-manifest.json"
        self.lock_path = state_dir / "embedding.lock"

    def _page_paths(self) -> list[Path]:
        if not self.wiki_dir.is_dir():
            return []
        return sorted(
            path
            for path in self.wiki_dir.rglob("*.md")
            if path.is_file() and path.name not in {"index.md", "log.md"}
        )

    def _slug(self, path: Path) -> str:
        return path.relative_to(self.wiki_dir).with_suffix("").as_posix()

    def _profile(self) -> dict[str, Any]:
        embedding = get_fallback_embedding_config()
        index = get_knowledge_multimodal_index_config()
        return {
            "embedding_model_id": str(embedding.get("model_id") or ""),
            "embedding_model": str(embedding.get("model") or ""),
            "embedding_provider": str(embedding.get("provider") or ""),
            "embedding_dimension": int(embedding.get("dimension") or 0),
            "parser": PARSER_NAME,
            "parser_version": PARSER_VERSION,
            "text_collection": str(index.get("text_collection") or "puddingclaw_knowledge_text"),
            "milvus_uri": str(index.get("milvus_uri") or "http://localhost:19530"),
        }

    def _load_manifest(self) -> dict[str, Any]:
        try:
            payload = json.loads(self.manifest_path.read_text(encoding="utf-8"))
            if isinstance(payload, dict) and isinstance(payload.get("pages"), dict):
                return payload
        except (OSError, json.JSONDecodeError):
            pass
        return {"version": MANIFEST_VERSION, "pages": {}}

    def status(self) -> dict[str, Any]:
        retrieval = get_llm_wiki_retrieval_config()
        index = get_knowledge_multimodal_index_config()
        profile = self._profile()
        manifest = self._load_manifest()
        stored_profile = manifest.get("profile") if isinstance(manifest.get("profile"), dict) else {}
        profile_matches = stored_profile == profile
        records = manifest.get("pages") if isinstance(manifest.get("pages"), dict) else {}
        pages: list[dict[str, Any]] = []
        current_slugs: set[str] = set()
        for path in self._page_paths():
            slug = self._slug(path)
            current_slugs.add(slug)
            content_sha256 = _sha256(path)
            record = records.get(slug) if isinstance(records.get(slug), dict) else {}
            indexed_sha256 = str(record.get("indexed_content_sha256") or "")
            if record.get("error"):
                state = "failed"
            elif indexed_sha256 and indexed_sha256 == content_sha256 and profile_matches:
                state = "indexed"
            elif indexed_sha256:
                state = "outdated"
            else:
                state = "pending"
            pages.append({
                "slug": slug,
                "virtual_path": f"{WIKI_VIRTUAL_PREFIX}{slug}.md",
                "content_sha256": content_sha256,
                "indexed_content_sha256": indexed_sha256 or None,
                "state": state,
                "chunk_count": int(record.get("chunk_count") or 0),
                "indexed_at": record.get("indexed_at"),
                "error": record.get("error"),
            })
        stale = sorted(set(records) - current_slugs)
        counts = {
            "total": len(pages),
            "indexed": sum(page["state"] == "indexed" for page in pages),
            "pending": sum(page["state"] == "pending" for page in pages),
            "outdated": sum(page["state"] == "outdated" for page in pages),
            "failed": sum(page["state"] == "failed" for page in pages),
            "chunks": sum(int(page["chunk_count"]) for page in pages if page["state"] == "indexed"),
            "stale": len(stale),
        }
        infrastructure_ready = bool(
            index.get("enabled") and str(index.get("vector_store") or "").lower() == "milvus"
        )
        return {
            "hybrid_enabled": bool(retrieval.get("hybrid_enabled")),
            "query_mode": "hybrid" if retrieval.get("hybrid_enabled") else "lexical",
            "infrastructure_ready": infrastructure_ready,
            "shared_collection": True,
            "profile": profile,
            "profile_matches": profile_matches,
            "counts": counts,
            "pages": pages,
            "stale_pages": stale,
            "last_sync": manifest.get("last_sync"),
        }

    def _delete_virtual_path(self, virtual_path: str) -> None:
        from pymilvus import MilvusClient

        index = get_knowledge_multimodal_index_config()
        client = MilvusClient(uri=index.get("milvus_uri", "http://localhost:19530"), timeout=10.0)
        collection = str(index.get("text_collection") or "puddingclaw_knowledge_text")
        if client.has_collection(collection):
            client.delete(
                collection_name=collection,
                filter=f"virtual_path == {json.dumps(virtual_path, ensure_ascii=False)}",
            )

    def sync(self, *, slugs: list[str] | None = None, force: bool = False) -> dict[str, Any]:
        index = get_knowledge_multimodal_index_config()
        if not index.get("enabled") or str(index.get("vector_store") or "").lower() != "milvus":
            return {"ok": False, "error": "知识库 Milvus 索引未启用", "updated": [], "skipped": [], "failed": []}

        requested = {str(slug).strip() for slug in (slugs or []) if str(slug).strip()}
        with _exclusive_lock(self.lock_path):
            manifest = self._load_manifest()
            old_profile = manifest.get("profile") if isinstance(manifest.get("profile"), dict) else {}
            profile = self._profile()
            profile_changed = old_profile != profile
            records = manifest.get("pages") if isinstance(manifest.get("pages"), dict) else {}
            records = dict(records)
            updated: list[str] = []
            skipped: list[str] = []
            failed: list[dict[str, str]] = []
            current_paths = {self._slug(path): path for path in self._page_paths()}
            # A model/dimension/parser change invalidates every page.  Never
            # stamp a new global profile after rebuilding only a subset.
            targets = sorted(set(current_paths) if profile_changed else (requested or set(current_paths)))

            unknown = sorted(set(targets) - set(current_paths))
            for slug in unknown:
                failed.append({"slug": slug, "error": "Wiki 页面不存在"})

            for slug in targets:
                path = current_paths.get(slug)
                if path is None:
                    continue
                digest = _sha256(path)
                record = records.get(slug) if isinstance(records.get(slug), dict) else {}
                if (
                    not force
                    and not profile_changed
                    and record.get("indexed_content_sha256") == digest
                    and not record.get("error")
                ):
                    skipped.append(slug)
                    continue
                try:
                    result = refresh_document_knowledge_index(
                        self.base_dir,
                        path,
                        include_linked_images=False,
                    )
                    if not result.get("refreshed"):
                        raise RuntimeError(str(result.get("error") or result.get("reason") or "Embedding 未完成"))
                    multimodal = result.get("multimodal") if isinstance(result.get("multimodal"), dict) else {}
                    records[slug] = {
                        "indexed_content_sha256": digest,
                        "indexed_at": _utcnow(),
                        "chunk_count": int(multimodal.get("text_count") or 0),
                        "virtual_path": f"{WIKI_VIRTUAL_PREFIX}{slug}.md",
                        "error": None,
                    }
                    updated.append(slug)
                except Exception as exc:  # noqa: BLE001 - other pages should continue
                    message = f"{type(exc).__name__}: {exc}"
                    records[slug] = {
                        **record,
                        "virtual_path": f"{WIKI_VIRTUAL_PREFIX}{slug}.md",
                        "error": message,
                        "last_attempted_at": _utcnow(),
                    }
                    failed.append({"slug": slug, "error": message})
                    logger.warning("LLM Wiki embedding failed for %s: %s", slug, message)

            stale = sorted(set(records) - set(current_paths))
            for slug in stale:
                try:
                    virtual_path = str(records[slug].get("virtual_path") or f"{WIKI_VIRTUAL_PREFIX}{slug}.md")
                    self._delete_virtual_path(virtual_path)
                    records.pop(slug, None)
                except Exception as exc:  # noqa: BLE001
                    failed.append({"slug": slug, "error": f"清理旧向量失败：{type(exc).__name__}: {exc}"})

            completed_at = _utcnow()
            manifest = {
                "version": MANIFEST_VERSION,
                "profile": profile,
                "pages": records,
                "last_sync": {
                    "completed_at": completed_at,
                    "force": force,
                    "updated": updated,
                    "skipped": skipped,
                    "failed": failed,
                },
            }
            _atomic_json_write(self.manifest_path, manifest)
            return {
                "ok": not failed,
                "updated": updated,
                "skipped": skipped,
                "failed": failed,
                "profile_changed": profile_changed,
                "completed_at": completed_at,
                "status": self.status(),
            }

    def semantic_query(self, question: str, *, limit: int) -> dict[str, Any]:
        from pymilvus import MilvusClient

        from llm.embed_client import get_embedding_model

        index = get_knowledge_multimodal_index_config()
        if not index.get("enabled") or str(index.get("vector_store") or "").lower() != "milvus":
            return {"hits": [], "error": "知识库 Milvus 索引未启用"}
        collection = str(index.get("text_collection") or "puddingclaw_knowledge_text")
        client = MilvusClient(uri=index.get("milvus_uri", "http://localhost:19530"), timeout=10.0)
        if not client.has_collection(collection):
            return {"hits": [], "error": f"Milvus Collection 不存在：{collection}"}
        query_embedding = get_embedding_model().get_query_embedding(question)
        # Retrieve a wider chunk pool than the final page limit. Multi-topic
        # questions often place the second concept behind several chunks from
        # the first concept; page-level fusion below performs the final trim.
        candidate_limit = max(limit * 8, 40)
        result_groups = client.search(
            collection_name=collection,
            data=[query_embedding],
            anns_field="embedding",
            filter=f'virtual_path like "{WIKI_VIRTUAL_PREFIX}%"',
            limit=candidate_limit,
            output_fields=["text", "_node_content", "_node_type", "doc_id"],
        )
        hits: list[dict[str, Any]] = []
        seen: set[str] = set()
        for rank, result in enumerate(result_groups[0] if result_groups else [], start=1):
            text, metadata, entity = _decode_entity(result)
            virtual_path = str(metadata.get("virtual_path") or "")
            if not virtual_path.startswith(WIKI_VIRTUAL_PREFIX) or not virtual_path.endswith(".md"):
                continue
            slug = virtual_path.removeprefix(WIKI_VIRTUAL_PREFIX).removesuffix(".md")
            if slug in seen:
                continue
            seen.add(slug)
            raw_score = result.get("distance", result.get("score")) if isinstance(result, dict) else None
            hits.append({
                "slug": slug,
                "rank": rank,
                "raw_score": float(raw_score) if raw_score is not None else None,
                "quote": text.strip(),
                "chunk_id": str(entity.get("id") or ""),
                "chunk_title": metadata.get("chunk_title"),
                "virtual_path": virtual_path,
            })
            if len(hits) >= max(limit * 4, limit):
                break
        return {"hits": hits, "error": None, "collection": collection}


def get_llm_wiki_embedding_service(base_dir: Path, wiki_root: Path) -> LlmWikiEmbeddingService:
    return LlmWikiEmbeddingService(base_dir, wiki_root)
