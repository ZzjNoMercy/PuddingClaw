"""LlamaIndex knowledge query tools."""

import json
import logging
from pathlib import Path
from typing import Any

from langchain_core.tools import BaseTool
from pydantic import BaseModel, Field

from config import get_knowledge_multimodal_index_config, get_rag_config, get_rag_hybrid_config
from knowledge.paths import get_knowledge_root

logger = logging.getLogger(__name__)


class LlamaIndexKnowledgeInput(BaseModel):
    query: str = Field(description="The question or retrieval query for the local LlamaIndex knowledge index.")


class LlamaIndexKnowledgeQueryTool(BaseTool):
    name: str = "llamaindex_knowledge_query"
    description: str = (
        "Query the local knowledge base through the project's LlamaIndex retrieval layer. "
        "Use this for RAG over uploaded PDFs, imported Markdown, and other indexed knowledge artifacts. "
        "Do not switch to glob/grep on your own after this tool returns results; only use glob/grep under "
        "/knowledge/ when the user explicitly asks for exact file-name or raw Markdown text lookup."
    )
    args_schema: type[BaseModel] = LlamaIndexKnowledgeInput
    risk_level: str = "safe"
    base_dir: str = ""
    _index: object | None = None
    _index_error: str | None = None

    class Config:
        arbitrary_types_allowed = True

    def _load_local_index(self):
        """Load a previously published local index without mutating it."""
        if self._index is not None:
            return self._index

        storage_dir = Path(self.base_dir) / "storage" / "knowledge_index"
        if not storage_dir.exists() or not any(storage_dir.iterdir()):
            self._index_error = "No manually published local knowledge index is available"
            return None

        try:
            from llama_index.core import StorageContext, load_index_from_storage

            from llm.embed_client import get_embedding_model

            storage_context = StorageContext.from_defaults(persist_dir=str(storage_dir))
            index = load_index_from_storage(storage_context, embed_model=get_embedding_model())
            self._index_error = None
            self._index = index
            return index
        except ImportError as e:
            self._index_error = f"LlamaIndex not fully installed: {e}"
            logger.warning(self._index_error)
            return None
        except Exception as e:
            self._index_error = f"Local index load error: {e}"
            logger.warning(self._index_error)
            return None

    @staticmethod
    def _node_content(node: Any) -> str:
        try:
            return str(node.get_content() or "")
        except Exception:
            return str(getattr(node, "text", "") or "")

    @staticmethod
    def _node_metadata(node: Any) -> dict[str, Any]:
        return dict(getattr(node, "metadata", {}) or {})

    @staticmethod
    def _candidate_key(candidate: dict[str, Any]) -> str:
        source = candidate.get("source") if isinstance(candidate.get("source"), dict) else {}
        document_id = str(source.get("document_id") or "")
        chunk_id = str(source.get("chunk_id") or "")
        if document_id or chunk_id:
            return f"{document_id}::{chunk_id}"
        return str(candidate.get("quote") or candidate.get("title") or "")

    @classmethod
    def _apply_rrf(
        cls,
        candidates: list[dict[str, Any]],
        *,
        weights: dict[str, float] | None = None,
        k: int = 60,
    ) -> list[dict[str, Any]]:
        """Fuse candidates from vector/image/BM25 routes by reciprocal rank."""

        if not candidates:
            return []
        weights = weights or {}
        merged: dict[str, dict[str, Any]] = {}
        for position, candidate in enumerate(candidates):
            channel = str(candidate.get("retrieval_channel") or "vector")
            rank = int(candidate.get("retrieval_rank") or (position + 1))
            weight = float(weights.get(channel, 1.0))
            key = cls._candidate_key(candidate)
            score = weight / float(k + max(1, rank))
            if key not in merged:
                merged[key] = {**candidate, "rrf_score": score}
            else:
                merged[key]["rrf_score"] = float(merged[key].get("rrf_score") or 0.0) + score
                if not merged[key].get("quote") and candidate.get("quote"):
                    merged[key]["quote"] = candidate["quote"]
        fused = sorted(merged.values(), key=lambda item: float(item.get("rrf_score") or 0.0), reverse=True)
        for item in fused:
            source = item.get("source")
            if isinstance(source, dict):
                source["score"] = float(item.get("rrf_score") or 0.0)
        return fused

    @staticmethod
    def _entity_payload(result: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
        entity = result.get("entity") if isinstance(result.get("entity"), dict) else result
        metadata: dict[str, Any] = {}
        text = str(entity.get("text") or "")
        node_content_raw = entity.get("_node_content") or entity.get("node_content")
        if isinstance(node_content_raw, str) and node_content_raw.strip().startswith("{"):
            try:
                node_content = json.loads(node_content_raw)
                if isinstance(node_content, dict):
                    metadata.update(node_content.get("metadata") if isinstance(node_content.get("metadata"), dict) else {})
                    text = text or str(
                        node_content.get("text")
                        or (node_content.get("text_resource") or {}).get("text")
                        or ""
                    )
                    image_resource = node_content.get("image_resource") if isinstance(node_content.get("image_resource"), dict) else {}
                    if image_resource:
                        metadata.setdefault("file_path", image_resource.get("path") or image_resource.get("image_path"))
            except json.JSONDecodeError:
                pass
        inline_metadata = entity.get("metadata") if isinstance(entity.get("metadata"), dict) else {}
        metadata.update(inline_metadata)
        for key, value in entity.items():
            if key.startswith("_") or key in {"entity", "vector", "embedding", "text", "metadata"}:
                continue
            if isinstance(value, (str, int, float, bool, type(None))):
                metadata.setdefault(key, value)
        return {"text": text, "metadata": metadata}, entity

    @staticmethod
    def _score(result: dict[str, Any]) -> float | None:
        value = result.get("distance", result.get("score"))
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _raw_candidate_score(candidate: dict[str, Any]) -> float | None:
        for value in (
            candidate.get("rerank_score"),
            candidate.get("rrf_score"),
            candidate.get("score"),
            (candidate.get("source") or {}).get("score") if isinstance(candidate.get("source"), dict) else None,
        ):
            try:
                score = float(value)
            except (TypeError, ValueError):
                continue
            if score == score:
                return score
        return None

    @classmethod
    def _normalized_candidate_score(cls, candidate: dict[str, Any], *, rank: int, total: int) -> float:
        """Convert backend-specific retrieval scores into a user-facing 0-1 relevance.

        RRF scores are intentionally tiny (rank-1 maximum is 1 / 61 with the
        default k=60), so exposing them directly makes good hits look like
        "0.0115".  The UI threshold is a 0-1 relevance control, therefore the
        API returns a normalized display score while keeping raw_score for
        debugging.
        """

        raw_score = cls._raw_candidate_score(candidate)
        if (
            candidate.get("retrieval_channel") == "bm25"
            and candidate.get("rrf_score") is None
            and candidate.get("rerank_score") is None
        ):
            if total <= 1:
                return 1.0
            return max(0.0, min(1.0, 1.0 - ((rank - 1) / max(1, total - 1)) * 0.45))

        if raw_score is None:
            return cls._rank_normalized_score(rank=rank, total=total)

        if candidate.get("rrf_score") is not None:
            return max(0.0, min(1.0, raw_score * 61.0))

        if 0.0 <= raw_score <= 1.0:
            return raw_score

        return max(0.0, min(1.0, 1.0 / (1.0 + abs(raw_score))))

    @classmethod
    def _annotate_candidate_scores(cls, candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
        rerank_scores: list[float] = []
        for candidate in candidates:
            if candidate.get("rerank_score") is None:
                continue
            try:
                rerank_scores.append(float(candidate["rerank_score"]))
            except (TypeError, ValueError):
                continue
        rerank_min = min(rerank_scores) if rerank_scores else None
        rerank_max = max(rerank_scores) if rerank_scores else None

        total = len(candidates)
        for index, candidate in enumerate(candidates, start=1):
            raw_score = cls._raw_candidate_score(candidate)
            if candidate.get("rerank_score") is not None:
                try:
                    rerank_score = float(candidate["rerank_score"])
                except (TypeError, ValueError):
                    rerank_score = None
                if (
                    rerank_score is not None
                    and rerank_min is not None
                    and rerank_max is not None
                    and rerank_max > rerank_min
                ):
                    normalized_score = 0.55 + 0.45 * ((rerank_score - rerank_min) / (rerank_max - rerank_min))
                else:
                    normalized_score = cls._rank_normalized_score(rank=index, total=total)
            else:
                normalized_score = cls._normalized_candidate_score(candidate, rank=index, total=total)
            candidate["raw_score"] = raw_score
            candidate["normalized_score"] = normalized_score
            source = candidate.get("source")
            if isinstance(source, dict):
                source["normalized_score"] = normalized_score
                metadata = source.get("metadata")
                if not isinstance(metadata, dict):
                    metadata = {}
                    source["metadata"] = metadata
                metadata["normalized_score"] = normalized_score
                if raw_score is not None:
                    metadata["raw_score"] = raw_score
            image_hit = candidate.get("image_hit")
            if isinstance(image_hit, dict):
                image_hit["normalized_score"] = normalized_score
                if raw_score is not None:
                    image_hit["raw_score"] = raw_score
        return candidates

    @staticmethod
    def _rank_normalized_score(*, rank: int, total: int) -> float:
        if total <= 1:
            return 1.0
        return max(0.0, min(1.0, 1.0 - ((rank - 1) / max(1, total - 1)) * 0.35))

    @staticmethod
    def _candidate_payload(candidate: dict[str, Any], *, rank: int) -> dict[str, Any]:
        return {
            "rank": rank,
            "modality": candidate.get("modality"),
            "title": candidate.get("title"),
            "quote": candidate.get("quote"),
            "score": (candidate.get("source") or {}).get("score"),
            "raw_score": candidate.get("raw_score"),
            "normalized_score": candidate.get("normalized_score"),
            "retrieval_channel": candidate.get("retrieval_channel"),
            "source": candidate.get("source"),
            "image_hit": candidate.get("image_hit"),
        }

    @staticmethod
    def _emit_rag_span(stage: str, payload: dict[str, Any], *, metadata: dict[str, Any] | None = None) -> None:
        try:
            from graph.trace_collector import get_current_trace_collector
        except Exception:
            return
        collector = get_current_trace_collector()
        if collector is None:
            return
        collector.add_rag_span(stage, payload, metadata=metadata)

    @classmethod
    def _rag_candidate_summary(
        cls,
        candidates: list[dict[str, Any]],
        *,
        include_preview: bool = False,
    ) -> list[dict[str, Any]]:
        summary: list[dict[str, Any]] = []
        for rank, candidate in enumerate(candidates, start=1):
            source = candidate.get("source") if isinstance(candidate.get("source"), dict) else {}
            image_hit = candidate.get("image_hit") if isinstance(candidate.get("image_hit"), dict) else None
            source_metadata = source.get("metadata") if isinstance(source.get("metadata"), dict) else {}
            item = {
                "rank": rank,
                "title": candidate.get("title"),
                "section": source_metadata.get("chunk_title") or source_metadata.get("header_path"),
                "modality": candidate.get("modality"),
                "retrieval_channel": candidate.get("retrieval_channel"),
                "retrieval_rank": candidate.get("retrieval_rank"),
                "score": candidate.get("score"),
                "raw_score": candidate.get("raw_score"),
                "normalized_score": candidate.get("normalized_score"),
                "rerank_score": candidate.get("rerank_score"),
                "source_id": source.get("source_id"),
                "document_id": source.get("document_id"),
                "chunk_id": source.get("chunk_id"),
                "uri": source.get("uri"),
                "image_path": image_hit.get("file_path") if image_hit else None,
            }
            if include_preview:
                image_context = (
                    image_hit.get("context")
                    if image_hit and isinstance(image_hit.get("context"), dict)
                    else {}
                )
                preview_value = (
                    image_context.get("snippet")
                    or image_context.get("caption")
                    or image_context.get("heading")
                    or candidate.get("quote")
                    or ""
                )
                preview = " ".join(str(preview_value).split())
                item["preview"] = preview
            summary.append(item)
        return summary

    @staticmethod
    def _embedding_summary(value: Any) -> dict[str, Any]:
        if value is None:
            return {"ok": False, "dimension": 0}
        try:
            dimension = len(value)
        except TypeError:
            dimension = 0
        return {"ok": True, "dimension": dimension}

    def _query_milvus_multimodal_hits(self, query: str, *, top_k: int = 3) -> dict[str, Any] | None:
        config = get_knowledge_multimodal_index_config()
        if not config.get("enabled") or str(config.get("vector_store") or "").lower() != "milvus":
            return None

        from pymilvus import MilvusClient

        from config import get_rag_rerank_config
        from graph.citations import normalize_source
        from llm.embed_client import get_embedding_model
        from llm.multimodal_embedding import get_multimodal_embedding_model

        knowledge_dir = get_knowledge_root(Path(self.base_dir))
        hybrid_config = get_rag_hybrid_config()
        bm25_requested = bool(config.get("bm25_enabled", True) and hybrid_config.get("enabled"))
        text_embed_model = get_embedding_model()
        image_embed_model = get_multimodal_embedding_model()
        embedding_errors: dict[str, str] = {}
        retrieval_errors: dict[str, str] = {}
        text_query_embedding = None
        image_query_embedding = None
        try:
            text_query_embedding = text_embed_model.get_query_embedding(query)
        except Exception as exc:  # noqa: BLE001
            embedding_errors["text"] = f"{type(exc).__name__}: {exc}"
            logger.warning("Text query embedding failed: %s", exc)
        try:
            image_query_embedding = image_embed_model.get_query_embedding(query)
        except Exception as exc:  # noqa: BLE001
            embedding_errors["image"] = f"{type(exc).__name__}: {exc}"
            logger.warning("Image query embedding failed; continuing with text retrieval: %s", exc)

        if text_query_embedding is None and image_query_embedding is None and not bm25_requested:
            raise RuntimeError(f"All query embedding channels failed: {embedding_errors}")

        client = MilvusClient(uri=config.get("milvus_uri", "http://localhost:19530"), timeout=10.0)
        rerank_config = get_rag_rerank_config()
        search_limit = max(top_k, int(hybrid_config.get("candidate_top_k") or top_k))
        text_collection = str(config.get("text_collection") or "puddingclaw_knowledge_text")
        self._emit_rag_span(
            "query",
            {
                "query": query,
                "top_k": top_k,
                "candidate_top_k": search_limit,
                "hybrid_enabled": bool(hybrid_config.get("enabled")),
                "rerank_enabled": bool(rerank_config.get("enabled")),
                "vector_store": "milvus",
                "collections": {
                    "text": text_collection,
                    "image": config.get("image_collection", "puddingclaw_knowledge_image"),
                },
            },
            metadata={"rag_query": query, "top_k": top_k},
        )
        self._emit_rag_span(
            "embedding",
            {
                "text_query_embedding": self._embedding_summary(text_query_embedding),
                "image_query_embedding": self._embedding_summary(image_query_embedding),
                "errors": embedding_errors,
                "raw_vector_recorded": False,
            },
        )

        search_routes: list[dict[str, Any]] = []
        if text_query_embedding is not None:
            search_routes.append({
                "modality": "text",
                "channel": "text_vector",
                "collection": text_collection,
                "data": [text_query_embedding],
                "anns_field": "embedding",
            })
        if bm25_requested:
            search_routes.append({
                "modality": "text",
                "channel": "bm25",
                "collection": text_collection,
                "data": [query],
                "anns_field": "sparse_embedding",
                "search_params": {"params": {"drop_ratio_search": 0.2}},
            })
        if image_query_embedding is not None:
            search_routes.append({
                "modality": "image",
                "channel": "image_vector",
                "collection": config.get("image_collection", "puddingclaw_knowledge_image"),
                "data": [image_query_embedding],
                "anns_field": "embedding",
            })
        text_vector_candidates: list[dict[str, Any]] = []
        image_candidates: list[dict[str, Any]] = []
        bm25_candidates: list[dict[str, Any]] = []

        for route in search_routes:
            modality = str(route["modality"])
            retrieval_channel = str(route["channel"])
            collection = str(route["collection"] or "")
            if not collection or not client.has_collection(collection):
                continue
            search_kwargs = {
                "collection_name": collection,
                "data": route["data"],
                "anns_field": route["anns_field"],
                "limit": search_limit,
                "output_fields": ["text", "_node_content", "_node_type", "doc_id"],
            }
            if route.get("search_params"):
                search_kwargs["search_params"] = route["search_params"]
            try:
                results = client.search(**search_kwargs)
            except Exception as exc:  # noqa: BLE001
                retrieval_errors[retrieval_channel] = f"{type(exc).__name__}: {exc}"
                logger.warning(
                    "Milvus %s retrieval failed; continuing with remaining channels: %s",
                    retrieval_channel,
                    exc,
                )
                continue
            for index, result in enumerate(results[0] if results else []):
                payload, entity = self._entity_payload(result)
                metadata = dict(payload.get("metadata") or {})
                file_path = str(metadata.get("file_path") or metadata.get("path") or "")
                virtual_path = str(metadata.get("virtual_path") or "")
                text = str(payload.get("text") or "")
                title = Path(file_path).name if file_path else (virtual_path or f"{modality} result {index + 1}")
                score = self._score(result)

                if modality == "image":
                    if not file_path and virtual_path.startswith("/knowledge/"):
                        file_path = str((knowledge_dir / virtual_path.removeprefix("/knowledge/")).resolve())
                    context = metadata.get("context") if isinstance(metadata.get("context"), dict) else {}
                    context_snippet = str(context.get("snippet") or context.get("caption") or context.get("heading") or "")
                    quote = f"图片命中：{title}\n路径：{file_path or virtual_path}"
                    if context_snippet:
                        quote += f"\n上下文：{context_snippet}"
                    rerank_document: str | dict[str, Any] = {
                        "text": "\n".join(
                            part
                            for part in [
                                f"图片：{title}",
                                f"上下文：{context_snippet}" if context_snippet else "",
                                f"关联文档：{metadata.get('linked_markdown_virtual_path') or metadata.get('linked_markdown') or ''}",
                            ]
                            if part
                        )
                    }
                else:
                    quote = text.strip() or f"文本命中：{title}"
                    rerank_document = quote

                source = normalize_source({
                    "title": title,
                    "uri": virtual_path or file_path,
                    "document_id": metadata.get("document_id") or metadata.get("doc_id") or file_path or virtual_path,
                    "chunk_id": metadata.get("chunk_id") or entity.get("id") or f"{modality}-{index}",
                    "source_type": "knowledge_image" if modality == "image" else "knowledge_base",
                    "quote": quote,
                    "score": score,
                    "metadata": {
                        "modality": modality,
                        "file_path": file_path,
                        "virtual_path": virtual_path,
                        "linked_markdown": metadata.get("linked_markdown"),
                        "linked_markdown_virtual_path": metadata.get("linked_markdown_virtual_path"),
                        "header_path": metadata.get("header_path"),
                        "chunk_title": metadata.get("chunk_title"),
                        "context": metadata.get("context"),
                    },
                })
                candidate = {
                    "modality": modality,
                    "title": title,
                    "quote": quote,
                    "score": score,
                    "source": source,
                    "rerank_document": rerank_document,
                    "image_hit": {
                        "title": title,
                        "file_path": file_path,
                        "virtual_path": virtual_path,
                        "score": score,
                        "linked_markdown": metadata.get("linked_markdown"),
                        "linked_markdown_virtual_path": metadata.get("linked_markdown_virtual_path"),
                        "context": metadata.get("context"),
                    } if modality == "image" and file_path else None,
                    "retrieval_channel": retrieval_channel,
                    "retrieval_rank": index + 1,
                }
                if modality == "image":
                    image_candidates.append(candidate)
                elif retrieval_channel == "bm25":
                    bm25_candidates.append(candidate)
                else:
                    text_vector_candidates.append(candidate)

        self._emit_rag_span(
            "retrieve.text_vector",
            {
                "channel": "text_vector",
                "candidate_count": len(text_vector_candidates),
                "top_candidates": self._rag_candidate_summary(text_vector_candidates, include_preview=True),
            },
            metadata={"rag_channel": "text_vector", "candidate_count": len(text_vector_candidates)},
        )
        self._emit_rag_span(
            "retrieve.bm25",
            {
                "channel": "bm25",
                "enabled": bm25_requested,
                "candidate_count": len(bm25_candidates),
                "error": retrieval_errors.get("bm25"),
                "top_candidates": self._rag_candidate_summary(bm25_candidates, include_preview=True),
            },
            metadata={"rag_channel": "bm25", "candidate_count": len(bm25_candidates)},
        )
        self._emit_rag_span(
            "retrieve.image_vector",
            {
                "channel": "image_vector",
                "candidate_count": len(image_candidates),
                "top_candidates": self._rag_candidate_summary(image_candidates, include_preview=True),
            },
            metadata={"rag_channel": "image_vector", "candidate_count": len(image_candidates)},
        )

        text_candidates = text_vector_candidates
        if text_vector_candidates and bm25_candidates:
            text_candidates = self._apply_rrf(
                [*text_vector_candidates, *bm25_candidates],
                weights={
                    "text_vector": float(hybrid_config.get("text_vector_weight") or 0.45),
                    "bm25": float(hybrid_config.get("bm25_weight") or 0.2),
                },
            )
            self._emit_rag_span(
                "fusion.text_hybrid",
                {
                    "method": "rrf",
                    "input_counts": {
                        "text_vector": len(text_vector_candidates),
                        "bm25": len(bm25_candidates),
                    },
                    "weights": {
                        "text_vector": float(hybrid_config.get("text_vector_weight") or 0.45),
                        "bm25": float(hybrid_config.get("bm25_weight") or 0.2),
                    },
                    "candidate_count": len(text_candidates),
                    "top_candidates": self._rag_candidate_summary(text_candidates),
                },
                metadata={"rag_stage_kind": "fusion", "candidate_count": len(text_candidates)},
            )
        elif bm25_candidates:
            text_candidates = bm25_candidates

        image_weight = float(hybrid_config.get("image_vector_weight") or 0.35)
        text_group_weight = max(0.0, 1.0 - image_weight)
        grouped_candidates = [
            {**candidate, "retrieval_channel": "text_hybrid", "retrieval_rank": index + 1}
            for index, candidate in enumerate(text_candidates)
        ] + [
            {**candidate, "retrieval_channel": "image_vector", "retrieval_rank": index + 1}
            for index, candidate in enumerate(image_candidates)
        ]

        candidates = grouped_candidates
        if text_candidates and image_candidates:
            candidates = self._apply_rrf(
                grouped_candidates,
                weights={
                    "text_hybrid": text_group_weight,
                    "image_vector": image_weight,
                },
            )
            self._emit_rag_span(
                "fusion.multimodal",
                {
                    "method": "rrf",
                    "input_counts": {
                        "text_hybrid": len(text_candidates),
                        "image_vector": len(image_candidates),
                    },
                    "weights": {
                        "text_hybrid": text_group_weight,
                        "image_vector": image_weight,
                    },
                    "candidate_count": len(candidates),
                    "top_candidates": self._rag_candidate_summary(candidates),
                },
                metadata={"rag_stage_kind": "fusion", "candidate_count": len(candidates)},
            )

        from llm.rerank_client import rerank_documents

        if rerank_config.get("enabled") and len(candidates) > 1:
            rerank_candidate_top_k = int(rerank_config.get("candidate_top_k") or search_limit)
            rerank_candidates = candidates[: max(1, rerank_candidate_top_k)]
            before_rerank = self._rag_candidate_summary(rerank_candidates)
            reranked = rerank_documents(
                query=query,
                documents=[candidate["rerank_document"] for candidate in rerank_candidates],
                top_n=top_k,
            )
            if reranked:
                ordered_candidates = []
                for item in reranked:
                    if 0 <= item.index < len(rerank_candidates):
                        candidate = rerank_candidates[item.index]
                        if item.score is not None:
                            candidate["source"]["score"] = item.score
                            candidate["rerank_score"] = item.score
                        ordered_candidates.append(candidate)
                candidates = ordered_candidates or candidates
            self._emit_rag_span(
                "rerank",
                {
                    "enabled": True,
                    "candidate_count": len(rerank_candidates),
                    "requested_top_n": top_k,
                    "before": before_rerank,
                    "after": self._rag_candidate_summary(candidates),
                    "returned_count": len(reranked),
                },
                metadata={"rag_stage_kind": "rerank", "candidate_count": len(rerank_candidates)},
            )

        selected = candidates[: max(1, top_k)]
        self._annotate_candidate_scores(selected)
        self._annotate_candidate_scores(text_vector_candidates)
        self._annotate_candidate_scores(bm25_candidates)
        self._annotate_candidate_scores(image_candidates)
        chunks = [candidate["quote"] for candidate in selected if candidate["modality"] == "text" and candidate.get("quote")]
        image_hits = [candidate["image_hit"] for candidate in selected if candidate.get("image_hit")]
        sources = [candidate["source"] for candidate in selected]
        self._emit_rag_span(
            "select",
            {
                "selected_count": len(selected),
                "chunks_count": len(chunks),
                "image_hits_count": len(image_hits),
                "sources_count": len(sources),
                "selected": self._rag_candidate_summary(selected),
            },
            metadata={"selected_count": len(selected), "sources_count": len(sources)},
        )
        return {
            "query": query,
            "top_k": top_k,
            "candidate_top_k": search_limit,
            "fusion": {
                "text_vector_weight": float(hybrid_config.get("text_vector_weight") or 0.45),
                "bm25_weight": float(hybrid_config.get("bm25_weight") or 0.2),
                "image_vector_weight": float(hybrid_config.get("image_vector_weight") or 0.35),
                "text_group_weight": max(0.0, 1.0 - float(hybrid_config.get("image_vector_weight") or 0.35)),
                "bm25_enabled": bm25_requested,
                "rerank_enabled": bool(rerank_config.get("enabled")),
                "rerank_top_n": top_k,
                "rerank_candidate_top_k": int(rerank_config.get("candidate_top_k") or search_limit),
            },
            "retrieval": {
                "text_vector": len(text_vector_candidates),
                "bm25": len(bm25_candidates) if hybrid_config.get("enabled") else 0,
                "image_vector": len(image_candidates),
                "selected": len(selected),
                "hybrid_enabled": bool(hybrid_config.get("enabled")),
                "rerank_enabled": bool(rerank_config.get("enabled")),
                "text_collection": text_collection,
                "errors": retrieval_errors,
            },
            "hits": [
                self._candidate_payload(candidate, rank=index + 1)
                for index, candidate in enumerate(selected)
            ],
            "candidate_pools": {
                "text_vector": [
                    self._candidate_payload(candidate, rank=index + 1)
                    for index, candidate in enumerate(text_vector_candidates)
                ],
                "bm25": [
                    self._candidate_payload(candidate, rank=index + 1)
                    for index, candidate in enumerate(bm25_candidates)
                ],
                "image_vector": [
                    self._candidate_payload(candidate, rank=index + 1)
                    for index, candidate in enumerate(image_candidates)
                ],
            },
            "sources": sources,
            "chunks": chunks,
            "image_hits": image_hits,
        }

    def _query_milvus_multimodal(self, query: str, *, top_k: int = 3) -> str | None:
        payload = self._query_milvus_multimodal_hits(query, top_k=top_k)
        if not payload:
            return None

        from graph.citations import encode_tool_result

        chunks = payload.get("chunks") if isinstance(payload.get("chunks"), list) else []
        image_hits = payload.get("image_hits") if isinstance(payload.get("image_hits"), list) else []
        sources = payload.get("sources") if isinstance(payload.get("sources"), list) else []

        sections: list[str] = []
        if chunks:
            sections.append("[文本命中]\n" + "\n\n---\n\n".join(chunks[:top_k]))
        if image_hits:
            lines = []
            task_suggestions = []
            for hit in image_hits[:top_k]:
                context_snippet = (hit.get("context") or {}).get("snippet") or ""
                resource_path = hit.get("virtual_path") or hit.get("file_path") or ""
                lines.append(
                    f"- {hit['title']}\n"
                    f"  图片路径：{hit['file_path']}\n"
                    f"  虚拟路径：{hit.get('virtual_path') or '无'}\n"
                    f"  文档上下文：{context_snippet or '无'}"
                )
                if resource_path:
                    task_suggestions.append({
                        "subagent_type": "image_analyzer",
                        "description": (
                            f"请分析知识库命中的图片：{hit['title']}。\n"
                            f"图片资源路径：{resource_path}\n"
                            f"文档上下文：{context_snippet or '无'}\n\n"
                            f"请先调用 read_resource(resource=\"{resource_path}\") 打开图片，"
                            "然后结合用户问题和文档上下文，返回这张图的结构化分析。"
                        ),
                    })
            sections.append(
                "[图片命中]\n"
                + "\n".join(lines)
                + "\n\n[图片分析任务建议]\n"
                + "如果图片和用户问题相关，主 Agent 应直接调用 native task 工具。"
                  "请只使用 subagent_type 和 description 字段，不要使用 prompt 字段。\n"
                + json.dumps(task_suggestions[:2], ensure_ascii=False, indent=2)
            )
        result = "\n\n".join(sections) if sections else "未找到相关内容。"
        truncated = False
        if len(result) > 6000:
            result = result[:6000] + "\n...[truncated]"
            truncated = True
        encoded = encode_tool_result(result, sources)
        self._emit_rag_span(
            "output",
            {
                "path": "milvus_multimodal",
                "answer_context_chars": len(result),
                "encoded_chars": len(encoded),
                "sources_count": len(sources),
                "chunks_count": len(chunks),
                "image_hits_count": len(image_hits),
                "truncated": truncated,
            },
            metadata={"rag_output_path": "milvus_multimodal", "sources_count": len(sources)},
        )
        return encoded

    def query_structured(self, query: str, *, top_k: int | None = None) -> dict[str, Any]:
        rag_config = get_rag_config()
        effective_top_k = max(1, int(top_k or rag_config.get("top_k") or 3))
        payload = self._query_milvus_multimodal_hits(query, top_k=effective_top_k)
        if payload:
            return payload
        return {
            "query": query,
            "top_k": effective_top_k,
            "candidate_top_k": 0,
            "retrieval": {
                "text_vector": 0,
                "bm25": 0,
                "image_vector": 0,
                "selected": 0,
                "hybrid_enabled": bool(get_rag_hybrid_config().get("enabled")),
                "rerank_enabled": False,
            },
            "hits": [],
            "sources": [],
            "chunks": [],
            "image_hits": [],
        }

    def _run(self, query: str) -> str:
        rag_config = get_rag_config()
        top_k = int(rag_config.get("top_k") or 3)
        index_config = get_knowledge_multimodal_index_config()
        uses_milvus = bool(index_config.get("enabled")) and str(
            index_config.get("vector_store") or ""
        ).lower() == "milvus"

        if uses_milvus:
            try:
                result = self._query_milvus_multimodal(query, top_k=top_k)
                return result or "未找到相关内容。"
            except Exception as exc:  # noqa: BLE001
                logger.exception("Milvus knowledge query failed")
                return (
                    "❌ Milvus knowledge query failed without modifying the index: "
                    f"{type(exc).__name__}: {exc}"
                )

        self._index = self._load_local_index()

        if self._index is None:
            if self._index_error:
                return f"📭 Knowledge base index is unavailable: {self._index_error}. Rebuild it manually."
            return "📭 Knowledge base index is unavailable. Rebuild it manually."

        try:
            from graph.citations import encode_tool_result, normalize_source

            # Use retriever directly so we don't depend on llama-index-llms-openai
            # for response synthesis. The agent LLM can synthesize the raw chunks.
            self._emit_rag_span(
                "query",
                {
                    "query": query,
                    "top_k": top_k,
                    "candidate_top_k": top_k,
                    "hybrid_enabled": False,
                    "rerank_enabled": False,
                    "vector_store": "local_text_index",
                },
                metadata={"rag_query": query, "top_k": top_k},
            )
            retriever = self._index.as_retriever(similarity_top_k=top_k)
            retrieved_nodes = retriever.retrieve(query)

            chunks = []
            sources = []
            for index, item in enumerate(retrieved_nodes):
                node = getattr(item, "node", item)
                metadata = dict(getattr(node, "metadata", {}) or {})
                quote = ""
                try:
                    quote = node.get_content()
                except Exception:
                    quote = getattr(node, "text", "") or ""
                if quote:
                    chunks.append(quote)
                file_name = metadata.get("file_name") or metadata.get("filename")
                file_path = metadata.get("file_path") or metadata.get("source") or ""
                page = metadata.get("page_label") or metadata.get("page")
                document_id = getattr(node, "ref_doc_id", None) or metadata.get("document_id") or file_path
                chunk_id = getattr(node, "node_id", None) or metadata.get("chunk_id") or str(index)
                score = getattr(item, "score", None)
                sources.append(normalize_source({
                    "title": file_name or (Path(file_path).name if file_path else f"知识库来源 {index + 1}"),
                    "uri": file_path,
                    "document_id": document_id,
                    "chunk_id": chunk_id,
                    "source_type": "knowledge_base",
                    "page": page,
                    "quote": quote,
                    "score": score,
                    "metadata": {
                        key: value for key, value in metadata.items()
                        if key not in {"file_path"} and isinstance(value, (str, int, float, bool, type(None)))
                    },
                }))
            fallback_candidates = []
            for index, item in enumerate(retrieved_nodes):
                node = getattr(item, "node", item)
                metadata = dict(getattr(node, "metadata", {}) or {})
                file_path = metadata.get("file_path") or metadata.get("source") or ""
                fallback_candidates.append({
                    "title": metadata.get("file_name") or metadata.get("filename") or (Path(file_path).name if file_path else f"知识库来源 {index + 1}"),
                    "modality": "text",
                    "retrieval_channel": "text_index",
                    "retrieval_rank": index + 1,
                    "score": getattr(item, "score", None),
                    "source": sources[index] if index < len(sources) else {},
                })
            self._emit_rag_span(
                "retrieve.text_index",
                {
                    "path": "fallback_text_index",
                    "query": query,
                    "top_k": top_k,
                    "candidate_count": len(retrieved_nodes),
                    "top_candidates": self._rag_candidate_summary(fallback_candidates, include_preview=True),
                },
                metadata={"rag_channel": "text_index", "candidate_count": len(retrieved_nodes)},
            )

            result = "\n\n---\n\n".join(chunks) if chunks else "未找到相关内容。"
            truncated = False
            if len(result) > 5000:
                result = result[:5000] + "\n...[truncated]"
                truncated = True
            encoded = encode_tool_result(result, sources)
            self._emit_rag_span(
                "output",
                {
                    "path": "fallback_text_index",
                    "answer_context_chars": len(result),
                    "encoded_chars": len(encoded),
                    "sources_count": len(sources),
                    "chunks_count": len(chunks),
                    "image_hits_count": 0,
                    "truncated": truncated,
                },
                metadata={"rag_output_path": "fallback_text_index", "sources_count": len(sources)},
            )
            return encoded
        except Exception as e:
            return f"❌ Search error: {str(e)}"


def create_search_knowledge_tool(base_dir: Path) -> list[BaseTool]:
    return [LlamaIndexKnowledgeQueryTool(base_dir=str(base_dir))]
